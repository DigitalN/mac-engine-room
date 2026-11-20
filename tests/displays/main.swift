//  Native/Displays' pure parts, compiled into one program with it: the brightness scale, the nits, the
//  boost's gamma factor, the keys' steps, the HUD's segments and bubble, DDC packets, matching DDC
//  channels to displays, reading a key event, which display the sound goes to, and what a
//  screen-parameters notification means. Nothing is shown,
//  no tap is made and no display is touched. Run by tests/test_displays_native.py; prints one line per
//  test and exits 1 on a failure.

import CoreGraphics
import Foundation

var failed = false

func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print(ok ? "ok \(name)" : "FAIL \(name) \(detail())")
    if !ok { failed = true }
}

func near(_ a: Double, _ b: Double, _ within: Double = 0.001) -> Bool { abs(a - b) <= within }

// ── nits ──

check("130% is BetterDisplay's 960 nits", near(Scale.nits(percent: 130, linear: 1, headroom: 3.2), 963, 1))
check("160% is the panel's 1,600-nit peak", near(Scale.nits(percent: 160, linear: 1, headroom: 3.2), 1600, 0.5))
check("100% is 500 nits", near(Scale.nits(percent: 100, linear: 1, headroom: 1), 500))
check("below 100% nits follow macOS's linear brightness",
      near(Scale.nits(percent: 80, linear: 0.5325, headroom: 1), 266.25))
check("with less headroom than asked, the nits say what's reached",
      near(Scale.nits(percent: 160, linear: 1, headroom: 2.4), 1200))
check("no headroom at all reads as 100%", near(Scale.nits(percent: 150, linear: 1, headroom: 0.5), 500))

// ── the gamma factor ──

check("100% leaves the table alone", Scale.gammaFactor(percent: 100, headroom: 3.2) == 1)
check("nothing goes up before the screen is in EDR", Scale.gammaFactor(percent: 160, headroom: 1.02) == 1)
check("160% with 3.2× headroom is BetterDisplay's ×1.658",
      near(Scale.gammaFactor(percent: 160, headroom: 3.2), 1.658, 0.002))
check("130% is about ×1.33", near(Scale.gammaFactor(percent: 130, headroom: 3.2), pow(pow(1.3, 2.5), 1 / 2.3)))
check("the factor never asks for more than the headroom",
      near(Scale.gammaFactor(percent: 160, headroom: 1.5), pow(1.5, 1 / 2.3)))
check("the factor rises with the percent",
      Scale.gammaFactor(percent: 110, headroom: 3.2) < Scale.gammaFactor(percent: 120, headroom: 3.2))

// ── the keys' steps ──

check("a press is 10%", Scale.stepped(50, up: true, fine: false, top: 160) == 60)
check("down too", Scale.stepped(50, up: false, fine: false, top: 160) == 40)
check("an odd value goes to the next multiple", Scale.stepped(47, up: true, fine: false, top: 100) == 50
      && Scale.stepped(47, up: false, fine: false, top: 100) == 40)
check("⌥⇧ is a quarter step", Scale.stepped(50, up: true, fine: true, top: 160) == 52.5
      && Scale.stepped(47, up: false, fine: true, top: 100) == 45)
check("the built-in display goes past 100%", Scale.stepped(100, up: true, fine: false, top: 160) == 110)
check("and stops at 160%", Scale.stepped(160, up: true, fine: false, top: 160) == 160
      && Scale.stepped(155, up: true, fine: false, top: 160) == 160)
check("a TV stops at 100%", Scale.stepped(100, up: true, fine: false, top: 100) == 100)
check("nothing goes below 0", Scale.stepped(0, up: false, fine: false, top: 100) == 0
      && Scale.stepped(5, up: false, fine: false, top: 100) == 0)
check("a value a hair off a step counts as that step", Scale.stepped(49.9999999, up: true, fine: false, top: 100) == 60
      && Scale.stepped(50.0000001, up: false, fine: false, top: 100) == 40)
