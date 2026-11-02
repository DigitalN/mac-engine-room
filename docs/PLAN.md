# Mac Engine Room: the plan

Pythonflow becomes **Mac Engine Room**, an app with two tools: **Thermals**, which replaces
[ThermalForge](https://github.com/ProducerGuy/ThermalForge), and **Power**, which is today's
app. Agreed with the maintainer on 2026-11-02. Each release ticks its part off here.

## Releases

Each is the usual +0.0.1 with the whole release checklist in DEVELOPMENT.md.

1. **The rename and the folder move.**
2. **The tool framework, the Thermals page with temperatures, and the new menu.**
3. **Fan control**, starting with a throwaway test of the root helper.

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
  Now and History pages. Settings is shared, with a section for each tool.
- Each tool is its own Python module that adds its readings, its menu section and its
  settings. No plugin system beyond that.

## Thermals

- CPU, GPU, memory, SSD and ambient temperatures (the hottest sensor in each group), each
  fan's RPM and mode, and a rolling chart like Power's. No temperature history on disk.
- Units follow macOS (System Settings > General > Language & Region). No setting in the app.
- Temperatures are only read while something needs them: the page is showing, the menu is
  open, the menu bar shows a temperature, or a profile is running.
- Profiles: macOS default, Balanced, Performance, Smart and Max with ThermalForge's numbers,
  plus **Manual**, one slider from minimum to maximum RPM for all fans. Starts on macOS
  default; the choice is remembered across relaunches and logins.
- Settings > Thermals tunes each profile: **Fans start at** (50–85°), **Full speed at**
  (at least 5° above start, at most 94°) and **Top speed** (30–100%). **Fans stop at** follows
  Fans start, keeping the profile's own gap. Max only has Fans start. Each profile has Reset.
  Smart's rate boost, trigger delays, ramp rates and the 95 °C override stay fixed.
- Defaults: Balanced 55° to 70° at 60%, Performance 55° to 65° at 85%, Smart 53° to 85° at
  100%, Max from 65°.
- Macs without fans show temperatures only. Desktops get everything. Where the helper can't
  be approved (for example a managed work Mac), fan control shows as unavailable.

## Menu

- One native menu with custom row views, Thermals first: profile buttons (3 × 2), the Manual
  slider only when Manual is chosen, then temperatures as tiles with fans in the last tile.
  Power rows below, then Open Dashboard, Check for Updates… and Quit.
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
  the profile comes back on wake; forced full speed at 95 °C while a profile or Manual holds
  the fans low; and no writes while another app is controlling the fans.
- Installed with `SMAppService` (one Allow and password in System Settings), proven first
  with a throwaway app on the maintainer's Mac. If that fails without notarization, fall back
  to a one-time admin-password install into a root-owned folder.
- Set up only the first time a profile other than macOS default is chosen. Until it's
  approved, the fans stay with macOS and the Thermals page says how to finish.
- Updates aren't held back by a running profile: the fans go back to macOS for the few
  seconds of the relaunch, and the profile comes back afterwards.

## Rules that carry over

Every key rule in CLAUDE.md applies to the new tool: the sampling cadence and QoS, nothing
animating, only real changes passed to AppKit and WebKit, the updater as the only network
access, and every SMC key optional.

## Credit

ThermalForge is MIT-licensed. Its notice goes in `THIRD-PARTY-NOTICES`, with a credit line in
the README.
