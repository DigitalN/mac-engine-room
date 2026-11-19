// libdisplays.dylib: the Displays tool. Python (Application Files/displays.py) loads it with ctypes,
// sets values and turns the keys on and off; no Python runs on the key tap's thread or the DDC queue.
//
// - Brightness for each display on its own: the built-in through DisplayServices (Builtin.swift),
//   externals and TVs over DDC (DDC.swift). No software dimming: a display that doesn't answer DDC
//   can't be adjusted.
// - The built-in XDR display's boost from 100% to 160% (Boost.swift).
// - The brightness, volume and mute keys while the switch is on (Keys.swift), with the HUD (HUD.swift).
//
// DDC only runs on a change, or when the menu or the tab opens (displays_refresh), on a queue of its
// own: each call waits for the monitor. What the app set is remembered rather than read back.
//
// C functions (@_cdecl). All but displays_list and displays_free run on the main thread (Python sends
// them there); the callbacks are called on the main thread:
//   void displays_start(bool chip_ignores_gamma, void (*changed)(void), void (*log)(const char *))
//   char *displays_list(void)            JSON: each display (see Controller.json); free with displays_free
//   void displays_free(char *)
//   void displays_refresh(void)          read the displays again (DDC on its queue); `changed` follows
//   void displays_set_brightness(uint32_t display, double percent)
//   void displays_set_volume(uint32_t display, double percent)
//   int32_t displays_set_keys(bool on)   1 tap running, 0 off, -1 Accessibility isn't allowed, -2 refused
//   void displays_stop_keys(void)        drop the tap, keep the switch (the updater; Accessibility gone)
//   bool displays_keys_running(void)

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

final class Display {
    let id: CGDirectDisplayID
    var name: String
    let builtin: Bool
    var channel: DDCChannel?
    var answered: Bool?  // DDC: nil until it has been asked
    var brightness: Double?  // percent, 0…top (the built-in's includes the boost)
    var brightnessMax = 100  // DDC's own maximum
    var volume: Double?  // percent, nil where DDC has no volume
    var volumeMax = 100
    var muted: Bool?  // nil where DDC has no mute
    var top = Scale.plainTop
    var xdr = false  // the built-in XDR panel: its HUD shows nits
    var linear: Double?  // the built-in's linear brightness

    init(id: CGDirectDisplayID, name: String, builtin: Bool) {
        self.id = id
        self.name = name
        self.builtin = builtin
    }

    var adjustable: Bool {
        return builtin ? brightness != nil : answered == true && brightness != nil
    }
}

final class Controller {
    static let shared = Controller()
    static let ddcQueue = DispatchQueue(label: "Mac Engine Room DDC", qos: .userInitiated)

    private(set) var displays: [Display] = []
    private let lock = NSLock()  // displays_list may read from any thread
    private var chipIgnoresGamma = false
    private var started = false
    let boost = Boost()
    private let routing = KeyRouting()
    private var keyTap: KeyTap?
    private var keysWanted = false
    private let sound = SoundOutput()
    private var pendingWrites: [String: (Display, VCP, Int)] = [:]  // DDC queue only
    private var writing = false  // DDC queue only
    private var probing = Set<CGDirectDisplayID>()
    private var observers: [NSObjectProtocol] = []
    private var lastLogged = ""
    var changed: () -> Void = {}
    var log: (String) -> Void = { _ in }

    // ── start and the list of displays ──

