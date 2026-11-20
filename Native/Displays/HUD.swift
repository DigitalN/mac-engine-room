// The app's own HUD for the brightness and volume keys, in place of macOS's: a rounded dark pill with
// a symbol, a bar of segments and, over the end of the lit part, a bubble such as "130% · 960 nits"
// (after the maintainer's screenshot of BetterDisplay's). It appears on the display being adjusted,
// at the top right under the menu bar, where macOS 26 and later put their own, and fades out about
// 1.5 s after the last press. Main thread only.

import AppKit

struct HUDContent: Equatable {
    var symbol: String  // an SF Symbol
    var value: Double  // where the bar's lit part ends, on the scale 0…top
    var top: Double
    var segments: Int
    var bubble: String?
    var dimmed = false  // muted: the bar shows the volume, greyed
}

final class HUD {
    static let shared = HUD()
    static let size = NSSize(width: 300, height: 62)
    static let margin = NSSize(width: 12, height: 8)  // from the screen's right edge and the menu bar
    static let shownFor = 1.5
    static let fadeFor = 0.25

    private var panel: NSPanel?
    private let view = HUDView(frame: NSRect(origin: .zero, size: HUD.size))
    private var hideTimer: Timer?
    private var shows = 0  // counts presses, so a fade's end knows whether one came after it began

    func show(_ content: HUDContent, on screen: NSScreen) {
        let panel = self.panel ?? makePanel()
        shows += 1
        view.content = content
        let visible = screen.visibleFrame
        let origin = NSPoint(x: screen.frame.maxX - Self.size.width - Self.margin.width,
                             y: visible.maxY - Self.size.height - Self.margin.height)
        panel.setFrameOrigin(origin)
        // Through the animator, so a fade still running is cancelled: setting alphaValue directly
        // leaves it going, and a press during the fade would vanish with it.
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0
            panel.animator().alphaValue = 1
        }
        panel.orderFrontRegardless()
        hideTimer?.invalidate()
        let timer = Timer(timeInterval: Self.shownFor, repeats: false) { [weak self] _ in self?.fade() }
        RunLoop.main.add(timer, forMode: .common)
        hideTimer = timer
    }

    private func fade() {
        guard let panel else {
            return
        }
        let fading = shows
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = Self.fadeFor
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            // A cancelled fade ends too: the panel only goes if no press came since it began.
            if let self, self.shows == fading {
                self.panel?.orderOut(nil)
            }
        })
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: Self.size), styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isReleasedWhenClosed = false
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.overlayWindow)))
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        panel.ignoresMouseEvents = true
        panel.isOpaque = false
        panel.hasShadow = true
        panel.backgroundColor = .clear
        panel.animationBehavior = .none
        panel.contentView = view
        self.panel = panel
        return panel
    }
}

final class HUDView: NSView {
    var content = HUDContent(symbol: "sun.max.fill", value: 0, top: 100, segments: 10) {
        didSet {
            if content != oldValue {
                needsDisplay = true
            }
        }
    }

    // Measured off the screenshot, at this size.
    static let symbolCenterX = 36.0
    static let symbolSize = 25.0
    static let barX = 66.0
    static let barRight = 26.0  // space after the bar
    static let barY = 22.0  // from the bottom
    static let barHeight = 3.5
    static let gap = 1.6
    static let bubbleHeight = 16.0
    static let pointer = 4.0

    override var isFlipped: Bool {
        return false
    }

    override func draw(_ dirtyRect: NSRect) {
        let bounds = self.bounds
        let pill = NSBezierPath(roundedRect: bounds.insetBy(dx: 0.5, dy: 0.5), xRadius: bounds.height / 2 - 0.5,
                                yRadius: bounds.height / 2 - 0.5)
        NSColor(white: 0.15, alpha: 0.96).setFill()
        pill.fill()
        NSColor(white: 1, alpha: 0.16).setStroke()
        pill.lineWidth = 1
        pill.stroke()

        drawSymbol()
        let lit = Scale.lit(content.value, top: content.top, segments: content.segments)
        let barWidth = bounds.width - Self.barX - Self.barRight
        let segment = (barWidth - Self.gap * Double(content.segments - 1)) / Double(content.segments)
        for i in 0..<content.segments {
            let x = Self.barX + Double(i) * (segment + Self.gap)
            let rect = NSRect(x: x, y: Self.barY, width: segment, height: Self.barHeight)
            NSColor(white: 1, alpha: 0.13).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1, yRadius: 1).fill()
            let share = max(0, min(1, lit - Double(i)))
            if share > 0 {
                (content.dimmed ? NSColor(white: 1, alpha: 0.4) : NSColor.white).setFill()
                NSBezierPath(roundedRect: NSRect(x: x, y: Self.barY, width: segment * share, height: Self.barHeight),
                             xRadius: 1, yRadius: 1).fill()
            }
        }
        if let text = content.bubble {
            // Over the end of the lit part: the right edge of the last lit segment.
            let end = Self.barX + lit * (segment + Self.gap) - (lit > 0 ? Self.gap : 0)
            drawBubble(text, pointingAt: max(Self.barX, end))
        }
    }

    private func drawSymbol() {
        let config = NSImage.SymbolConfiguration(pointSize: Self.symbolSize, weight: .regular)
            .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
        guard let image = NSImage(systemSymbolName: content.symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(config) else {
            return
        }
        let size = image.size
        image.draw(in: NSRect(x: Self.symbolCenterX - size.width / 2, y: bounds.midY - size.height / 2,
                              width: size.width, height: size.height))
    }

    private func drawBubble(_ text: String, pointingAt x: Double) {
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5, weight: .semibold),
            .foregroundColor: NSColor(white: 0.25, alpha: 1),
        ]
        let string = NSAttributedString(string: text, attributes: attributes)
        let textSize = string.size()
        let width = ceil(textSize.width) + 14
        let bottom = Self.barY + Self.barHeight + 5 + Self.pointer
        let left = max(Self.barX - 8, min(bounds.width - width - 10, x - width / 2))  // never over the symbol
        let box = NSRect(x: left, y: bottom, width: width, height: Self.bubbleHeight)
        let shape = NSBezierPath(roundedRect: box, xRadius: Self.bubbleHeight / 2, yRadius: Self.bubbleHeight / 2)
        let tip = max(box.minX + 8, min(box.maxX - 8, x))
        shape.move(to: NSPoint(x: tip - Self.pointer, y: bottom + 0.5))
        shape.line(to: NSPoint(x: tip, y: bottom - Self.pointer))
        shape.line(to: NSPoint(x: tip + Self.pointer, y: bottom + 0.5))
        shape.close()
        NSColor(white: 0.82, alpha: 1).setFill()
        shape.fill()
        string.draw(at: NSPoint(x: box.minX + (width - textSize.width) / 2, y: box.minY + (Self.bubbleHeight - textSize.height) / 2))
    }
}
