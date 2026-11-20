//  Native/Mouse's pure parts, compiled into one program with it: which devices count as wheel mice,
//  the glide, which apps get a swipe for Back and Forward, the swipe's fields, and reversing a scroll
//  event. No tap is made and nothing is posted, so it needs no Accessibility. Run by
//  tests/test_mouse_native.py; prints one line per test and exits 1 on a failure.

import CoreGraphics
import Foundation

var failed = false

func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print(ok ? "ok \(name)" : "FAIL \(name) \(detail())")
    if !ok { failed = true }
}

// ── devices ──

// The M1 Pro's own trackpad, as its sender's driver and providers are in the IORegistry.
let trackpad = DeviceInfo(classNames: ["AppleMultitouchTrackpadHIDEventDriver", "AppleMultitouchDevice", "AppleHIDTransportHIDDevice"],
                          scrollAcceleration: "HIDTrackpadScrollAcceleration")
let magicMouse = DeviceInfo(classNames: ["AppleMultitouchMouseHIDEventDriver", "AppleMultitouchDevice", "BNBMouseDevice"],
                            scrollAcceleration: "HIDMouseScrollAcceleration")
let wheelMouse = DeviceInfo(classNames: ["AppleUserHIDEventDriver", "IOHIDInterface", "IOHIDDevice"],
                            scrollAcceleration: "HIDMouseScrollAcceleration")
check("a trackpad isn't a wheel mouse", !isWheelMouseDevice(trackpad))
check("a Magic Mouse isn't a wheel mouse", !isWheelMouseDevice(magicMouse))
check("a USB or Bluetooth mouse is", isWheelMouseDevice(wheelMouse))
check("one without an acceleration type is too",
      isWheelMouseDevice(DeviceInfo(classNames: ["IOHIDEventDriver"], scrollAcceleration: nil)))
check("trackpad acceleration on another driver isn't",
      !isWheelMouseDevice(DeviceInfo(classNames: ["IOHIDEventDriver"], scrollAcceleration: "HIDTrackpadScrollAcceleration")))

var lookups = 0
let senders = Senders { id in
    lookups += 1
    return [1: wheelMouse, 2: trackpad][id]
}
check("senders: a wheel mouse", senders.isWheelMouse(1))
check("senders: a trackpad", !senders.isWheelMouse(2))
check("senders: one that isn't in the IORegistry is left alone", !senders.isWheelMouse(3))
_ = senders.isWheelMouse(1)
_ = senders.isWheelMouse(3)
check("senders: each is looked up once", lookups == 3, "\(lookups) lookups")
check("sender 0 has no device", lookUpDevice(senderID: 0) == nil)

// ── the glide ──

func run(_ glide: inout Glide, frames: Int, dt: Double = Glide.frameInterval) -> (y: Int, x: Int, frames: Int) {
    var y = 0, x = 0, n = 0
    while !glide.done && n < frames {
        let step = glide.step(dt: dt)
        y += Int(step.y)
        x += Int(step.x)
        n += 1
    }
    return (y, x, n)
}

var glide = Glide()
glide.add(linesY: 1, linesX: 0)
var moved = run(&glide, frames: 1000)
check("a notch glides its whole distance", moved.y == 36 && moved.x == 0, "\(moved)")
check("a notch is done in under 0.4 s", Double(moved.frames) * Glide.frameInterval < 0.4, "\(moved.frames) frames")

glide = Glide()
glide.add(linesY: 1, linesX: 0)
let first = glide.step(dt: Glide.frameInterval)
check("it starts quickly and eases out", first.y >= 3 && first.y <= 8, "\(first.y) points in the first frame")

glide = Glide()
glide.add(linesY: -2, linesX: 1)
moved = run(&glide, frames: 1000)
check("both axes, signs kept", moved.y == -72 && moved.x == 36, "\(moved)")

glide = Glide()
glide.add(linesY: 1, linesX: 0)
var sum = Int(glide.step(dt: Glide.frameInterval).y)
glide.add(linesY: 1, linesX: 0)
sum += run(&glide, frames: 1000).y
check("quick notches add up", sum == 72, "\(sum)")