check("volume has macOS's 16 steps", Scale.volumeStepped(50, up: true, fine: false) == 56.25
      && Scale.volumeStepped(0, up: true, fine: false) == 6.25)
check("⌥⇧ volume is a quarter of one", Scale.volumeStepped(50, up: true, fine: true) == 51.5625)
check("volume stops at 100", Scale.volumeStepped(100, up: true, fine: false) == 100)

// ── the HUD ──

check("the built-in display's bar has 16 segments", Scale.segments(top: 160) == 16)
check("a TV's has 10", Scale.segments(top: 100) == 10)
check("100% lights the 10th of 16", Scale.lit(100, top: 160, segments: 16) == 10)
check("130% lights 13", Scale.lit(130, top: 160, segments: 16) == 13)
check("45% on a TV lights four and a half", Scale.lit(45, top: 100, segments: 10) == 4.5)
check("the bar never overflows", Scale.lit(200, top: 160, segments: 16) == 16 && Scale.lit(-5, top: 100, segments: 10) == 0)
check("the bubble reads like BetterDisplay's", Scale.bubble(percent: 130, nits: 963) == "130% · 960 nits",
      Scale.bubble(percent: 130, nits: 963))
check("thousands get a comma", Scale.bubble(percent: 160, nits: 1600) == "160% · 1,600 nits")
check("low nits aren't rounded to tens", Scale.bubble(percent: 10, nits: 7.3) == "10% · 7 nits")
check("a quarter step keeps its half", Scale.bubble(percent: 42.5, nits: nil) == "42.5%")
check("a TV's has no nits", Scale.bubble(percent: 40, nits: nil) == "40%")

// ── DDC ──

check("a DDC reading becomes a percent", Scale.percent(current: 50, maximum: 100) == 50
      && Scale.percent(current: 30, maximum: 60) == 50 && Scale.percent(current: 5, maximum: 0) == 0)
check("and back", Scale.ddcValue(percent: 50, maximum: 60) == 30 && Scale.ddcValue(percent: 120, maximum: 100) == 100)
check("Set VCP is m1ddc's packet", DDCPacket.set(0x10, 70) == [0x84, 0x03, 0x10, 0x00, 70, 0x6E ^ 0x51 ^ 0x84 ^ 0x03 ^ 0x10 ^ 0x00 ^ 70])
check("Get VCP too", DDCPacket.get(0x62) == [0x82, 0x01, 0x62, 0x6E ^ 0x51 ^ 0x82 ^ 0x01 ^ 0x62])
var reply: [UInt8] = [0x6E, 0x88, 0x02, 0x00, 0x10, 0x00, 0x00, 0x64, 0x00, 0x2D]
reply.append(reply.reduce(0x50, ^))
reply.append(0)  // the 12th byte read, unused
check("a reply gives (current, maximum)", DDCPacket.parseReply(reply, vcp: 0x10).map { $0.current == 45 && $0.maximum == 100 } ?? false)
check("a reply for another feature is ignored", DDCPacket.parseReply(reply, vcp: 0x62) == nil)
var corrupt = reply
corrupt[9] = 0x2E
check("a bad checksum is ignored", DDCPacket.parseReply(corrupt, vcp: 0x10) == nil)
var unsupported = reply
unsupported[3] = 0x01
unsupported[10] = Array(unsupported[0..<10]).reduce(0x50, ^)
check("an unsupported feature is ignored", DDCPacket.parseReply(unsupported, vcp: 0x10) == nil)
check("all zeros (no display) is ignored", DDCPacket.parseReply([UInt8](repeating: 0, count: 12), vcp: 0x10) == nil)

// ── matching DDC channels to displays ──

let lg: (id: CGDirectDisplayID, vendor: UInt32, product: UInt32) = (5, 0x1E6D, 0xC0DE)
let dell: (id: CGDirectDisplayID, vendor: UInt32, product: UInt32) = (6, 0x10AC, 0x4321)
check("one display, one channel", DDCDiscovery.match(displays: [lg], channels: [(0, 0)]) == [5: 0])
check("the same manufacturer and product win", DDCDiscovery.match(displays: [lg, dell],
      channels: [(0x10AC, 0x4321), (0x1E6D, 0xC0DE)]) == [5: 1, 6: 0])
