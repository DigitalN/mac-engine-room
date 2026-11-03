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
//  fan). The fans themselves, and the unlock, are in Fans.swift.

import Foundation
import IOKit.pwr_mgt
import Security
import os

let log = Logger(subsystem: "com.pythonflow.monitor", category: "fans")

/// The fans go back to macOS when the app has said nothing for this long while holding them.
let checkInTimeout: TimeInterval = 10
/// With the fans back with macOS and nobody connected, the helper exits after this long.
let idleTimeout: TimeInterval = 10

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
