// Back and forward buttons: the side buttons (buttons 4 and 5) become Back and Forward everywhere.
// Apple's apps (Safari and Finder among them), Firefox, Opera and ForkLift ignore those buttons, so
// for them a press becomes the navigation swipe a trackpad sends. Every other app already takes the
// buttons as Back and Forward (Chrome, Electron apps), so they get them unchanged. The list of apps
// and the swipe event's fields are LinearMouse's (see THIRD-PARTY-NOTICES).

import AppKit
import CoreGraphics

enum SideButton: Int64 {
    case back = 3  // CGMouseButton's numbering: 0 left, 1 right, 2 middle
    case forward = 4

    /// IOHIDSwipeMask: kIOHIDSwipeLeft goes back, kIOHIDSwipeRight forward.
    var swipeMask: Int64 {
        return self == .back ? 0x04 : 0x08
    }
}

private let swipeApps = ["com.apple.*", "com.binarynights.ForkLift*", "org.mozilla.firefox", "com.operasoftware.Opera"]

/// Pure: whether an app needs the swipe, going by its bundle identifier.
func needsSwipe(bundleID: String?) -> Bool {
    guard let bundleID else {
        return false
    }
    return swipeApps.contains { pattern in
        pattern.hasSuffix("*") ? bundleID.hasPrefix(pattern.dropLast()) : bundleID == pattern
    }
}

// CGEvent's private gesture fields (WebKit's CoreGraphicsTestSPI.h lists them).
let gestureHIDType = CGEventField(rawValue: 110)!
let gestureSwipeValue = CGEventField(rawValue: 115)!
let gesturePhase = CGEventField(rawValue: 132)!
private let navigationSwipe: Int64 = 16  // kIOHIDEventTypeNavigationSwipe
private let gestureEventType = CGEventType(rawValue: 29)!  // NSEventType.gesture

/// A navigation swipe, began then ended, which the app under the pointer takes as Back or Forward.
func swipeEvents(_ button: SideButton) -> [CGEvent] {
    guard let began = CGEvent(source: nil), let ended = CGEvent(source: nil) else {
        return []
    }
    for (event, phase) in [(began, Int64(1)), (ended, Int64(4))] {  // CGSGesturePhase began, ended
        event.type = gestureEventType
        event.setIntegerValueField(gestureHIDType, value: navigationSwipe)
        event.setIntegerValueField(gesturePhase, value: phase)
    }
    began.setIntegerValueField(gestureSwipeValue, value: button.swipeMask)
    return [began, ended]
}

func postSwipe(_ button: SideButton) {
    for event in swipeEvents(button) {
        event.post(tap: .cgSessionEventTap)
    }
}

/// The bundle identifier of the app a button's event goes to. Not cached: presses are rare, and
/// process IDs are reused.
func bundleID(pid: pid_t) -> String? {
    return pid > 0 ? NSRunningApplication(processIdentifier: pid)?.bundleIdentifier : nil
}
