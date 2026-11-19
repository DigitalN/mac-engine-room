// DDC/CI for external displays and TVs on Apple silicon, through IOKit's IOAVService: m1ddc's
// method (MIT; see THIRD-PARTY-NOTICES). Each external display has a DCPAVServiceProxy whose
// Location is "External"; IOAVServiceWriteI2C and IOAVServiceReadI2C talk to its monitor at chip
// address 0x37 and data address 0x51, or chip 0xB7 behind the HDMI chip (AppleMCDP29XX, which m1ddc
// found on the M1 Mac mini's HDMI port). Matching each service to its display follows MonitorControl
// (MIT): the IORegistry lists each external framebuffer just before its service.
//
// Nothing identifying is read: only the framebuffer's manufacturer and product IDs, never the EDID
// or its serial numbers. Display names come from NSScreen.

import CoreGraphics
import Foundation
import IOKit

enum VCP: UInt8 {
    case brightness = 0x10
    case volume = 0x62
    case mute = 0x8D  // 1 muted, 2 not
}

private typealias CreateWithService = @convention(c) (CFAllocator?, io_service_t) -> Unmanaged<CFTypeRef>?
private typealias ReadI2C = @convention(c) (CFTypeRef, UInt32, UInt32, UnsafeMutableRawPointer, UInt32) -> IOReturn
private typealias WriteI2C = @convention(c) (CFTypeRef, UInt32, UInt32, UnsafeRawPointer, UInt32) -> IOReturn

/// IOKit's private IOAVService calls, looked up once; nil on a macOS without them.
private struct AVService {
    let create: CreateWithService
    let read: ReadI2C
    let write: WriteI2C

    static let shared: AVService? = {
        guard let iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW),
              let create = dlsym(iokit, "IOAVServiceCreateWithService"),
              let read = dlsym(iokit, "IOAVServiceReadI2C"),
              let write = dlsym(iokit, "IOAVServiceWriteI2C") else {
            return nil
        }
        return AVService(create: unsafeBitCast(create, to: CreateWithService.self),
                         read: unsafeBitCast(read, to: ReadI2C.self),
                         write: unsafeBitCast(write, to: WriteI2C.self))
    }()
}

/// One external display's DDC channel. Its calls block (each waits for the monitor) and must run on
/// the DDC queue, never the main thread.
final class DDCChannel {
    static let dataAddress: UInt32 = 0x51
    static let hostAddress: UInt8 = 0x50  // the host's address in a reply's checksum
    static let displayAddress: UInt8 = 0x6E  // the display's address in a request's checksum
    // MonitorControl's timings for Apple silicon.
    static let writeCycles = 2
    static let writeWait: useconds_t = 10_000
    static let readWait: useconds_t = 50_000
    static let retries = 4
    static let retryWait: useconds_t = 20_000

    let vendor: UInt32
    let product: UInt32
    let chipAddress: UInt32
    private let service: CFTypeRef

    init?(proxy: io_service_t, vendor: UInt32, product: UInt32, chipAddress: UInt32) {
        guard let api = AVService.shared, let made = api.create(kCFAllocatorDefault, proxy) else {
            return nil
        }
        service = made.takeRetainedValue()
        self.vendor = vendor
        self.product = product
        self.chipAddress = chipAddress
    }

    /// Sets a VCP feature. False if the display didn't take the write.
    @discardableResult
    func write(_ vcp: VCP, _ value: Int) -> Bool {
        guard let api = AVService.shared else {
            return false
        }
        var packet = DDCPacket.set(vcp.rawValue, UInt16(clamping: value))
        for _ in 0..<Self.retries {
            var ok = true
            for _ in 0..<Self.writeCycles {
                usleep(Self.writeWait)
                ok = api.write(service, chipAddress, Self.dataAddress, &packet, UInt32(packet.count)) == kIOReturnSuccess && ok
            }
            if ok {
                return true
            }
            usleep(Self.retryWait)
        }
        return false
    }

    /// Reads a VCP feature: (current, maximum), or nil if the display doesn't answer.
    func read(_ vcp: VCP) -> (current: Int, maximum: Int)? {
        guard let api = AVService.shared else {
            return nil
        }
        var request = DDCPacket.get(vcp.rawValue)
        var reply = [UInt8](repeating: 0, count: 12)
        for _ in 0..<Self.retries {
            var sent = true
            for _ in 0..<Self.writeCycles {
                usleep(Self.writeWait)
                sent = api.write(service, chipAddress, Self.dataAddress, &request, UInt32(request.count)) == kIOReturnSuccess && sent
            }
            usleep(Self.readWait)
            if sent, api.read(service, chipAddress, Self.dataAddress, &reply, UInt32(reply.count)) == kIOReturnSuccess,
               let value = DDCPacket.parseReply(reply, vcp: vcp.rawValue) {
                return value
            }
            usleep(Self.retryWait)
        }
        return nil
    }
}

/// DDC/CI packets (VESA DDC/CI 1.1), pure: tested by tests/displays/main.swift.
enum DDCPacket {
    /// Set VCP Feature: length, opcode 0x03, the feature, the value (big-endian), checksum.
    static func set(_ vcp: UInt8, _ value: UInt16) -> [UInt8] {
        let body: [UInt8] = [0x84, 0x03, vcp, UInt8(value >> 8), UInt8(value & 0xFF)]
        return body + [checksum(DDCChannel.displayAddress ^ UInt8(DDCChannel.dataAddress), body)]
    }

