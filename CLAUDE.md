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
check. Each launch logs `started (dashboard shown)` or `started (dashboard hidden)`,
plus `, opened at login` for a login launch. A Mac missing any SMC key the app reads
logs `SMC keys missing on <model>, macOS <version>: …` once per launch, with the power
keys it does have. Reopening the app at one bar of brightness and again at full shows
which of them is the screen. The first temperature reading after launch (or after an SMC
reconnect) logs `Temperature sensors on <chip>: cpu N, gpu N, …; N fans`, plus the groups
found by prefix when the chip isn't in the core key table.

Fan control logs `Fan profile: <name>` when one is chosen (and at launch), `Asked macOS to
allow the fan helper`, `Fan control is waiting for the helper to be allowed in Login Items`,
`Fans held by <profile> at <rpm> rpm (cores N°)`, `Fans back to macOS: <why>`,
`Safety override: fans at full speed (<why>)` and `Safety override over`, `Another app is
controlling the fans; leaving them alone`, and `Fan control isn't available …` or `Fan
control stopped because …`. The helper logs to the unified log, not app.log:
`/usr/bin/log show --last 1h --predicate 'subsystem == "com.pythonflow.monitor"'` (zsh's own
`log` command shadows it) shows `Holding the fans at [rpm, rpm] rpm` and `Fans back with
macOS (<why>)`, where why is the app asking, it disconnecting, no word for 10 seconds, or
sleep. `/var/run/<bundle id>.fans.holding` exists while the helper holds the fans.

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
  class exposed to the page. `VERSION` here is the app's only version number.
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
  (read-only) is shared with the Thermals tool.
- **Thermals sensors**: `Application Files/thermals.py`. CPU and GPU are the hottest
  *core* sensor, from a per-chip table of core keys taken from Stats, with keys found by
  prefix for chips it doesn't know; memory, SSD and ambient the hottest of their group;
  fans' speed, limits and mode. Keys are found once per SMC connection, and only the
  groups asked for are read. `sensor_keys()` and `normalize()` are pure and unit-tested
  with a captured M1 Pro key map.
