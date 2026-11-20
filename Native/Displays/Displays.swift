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
// own: each call waits for the monitor. What the app set is remembered rather than read back. The
// IORegistry walk that finds the externals' DDC channels (10–14 ms) runs on that queue too.
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

/// Eases the built-in display's brightness to a key step's target over macOS's own fade time
/// (Scale.fadeFor), on the main thread with a short timer that stops when it arrives: never a
/// continuous animation. The timer fires at user-interactive QoS, since the main thread sits at
/// background QoS while the app is idle (priority.py), where timers are coalesced and fire late.
final class Fade {
    static let interval = 1.0 / 60
    private(set) var level = 0.0
    private(set) var target = 0.0
    private var from = 0.0
    private var began = Date.distantPast
    private var source: DispatchSourceTimer?
    var step: (Double) -> Void = { _ in }  // puts a level on the screen
    var finished: () -> Void = {}

    var running: Bool {
        return source != nil
    }

    /// From where a running fade is now (or `current` when none is), towards `target`.
    func go(from current: Double, to target: Double) {
        from = running ? level : current
        self.target = target
        began = Date()
        if source == nil {
            let timer = DispatchSource.makeTimerSource(flags: .strict, queue: .main)
            timer.schedule(deadline: .now() + Self.interval, repeating: Self.interval, leeway: .milliseconds(2))
            timer.setEventHandler(qos: .userInteractive, flags: .enforceQoS) { [weak self] in self?.tick() }
            timer.resume()
            source = timer
        }
    }

    func stop() {
        source?.cancel()
        source = nil
    }

    private func tick() {
        let progress = Date().timeIntervalSince(began) / Scale.fadeFor
        level = Scale.eased(from: from, to: target, progress: progress)
        step(level)
        if progress >= 1 {
            stop()
            finished()
        }
    }
}

