# Mac Engine Room

A macOS menu bar app with two tools. **Thermals** shows how hot your Mac's CPU, GPU,
memory and SSD are, and runs its fans the way you choose. **Power** shows where its power
goes: what the charger delivers, what the Mac uses, and what goes into or out of the
battery, with a history of every charge.

Formerly **Pythonflow**. Installed copies rename themselves when they update, and keep
your history and settings.

A Python rewrite of [Powerflow](https://github.com/lzt1008/powerflow), which crashes on
macOS 27 and is no longer maintained.

![Mac Engine Room dashboard](docs/screenshot.png)

**[⬇︎ Download the latest version](https://github.com/DigitalN/mac-engine-room/releases/latest)**
(free, for Macs with Apple silicon)

## Features

- **Menu bar reading**: the CPU's temperature, the hottest temperature, or system,
  screen or chip power, or the charger's input while charging. Click it for the fan
  profiles and a quick summary of temperatures, fans and power.
- **Dashboard**: the window opens on a short page with a row of readings per tool:
  CPU, GPU and fans, then system power, battery and charger.
- **Temperatures** of the CPU and GPU (their hottest core), memory, SSD and ambient
  air, with a live chart of the last 1, 5 or 15 minutes. In Celsius, or Fahrenheit if
  you choose it in Settings.
- **Fans**: each fan's speed, its range, and who's in control. Macs without fans show
  temperatures only.
- **Fan control**: leave the fans to macOS, or choose a profile that starts them sooner
  and runs them faster (see [Fan profiles](#fan-profiles)), or set them by hand. Each
  profile's temperatures and top speed can be tuned in Settings.
- **Power flow** from charger to Mac to battery, with the charger's conversion loss and
  where the Mac's power goes.
- **Live power chart** of the last 1, 5 or 15 minutes.
- **Battery and charger details**: battery health, cycle count, temperature, voltage and
  current, and the charger's rating and negotiated voltage.
- **Charging history**: each charge's levels, duration and power curve, exportable to CSV.
- Works with macOS's charge limit: the time while charging counts to the limit, and
  Settings has a shortcut to where it's set.
- Opens at login, straight to the menu bar, and keeps itself up to date. Both can be
  turned off in Settings.

## Install

1. **Download** `Mac-Engine-Room-….dmg` from the
   [latest release](https://github.com/DigitalN/mac-engine-room/releases/latest) and open it.
2. **Drag Mac Engine Room into Applications**, as the arrow shows.
3. **Open Mac Engine Room** from Applications. Its wattage appears in the menu bar; click
   it and choose **Open Dashboard** for the full view.

The first time you open it, macOS may say it can't verify Mac Engine Room. Click
**Done**, then go to **System Settings → Privacy & Security**, scroll down, and click
**Open Anyway**. You only do this once: Mac Engine Room isn't distributed through Apple, so Apple
hasn't checked it. All of its code is in this repository.

The first time you choose a fan profile, macOS asks you to allow Mac Engine Room's fan
helper: in **System Settings → General → Login Items & Extensions**, turn on Mac Engine
Room under **Allow in the Background** and enter your password. Mac Engine Room shows how,
and the fans stay with macOS until then.

Tested on macOS 27.0 with Apple silicon. Fan control needs macOS 13 or later. To build
from source, see [DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Privacy

- **The only connection is the update check**: once a day, after your Mac wakes, it asks
  GitHub for the latest release. It only installs updates signed by the same developer.
  No analytics, crash reports or web server.
- **It reads power and temperature sensors only**, never serial numbers or your
  computer's name.
- **Fan control uses a small helper that runs as an administrator**, because only an
  administrator can set fan speeds. It's only installed when you choose a fan profile and
  allow it. All it can do is set fan speeds within each fan's range, give the fans back to
  macOS, and report on them, and it only answers Mac Engine Room. It gives the fans back
  to macOS if Mac Engine Room quits or stops responding, and before your Mac sleeps.
- **Your data stays on your Mac**, in `~/Library/Application Support/Mac Engine Room`.
  You can turn off history recording or delete it in Settings.

## What the numbers mean

| Reading | What it is |
|---|---|
| **System** | Total power the Mac is using |
| **Adapter in** | Power from the charger into the Mac. The Adapter box adds the charger's conversion loss. |
| **Battery** | Power into the battery (positive) or out of it (negative) |
| **Screen** | Display backlight. On mini-LED screens it rises with bright content. Not shown on a Mac with no screen sensor. |
| **Chip (CPU + GPU)** | The Apple silicon chip. Apple names the sensor after the heatpipe, but it measures the chip. |
| **Time to full / remaining** | macOS's own estimate, rough above about 80%, where charging slows down. With a charge limit set, the time to the limit instead, since macOS's estimate counts to 100%. |
| **CPU, GPU** | The hottest of the chip's per-core sensors. Not its hotspot sensors, which run up to 10° hotter, and not the battery sensors some apps mistake for the GPU. |
| **Memory, SSD, Ambient** | The hottest sensor in each group. Ambient is the Mac's air temperature sensor. |

## Fan profiles

The profiles follow the hottest CPU or GPU core. Each waits a few seconds above Fans start
before taking the fans, so a short spike doesn't, and gives them back to macOS once the
cores have been below Fans stop for 5 seconds.

| Profile | Fans start | Full speed at | Top speed | Fans stop |
|---|---|---|---|---|
| **macOS default** | macOS decides | | | |
| **Balanced** | 55° | 70°, quiet low down | 60% | 50° |
| **Performance** | 55° | 65° | 85% | 50° |
| **Smart** | 53° | 85°, sooner while it's heating up | 100% | 50° |
| **Max** | 65°, at once to full speed | | 100% | 50° |
| **Manual** | One slider for every fan, from its slowest to its fastest | | | |

Whatever the profile, the fans go to full speed if macOS reports serious thermal pressure,
or the chip, case or battery gets too hot.

## Coming from Pythonflow

Nothing to do: Pythonflow updates itself into Mac Engine Room, renames its app and data
folders, and stays open at login if it was. If you install Mac Engine Room by hand
instead, opening it quits Pythonflow, moves it to the Trash and takes over its history
and settings.

## Coming from Powerflow

It fixes the original's problems on macOS 27: the crash at launch, blank battery
health, "charging" at 100%, "1092 hours" remaining, the menu bar panel that wouldn't open,
and the chart freezing after an upgrade.

On first launch it copies over your charging history and preferences, reading the old
database without changing it. You can then delete `/Applications/powerflow.app`.

## Credits

Based on [Powerflow](https://github.com/lzt1008/powerflow) by The Powerflow Team
(Samuel Lyon / lzt1008). The macOS 27 fixes build on diagnoses from community pull
requests [#19](https://github.com/lzt1008/powerflow/pull/19),
[#21](https://github.com/lzt1008/powerflow/pull/21) and
[#22](https://github.com/lzt1008/powerflow/pull/22).

The per-chip list of CPU and GPU core sensors comes from
[Stats](https://github.com/exelban/stats) by Serhiy Mytrovtsiy (MIT).

Fan control is based on [ThermalForge](https://github.com/ProducerGuy/ThermalForge) by
ProducerGuy (MIT): its fan profiles, and the fan and SMC code, which it adapted from
[macos-smc-fan](https://github.com/agoodkind/macos-smc-fan) by Alexander Goodkind (MIT).
Their notices are in [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES).

## License

MIT. See [LICENSE](LICENSE).
