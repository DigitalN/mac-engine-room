// libmouse.dylib: Mouse's event tap. Python (Application Files/mouse.py) loads it with ctypes and
// only turns things on and off; no Python runs on the tap's thread.
//
// The tap is active (it changes and drops events), so it needs Accessibility. It sits on a thread
// of its own with its own run loop, at user-interactive QoS the whole time, and listens only for
// scroll events and the other buttons' presses and releases: never for mouse movement. There's no
// tap at all while every switch is off. macOS disables a tap that's too slow, or while secure input
// takes over, and this one enables itself again; a tap that's dropped is always invalidated, since
// left-over taps slow down all input.
//
// C functions (@_cdecl), called from one Python thread at a time (the main thread):
//   int32_t mouse_configure(bool reverse, bool smooth, bool back_forward)
//       The three switches. Makes the tap when one is on, drops it when all are off.
//       Returns 1 (tap running), 0 (no tap: all off), -1 (Accessibility isn't allowed), or
//       -2 (macOS refused the tap).
//   void mouse_stop(void)      Drops the tap but keeps the switches (the updater, or Accessibility
//                              taken away). The next mouse_configure makes it again.
//   bool mouse_running(void)

import ApplicationServices
import CoreGraphics
import Foundation
import os

private let log = Logger(subsystem: "com.pythonflow.monitor", category: "mouse")

/// Marks the scroll events the glide posts, so the tap lets them through.
let glideMarker: Int64 = 0x4D45_524D

struct Switches: Equatable {
    var reverse = false
    var smooth = false
    var backForward = false

    var any: Bool {
        return reverse || smooth || backForward
    }
}

/// Reverses an event's vertical scrolling. DeltaAxis1 goes first, because macOS recomputes the point
/// deltas from it; then the fixed-point and point deltas, and the HID event's own (Scroll Reverser's
/// order; see THIRD-PARTY-NOTICES). Horizontal scrolling is left as it is.
func reverseVertical(_ event: CGEvent, hid: CFTypeRef?) {
    let lines = event.getIntegerValueField(.scrollWheelEventDeltaAxis1)
    let fixed = event.getDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1)
    let points = event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1)
    event.setIntegerValueField(.scrollWheelEventDeltaAxis1, value: -lines)
    event.setDoubleValueField(.scrollWheelEventFixedPtDeltaAxis1, value: -fixed)
    event.setIntegerValueField(.scrollWheelEventPointDeltaAxis1, value: -points)
    if let hid {
        IOHIDEventSetFloatValue(hid, hidScrollY, -IOHIDEventGetFloatValue(hid, hidScrollY))
    }
}

/// The tap's state and what it does with each event. Tap thread only.
final class Engine {
    var switches: Switches
    var tap: CFMachPort?
    private let senders: Senders
    private let targetApp: (CGEvent) -> String?
    private var glide = Glide()
    private var glideTimer: CFRunLoopTimer?
    private var lastFrame = 0.0
    private var glideFlags: CGEventFlags = []
    private var swallowed = Set<Int64>()  // side buttons whose press became a swipe: their release goes too

    init(_ switches: Switches, senders: Senders = Senders(), targetApp: @escaping (CGEvent) -> String? = Engine.targetApp) {
        self.switches = switches
        self.senders = senders
        self.targetApp = targetApp
    }

    /// The event to pass on, or nil to drop it.
    func handle(_ type: CGEventType, _ event: CGEvent) -> CGEvent? {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
            }
            let why = type == .tapDisabledByTimeout ? "too slow" : "user input"
            log.info("macOS disabled the mouse tap (\(why, privacy: .public)); enabled it again")
            return event
        case .scrollWheel:
            return scroll(event)
        case .otherMouseDown, .otherMouseUp:
            return button(type, event)
        default:
            return event
        }
    }

    private func scroll(_ event: CGEvent) -> CGEvent? {
        guard switches.reverse || switches.smooth,
              event.getIntegerValueField(.eventSourceUserData) != glideMarker,
              let hid = CGEventCopyIOHIDEvent(event)?.takeRetainedValue() else {
            return event  // posted by an app (or by the glide): not a wheel's
        }
        let sender = IOHIDEventGetSenderID(hid)
        guard sender != 0, senders.isWheelMouse(sender) else {
            return event
        }
        if switches.reverse {
            reverseVertical(event, hid: hid)
        }
        if switches.smooth && event.getIntegerValueField(.scrollWheelEventIsContinuous) == 0 {
            glide.add(linesY: event.getIntegerValueField(.scrollWheelEventDeltaAxis1),
                      linesX: event.getIntegerValueField(.scrollWheelEventDeltaAxis2))
            glideFlags = event.flags  // ⇧ and ⌘ with the wheel keep their meaning
            startGlide()
            return nil
        }
        return event
    }

    private func button(_ type: CGEventType, _ event: CGEvent) -> CGEvent? {
        guard let side = SideButton(rawValue: event.getIntegerValueField(.mouseEventButtonNumber)) else {
            return event
        }
        if type == .otherMouseUp {
            return swallowed.remove(side.rawValue) != nil ? nil : event
        }
        guard switches.backForward, CGEventCopyIOHIDEvent(event)?.takeRetainedValue() != nil,
              needsSwipe(bundleID: targetApp(event)) else {
            return event  // an app that takes the buttons as they are, or a press another app posted
        }
        swallowed.insert(side.rawValue)
        postSwipe(side)
        return nil
    }

    /// The app the press goes to: the event's target, or the owner of the window under the pointer.
    static func targetApp(_ event: CGEvent) -> String? {
        let pid = pid_t(truncatingIfNeeded: event.getIntegerValueField(.eventTargetUnixProcessID))
        return bundleID(pid: pid > 0 ? pid : windowOwner(at: event.location))
    }

    private static func windowOwner(at point: CGPoint) -> pid_t {
        let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []
        for window in windows where (window[kCGWindowLayer as String] as? Int) == 0 {
            if let bounds = window[kCGWindowBounds as String] as? NSDictionary,
               let rect = CGRect(dictionaryRepresentation: bounds), rect.contains(point),
               let pid = window[kCGWindowOwnerPID as String] as? Int {
                return pid_t(pid)
            }
        }
        return 0
    }

    // ── the glide: a timer on the tap thread's run loop, only while something is left ──

    private func startGlide() {
        guard glideTimer == nil else {
            return
        }
        lastFrame = ProcessInfo.processInfo.systemUptime
        let timer = CFRunLoopTimerCreateWithHandler(
            kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + Glide.frameInterval, Glide.frameInterval, 0, 0
        ) { [unowned self] _ in self.frame() }
        glideTimer = timer
        CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, .commonModes)
    }

    private func frame() {
        let now = ProcessInfo.processInfo.systemUptime
        let (y, x) = glide.step(dt: now - lastFrame)
        lastFrame = now
        if y != 0 || x != 0,
           let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: y, wheel2: x, wheel3: 0) {
            event.setIntegerValueField(.eventSourceUserData, value: glideMarker)
            event.flags = glideFlags
            event.post(tap: .cgSessionEventTap)
        }
        if glide.done {
            stopGlide()
        }
    }

    func stopGlide() {
        if let glideTimer {
            CFRunLoopTimerInvalidate(glideTimer)
        }
        glideTimer = nil
        glide = Glide()
    }
}

