# Developing Mac Engine Room

Notes for building Mac Engine Room (formerly Pythonflow) from source and publishing
releases. For installing and using it, see the [README](../README.md). The architecture
and the rules the code follows are in [CLAUDE.md](../CLAUDE.md).

## Build from source

You need Python 3 from [python.org](https://www.python.org/downloads/) and the pinned
packages in `Application Files/requirements.txt` (`scripts/First Time Setup.command`
installs them). Then:

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
  `Installed <version>; relaunching`.

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
  `replaceItemAtURL` call and relaunches it in the background (`--background`, so the
  dashboard doesn't pop up).
- **Off** for copies that run from source, aren't team-signed, aren't in an Applications
  folder, or can't replace themselves. Settings says why, and **Check for Updates…**
  (**Open Releases Page** in Settings) opens the releases page instead.

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
   echo '{"open_dashboard_at_launch": false, "record_history": false}' > /tmp/pf-data/settings.json
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
