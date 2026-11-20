// The built-in XDR display's boost past 100%: BrightIntosh's method (GPL-3.0, so evidence only; none
// of its code is here), checked on the maintainer's M1 Pro on macOS 27.2.1.
//
// A 1×1 window above everything holds a Metal layer with EDR content, which puts the screen in EDR
// mode: macOS raises the backlight and grants headroom (3.2× at full brightness on the 16" M1 Pro).
// The display's gamma table then lifts everything into that headroom. Measured there:
// - The table must be written with 256 entries. Written with 1,024 (the size macOS reports), every
//   value is clamped to 1.0, which only washes the picture out.
// - The trigger is drawn once. Drawn once, ×1.3 held the screen at 13.6 W (from 9.3 W) for a minute,
//   so nothing redraws while boosted, and nothing runs at all at or below 100%.
// - macOS posts a screen-parameters notification every time the headroom moves (dozens a second while
//   it ramps up after the trigger appears): each one only writes the factor for the new headroom
//   (screensChanged). The trigger is made again only when the built-in display itself moved; making
//   it again on every one restored the table each time and flickered the screen (1.5.0's test copy).
// - At alpha 0.003 (BetterDisplay's), the pixel can't be seen and still triggers EDR.
// - Entering EDR isn't visible: as the trigger appears, macOS ramps the backlight from 600 to 1,600
//   nits over 0.75 s while WindowServer holds SDR white at 500 nits (its log). It costs about 2 W:
//   the screen drew 9.30 W before and 11.34 W with the trigger at ×1, at full brightness. macOS
//   itself keeps EDR for 15 s after the trigger goes.
// - The trigger stays for `triggerGrace` after the boost drops back to 100%, so stepping across 100%
//   doesn't take it away and make it again; a key step aiming past 100% (or the slider near it) makes
//   it ahead of the boost (prepare), so macOS has granted the headroom by the time the factor rises.
// When the app quits or crashes, macOS puts the user's ColorSync tables back.
//
// Main thread only.

import AppKit
import Metal
import QuartzCore

final class Boost {
    static let tableSize: UInt32 = 256
    static let integrityInterval = 5.0  // seconds between checks that the table is still ours
    static let headroomWait = 3.0  // app.log says so if macOS hasn't granted any headroom by then
    static let wakeSettle = 2.0  // seconds after the screens wake (or the built-in display is back) before the boost comes back
    /// Seconds the trigger stays at ×1 once the boost is back at 100% (or after prepare): about 2 W
    /// while it does (9.30 W against 11.34 W, measured), plus macOS's own 15 s of EDR after it goes.
    static let triggerGrace = 5.0
    /// DisplayServices' notifications for a brightness the controller just set are its own, not macOS's.
    static let ownBrightnessFor = 0.5

    /// What the boost asks for (percent, 100 = none) and whether it's paused (screens asleep).
    private(set) var percent = 100.0
    private var paused = false
    private var gone = false  // the built-in display isn't a screen (the lid closed with an external)
    private var display: CGDirectDisplayID = 0
    private var window: NSWindow?
    private var placedOn: ScreenPlace?  // where the trigger is
    private var layer: CAMetalLayer?
    private var device: MTLDevice?
    private var queue: MTLCommandQueue?
    private var base: (r: [CGGammaValue], g: [CGGammaValue], b: [CGGammaValue])?
    private var written: Double = 1  // the factor last written to the table
    /// The table's top red entry as macOS reads it back after our last write: the 5 s check compares
    /// against this, not against base × factor, in case macOS clamps or resamples what it reads back
    /// (it clamps a 1,024-entry write), which would otherwise restore and rewrite the table, and dip
    /// the screen to 100%, every 5 s.
    private var reference: CGGammaValue?
    private var timer: Timer?
    private var starts = 0
    private var observing: CGDirectDisplayID = 0
    private var reported = false  // app.log has the first table write (or no headroom) since the trigger appeared
    private var release: DispatchWorkItem?  // takes the trigger away after the grace period
    private var ignoreBrightnessUntil = Date.distantPast
    /// The boost ended because macOS's brightness went below its maximum (its reason, for app.log).
    var onEnded: (String) -> Void = { _ in }
    /// It's on (with its gamma factor and headroom), or off, or paused (why), for app.log.
    var onLog: (String) -> Void = { _ in }
    /// The table was written with a new factor, or put back: the nits and "boosting" it lists change.
    var onWritten: () -> Void = {}

