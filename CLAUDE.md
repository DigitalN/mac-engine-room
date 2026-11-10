# Mac Engine Room

Formerly Pythonflow. Building, the release checklist, how updates work, and testing the
updater are in @docs/DEVELOPMENT.md. The agreed plan for turning it into a power and
thermals app, release by release, is in `docs/PLAN.md`.

## Debugging

Application logs are written to `~/Library/Logs/Mac Engine Room/app.log` (rotating,
3 x 1 MB). Dashboard (JavaScript) errors are forwarded there too. When the user
reports a bug, **read the log first** before asking questions. The updater logs
`Automatic updates off: <reason>` at launch, and `Up to date at`, `Downloading`,
`ready to install`, `Installed …; relaunching` or `Update check failed: …` after a
check. `Installed …; relaunching` and `Quitting` with no `started` after them means the
relaunch didn't start the new copy: `/usr/bin/log show` then has `open` logging `LAUNCH`,
and no new `Mac Engine Room` process (see *How updates work* in DEVELOPMENT.md). Each launch logs `started`, plus `, opened at login` for a
login launch. A Mac missing any SMC key the app reads
logs `SMC keys missing on <model>, macOS <version>: …` once per launch, with the power
keys it does have. Reopening the app at one bar of brightness and again at full shows
which of them is the screen. The first SMC reading also logs where Chip comes from: `Chip power
from PHPC (reads 1.85 W)`, or where PHPC can't give it, `Chip power from IOReport's CLPC energy
counters (PHPC reads 0.01 W)` (`IOReport's Energy Model` before macOS 27), or `IOReport
unavailable: <why> (PHPC reads 0.01 W)`, and Chip stays hidden. That last line comes later too if
IOReport stops counting. `PYTHONFLOW_CHIP_IOREPORT=1` at launch reads Chip from IOReport on a Mac
whose PHPC works, to test that path. The first temperature reading after launch (or after an SMC
reconnect) logs `Temperature sensors on <chip>: cpu N, gpu N, …; N fans`, plus the groups
found by prefix when the chip isn't in the core key table (or its keys don't fit this Mac), and
`no GPU sensor`. For a Mac you can't reach, ask for its sensor report (Settings › About ›
Copy, or the "Wrong or missing reading" issue template): it lists every power, temperature and
fan key with its value, and which keys each reading came from: a screen or chip key that reads
under 0.1 W shows as e.g. `Chip power: none (PHPC reads 0.01 W)`, and Chip from IOReport as
`Chip power: IOReport's CLPC energy counters, CPU + GPU + ANE (PHPC reads 0.01 W)` (or with
`IOReport unavailable: <why>`). The app logs `Copied the sensor report` or `Couldn't make the
sensor report (…)`.

Fan control logs `Fan profile: <name>` when one is chosen (and at launch), `Asked macOS to
allow the fan helper`, `Fan control is waiting for the helper to be allowed in Login Items`,
`Fans held by <profile> at <rpm> rpm (cores N°)`, `Fans back to macOS: <why>`,
`Safety override: fans at full speed (<why>)` and `Safety override over`, `Another app is
controlling the fans; leaving them alone`, and `Fan control isn't available …` or `Fan
control stopped because …`. A quit with the fans held logs `Fans left for the fan helper to hand
back to macOS as Mac Engine Room quits`: it doesn't ask for `auto` and wait for the answer (4.4 s
at a quit on a busy Mac), it closes the connection, and the helper hands them back as it closes,
logging `Fans back with macOS (Mac Engine Room disconnected)`. The helper logs to the unified log,
not app.log:
`/usr/bin/log show --last 1h --predicate 'subsystem == "com.pythonflow.monitor"'` (zsh's own
`log` command shadows it) shows `Holding the fans at [rpm, rpm] rpm` and `Fans back with
macOS (<why>)`, where why is the app asking, it disconnecting, no word for 10 seconds, or
sleep. `/var/run/<bundle id>.fans.holding` exists while the helper holds the fans.

`Another app set a fan speed; letting it have the fans (fan 0 read 2300 rpm, had written 0
until 12.0 s ago, 2150 until 2.6 s ago, 2177 since; fan 1 …)` is the helper giving the fans
up. For each fan it says what the target read and what the helper had written to it, each
speed with how long ago it was replaced (the first can be what macOS left). A read of the
current speed, or of one replaced in the last 2 s, isn't another app's: a target can read back
late. After giving up the helper looks again every second for 10 s, and doesn't quit before:
fans still in manual mode at the speeds it wrote are still its, and it logs `The fans are
still at the speeds the helper set` and `Fans back with macOS (looking again at fans it had
given up)`. The next `set` takes them back instead (`Holding the fans again at … rpm; they
were still at the speeds the helper set`), and a hand-back (the app asking, disconnecting,
sleep) logs the first line too. Fans at any other speed are left alone, and a helper that has
restarted knows no speeds of its own, so it leaves manual fans alone too.

