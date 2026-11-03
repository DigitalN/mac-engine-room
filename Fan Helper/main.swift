//  Mac Engine Room's fan helper: the only part of the app that runs as root.
//
//  Not Python: the app bundle is writable by the user, and Python reads its code with
//  ordinary file reads that nothing re-checks. launchd starts this on the first connection
//  to its socket (see com.pythonflow.monitor.fans.plist), and it exits once the fans are
//  back with macOS and nobody is connected. It only answers the signed app, one command per
//  line:
//
//    set <rpm> <rpm> …   hold each fan at a speed, clamped to its minimum and maximum
//    auto                hand the fans back to macOS
//    checkin             still here; "ok holding", or "ok released" if they went back
//    status              the fans, and whether this or another app holds them
//
//  Replies are "ok …" or "error <reason>". The safety nets live here, so they work even if
//  the app hangs or crashes: the fans go back to macOS when the app disconnects, when it
//  stops checking in, and before the Mac sleeps. Nothing is written while another app has
//  the fans in manual mode, and a target of 0 is never written (in manual mode it stops the
//  fan). The fan unlock follows ThermalForge's (MIT; see THIRD-PARTY-NOTICES).

import Foundation
import IOKit.pwr_mgt
import Security
import os

let log = Logger(subsystem: "com.pythonflow.monitor", category: "fans")