glide = Glide()
glide.add(linesY: 3, linesX: 0)
_ = glide.step(dt: Glide.frameInterval)
glide.add(linesY: -1, linesX: 0)
moved = run(&glide, frames: 1000)
check("a notch the other way drops what was left", moved.y == -36, "\(moved.y)")

glide = Glide()
glide.add(linesY: 1, linesX: 0)
let late = glide.step(dt: 5)
check("a late frame doesn't jump the whole way", late.y < 36 && !glide.done, "\(late.y)")

glide = Glide()
check("an empty glide is done", glide.done && glide.step(dt: 1) == (0, 0))

// ── back and forward ──

check("Safari gets a swipe", needsSwipe(bundleID: "com.apple.Safari"))
check("Finder gets a swipe", needsSwipe(bundleID: "com.apple.finder"))
check("Firefox gets a swipe", needsSwipe(bundleID: "org.mozilla.firefox"))
check("ForkLift gets a swipe", needsSwipe(bundleID: "com.binarynights.ForkLift-3"))
check("Chrome keeps the buttons", !needsSwipe(bundleID: "com.google.Chrome"))
check("an Opera that isn't Opera keeps them", !needsSwipe(bundleID: "com.operasoftware.OperaGX"))
check("no app keeps them", !needsSwipe(bundleID: nil))

let back = swipeEvents(.back)
check("a swipe is two events", back.count == 2)
if back.count == 2 {
    check("a swipe is a gesture", back.allSatisfy { $0.type.rawValue == 29 && $0.getIntegerValueField(gestureHIDType) == 16 })
    check("it begins, then ends", back.map { $0.getIntegerValueField(gesturePhase) } == [1, 4])
    check("back swipes left", back[0].getIntegerValueField(gestureSwipeValue) == 0x04)
}
check("forward swipes right", swipeEvents(.forward).first?.getIntegerValueField(gestureSwipeValue) == 0x08)

// ── reversing ──

if let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: 3, wheel2: 2, wheel3: 0) {
    let points = event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1)
    let fixed = event.getDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1)
    reverseVertical(event, hid: nil)
    check("vertical lines reversed", event.getIntegerValueField(.scrollWheelEventDeltaAxis1) == -3)
    check("vertical points reversed", event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) == -points, "\(points)")
    check("vertical fixed-point reversed", event.getDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1) == -fixed, "\(fixed)")
    check("horizontal left alone", event.getIntegerValueField(.scrollWheelEventDeltaAxis2) == 2)
} else {
    check("made a scroll event", false)
}

// ── the engine leaves alone what isn't a wheel mouse's ──

let engine = Engine(Switches(reverse: true, smooth: true, backForward: true), senders: senders, targetApp: { _ in "com.apple.Safari" })
if let posted = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: 2, wheel2: 0, wheel3: 0) {
    let out = engine.handle(.scrollWheel, posted)
    check("a posted scroll event goes through unchanged",
          out === posted && posted.getIntegerValueField(.scrollWheelEventDeltaAxis1) == 2)
}
if let press = CGEvent(mouseEventSource: nil, mouseType: .otherMouseDown, mouseCursorPosition: .zero, mouseButton: .center) {
    press.setIntegerValueField(.mouseEventButtonNumber, value: 3)
    check("a posted side-button press goes through", engine.handle(.otherMouseDown, press) === press)
    if let release = CGEvent(mouseEventSource: nil, mouseType: .otherMouseUp, mouseCursorPosition: .zero, mouseButton: .center) {
        release.setIntegerValueField(.mouseEventButtonNumber, value: 3)
        check("and so does its release", engine.handle(.otherMouseUp, release) === release)
    }
}
if let middle = CGEvent(mouseEventSource: nil, mouseType: .otherMouseDown, mouseCursorPosition: .zero, mouseButton: .center) {
    check("the middle button goes through", engine.handle(.otherMouseDown, middle) === middle)
}

check("no tap while every switch is off", mouse_configure(false, false, false) == 0 && !mouse_running())
mouse_stop()

exit(failed ? 1 : 0)