    /// Boosting: past 100% with the trigger up.
    var active: Bool {
        return percent > Scale.plainTop && !paused && window != nil
    }

    /// The screen is in EDR because of the trigger (boosting, or at ×1 for the grace period).
    var hasTrigger: Bool {
        return window != nil
    }

    /// Asked for, even while paused for the screens' sleep: the built-in display's percent is the boost's.
    var requested: Bool {
        return percent > Scale.plainTop
    }

    /// The headroom macOS grants the built-in display now.
    var headroom: Double {
        return Double(screen(for: display)?.maximumExtendedDynamicRangeColorComponentValue ?? 1)
    }

    /// Boost the built-in display to `percent`. Above 100%, macOS's own brightness must be at its
    /// maximum (the caller sets it first). At 100 or less the factor goes back to ×1 at once, before
    /// the caller brings macOS's brightness down, and the trigger stays for the grace period. `target`
    /// is where a fade is going, for app.log.
    func set(_ percent: Double, display: CGDirectDisplayID, target: Double? = nil) {
        let was = self.percent
        self.percent = percent
        self.display = display
        guard percent > Scale.plainTop else {
            if was > Scale.plainTop {
                apply()
                onLog("Display boost off")
                releaseSoon()
            }
            return
        }
        guard !paused, !gone else {
            return
        }
        release?.cancel()
        if was <= Scale.plainTop {
            onLog(String(format: "Display boost on at %.0f%%", target ?? percent))
            reported = false  // its first table write is logged too
        }
        if window == nil {
            start()
        } else {
            apply()
        }
    }

    /// Puts the screen in EDR ahead of a boost (the trigger at ×1): a key step aiming past 100%, or
    /// the slider near it. It goes after the grace period if no boost follows.
    func prepare(display: CGDirectDisplayID) {
        guard !paused, !gone, !requested else {
            return
        }
        self.display = display
        if window == nil {
            start()
        }
        releaseSoon()
    }

    /// The controller is about to set macOS's brightness itself.
    func ownBrightnessChange() {
        ignoreBrightnessUntil = Date().addingTimeInterval(Self.ownBrightnessFor)
    }

