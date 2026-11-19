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
// - At alpha 0.003 (BetterDisplay's), the pixel can't be seen and still triggers EDR.
// When the app quits or crashes, macOS puts the user's ColorSync tables back.
//
// Main thread only.

import AppKit
import Metal
import QuartzCore

final class Boost {
    static let tableSize: UInt32 = 256
    static let integrityInterval = 5.0  // seconds between checks that the table is still ours
    static let rampInterval = 0.1  // while macOS's headroom settles after the trigger appears
    static let rampFor = 3.0
    static let wakeSettle = 2.0  // seconds after the screens wake before the boost comes back

    /// What the boost asks for (percent, 100 = none) and whether it's paused (screens asleep).
    private(set) var percent = 100.0
    private var paused = false
    private var display: CGDirectDisplayID = 0
    private var window: NSWindow?
    private var layer: CAMetalLayer?
    private var device: MTLDevice?
    private var queue: MTLCommandQueue?
    private var base: (r: [CGGammaValue], g: [CGGammaValue], b: [CGGammaValue])?
    private var written: Double = 1  // the factor last written to the table
    private var timer: Timer?
    private var rampUntil = Date.distantPast
    private var observing: CGDirectDisplayID = 0
    private var reported = false  // app.log has the first table write (or no headroom) since the trigger appeared
    /// The boost ended because macOS's brightness went below its maximum (its reason, for app.log).
    var onEnded: (String) -> Void = { _ in }
    /// It's on (with its gamma factor and headroom), or off, or paused (why), for app.log.
    var onLog: (String) -> Void = { _ in }

    var active: Bool {
        return percent > Scale.plainTop && !paused && window != nil
    }

    /// Asked for, even while paused for the screens' sleep: the built-in display's percent is the boost's.
    var requested: Bool {
        return percent > Scale.plainTop
    }

    /// The headroom macOS grants the built-in display now.
    var headroom: Double {
        return Double(screen(for: display)?.maximumExtendedDynamicRangeColorComponentValue ?? 1)
    }

    /// Boost the built-in display to `percent` (100 or less turns it off). macOS's own brightness must
    /// be at its maximum; the caller sets it first.
    func set(_ percent: Double, display: CGDirectDisplayID) {
        let was = self.percent
        self.percent = percent
        self.display = display
        guard percent > Scale.plainTop else {
            if was > Scale.plainTop {
                stop()
                onLog("Display boost off")
            }
            return
        }
        guard !paused else {
            return
        }
        if window == nil {
            start()
            onLog(String(format: "Display boost on at %.0f%%", percent))
        } else {
            apply()
        }
    }

    /// The screens went to sleep (true) or woke (false): the trigger and table go while they sleep.
    func setPaused(_ asleep: Bool) {
        guard asleep != paused else {
            return
        }
        paused = asleep
        guard percent > Scale.plainTop else {
            return
        }
        if asleep {
            stop()
            onLog("Display boost paused (the screens are asleep)")
            return
        }
        // macOS may still be bringing its brightness back as the screens wake.
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.wakeSettle) { [weak self] in
            guard let self, !self.paused, self.percent > Scale.plainTop, self.window == nil else {
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

    /// The displays were reconfigured: the trigger moves to the built-in display's new frame, and the
    /// table is read again (macOS may have reset it).
    func screensChanged() {
        guard active else {
            return
        }
        stop()
        start()
    }

    // ── the trigger and the table ──

    private func start() {
        guard let screen = screen(for: display) else {
            return
        }
        CGDisplayRestoreColorSyncSettings()  // the user's own table, as the base to multiply
        base = Self.readTable(display)
        written = 1
        makeTrigger(on: screen)
        if observing != display, DisplayServices.observe(display, brightnessChanged) {
            observing = display
        }
        Boost.current = self
        reported = false
        rampUntil = Date().addingTimeInterval(Self.rampFor)
        schedule(Self.rampInterval)
        apply()
    }

    private func stop() {
        timer?.invalidate()
        timer = nil
        if observing != 0 {
            DisplayServices.stopObserving(observing)
            observing = 0
        }
        window?.orderOut(nil)
        window = nil
        layer = nil
        if written != 1 {
            CGDisplayRestoreColorSyncSettings()
        }
        written = 1
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
        if Date() > rampUntil, timer?.timeInterval != Self.integrityInterval {
            schedule(Self.integrityInterval)
            if !reported {
                reported = true
                onLog(String(format: "Display boost has no EDR headroom from macOS yet (%.2f)", headroom))
            }
        }
        // Something else (ColorSync, Night Shift, another app) put its own table in: ours goes back.
        if written != 1, let top = Self.readTable(display)?.r.last, let ours = base?.r.last,
           abs(Double(top) - Double(ours) * written) > 0.003 {
            base = nil
            CGDisplayRestoreColorSyncSettings()
            base = Self.readTable(display)
            written = 1
        }
        apply()
    }

    /// Writes the table for the headroom macOS grants now (nothing until the screen is in EDR).
    private func apply() {
        guard active, let base else {
            return
        }
        let factor = Scale.gammaFactor(percent: percent, headroom: headroom)
        guard abs(factor - written) > 0.0005 else {
            return
        }
        let f = CGGammaValue(factor)
        if CGSetDisplayTransferByTable(display, Self.tableSize, base.r.map { $0 * f }, base.g.map { $0 * f },
                                       base.b.map { $0 * f }) == .success {
            written = factor
            if !reported {
                reported = true
                onLog(String(format: "Display boost at ×%.3f, macOS's headroom %.2f", factor, headroom))
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
        guard percent > Scale.plainTop, value < 0.999 else {
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
