//  Fans.swift against FakeSMC.swift: the unlock and hand-back on an M4 (Ftst) and an M1 Pro
//  (no Ftst). Run by tests/test_fan_helper.py; prints one line per test and exits 1 on a failure.

import Foundation
import os

let log = Logger(subsystem: "com.pythonflow.monitor.tests", category: "fans")
let marker = NSTemporaryDirectory() + "fan-helper-test.holding"
var failed = false

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

exit(failed ? 1 : 0)
