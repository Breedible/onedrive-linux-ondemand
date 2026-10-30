# onedrive-linux-ondemand

A Linux desktop client for Microsoft OneDrive that behaves like the Windows
client's **Files On-Demand**, built for Linux Mint (Cinnamon / Nemo).

> **Status: early version (0.2.0).** It works, but it's new. Read
> [Limitations](#limitations) first, and keep OneDrive's own recycle bin and
> version history in mind as your safety net.

- Each account gets one folder, like on Windows: `~/OneDrive - Personal`,
  `~/OneDrive - Work`. It shows all your files **without downloading them**.
  A file downloads when you open it.
- **Several accounts** side by side (for example personal and work). Add or
  sign out of accounts in the tray's Settings.
- Right-click in Nemo → **OneDrive**:
  - **Always keep on this device**: download now and never free it.
  - **Free up space**: remove the downloaded copy; it stays on OneDrive.
  - **View online**: open it on onedrive.com.
- Emblems in Nemo show each file's state:
  ☁ online-only · ✓ (outline) locally available · ✔ (solid) always kept ·
  ⟳ syncing · ✖ problem.
- **Picture previews without downloading**: Nemo shows OneDrive's own small
  previews of photos, videos and PDFs. They're deleted again a couple of
  minutes after you leave the folder.
- Files you haven't opened for 30 days are freed automatically, and space is
  freed when the disk gets full (both adjustable). Files you chose to always
  keep are never freed.
- Changes you save upload automatically; changes made elsewhere appear
  within about a minute.
- A **tray icon** shows the status, and spins while files transfer.
  **Left-click** it to see what's downloading and uploading, with progress
  bars, and recent activity. **Right-click** for the menu: Pause, Settings
  and accounts, Check for updates.

It's built on [rclone](https://rclone.org/), which does the downloading and
uploading. This project adds pins, freeing space, status, Nemo integration,
previews and the tray. See [docs/DESIGN.md](docs/DESIGN.md) for how it works.

## Install

1. **Download** `onedrive-ondemand_<version>_all.deb` from the
   [Releases](https://github.com/Breedible/onedrive-linux-ondemand/releases)
   page (or build it yourself, see [Development](#development)) and install
   it, either by double-clicking it or with:

   ```bash
   sudo apt install ./onedrive-ondemand_0.2.0_all.deb
   ```

   This also installs `nemo-python`, `fuse3` and the other small things it needs.

2. **Set up**: open the menu → **OneDrive** (or click the OneDrive icon in the
   panel after logging in again, or run `onedrive-ondemand setup`). A window
   guides you through it:
   - If rclone 1.75 or newer isn't installed, setup offers to install it
     from rclone.org (Mint's own rclone is too old). You type your password.
   - Your browser opens the Microsoft sign-in page; sign in and allow access.
   - You choose a name for the account ("Personal" or "Work" by default).
   - Setup asks whether to show picture previews, then starts everything,
     now and at every login.

   **For now each account only shows one folder of its OneDrive,
   `LinuxClientTest`** (setup offers to create it). Showing the whole drive
   will be switched on after more testing.

3. **Close all Nemo windows once** (or run `nemo -q`) so Nemo loads the
   OneDrive menu.

**Adding a work or school account:** tray → right-click → **Settings and
accounts…** → **Add an account…** (or `onedrive-ondemand add-account`). If the
sign-in page signs you straight in to your personal account, choose **Use
another account**, or open the link it prints in a private browser window.
Some organisations must approve rclone before you can sign in; if the sign-in
page says you need an admin's approval, ask your IT department.

## Updating

Tray → right-click → **Check for updates…**. The tray also checks once a day
and tells you when there's a new version. Installing it asks for your
password, then restarts OneDrive (close files you have open from OneDrive
first; changes waiting to upload are kept).

Running from a source folder instead (a `git clone`)? Then "Check for
updates" runs `git pull` there and restarts everything.

**Updating from version 0.1:** after updating, run `onedrive-ondemand setup`
once. It moves your existing setup (sign-in, downloaded files, pins and
changes waiting to upload) to the new layout: your files then appear in
`~/OneDrive - Personal` instead of `~/OneDrive`.

## Everyday use

Use the OneDrive folders like any other folder. The tray icon tells you if
something needs attention. For scripts or troubleshooting there's also a
command:

```bash
onedrive-ondemand status                              # overall status of every account
onedrive-ondemand status ~/"OneDrive - Personal"/x    # state of one file
onedrive-ondemand pin ~/"OneDrive - Personal"/Work    # always keep on this device
onedrive-ondemand unpin ~/"OneDrive - Personal"/Work
onedrive-ondemand free ~/"OneDrive - Work"/Videos     # free up space
onedrive-ondemand activity                            # recent uploads, downloads and problems
onedrive-ondemand pause                               # pause uploads (and resume)
onedrive-ondemand settings --evict-after-days 60 --min-free-space 15
onedrive-ondemand accounts                            # list accounts
onedrive-ondemand add-account                         # add an account
onedrive-ondemand remove-account Work                 # sign out of one
onedrive-ondemand update                              # check for a new version
```

Add `--account Work` before the command to only affect one account, e.g.
`onedrive-ondemand --account Work pause`.

## Limitations

Linux has no equivalent of Windows' placeholder files, so some things work
differently. Read these:

- **The OneDrive folders only show files while OneDrive is running.** If it
  isn't (for example right at login, or after a crash), a folder looks empty
  and is read-only. Your downloaded files are safe in
  `~/.local/share/onedrive-ondemand/accounts/` and reappear when it starts.
- **Offline:**
  - Downloaded and "always kept" files open and save normally; saves
    upload when you're back online.
  - Opening a file that isn't downloaded fails: the program trying to open
    it waits for up to about a minute, then gets an error. The tray tells
    you why.
  - If OneDrive starts while you're offline (for example after a restart
    without Wi-Fi), it shows only your downloaded files, **read-only**,
    until you're back online. It then reconnects by itself as soon as no
    program has a file from it open.
- **Deleting a file in a OneDrive folder deletes it from OneDrive** (it goes
  to the OneDrive recycle bin for 30 days), just like on Windows.
- **Conflicts:** if the same file is changed on another device *and* here
  before your change uploads, this client keeps both: the other version
  is saved as `name (conflict copy <date>).ext` next to yours. This is
  **best effort**: if both changes happen within a few seconds the other
  version may only be in OneDrive's **version history** (onedrive.com →
  right-click the file → Version history).
- **Previews** come from OneDrive, so you need to be online to see new ones
  (offline you see a plain grey square). Files that are already downloaded
  get normal previews made from the file.
- Only **Nemo** shows emblems, the OneDrive menu and the no-download
  previews; other programs just see normal files.
- OneDrive **Business** (work or school) accounts should work, but haven't
  been tested yet.
- Your sign-in expires if the client isn't used for 90 days; the tray then
  offers **Sign in again**.
- Names OneDrive doesn't allow (`" * : < > ? \ |`) are shown with
  look-alike characters. Two names that differ only in upper/lower case
  can't both exist on OneDrive. Symlinks aren't supported.
- Dual boot with Windows: keep the Linux cache on the Linux partition (the
  default). Don't point a Linux sync program at Windows' OneDrive folder.

## Uninstall

```bash
onedrive-ondemand uninstall          # stops everything; keeps your files
sudo apt remove onedrive-ondemand
```

`uninstall` tells you if any change hasn't been uploaded yet. When it says
nothing is waiting, you can delete the downloaded copies, settings and
sign-ins with the `rm -r` command it prints. Your files on OneDrive are never
touched.

## Development

The code is Python 3 (standard library + PyGObject), small on purpose and
commented for beginners. Notes for contributors and AI assistants are in
[CLAUDE.md](CLAUDE.md), including the **safety rules** for testing against
a real OneDrive.

```bash
./bin/onedrive-ondemand setup                      # run from the source folder
python3 -m unittest discover -s tests -t .         # tests (no network needed)
./packaging/build-deb.sh                           # build dist/*.deb
```

### Making a release

The tray's "Check for updates" looks at this repository's GitHub releases.
To publish one:

1. Set `__version__` in `onedrive_ondemand/__init__.py` (e.g. `"0.2.1"`) and
   commit it.
2. Tag and push the tag:

   ```bash
   git tag v0.2.1
   git push origin v0.2.1
   ```

GitHub then runs the tests, builds the `.deb` and publishes the release
(`.github/workflows/release.yml`). Everyone's tray offers the update within
a day.

## License

[GPL-3.0](LICENSE)
