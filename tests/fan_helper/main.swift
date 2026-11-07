//  Fans.swift against FakeSMC.swift: the unlock and hand-back on an M4 (Ftst) and an M1 Pro
//  (no Ftst), and telling the helper's own fan speeds from another app's, with targets that read
//  back late. Run by tests/test_fan_helper.py; prints one line per test and exits 1 on a failure.

import Foundation
import os

let log = Logger(subsystem: "com.pythonflow.monitor.tests", category: "fans")
let marker = NSTemporaryDirectory() + "fan-helper-test-\(getpid()).holding"  // one per run: runs can overlap
var failed = false
try? FileManager.default.removeItem(atPath: marker)  // a leftover from an earlier run with this pid

func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print(ok ? "ok \(name)" : "FAIL \(name) \(detail())")
    if !ok { failed = true }
}

func fans(_ smc: SMCConnection) -> Fans { Fans(smc: smc, marker: marker, unlockPatience: 0.3)! }
func modes(_ smc: SMCConnection) -> [UInt8] { [smc.keys["F0Md"]![0], smc.keys["F1Md"]![0]] }
func index(_ smc: SMCConnection, _ key: String, _ value: UInt8) -> Int? {
    smc.writes.firstIndex { $0.key == key && $0.bytes.first == value }
}
func thrown(_ body: () throws -> Void) -> Failure? {
    do { try body(); return nil } catch { return error as? Failure }
}
/// What a fan's target really is, which a read may not show yet.
func target(_ smc: SMCConnection, _ fan: Int) -> Float {
    smc.keys["F\(fan)Tg"]!.withUnsafeBytes { $0.loadUnaligned(as: Float.self) }
}
func marked() -> Bool { FileManager.default.fileExists(atPath: marker) }

// An M4 takes the fans once Ftst is 1, and gives them back with Ftst 0.
do {
    let smc = SMCConnection(model: "m4"), f = fans(smc)
    let set = try? f.hold([1499, 1499])
    check("m4 holds the fans", set == [1499, 1499] && f.holding && modes(smc) == [1, 1], "\(String(describing: set)) \(modes(smc))")
    check("m4 sets Ftst before any mode", (index(smc, "Ftst", 1) ?? .max) < (index(smc, "F0Md", 1) ?? -1))
    let firstTarget = smc.writes.firstIndex { $0.key == "F0Tg" } ?? .max
    check("m4 sets the speed before the mode", firstTarget < (index(smc, "F0Md", 1) ?? -1))
    check("m4 marker while holding", FileManager.default.fileExists(atPath: marker))
    check("m4 hands back", f.release("test") && modes(smc) == [0, 0] && smc.keys["Ftst"] == [0], "\(modes(smc)) \(smc.keys["Ftst"]!)")
    check("m4 clears Ftst after the modes", (index(smc, "F1Md", 0) ?? .max) < (index(smc, "Ftst", 0) ?? -1))
    check("m4 marker gone", !FileManager.default.fileExists(atPath: marker))
    check("never a target of 0", !smc.writes.contains { $0.key.hasSuffix("Tg") && $0.bytes == SMCConnection.bytes(0) })
}

// If Ftst can't be set, nothing is left half done.
do {
    let smc = SMCConnection(model: "m4"), f = fans(smc)
    smc.refuse = ["Ftst"]
    check("m4 Ftst refused fails as unlock", thrown { _ = try f.hold([1499]) } == .unlock)
    check("m4 Ftst refused leaves the fans with macOS", !f.holding && !modes(smc).contains(1) && smc.keys["Ftst"] == [0])
    check("m4 Ftst refused leaves no marker", !FileManager.default.fileExists(atPath: marker))
}

