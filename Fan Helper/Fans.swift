//  The fans: holding them at a speed and handing them back to macOS, with the checks that
//  keep that safe. The unlock follows ThermalForge's (MIT; see THIRD-PARTY-NOTICES). Kept out
//  of main.swift so tests/fan_helper can run it against a simulated SMC.

import Foundation

/// After handing back, the modes are watched this long, and the hand-back repeated for any
/// fan still in manual (ThermalForge #76).
let handBackWatch: TimeInterval = 10
/// A mode write reads back as the old mode for up to a second (measured on an M1 Pro). A fan
/// still not in the mode written after this gets the write again.
let modeSettle: TimeInterval = 2
/// After Ftst = 1, a moment for macOS's thermalmonitord to let go of the fans (ThermalForge's).
let forceTestSettle: TimeInterval = 0.5

enum Failure: String, Error {
    case asleep, otherApp = "other-app", unlock, write, limits
}

final class Fans {
    let count: Int
    private let smc: SMCConnection
    private let lock = NSLock()
    private let modeKeys: [String]
    private let limits: [(min: Float, max: Float)]
    /// Exists while the fans are held, so a helper that quit while holding them (it never
    /// should) can tell, the next time it starts, that the manual fans are its own.
    private let marker: String
    /// Macs with an `Ftst` key (M1–M4 by ThermalForge's account, though this M1 Pro has none)
    /// only take a manual mode while it's 1: it keeps macOS's thermalmonitord off the fans, so
    /// it goes back to 0 with them. Without it, every mode write is refused (the M4).
    private let hasForceTest: Bool
    private var held = false
    private var heldSince = Date.distantPast
    private var written: [Float]
    private var asleep = false
    /// The first unlock on M1–M4 can take several seconds (ThermalForge #69): keep trying this long.
    private let unlockPatience: TimeInterval

    init?(smc: SMCConnection, marker: String, unlockPatience: TimeInterval = 30) {
        guard let n = smc.read("FNum")?.first, n > 0 else { return nil }
        self.smc = smc
        self.marker = marker
        self.unlockPatience = unlockPatience
        count = Int(n)
        modeKeys = (0..<count).map { smc.read("F\($0)md") != nil ? "F\($0)md" : "F\($0)Md" }  // lowercase from M5
        limits = (0..<count).map { (smc.readFloat("F\($0)Mn") ?? 0, smc.readFloat("F\($0)Mx") ?? 0) }
        hasForceTest = smc.read("Ftst") != nil
        written = limits.map { $0.min }
    }

    var holding: Bool { lock.withLock { held } }

    private func mode(_ fan: Int) -> UInt8? { smc.read(modeKeys[fan])?.first }

    /// Whether `Ftst` is set, keeping macOS off the fans even with every mode back on auto.
    private var forced: Bool { hasForceTest && smc.read("Ftst")?.first != 0 }

    /// Holds each fan at its speed (the last one given for fans past the list). Returns the
    /// speeds set, after clamping.
    func hold(_ requested: [Float]) throws -> [Float] {
        try lock.withLock {
            guard !asleep else { throw Failure.asleep }
            guard limits.allSatisfy({ $0.min > 0 && $0.max > $0.min }) else { throw Failure.limits }
            if held, (0..<count).contains(where: { abs((smc.readFloat("F\($0)Tg") ?? written[$0]) - written[$0]) > 1 }) {
                held = false  // another app wrote a speed: the fans are its now
                unlink(marker)
                log.notice("Another app set a fan speed; letting it have the fans")
                throw Failure.otherApp
            }
            if !held, (0..<count).contains(where: { mode($0) == 1 }) { throw Failure.otherApp }
            let targets = (0..<count).map { fan in
                min(max(requested[min(fan, requested.count - 1)], limits[fan].min), limits[fan].max)
            }
            let taking = !held || Date().timeIntervalSince(heldSince) > modeSettle
            let unlock = (0..<count).filter { !held || (taking && mode($0) != 1) }
            do {
                if !unlock.isEmpty {
                    try unlockForceTest()
                    try writeTargets(targets)  // before the mode: the target macOS leaves is often 0
                }
                for fan in unlock { try unlockFan(fan) }
                try writeTargets(targets)
            } catch {
                if !unlock.isEmpty { _ = releaseLocked("a write failed") }
                throw error
            }
            if !held {
                held = true
                heldSince = Date()
                FileManager.default.createFile(atPath: marker, contents: nil)
                log.notice("Holding the fans at \(targets.map { Int($0) }, privacy: .public) rpm")
            }
            return targets
        }
    }