    /// Get VCP Feature: length, opcode 0x01, the feature, checksum.
    static func get(_ vcp: UInt8) -> [UInt8] {
        let body: [UInt8] = [0x82, 0x01, vcp]
        return body + [checksum(DDCChannel.displayAddress ^ UInt8(DDCChannel.dataAddress), body)]
    }

    /// The reply to Get VCP Feature: source, length, opcode 0x02, result (0: supported), the feature,
    /// type, maximum (2 bytes), current (2 bytes), checksum.
    static func parseReply(_ reply: [UInt8], vcp: UInt8) -> (current: Int, maximum: Int)? {
        guard reply.count >= 11, reply[2] == 0x02, reply[3] == 0x00, reply[4] == vcp,
              checksum(DDCChannel.hostAddress, Array(reply[0..<10])) == reply[10] else {
            return nil
        }
        let maximum = Int(reply[6]) << 8 | Int(reply[7])
        let current = Int(reply[8]) << 8 | Int(reply[9])
        return maximum > 0 ? (current, maximum) : nil
    }

    static func checksum(_ start: UInt8, _ bytes: [UInt8]) -> UInt8 {
        return bytes.reduce(start, ^)
    }
}

/// The external displays' DDC channels, found in the IORegistry, each with the manufacturer and
/// product IDs of the framebuffer listed just before it.
enum DDCDiscovery {
    struct Found {
        let proxy: io_service_t  // retained; released by the caller
        let vendor: UInt32
        let product: UInt32
        let chipAddress: UInt32
    }

    static func externalServices() -> [Found] {
        var iterator = io_iterator_t()
        guard IORegistryEntryCreateIterator(IORegistryGetRootEntry(kIOMainPortDefault), kIOServicePlane,
                                            IOOptionBits(kIORegistryIterateRecursively), &iterator) == KERN_SUCCESS else {
            return []
        }
        defer { IOObjectRelease(iterator) }
        var found: [Found] = []
        var framebuffer: (vendor: UInt32, product: UInt32)?
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            let name = className(entry)
            if name == "AppleCLCD2" || name == "IOMobileFramebufferShim" {
                framebuffer = productIDs(entry)
                IOObjectRelease(entry)
            } else if name == "DCPAVServiceProxy", string(entry, "Location") == "External" {
                let ids = framebuffer ?? (0, 0)
                framebuffer = nil
                found.append(Found(proxy: entry, vendor: ids.vendor, product: ids.product,
                                   chipAddress: behindHDMIChip(entry) ? 0xB7 : 0x37))
            } else {
                IOObjectRelease(entry)
            }
        }
        return found
    }

    /// Pairs each external CoreGraphics display with a channel: the same manufacturer and product,
    /// or the same manufacturer, or, with nothing better, in the order both lists give them.
    static func match(displays: [(id: CGDirectDisplayID, vendor: UInt32, product: UInt32)],
                      channels: [(vendor: UInt32, product: UInt32)]) -> [CGDirectDisplayID: Int] {
        var result: [CGDirectDisplayID: Int] = [:]
        var free = Array(channels.indices)
        for rule in 0..<3 {
            for display in displays where result[display.id] == nil {
                let pick = free.first { i in
                    switch rule {
                    case 0: return channels[i].vendor == display.vendor && channels[i].product == display.product
                    case 1: return channels[i].vendor == display.vendor && channels[i].vendor != 0
                    default: return true
                    }
                }
                if let pick {
                    result[display.id] = pick
                    free.removeAll { $0 == pick }
                }
            }
        }
        return result
    }

    private static func className(_ entry: io_object_t) -> String {
        var name = [CChar](repeating: 0, count: 128)
        IOObjectGetClass(entry, &name)
        return String(cString: name)
    }

    private static func string(_ entry: io_registry_entry_t, _ key: String) -> String? {
        return IORegistryEntryCreateCFProperty(entry, key as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? String
    }

    /// Only these two IDs are taken from the framebuffer's attributes; its serial numbers are never read.
    private static func productIDs(_ entry: io_registry_entry_t) -> (vendor: UInt32, product: UInt32) {
        guard let attributes = IORegistryEntryCreateCFProperty(entry, "DisplayAttributes" as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? [String: Any],
              let product = attributes["ProductAttributes"] as? [String: Any] else {
            return (0, 0)
        }
        return ((product["LegacyManufacturerID"] as? NSNumber)?.uint32Value ?? 0,
                (product["ProductID"] as? NSNumber)?.uint32Value ?? 0)
    }

    /// The HDMI port's chip (AppleMCDP29XX): its DDC answers at chip address 0xB7.
    private static func behindHDMIChip(_ proxy: io_registry_entry_t) -> Bool {
        var parent = io_registry_entry_t()
        guard IORegistryEntryGetParentEntry(proxy, kIOServicePlane, &parent) == KERN_SUCCESS else {
            return false
        }
        defer { IOObjectRelease(parent) }
        return string(parent, "EPICProviderClass") == "AppleMCDP29XX"
    }
}
