//  Reading and writing SMC keys through IOKit.
//
//  From ThermalForge's SMCConnection.swift and SMCKeys.swift (github.com/ProducerGuy/ThermalForge
//  at 42bb534, MIT), which adapted it from agoodkind/macos-smc-fan (MIT); see
//  THIRD-PARTY-NOTICES. Trimmed to what the helper uses.

import Foundation
import IOKit

/// The 80-byte structure AppleSMC's struct method takes. The layout must match exactly.
struct SMCParamStruct {
    struct Version {
        var major: UInt8 = 0
        var minor: UInt8 = 0
        var build: UInt8 = 0
        var reserved: UInt8 = 0
        var release: UInt16 = 0
    }

    struct PLimitData {
        var version: UInt16 = 0
        var length: UInt16 = 0
        var cpuPLimit: UInt32 = 0
        var gpuPLimit: UInt32 = 0
        var memPLimit: UInt32 = 0
    }

    struct KeyInfo {
        var dataSize: UInt32 = 0
        var dataType: UInt32 = 0
        var dataAttributes: UInt8 = 0
    }

    typealias Bytes = (
        UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
        UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
        UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
        UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8
    )

    var key: UInt32 = 0
    var vers = Version()
    var pLimitData = PLimitData()
    var keyInfo = KeyInfo()
    var padding: UInt16 = 0
    var result: UInt8 = 0
    var status: UInt8 = 0
    var data8: UInt8 = 0
    var data32: UInt32 = 0
    var bytes: Bytes = (
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    )
}

final class SMCConnection {
    private static let readBytes: UInt8 = 5
    private static let writeBytes: UInt8 = 6
    private static let readKeyInfo: UInt8 = 9
    private static let handleIndex: UInt32 = 2  // AppleSMC's struct method selector

    private let connection: io_connect_t

    init?() {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"))
        guard service != 0 else { return nil }
        defer { IOObjectRelease(service) }
        var conn: io_connect_t = 0
        guard IOServiceOpen(service, mach_task_self_, 0, &conn) == kIOReturnSuccess else { return nil }
        connection = conn
    }

    deinit {
        IOServiceClose(connection)
    }

    /// The key's bytes, or nil if this Mac doesn't have it.
    func read(_ key: String) -> [UInt8]? {
        var input = SMCParamStruct()
        var output = SMCParamStruct()
        input.key = fourCharCode(key)
        input.data8 = Self.readKeyInfo
        guard call(&input, &output) == kIOReturnSuccess, output.result == 0 else { return nil }
        let size = output.keyInfo.dataSize
        guard size > 0, size <= 32 else { return nil }
        input.keyInfo.dataSize = size
        input.data8 = Self.readBytes
        guard call(&input, &output) == kIOReturnSuccess, output.result == 0 else { return nil }
        return withUnsafeBytes(of: output.bytes) { Array($0.prefix(Int(size))) }
    }

    /// Whether the SMC took the write. IOKit can succeed while the SMC refuses it, so its
    /// own result is checked too.
    func write(_ key: String, _ bytes: [UInt8]) -> Bool {
        var input = SMCParamStruct()
        var output = SMCParamStruct()
        input.key = fourCharCode(key)
        input.data8 = Self.readKeyInfo
        guard call(&input, &output) == kIOReturnSuccess, output.result == 0 else { return false }
        input.data8 = Self.writeBytes
        input.keyInfo.dataSize = output.keyInfo.dataSize
        let padded = Array(bytes.prefix(32)) + Array(repeating: 0, count: max(0, 32 - bytes.count))
        withUnsafeMutableBytes(of: &input.bytes) { $0.copyBytes(from: padded) }
        guard call(&input, &output) == kIOReturnSuccess else { return false }
        return output.result == 0
    }

    /// A little-endian `flt`, as Apple silicon stores fan speeds.
    func readFloat(_ key: String) -> Float? {
        guard let bytes = read(key), bytes.count == 4 else { return nil }
        return bytes.withUnsafeBytes { $0.loadUnaligned(as: Float.self) }
    }

    func writeFloat(_ key: String, _ value: Float) -> Bool {
        write(key, withUnsafeBytes(of: value) { Array($0) })
    }

    private func call(_ input: inout SMCParamStruct, _ output: inout SMCParamStruct) -> kern_return_t {
        var outputSize = MemoryLayout<SMCParamStruct>.stride
        return IOConnectCallStructMethod(connection, Self.handleIndex, &input,
                                         MemoryLayout<SMCParamStruct>.stride, &output, &outputSize)
    }

    private func fourCharCode(_ key: String) -> UInt32 {
        key.utf8.reduce(0) { ($0 << 8) | UInt32($1) }
    }
}