    func start(chipIgnoresGamma: Bool) {
        self.chipIgnoresGamma = chipIgnoresGamma
        guard !started else {
            return
        }
        started = true
        boost.onLog = { [weak self] in self?.log($0) }
        boost.onEnded = { [weak self] why in
            guard let self else {
                return
            }
            self.log("Display boost off (\(why))")
            if let builtin = self.displays.first(where: { $0.builtin }) {
                self.readBuiltin(builtin)
            }
            self.changed()
        }
        sound.onChange = { [weak self] in self?.route() }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            self?.discover()
            self?.boost.screensChanged()
        })
        let workspace = NSWorkspace.shared.notificationCenter
        observers.append(workspace.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.boost.setPaused(true)
        })
        observers.append(workspace.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.boost.setPaused(false)
        })
        discover()
    }

    /// The displays macOS has now; new externals are asked over DDC whether they answer.
    func discover() {
        let channels = DDCDiscovery.externalServices()
        defer { channels.forEach { IOObjectRelease($0.proxy) } }
        let screens = NSScreen.screens.filter { CGDisplayMirrorsDisplay($0.displayID) == kCGNullDirectDisplay }
        let externals = screens.filter { CGDisplayIsBuiltin($0.displayID) == 0 }
        let pairs = DDCDiscovery.match(
            displays: externals.map { ($0.displayID, CGDisplayVendorNumber($0.displayID), CGDisplayModelNumber($0.displayID)) },
            channels: channels.map { ($0.vendor, $0.product) })
        var next: [Display] = []
        for screen in screens {
            let id = screen.displayID
            let builtin = CGDisplayIsBuiltin(id) != 0
            let display = displays.first { $0.id == id } ?? Display(id: id, name: "", builtin: builtin)
            display.name = builtin ? "Built-in Display" : screen.localizedName
            if builtin {
                let supported = BoostSupport.supported(builtin: true,
                                                       potentialHeadroom: Double(screen.maximumPotentialExtendedDynamicRangeColorComponentValue),
                                                       chipIgnoresGamma: chipIgnoresGamma)
                display.top = supported ? Scale.boostTop : Scale.plainTop
                display.xdr = Double(screen.maximumPotentialExtendedDynamicRangeColorComponentValue) >= BoostSupport.minimumPotentialHeadroom
                if display.brightness == nil || !boost.requested {
                    readBuiltin(display)
                }
            } else if display.channel == nil, let index = pairs[id] {
                let found = channels[index]
                display.channel = DDCChannel(proxy: found.proxy, vendor: found.vendor, product: found.product,
                                             chipAddress: found.chipAddress)
                if display.channel == nil {
                    display.answered = false
                }
            } else if display.channel == nil {
                display.answered = false  // no DDC channel for it: DisplayLink, AirPlay, Sidecar…
            }
            next.append(display)
        }
        lock.lock()
        displays = next
        lock.unlock()
        for display in next where !display.builtin && display.channel != nil && display.answered == nil {
            probe(display)
        }
        describe()
        route()
        changed()
    }

    /// Reads every display again: the menu or the tab opened.
    func refresh() {
        for display in displays {
            if display.builtin {
                if !boost.requested {
                    readBuiltin(display)
                }
            } else if display.channel != nil {
                probe(display)
            }
        }
        changed()
    }

    private func readBuiltin(_ display: Display) {
        if boost.requested {
            display.brightness = boost.percent
        } else if DisplayServices.canAdjust(display.id), let value = DisplayServices.brightness(display.id) {
            display.brightness = value * 100
        } else {
            display.brightness = nil
        }
        display.linear = DisplayServices.linearBrightness(display.id)
    }

    /// Asks an external display for its brightness, volume and mute, on the DDC queue.
    private func probe(_ display: Display) {
        guard let channel = display.channel, !probing.contains(display.id) else {
            return
        }
        probing.insert(display.id)
        Self.ddcQueue.async { [weak self] in
            let brightness = channel.read(.brightness)
            let volume = brightness == nil ? nil : channel.read(.volume)
            let mute = volume == nil ? nil : channel.read(.mute)
            DispatchQueue.main.async {
                guard let self else {
                    return
                }
                self.probing.remove(display.id)
                let first = display.answered == nil
                display.answered = brightness != nil
                if let brightness {
                    display.brightnessMax = brightness.maximum
                    display.brightness = Scale.percent(current: brightness.current, maximum: brightness.maximum)
                }
                if let volume {
                    display.volumeMax = volume.maximum
                    display.volume = Scale.percent(current: volume.current, maximum: volume.maximum)
                }
                if let mute {
                    display.muted = mute.current == 1
                }
                if first && brightness == nil {
                    self.log("\(display.name) doesn't answer DDC, so its brightness stays its own")
                }
                self.describe()
                self.route()
                self.changed()
            }
        }
    }

    /// app.log: the displays and what can be adjusted, once per change.
    private func describe() {
        let parts = displays.map { d -> String in
            if d.builtin {
                return d.brightness == nil ? "\(d.name) (can't be adjusted)" : "\(d.name) (\(d.top > Scale.plainTop ? "boost to 160%" : "to 100%"))"
            }
            guard let answered = d.answered else {
                return "\(d.name) (asking DDC)"
            }
            return answered ? "\(d.name) (DDC: brightness\(d.volume != nil ? ", volume" : ""))" : "\(d.name) (no DDC)"
        }
        let line = "Displays: " + (parts.isEmpty ? "none" : parts.joined(separator: ", "))
        if line != lastLogged, !line.contains("asking DDC") {
            lastLogged = line
            log(line)
        }
    }

    // ── setting values ──

    func setBrightness(_ id: CGDirectDisplayID, _ percent: Double) {
        guard let display = displays.first(where: { $0.id == id }) else {
            return
        }
        let value = max(0, min(display.top, percent))
        if display.builtin {
            if value > Scale.plainTop {
                if (DisplayServices.brightness(id) ?? 0) < 0.999 {
                    DisplayServices.setBrightness(id, 1)
                }
                boost.set(value, display: id)
            } else {
                boost.set(Scale.plainTop, display: id)  // off before macOS's brightness goes down
                DisplayServices.setBrightness(id, value / 100)
            }
            display.brightness = value
            display.linear = DisplayServices.linearBrightness(id)
        } else if display.adjustable {
            display.brightness = value
            write(display, .brightness, Scale.ddcValue(percent: value, maximum: display.brightnessMax))
        }
    }

    func setVolume(_ id: CGDirectDisplayID, _ percent: Double) {
        guard let display = displays.first(where: { $0.id == id }), display.volume != nil else {
            return
        }
        let value = max(0, min(100, percent))
        display.volume = value
        write(display, .volume, Scale.ddcValue(percent: value, maximum: display.volumeMax))
        if display.muted == true {
            display.muted = false
            write(display, .mute, 2)
        }
    }

    private func toggleMute(_ display: Display) {
        if let muted = display.muted {
            display.muted = !muted
            write(display, .mute, muted ? 2 : 1)
        }
    }

    /// Queues a DDC write. Only the latest value for each display and feature is written, so dragging a
    /// slider doesn't build a backlog.
    private func write(_ display: Display, _ vcp: VCP, _ value: Int) {
        guard display.channel != nil else {
            return
        }
        Self.ddcQueue.async { [weak self] in
            guard let self else {
                return
            }
            self.pendingWrites["\(display.id).\(vcp.rawValue)"] = (display, vcp, value)
            guard !self.writing else {
                return
            }
            self.writing = true
            while let (key, write) = self.pendingWrites.first {
                self.pendingWrites.removeValue(forKey: key)
                if let channel = write.0.channel, !channel.write(write.1, write.2) {
                    let name = write.0.name
                    DispatchQueue.main.async { self.log("\(name) didn't take a DDC write") }
                }
                usleep(50_000)  // DDC/CI asks for 50 ms between commands
            }
            self.writing = false
        }
    }

    // ── the keys ──

    func setKeys(_ on: Bool) -> Int32 {
        keysWanted = on
        guard on else {
            stopKeys()
            return 0
        }
        if keyTap != nil {
            return 1
        }
        guard AXIsProcessTrusted() else {
            return -1
        }
        sound.listen(true)
        route()
        guard let tap = KeyTap.start(KeyEngine(routing: routing, onPress: { [weak self] in self?.pressed($0) })) else {
            sound.listen(false)
            return -2
        }
        keyTap = tap
        return 1
    }

    func stopKeys() {
        keyTap?.stop()
        keyTap = nil
        sound.listen(false)
    }

    var keysRunning: Bool {
        return keyTap != nil
    }

    /// Which keys the tap takes: brightness while any display can be adjusted, volume and mute while
    /// macOS's sound goes to a display whose volume answers DDC.
    private func route() {
        let anyAdjustable = displays.contains { $0.adjustable }
        routing.set(brightness: anyAdjustable, volume: soundTarget() != nil)
    }

    private func soundTarget() -> Display? {
        guard keyTap != nil || keysWanted else {
            return nil
        }
        let candidates = displays.filter { !$0.builtin && $0.volume != nil }
        let id = soundDisplay(output: sound.current(), candidates: candidates.map { ($0.id, $0.name) })
        return candidates.first { $0.id == id }
    }

    /// A key press, on the main queue.
    private func pressed(_ press: KeyPress) {
        if press.key.isBrightness {
            guard let display = keyDisplay() else {
                return
            }
            if display.builtin && !boost.requested {
                readBuiltin(display)  // macOS's own brightness may have moved since
            }
            let next = Scale.stepped(display.brightness ?? 0, up: press.key == .brightnessUp, fine: press.fine, top: display.top)
            setBrightness(display.id, next)
            showHUD(brightnessOf: display)
        } else {
            guard let display = soundTarget(), let volume = display.volume else {
                return
            }
            switch press.key {
            case .mute:
                toggleMute(display)
            default:
                setVolume(display.id, Scale.volumeStepped(volume, up: press.key == .volumeUp, fine: press.fine))
            }
            showHUD(volumeOf: display)
        }
        changed()
    }

    /// The display under the pointer, or the built-in one when that can't be adjusted.
    private func keyDisplay() -> Display? {
        let point = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(point, $0.frame, false) }
        if let display = displays.first(where: { $0.id == screen?.displayID }), display.adjustable {
            return display
        }
        return displays.first { $0.builtin && $0.adjustable }
    }

    private func showHUD(brightnessOf display: Display) {
        guard let screen = NSScreen.screens.first(where: { $0.displayID == display.id }) else {
            return
        }
        let percent = display.brightness ?? 0
        let nits = display.builtin && display.xdr
            ? Scale.nits(percent: percent, linear: display.linear ?? 0, headroom: boost.active ? boost.headroom : 1) : nil
        HUD.shared.show(HUDContent(symbol: "sun.max.fill", value: percent, top: display.top,
                                   segments: Scale.segments(top: display.top), bubble: Scale.bubble(percent: percent, nits: nits)),
                        on: screen)
    }

    private func showHUD(volumeOf display: Display) {
        guard let screen = NSScreen.screens.first(where: { $0.displayID == display.id }) else {
            return
        }
        let volume = display.volume ?? 0
        let muted = display.muted == true
        let symbol = muted ? "speaker.slash.fill" : volume == 0 ? "speaker.fill"
            : volume < 34 ? "speaker.wave.1.fill" : volume < 67 ? "speaker.wave.2.fill" : "speaker.wave.3.fill"
        HUD.shared.show(HUDContent(symbol: symbol, value: volume, top: 100, segments: Int(Scale.volumeSteps),
                                   bubble: muted ? "Muted" : Scale.bubble(percent: volume.rounded(), nits: nil), dimmed: muted),
                        on: screen)
    }

    // ── for Python ──

    /// [{id, name, builtin, adjustable, answered (null while DDC is being asked), percent, top, nits,
    ///   volume, muted, boosting}]
    func json() -> String {
        lock.lock()
        let list = displays
        lock.unlock()
        let items: [[String: Any]] = list.map { d in
            var item: [String: Any] = ["id": d.id, "name": d.name, "builtin": d.builtin, "adjustable": d.adjustable,
                                       "top": d.top, "boosting": d.builtin && boost.active]
            item["answered"] = d.builtin ? true : (d.answered.map { $0 as Any } ?? NSNull())
            item["percent"] = d.brightness.map { ($0 * 10).rounded() / 10 } ?? NSNull()
            item["volume"] = d.volume.map { $0.rounded() } ?? NSNull()
            item["muted"] = d.muted ?? NSNull()
            if d.builtin && d.xdr, let percent = d.brightness {
                item["nits"] = Scale.shown(Scale.nits(percent: percent, linear: d.linear ?? 0, headroom: boost.active ? boost.headroom : 1))
            } else {
                item["nits"] = NSNull()
            }
            return item
        }
        guard let data = try? JSONSerialization.data(withJSONObject: items, options: [.sortedKeys]) else {
            return "[]"
        }
        return String(decoding: data, as: UTF8.self)
    }
}

