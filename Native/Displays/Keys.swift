// The brightness, volume and mute keys, while the Displays tab's switch is on: an event tap on a thread
// of its own, with its own run loop, at user-interactive QoS the whole time. It needs Accessibility.
//
// The keys arrive as system-defined events (NX_SYSDEFINED, subtype 8, aux control buttons): brightness
// up 2, down 3 (checked on the maintainer's M1 Pro on macOS 27.2.1: they reach the tap and swallowing
// them works), volume up 0, down 1, mute 7. The tap only decides whether to take a key, from two flags
// the main thread keeps; what a press does happens on the main queue. No Python runs on the tap
// thread, and the tap never waits for the main thread.

import AppKit
import CoreAudio
import CoreGraphics
import Foundation

enum Key: Int64 {
    case volumeUp = 0
    case volumeDown = 1
    case brightnessUp = 2
    case brightnessDown = 3
    case mute = 7

    var isBrightness: Bool {
        return self == .brightnessUp || self == .brightnessDown
    }
}

struct KeyPress {
    let key: Key
    let fine: Bool  // ⌥⇧: quarter steps
}

/// The two flags the tap reads, kept by the main thread.
final class KeyRouting {
    private let lock = NSLock()
    private var brightness = false  // some display's brightness can be adjusted
    private var volume = false  // macOS's sound output is a display whose volume answers DDC

    func set(brightness: Bool, volume: Bool) {
        lock.lock()
        self.brightness = brightness
        self.volume = volume
        lock.unlock()
    }

    func takes(_ key: Key) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return key.isBrightness ? brightness : volume
    }
}

/// A system-defined event's key, whether it's a press (not a release), and whether ⌥⇧ is held. Pure.
func systemKey(subtype: Int16, data1: Int, flags: CGEventFlags) -> (key: Key, down: Bool, fine: Bool)? {
    guard subtype == 8, let key = Key(rawValue: Int64((data1 & 0xFFFF_0000) >> 16)) else {
        return nil
    }
    let state = (data1 & 0xFF00) >> 8  // 0xA pressed, 0xB released
    let fine = flags.contains(.maskAlternate) && flags.contains(.maskShift)
    return (key, state == 0xA, fine)
}

final class KeyEngine {
    var tap: CFMachPort?
    let routing: KeyRouting
    let onPress: (KeyPress) -> Void  // called on the main queue

    init(routing: KeyRouting, onPress: @escaping (KeyPress) -> Void) {
        self.routing = routing
        self.onPress = onPress
    }

    func handle(_ type: CGEventType, _ event: CGEvent) -> CGEvent? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap {
                CGEvent.tapEnable(tap: tap, enable: true)
            }
            return event
        }
        guard type.rawValue == 14, let ns = NSEvent(cgEvent: event),
              let (key, down, fine) = systemKey(subtype: ns.subtype.rawValue, data1: ns.data1, flags: event.flags),
              routing.takes(key) else {
            return event
        }
        if down {
            // At user-interactive QoS: the main thread sits at background QoS while the app is idle
            // (priority.py), and a press shouldn't wait behind a busy Mac's efficiency cores.
            let onPress = self.onPress
            DispatchQueue.main.async(qos: .userInteractive, flags: .enforceQoS) { onPress(KeyPress(key: key, fine: fine)) }
        }
        return nil  // the press and its release: macOS never sees either
    }
}

private func keyTapCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent,
                            info: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let info else {
        return Unmanaged.passUnretained(event)
    }
    let engine = Unmanaged<KeyEngine>.fromOpaque(info).takeUnretainedValue()
    return engine.handle(type, event).map { Unmanaged.passUnretained($0) }
}

/// The tap's thread, from making the tap to invalidating it.
final class KeyTap {
    let engine: KeyEngine
    private var runLoop: CFRunLoop?
    private var made = false
    private let finished = DispatchSemaphore(value: 0)

    private init(_ engine: KeyEngine) {
        self.engine = engine
    }

    /// Makes the tap on a thread of its own and waits until it's in place, or nil if macOS refused it.
    static func start(_ engine: KeyEngine) -> KeyTap? {
        let keyTap = KeyTap(engine)
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { keyTap.run(ready) }
        thread.name = "Mac Engine Room display keys tap"
        thread.qualityOfService = .userInteractive
        thread.start()
        ready.wait()
        return keyTap.made ? keyTap : nil
    }

    private func run(_ ready: DispatchSemaphore) {
        let mask = CGEventMask(1) << 14  // NX_SYSDEFINED: the media keys, nothing else
        guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                          eventsOfInterest: mask, callback: keyTapCallback,
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
        // `ready` is signalled from inside the run, not just before it: each run starts with its stop
        // flag cleared, so a CFRunLoopStop that landed between the two would be lost, and stop() would
        // wait forever on the main thread.
        CFRunLoopPerformBlock(loop, CFRunLoopMode.defaultMode.rawValue) { ready.signal() }

        CFRunLoopRun()  // until stop()

        CGEvent.tapEnable(tap: tap, enable: false)
        CFRunLoopRemoveSource(loop, source, .commonModes)
        CFMachPortInvalidate(tap)
        engine.tap = nil
        finished.signal()
    }

    /// Drops the tap and waits until it's invalidated.
    func stop() {
        guard let runLoop else {
            return
        }
        // The stop runs inside the run loop too, so it can't land between two runs and be lost.
        CFRunLoopPerformBlock(runLoop, CFRunLoopMode.defaultMode.rawValue) { CFRunLoopStop(CFRunLoopGetCurrent()) }
        CFRunLoopWakeUp(runLoop)
        finished.wait()
    }
}

/// macOS's sound output, to tell whether the volume keys go to a display over DDC. Main thread; it
/// listens only while the keys switch is on.
final class SoundOutput {
    private var listening = false
    var onChange: () -> Void = {}

    private static var defaultOutput = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)

    private lazy var listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.onChange() }

    func listen(_ on: Bool) {
        guard on != listening else {
            return
        }
        listening = on
        if on {
            AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &Self.defaultOutput, .main, listener)
        } else {
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &Self.defaultOutput, .main, listener)
        }
    }

    /// The output device's name, and whether it's an HDMI or DisplayPort one (a display's speakers).
    func current() -> (name: String, display: Bool)? {
        var device = AudioObjectID(0)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &Self.defaultOutput, 0, nil, &size, &device) == noErr,
              device != 0 else {
            return nil
        }
        var transport = UInt32(0)
        size = UInt32(MemoryLayout<UInt32>.size)
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        AudioObjectGetPropertyData(device, &address, 0, nil, &size, &transport)
        var name: Unmanaged<CFString>?
        size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        address.mSelector = kAudioObjectPropertyName
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &name) == noErr, let name else {
            return nil
        }
        let isDisplay = transport == kAudioDeviceTransportTypeHDMI || transport == kAudioDeviceTransportTypeDisplayPort
        return (name.takeRetainedValue() as String, isDisplay)
    }
}

/// Which display the sound goes to, if any: the one whose name the output device has, or, if there's
/// only one display it could be, that one. Pure.
func soundDisplay(output: (name: String, display: Bool)?, candidates: [(id: CGDirectDisplayID, name: String)]) -> CGDirectDisplayID? {
    guard let output, output.display else {
        return nil
    }
    let wanted = output.name.lowercased()
    if let named = candidates.first(where: { $0.name.lowercased() == wanted || wanted.contains($0.name.lowercased()) }) {
        return named.id
    }
    return candidates.count == 1 ? candidates[0].id : nil
}