- **Fan control**: `Application Files/fans.py`. The profiles (ThermalForge's numbers), the
  curve (`Curve`) and the safety override (`Safety`), both pure and unit-tested, and
  `FanControl`, a thread that runs the chosen profile on each sample and talks to the helper
  (`Helper`: its `SMAppService` daemon registration and a socket connection).
- **Fan helper**: `Fan Helper/`. The only code that runs as root: Swift, launched by launchd
  on the first connection to `/var/run/<bundle id>.fans.sock` (its plist,
  `com.pythonflow.monitor.fans.plist`, goes in `Contents/Library/LaunchDaemons`). Four
  commands (`set`, `auto`, `checkin`, `status`), only from a caller signed with the app's
  identifier by the helper's own team, and the safety nets: the fans go back to macOS when
  the app disconnects, after 10 s without a word, and before sleep.
- **Menu bar**: `Application Files/statusbar.py`. Native `NSStatusItem` and `NSMenu`
  (PyObjC). The title shows watts or a temperature; the menu's fan profile buttons, Manual
  slider, Thermals tiles and Power rows are custom views (NSTextField labels, NSBox tiles,
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
  (`renderFanControls`): all six profiles, Manual's slider and the status line.
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
  override's hotspot and case sensors only while fan control holds the fans.
- **Idle work stays on the efficiency cores.** With only the menu bar item showing,
  the sampler and the main thread run at background QoS, the only one Apple silicon
  keeps off the performance cores. The dashboard raises the sampler to utility, and the
  dashboard or the menu raises the main thread to user-interactive: with every core
  busy, a background thread waited up to 1.6 s for a turn (measured). Samples are
  scheduled from when they were due, since background waits end ~90 ms late. A fan profile
  watching the temperatures stays at background QoS; holding the fans raises the sampler
  and the fan thread to utility, so check-ins reach the helper on a busy Mac.
- **Only pass real changes to AppKit and WebKit.** A new menu bar title costs about
  0.9 ms of redraw, and setting the same one again still costs 0.2 ms (measured). The
  menu's rows and tiles are only filled in while it's open, through `_put()`, which
  skips values already shown. On the page, the once-a-second writes go through `put()`,
  which does the same, and the Thermals and Now pages only render while they're the
  active tab. Charts draw at most one point per device pixel.
- The SMC key for screen power depends on the Mac, and the first one found wins: `PDBR`
  (M1 Pro, measured), `PBLR` (base M4 Mac16,1, which has no `PDBR`; measured at 0.3 W at
  one bar and 7.2 W at full), `PBwo` (M5 and A18 per macpow's docs, not measured).
- SMC `PHPC` is chip (CPU + GPU package) power, **not** "heatpipe" or fan power,
  despite Apple's key name. Verified: it rises about 11 W under full CPU load while
  the fans stay at minimum.
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
  macOS turns a handed-back 0 into 3 within about 20 s by itself.
- **Ask `backgroundtaskmanagementd` as little as possible.** `SMAppService.status()` is
  answered by it, and it took 8 s once while busy. Fan control asks while waiting for the
  user's approval, and after that only when the helper can't be reached. A daemon that
  was never registered reports *not found*, not *not registered*.
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
- **Fan control is tested with a test copy too** (see *Testing fan control* in
  DEVELOPMENT.md): its own identifier, helper label and socket, so it can't touch the
  maintainer's helper. Allowing each new helper in Login Items is the maintainer's click
  and password, so ask first, and say the fans will be audible. Afterwards, unregister its
  helper from inside its own bundle before deleting it, or launchd keeps a dead job.
- The settings, the profile tuning and the curve can be tested without the helper: the
  pure parts of `fans.py` and `FanControl` with a stand-in helper (`tests/test_fans.py`).
- To test a login launch without logging out, open the app with
  `NSWorkspace.openApplicationAtURL_configuration_completionHandler_` and a configuration
  whose `appleEvent` is `oapp` with `keyAEPropData` ('prdt') set to the enum 'lgit', which
  is what loginwindow sends. From source, open Python.app with `main.py` as its argument.
- Claude's shell can't take screenshots or click menus. To see what the app decided,
  read its log. To drive something only a menu or a wake can trigger, add a temporary
  hook to a scratch copy of the source (not the repository) and build from that.

## Git

- Work on a branch, not `main`.
- When the maintainer says to commit, also push and open the pull request, with Auto-fix
  on. Don't ask about the PR separately.

## Decisions already made

Don't undo these without asking:

- The window opens on the Dashboard, a short page with a row of tiles per tool, rather
  than cutting Power's Battery, Adapter and System cards to make Now shorter.
- Fan control starts on macOS default, and the helper is only registered the first time
  another profile is chosen. The choice is remembered across relaunches and logins.
- The menu offers three fan profiles, chosen in Settings (macOS default, Balanced and Max by
  default); all six are on the Dashboard and the Thermals page.
- Settings shows the fan profiles as a compact table (Starts at, Top speed, Top speed at),
  with no descriptions and no Fans stop: the maintainer found the wordier version text-heavy.
- Fan text is plain English, checked as whole sentences: the fans go back to macOS (they
  don't "stop"), and a reason completes "Fan control isn't available …" or "Fan control
  stopped because …".
- Temperatures are in Celsius unless Fahrenheit is chosen in Settings > Thermals. The app
  doesn't follow macOS's unit: Americans who read Celsius would have no way to get it.
- Temperatures are read all the time, like power, so the Thermals chart has no gaps.
  Reading them only while shown left a gap whenever the page was covered or another tab
  showed.
- Choosing Power always opens Now, not the last Power page.

- Updates are checked once a day, only when the Mac wakes from sleep (plus **Check for
  Updates…** in the menu and **Check Now** in Settings), and installed silently at a quiet
  moment. On by default; there's a switch in Settings.
- **Delete All…** for charging history only goes ahead once "delete" is typed in its
  confirmation. Deleting one session keeps the plain confirmation.
- Open at login is turned on once, the first time the app runs from Applications.
  Turning it off sticks.
- At login the app starts in the menu bar only, whatever *Open this window at launch*
  says; that setting covers opening the app by hand. Opening the app again while it
  runs in the background shows the dashboard.
- A Mac with no screen reading doesn't show Screen anywhere, and the menu bar falls back
  to System if Screen was chosen. It happens automatically, and there's no estimate.
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
hardware read, and is skipped where there's no battery or SMC.
