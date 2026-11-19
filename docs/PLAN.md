# Mac Engine Room: the plan

Pythonflow becomes **Mac Engine Room**, an app with two tools: **Thermals**, which replaces
[ThermalForge](https://github.com/ProducerGuy/ThermalForge), and **Power**, which is today's
app. Agreed with the maintainer on 2026-11-02. Each release ticks its part off here.
Keep Awake joined them in 1.4.0, and on 2026-11-19 two more were agreed to trim the menu
bar: **Displays**, which replaces what the maintainer uses BetterDisplay for, and **Mouse**,
which replaces LinearMouse.

## Releases

Each is the usual +0.0.1 with the whole release checklist in DEVELOPMENT.md.

1. **The rename and the folder move.** Merged (DigitalN/pythonflow#21) and released together
   with release 2 as 1.2.7 on 2026-11-02, when the repository became DigitalN/mac-engine-room.
2. **The tool framework, the Thermals page with temperatures, and the new menu.** Merged
   (DigitalN/pythonflow#22) and released as 1.2.7 on 2026-11-02. After trying it, the
   maintainer asked for a Celsius/Fahrenheit setting, no gaps in the chart, and Power always
   opening on Now. Checked on the M1 Pro: under a full CPU load the CPU reading went from 58°
   to 83° (GPU 57° to 68°), and under a GPU load the GPU reading from 60° to 73° (CPU 61° to
   69°). The fans stayed at minimum. In the menu, the fans tile shows the fastest fan: two
   speeds don't fit in a tile. The dashboard shows each fan.
3. **Fan control and the Dashboard**, in one release. Fan control starts with a throwaway
   test of the root helper. Merged (DigitalN/mac-engine-room#24) and released as 1.2.8 on
   2026-11-02. The throwaway test passed (see Fan control).
   End to end on the M1 Pro with a test copy: after one Allow in Login Items, Manual at 10%
   held the fans at 1,779 and 1,824 rpm (each fan's own range), and Balanced at 59° held
   both at their minimum, 1,499 rpm. The fans went back to macOS on quit, 0.6 seconds after
   `kill -9`, and 11 seconds after the app was stopped (`SIGSTOP`); resumed, it took them
   again. Relaunching brought the profile back without asking again. The window opened on
   the Dashboard at 1060 × 439. **Not yet tried on hardware:** sleep and wake, the safety
   override, another app holding the fans, Fahrenheit tuning in the app's own WebKit, and
   an update replacing the helper (BTM keeps a checksum of the job, so check after the
   first update that changes it that no new Allow is asked for). The changes after trying it
   (profile tiles and three in the menu, the compact Fan profiles settings, the Dashboard's
   fan controls) were merged as DigitalN/mac-engine-room#26 and released as 1.2.9 on
   2026-11-02.
4. **Keep Awake** (see Keep Awake), designed with the maintainer on 2026-11-19 in three
   grilling rounds. Released as **1.4.0** on 2026-11-19.
5. **Displays and Mouse** (see Displays, Mouse and Shared groundwork), designed with the
   maintainer on 2026-11-19 in three grilling rounds. Built at the same time by two sessions,
   each with its own branch and pull request. **They ship together as 1.5.0 once both are
   merged:** when the first one is merged, don't release it on its own.

## Name and identity

- Mac Engine Room everywhere a person sees it: the app, the window, the menu, the disk image
  (`Mac-Engine-Room-<version>.dmg`).
- The bundle identifier stays `com.pythonflow.monitor`, so installed copies update themselves
  into the new name and keep their Login Items entry. The updater only accepts an app with
  the same bundle identifier.
- The repository becomes `DigitalN/mac-engine-room`. GitHub redirects the old name, and the
  updater follows redirects.
- Data and logs move once, to `~/Library/Application Support/Mac Engine Room` and
  `~/Library/Logs/Mac Engine Room`. The local checkout keeps its folder name for now.

## Structure

- A **Thermals | Power** switcher at the top of the window, Thermals first. Power keeps its
  Now and History pages, and always opens on Now. Settings is shared, with a section for
  each tool. Release 3 puts a **Dashboard** in front of them (see Dashboard).
- **Changed on 2026-11-19 for 1.5.0:** the switcher becomes **Dashboard | Thermals | Keep
  Awake | Power | Displays | Mouse**. Like Keep Awake, Displays and Mouse have nothing in
  Settings and no Dashboard row.
- Each tool is its own Python module that adds its readings, its menu section and its
  settings. No plugin system beyond that.

## Dashboard

Agreed on 2026-11-02, for release 3. Power's Now page is much taller than Thermals, so the
window changed size a lot between them. Rather than cutting Power's Battery, Adapter and
System cards, a Dashboard gives a short page to land on:

- The switcher becomes **Dashboard | Thermals | Power**, Dashboard first. The window opens on
  it, and **Open Dashboard** in the menu opens it too.
- One row of tiles per tool, each row headed by the tool's name with a "Thermals ›" or
  "Power ›" link to its tab:
  - **Thermals:** CPU, GPU, Fans (RPM, plus the active profile once fan control exists)
  - **Power:** System (W), Battery (% and time left, or charging), Adapter (W in, or "Not
    connected")
- No charts; those stay on the tool tabs, so the page stays short. A future tool adds a row.
- **Added on 2026-11-02 after trying 1.2.8:** the fan profile buttons (all six) in the Thermals
  row's heading. Under the tiles, a slim card shows Manual's slider while Manual is chosen,
  and a status line only when something needs attention (approving the helper, another app,
  the safety override, an error). The Fans tile says who has the fans: macOS, the profile,
  or another app.
- Power and Thermals don't change: Battery, Adapter and System stay on Power's Now page, and
  charging history stays where it is.

## Sensors

ThermalForge's issues (#62, #63, #61, #35, #78) show it reads the wrong sensors, which also
made its fan curves too loud. Do it right from the start:

- **CPU and GPU are the hottest *core* sensor**, from a per-chip table of core keys like the one
  in [Stats](https://github.com/exelban/stats) (MIT), with keys discovered at runtime from the
  SMC's own key list as the fallback for chips the table doesn't know (Stats' table covers M1
  to M6 and the A18 Pro as of 2026-11-02). Check the result here against a CPU load and a GPU
  load: the reading has to rise with the load.
- **The table is checked against real dumps, not just Stats** (1.2.13). Stats took between 3
  weeks and 18 months to support each chip, still has the M4 Pro partly wrong (Stats #3270),
  and counted the M3's GPU fabric (`Tf…`) as CPU cores. [iSMC](https://github.com/dkorunic/iSMC)'s
  table is built from dumps users sent in, but it's GPL, so its dumps are evidence for which
  keys exist, never a file to copy. One key per core: the middle of each `Tp`/`Te` triplet, the
  second of each `Tg` pair. The table also falls back to the prefixes when it finds fewer CPU
  keys than half the chip's cores (`hw.ncpu`): a variant whose cores live elsewhere.
- **The base M1 names its cores differently** from the M1 Pro (its sensor report, 1.2.14): four
  keys per core, `a`/`b`/`x`/`z`, where `z` is `x` plus a whole number of degrees. `TCMb`
  equals the hottest `b` key there, as it equals the hottest middle key on the M1 Pro, so `b`
  is the core. Its two GPU keys read 0 while idle, and it has no `Tm` or `TA` keys.
  Its `PBLR` read 0.00 W at full brightness and at one bar, and no other P key moved (only
  `PSTR`, by 2.2 W), so the M1 Air has no screen sensor. A screen key now only shows once it has
  read above 0, on any Mac, and Settings only asks for a report when a laptop has no screen key at
  all (one that reads 0 may be a closed lid).
- **The 13" M4 Air's** `PBLR`, `PHPC`, `PPMR` and `PPSR` read 0.01 W (its sensor report, #51),
  where the base M4 MacBook Pro (Mac16,1) reads real watts (`PHPC` 1.1–2.3 W, `PBLR`
  0.29–7.3 W). So a key now counts at 0.1 W, not "above 0", and Chip follows the same rule as
  Screen: hidden everywhere until `PHPC` has read 0.1 W, and then kept for the launch. Mac16,12
  is in `power.NO_SCREEN_SENSOR` and `power.NO_CHIP_SENSOR`; the 15" (Mac16,13) isn't proven, so
  it is judged by what it reads. No other key stands in for them: `PZC0`, `PZC1` and `PHPS` are
  undocumented, and aren't chip power on the M1 Pro.
- **Sensor reports** (Settings › About) list a Mac's P/T/F keys with their values, and name the key
  each power reading uses. Each one that comes in is kept in `tests/sensor_reports/`, and
  `tests/test_report.py` fails until that Mac finds its CPU and GPU in the table and, on a laptop,
  a screen key and `PHPC` that read at least 0.1 W (unless it's in `power.NO_SCREEN_SENSOR` or
  `power.NO_CHIP_SENSOR`). Settings asks for one only when the app guessed: CPU or GPU by prefix,
  no GPU key, or a laptop with no screen key that isn't in `power.NO_SCREEN_SENSOR`. A chip key
  that reads next to nothing doesn't ask.
- **Not counted as CPU:** the chip hotspot keys (`TCDX`, `TCMz` and the like), which run up to
  ~10° above any core, and the helper `Tp` keys next to each core key (on M4 one key in four is
  the core; others read 0 while the core sleeps and 10–13° hot under load). Hotspots are used
  only by the safety override. On this M1 Pro `TCMb` turned out to equal the hottest CPU or GPU
  core exactly, idle and under either load.
- **Not counted as GPU:** `TG0B`, `TG0C`, `TG0H`, `TG0V`, `TG1B`, `TG2B`. They're the battery's
  gas-gauge sensors (on this M1 Pro `TG0B` reads exactly `TB0T`, the battery). GPU is `Tg…`.
- Readings of exactly 0, or outside -40–130°, are ignored (`TH1a`/`TH1b` read 0 here). Below
  freezing is real, outside on a cold day.
- **Parked chip sensors are ignored.** A powered-down core cluster or GPU reads -4°, 0°, 1.5–5°,
  or one value on every key (a flat 40.0° on an M4 Pro). A CPU or GPU reading more than 10°
  under the Mac's coolest other reading (ambient, memory or SSD) is parked: nothing on the chip
  is cooler than the air and parts around it. Not the hottest CPU core: an idle GPU that's
  awake reads far below a loaded CPU. Nor does a group whose keys (three or more) all read one
  exact value.
- **A parked GPU shows its last real reading**, for display only (the fan curve and the
  "hottest" title use live readings), with "Last reported x min ago" under it after a minute,
  and dimmed in the menu.
- Apple silicon SMC values are little-endian `flt`; M5 and later also use 8-byte `ioft`
  (16.16 fixed point in the first 4 bytes, little-endian, signed), which
  `power.decode_smc_value` decodes. This M1 Pro has a few `ioft` keys too: the `TG…` battery keys and `TR0Z`/`TR1d`/`TR2d`.
- The `SMC` class caches each key's type, so a reading is one IOKit call. Find the keys once
  per connection and read only those, never the whole list (ThermalForge #79, #70).

This M1 Pro (MacBookPro18,1), lightly loaded on 2026-11-02, has 2,181 SMC keys:

| Prefix | What it looked like |
|---|---|
| `Tp` (30) | in threes per core: `Tp00`/`Tp01`/`Tp02`, `Tp04`/`Tp05`/`Tp06`…; the first ~51°, the second ~61°, the third ~68° |
| `TC` (16) | `TC10`–`TC33` 52–57°, `TCDX` 57°, `TCMb` 63.5°, `TCMz` 72° (hotspots) |
| `Tg` (4) | `Tg04` 51.6, `Tg05` 60.8, `Tg0C` 50.8, `Tg0D` 60.0 |
| `TG` (6) | all ~29–30°, the same as `TB0T`: battery |
| `Tm` (6), `TR` (24) | memory candidates, ~49–53° |
| `TH` (5) | SSD: `TH0a`/`TH0b`/`TH0x` ~35.5°; `TH1a`/`TH1b` 0 |
| `TA` (1) | `TAOL` 26.3°: ambient (`Ta…` keys are something else; several read 0 or 8°) |
| `Te`, `Ts`, `Td`, `Th`, `TP`, `TD`, `TV`… | other die, power-delivery and board sensors |
| fans | `FNum` 2; `F0`/`F1`: `Ac`, `Tg`, `Mn` 1499, `Mx` 4296/4744, `Md` 3 (macOS in control); `Ac` reads 0 when the fans have stopped |

## Thermals

- CPU, GPU, memory, SSD and ambient temperatures (CPU and GPU from core sensors, the others the
  hottest sensor in their group), each fan's RPM and mode, and a rolling chart like Power's.
  No temperature history on disk. macOS's thermal pressure is read (for the safety override)
  but not shown.
- Celsius by default, with Fahrenheit as a choice in Settings > Thermals. Changed on
  2026-11-02 after trying release 2: following macOS's unit left no way to read Celsius on
  a Mac set up for the US.
- Temperatures are read every sample, like power, so the chart has no gaps. Changed on
  2026-11-02 after trying release 2: reading them only while shown left a gap whenever the
  page was covered or another tab showed. It costs about 0.9 ms of CPU per sample (0.03% of
  a core). Fans are only read while the page or the menu shows them, or a profile is running.
- Profiles: macOS default, Balanced, Performance, Smart and Max with ThermalForge's numbers,
  plus **Manual**, one slider from minimum to maximum RPM for all fans. Starts on macOS
  default; the choice is remembered across relaunches and logins.
- The curves follow the hottest CPU or GPU **core** temperature, not the hotspots or the
  battery sensors ThermalForge mixed in, so the same numbers start the fans later and quieter.
- Max stays as in ThermalForge: nothing until the cores pass its start temperature (65°) for
  5 seconds, then full speed. Manual with the slider at the right is instant full speed; there's
  no separate button for it.
- Settings > Thermals tunes each profile: **Fans start at** (50–85°), **Full speed at**
  (at least 5° above start, at most 94°) and **Top speed** (30–100%). **Fans stop at** follows
  Fans start, keeping the profile's own gap. Max only has Fans start. Each profile has Reset.
  Smart's rate boost, trigger delays, ramp rates and the safety override stay fixed.
  **Changed on 2026-11-02 after trying 1.2.8:** "Full speed at" beside "Top speed 60%" made
  no sense, and the section was too wordy. Settings has a Fan profiles group: the menu's three
  pickers, then a compact table with the headings once (Starts at, Top speed, Top speed at)
  and a row of numbers per profile (Max: 100%, At once), Reset only on a changed one. No
  descriptions, and Fans stop isn't shown.
- Defaults: Balanced 55° to 70° at 60%, Performance 55° to 65° at 85%, Smart 53° to 85° at
  100%, Max from 65°.
- **Changed on 2026-11-03 after trying 1.2.10:** four profiles; Smart and Manual are gone. A
  profile has the fans from the moment it's chosen: Balanced and Performance at their minimum
  below their start, Max at full speed the whole time. Leaving them with macOS until the
  cores warmed up made choosing a profile on a cool Mac seem to do nothing, and Max not
  maxing the fans made no sense. Settings tunes Balanced and Performance only (Speeds up at,
  Top speed, Top speed at). The Dashboard's Fans tile shows each fan's speed and gauge like
  the Thermals page, the gauges lose their Min label (off is a fan's slowest), and the app
  always starts in the menu bar: the *Open this window at launch* setting is gone.
- Macs without fans show temperatures only. Desktops get everything. Where the helper can't
  be approved (for example a managed work Mac), fan control shows as unavailable.

## Menu

- **Changed on 2026-11-19 for 1.4.0:** **Open Dashboard** (⌘D) moved to the very top. Then
  Thermals, then Keep Awake (see Keep Awake), then Power; Check for Updates… and Quit stay at
  the bottom.
- **Changed on 2026-11-19 for 1.5.0:** a **Displays** section below Power, with a brightness
  slider for each display that can be adjusted (see Displays). Mouse has nothing in the menu.
- One native menu with custom row views, Thermals first: profile buttons (3 × 2), the Manual
  slider only when Manual is chosen, then temperatures as tiles with fans in the last tile.
  **Changed on 2026-11-02 after trying 1.2.8:** one row of three profile buttons, drawn as
  tiles like the temperatures' (stock push buttons looked out of place), the chosen one in
  the accent colour. Settings chooses the three: macOS default, Balanced and Max by default.
  If the profile in use isn't one of them, none is lit; Manual's slider shows whenever
  Manual is chosen.
  Power rows below, then Open Dashboard, Check for Updates… and Quit. Release 2 has the
  temperature tiles and the new Power rows; the profile buttons and slider come with fan
  control in release 3. A mockup was approved in the planning chat: compact tiles with a
  small grey label above each value, Power rows with an SF Symbol, a grey label and the value
  on the right in tabular digits, and small grey section headers.
- Temperatures in the menu bar title keep a fixed width (figure spaces, like the watts), so
  the item doesn't jump as digits change (ThermalForge #51).
- The menu bar still shows one value. Temperature choices (hottest CPU, hottest overall) join
  "Menu bar shows"; watts stays the default.

## Keep Awake

Agreed on 2026-11-19, for 1.4.0. The maintainer keeps the screen on and unlocked at work with
Caffeine and keeps forgetting to turn it off, so it's built in, with work hours that never
wake the Mac. The name everywhere is **Keep Awake** (never "Caffeine").

- **What it holds:** IOKit's `PreventUserIdleDisplaySleep` assertion, as Caffeine does. The
  screen stays on, so the Mac doesn't idle-sleep, the screen saver doesn't start and it
  doesn't lock. Closing the lid, Apple menu › Sleep, locking by hand and macOS's near-empty
  battery sleep still work. No "Mac awake but screen may sleep" option.
- **The choices:** Off · 1 Hour · 2 Hours · Until Sleep. Fixed: no setting, no "forever".
  Every pick counts from now: the same timed pick again restarts it, and the last pick wins
  even when it's shorter; Until Sleep replaces a timer and the other way round. 1 Hour and
  2 Hours run on the wall clock (absolute end times), so sleep neither ends nor pauses them.
  Until Sleep ends at the next system sleep, however it's caused; clamshell on an external
  display isn't sleep, so it carries on.
- **Work hours:** one block on chosen weekdays: on/off, From, To, days. Off until turned on;
  Mon–Fri 9:00–17:00 by default. Local time wherever the Mac is, worked out on each tick, so
  time zone and daylight saving changes just work. The end after the start (no blocks past
  midnight), at least one day; no per-day hours, no second block. It never wakes the Mac (no
  scheduled wakes): it takes effect when the Mac is awake with its screens on, as the lid
  opens, at login, after a restart, or when it's turned on inside the block, and not in a
  dark wake (Power Nap). **Anything picked in the menu or on the page (Off, 1 Hour, 2 Hours,
  Until Sleep) on a workday, before or during that day's block, takes over from the work hours
  for the rest of that day**, across sleeps, back by itself the next workday. The pick holds on
  its own terms: 2 Hours at 8:00 ends at 10:00 and the screen goes back to normal, 1 Hour at
  10:00 ends at 11:00, 2 Hours at 16:30 runs to 18:30, Until Sleep ends at the next sleep, and
  Off at 8:00 means no work hours that day. Then there's no line, Off lit. A pick after the
  day's block has ended, on a day that isn't a workday, or while work hours are off changes
  nothing about them, and turning work hours on inside the block takes effect at once.
  (Settled in the maintainer's hands-on check on 2026-11-19, in two steps: first for picks made
  during the block, since a 1 Hour pick that changed nothing looked like the click did nothing,
  then for picks made before it. It replaces "whichever ends later", which now only applies to
  a pick from an earlier day, such as Until Sleep left running overnight, which carries past the
  block's end, or a timer from before midnight; no tile is lit while the block holds the screen
  on. `keepawake.skips_todays_hours` is the one place that decides.) Sleeping at lunch doesn't
  end the block.
- **Battery rule:** a switch, "Stop when the battery is below [20%]" (10, 20, 30, 40 or 50%),
  off by default. Only on battery: below the level, Keep Awake lets go (a pick or work hours
  alike) and says "Paused because the battery is below 20%". Plugging in brings it back if its
  time hasn't run out (timers keep counting); on the adapter it holds even below the level.
  Hidden on Macs with no battery. It uses the battery reading the sampler already takes.
- **Pauses and endings:** locking the screen, or switching to another user's session, pauses
  it; unlocking resumes it if time remains. The lock notifications are delivered at once even
  while the app isn't active, and whether the screen is locked is read again as the Mac or its
  screens wake. Whenever it lets go while the Mac is in use (a
  timer runs out, the block ends, the battery rule), it declares user activity first, so the
  screen gets macOS's full display-sleep time from then instead of dimming and locking at
  once.
- **The updater's relaunch** (it installs after 10 s idle, so it can come mid-day): a running
  pick and today's Off come back in the new copy with the same end times, and the relaunch never
  lets the screen go dark. The old copy declares user activity, but that ends with the old
  process (it's a `UserIsActive` assertion of its own), so while it holds the screen on it also
  starts `/usr/bin/caffeinate -d -t N` on its own, N being until whatever holds the screen would
  end, at most 10 minutes. The new copy ends it (only if that pid is still caffeinate) once it
  holds the screen on itself; if it never starts, caffeinate ends by itself. Only that path
  writes a handover file (`keep-awake-handover.json` in the data folder), and only with a pick,
  today's Off or caffeinate to pass on. The new copy reads it once and deletes it, ignoring one
  older than 10 minutes (the copy an update starts has never run, and one sat at exec for 224 s
  once). A quit by the user, a restart or a logout brings back nothing but work hours.
- **Menu:** a section header, then the choices as tiles drawn like the fan profile buttons, in
  two rows (**1 Hour · 2 Hours**, then **Off · Until Sleep**), the active one in the accent
  colour, and one line under them, with no full stop (the maintainer's wording, 2026-11-19):
  "Keep Awake ends at 1:22 PM (59 minutes)" (a timer, with the time left: "N hours N minutes",
  zero parts left out, rounded up so it never says 0; it counts down as the menu and the page
  refresh), "Keep Awake ends at 5 PM (4 hours 38 minutes)" (work hours, no tile lit),
  "Keep Awake ends when the Mac sleeps", "Paused because the battery is below 20%", or no line
  while nothing holds the screen on, Off for the rest of today's work hours included (Off lit).
  Times follow the Mac's 12/24-hour setting, except that on a 12-hour Mac whole hours drop
  ":00" ("1 PM", "5 PM"), as the bar's labels do, and 12 o'clock is always **Noon** or
  **Midnight**, never 12 AM or 12 PM; other times keep their minutes ("1:07 PM"). On a 24-hour
  Mac it's the Mac's own format, and a block ending at midnight ends at 24:00. The line is
  built in one place (`keepawake.sentence`, with `keepawake.short_clock` and `clock_label` for
  the times, `keepawake.time_left` for the time left) for the menu and the page. It replaced
  the first wording ("Screen stays on for 42 more minutes.") after the hands-on check, and the
  time left was added after the maintainer checked the next build.
- **Changed on 2026-11-19 after the maintainer tried it:** the steaming cup and the work-hours
  bar below, in place of From/To time fields.
- **Menu bar:** while Keep Awake holds the screen on (work hours included), a steaming cup
  (`cup.and.heat.waves.fill`, macOS 14 and later; `cup.and.saucer.fill` before) follows the reading, with the charging bolt still in front:
  "⚡ 12.3 W ☕", or "12.3 W ☕".
- **Page:** a **Keep Awake** tab between Thermals and Power, with three cards: the four
  choices (Off · 1 Hour · 2 Hours · Until Sleep) drawn like the Thermals page's fan profiles,
  with the same sentence under them; Work hours (the switch, a 24-hour bar, and seven day buttons
  starting on the Mac's first weekday, editable while it's off); and Battery (hidden on a Mac
  with no battery). Nothing about it in Settings, and no Dashboard row.
- **The work-hours bar:** a track as wide as the card, the block in the accent colour with a
  handle at each end. Dragging an end resizes it, dragging the middle moves it at the same
  length, and a click on the empty track takes the nearer end there. At least 30 minutes, from
  0:00 to at most 24:00 (Midnight; `"24:00"` is allowed for the end only), never past
  midnight. While dragging, the end follows the pointer exactly, a label above each moving end
  shows the half hour it will land on, and the line above the bar shows the snapped times ("9:00
  AM to 5:00 PM · 8 hours", or "9:00 to 17:00 · 8 hours" on a 24-hour Mac). On release the ends
  snap to the nearest half hour with a 120 ms transition, and the settings are saved once. A tick
  every hour, taller every 3 hours, labelled Midnight · 3 AM · 6 AM · 9 AM · Noon · 3 PM · 6 PM ·
  9 PM · Midnight (the times themselves, 0:00 to 24:00, on a 24-hour Mac). The labels come from
  Python (`half_hour_labels`, `bar_labels`) so they match the menu. Mouse only: no keyboard, by
  the maintainer's decision. Polls never redraw the bar under the pointer.
- **Cost:** no timers or wakeups of its own: it's evaluated on the sampler's ticks and on
  notifications. Holding the screen on doesn't raise the app's priority.

## Fan control

- The fan curve runs in the app, once a second.
- The root helper is a small **Swift** program, mostly ThermalForge's tested SMC and fan
  unlock code. Not Python: the app bundle is writable by the user, and Python reads its code
  with ordinary file reads that nothing re-checks, so a Python helper would hand root to
  anything running as the user.
- It accepts four commands over a private socket that launchd opens for it: set speeds
  (clamped to each fan's minimum and maximum), hand back to macOS, check-ins and status. It
  starts on the first connection, exits once the fans are back with macOS, and checks that
  the caller is the signed app.
- Safety nets live in the helper: back to macOS on quit, crash (missed check-ins) and sleep;
  the profile comes back on wake; and no writes while another app is controlling the fans.
- **The safety override** (forced full speed while a profile or Manual holds the fans low) is
  not a fixed 95°: on M4, hotspots legitimately read 96–113° while macOS is calm, and a 95° rule
  overrode every hold (ThermalForge #68). It follows macOS's thermal pressure (Serious or
  Critical at once, Fair after 30 seconds), backed up per kind of sensor: hotspots at 118° for
  10 seconds judged by the *second*-highest reading (one bad sensor can't trip it or block a
  clear), chassis (`TS0P`) at 95° for 30 seconds, battery (`TB0T`) at 50° for 60 seconds.
- From ThermalForge's issues and pull requests:
  - Hand the fans back only after the temperature has stayed below Fans stop for 5 seconds,
    holding minimum speed until then. ThermalForge's instant hand-back cycled the fans 2,182
    times in 3 days (#73).
  - Handing back checks every write and tries every fan. A fan whose mode write failed keeps
    its last speed, never a target of 0 in manual mode, which stops it (#72, #30).
  - After handing back, read the fan mode for up to 10 seconds and repeat the hand-back while
    any fan still reads manual. On quit, let any pending fan write finish before the final
    hand-back (#76, #77).
  - After wake, start from the current temperatures; never replay an old speed (#48).
  - The first fan unlock on M1–M4 can take several seconds; don't time it out (#69).
  - Jump straight to the fan's minimum when fans start, at most two fan writes a second, and
    smooth Smart's temperature-trend input (#37).
  - Every socket sets `SO_NOSIGPIPE`, or a dropped connection kills the helper (#71).
- Installed with `SMAppService` (one Allow and password in System Settings), proven first
  with a throwaway app on the maintainer's Mac. If that fails without notarization, fall back
  to a one-time admin-password install into a root-owned folder.
- **The throwaway test passed** on the M1 Pro, macOS 27.2, on 2026-11-02, with an app and
  helper signed with the Apple Development certificate and not notarized:
  - `register()` reports "Operation not permitted" with status *requires approval*, as
    documented; after one Allow and password in Login Items & Extensions it's *enabled*.
  - launchd starts the helper as root on the first connection to its socket
    (`SockPathMode` 0666 in `/var/run`), it exits when idle, and the next connection starts it
    again. It still started after the whole bundle was given a browser's quarantine flag.
  - The caller check (the socket's `LOCAL_PEERTOKEN` audit token, checked against
    `identifier "…" and anchor apple generic and certificate leaf[subject.OU] = "<the
    helper's own team>"`) let the signed app in and refused a plain `python3`.
  - This Mac has **no `Ftst`** (not even listed), yet writing `F0Md`/`F1Md` = 1 as root
    worked on the first try, and the fans followed `F0Tg`/`F1Tg` from 0 to 2,000 rpm in about
    4 seconds. The mode reads back as 3 for up to a second after the write before showing 1.
  - **Found on 2026-11-03 with 1.2.11:** the maintainer's M4 MacBook Pro refused every mode
    write for 30 seconds ("Fan control stopped because the fan helper reported an error
    (unlock)"). Leaving out ThermalForge's `Ftst` step, because the M1 Pro has no `Ftst`,
    broke the M4, which does. The helper now sets `Ftst` = 1 before the modes and back to 0
    after them, where the key exists.
  - Handing back is `F%dMd` = 0: it reads 0 at once, the fans stopped, and macOS set the
    mode back to 3 (status 3) within about 20 seconds by itself.
  - Fan keys seen here besides the known ones: `F0Dc`, `F0Fb`, `F0Fc`, `F0Sf`, `F0St`
    (3 under macOS, 2 just after handing back), `FBAD`, `FOFC`, `FOff`, `FRmp`.
- Found while building it:
  - A daemon that was never registered reports *not found*, not *not registered*, so the
    app registers on either the first time.
  - `SMAppService.status()` once took 8 seconds, while `backgroundtaskmanagementd` was busy
    right after an approval; the app only asks while waiting for approval, and when the
    helper can't be reached.
  - "Minimum speed" puts every fan at its own minimum: a curve's fraction is of each fan's
    maximum, never below its minimum, and starts from the slowest fan's minimum.
  - A profile chosen but with the fans still with macOS samples once a second at
    background QoS; holding the fans raises the sampler and the fan thread to utility.
- Set up only the first time a profile other than macOS default is chosen. Until it's
  approved, the fans stay with macOS and the Thermals page says how to finish.
- Updates aren't held back by a running profile: the fans go back to macOS for the few
  seconds of the relaunch, and the profile comes back afterwards.

## Displays

Agreed on 2026-11-19, for 1.5.0. It replaces what the maintainer uses BetterDisplay for:
brightness on the 16" M1 Pro's XDR screen up to its HDR peak, and the LG TV's own brightness
and volume.

- **One slider per display, each on its own.** A slider sets only its own display's backlight.
  The built-in display uses DisplayServices (`DisplayServicesGetBrightness` and
  `DisplayServicesSetBrightness`, private, still there on 27.2.1; MonitorControl and Lunar use
  them). External displays and TVs use DDC/CI (VCP 0x10). There's no software dimming
  anywhere: on the TV a gamma table would also wash out its HDR. No slider combines hardware
  and software dimming, and nothing links displays together. A display that doesn't answer
  DDC gets no slider.
- **The built-in display goes past 100%.** Its scale runs from 0 to 160%. 0 to 100% is macOS's
  own brightness, and 100 to 160% is the boost (the level saved as `display_boost`, 1.0 to
  1.6). 160% is the panel's 1,600-nit HDR peak. Its rated full-screen brightness is 1,000 nits,
  about 132%, so above that macOS may pull it back after a while. The boost only holds while
  macOS's own brightness is at its maximum: anything that lowers it (Control Center,
  auto-brightness) ends the boost.
- **Which Macs get the boost.** Only the built-in XDR display gets it; the TV's own HDR isn't
  boosted. It also needs gamma tables to work:
  - M5 Pro, M5 Max and MacBook Neo have ignored gamma tables since macOS 26.3, and still do on
    27.2.1, on built-in and external displays alike, so it isn't their panels. Apple's forum
    thread 819331 has the reports, and FB22273730 is open. Their slider stops at 100% until
    Apple fixes it, using a model list like `power.NO_CHIP_SENSOR`.
  - The base M5 works.
  - A Mac without an XDR screen stops at 100% too.
- **How the boost works.** This is BrightIntosh's method; BrightIntosh is GPL-3.0, so it's
  evidence only, not code to copy.
  - A 1×1 window above everything holds a Metal view with EDR content (`rgba16Float`,
    `extendedLinearSRGB`, `wantsExtendedDynamicRangeContent`). That puts the screen in EDR
    mode.
  - The display's gamma table then lifts everything into the headroom: read it with
    `CGGetDisplayTransferByTable`, multiply it, and write it back with
    `CGSetDisplayTransferByTable`.
  - The strength follows the headroom macOS grants at the moment
    (`NSScreen.maximumExtendedDynamicRangeColorComponentValue`).
  - When the app quits or crashes, macOS puts the user's ColorSync tables back.
  - Whether the trigger window has to keep redrawing is still to be measured (BrightIntosh
    redraws at 5 fps). Either way it runs in Swift (see Shared groundwork), only while above
    100%, and it pauses while the screen sleeps.
  - macOS 26.3 blocked the preset-unlocking methods BetterDisplay and Lunar used before, so
    don't use those.
- **Trade-offs accepted:** real HDR video looks blown out while boosted, and screenshots don't
  show the boost. On the 27 beta, full-screen video was reported to lose the boost after about
  10 s.
- **Remembered:** the boost survives sleep, lock and relaunch, like macOS's own brightness.
  There's no battery rule, and macOS's own heat and power limits stay in charge. Over an
  update's relaunch it drops to 100% for a few seconds, then comes back; there's no handover.
- **The keys.** A **Brightness and volume keys** switch on the Displays tab, off until turned
  on. Turning it on asks for Accessibility (see Shared groundwork). While it's on:
  - **Brightness keys** adjust the display the pointer is on, or the built-in display when
    that one can't be adjusted. Each press is 10%, and ⌥⇧ gives quarter steps (2.5%), as
    macOS does.
  - **The app's own HUD** replaces macOS's at every step. It looks like the maintainer's
    screenshot of BetterDisplay's: a sun symbol, a bar of 16 segments with 100% at the 10th,
    and a bubble over the end of the lit part saying e.g. "130% · 960 nits". It appears on the
    display being adjusted, where macOS puts its own, and fades after about 1.5 s.
  - **Nits are calculated, not read from a sensor.** At 100% and above they're 500 × (percent
    / 100)^2.5, which matches BetterDisplay's 960 at 130% and 1,600 at 160%. Below 100%
    they're 500 × macOS's linear brightness (`DisplayServicesGetLinearBrightness`). When macOS
    grants less headroom than asked for, the bubble shows what's actually reached.
  - **A TV's HUD** has 10 segments from 0 to 100% and no nits.
  - **Volume and mute keys** control a display's speakers over DDC (VCP 0x62 for volume, 0x8D
    for mute) when it's macOS's sound output and answers DDC. They get the same HUD with a
    speaker symbol, in macOS's 16 volume steps. Otherwise the keys are macOS's.

  While the switch is off, the keys belong to macOS and only the menu's slider reaches the
  boost.
- **The menu:** a Displays section below Power.
  - One slider for each display that can be adjusted, labelled with macOS's name for it
    (`NSScreen.localizedName`, and "Built-in Display" for the laptop's own), with the percent
    on the right.
  - The built-in display's slider runs from 0 to 160%, with a tick at 100%.
  - No volume in the menu.
- **The Displays tab** (after Power):
  - A card for each connected display, with its brightness slider and, where DDC allows, a
    volume slider.
  - The keys switch, with whether Accessibility is allowed.
- **DDC** (m1ddc's method; m1ddc is MIT):
  - Find each `DCPAVServiceProxy` whose `Location` is "External", then use
    `IOAVServiceCreateWithService` and `IOAVServiceWriteI2C`/`IOAVServiceReadI2C`, at chip
    address 0x37 and data address 0x51.
  - Use chip address 0xB7 when the proxy's parent's `EPICProviderClass` is `AppleMCDP29XX`.
    That's the HDMI chip, and this Mac has it. m1ddc tested that only on an M1 Mac mini, and
    MonitorControl says DDC doesn't work over any M1 Mac's HDMI port.
  - Nobody has confirmed DDC on an LG TV. BetterDisplay controls LG TVs over the network,
    which this app won't do: the updater is its only network access. If the TV doesn't answer
    on the HDMI port, try a USB-C to HDMI adapter.
  - The maintainer can only plug the TV in later.
- **No idle cost:**
  - DDC is only used on a change, or when the menu or the tab opens. The app remembers what
    it set.
  - There's no event tap unless the keys switch is on.
  - The trigger window only runs above 100%.
- **Never read identifying keys.** Don't read EDID serial numbers; names come from
  `NSScreen.localizedName`.

## Mouse

Agreed on 2026-11-19, for 1.5.0. It replaces LinearMouse, which the maintainer uses to reverse
scrolling on two wheel mice (an MX Vertical and a Keychron M5 8K) while the trackpad keeps
natural scrolling. macOS 27.2 still has one natural-scrolling setting for mice and trackpads
(`com.apple.swipescrolldirection`), so this needs an event tap.

- **Three switches on the Mouse tab, all off until turned on:**
  - **Reverse scrolling:** vertical scrolling on every wheel mouse goes the other way, and
    horizontal is left as it is. The trackpad and a Magic Mouse (which scrolls like a
    trackpad) are left alone. There's no list of devices.
  - **Smooth scrolling:** each notch of the wheel becomes a short glide of pixel scrolling. It
    has one fixed feel with no tuning, applies to every wheel mouse, and works with or without
    reversing. Mos is CC BY-NC, which isn't GPL-compatible, so don't use its code.
  - **Back and forward buttons:** the side buttons (buttons 4 and 5) become Back and Forward
    everywhere, including Safari and Finder, which ignore them today. Every other button stays
    as it is.
- **Pointer acceleration isn't the app's.** System Settings › Mouse › Advanced has a switch for
  it (since macOS 14, still there on 27, mice only). The Mouse tab has a **Pointer
  acceleration…** link that opens that pane
  (`x-apple.systempreferences:com.apple.Mouse-Settings.extension`), like Power's link to the
  battery's charging settings.
- **Telling a mouse from the trackpad:**
  - Go by the device that sent each event. Guessing from scroll phases fails on a Magic Mouse,
    and it flipped trackpads mid-scroll in Scroll Reverser #130.
  - LinearMouse (MIT) reads the HID sender ID from the event (`CGEventCopyIOHIDEvent`,
    `IOHIDEventGetSenderID`) and matches it to the device.
  - Scroll Reverser (Apache-2.0) looks the device up in the IORegistry. It treats a device as a
    trackpad when its `HIDScrollAccelerationType` is `HIDTrackpadScrollAcceleration`, or when
    it's on Apple's multitouch driver.
  - Flip every event from the same sender, so momentum phases stay consistent.
  - Set `DeltaAxis1` first, because macOS recomputes the point deltas from it. Then set the
    fixed-point and point deltas, and the embedded HID event's.
  - Leave alone the events another app posts (Logi Options+, for example).
  - **Never read a device's serial number.**
- **The Mouse tab** (after Displays):
  - The three switches and the link.
  - Whether Accessibility is allowed, with a button that opens Privacy & Security ›
    Accessibility.
  - Nothing in the menu.
- **No idle cost:** there's no tap at all while all three switches are off. The tap listens
  only for scroll events and the other buttons' events, never for mouse movement.

## Shared groundwork

Displays and Mouse are built at the same time by two sessions, each in its own worktree with
its own branch and pull request. Both need Swift event taps and Accessibility, so the pieces
they share are specified here and both build to them. Whichever merges second takes the other's
copy of these pieces.

- **Swift, in the app's own process.** An event tap sits in the path of every input event, so
  Python can't run one. The app's threads share the GIL and sit at background QoS while idle
  (a starved one once waited 26 s), and macOS disables a tap that stalls.
  - Each tool has a Swift dylib, loaded with ctypes:
    - `Native/Displays/*.swift` → `Contents/Frameworks/libdisplays.dylib`.
    - `Native/Mouse/*.swift` → `Contents/Frameworks/libmouse.dylib`.
  - Each dylib exports plain C functions (`@_cdecl`).
  - A tap runs on a thread of its own, with its own run loop, at user-interactive QoS all the
    time. It isn't a GIL thread, so *all three always have the same priority* doesn't cover it.
  - **No Python runs on a tap thread.** Python turns things on and off and sets values.
    Anything Python must hear about, such as a key press for the HUD, goes to the main queue
    with `DispatchQueue.main.async`.
- **Build.**
  - `scripts/build.sh` compiles each `Native/<Tool>/` folder after PyInstaller, alongside the
    fan helper. It uses the same `xcrun swiftc -O -target arm64-apple-macos13.0` plus
    `-emit-library`, writes `Contents/Frameworks/lib<tool>.dylib` (lower case), and runs
    before signing.
  - Without a Swift compiler there are no dylibs, and the switches that need them are hidden,
    as fan control is.
  - Tests compile the dylibs with `swiftc`, like `tests/test_fan_helper.py`, and are skipped
    without it.
- **`Application Files/accessibility.py`**, which both tools use:
  - `allowed()` calls `AXIsProcessTrusted()`.
  - `ask()` calls `AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})`. It
    runs the first time a switch that needs a tap is turned on. That's macOS's own prompt, and
    it also adds the app to the list.
  - `open_settings()` opens
    `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`.
  - `watch(callback)` calls back on the main thread when the grant may have changed. It
    listens for the `com.apple.accessibility.api` distributed notification and reads the grant
    again after a short delay, since the value lags behind the notification. So a tap starts
    as soon as it's allowed, without a relaunch, and stops if the grant is taken away.
  - `register(stop, start)`, `stop_taps()` and `start_taps()`: **the updater calls
    `stop_taps()` just before it swaps the bundle** (in `Updater._install`, before
    `replaceItemAtURL…`), and `start_taps()` if the swap fails. On macOS 27.2, a bundle
    replaced under a running tap made tccd refuse the old process, and the keyboard and
    trackpad froze for about two minutes (Scroll Reverser #220).
- **Taps:**
  - Create one only when Accessibility is allowed.
  - Re-enable it on `kCGEventTapDisabledByTimeout` and `kCGEventTapDisabledByUserInput`.
  - Call `CFMachPortInvalidate` on every tap that's dropped. Left-over taps slow down all
    input (Mos #970).
- **The grant:** TCC keys it to the designated requirement, which pins the Apple Development
  certificate's name, so updates signed with the same certificate keep it. A renewed
  certificate (this one expires 2027-10-05) probably keeps it too, but that's unverified.
- **Settings:** new keys, each validated in `settings.py`:
  - `display_keys`: false.
  - `display_boost`: 1.0 to 1.6, default 1.0.
  - `mouse_reverse_scrolling`, `mouse_smooth_scrolling` and `mouse_back_forward`: all false.
- **Testing:**
  - A test copy (with its own bundle identifier) needs its own Accessibility allowance. That's
    the maintainer's click, so ask first.
  - Quit LinearMouse and BetterDisplay before any hands-on test. Two apps reversing cancel
    each other out, and two apps can't both own the keys. The maintainer is deleting both once
    this is ready to test.
  - Say so before anything appears on screen, and before the screen's brightness changes.
  - Each new state goes into `tools/preview/stub.js` and `tools/render_menu.py`.
  - New log lines go in CLAUDE.md's Debugging section.
  - Code adapted from LinearMouse, Scroll Reverser or m1ddc gets its notice in
    `THIRD-PARTY-NOTICES`.

## Left out on purpose

From ThermalForge's issues, decided against on 2026-11-02: a timer back to macOS default
(#17), showing thermal pressure, a separate full-speed button (#21), switching to Low Power
Mode when hot (#41, #42), custom or two-sensor curves (#52), Extra Cool (#15), a Smart
settings file (#64), per-core lists and small trend charts (#25), and hiding profiles or menu
items (#46). ThermalForge's command-line tool, installers and spike logging don't apply.

For Displays and Mouse, decided against on 2026-11-19:
- From BetterDisplay: software dimming, sliders that combine hardware and software dimming,
  linking displays together, controlling an LG TV over the network, resolution and HiDPI, and
  boosting an external display's HDR.
- From LinearMouse: per-device settings, tuning for smooth scrolling, button mappings beyond
  Back and Forward, and pointer acceleration inside the app.
- A note when another scrolling or display app is running: the maintainer is deleting
  LinearMouse and BetterDisplay.

## Rules that carry over

Every key rule in CLAUDE.md applies to the new tool: the sampling cadence and QoS, nothing
animating, only real changes passed to AppKit and WebKit, the updater as the only network
access, and every SMC key optional.

## Credit

ThermalForge is MIT-licensed. Its notice goes in `THIRD-PARTY-NOTICES`, with a credit line in
the README.
