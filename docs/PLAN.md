# Mac Engine Room: the plan

Pythonflow becomes **Mac Engine Room**, an app with two tools: **Thermals**, which replaces
[ThermalForge](https://github.com/ProducerGuy/ThermalForge), and **Power**, which is today's
app. Agreed with the maintainer on 2026-11-02. Each release ticks its part off here.

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
- **Not counted as CPU:** the chip hotspot keys (`TCDX`, `TCMz` and the like), which run up to
  ~10° above any core, and the helper `Tp` keys next to each core key (on M4 one key in four is
  the core; others read 0 while the core sleeps and 10–13° hot under load). Hotspots are used
  only by the safety override. On this M1 Pro `TCMb` turned out to equal the hottest CPU or GPU
  core exactly, idle and under either load.
- **Not counted as GPU:** `TG0B`, `TG0C`, `TG0H`, `TG0V`, `TG1B`, `TG2B`. They're the battery's
  gas-gauge sensors (on this M1 Pro `TG0B` reads exactly `TB0T`, the battery). GPU is `Tg…`.
- Readings of 0, or outside 1–130°, are ignored (`TH1a`/`TH1b` read 0 here).
- Apple silicon SMC values are little-endian `flt`; M5 and later also use 8-byte `ioft`
  (16.16 fixed point in the first 4 bytes, little-endian), which `power.decode_smc_value`
  decodes. This M1 Pro has a few `ioft` keys too: the `TG…` battery keys and `TR0Z`/`TR1d`/`TR2d`.
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
- Macs without fans show temperatures only. Desktops get everything. Where the helper can't
  be approved (for example a managed work Mac), fan control shows as unavailable.

## Menu

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

## Left out on purpose

From ThermalForge's issues, decided against on 2026-11-02: a timer back to macOS default
(#17), showing thermal pressure, a separate full-speed button (#21), switching to Low Power
Mode when hot (#41, #42), custom or two-sensor curves (#52), Extra Cool (#15), a Smart
settings file (#64), per-core lists and small trend charts (#25), and hiding profiles or menu
items (#46). ThermalForge's command-line tool, installers and spike logging don't apply.

## Rules that carry over

Every key rule in CLAUDE.md applies to the new tool: the sampling cadence and QoS, nothing
animating, only real changes passed to AppKit and WebKit, the updater as the only network
access, and every SMC key optional.

## Credit

ThermalForge is MIT-licensed. Its notice goes in `THIRD-PARTY-NOTICES`, with a credit line in
the README.