`Menu took 1.8 s to open` is timed from the click to the open, filled menu (only logged above
0.3 s). `Mac busy: running at full priority` and `Mac quiet again` are each switch of the app's
threads between user-interactive and background, with what the app saw in brackets: `(a wait
ended 1.4 s late)` or `(efficiency cores 3% idle)` (a quiet launch logs neither: it just stays at
full priority until the Mac has been quiet for 30 s). A `Mac quiet again` followed within seconds
by a slow menu or by `Mac busy` means the Mac wasn't quiet: the brackets say how idle the
efficiency cores were. The first reading also logs which cores quiet is judged on: `Quiet is
judged on the efficiency cores (cpu 0, 1 of 10)`, or `… on all 10 cores (no efficiency cores
found)`. `First reading 62.4 s after launch (read at 9.6 s)` is timed from the process's start to
the first reading in the menu bar (only logged above 2 s); `read at` is when the sampler had it. A
`read at` well before the first reading means the main thread wasn't running its run loop yet: at
a 1.2.18 login launch it was silent for 10 s just after AppKit's `finishLaunching` began, cause
unknown (#59). When most of the wait was before the app's own code began to run, the bracket says
so too: a copy that had never run sat at exec (`sample` showed it in `_dyld_start`) for 224 s
once, with XprotectService busy, so probably macOS's security scan of a first launch; that wait
isn't the app's. A slow menu or first reading with no `Mac busy` line before it isn't the app's
priority: look at the macOS log for what else was waiting.

`Dashboard took 0.8 s to open the first time` is timed from the click to the page loaded: the
first open of each launch makes the window and starts WebKit (see *Launcher* below). `Dashboard
hadn't loaded after 6.0 s` means the window was closed, or the app quit, before then: that's a
blank dashboard, WebKit still starting. On a busy Mac 1.2.19 left it blank for over 11 s, the main
thread handing WebKit's start off at background QoS (see *Idle work stays on the efficiency
cores*). A window opened again before its page loaded times from the new click. `Couldn't open
the dashboard` with a traceback means pywebview couldn't make the window.

A blank dashboard has been WebKit's graphics helper hanging. At login it got stuck
setting up audio, and macOS doesn't let WebKit kill its own helpers. The app logs
`WebKit's graphics process stopped responding; ending it` when it does that itself,
and `…reloading the page` if the page's own process quits. The macOS log
(`log show --predicate 'process == "Mac Engine Room"'`) has WebKit's side, e.g.
`GPUProcess with PID … became unresponsive`.

`~/Library/Logs/Powerflow/` belongs to the original Rust app (`powerflow.log`)
and to the first Python build, which ran under the old name.

The first launch after the rename logs `Moved …/Pythonflow to …/Mac Engine Room` for the
data and log folders, and, for a copy that updated itself, `Renamed …/Pythonflow.app to
Mac Engine Room.app; relaunching`. A copy installed by hand next to the old one logs
`Quit the old copy at …` and `Moved the old copy at … to the Trash`.

To reproduce without touching the user's data, run with
`PYTHONFLOW_DATA_DIR=<tmp folder>` (logs then go to `<tmp folder>/Logs/`).

## Architecture

