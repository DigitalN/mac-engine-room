//  A stand-in for SMC.swift's SMCConnection, compiled with Fans.swift in its place, that
//  behaves like one of two Macs:
//
//    m4      has Ftst; refuses a manual mode until Ftst is 1, as thermalmonitord does
//    m1pro   has no Ftst; takes a manual mode at once (measured on the maintainer's M1 Pro)
//
//  Every write is recorded, so the tests can check the order things happened in.

import Foundation

final class SMCConnection {
    var keys: [String: [UInt8]]
    var writes: [(key: String, bytes: [UInt8])] = []
    var refuse: Set<String> = []  // keys whose writes fail
    var thermalmonitordYields = true  // m4: whether Ftst = 1 frees the fans

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

    func read(_ key: String) -> [UInt8]? { keys[key] }

    func write(_ key: String, _ bytes: [UInt8]) -> Bool {
        guard keys[key] != nil, !refuse.contains(key) else { return false }
        if key.hasSuffix("Md"), bytes.first == 1, let ftst = keys["Ftst"], ftst.first != 1 || !thermalmonitordYields {
            return false  // thermalmonitord still has the fans
        }
        writes.append((key, bytes))
        keys[key] = bytes
        return true
    }

    func readFloat(_ key: String) -> Float? {
        guard let bytes = read(key), bytes.count == 4 else { return nil }
        return bytes.withUnsafeBytes { $0.loadUnaligned(as: Float.self) }
    }

    func writeFloat(_ key: String, _ value: Float) -> Bool { write(key, Self.bytes(value)) }

    static func bytes(_ value: Float) -> [UInt8] { withUnsafeBytes(of: value) { Array($0) } }
}