check("then the manufacturer", DDCDiscovery.match(displays: [lg, dell],
      channels: [(0x10AC, 1), (0x1E6D, 2)]) == [5: 1, 6: 0])
check("then the order", DDCDiscovery.match(displays: [lg, dell], channels: [(0, 0), (0, 0)]) == [5: 0, 6: 1])
check("a display with no channel gets none", DDCDiscovery.match(displays: [lg, dell], channels: [(0x10AC, 0x4321)]) == [6: 0])
check("no channels, no pairs", DDCDiscovery.match(displays: [lg], channels: []).isEmpty)

// ── key events ──

func data1(_ key: Int, _ state: Int) -> Int { key << 16 | state << 8 }
check("brightness up pressed", systemKey(subtype: 8, data1: data1(2, 0xA), flags: []).map { $0.key == .brightnessUp && $0.down && !$0.fine } ?? false)
check("brightness down released", systemKey(subtype: 8, data1: data1(3, 0xB), flags: []).map { $0.key == .brightnessDown && !$0.down } ?? false)
check("⌥⇧ is fine", systemKey(subtype: 8, data1: data1(0, 0xA), flags: [.maskAlternate, .maskShift])?.fine ?? false)
check("⌥ alone isn't", !(systemKey(subtype: 8, data1: data1(0, 0xA), flags: [.maskAlternate])?.fine ?? true))
check("mute is 7", systemKey(subtype: 8, data1: data1(7, 0xA), flags: [])?.key == .mute)
check("play/pause isn't ours", systemKey(subtype: 8, data1: data1(16, 0xA), flags: []) == nil)
check("another subtype isn't a key", systemKey(subtype: 7, data1: data1(2, 0xA), flags: []) == nil)

// ── the sound output ──

let tv: (id: CGDirectDisplayID, name: String) = (5, "LG TV SSCR2")
let monitor: (id: CGDirectDisplayID, name: String) = (6, "DELL U2720Q")
check("the display the output is named after", soundDisplay(output: ("LG TV SSCR2", true), candidates: [monitor, tv]) == 5)
check("names match whatever the case", soundDisplay(output: ("lg tv sscr2", true), candidates: [monitor, tv]) == 5)
check("the only display it could be", soundDisplay(output: ("HDMI", true), candidates: [tv]) == 5)
check("two it could be and no name: neither", soundDisplay(output: ("HDMI", true), candidates: [monitor, tv]) == nil)
check("the Mac's own speakers aren't a display", soundDisplay(output: ("MacBook Pro Speakers", false), candidates: [tv]) == nil)
check("no output, no display", soundDisplay(output: nil, candidates: [tv]) == nil)

// ── which Macs boost ──

check("the 16\" M1 Pro's XDR boosts", BoostSupport.supported(builtin: true, potentialHeadroom: 16, chipIgnoresGamma: false))
check("an M5 Pro doesn't", !BoostSupport.supported(builtin: true, potentialHeadroom: 16, chipIgnoresGamma: true))
check("a screen without XDR doesn't", !BoostSupport.supported(builtin: true, potentialHeadroom: 1, chipIgnoresGamma: false))
check("an external display doesn't", !BoostSupport.supported(builtin: false, potentialHeadroom: 16, chipIgnoresGamma: false))

// ── easing the keys' steps ──

check("a fade starts where it is", near(Scale.eased(from: 50, to: 60, progress: 0), 50))
check("and ends at the target", near(Scale.eased(from: 50, to: 60, progress: 1), 60))
check("past the end it stays there", near(Scale.eased(from: 50, to: 60, progress: 1.4), 60))
check("it eases out: more than half way at half the time", Scale.eased(from: 0, to: 10, progress: 0.5) > 5)
check("downward too", near(Scale.eased(from: 110, to: 100, progress: 0.5), 102.5))
let fadeSteps = stride(from: 0.0, through: 1.0, by: 0.1).map { Scale.eased(from: 95, to: 105, progress: $0) }
check("it never turns back", zip(fadeSteps, fadeSteps.dropFirst()).allSatisfy { $0 <= $1 })
check("macOS's own fade time", near(Scale.fadeFor, 0.25))