// ── the C functions ──

public typealias ChangedCallback = @convention(c) () -> Void
public typealias LogCallback = @convention(c) (UnsafePointer<CChar>) -> Void

private func onMain(_ work: @escaping () -> Void) {
    if Thread.isMainThread {
        work()
    } else {
        DispatchQueue.main.async(execute: work)
    }
}

@_cdecl("displays_start")
public func displays_start(_ chipIgnoresGamma: Bool, _ changed: ChangedCallback?, _ log: LogCallback?) {
    onMain {
        let controller = Controller.shared
        controller.changed = { changed?() }
        controller.log = { line in line.withCString { log?($0) } }
        controller.start(chipIgnoresGamma: chipIgnoresGamma)
    }
}

@_cdecl("displays_list")
public func displays_list() -> UnsafeMutablePointer<CChar>? {
    return strdup(Controller.shared.json())
}

@_cdecl("displays_free")
public func displays_free(_ pointer: UnsafeMutablePointer<CChar>?) {
    free(pointer)
}

@_cdecl("displays_refresh")
public func displays_refresh() {
    onMain { Controller.shared.refresh() }
}

@_cdecl("displays_set_brightness")
public func displays_set_brightness(_ display: UInt32, _ percent: Double) {
    onMain { Controller.shared.setBrightness(display, percent) }
}

@_cdecl("displays_set_volume")
public func displays_set_volume(_ display: UInt32, _ percent: Double) {
    onMain { Controller.shared.setVolume(display, percent) }
}

@_cdecl("displays_set_keys")
public func displays_set_keys(_ on: Bool) -> Int32 {
    guard Thread.isMainThread else {  // Python sends it to the main thread; never wait for it from here
        DispatchQueue.main.async { _ = Controller.shared.setKeys(on) }
        return on ? (Controller.shared.keysRunning ? 1 : -2) : 0
    }
    return Controller.shared.setKeys(on)
}

@_cdecl("displays_stop_keys")
public func displays_stop_keys() {
    onMain { Controller.shared.stopKeys() }
}

@_cdecl("displays_keys_running")
public func displays_keys_running() -> Bool {
    return Controller.shared.keysRunning
}
