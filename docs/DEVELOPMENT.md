# Developing Mac Engine Room

Notes for building Mac Engine Room (formerly Pythonflow) from source and publishing
releases. For installing and using it, see the [README](../README.md). The architecture
and the rules the code follows are in [CLAUDE.md](../CLAUDE.md).

## Build from source

You need Python 3 from [python.org](https://www.python.org/downloads/) and the pinned
packages in `Application Files/requirements.txt` (`scripts/First Time Setup.command`
installs them). Fan control also needs a Swift compiler, from Xcode or its Command Line
Tools (`xcode-select --install`); without one the build has no fan control. Then:

```bash
scripts/install.sh
```

This builds `Mac Engine Room.app`, signs it with your **Apple Development** certificate,
installs it to `/Applications` and launches it. `scripts/Build Mac Engine Room.command` does
the same after pulling the latest code and running the tests.

Without a certificate the build is signed ad hoc: it works, but it can't update itself.
A free Apple ID is enough to get one: Xcode → Settings → Apple Accounts → sign in →
select your **Personal Team** → Manage Certificates… → **+** → **Apple Development**.

Builds go to `~/Library/Caches/MacEngineRoom-build`, not the repository. Folders synced by
iCloud Drive (such as `~/Documents`) add Finder metadata to app bundles, and codesign
rejects bundles that have it.

To run from source without building:

```bash
cd "Application Files" && python3 main.py
```

Set `PYTHONFLOW_DATA_DIR=/some/folder` to run against a throwaway data folder. Without
it, a run from source moves `~/Library/Application Support/Pythonflow` to the new name
(`rename.py`), or waits for a running Pythonflow and gives up. Run the tests with
`python3 -m pytest`.

To look at the page or the menu without running the app:

```bash
python3 tools/preview/serve.py
```

serves the page at `http://127.0.0.1:8765/` with made-up readings; add
`?s=holding,fahrenheit&tab=thermals` for other states (listed at the top of
`tools/preview/stub.js`). `python3 tools/render_menu.py /tmp/menu --state holding` draws the
menu to `/tmp/menu-light.png` and `/tmp/menu-dark.png` without showing it.

## Release checklist

Go through all of it for every release. Releases are public, and installed copies update
themselves from the latest one, so a mistake reaches everyone. Merging a pull request
doesn't release anything: the download links only change when a release is published.

### Before building

- [ ] Everything going out is merged into `main`, and the local `main` matches GitHub
  (`git pull`).
- [ ] `VERSION` in `Application Files/main.py` is raised. The build copies it into the
  app's `Info.plist`. It must be higher than the latest release, or installed copies
  won't update.
- [ ] `python3 -m pytest` passes.
- [ ] The signing certificate is valid and not about to expire:
  `security find-identity -v -p codesigning` lists an *Apple Development* identity from
  the same team as the last release (`XT4LT7MS6Y`). Installed copies refuse updates
  signed by any other team.
- [ ] The README matches what's shipping: install steps, features, privacy.

### Build and check the disk image

- [ ] `scripts/package.sh` makes `dist/Mac-Engine-Room-<version>.dmg`. That's the only file a
  release has: people download it, and installed copies update from it.
- [ ] Open the DMG: Mac Engine Room and Applications with the arrow between them, and the
  first-launch card at the bottom easy to read.
- [ ] The app inside has the new version, a valid signature, and the right team:
  ```bash
  M=$(hdiutil attach -nobrowse -readonly -noautoopen dist/Mac-Engine-Room-1.2.7.dmg | grep -o '/Volumes/.*$')
  defaults read "$M/Mac Engine Room.app/Contents/Info" CFBundleShortVersionString
  codesign --verify --deep --strict "$M/Mac Engine Room.app" && codesign -dvv "$M/Mac Engine Room.app" 2>&1 | grep TeamIdentifier
  hdiutil detach "$M"
  ```
- [ ] The fan helper is inside, signed by the same team with the hardened runtime, next to
  its launchd job (before the `hdiutil detach` above):
  ```bash
  codesign -dvv "$M/Mac Engine Room.app/Contents/MacOS/Mac Engine Room Fans" 2>&1 | grep -E "^Identifier|flags|TeamIdentifier"
  plutil -p "$M/Mac Engine Room.app/Contents/Library/LaunchDaemons/com.pythonflow.monitor.fans.plist" | grep Label
  ```

### Test fan control, if fans.py, the helper or signing changed

- [ ] A test copy holds the fans, and hands them back on quit, when killed, and when it
  stops responding (see *Testing fan control* below).
- [ ] After the release, the maintainer's own copy still runs its profile after updating,
  without being asked to allow the helper again.

### Test updating, if the updater, packaging or signing changed

- [ ] A test copy updates to a higher-version build from a stand-in release (see
  *Testing the updater* below): it downloads, swaps, and relaunches in the background.
- [ ] It refuses a build changed after signing, a build signed ad hoc or by another team,
  and a release whose tag is newer than the app inside it.
- [ ] Afterwards, the test copy, its data folder and the test disk images are deleted.

### Publish

- [ ] Release notes: what's new in this version, then the README's Install section. Only
  the current version: no notes for people on older versions.
- [ ] Publish it, tagged `v<version>`, from the commit it was built from:
  ```bash
  gh release create v1.2.7 dist/Mac-Engine-Room-1.2.7.dmg --title "Mac Engine Room 1.2.7" --notes-file notes.md
  ```
  Drafts and pre-releases are skipped by the updater, so publish a pre-release first to
  try a build before everyone gets it.
- [ ] Turn the previous release into a draft, so the page lists only the current one.
  This hides it without deleting it: `gh release edit v1.0.0 --draft=true`.

### After publishing

- [ ] `https://github.com/DigitalN/mac-engine-room/releases/latest` goes to the new tag.
- [ ] GitHub's API, which the updater reads, shows the new tag with only the `.dmg`:
  ```bash
  curl -s https://api.github.com/repos/DigitalN/mac-engine-room/releases/latest | grep -E '"tag_name"|"name": "Mac-Engine-Room'
  ```
- [ ] The DMG downloaded from the page is identical to the one in `dist/`
  (`shasum -a 256`).
- [ ] An older installed copy picks it up: choose **Check for Updates…** in its menu bar
  menu, or wait for the next wake. `app.log` shows `Downloading`, `ready to install`, then
  `Installed <version>; relaunching`, then `Mac Engine Room <version> started`. Without that
  last line the new copy didn't start: open it, and see *How updates work*.

### Signing

Releases are signed with a free Apple Development certificate. They aren't notarized by
Apple, so people have to click **Open Anyway** the first time (see Install in the
README). Development certificates last a year, so package a new release with a fresh
certificate from the same Apple account before the old one expires.

## How updates work

`Application Files/updater.py`, ported from Side Tabs' updater:

- **When:** once a day, the first time the Mac wakes from sleep (a minute after waking,
  so the network is back). A day counts once GitHub has answered; if the Mac was offline,
  the next wake tries again. **Check for Updates…** in the menu, or **Check Now** in
  Settings, checks right away.
- **What:** GitHub's
  [latest release](https://docs.github.com/en/rest/releases/releases#get-the-latest-release),
  through `/usr/bin/curl` (the bundled Python has no certificates of its own). Drafts and
  pre-releases don't count. If its tag is newer, it downloads the release's `.dmg`.
- **Checks:** copies the app with this bundle identifier out of the disk image (by
  identifier, not name, since copies older than 1.2.7 are still called Pythonflow) and runs
  `codesign --verify --deep --strict` with the requirement
  `identifier "com.pythonflow.monitor" and anchor apple generic and certificate leaf[subject.OU] = "<this copy's team>"`.
  The version inside must also be newer than the installed one.
- **Installing:** at a quiet moment (dashboard closed, no charge being recorded, no
  keyboard or mouse input for 10 seconds), it swaps the new app in with one
  `replaceItemAtURL` call and relaunches it in the background (`open -g`). The relaunch waits for the old copy to exit, opens the new one
  with `open -n`, and opens it again if it isn't running 5 seconds later
  (`launch.RELAUNCH_SCRIPT`): LaunchServices can still list a copy that has just quit as
  running, and a plain `open` then goes to it and nothing starts. That happened updating the
  maintainer's copy from 1.2.8 to 1.2.9; the quitting copy runs the relaunch, so copies
  older than the fix can still hit it, and come back at the next login or when opened.
- **Off** for copies that run from source, aren't team-signed, aren't in an Applications
  folder, or can't replace themselves. Settings says why, and **Check for Updates…**
  (**Open Releases Page** in Settings) opens the releases page instead.

## How fan control works

`Application Files/fans.py` and `Fan Helper/`:

- **The curve runs in the app**, once a second, on the hottest CPU or GPU core, in
  `FanControl`'s own thread. Setting a fan's speed needs root, so the app asks the helper.
- **The helper** is a small Swift program in the app bundle
  (`Contents/MacOS/Mac Engine Room Fans`). The app registers it with `SMAppService` the
  first time a profile other than macOS default is chosen, and the user allows it once in
  System Settings → General → Login Items & Extensions. launchd then starts it as root on
  the first connection to `/var/run/com.pythonflow.monitor.fans.sock`, and it exits once
  the fans are back with macOS and nobody is connected.
- **Who may talk to it:** the socket is open to every user, so the helper checks each
  caller's code signature: the app's bundle identifier, signed by the helper's own team.
- **What it does:** `set` holds each fan at a speed (clamped to its range; `Ftst` set to 1
  first on Macs that have it, such as the M4, then the target `F%dTg` and the mode key
  `F%dMd`, or `F%dmd` from M5, set to 1), `auto` sets the modes back to 0, then `Ftst`, and
  watches them for 10 seconds, `checkin` keeps the hold, and `status` reports. The app keeps its connection open while it holds the fans: the helper hands
  them back when it closes, after 10 seconds without a word, and before sleep.
- **Another app's speed:** before each `set` while holding, the helper reads every fan's
  target. One it wrote itself in the last 2 seconds (a written target can read back late) or
  its last, however long ago, is its own; any other speed means another app took the fan, and
  the helper lets it have the fans and writes nothing. Fans left in manual mode at the speeds
  the helper wrote (it gave them up too soon, or couldn't hand them back) are still its: the
  next `set` takes them back, and `auto`, a disconnect and sleep hand them to macOS. The speeds
  are kept in memory only, so a helper that has restarted leaves manual fans alone.

## Testing fan control

Test with a copy that has its own bundle identifier, and its own helper label and socket,
so it can't touch the installed app's helper. The app looks for
`<bundle identifier>.fans.plist`, so a copy whose plist wasn't renamed has no fan control.

1. Build, then make the test copy, `~/Applications/Mac Engine Room Test.app`, with its own
   identifier, helper label and socket:
   ```bash
   T=$(scripts/make-test-copy.sh)
   ```
2. Give it its own data folder, starting on a profile, and keep it out of Login Items:
   ```bash
   mkdir -p /tmp/pf-data && touch /tmp/pf-data/.open-at-login-default-applied
   echo '{"record_history": false, "fan_profile": "balanced"}' > /tmp/pf-data/settings.json
   open -n --env PYTHONFLOW_DATA_DIR=/tmp/pf-data "$T"
   ```
   It registers its helper and logs that it's waiting; allow "Mac Engine Room Test" in Login
   Items. `/tmp/pf-data/Logs/app.log` then shows `Fans held by Balanced at …`, and
   `python3 -c 'import power; s = power.SMC(); print(s.read("F0Md"), s.read("F0Tg"))'` in
   `Application Files` shows mode 1 and the target.
3. Quit it, `kill -9` it, and `kill -STOP` it: each time the fans go back to mode 0 (within
   a second, or 10 seconds for the stopped app), and the helper's unified log says why.
   After `kill -CONT` the app logs that it's taking the fans again.
4. Clean up. The helper has to be unregistered by the app it belongs to, so put a small
   tool that calls `SMAppService.daemon(plistName: "com.pythonflow.monitor.test.fans.plist").unregister()`
   in the test copy's `Contents/MacOS`, sign it and the copy again, run it, then delete the
   copy and `/tmp/pf-data`. `launchctl print system/com.pythonflow.monitor.test.fans` should
   no longer find the job.

## Testing on a busy Mac

The menu, the dashboard and fan control have to stay responsive when every core is busy
(`Application Files/priority.py`; *Idle work stays on the efficiency cores* in CLAUDE.md). This
takes over the Mac for a few minutes (it gets hot, its fans speed up, a second menu bar item
appears), so ask the maintainer first and agree a time when they aren't using it. Test with a
copy that has its own identifier (`scripts/make-test-copy.sh`), as for fan control.

1. Give it its own data folder, off Login Items, with history and updates off:
   ```bash
   mkdir -p /tmp/pf-data && touch /tmp/pf-data/.open-at-login-default-applied
   echo '{"record_history": false, "update_automatically": false}' > /tmp/pf-data/settings.json
   ```
2. Fill every core with more processes than there are cores, at the default QoS like a build's
   (20 on a 10-core M1 Pro), for the length of the test (they stop by themselves):
   ```bash
   for i in $(seq 20); do perl -e 'alarm 300; 1 while 1' & done
   ```
3. Launch the test copy during the load (`open -n --env PYTHONFLOW_DATA_DIR=/tmp/pf-data "$T"`),
   then open its menu by hand a few times. In `/tmp/pf-data/Logs/app.log`: `First reading … s after
   launch` is only there above 2 s, `Menu took … s to open` above 0.3 s, and `Mac busy: running at
   full priority` should follow the load starting (a wait of the sampler ending late is what it
   listens for; the sampler is itself starved by a sudden heavy load, so it can take a while),
   `Mac quiet again` about 30 s after the load ends.
4. `ps -M -p <pid>` shows each thread's priority: the main thread, the sampler and fan control
   at 4 (background) when the Mac is quiet, nothing is open and no fan profile holds the fans, and
   all at 47 during the load (and while a profile holds the fans, whatever the load).

With a profile chosen and the real helper allowed, `Fans held by …` should follow the first
reading within seconds. A busy Mac can't be told from a slow helper by the app's log alone: the
helper's side is in the unified log (see *Testing fan control*).

The idle cost is measured with `proc_pid_rusage(pid, RUSAGE_INFO_V6)` twice, 10 minutes apart on
a quiet Mac with only the menu bar item showing: `ri_energy_nj` is the energy,
`ri_user_ptime + ri_system_ptime` the time on the performance cores, `ri_runnable_time` the time
spent waiting for a CPU. All of them are in Mach ticks (`mach_timebase_info`).

## Testing the updater

Test with a separate copy that has its own bundle identifier, so it can't touch the
installed app and Launch Services doesn't mix the two up. Name the test copy
`Pythonflow.app` to also test the rename (`rename.py`): after the update it renames itself
to `Mac Engine Room.app` in the same folder.

1. Build, then make the test copy and a higher-version update from it, each re-signed
   with your certificate after editing its `Info.plist`:
   ```bash
   APP=$(scripts/build.sh)
   ID=$(security find-identity -v -p codesigning | awk '/"Apple Development/ {print $2; exit}')
   mkdir -p /tmp/pf-update /tmp/pf-feed
   copy() {  # destination version
     ditto "$APP" "$1"
     /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.pythonflow.monitor.test" \
       -c "Set :CFBundleShortVersionString $2" -c "Set :CFBundleVersion $2" "$1/Contents/Info.plist"
     codesign --force --deep -s "$ID" "$1"
   }
   copy "$HOME/Applications/Mac Engine Room Test.app" 1.0
   copy "/tmp/pf-update/Mac Engine Room.app" 9.9
   hdiutil create -volname "Mac Engine Room" -srcfolder /tmp/pf-update -format UDZO /tmp/pf-feed/Mac-Engine-Room-9.9.dmg
   ```
2. Write a stand-in for GitHub's latest-release response whose `.dmg` asset's
   `browser_download_url` is a `file://` URL, e.g. `/tmp/pf-feed/latest.json`:
   ```json
   {"tag_name": "v9.9", "assets": [{"name": "Mac-Engine-Room-9.9.dmg",
     "browser_download_url": "file:///tmp/pf-feed/Mac-Engine-Room-9.9.dmg"}]}
   ```
3. Give the test copy its own data folder. Keep its dashboard closed and its history off,
   and keep it out of Login Items:
   ```bash
   mkdir -p /tmp/pf-data && touch /tmp/pf-data/.open-at-login-default-applied
   echo '{"record_history": false}' > /tmp/pf-data/settings.json
   open -n --env PYTHONFLOW_DATA_DIR=/tmp/pf-data --env PYTHONFLOW_UPDATE_FEED=file:///tmp/pf-feed/latest.json \
     "$HOME/Applications/Mac Engine Room Test.app"
   ```
   Choose **Check for Updates…** in the test copy's menu (it installs right away), or
   sleep and wake the Mac (it waits for a quiet moment). The signature check still applies.
   `/tmp/pf-data/Logs/app.log` shows `Downloading 9.9`, `9.9 is ready to install`, then
   `Installed 9.9; relaunching`.
4. To check that bad updates are refused, change the update's `Info.plist` after it's
   signed, re-sign it with `codesign --force --deep -s -`, or sign it with another bundle
   identifier. Each should log `Update check failed` and leave the test copy alone.
5. Clean up: quit the test copy, then delete it, `/tmp/pf-data`, `/tmp/pf-update` and
   `/tmp/pf-feed`.

The updater mounts the disk image in `/Volumes`, hidden from Finder: mounted anywhere
else, macOS's file access protection stops the app from reading it. The first mount of a
freshly written image sometimes fails with "Resource temporarily unavailable" and leaves
it attached with no volume; the updater detaches it and tries again. `hdiutil attach` is
deprecated in macOS 27 in favor of `diskutil image attach`; it still works.
