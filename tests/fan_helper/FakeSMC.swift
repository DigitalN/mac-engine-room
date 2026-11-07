//  A stand-in for SMC.swift's SMCConnection, compiled with Fans.swift in its place, that
//  behaves like one of two Macs:
//
//    m4      has Ftst; refuses a manual mode until Ftst is 1, as thermalmonitord does
//    m1pro   has no Ftst; takes a manual mode at once (measured on the maintainer's M1 Pro)
//
//  Every write is recorded, so the tests can check the order things happened in. A fan's
//  target can be made to read back late (`targetLag`).

import Foundation

final class SMCConnection {
    var keys: [String: [UInt8]]
    var writes: [(key: String, bytes: [UInt8])] = []
    var refuse: Set<String> = []  // keys whose writes fail
    var thermalmonitordYields = true  // m4: whether Ftst = 1 frees the fans
    /// Seconds a target written through `write` reads back as what it held before, as the modes
    /// do on the M1 Pro (up to a second): a read shows the latest write at least this old. 0
    /// reads back at once. `keys` always holds what the target really is.
    var targetLag: TimeInterval = 0
    /// The time the lag is counted in; the tests move it by hand, along with the helper's.
    var clock: () -> Date = Date.init
    /// Reads that showed a target other than the one it really is.
    private(set) var lateReads = 0
    /// Every read, to check that the helper reads nothing when it has nothing to look at.
    private(set) var reads = 0
    private var targetWrites: [String: [(at: Date, bytes: [UInt8])]] = [:]

    init(model: String, fans: Int = 2) {
        keys = ["FNum": [UInt8(fans)]]
        for fan in 0..<fans {
            keys["F\(fan)Md"] = [3]
            keys["F\(fan)Mn"] = Self.bytes(1499)
            keys["F\(fan)Mx"] = Self.bytes(fan == 0 ? 4296 : 4744)
            keys["F\(fan)Tg"] = Self.bytes(0)
            keys["F\(fan)Ac"] = Self.bytes(0)
        }
        if model == "m4" { keys["Ftst"] = [0] }
    }

    func read(_ key: String) -> [UInt8]? {
        reads += 1
        guard targetLag > 0, let seen = targetWrites[key] else { return keys[key] }
        let cutoff = clock().addingTimeInterval(-targetLag)
        let shown = seen.last { $0.at <= cutoff }?.bytes ?? seen[0].bytes
        if shown != keys[key] { lateReads += 1 }
        return shown
    }

    func write(_ key: String, _ bytes: [UInt8]) -> Bool {
        guard keys[key] != nil, !refuse.contains(key) else { return false }
        if key.hasSuffix("Md"), bytes.first == 1, let ftst = keys["Ftst"], ftst.first != 1 || !thermalmonitordYields {
            return false  // thermalmonitord still has the fans
        }
        writes.append((key, bytes))
        if key.hasSuffix("Tg"), targetLag > 0 {
            // The target read back before this write, first: the value from before any of them.
            if targetWrites[key] == nil { targetWrites[key] = [(.distantPast, keys[key]!)] }
            targetWrites[key]!.append((clock(), bytes))
        }
        keys[key] = bytes
        return true
    }

    /// Another app setting a fan's target. Its write isn't the helper's, so it isn't in `writes`,
    /// and it reads back at once.
    func appSetsTarget(fan: Int, _ rpm: Float) {
        keys["F\(fan)Tg"] = Self.bytes(rpm)
        targetWrites["F\(fan)Tg"] = [(.distantPast, Self.bytes(rpm))]
    }

    func readFloat(_ key: String) -> Float? {
        guard let bytes = read(key), bytes.count == 4 else { return nil }
        return bytes.withUnsafeBytes { $0.loadUnaligned(as: Float.self) }
    }

    func writeFloat(_ key: String, _ value: Float) -> Bool { write(key, Self.bytes(value)) }

    static func bytes(_ value: Float) -> [UInt8] { withUnsafeBytes(of: value) { Array($0) } }
}
