// The built-in display's brightness, through DisplayServices (private, still there on macOS 27.2.1;
// MonitorControl and Lunar use it too). Its brightness is the value macOS's own slider shows (0 to 1),
// and its linear brightness is the backlight's share of its maximum, which the nits follow.

import CoreGraphics
import Foundation

enum DisplayServices {
    private typealias Get = @convention(c) (CGDirectDisplayID, UnsafeMutablePointer<Float>) -> Int32
    private typealias Set = @convention(c) (CGDirectDisplayID, Float) -> Int32
    private typealias Can = @convention(c) (CGDirectDisplayID) -> Bool
    private typealias Register = @convention(c) (CGDirectDisplayID, UnsafeRawPointer?, CFNotificationCallback) -> Int32
    private typealias Unregister = @convention(c) (CGDirectDisplayID, UnsafeRawPointer?) -> Int32

    private static let library = dlopen("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices", RTLD_NOW)

    private static func symbol<T>(_ name: String, _ type: T.Type) -> T? {
        guard let library, let pointer = dlsym(library, name) else {
            return nil
        }
        return unsafeBitCast(pointer, to: type)
    }

    private static let getBrightness = symbol("DisplayServicesGetBrightness", Get.self)
    private static let setBrightness = symbol("DisplayServicesSetBrightness", Set.self)
    private static let getLinear = symbol("DisplayServicesGetLinearBrightness", Get.self)
    private static let canChange = symbol("DisplayServicesCanChangeBrightness", Can.self)
    private static let register = symbol("DisplayServicesRegisterForBrightnessChangeNotifications", Register.self)
    private static let unregister = symbol("DisplayServicesUnregisterForBrightnessChangeNotifications", Unregister.self)

    static var available: Bool {
        return getBrightness != nil && setBrightness != nil
    }

    static func canAdjust(_ display: CGDirectDisplayID) -> Bool {
        guard available else {
            return false
        }
        return canChange?(display) ?? true
    }

    /// 0 to 1, or nil if it can't be read.
    static func brightness(_ display: CGDirectDisplayID) -> Double? {
        var value: Float = -1
        guard let getBrightness, getBrightness(display, &value) == 0, value >= 0 else {
            return nil
        }
        return Double(value)
    }

    static func linearBrightness(_ display: CGDirectDisplayID) -> Double? {
        var value: Float = -1
        guard let getLinear, getLinear(display, &value) == 0, value >= 0 else {
            return nil
        }
        return Double(value)
    }

    @discardableResult
    static func setBrightness(_ display: CGDirectDisplayID, _ value: Double) -> Bool {
        return setBrightness?(display, Float(max(0, min(1, value)))) == 0
    }

    /// `callback` runs on the main thread whenever macOS's brightness changes (Control Center, the keys,
    /// auto-brightness, this app), with the new value (measured on 27.2.1: the notification's userInfo
    /// carries it as "value"). Only while the boost is on.
    static func observe(_ display: CGDirectDisplayID, _ callback: CFNotificationCallback) -> Bool {
        return register?(display, UnsafeRawPointer(bitPattern: UInt(display)), callback) == 0
    }

    static func stopObserving(_ display: CGDirectDisplayID) {
        _ = unregister?(display, UnsafeRawPointer(bitPattern: UInt(display)))
    }
}