    private func writeTargets(_ targets: [Float]) throws {
        for fan in 0..<count {
            guard smc.writeFloat("F\(fan)Tg", targets[fan]) else {
                log.error("Couldn't set fan \(fan) to \(Int(targets[fan])) rpm")
                throw Failure.write
            }
            written[fan] = targets[fan]
        }
    }

    /// Ftst = 1 before any mode write, where the Mac has it. The marker goes first, so a helper
    /// that dies between the two still hands back (and clears Ftst) when it next starts.
    private func unlockForceTest() throws {
        guard hasForceTest, !forced else { return }
        if !held { FileManager.default.createFile(atPath: marker, contents: nil) }
        guard smc.write("Ftst", [1]) else {
            log.error("Couldn't set Ftst, which this Mac needs before a fan can go into manual mode")
            throw Failure.unlock
        }
        Thread.sleep(forTimeInterval: forceTestSettle)
    }

    private func unlockFan(_ fan: Int) throws {
        let start = Date()
        while !smc.write(modeKeys[fan], [1]) {
            guard Date().timeIntervalSince(start) < unlockPatience else {
                let ftst = hasForceTest ? (smc.read("Ftst")?.first).map(String.init) ?? "unreadable" : "none"
                log.error("Fan \(fan) didn't go into manual mode (mode \(self.mode(fan).map(String.init) ?? "unreadable", privacy: .public), Ftst \(ftst, privacy: .public))")
                throw Failure.unlock
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    /// Hands the fans back to macOS. Returns whether every fan left manual mode and Ftst is clear.
    @discardableResult
    func release(_ why: String) -> Bool {
        lock.withLock { held ? releaseLocked(why) : true }
    }

    /// Every fan is tried, and every write checked. A fan whose mode write fails keeps its
    /// last speed, never 0 (ThermalForge #72, #30). Then Ftst goes back to 0, where the Mac has
    /// it, so thermalmonitord runs the fans again. For up to 10 seconds, any fan still in manual,
    /// or Ftst still set, gets the hand-back again.
    private func releaseLocked(_ why: String) -> Bool {
        for fan in 0..<count where !smc.write(modeKeys[fan], [0]) {
            log.error("Couldn't hand fan \(fan) back to macOS")
        }
        if hasForceTest && !smc.write("Ftst", [0]) { log.error("Couldn't clear Ftst") }
        held = false
        unlink(marker)
        let start = Date()
        var manual = (0..<count).filter { mode($0) == 1 }
        var stillForced = forced
        while (!manual.isEmpty || stillForced) && Date().timeIntervalSince(start) < handBackWatch {
            Thread.sleep(forTimeInterval: 0.5)
            manual = (0..<count).filter { mode($0) == 1 }
            stillForced = forced
            if Date().timeIntervalSince(start) >= modeSettle {
                for fan in manual { _ = smc.write(modeKeys[fan], [0]) }
                if stillForced { _ = smc.write("Ftst", [0]) }
            }
        }
        if manual.isEmpty && !stillForced {
            log.notice("Fans back with macOS (\(why, privacy: .public))")
        } else if !manual.isEmpty {
            log.error("Fans \(manual, privacy: .public) still in manual after handing back (\(why, privacy: .public))")
        } else {
            log.error("Ftst still set after handing back (\(why, privacy: .public)), so macOS can't run the fans")
        }
        return manual.isEmpty && !stillForced
    }

    /// At launch: fans left in manual by an earlier run of this helper go back to macOS.
    func releaseLeftovers() {
        guard FileManager.default.fileExists(atPath: marker) else { return }
        lock.withLock {
            held = true
            _ = releaseLocked("left held by an earlier run")
        }
    }

    func willSleep() {
        lock.withLock {
            asleep = true
            if held { _ = releaseLocked("the Mac is going to sleep") }
        }
    }

    func didWake() {
        lock.withLock { asleep = false }  // the app takes the fans again from the temperatures now
    }

    func status() -> String {
        lock.withLock {
            let fans = (0..<count).map { fan in
                let rpm = Int(smc.readFloat("F\(fan)Ac") ?? 0), target = Int(smc.readFloat("F\(fan)Tg") ?? 0)
                return "{\"rpm\": \(rpm), \"target\": \(target), \"min\": \(Int(limits[fan].min)), "
                    + "\"max\": \(Int(limits[fan].max)), \"mode\": \(mode(fan).map(String.init) ?? "null")}"
            }
            let other = !held && (0..<count).contains { mode($0) == 1 }
            return "{\"fans\": [\(fans.joined(separator: ", "))], \"holding\": \(held), \"other\": \(other)}"
        }
    }
}
