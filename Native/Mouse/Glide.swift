// Smooth scrolling: each notch of a wheel becomes a short glide of pixel scrolling. One fixed feel,
// no tuning (decided). A notch adds its distance to what's left to scroll, and each frame scrolls
// a fixed share of what's left, so the glide eases out and quick notches add up. A notch the other
// way drops what was left, so turning back is immediate.
//
// A notch's distance is its line delta, which already carries macOS's scroll acceleration and
// speed setting, times 36 points (LinearMouse's line step; see THIRD-PARTY-NOTICES). Pure: the tap
// thread runs it from a timer only while something is left to scroll.

import Foundation

struct Glide {
    static let pointsPerLine = 36.0
    /// Seconds for what's left to fall to 37%: a notch is 95% done in about 0.2 s.
    static let timeConstant = 0.07
    static let frameInterval = 1.0 / 120

    private(set) var left = (y: 0.0, x: 0.0)  // points still to scroll
    private var carry = (y: 0.0, x: 0.0)  // the fraction of a point not yet posted

    var done: Bool {
        return left.y == 0 && left.x == 0
    }

    /// A notch of `lines` (vertical, horizontal), signed as the event's DeltaAxis1 and DeltaAxis2.
    mutating func add(linesY: Int64, linesX: Int64) {
        left.y = Glide.added(Double(linesY) * Glide.pointsPerLine, to: left.y)
        left.x = Glide.added(Double(linesX) * Glide.pointsPerLine, to: left.x)
        if linesY != 0 && left.y.sign != carry.y.sign { carry.y = 0 }
        if linesX != 0 && left.x.sign != carry.x.sign { carry.x = 0 }
    }

    private static func added(_ amount: Double, to left: Double) -> Double {
        if amount == 0 {
            return left
        }
        return left != 0 && (amount < 0) != (left < 0) ? amount : left + amount
    }

    /// The whole points to post for a frame `dt` seconds after the last one. The last frame of a
    /// glide posts whatever is left, fraction included.
    mutating func step(dt: Double) -> (y: Int32, x: Int32) {
        let share = 1 - exp(-max(0, min(dt, 0.05)) / Glide.timeConstant)
        let y = Glide.advance(&left.y, &carry.y, share)
        let x = Glide.advance(&left.x, &carry.x, share)
        return (y, x)
    }

    private static func advance(_ left: inout Double, _ carry: inout Double, _ share: Double) -> Int32 {
        guard left != 0 else {
            return 0
        }
        var move = left * share
        if abs(left - move) < 0.5 {
            move = left  // close enough: finish
        }
        left -= move
        carry += move
        let whole = left == 0 ? carry.rounded() : carry.rounded(.towardZero)
        carry = left == 0 ? 0 : carry - whole
        return Int32(whole)
    }
}