// If thermalmonitord never lets go, Ftst goes back to 0 rather than leaving the fans unrun.
do {
    let smc = SMCConnection(model: "m4"), f = fans(smc)
    smc.thermalmonitordYields = false
    check("m4 mode refused fails as unlock", thrown { _ = try f.hold([1499]) } == .unlock)
    check("m4 mode refused clears Ftst", !f.holding && smc.keys["Ftst"] == [0], "\(smc.keys["Ftst"]!)")
    check("m4 mode refused leaves no marker", !FileManager.default.fileExists(atPath: marker))
}

// Another app with the fans in manual is left alone; Ftst left set with every fan on auto
// (an app that crashed) isn't another app holding them, and is cleared on hand-back.
do {
    let smc = SMCConnection(model: "m4"), f = fans(smc)
    smc.keys["Ftst"] = [1]; smc.keys["F0Md"] = [1]
    check("m4 another app is left alone", thrown { _ = try f.hold([1499]) } == .otherApp && smc.writes.isEmpty)
    smc.keys["F0Md"] = [3]
    check("m4 leftover Ftst is taken over", (try? f.hold([1499])) != nil && modes(smc) == [1, 1])
    check("m4 leftover Ftst is cleared on hand-back", f.release("test") && smc.keys["Ftst"] == [0])
}

// Before sleep the fans go back, Ftst too.
do {
    let smc = SMCConnection(model: "m4"), f = fans(smc)
    _ = try? f.hold([2000])
    f.willSleep()
    check("m4 sleep hands back", !f.holding && modes(smc) == [0, 0] && smc.keys["Ftst"] == [0])
    check("m4 asleep refuses", thrown { _ = try f.hold([2000]) } == .asleep)
}

// The M1 Pro has no Ftst: the mode alone, as before.
do {
    let smc = SMCConnection(model: "m1pro"), f = fans(smc)
    check("m1pro holds the fans", (try? f.hold([1499, 1499])) == [1499, 1499] && modes(smc) == [1, 1])
    check("m1pro hands back", f.release("test") && modes(smc) == [0, 0])
    check("m1pro never writes Ftst", !smc.writes.contains { $0.key == "Ftst" })
}

// A Mac that doesn't report FNum: its fans are the ones with a speed key. A fanless Mac has none.
do {
    let smc = SMCConnection(model: "m1pro")
    smc.keys["FNum"] = nil
    let f = Fans(smc: smc, marker: marker, unlockPatience: 0.3)
    check("no FNum: the fans are counted", (try? f?.hold([1499, 1499])) == [1499, 1499] && modes(smc) == [1, 1], "\(modes(smc))")
    _ = f?.release("test")
    check("fanless: no fans", Fans(smc: SMCConnection(model: "m1pro", fans: 0), marker: marker) == nil)
}

// A written target can read back late, as the old value for a moment, as the modes do. A ramp sets
// a new speed every second, so a late read of the helper's own earlier speed must not pass for
// another app's (most likely how Balanced gave up the fans in #48). The first few sets come back
// to back, so their reads are all from before the first write; the rest are paced, so the reads
// show speeds from a few sets back.
do {
    let smc = SMCConnection(model: "m1pro"), f = fans(smc)
    smc.targetLag = 0.3
    var refused: Failure?
    for step in 0..<12 {
        let rpm = Float(1500 + 200 * step)
        do { _ = try f.hold([rpm, rpm]) } catch { refused = refused ?? (error as? Failure) }
        if step >= 3 { Thread.sleep(forTimeInterval: 0.1) }
    }
    check("a ramp with late target read-backs keeps the fans held",
          refused == nil && f.holding && modes(smc) == [1, 1] && target(smc, 0) == 3700 && target(smc, 1) == 3700,
          "\(String(describing: refused)) holding \(f.holding) \(modes(smc)) \(target(smc, 0))")
    check("the ramp's reads were late", smc.lateReads > 0)
    check("a ramp that held the fans never wrote a target of 0", !smc.writes.contains { $0.key.hasSuffix("Tg") && $0.bytes == SMCConnection.bytes(0) })
    check("the fans are still handed back after the ramp", f.release("test") && modes(smc) == [0, 0] && !marked())
}