/// The fans go back to macOS when the app has said nothing for this long while holding them.
let checkInTimeout: TimeInterval = 10
/// With the fans back with macOS and nobody connected, the helper exits after this long.
let idleTimeout: TimeInterval = 10
/// After handing back, the modes are watched this long, and the hand-back repeated for any
/// fan still in manual (ThermalForge #76).
let handBackWatch: TimeInterval = 10
/// A mode write reads back as the old mode for up to a second (measured on an M1 Pro). A fan
/// still not in the mode written after this gets the write again.
let modeSettle: TimeInterval = 2
/// The first unlock on M1–M4 can take several seconds (ThermalForge #69): keep trying this long.
let unlockPatience: TimeInterval = 30

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
    private var held = false
    private var heldSince = Date.distantPast
    private var written: [Float]
    private var asleep = false

    init?(smc: SMCConnection, marker: String) {
        guard let n = smc.read("FNum")?.first, n > 0 else { return nil }
        self.smc = smc
        self.marker = marker
        count = Int(n)
        modeKeys = (0..<count).map { smc.read("F\($0)md") != nil ? "F\($0)md" : "F\($0)Md" }  // lowercase from M5
        limits = (0..<count).map { (smc.readFloat("F\($0)Mn") ?? 0, smc.readFloat("F\($0)Mx") ?? 0) }
        written = limits.map { $0.min }
    }

    var holding: Bool { lock.withLock { held } }

    private func mode(_ fan: Int) -> UInt8? { smc.read(modeKeys[fan])?.first }

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
                // The speed first: the target a fan has while macOS runs it is often 0.
                if !unlock.isEmpty { try writeTargets(targets) }
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

    private func unlockFan(_ fan: Int) throws {
        let start = Date()
        while !smc.write(modeKeys[fan], [1]) {
            guard Date().timeIntervalSince(start) < unlockPatience else {
                log.error("Fan \(fan) didn't go into manual mode")
                throw Failure.unlock
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    /// Hands the fans back to macOS. Returns whether every fan left manual mode.
    @discardableResult
    func release(_ why: String) -> Bool {
        lock.withLock { held ? releaseLocked(why) : true }
    }

    /// Every fan is tried, and every write checked. A fan whose mode write fails keeps its
    /// last speed, never 0 (ThermalForge #72, #30). Then, for up to 10 seconds, any fan still
    /// in manual gets the hand-back again.
    private func releaseLocked(_ why: String) -> Bool {
        for fan in 0..<count where !smc.write(modeKeys[fan], [0]) {
            log.error("Couldn't hand fan \(fan) back to macOS")
        }
        held = false
        unlink(marker)
        let start = Date()
        var manual = (0..<count).filter { mode($0) == 1 }
        while !manual.isEmpty && Date().timeIntervalSince(start) < handBackWatch {
            Thread.sleep(forTimeInterval: 0.5)
            manual = (0..<count).filter { mode($0) == 1 }
            if Date().timeIntervalSince(start) >= modeSettle {
                for fan in manual { _ = smc.write(modeKeys[fan], [0]) }
            }
        }
        if manual.isEmpty {
            log.notice("Fans back with macOS (\(why, privacy: .public))")
        } else {
            log.error("Fans \(manual, privacy: .public) still in manual after handing back (\(why, privacy: .public))")
        }
        return manual.isEmpty
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

final class Server {
    private let fans: Fans
    private let requirement: SecRequirement?
    private let lock = NSLock()
    private var clients = 0
    private var owner: Int32 = -1  // the connection that holds the fans
    private var lastWord = Date()
    private var lastSeen = Date()

    init(fans: Fans, requirement: SecRequirement?) {
        self.fans = fans
        self.requirement = requirement
    }

    func accept(on listener: Int32) {
        while true {
            let fd = Darwin.accept(listener, nil, nil)
            if fd < 0 { continue }
            Thread.detachNewThread { self.serve(fd) }
        }
    }

    /// The caller must be the app this helper belongs to, signed by the same team.
    private func allowed(_ fd: Int32) -> Bool {
        guard let requirement else { return false }
        var token = audit_token_t()
        var size = socklen_t(MemoryLayout<audit_token_t>.size)
        guard getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &size) == 0 else { return false }
        let attributes = [kSecGuestAttributeAudit: withUnsafeBytes(of: &token) { Data($0) }] as CFDictionary
        var code: SecCode?
        guard SecCodeCopyGuestWithAttributes(nil, attributes, [], &code) == errSecSuccess, let code else { return false }
        return SecCodeCheckValidity(code, [], requirement) == errSecSuccess
    }

    private func serve(_ fd: Int32) {
        defer { close(fd) }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))  // ThermalForge #71
        guard allowed(fd) else {
            log.error("Refused a connection from an app other than Mac Engine Room")
            _ = reply(fd, "error not-allowed")
            return
        }
        lock.withLock { clients += 1; lastSeen = Date() }
        defer { disconnected(fd) }
        var pending = [UInt8]()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while true {
            let n = read(fd, &buffer, buffer.count)
            guard n > 0 else { return }
            pending += buffer[0..<n]
            while let newline = pending.firstIndex(of: 10) {
                let line = String(decoding: pending[..<newline], as: UTF8.self)
                pending.removeSubrange(...newline)
                guard reply(fd, handle(line, from: fd)) else { return }
            }
            guard pending.count < 4096 else { return }
        }
    }

    private func disconnected(_ fd: Int32) {
        let wasOwner = lock.withLock {
            clients -= 1
            lastSeen = Date()
            defer { if owner == fd { owner = -1 } }
            return owner == fd
        }
        if wasOwner { fans.release("Mac Engine Room disconnected") }
    }

    private func handle(_ line: String, from fd: Int32) -> String {
        lock.withLock { lastWord = Date() }
        let words = line.split(separator: " ")
        switch words.first {
        case "set":
            let rpms = words.dropFirst().compactMap { Float($0) }.filter { $0.isFinite }
            guard !rpms.isEmpty, rpms.count == words.count - 1 else { return "error bad-request" }
            do {
                let set = try fans.hold(rpms)
                lock.withLock { owner = fd }
                return "ok " + set.map { String(Int($0.rounded())) }.joined(separator: " ")
            } catch let failure as Failure {
                return "error \(failure.rawValue)"
            } catch {
                return "error write"
            }
        case "auto":
            return fans.release("Mac Engine Room asked") ? "ok" : "error still-manual"
        case "checkin":
            return fans.holding ? "ok holding" : "ok released"
        case "status":
            return "ok " + fans.status()
        default:
            return "error unknown-command"
        }
    }

    private func reply(_ fd: Int32, _ text: String) -> Bool {
        let bytes = Array((text + "\n").utf8)
        var sent = 0
        while sent < bytes.count {
            let n = bytes[sent...].withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            if n <= 0 { return false }
            sent += n
        }
        return true
    }

    /// Hands the fans back when the app stops checking in, and quits when there's nothing to do.
    func watch() {
        while true {
            Thread.sleep(forTimeInterval: 1)
            let (silent, idle) = lock.withLock {
                (Date().timeIntervalSince(lastWord), clients == 0 ? Date().timeIntervalSince(lastSeen) : 0)
            }
            if fans.holding && silent > checkInTimeout {
                fans.release("no word from Mac Engine Room for \(Int(checkInTimeout)) seconds")
            } else if !fans.holding && idle > idleTimeout {
                exit(0)  // launchd starts it again on the next connection
            }
        }
    }
}

/// `identifier "<this app>" and anchor apple generic and certificate leaf[subject.OU] = "<team>"`,
/// with this helper's own team. None when it isn't signed by a team (an ad hoc build), so
/// nobody gets in.
func callerRequirement() -> SecRequirement? {
    var me: SecCode?
    var staticCode: SecStaticCode?
    var info: CFDictionary?
    guard let app = Bundle.main.bundleIdentifier,
          SecCodeCopySelf([], &me) == errSecSuccess, let me,
          SecCodeCopyStaticCode(me, [], &staticCode) == errSecSuccess, let staticCode,
          SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
          let team = (info as? [String: Any])?[kSecCodeInfoTeamIdentifier as String] as? String
    else { return nil }
    let text = "identifier \"\(app)\" and anchor apple generic and certificate leaf[subject.OU] = \"\(team)\""
    var requirement: SecRequirement?
    return SecRequirementCreateWithString(text as CFString, [], &requirement) == errSecSuccess ? requirement : nil
}

// ── launch ──

let appID = Bundle.main.bundleIdentifier ?? "com.pythonflow.monitor"
guard let smc = SMCConnection(), let fans = Fans(smc: smc, marker: "/var/run/\(appID).fans.holding") else {
    log.error("No fans to control on this Mac")
    exit(1)
}
var sockets = UnsafeMutablePointer<Int32>.allocate(capacity: 1)  // launchd replaces it with its own array
var socketCount = 0
guard launch_activate_socket("Listener", &sockets, &socketCount) == 0, socketCount > 0 else {
    log.error("Not started by launchd")
    exit(1)
}
let requirement = callerRequirement()
if requirement == nil { log.error("Not signed by a team, so no app can use the fans") }
fans.releaseLeftovers()
let server = Server(fans: fans, requirement: requirement)
Thread.detachNewThread { server.accept(on: sockets[0]) }
Thread.detachNewThread { server.watch() }

// The fans go back to macOS before the Mac sleeps. After it wakes, the app takes them again
// from the temperatures then, never an old speed (ThermalForge #48).
final class SleepWatch {
    private let fans: Fans
    private var port: io_connect_t = 0

    init(fans: Fans) {
        self.fans = fans
    }

    func start() {
        var notifyPort: IONotificationPortRef?
        var notifier: io_object_t = 0
        port = IORegisterForSystemPower(Unmanaged.passUnretained(self).toOpaque(), &notifyPort, { refcon, _, message, argument in
            guard let refcon else { return }
            Unmanaged<SleepWatch>.fromOpaque(refcon).takeUnretainedValue().handle(message, argument)
        }, &notifier)
        guard port != 0, let notifyPort else {
            log.error("Couldn't watch for sleep")
            return
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), IONotificationPortGetRunLoopSource(notifyPort).takeUnretainedValue(), .defaultMode)
    }

    private func handle(_ message: UInt32, _ argument: UnsafeMutableRawPointer?) {
        let canSleep: UInt32 = 0xe000_0270, willSleep: UInt32 = 0xe000_0280, poweredOn: UInt32 = 0xe000_0300
        switch message {
        case willSleep:
            fans.willSleep()
            IOAllowPowerChange(port, Int(bitPattern: argument))
        case canSleep:
            IOAllowPowerChange(port, Int(bitPattern: argument))
        case poweredOn:
            fans.didWake()
        default:
            break
        }
    }
}

let sleepWatch = SleepWatch(fans: fans)
sleepWatch.start()
CFRunLoopRun()