final class Controller {
    static let shared = Controller()
    /// The slider at or above this on the built-in XDR display puts the screen in EDR ahead of a boost.
    static let prepareFrom = 90.0
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
    private var walk = 0  // the latest IORegistry walk asked for; an older one's result is dropped
    private var shapes: [ScreenShape]?  // the screens as discover() last saw them
    private var changeSoon: DispatchWorkItem?
    private let fade = Fade()
    private var fading: CGDirectDisplayID = 0  // the display the fade is on
    private var keysLogged: String?
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
            self.fade.stop()
            if let builtin = self.displays.first(where: { $0.builtin }) {
                self.readBuiltin(builtin)
            }
            self.changed()
        }
        boost.onWritten = { [weak self] in self?.changedSoon() }
        fade.step = { [weak self] level in
            guard let self else {
                return
            }
            self.putBuiltin(self.fading, level, target: self.fade.target)
        }
        fade.finished = { [weak self] in self?.fadeFinished() }
        sound.onChange = { [weak self] in self?.route() }
        let center = NotificationCenter.default
        // macOS posts this every time a screen's EDR headroom moves (dozens a second while the boost's
        // trigger ramps it up), not only when the screens change: each part checks what really did.
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

    /// The displays macOS has now, if they changed. Externals without a DDC channel are looked for on
    /// the DDC queue, then asked whether they answer; the built-in display is in the list at once.
    func discover() {
        let screens = NSScreen.screens.filter { CGDisplayMirrorsDisplay($0.displayID) == kCGNullDirectDisplay }
        let shapes = screens.map { ScreenShape(id: $0.displayID, frame: $0.frame, name: $0.localizedName) }
        guard ScreenChange.changed(self.shapes, shapes) else {
            return  // only a headroom moved
        }
        self.shapes = shapes
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
            }
            next.append(display)
        }
        lock.lock()
        displays = next
        lock.unlock()
        let externals = next.filter { !$0.builtin }
        if externals.contains(where: { $0.channel == nil }) {
            findChannels(externals)
        }
        describe()
        route()
        changed()
    }

    /// Finds DDC channels for the externals that have none, on the DDC queue (the IORegistry walk takes
    /// 10–14 ms), and gives them to those displays on the main thread. Every external takes part in
    /// the matching, so the ones that have their channel already keep theirs out of it.
    private func findChannels(_ externals: [Display]) {
        walk += 1
        let asked = walk
        let ids = externals.map { ($0.id, CGDisplayVendorNumber($0.id), CGDisplayModelNumber($0.id)) }
        let wanted = Set(externals.filter { $0.channel == nil }.map { $0.id })
        Self.ddcQueue.async { [weak self] in
            let found = DDCDiscovery.externalServices()
            let pairs = DDCDiscovery.match(displays: ids, channels: found.map { ($0.vendor, $0.product) })
            var channels: [CGDirectDisplayID: DDCChannel] = [:]
            for (id, index) in pairs where wanted.contains(id) {
                let f = found[index]
                channels[id] = DDCChannel(proxy: f.proxy, vendor: f.vendor, product: f.product, chipAddress: f.chipAddress)
            }
            found.forEach { IOObjectRelease($0.proxy) }
            DispatchQueue.main.async {
                guard let self, asked == self.walk else {
                    return
                }
                for display in self.displays where wanted.contains(display.id) && display.channel == nil {
                    if let channel = channels[display.id] {
                        display.channel = channel
                        display.answered = nil  // asked, even if an earlier look found no channel for it
                    } else {
                        display.answered = false  // no DDC channel for it: DisplayLink, AirPlay, Sidecar…
                    }
                }
                for display in self.displays where !display.builtin && display.channel != nil && display.answered == nil {
                    self.probe(display)
                }
                self.describe()
                self.route()
                self.changed()
            }
        }
    }

    /// Tells Python once the boost's table has settled: while macOS ramps the headroom up it's written
    /// dozens of times a second, and only the last one changes what the menu and the page show.
    private func changedSoon() {
        changeSoon?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.changed() }
        changeSoon = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: item)
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
        if fade.running && fading == display.id {
            return  // the fade's target stands
        }
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
            // The slider follows the finger directly. Near 100% the screen goes into EDR first, so
            // crossing it is as seamless as the keys' fades.
            fade.stop()
            if display.top > Scale.plainTop && value >= Self.prepareFrom {
                boost.prepare(display: id)
            }
            putBuiltin(id, value)
            display.brightness = value
            display.linear = DisplayServices.linearBrightness(id)
        } else if display.adjustable {
            display.brightness = value
            write(display, .brightness, Scale.ddcValue(percent: value, maximum: display.brightnessMax))
        }
    }

    /// Puts a level on the built-in display: macOS's own brightness up to 100%, the boost's factor
    /// above it. Crossing upward, macOS's brightness is at its maximum before the factor rises;
    /// downward, the factor is back at ×1 before macOS's brightness comes down.
    private func putBuiltin(_ id: CGDirectDisplayID, _ level: Double, target: Double? = nil) {
        if level > Scale.plainTop {
            if (DisplayServices.brightness(id) ?? 0) < 0.999 {
                boost.ownBrightnessChange()
                DisplayServices.setBrightness(id, 1)
            }
            boost.set(level, display: id, target: target)
        } else {
            boost.set(Scale.plainTop, display: id)
            boost.ownBrightnessChange()
            DisplayServices.setBrightness(id, level / 100)
        }
    }

    /// A key step's fade arrived: the nits a 100% or lower step reaches are known now.
    private func fadeFinished() {
        guard let display = displays.first(where: { $0.id == fading }) else {
            return
        }
        display.linear = DisplayServices.linearBrightness(display.id)
        if let content = brightnessHUD(display) {
            HUD.shared.update(content)
        }
        changed()
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
        route()  // app.log: what it takes
        return 1
    }

    func stopKeys() {
        keyTap?.stop()
        keyTap = nil
        keysLogged = nil
        sound.listen(false)
    }

    var keysRunning: Bool {
        return keyTap != nil
    }

    /// Which keys the tap takes: brightness while any display can be adjusted, volume and mute while
    /// macOS's sound goes to a display whose volume answers DDC.
    /// app.log has what the tap takes while it runs, again whenever that changes.
    private func route() {
        let anyAdjustable = displays.contains { $0.adjustable }
        let output = keyTap != nil || keysWanted ? sound.current() : nil
        let target = soundTarget(output)
        routing.set(brightness: anyAdjustable, volume: target != nil)
        guard keyTap != nil else {
            return
        }
        let line = keysLine(brightness: anyAdjustable, volumeTo: target?.name, soundToDisplay: output?.display ?? false)
        if line != keysLogged {
            keysLogged = line
            log(line)
        }
    }

    private func soundTarget(_ output: (name: String, display: Bool)?? = nil) -> Display? {
        guard keyTap != nil || keysWanted else {
            return nil
        }
        let candidates = displays.filter { !$0.builtin && $0.volume != nil }
        let id = soundDisplay(output: output ?? sound.current(), candidates: candidates.map { ($0.id, $0.name) })
        return candidates.first { $0.id == id }
    }

    /// A key press, on the main queue.
    private func pressed(_ press: KeyPress) {
        if press.key.isBrightness {
            guard let display = keyDisplay() else {
                return
            }
            let up = press.key == .brightnessUp
            if display.builtin {
                // Each step eases over macOS's own fade time; a press during a fade aims one step past
                // its target, from where it is now. The HUD shows the target at once.
                if !(fade.running && fading == display.id) && !boost.requested {
                    readBuiltin(display)  // macOS's own brightness may have moved since
                }
                if fading != display.id {
                    fade.stop()
                }
                let from = display.brightness ?? 0
                let next = Scale.stepped(from, up: up, fine: press.fine, top: display.top)
                if display.top > Scale.plainTop && up && next >= Scale.plainTop {
                    boost.prepare(display: display.id)  // EDR ahead of the boost: no wait at 100%
                }
                display.brightness = next
                fading = display.id
                fade.go(from: from, to: next)
            } else {
                setBrightness(display.id, Scale.stepped(display.brightness ?? 0, up: up, fine: press.fine, top: display.top))
            }
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
        guard let screen = NSScreen.screens.first(where: { $0.displayID == display.id }),
              let content = brightnessHUD(display) else {
            return
        }
        HUD.shared.show(content, on: screen)
    }

    /// The HUD for a display's brightness: its target. Past 100% the nits are the scale's (500 ×
    /// ratio, as BetterDisplay shows them); at 100% and below, macOS's linear brightness, known once a
    /// fade has arrived (the HUD is updated then).
    private func brightnessHUD(_ display: Display) -> HUDContent? {
        let percent = display.brightness ?? 0
        let nits = display.builtin && display.xdr
            ? Scale.nits(percent: percent, linear: display.linear ?? 0, headroom: .infinity) : nil
        return HUDContent(symbol: "sun.max.fill", value: percent, top: display.top,
                          segments: Scale.segments(top: display.top), bubble: Scale.bubble(percent: percent, nits: nits))
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