// Another app's speed is not one of the helper's: it's seen at the next set, even while targets
// read back late, and nothing is written over it.
do {
    let smc = SMCConnection(model: "m1pro"), f = fans(smc)
    smc.targetLag = 0.3
    for rpm: Float in [1500, 1700, 1900] { _ = try? f.hold([rpm, rpm]) }
    smc.appSetsTarget(fan: 1, 3000)
    let before = smc.writes.count
    check("another app's speed is seen at once", thrown { _ = try f.hold([2100, 2100]) } == .otherApp && !f.holding)
    check("another app's speed isn't written over", smc.writes.count == before && target(smc, 1) == 3000 && modes(smc) == [1, 1],
          "\(smc.writes.count - before) writes, \(target(smc, 1))")
    check("another app's speed isn't written over by a later set", thrown { _ = try f.hold([2300, 2300]) } == .otherApp && smc.writes.count == before)
    check("another app's speed isn't taken back by a hand-back", f.release("test") && smc.writes.count == before && modes(smc) == [1, 1])
    check("giving the fans up removed the marker", !marked())
}

// An earlier speed of the helper's reads as its own only for two seconds after it was set; the last one
// for good. Otherwise a speed set minutes ago would hide another app that happened to pick it.
do {
    let smc = SMCConnection(model: "m1pro"), f = fans(smc)
    _ = try? f.hold([1500, 1500]); _ = try? f.hold([2500, 2500])
    smc.appSetsTarget(fan: 0, 1500)  // reads as the first speed: could be a late read of it
    check("an earlier speed, just set, reads as the helper's own", (try? f.hold([3000, 3000])) == [3000, 3000] && f.holding)
    Thread.sleep(forTimeInterval: modeSettle + 0.1)
    smc.appSetsTarget(fan: 0, 1500)
    let before = smc.writes.count
    check("an earlier speed, set long ago, is another app's", thrown { _ = try f.hold([3200, 3200]) } == .otherApp && smc.writes.count == before)
    _ = f.release("test")
}

// The helper gave the fans up for a speed it didn't set. If they read as its own speeds again (the
// read was late, or its hand-back failed), they're still its: set takes them back, and a hand-back,
// disconnect or sleep gives them to macOS. At any other speed they stay another app's.
func gaveUp(_ model: String = "m1pro") -> (SMCConnection, Fans) {
    let smc = SMCConnection(model: model), f = fans(smc)
    _ = try? f.hold([2000, 2200])
    smc.appSetsTarget(fan: 0, 3000)
    check("\(model): another app's speed makes the helper give the fans up", thrown { _ = try f.hold([2000, 2200]) } == .otherApp && !f.holding && modes(smc) == [1, 1])
    return (smc, f)
}

do {
    let (smc, f) = gaveUp()
    smc.appSetsTarget(fan: 0, 2000)  // back at the speed the helper set
    let set = try? f.hold([2100, 2300])
    check("fans left at the helper's own speeds are taken back by set",
          set == [2100, 2300] && f.holding && modes(smc) == [1, 1] && target(smc, 0) == 2100 && target(smc, 1) == 2300 && marked(),
          "\(String(describing: set)) \(f.holding) \(modes(smc))")
    check("fans taken back are handed back", f.release("test") && modes(smc) == [0, 0] && !marked())
}

do {
    let (smc, f) = gaveUp()
    smc.appSetsTarget(fan: 0, 2001)  // within 1 rpm of what the helper set
    check("fans left at the helper's own speeds are handed back by auto", f.release("test") && modes(smc) == [0, 0] && !f.holding)
    check("after that they're with macOS for set", (try? f.hold([2100, 2300])) == [2100, 2300] && modes(smc) == [1, 1])
    _ = f.release("test")
}