    private func releaseSoon() {
        release?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, !self.requested else {
                return
            }
            self.stop()
        }
        release = item
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.triggerGrace, execute: item)
    }

    /// The screens went to sleep (true) or woke (false): the trigger and table go while they sleep.
    func setPaused(_ asleep: Bool) {
        guard asleep != paused else {
            return
        }
        paused = asleep
        if asleep {
            if window != nil {
                stop()
                if requested {
                    onLog("Display boost paused (the screens are asleep)")
                }
            }
            return
        }
        if requested {
            resumeSoon()
        }
    }

    /// macOS changed something about the screens. Most of these only mean the headroom moved, and
    /// apply() writes the factor for it. When the built-in display's ID or frame changed, the trigger
    /// is made again and the table read again (macOS may have reset it); while the built-in display
    /// isn't a screen, the boost pauses.
    func screensChanged() {
        guard requested || window != nil else {
            return
        }
        let now = NSScreen.screens.first { CGDisplayIsBuiltin($0.displayID) != 0 }
            .map { ScreenPlace(id: $0.displayID, frame: $0.frame) }
        if let now {
            display = now.id
        }
        switch ScreenChange.trigger(placed: placedOn, now: now, wasGone: gone) {
        case .keep:
            apply()
        case .move:
            stop()
            if requested {
                start()
            }
        case .gone:
            gone = true
            if window != nil {
                stop()
                if requested {
                    onLog("Display boost paused (the built-in display isn't in use)")
                }
            }
        case .back:
            gone = false
            resumeSoon()
        }
    }

    /// Brings the boost back after the screens wake or the built-in display is back, once macOS has had
    /// a moment (it may still be bringing its brightness back), if its brightness is at its maximum.
    private func resumeSoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.wakeSettle) { [weak self] in
            guard let self, !self.paused, !self.gone, self.percent > Scale.plainTop, self.window == nil else {
                return
            }
            if DisplayServices.brightness(self.display).map({ $0 >= 0.999 }) ?? false {
                self.start()
                self.onLog("Display boost resumed")
            } else {
                self.percent = 100
                self.onEnded("macOS's brightness is below its maximum")
            }
        }
    }

    // ── the trigger and the table ──

    private func start() {
        guard let screen = screen(for: display) else {
            return
        }
        CGDisplayRestoreColorSyncSettings()  // the user's own table, as the base to multiply
        base = Self.readTable(display)
        written = 1
        reference = nil
        makeTrigger(on: screen)
        if observing != display, DisplayServices.observe(display, brightnessChanged) {
            observing = display
        }
        Boost.current = self
        reported = false
        schedule(Self.integrityInterval)
        // Nothing polls while macOS ramps the headroom up: each step posts a screen-parameters
        // notification, and screensChanged() writes the factor for it.
        starts += 1
        let started = starts
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.headroomWait) { [weak self] in
            guard let self, self.starts == started, self.active else {
                return
            }
            self.apply()
            if !self.reported {
                self.reported = true
                self.onLog(String(format: "Display boost has no EDR headroom from macOS yet (%.2f)", self.headroom))
            }
        }
        apply()
    }

    private func stop() {
        release?.cancel()
        release = nil
        timer?.invalidate()
        timer = nil
        if observing != 0 {
            DisplayServices.stopObserving(observing)
            observing = 0
        }
        window?.orderOut(nil)
        window = nil
        layer = nil
        placedOn = nil
        if written != 1 {
            CGDisplayRestoreColorSyncSettings()
            onWritten()
        }
        written = 1
        reference = nil
        base = nil
    }

    private func schedule(_ interval: Double) {
        timer?.invalidate()
        let made = Timer(timeInterval: interval, repeats: true) { [weak self] _ in self?.tick() }
        made.tolerance = interval / 5
        RunLoop.main.add(made, forMode: .common)
        timer = made
    }

    private func tick() {
        guard active else {
            return
        }
        // Something else (ColorSync, Night Shift, another app) put its own table in: ours goes back.
        if written != 1, let reference, let top = Self.readTable(display)?.r.last,
           abs(Double(top) - Double(reference)) > 0.003 {
            CGDisplayRestoreColorSyncSettings()
            base = Self.readTable(display)
            written = 1
            self.reference = nil
        }
        apply()
    }

    /// Writes the table for the headroom macOS grants now (nothing until the screen is in EDR), or ×1
    /// at 100% and below while the trigger stays.
    private func apply() {
        guard window != nil, !paused, let base else {
            return
        }
        let factor = percent > Scale.plainTop ? Scale.gammaFactor(percent: percent, headroom: headroom) : 1
        guard abs(factor - written) > 0.0005 else {
            return
        }
        let f = CGGammaValue(factor)
        if CGSetDisplayTransferByTable(display, Self.tableSize, base.r.map { $0 * f }, base.g.map { $0 * f },
                                       base.b.map { $0 * f }) == .success {
            written = factor
            onWritten()
            reference = Self.readTable(display)?.r.last
            if !reported {
                reported = true
                let wrote = Double(base.r.last ?? 1) * factor
                let readBack = reference.map { String(format: "%.3f", Double($0)) } ?? "nothing"
                onLog(String(format: "Display boost at ×%.3f, macOS's headroom %.2f (the table's top entry: wrote %.3f, reads back ",
                             factor, headroom, wrote) + readBack + ")")
            }
        }
    }

    private static func readTable(_ display: CGDirectDisplayID) -> (r: [CGGammaValue], g: [CGGammaValue], b: [CGGammaValue])? {
        var r = [CGGammaValue](repeating: 0, count: Int(tableSize)), g = r, b = r
        var count: UInt32 = 0
        guard CGGetDisplayTransferByTable(display, tableSize, &r, &g, &b, &count) == .success, count == tableSize else {
            return nil
        }
        return (r, g, b)
    }

    private func makeTrigger(on screen: NSScreen) {
        if device == nil {
            device = MTLCreateSystemDefaultDevice()
            queue = device?.makeCommandQueue()
        }
        guard let device, let queue else {
            return
        }
        let frame = screen.frame
        let window = NSWindow(contentRect: NSRect(x: frame.minX, y: frame.minY, width: 1, height: 1),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.level = NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()) + 1)  // above full-screen games
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        window.ignoresMouseEvents = true
        window.isOpaque = false
        window.hasShadow = false
        window.backgroundColor = .clear
        window.alphaValue = 0.003  // can't be seen, still EDR content
        window.animationBehavior = .none
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 1, height: 1))
        view.wantsLayer = true
        let layer = CAMetalLayer()
        layer.device = device
        layer.pixelFormat = .rgba16Float
        layer.colorspace = CGColorSpace(name: CGColorSpace.extendedLinearSRGB)
        layer.wantsExtendedDynamicRangeContent = true
        layer.drawableSize = CGSize(width: 1, height: 1)
        layer.isOpaque = false
        view.layer = layer
        window.contentView = view
        window.setFrame(NSRect(x: frame.minX, y: frame.minY, width: 1, height: 1), display: false)
        window.orderFrontRegardless()
        self.window = window
        self.layer = layer
        placedOn = ScreenPlace(id: display, frame: frame)
        // One frame of EDR white, once: it stays composited.
        let value = Double(screen.maximumPotentialExtendedDynamicRangeColorComponentValue)
        if let drawable = layer.nextDrawable(), let buffer = queue.makeCommandBuffer() {
            let pass = MTLRenderPassDescriptor()
            pass.colorAttachments[0].texture = drawable.texture
            pass.colorAttachments[0].loadAction = .clear
            pass.colorAttachments[0].storeAction = .store
            pass.colorAttachments[0].clearColor = MTLClearColor(red: value, green: value, blue: value, alpha: 1)
            buffer.makeRenderCommandEncoder(descriptor: pass)?.endEncoding()
            buffer.present(drawable)
            buffer.commit()
        }
    }

    private func screen(for display: CGDirectDisplayID) -> NSScreen? {
        return NSScreen.screens.first { $0.displayID == display }
    }

    // ── macOS's brightness ──

    fileprivate static weak var current: Boost?

    fileprivate func macOSBrightness(_ value: Double) {
        // The controller's own fades set macOS's brightness below its maximum just before a boost, and
        // their notifications can arrive once it's boosting: those, and any the current value
        // contradicts, aren't macOS's.
        guard percent > Scale.plainTop, value < 0.999, Date() > ignoreBrightnessUntil,
              (DisplayServices.brightness(display) ?? value) < 0.999 else {
            return
        }
        stop()
        percent = 100
        onEnded("macOS's brightness went below its maximum")
    }
}

/// DisplayServices' brightness notification (main thread): the boost ends when macOS's own brightness
/// goes below its maximum.
private let brightnessChanged: CFNotificationCallback = { _, _, _, _, info in
    let value = (info as NSDictionary?)?["value"] as? NSNumber
    guard let value else {
        return
    }
    if Thread.isMainThread {
        Boost.current?.macOSBrightness(value.doubleValue)
    } else {
        DispatchQueue.main.async { Boost.current?.macOSBrightness(value.doubleValue) }
    }
}

extension NSScreen {
    var displayID: CGDirectDisplayID {
        return (deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
    }
}