// ── what the keys take, for app.log ──

check("brightness only, with the sound on the Mac's speakers",
      keysLine(brightness: true, volumeTo: nil, soundToDisplay: false)
        == "Display keys on: brightness (volume and mute stay with macOS: the sound isn't going to a display)")
check("everything, with the sound on a TV that answers",
      keysLine(brightness: true, volumeTo: "LG TV SSCR2", soundToDisplay: true) == "Display keys on: brightness, volume and mute (LG TV SSCR2)")
check("the sound on a display that doesn't answer DDC for volume",
      keysLine(brightness: true, volumeTo: nil, soundToDisplay: true)
        == "Display keys on: brightness (volume and mute stay with macOS: the display the sound goes to doesn't answer DDC for volume)")
check("nothing to take", keysLine(brightness: false, volumeTo: nil, soundToDisplay: false).hasPrefix("Display keys on, taking nothing yet"))

// ── screen-parameters notifications ──
// macOS posts one every time a screen's EDR headroom moves, dozens a second while the boost's trigger
// ramps it up. 1.5.0's test copy made the trigger again on every one, restoring the table each time,
// and the screen flickered.

let builtinShape = ScreenShape(id: 1, frame: CGRect(x: 0, y: 0, width: 1728, height: 1117), name: "Built-in Retina Display")
let tvShape = ScreenShape(id: 4, frame: CGRect(x: 1728, y: 0, width: 1920, height: 1080), name: "LG TV SSCR2")
check("the first look is a change", ScreenChange.changed(nil, [builtinShape]))
check("the same screens again (a headroom moved) aren't", !ScreenChange.changed([builtinShape, tvShape], [builtinShape, tvShape]))
check("a display plugged in is", ScreenChange.changed([builtinShape], [builtinShape, tvShape]))
check("one unplugged is", ScreenChange.changed([builtinShape, tvShape], [builtinShape]))
check("one moved is", ScreenChange.changed([builtinShape, tvShape],
                                           [builtinShape, ScreenShape(id: 4, frame: CGRect(x: -1920, y: 0, width: 1920, height: 1080), name: "LG TV SSCR2")]))
check("a new resolution is", ScreenChange.changed([builtinShape],
                                                   [ScreenShape(id: 1, frame: CGRect(x: 0, y: 0, width: 1512, height: 982), name: "Built-in Retina Display")]))
check("another main screen is", ScreenChange.changed([builtinShape, tvShape], [tvShape, builtinShape]))

let placed = ScreenPlace(id: 1, frame: builtinShape.frame)
check("a headroom change keeps the trigger", ScreenChange.trigger(placed: placed, now: placed, wasGone: false) == .keep)
check("a new resolution moves it", ScreenChange.trigger(placed: placed, now: ScreenPlace(id: 1, frame: CGRect(x: 0, y: 0, width: 1512, height: 982)),
                                                          wasGone: false) == .move)
check("the built-in display moved in the arrangement moves it",
      ScreenChange.trigger(placed: placed, now: ScreenPlace(id: 1, frame: CGRect(x: -1728, y: 0, width: 1728, height: 1117)), wasGone: false) == .move)
check("a new ID for it moves it", ScreenChange.trigger(placed: placed, now: ScreenPlace(id: 2, frame: placed.frame), wasGone: false) == .move)
check("no trigger (paused, or none made): nothing to move", ScreenChange.trigger(placed: nil, now: placed, wasGone: false) == .keep)
check("the lid closed with an external: gone", ScreenChange.trigger(placed: placed, now: nil, wasGone: false) == .gone)
check("still closed: nothing more", ScreenChange.trigger(placed: nil, now: nil, wasGone: true) == .keep)
check("opened again: back", ScreenChange.trigger(placed: nil, now: placed, wasGone: true) == .back)

exit(failed ? 1 : 0)
