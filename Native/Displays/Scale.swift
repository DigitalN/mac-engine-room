// The pure parts of Displays: the brightness scale, the nits the HUD shows, the boost's gamma
// factor, the keys' steps and the HUD's segments. Tested by tests/displays/main.swift.

import Foundation

enum Scale {
    /// The built-in XDR display's scale runs to 160%: 100% is macOS's own maximum (500 nits of SDR
    /// white), 160% the panel's 1,600-nit HDR peak.
    static let boostTop = 160.0
    static let plainTop = 100.0
    static let sdrNits = 500.0
    /// Nits at 100% and above are 500 × (percent / 100)^2.5: BetterDisplay's 960 at 130% and 1,600
    /// at 160%.
    static let nitsExponent = 2.5
    /// The gamma table works on encoded values, so lifting the light by a ratio multiplies the table by
    /// ratio^(1/2.3). Measured: BetterDisplay writes ×1.658 for the full 3.2× headroom at 160%, and
    /// 3.2^(1/2.3) is 1.658.
    static let tableExponent = 2.3
    /// macOS's own headroom has to be at least this before the table goes up (BrightIntosh's 1.05):
    /// below it the screen isn't in EDR yet, and values above 1 would only wash the picture out.
    static let edrReady = 1.05

    static let step = 10.0  // a brightness key press
    static let fineStep = 2.5  // with ⌥⇧, as macOS does
    static let volumeSteps = 16.0  // macOS's volume steps; ⌥⇧ gives quarters

    /// How many times brighter than 100% `percent` asks for (1 at or below 100%).
    static func ratio(_ percent: Double) -> Double {
        return percent <= plainTop ? 1 : pow(percent / plainTop, nitsExponent)
    }

    /// What the HUD's bubble says the built-in display gives. Below 100%: 500 × macOS's linear
    /// brightness. Above: 500 × the ratio asked for, but no more than macOS's headroom grants
    /// (`headroom`: NSScreen's maximumExtendedDynamicRangeColorComponentValue while boosted).
    static func nits(percent: Double, linear: Double, headroom: Double) -> Double {
        if percent <= plainTop {
            return sdrNits * max(0, min(1, linear))
        }
        return sdrNits * min(ratio(percent), max(1, headroom))
    }

    /// The gamma table's multiplier for `percent`: 1 at or below 100%, or while the screen isn't in
    /// EDR; otherwise the ratio asked for, capped by the headroom, as a factor on encoded values.
    static func gammaFactor(percent: Double, headroom: Double) -> Double {
        guard percent > plainTop, headroom >= edrReady else {
            return 1
        }
        return pow(min(ratio(percent), headroom), 1 / tableExponent)
    }

    /// One press of a brightness key from `percent`: to the next multiple of the step in that
    /// direction (so 47% goes to 50% or 40%), within 0 and `top`.
    static func stepped(_ percent: Double, up: Bool, fine: Bool, top: Double) -> Double {
        return snap(percent, up: up, size: fine ? fineStep : step, top: top)
    }

    /// One press of a volume key: macOS's 16 steps, or quarters of one with ⌥⇧.
    static func volumeStepped(_ percent: Double, up: Bool, fine: Bool) -> Double {
        return snap(percent, up: up, size: 100 / volumeSteps / (fine ? 4 : 1), top: 100)
    }

    private static func snap(_ value: Double, up: Bool, size: Double, top: Double) -> Double {
        let units = value / size
        let next = up ? (units + 1e-6).rounded(.down) + 1 : (units - 1e-6).rounded(.up) - 1
        return max(0, min(top, next * size))
    }

    /// The HUD's bar: `segments` segments from 0 to `top`, and how much of it is lit (0…segments,
    /// fractional for a part-lit segment). The built-in XDR display has 16 from 0 to 160% (100% at
    /// the 10th); a display that stops at 100% has 10; volume has 16.
    static func lit(_ value: Double, top: Double, segments: Int) -> Double {
        guard top > 0 else {
            return 0
        }
        return max(0, min(Double(segments), value / top * Double(segments)))
    }

    static func segments(top: Double) -> Int {
        return top > plainTop ? 16 : 10
    }

    /// The bubble's text: "130% · 960 nits", or "45%" where there are no nits. Percents are whole
    /// unless a quarter step left a fraction ("42.5%"); nits round to 10 above 100 ("1,600").
    static func bubble(percent: Double, nits: Double?) -> String {
        let p = percent.rounded() == percent ? String(Int(percent)) : String(format: "%.1f", percent)
        guard let nits else {
            return "\(p)%"
        }
        let n = Int(shown(nits))
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.locale = Locale(identifier: "en_US")
        return "\(p)% · \(formatter.string(from: NSNumber(value: n)) ?? String(n)) nits"
    }

    /// Nits as they're shown: to the nearest 10 from 100 up (BetterDisplay's "960"), whole below.
    static func shown(_ nits: Double) -> Double {
        return nits >= 100 ? (nits / 10).rounded() * 10 : nits.rounded()
    }

    /// A DDC reading (current, maximum) as a percent, and back.
    static func percent(current: Int, maximum: Int) -> Double {
        guard maximum > 0 else {
            return 0
        }
        return max(0, min(100, Double(current) / Double(maximum) * 100))
    }

    static func ddcValue(percent: Double, maximum: Int) -> Int {
        return Int((max(0, min(100, percent)) / 100 * Double(maximum)).rounded())
    }
}

/// Which built-in displays can boost: an XDR panel (its potential headroom is far above 1; the
/// Liquid Retina XDR reports 16), and not on a chip that ignores gamma tables. M5 Pro, M5 Max and
/// the MacBook Neo's A18 Pro have ignored them since macOS 26.3 (Apple forum thread 819331,
/// FB22273730); Python passes that list.
enum BoostSupport {
    static let minimumPotentialHeadroom = 3.0

    static func supported(builtin: Bool, potentialHeadroom: Double, chipIgnoresGamma: Bool) -> Bool {
        return builtin && potentialHeadroom >= minimumPotentialHeadroom && !chipIgnoresGamma
    }
}