do {
    let (smc, f) = gaveUp()
    smc.appSetsTarget(fan: 0, 2000)
    f.willSleep()
    check("fans left at the helper's own speeds are handed back before sleep", modes(smc) == [0, 0])
    check("a Mac asleep still refuses set", thrown { _ = try f.hold([2000, 2200]) } == .asleep)
}

do {
    let (smc, f) = gaveUp("m4")
    smc.appSetsTarget(fan: 0, 2000)
    check("m4: fans left at the helper's own speeds are handed back with Ftst cleared",
          f.release("test") && modes(smc) == [0, 0] && smc.keys["Ftst"] == [0], "\(modes(smc)) \(smc.keys["Ftst"]!)")
}

do {
    let (smc, f) = gaveUp()  // fan 0 is still at 3000
    let before = smc.writes.count
    check("fans at another app's speed are left alone by set", thrown { _ = try f.hold([2000, 2200]) } == .otherApp && smc.writes.count == before)
    check("fans at another app's speed are left alone by auto", f.release("test") && smc.writes.count == before && modes(smc) == [1, 1])
    f.willSleep()
    check("fans at another app's speed are left alone before sleep", smc.writes.count == before && modes(smc) == [1, 1])
}

do {
    let (smc, f) = gaveUp()
    check("status says another app has a fan at another app's speed", f.status().contains("\"other\": true"), f.status())
    smc.appSetsTarget(fan: 0, 2000)
    check("status doesn't blame another app for fans at the helper's own speeds", f.status().contains("\"other\": false"), f.status())
}

do {
    let (smc, f) = gaveUp()
    smc.appSetsTarget(fan: 0, 2000)  // this one is the helper's again, the other isn't
    smc.appSetsTarget(fan: 1, 3500)
    let before = smc.writes.count
    check("one fan at another app's speed keeps the helper off both", thrown { _ = try f.hold([2000, 2200]) } == .otherApp && smc.writes.count == before)
    check("one fan at another app's speed keeps auto off both", f.release("test") && smc.writes.count == before && modes(smc) == [1, 1])
}

// Nothing is the helper's until it has set it: fans found in manual mode at their minimum, which is
// what the helper's own first speed would be, belong to another app. And once the fans are back with
// macOS the helper forgets its speeds: an app that takes a fan and hasn't set its target yet
// shows the old one.
do {
    let smc = SMCConnection(model: "m1pro")
    smc.keys["F0Md"] = [1]; smc.keys["F0Tg"] = SMCConnection.bytes(1499)
    let f = fans(smc)
    check("fans in manual mode at their minimum are another app's before the helper has set anything",
          thrown { _ = try f.hold([1499, 1499]) } == .otherApp && smc.writes.isEmpty)
    check("and auto leaves them alone", f.release("test") && smc.writes.isEmpty)
}

do {
    let smc = SMCConnection(model: "m1pro"), f = fans(smc)
    _ = try? f.hold([2000, 2200])
    check("hands back", f.release("test") && modes(smc) == [0, 0])
    smc.keys["F0Md"] = [1]  // an app takes the fan; the target is still the helper's last
    let before = smc.writes.count
    check("after a hand-back, a fan left at the helper's last speed isn't its own",
          thrown { _ = try f.hold([2000, 2200]) } == .otherApp && smc.writes.count == before)
}

// What the log says when the fans are given up: for each fan what it read and what had been set.
check("the give-up log says what each fan read and what the helper had set",
      Fans.describe(read: [2300, nil], wrote: [[2150, 2177], []]) == "fan 0 read 2300 rpm, had written 2150, 2177 rpm; fan 1 read nothing, had written nothing",
      Fans.describe(read: [2300, nil], wrote: [[2150, 2177], []]))
check("the give-up log copes with a speed that isn't a number", Fans.describe(read: [.nan], wrote: [[2000]]) == "fan 0 read nan rpm, had written 2000 rpm")

try? FileManager.default.removeItem(atPath: marker)
exit(failed ? 1 : 0)
