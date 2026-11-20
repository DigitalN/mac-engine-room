// Which device sent a scroll event, and whether it's a wheel mouse.
//
// Each hardware event carries the IOHIDEvent it came from, and that event's sender ID is the
// IORegistry entry ID of the device's event service (LinearMouse's method: CGEventCopyIOHIDEvent
// and IOHIDEventGetSenderID; see THIRD-PARTY-NOTICES). Going by the device rather than by scroll
// phases keeps a Magic Mouse and the trackpad apart from wheel mice, and every event of one
// device, momentum included, gets the same treatment. Events another app posts carry no HID
// event (or a sender of 0), and are left alone.
//
// A device is a trackpad, not a wheel mouse, when it's on Apple's multitouch driver (the trackpad
// and the Magic Mouse) or its HIDScrollAccelerationType is HIDTrackpadScrollAcceleration (Scroll
// Reverser's test). Only those two properties and the drivers' class names are read: never a
// serial number.

import CoreGraphics
import Foundation
import IOKit

@_silgen_name("CGEventCopyIOHIDEvent")
func CGEventCopyIOHIDEvent(_ event: CGEvent) -> Unmanaged<CFTypeRef>?

@_silgen_name("IOHIDEventGetSenderID")
func IOHIDEventGetSenderID(_ event: CFTypeRef) -> UInt64

@_silgen_name("IOHIDEventGetFloatValue")
func IOHIDEventGetFloatValue(_ event: CFTypeRef, _ field: UInt32) -> Double

@_silgen_name("IOHIDEventSetFloatValue")
func IOHIDEventSetFloatValue(_ event: CFTypeRef, _ field: UInt32, _ value: Double)

/// kIOHIDEventFieldScrollY: (kIOHIDEventTypeScroll << 16) | 1.
let hidScrollY: UInt32 = (6 << 16) | 1

/// What the IORegistry says about a sender: its driver's class and its ancestors', and its scroll
/// acceleration type.
struct DeviceInfo {
    var classNames: [String]
    var scrollAcceleration: String?
}

/// Pure: a wheel mouse is anything that scrolls and isn't a trackpad or a Magic Mouse.
func isWheelMouseDevice(_ info: DeviceInfo) -> Bool {
    if info.classNames.contains(where: { $0.hasPrefix("AppleMultitouch") }) {
        return false
    }
    return info.scrollAcceleration != "HIDTrackpadScrollAcceleration"
}

/// The sender's entry in the IORegistry, or nil when it's gone (or was never a device).
func lookUpDevice(senderID: UInt64) -> DeviceInfo? {
    guard senderID != 0, let matching = IORegistryEntryIDMatching(senderID) else {
        return nil
    }
    let service = IOServiceGetMatchingService(kIOMainPortDefault, matching)  // consumes `matching`
    guard service != 0 else {
        return nil
    }
    var names: [String] = []
    var entry = service
    IOObjectRetain(entry)
    for _ in 0..<8 {  // the driver, then its providers: enough to reach the multitouch device
        var name = [CChar](repeating: 0, count: 128)
        if IOObjectGetClass(entry, &name) == KERN_SUCCESS {
            names.append(String(cString: name))
        }
        var parent: io_registry_entry_t = 0
        let found = IORegistryEntryGetParentEntry(entry, kIOServicePlane, &parent) == KERN_SUCCESS
        IOObjectRelease(entry)
        guard found else {
            entry = 0
            break
        }
        entry = parent
    }
    if entry != 0 {
        IOObjectRelease(entry)
    }
    let options = IOOptionBits(kIORegistryIterateRecursively | kIORegistryIterateParents)
    let acceleration = IORegistryEntrySearchCFProperty(
        service, kIOServicePlane, "HIDScrollAccelerationType" as CFString, kCFAllocatorDefault, options) as? String
    IOObjectRelease(service)
    return DeviceInfo(classNames: names, scrollAcceleration: acceleration)
}

/// Each sender's verdict, looked up once: the tap thread asks for every scroll event. A device that
/// reconnects gets a new sender ID, so a stale entry is only ever a few bytes.
final class Senders {
    private var wheelMice: [UInt64: Bool] = [:]
    private let lookUp: (UInt64) -> DeviceInfo?

    init(lookUp: @escaping (UInt64) -> DeviceInfo? = lookUpDevice) {
        self.lookUp = lookUp
    }

    func isWheelMouse(_ senderID: UInt64) -> Bool {
        if let known = wheelMice[senderID] {
            return known
        }
        let verdict = lookUp(senderID).map(isWheelMouseDevice) ?? false
        wheelMice[senderID] = verdict
        return verdict
    }
}
