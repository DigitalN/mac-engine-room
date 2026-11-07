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
/// After giving the fans up, or failing to hand them back, the helper keeps looking this long at
/// whether they're still at its own speeds (`Fans.recheck`).
let recheckWindow: TimeInterval = 10
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
    /// What each fan's target has been since the helper first set it, oldest first, with when each
    /// speed stopped being the target (nil: it still is). A written target can read back late, as
    /// the speed from before the write (as the modes do; not measured for targets, but #48 looks
    /// like it), and that speed may have been set long before, so what counts is when it was
    /// replaced: a read of the current speed, or of one replaced in the last `modeSettle`, is the
    /// helper's own, not another app's. The target macOS left before the helper's first write is
    /// replaced by that write like any other. Empty for a fan the helper hasn't written since the
    /// fans were last with macOS: a fan in manual mode isn't the helper's until it has set it.
    private var written: [[(rpm: Float, replaced: Date?)]]
    private var asleep = false
    /// When the helper stops looking at fans it gave up or couldn't hand back (`recheck`).
    private var recheckUntil = Date.distantPast
    /// The first unlock on M1–M4 can take several seconds (ThermalForge #69): keep trying this long.
    private let unlockPatience: TimeInterval
    /// The time the speeds are dated by; the tests move it by hand.
    private let clock: () -> Date

    init?(smc: SMCConnection, marker: String, unlockPatience: TimeInterval = 30, clock: @escaping () -> Date = Date.init) {
        // A Mac that doesn't say in FNum: the fans with a speed key, F0Ac, F1Ac… in a row
        // (ThermalForge #2), as the app counts them.
        let listed = Int(smc.read("FNum")?.first ?? 0)
        let n = listed > 0 ? listed : (0..<10).prefix { smc.read("F\($0)Ac") != nil }.count
        guard n > 0 else { return nil }
        self.smc = smc
        self.marker = marker
        self.unlockPatience = unlockPatience
        self.clock = clock
        count = n
        modeKeys = (0..<count).map { smc.read("F\($0)md") != nil ? "F\($0)md" : "F\($0)Md" }  // lowercase from M5
        limits = (0..<count).map { (smc.readFloat("F\($0)Mn") ?? 0, smc.readFloat("F\($0)Mx") ?? 0) }
        hasForceTest = smc.read("Ftst") != nil
        written = Array(repeating: [], count: n)
    }

    var holding: Bool { lock.withLock { held } }

    private func mode(_ fan: Int) -> UInt8? { smc.read(modeKeys[fan])?.first }

    /// A fan's target as the SMC reports it now; nil if it can't be read.
    private func target(_ fan: Int) -> Float? { smc.readFloat("F\(fan)Tg").flatMap { $0.isFinite ? $0 : nil } }

    /// Whether a target read back is a speed the helper set: the current one, or one it replaced in
    /// the last `modeSettle` (a write can read back late, as the speed from before it, however long
    /// ago that was set). Within 1 rpm. Another app that sets a fan to exactly such a speed can't be
    /// told apart from the helper, and counts as it: the fan's minimum is the likely case.
    private func isOwn(_ fan: Int, _ rpm: Float) -> Bool {
        let now = clock()
        return written[fan].contains { abs($0.rpm - rpm) <= 1 && ($0.replaced.map { now.timeIntervalSince($0) <= modeSettle } ?? true) }
    }

    /// Whether a fan is in manual mode at a speed the helper didn't set, so it's another app's. A
    /// fan in manual mode whose target can't be read isn't known to be the helper's.
    private func foreign() -> Bool {
        (0..<count).contains { fan in mode(fan) == 1 && !(target(fan).map { isOwn(fan, $0) } ?? false) }
    }

    /// Whether the helper doesn't hold the fans but some are still in manual mode, all at speeds it
    /// set: it gave them up when it shouldn't have, or couldn't hand them back. Still its to hand back.
    private func leftOver() -> Bool {
        !held && (0..<count).contains { mode($0) == 1 } && !foreign()
    }

    /// Whether `Ftst` is set, keeping macOS off the fans even with every mode back on auto.
    private var forced: Bool { hasForceTest && smc.read("Ftst")?.first != 0 }

    /// Holds each fan at its speed (the last one given for fans past the list). Returns the
    /// speeds set, after clamping.
    func hold(_ requested: [Float]) throws -> [Float] {
        try lock.withLock {
            guard !asleep else { throw Failure.asleep }
            guard limits.allSatisfy({ $0.min > 0 && $0.max > $0.min }) else { throw Failure.limits }
            let read = (0..<count).map { target($0) }
            if held, (0..<count).contains(where: { fan in read[fan].map { !isOwn(fan, $0) } ?? false }) {
                held = false  // another app wrote a speed: the fans are its now
                unlink(marker)
                let now = clock()
                recheckUntil = now.addingTimeInterval(recheckWindow)  // unless the read was only late: see `recheck`
                let past = written.map { sets in sets.map { (rpm: $0.rpm, replacedAgo: $0.replaced.map { now.timeIntervalSince($0) }) } }
                log.notice("Another app set a fan speed; letting it have the fans (\(Self.describe(read: read, wrote: past), privacy: .public))")
                throw Failure.otherApp
            }
            if !held, foreign() { throw Failure.otherApp }
            let again = !held && (0..<count).contains { mode($0) == 1 }  // in manual mode, at speeds it set
            let targets = (0..<count).map { fan in
                min(max(requested[min(fan, requested.count - 1)], limits[fan].min), limits[fan].max)
            }
            let taking = !held || Date().timeIntervalSince(heldSince) > modeSettle
            let unlock = (0..<count).filter { !held || (taking && mode($0) != 1) }
            do {
                if !unlock.isEmpty {
                    try unlockForceTest()
                    try writeTargets(targets, replacing: read)  // before the mode: the target macOS leaves is often 0
                }
                for fan in unlock { try unlockFan(fan) }
                try writeTargets(targets, replacing: unlock.isEmpty ? read : Array(repeating: nil, count: count))
            } catch {
                if !unlock.isEmpty { _ = releaseLocked("a write failed") }
                throw error
            }
            if !held {
                held = true
                heldSince = Date()
                FileManager.default.createFile(atPath: marker, contents: nil)
                if again {
                    log.notice("Holding the fans again at \(targets.map { Int($0) }, privacy: .public) rpm; they were still at the speeds the helper set")
                } else {
                    log.notice("Holding the fans at \(targets.map { Int($0) }, privacy: .public) rpm")
                }
            }
            return targets
        }
    }

    /// `before` is what each target read just before: the first write replaces what macOS left.
    private func writeTargets(_ targets: [Float], replacing before: [Float?]) throws {
        for fan in 0..<count {
            guard smc.writeFloat("F\(fan)Tg", targets[fan]) else {
                log.error("Couldn't set fan \(fan) to \(Int(targets[fan])) rpm")
                throw Failure.write
            }
            remember(fan, targets[fan], replacing: before[fan])
        }
    }

    /// Notes that a fan's target is now `rpm`. What it was until now stops being the target: the
    /// helper's last speed, and `before` if that was none of the helper's (what macOS left, often 0).
    /// Speeds replaced too long ago to be read back late are forgotten.
    private func remember(_ fan: Int, _ rpm: Float, replacing before: Float?) {
        let now = clock()
        if let before, !isOwn(fan, before) { written[fan].append((before, nil)) }
        for i in written[fan].indices where written[fan][i].replaced == nil { written[fan][i].replaced = now }
        written[fan].removeAll { $0.replaced.map { now.timeIntervalSince($0) > modeSettle } ?? false }
        written[fan].append((rpm, nil))
    }

    /// For the log: what each fan's target read, and what it had been since the helper first set it
    /// (oldest first): each speed with how long ago it was replaced, the last "since".
    static func describe(read: [Float?], wrote: [[(rpm: Float, replacedAgo: TimeInterval?)]]) -> String {
        func rpm(_ speed: Float) -> String { String(format: "%.0f", speed) }
        return read.indices.map { fan in
            let past = wrote[fan].map { rpm($0.rpm) + ($0.replacedAgo.map { String(format: " until %.1f s ago", max(0, $0)) } ?? " since") }
            return "fan \(fan) read \(read[fan].map { rpm($0) + " rpm" } ?? "nothing"), had written \(past.isEmpty ? "nothing" : past.joined(separator: ", "))"
        }.joined(separator: "; ")
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

    /// Hands the fans back to macOS if they're the helper's: held, or left in manual mode at speeds
    /// it set. Returns whether every fan left manual mode and Ftst is clear (true if there was
    /// nothing to hand back; another app's fans are left alone).
    @discardableResult
    func release(_ why: String) -> Bool {
        lock.withLock { releaseIfOurs(why) }
    }

    private func releaseIfOurs(_ why: String) -> Bool {
        if !held {
            guard leftOver() else { return true }
            log.notice("The fans are still at the speeds the helper set")
        }
        return releaseLocked(why)
    }

    /// For the watchdog, every second. A read that is only late can make the helper's own fans look
    /// like another app's, and it decides that once, in `hold`. So after it gives the fans up, or
    /// fails to hand them back, it keeps looking for `recheckWindow`: once the target has settled
    /// they read as its own speeds, and it hands them back. Fans at any other speed are left alone.
    /// Reads nothing when it hasn't given anything up. Returns whether it is still looking, so the
    /// helper doesn't quit before it has looked.
    func recheck() -> Bool {
        lock.withLock {
            guard !held, clock() < recheckUntil else { return false }
            _ = releaseIfOurs("looking again at fans it had given up")
            return !held && clock() < recheckUntil
        }
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
            written = Array(repeating: [], count: count)  // nothing of the helper's is on them now
            recheckUntil = .distantPast
        } else if !manual.isEmpty {
            log.error("Fans \(manual, privacy: .public) still in manual after handing back (\(why, privacy: .public))")
            recheckUntil = clock().addingTimeInterval(recheckWindow)  // they're at its speeds still: try again
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
            _ = releaseIfOurs("the Mac is going to sleep")
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
            let other = !held && foreign()
            return "{\"fans\": [\(fans.joined(separator: ", "))], \"holding\": \(held), \"other\": \(other)}"
        }
    }
}