- **Launcher**: `Application Files/main.py`. Handles logging, the single-instance
  lock, the sampler thread, the menu bar item, the pywebview window, and the `Api`
  class exposed to the page. `VERSION` here is the app's only version number. The app runs
  AppKit's run loop itself (`NSApp.run()`), and the window, with WebKit and its processes, is only
  made the first time the dashboard opens (`_make_window`, which calls `webview.start()` inside the
  running app): until then there's nothing of WebKit's to start, keep in memory or wait for, and
  1.2.18's menu bar couldn't show a reading until pywebview had made the window and started the
  run loop (#59). pywebview sets an app delegate and the menu bar's menus only when it starts the
  run loop itself, so the app sets the delegate at launch (quitting must hand the fans back) and
  the menus (`_recreate_menus`) when it makes the window. Those are pywebview 6.1's internals:
  `tests/test_window.py` fails if a newer pywebview changes them. Quitting (the menu's Quit, ⌘Q,
  the updater's relaunch) goes through `_should_terminate`: the menu bar item goes at once, and
  AppKit gets `NSTerminateLater` and waits, its run loop still running in the modal panel mode,
  while a thread of its own lets a fan write in progress finish and closes the helper's
  connection, then answers through `AppHelper.callAfter` (it posts in the common modes;
  `callLater` doesn't run there). A second `terminate:` during that wait quits at once without
  asking the delegate (measured), so `quit()` doesn't send one. The relaunch after an update waits
  up to 60 s for the old copy to exit (`launch.RELAUNCH_SCRIPT`).
- **Priority**: `Application Files/priority.py`. The main thread, the sampler and fan control run at
  one priority: background QoS, with a user-interactive override on all three at once while the
  menu is open, the dashboard is opening or watched or a fan profile holds the fans, from launch
  until the app has started and the Mac has been quiet, while the Mac is busy, and once the app is
  quitting. The main thread also sets its own QoS to user-interactive for all of those but the fans
  alone (`register(main=True)`, `_apply_main`). `Load` and `efficiency_cores` (pure, unit-tested)
  say when it's busy, when it's quiet again and which cores that is judged on; `Priority` raises
  and lowers the threads. Why: see *Idle work stays on the efficiency cores* below.
- **Launching**: `Application Files/launch.py`. The offer to move into Applications,
  relaunching, and open at login (`SMAppService`, loaded with `objc.loadBundle`).
- **The rename**: `Application Files/rename.py`. Moves an installed Pythonflow over once:
  the bundle (the updater swaps new versions in under the old name), the data and log
  folders, old copies (quit, then Trash) and the login item. Delete it once no installed
  copy can still be older than 1.2.7.
- **Updates**: `Application Files/updater.py`. Daily check of GitHub releases after
  a wake, signature check, swap and relaunch. See *How updates work* in DEVELOPMENT.md.
- **Tools**: Thermals and Power. Each is its own module for its readings; the menu
  (`statusbar.py`) and the page (`index.html`) have a section for each, and Settings a
  group for each, and the Dashboard a row of tiles for each. No plugin system beyond that.
- **Power sensors**: `Application Files/power.py`. Reads IOKit `AppleSmartBattery`
  (ctypes + PyObjC bridging) and the SMC (ctypes `IOConnectCallStructMethod`), plus
  the charge limit from powerd's `/Library/Preferences/com.apple.powerd.charging.plist`.
  `normalize()` is pure and unit-tested with captured registry dicts. Its `SMC` class
  (read-only) is shared with the Thermals tool. Chip is `PHPC`, or IOReport's where
  `wants_ioreport()` says PHPC can't give it.
- **IOReport**: `Application Files/ioreport.py`. Chip power for Macs whose `PHPC` doesn't work:
  the CPU, GPU and Neural Engine energy counters, AppleCLPC's on macOS 27 (channels made from
  macmon's table of report IDs, `CLPC_REPORTS`) and the Energy Model's before. `ChipEnergy`
  keeps one subscription for the reader's life. Picking the channels, the watts and the
  frozen-counter check are pure and unit-tested.
- **Thermals sensors**: `Application Files/thermals.py`. CPU and GPU are the hottest
  *core* sensor, from a per-chip table of core keys (Stats', checked against iSMC's dumps),
  with keys found by prefix for chips it doesn't know or whose table doesn't fit; memory, SSD
  and ambient the hottest of their group; fans' speed, limits and mode. Parked chip sensors are
  ignored, and a parked GPU's last reading is kept in `gpu_held` for display. Keys are found
  once per SMC connection, and only the groups asked for are read. `sensor_keys()` and
  `normalize()` are pure and unit-tested with a captured M1 Pro key map.
- **Sensor report**: `Application Files/report.py`. What Settings › About's Copy puts on the
  clipboard: the kind of Mac, which keys each reading came from, and every P/T/F key's value.
  `parse()` reads one back: each report that comes in is saved in `tests/sensor_reports/` as
  `<model>.txt`, and `tests/test_report.py` checks every one still finds its sensors.
- **Fan control**: `Application Files/fans.py`. The profiles (ThermalForge's numbers), the
  curve (`Curve`) and the safety override (`Safety`), both pure and unit-tested, and
  `FanControl`, a thread that runs the chosen profile on each sample and talks to the helper
  (`Helper`: its `SMAppService` daemon registration and a socket connection).
- **Fan helper**: `Fan Helper/`. The only code that runs as root: Swift, launched by launchd
  on the first connection to `/var/run/<bundle id>.fans.sock` (its plist,
  `com.pythonflow.monitor.fans.plist`, goes in `Contents/Library/LaunchDaemons`). Four
  commands (`set`, `auto`, `checkin`, `status`), only from a caller signed with the app's
  identifier by the helper's own team, and the safety nets: the fans go back to macOS when
  the app disconnects, after 10 s without a word, and before sleep. `main.swift` is the
  server; `Fans.swift` holds and hands back the fans, and `tests/test_fan_helper.py` runs it
  against a simulated SMC (`tests/fan_helper/FakeSMC.swift`) behaving like an M4 or the M1 Pro.
- **Menu bar**: `Application Files/statusbar.py`. Native `NSStatusItem` and `NSMenu`
  (PyObjC). The title shows watts or a temperature; the menu's fan profile buttons,
  Thermals tiles and Power rows are custom views (NSTextField labels, NSBox tiles,
  NSSlider, SF Symbols), then the actions. The profile buttons are the three chosen in
  Settings, drawn as tiles like the temperatures' (a borderless NSButton over an NSBox), the
  chosen one in the accent colour: stock push buttons looked out of place there. Main
  thread only.
- **History**: `Application Files/history.py`. SQLite
  (`~/Library/Application Support/Mac Engine Room/history.db`), the `ChargingRecorder`
  state machine, CSV export, and the one-time read-only import from the original
  app's `~/Library/Application Support/Powerflow/db.sqlite`.
- **Settings**: `Application Files/settings.py`. JSON, validated on every load and
  save. Open at login isn't in it: it lives in ServiceManagement, and `Api` merges it
  in for the page.
- **Frontend**: `Application Files/index.html`. A single page in vanilla JS with a
  hand-rolled canvas chart and no external resources. A **Dashboard | Thermals | Power**
  switcher and a shared Settings page. The window opens on the Dashboard, and **Open
  Dashboard** in the menu switches to it (`EngineRoomApp._show_tab`); Power always opens on
  Now, with History beside it. The Dashboard and the Thermals page share the fan controls
  (`renderFanControls`): all four profiles and the status line; each fan's speed and gauge
  (`fansHTML`) are in the Dashboard's Fans tile and on the Thermals page.
  Temperatures arrive in °C and are shown in the unit chosen in Settings.
- **Build**: `scripts/build.sh` runs PyInstaller with `Application Files/MacEngineRoom.spec`
  in `~/Library/Caches/MacEngineRoom-build`, compiles the fan helper into
  `Contents/MacOS/Mac Engine Room Fans` with `swiftc` (no compiler, no fan control), and
  signs the app. `scripts/install.sh` installs
  it in `/Applications`; `scripts/package.sh` makes the release disk image. The
  `.command` files in `scripts/` are double-click wrappers around `install.sh`.

## Key rules

- **The updater is the only network access.** Once a day it asks GitHub's API for the
  latest release and downloads its disk image, through `/usr/bin/curl`. Don't add
  anything else that connects: no HTTP server (unlike ReconciliationTool, the page uses
  pywebview's `js_api` bridge), analytics, CDNs or remote fonts. The CSP in
  `index.html` sets `connect-src 'none'`. `'unsafe-eval'` is there only because
  pywebview returns API results via `eval`.
- **Never read identifying registry keys** (`Serial`, `AdapterDetails.SerialString`,
  the computer name). `power.BATTERY_KEYS` and the `_pick` filters whitelist what
  is read.
- **Always sample inside `objc.autorelease_pool()`.** Without it, PyObjC bridging
  leaks about 2.5 KB per sample on the background thread (measured).
- **Treat every registry key as optional.** macOS 27 removed several of them, and
  the original app crashed on that.
- pywebview exposes every public attribute of `Api` to JavaScript, recursively.
  Keep internals behind a leading underscore.
- AppKit calls must run on the main thread: use `AppHelper.callAfter`.
  Non-locking pywebview events (`loaded`, `shown`) fire on worker threads.
- **Nothing on the dashboard may animate continuously**, and no CSS transition may
  outlast the 1 s refresh. The original flowing pipes and eased bars kept WebKit
  repainting every display frame: about 28% of a core, against 3% now (measured).
- The sampler runs every 1 s while the page is polling (it only polls when visible) or a
  fan profile is in charge (the curve runs once a second), and every 2.5 s otherwise.
  These are fixed, not a setting.
- **Temperatures are read every sample, like power**, so the chart has no gaps when the
  page is covered or another tab shows: about 0.9 ms of CPU per sample (measured on an
  M1 Pro), 0.03% of a core at the 2.5 s rate. **Fans only while something shows them**:
  the Dashboard or Thermals page (its polls say which page is showing), the open menu
  (which wakes the sampler as it opens), or fan control holding them. The safety
  override's hotspot and `TS0P` sensors only while fan control holds the fans.
- **Idle work stays on the efficiency cores; full priority when someone needs it.** With only the
  menu bar item showing and no fan profile chosen, the main thread, the sampler and fan control
  run at background QoS, the only one Apple silicon keeps off the performance cores. They share
  the GIL, and CPython makes the holder wait until a thread that has asked for it for 5 ms
  actually runs, so one starved thread holds the others up: **all three always have the same
  priority.** With every core busy a background thread waited up to 26 s for a turn (12 busy
  processes on the M1 Pro's 10 cores; a login launch took 62 s to its first reading), against
  5 ms at user-interactive. So they're at user-interactive (a `pthread_override_qos_class_start_np`
  override on each, which works on a blocked thread; a thread can only set its own QoS) while the
  menu is open or the dashboard is watched, from launch until the Mac has been quiet, while it's
  busy, and **while a fan profile holds the fans**: the curve's check-ins must reach the helper,
  which hands the fans back after 10 s without a word, and a background thread can wait longer
  than that. **A thread takes its override before it lowers its own QoS** (`Priority.register`):
  1.2.18 did it the other way round, and at a login launch with the efficiency cores 0% idle fan
  control's first line came 9.6 s after the menu bar item was made (an ordinary launch logs it
  before the sampler's first reading), while the main thread and the sampler stalled with it
  (#59). **The main thread keeps its own QoS until the app has started** (`_finish_launching`
  registers it, then `Priority.started`), with all three at full priority until then, so the work
  AppKit hands off while the app starts isn't sent at background QoS.
  **The main thread's own QoS goes up too, except for the fans alone.** The override raises only
  its kernel priority, and the work it hands off goes at its own QoS. WebKit's start is such work:
  during a game recompile on the maintainer's M1 Pro, with every core busy, a scratch app loaded the
  real `index.html` into a WKWebView in a window never shown, five or six times each way. It has to
  be an app of its own (LSUIElement, opened with `open -n -W`): run as a child of Claude, WebKit's
  helpers get Claude's foreground role and are always fast.

  | Main thread | Page loaded in |
  |---|---|
  | user-interactive QoS | 0.35, 0.33, 0.32, 0.34, 0.32 s |
  | background QoS with the override (1.2.19) | 7.9, 3.3, 7.3, 18.6, 0.4, 1.6 s (the fast two when the performance cores briefly had idle time) |

  So the main thread sets its own QoS to user-interactive (`pthread_set_qos_class_self_np`, which
  only works on the calling thread) while the menu is open, the dashboard is opening (from **Open
  Dashboard** until its page has loaded or the window closes: a 1.2.19 first open under load took
  6.0 s, and "watched" lapses 3 s after the last poll) or watched, the app is launching, the Mac is
  busy, or the app is quitting. Otherwise it's at background QoS, **including while a fan profile
  holds the fans on a quiet Mac**: that stays exactly as measured below for energy, with the
  override alone. A change decided on the main thread (opening the menu or the dashboard) applies
  at once, before the window and WebKit are made; one decided on another thread (the sampler
  noticing a busy Mac) is sent to the main thread with `AppHelper.callAfter`, and its override lets
  it run promptly. The sampler and fan control keep the override alone. **Once the app is quitting
  nothing is lowered** (`Priority.quit`): at a 1.2.19 quit during a recompile, the fans' hand-back
  refreshed the priority, nothing else held it up (the blank page had never polled, the menu was
  closed, `Load` had said quiet for nearly two hours), and the main thread waited 4.6 s at
  background QoS before it could answer AppKit. Busy: a sampler wait ended more than 0.25 s late
  (they normally end ~0.09 s late).
  Quiet again: 30 s with at least 15% of the **efficiency cores'** time idle (per-core tick
  counts from `host_processor_info`, read whenever the threads are at full priority for any reason,
  and never at background QoS), not a quarter of all the cores together as 1.2.15 had it. Raised,
  the threads' waits don't end late, so the cores are what notice a Mac that has turned busy: 1.2.19
  read them only while `Load` said busy, so while a profile held the fans it never saw the
  recompile above. A background thread runs only on the efficiency cores, and those can
  stay saturated while the performance cores have time to spare: on the maintainer's M1 Pro during
  a game compile, `Mac quiet again` came at 09:36:11, a menu took 2.3 s a few seconds later and
  `Mac busy` followed at 09:36:19. After a later compile there the efficiency cores stayed saturated
  for 15 minutes (background daemons), then `spotlightknowledged` held about one of the two for
  over an hour. A thread at background QoS doing 10 ms of CPU every 2.5 s (a passive probe), with
  the rest of the Mac at least half idle, was slow only when the efficiency cores were under 5%
  idle: 27 of 193 tries took over 100 ms (up to 27 s), and a timed wait ended over 0.25 s late in
  46 of 213 windows of 2.5 s. At 5–25% idle none of 437 tries took over 100 ms (0.1 s at most) and
  1 of 467 windows had a late wait. So the bar is 15%: replayed over that recording, a quarter
  called the Mac busy for a daemon's ordinary share (quiet for 54% of the time the Mac was at least
  half idle, against 76%) with the same 2 late waits and 2 slow tries landing while quiet. Late
  waits show saturation only once the threads have been dropped; the idle share shows it before.
  The efficiency cores are the IORegistry `cpu` nodes with `cluster-type` "E" (their
  `logical-cpu-id` is the row `host_processor_info` gives them), not the first CPU numbers. A Mac
  with none, or whose nodes don't say clearly, counts every core. On macOS default a quiet Mac that
  turns busy is exposed until the sampler gets a turn to notice, so the first click after a build
  starts can still be slow. Samples are scheduled from when they were due, since background waits
  end ~90 ms late. Idle energy must not rise: the quiet state reads nothing extra. Holding at full
  priority costs next to nothing (measured on the M1 Pro, Balanced held through a stand-in helper,
  10 minutes each, `proc_pid_rusage` V6): 1.2.14 0.61 mW, 1.2.15 0.63 mW per minute at the median
  (0.70 mW averaged, with one disturbed minute at 1.35), 99.7% of the CPU time on the efficiency
  cores either way, so it stays user-interactive. Not measured: a Mac that is idle all the way
  down (the efficiency cores were 33–55% idle, Spotlight was busy), where a raised thread might
  land on a performance core.
  **Why the 0.3–1.0 s a menu can still take to open with every core busy is not known.** Seen: during
  every slow open the main thread's Python stack was in `performClick_`, in no callback of ours, and
  every other Python thread was idle in a wait, so the GIL was free. That rules out our Python code
  and the GIL, not macOS's own part. Also seen: the override raises only the main thread's kernel
  priority; its own QoS stays background (`qos_class_self()` still says so), and libdispatch, XPC and
  NSOperation hand the QoS on to the work the main thread passes off; and `ps -M` under load showed
  two threads at priority 4 while the three managed ones were at 47. The experiment that would
  tell, the main thread also setting its own QoS to user-interactive while it's raised, has now been
  run for WebKit's start (the table above), not for menus, so the menu delay is still unmeasured. The
  app now does it while the menu is open and while the Mac is busy, so `Menu took … s to open` lines
  from a busy Mac after a `Mac busy` line (the main thread already up when the click came) are that
  measurement. It's probably the same mechanism, but until menus are measured, don't write that the
  delay is inside macOS, or that raising the main thread's QoS fixed it.
- **Only pass real changes to AppKit and WebKit.** A new menu bar title costs about
  0.9 ms of redraw, and setting the same one again still costs 0.2 ms (measured). The
  menu's rows and tiles are only filled in while it's open, through `_put()`, which
  skips values already shown. On the page, the once-a-second writes go through `put()`,
  which does the same, and the Thermals and Now pages only render while they're the
  active tab. Charts draw at most one point per device pixel.
- The SMC key for screen power follows the chip's tier, and the first one with a reading from
  0 to 25 W wins: `PDBR` on Pro and Max chips, M1 to M5 (measured here and by others on the M1
  Max, M3 Max, M4 Pro, M5 Pro and M5 Max), `PBLR` on base chips (measured on the base M4 at 0.3
  W at one bar and 7.2 W at full; in dumps from a base M2 and M3), `PBwo` on the A18 Pro
  MacBook Neo (measured by others; the M5 has none). A screen or chip key only counts once it has
  read at least 0.1 W (`power.SENSOR_MIN_W`), and then it stays for the launch: a board without
  the sensor can still list the key. The M1 Air's `PBLR` reads 0 at any brightness, and the 13"
  M4 Air's `PBLR` and `PHPC` read 0.01 W even under load (their sensor reports; #51), so they're
  in `power.NO_SCREEN_SENSOR` and, for the M4 Air, `power.NO_CHIP_SENSOR`. The quietest working
  key on file is the base M4's `PBLR` at one bar, 0.3 W.
- SMC `PHPC` is chip (CPU + GPU package) power, **not** "heatpipe" or fan power,
  despite Apple's key name. Verified: it rises about 11 W under full CPU load while
  the fans stay at minimum. No other SMC key stands in for it: `PZC0`, `PZC1` and `PHPS` are
  undocumented, and aren't chip power on the M1 Pro.
- **Where `PHPC` can't give Chip, IOReport does, and nowhere else.** That's a model in
  `power.NO_CHIP_SENSOR` (the 13" M4 Air) or a Mac with no `PHPC` key; every other Mac never
  opens IOReport, so its idle cost doesn't rise. An unlisted Mac whose `PHPC` is listed but dead
  stays hidden until its sensor report lists it. IOReport counts only the CPU, GPU and Neural
  Engine, not the rest of the package, so it reads lower than `PHPC`: a steady 7 to 8 W less on
  the M1 Pro (two 20 s runs, medians 9.3 W against 16.8 W and 4.0 W against 13.5 W). On macOS 27 the Energy Model's CPU
  counter doesn't count without an Apple entitlement (measured here: 0 while `PHPC` read 10 to
  30 W), so 27 uses AppleCLPC's counters, whose report IDs are per driver and per macOS: macmon
  extracts them from Apple's restore images. A newer macOS isn't in `ioreport.CLPC_REPORTS`, so
  it tries the Energy Model and gives up if its CPU counter doesn't move; take macmon's new table
  when it has one. Chip never shows a counter that doesn't count, and IOReport's readings follow
  `SENSOR_MIN_W` like `PHPC`'s.
- `~/Documents` is iCloud-synced on this machine. Finder metadata blocks
  `codesign`, so `scripts/build.sh` builds in `~/Library/Caches` and runs `xattr -cr`
  before signing.
- **Don't relaunch into an update while a charge is being recorded.** A session in
  progress lives in memory until it ends, so quitting loses it. A running fan profile
  doesn't hold updates back: the fans go back to macOS at quit and the profile resumes.
- **The fan helper stays small and Swift.** It's the only code that runs as root, and the
  bundle it runs from is writable by the user, so it never runs anything from the bundle,
  takes no command beyond the four, clamps every speed to the fan's own minimum and
  maximum, never writes a target of 0 (in manual mode it stops the fan), and writes
  nothing while another app has a fan in manual mode. Its sockets set `SO_NOSIGPIPE`.
- **Mode keys read back late.** `F%dMd` written as root reads as the old mode for up to a
  second (measured on the M1 Pro), so the helper only rewrites a mode after 2 s, and
  macOS turns a handed-back 0 into 3 within about 20 s by itself. **Targets probably do too**
  (not measured; #48 was most likely the helper reading back its own earlier speed and taking
  it for another app's, which left the fans stuck in manual mode). So the helper keeps each
  fan's target speeds with when each was replaced, and a read of the current one, or of one
  replaced in the last 2 s, is its own: it counts from the replacing write, since a late read
  shows a speed that may have been set long before. Any other read is another app's. It
  forgets them once the fans are back with macOS, and starts with none: a fan in manual mode
  isn't the helper's until it has set it.
- **Macs with `Ftst` refuse a manual mode until it's 1.** It keeps macOS's thermalmonitord
  off the fans, so it must go back to 0 with them: left at 1, nothing runs the fans. The M4
  has it (every mode write failed for 30 s without it, "error unlock" in 1.2.11); the M1 Pro
  doesn't. The helper sets it before the modes and clears it after them, on every hand-back.
- **Ask `backgroundtaskmanagementd` as little as possible.** `SMAppService.status()` is
  answered by it, and it took 8 s once while busy. Fan control asks while waiting for the
  user's approval, and after that only when the helper can't be reached. A daemon that
  was never registered reports *not found*, not *not registered*.
- **A key from another project's table is used when its reading makes sense**: one established
  project (Stats, iSMC, Asahi Linux, macmon…) listing it for that chip is enough, with the
  source in a comment next to it. iSMC is GPL-3.0: use its dumps as evidence, never copy its
  table. The app being GPL too would allow it, but keeping other people's GPL code out leaves
  the maintainer free to relicense their own code, or give a fix to Powerflow or ThermalForge
  under MIT. A Mac that's missing a reading for real hides it; a report proves which it is.
- **The app is GPL-3.0-or-later** (MIT up to 1.2.17). Anything the build bundles, and code or
  other material adapted from another project, needs its notice in `THIRD-PARTY-NOTICES`, which
  the build copies into the app next to `LICENSE`; `scripts/package.sh` checks each build
  against it (`tools/check_notices.py`). The spec excludes what only pywebview's web
  server would use (cryptography, bcrypt, jinja2, markupsafe): the page goes in as `html=`, so
  it never runs, but PyInstaller follows deferred imports. Powerflow's MIT notice stays there:
  the icon is still Powerflow's.
- WebKit pauses rendering in a window that's covered or hidden, so `ResizeObserver` and
  `requestAnimationFrame` don't run there. A test that drives the page while other
  windows cover it sees no size reports; that's not a bug.

## Releasing

- A release means the whole **Release checklist** in DEVELOPMENT.md, every time, without
  being reminded: version bump, tests, DMG checks, release notes, publishing, drafting
  the previous release, the README, and checking the live GitHub page afterwards.
- Merging to `main` is not a release. If user-facing changes are merged but unreleased,
  say so: installed copies only update when a release is published.
- When the maintainer says a pull request is merged, do the release straight away without
  asking: the version bump on a `claude/release-<version>` branch, its pull request,
  merging that yourself, then building, publishing, drafting the previous release and the
  checks. Asking first only adds approvals.
- Check each step's result before the next step that can't be undone. Don't chain a
  check and a publish with `&&`.
- Releases are signed with the maintainer's Apple Development certificate (team
  `XT4LT7MS6Y`). Installed copies refuse updates from any other team. The paid developer
  membership has lapsed and the maintainer doesn't want to renew it yet, so don't plan
  around Developer ID or notarization.
- Releases have only the `.dmg`. The page should list only the current release.

## Testing on the maintainer's Mac

- The maintainer's own copy is running from `/Applications` (as Pythonflow until it
  updates to 1.2.7). Don't quit it, reinstall it or change its data to test something
  without asking. Test with `PYTHONFLOW_DATA_DIR` and, for the updater, a copy with its
  own bundle identifier (see *Testing the updater*).
- **Never launch a build with the real bundle identifier, or run from source, without
  `PYTHONFLOW_DATA_DIR`.** `rename.py` would quit the maintainer's running Pythonflow,
  move it to the Trash and move its data folder. Test copies with their own identifier
  only touch copies with that identifier.
- Say so before opening windows or menu bar items on screen for a test. The maintainer
  may be in the middle of something.
- After testing the updater, delete the test copy, its data folder and the test disk
  images, and make sure no test copy is left in Login Items.
- **Look at every change to the page or the menu yourself**, in light and dark and in the
  states it touches, before the maintainer sees it. `page-preview` in `.claude/launch.json`
  (`tools/preview/serve.py`) runs `index.html` in the browser pane with made-up readings,
  e.g. `?s=holding,fahrenheit&tab=thermals`; its states are listed in
  `tools/preview/stub.js`. `tools/render_menu.py` draws the menu to PNGs (`--help`). When
  the app gains a state or an API call, add it to both.
- **Fan control is tested with a test copy too** (`scripts/make-test-copy.sh`; see
  *Testing fan control* in DEVELOPMENT.md): its own identifier, helper label and socket, so
  it can't touch the maintainer's helper. Allowing each new helper in Login Items is the
  maintainer's click and password, so ask first, and say the fans will be audible.
  Afterwards, unregister its helper from inside its own bundle before deleting it, or
  launchd keeps a dead job.
- The settings, the profile tuning and the curve can be tested without the helper: the
  pure parts of `fans.py` and `FanControl` with a stand-in helper (`tests/test_fans.py`).
- To test a login launch without logging out, open the app with
  `NSWorkspace.openApplicationAtURL_configuration_completionHandler_` and a configuration
  whose `appleEvent` is `oapp` with `keyAEPropData` ('prdt') set to the enum 'lgit', which
  is what loginwindow sends. From source, open Python.app with `main.py` as its argument.
- Claude can't screenshot the running app or click its menu. To see what the app decided,
  read its log. To drive something only a menu or a wake can trigger, add a temporary
  hook to a scratch copy of the source (not the repository) and build from that.

## Git

- Work on a branch, not `main`.
- When the maintainer says to commit, also push and open the pull request, with Auto-fix
  on. Don't ask about the PR separately.

## Decisions already made

Don't undo these without asking:

- WebKit still starts the first time the dashboard opens, not at launch (#59), and there's no
  "Starting…" placeholder while it does: the fix for the blank dashboard under load is the main
  thread's own QoS (see *Idle work stays on the efficiency cores*).
- The window opens on the Dashboard, a short page with a row of tiles per tool, rather
  than cutting Power's Battery, Adapter and System cards to make Now shorter.
- Fan control starts on macOS default, and the helper is only registered the first time
  another profile is chosen. The choice is remembered across relaunches and logins.
- Four fan profiles: macOS default, Balanced, Performance and Max. Smart and Manual were
  removed in 1.2.11; a saved choice of either becomes macOS default.
- A profile has the fans from the moment it's chosen until another is: Balanced and
  Performance hold them at their minimum below their start temperature, Max at full speed
  the whole time. Leaving them with macOS until the cores warmed up made choosing a profile
  seem to do nothing. The helper never writes 0, so only macOS default lets the fans stop.
- The menu offers three fan profiles, chosen in Settings (macOS default, Balanced and Max by
  default), not all four; all four are on the Dashboard and the Thermals page.
- Settings shows Balanced and Performance as a compact table (Speeds up at, Top speed, Top
  speed at), with no descriptions: the maintainer found the wordier version text-heavy. Max
  has nothing to tune.
- Fan gauges run from off to the fan's maximum, with no Min label: off is a fan's slowest.
- Fan text is plain English, checked as whole sentences: the fans go back to macOS (they
  don't "stop"), and a reason completes "Fan control isn't available …" or "Fan control
  stopped because …".
- Temperatures are in Celsius unless Fahrenheit is chosen in Settings > Thermals. The app
  doesn't follow macOS's unit: Americans who read Celsius would have no way to get it.
- Temperatures are read all the time, like power, so the Thermals chart has no gaps.
  Reading them only while shown left a gap whenever the page was covered or another tab
  showed.
- Choosing Power always opens Now, not the last Power page.
- Apple silicon only (the build is arm64, so Intel Macs can't open it). Desktops get the same
  sensor work as laptops: on a Mac with no battery, Power says "Desktop Mac" and shows System and
  Chip power, with no Battery or Adapter card, row, tile or chart line; fan control is offered
  wherever fans are found.
- A reading a Mac doesn't have stays hidden. Settings › About has **Sensor report** (Copy, and
  Report a Problem…, which opens the GitHub issue template in the browser) on the row under the
  log file, not in a section of its own, with a hint under it only when the app guessed.
- Readings below freezing count: only chip sensors are judged parked, against the Mac's coolest
  other reading, not by a fixed floor.
- A parked GPU shows its last real reading (display only), with "Last reported x min ago" under
  it after a minute on the Dashboard and Thermals page, and dimmed in the menu; "—" until the
  first real reading.
- The safety override's `TS0P` trip says "a sensor inside the Mac passed 95°": iSMC places it
  by the SSD, not on the case.

- Updates are checked once a day, only when the Mac wakes from sleep (plus **Check for
  Updates…** in the menu and **Check Now** in Settings), and installed silently at a quiet
  moment. On by default; there's a switch in Settings.
- **Delete All…** for charging history only goes ahead once "delete" is typed in its
  confirmation. Deleting one session keeps the plain confirmation.
- Open at login is turned on once, the first time the app runs from Applications.
  Turning it off sticks.
- The app always starts in the menu bar only, at login or opened by hand; there's no
  setting. Opening it again while it runs, or **Open Dashboard**, shows the dashboard.
- A Mac with no screen or chip reading doesn't show Screen or Chip anywhere, and the menu bar
  falls back to System if that one was chosen. It happens automatically, with no estimate and
  no stand-in SMC key (#51). Chip comes from IOReport where `PHPC` can't give it (#51 again);
  only when that can't give it either is Chip hidden. Screen has no such source.
- The dashboard window opens as tall as the page, up to the screen's height, and can't
  be made wider or taller than the page needs: extra size only added empty space.
  Switching tabs resizes it to the new page.
- The rename to Mac Engine Room kept these on the old name, because installed copies,
  the test procedures or stored data depend on them: the bundle identifier
  `com.pythonflow.monitor`, the `PYTHONFLOW_DATA_DIR` and `PYTHONFLOW_UPDATE_FEED`
  variables (1.2.6 passes the old one on when it relaunches a test copy into an update),
  the `pythonflow` logger, and `source = 'pythonflow'` on recorded charges.
- Never create a new repository called `DigitalN/pythonflow`: GitHub's redirect to
  `DigitalN/mac-engine-room`, which copies older than 1.2.7 check for updates through,
  would stop.

## Working style

- When fixing a reported problem, think through the related cases and fix the whole
  class, so the maintainer doesn't have to report each one.

## Tests

`python3 -m pytest`. `tests/test_power.py::test_reads_this_mac` does a live
hardware read, and is skipped where there's no battery or SMC; `tests/test_ioreport.py::test_reads_this_mac`
reads this Mac's IOReport for 0.3 s, and is skipped where it can't be used. `tests/test_fan_helper.py`
compiles the fan helper with `swiftc` (about 3 s), and is skipped without it.