private func tapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                         info: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let info else {
        return Unmanaged.passUnretained(event)
    }
    let engine = Unmanaged<Engine>.fromOpaque(info).takeUnretainedValue()
    return engine.handle(type, event).map { Unmanaged.passUnretained($0) }
}

/// The tap's thread, from making the tap to invalidating it.
final class TapThread {
    let engine: Engine
    private var runLoop: CFRunLoop?
    private var made = false
    private let finished = DispatchSemaphore(value: 0)

    private init(_ switches: Switches) {
        engine = Engine(switches)
    }

    /// Makes the tap on a thread of its own and waits until it's in place, or nil if macOS refused it.
    static func start(_ switches: Switches) -> TapThread? {
        let tapThread = TapThread(switches)
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { tapThread.run(ready) }
        thread.name = "Mac Engine Room mouse tap"
        thread.qualityOfService = .userInteractive
        thread.start()
        ready.wait()
        return tapThread.made ? tapThread : nil
    }

    private func run(_ ready: DispatchSemaphore) {
        let types: [CGEventType] = [.scrollWheel, .otherMouseDown, .otherMouseUp]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << CGEventMask($1.rawValue)) }
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                          eventsOfInterest: mask, callback: tapCallback,
                                          userInfo: Unmanaged.passUnretained(engine).toOpaque()) else {
            ready.signal()
            finished.signal()
            return
        }
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        let loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(loop, source, .commonModes)
        engine.tap = tap
        runLoop = loop
        CGEvent.tapEnable(tap: tap, enable: true)
        made = true
        ready.signal()

        CFRunLoopRun()  // until stop()

        engine.stopGlide()
        CGEvent.tapEnable(tap: tap, enable: false)
        CFRunLoopRemoveSource(loop, source, .commonModes)
        CFMachPortInvalidate(tap)
        engine.tap = nil
        finished.signal()
    }

    /// New switches, applied on the tap thread between events.
    func update(_ switches: Switches) {
        guard let runLoop else {
            return
        }
        CFRunLoopPerformBlock(runLoop, CFRunLoopMode.commonModes.rawValue) { self.engine.switches = switches }
        CFRunLoopWakeUp(runLoop)
    }

    /// Drops the tap and waits until it's invalidated.
    func stop() {
        guard let runLoop else {
            return
        }
        CFRunLoopStop(runLoop)  // a stop that comes before the run loop runs still counts
        finished.wait()
    }
}

// ── the C functions ──

private let lock = NSLock()
private var current: TapThread?
private var wanted = Switches()

@_cdecl("mouse_configure")
public func mouse_configure(_ reverse: Bool, _ smooth: Bool, _ backForward: Bool) -> Int32 {
    lock.lock()
    defer { lock.unlock() }
    wanted = Switches(reverse: reverse, smooth: smooth, backForward: backForward)
    guard wanted.any else {
        drop()
        return 0
    }
    if let current {
        current.update(wanted)
        return 1
    }
    guard AXIsProcessTrusted() else {
        return -1
    }
    guard let started = TapThread.start(wanted) else {
        log.error("macOS refused the mouse tap")
        return -2
    }
    current = started
    return 1
}

@_cdecl("mouse_stop")
public func mouse_stop() {
    lock.lock()
    defer { lock.unlock() }
    drop()
}

@_cdecl("mouse_running")
public func mouse_running() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return current != nil
}

private func drop() {
    current?.stop()
    current = nil
}
