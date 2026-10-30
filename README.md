# onedrive-linux-ondemand

A Linux desktop client for Microsoft OneDrive that behaves like the Windows
client's **Files On-Demand**, built for Linux Mint (Cinnamon / Nemo).

> **Status: early version (0.1.0).** It works, but it's new. Read
> [Limitations](#limitations) first, and keep OneDrive's own recycle bin and
> version history in mind as your safety net.

- One folder, `~/OneDrive`, shows all your OneDrive files **without
  downloading them**. A file downloads when you open it.
- Right-click in Nemo → **OneDrive**:
  - **Always keep on this device**: download now and never free it.
  - **Free up space**: remove the downloaded copy; it stays on OneDrive.
  - **View online**: open it on onedrive.com.
- Emblems in Nemo show each file's state:
  ☁ online-only · ✓ (outline) locally available · ✔ (solid) always kept ·
  ⟳ syncing · ✖ problem.
- Files you haven't opened for 30 days are freed automatically, and space is
  freed when the disk gets full (both adjustable). Files you chose to always
  keep are never freed.
- Changes you save upload automatically; changes made elsewhere appear
  within about a minute.
- A tray icon shows the status, with Pause, Recent activity and Settings.

It's built on [rclone](https://rclone.org/), which does the downloading and
uploading. This project adds pins, freeing space, status, Nemo integration and
the tray. See [docs/DESIGN.md](docs/DESIGN.md) for how it works.

## Install

1. **rclone 1.75 or newer**, from rclone.org. The version in Mint's own
   Software Manager is too old. Download `rclone-current-linux-amd64.deb`
   from <https://rclone.org/downloads/> and install it:

   ```bash
   sudo apt install ./rclone-current-linux-amd64.deb
   ```

2. **This program**: download `onedrive-ondemand_<version>_all.deb` from the
   [Releases](https://github.com/Breedible/onedrive-linux-ondemand/releases)
   page (or build it yourself, see [Development](#development)) and install it:

   ```bash
   sudo apt install ./onedrive-ondemand_0.1.0_all.deb
   ```

   This also installs `nemo-python`, `fuse3` and the other small things it needs.

3. **Set up**: open the menu → **OneDrive**, or run:

   ```bash
   onedrive-ondemand setup
   ```

   Your browser opens the Microsoft sign-in page; sign in and allow access.
   Setup asks whether Nemo may download pictures to show previews, then
   starts everything, now and at every login.

   **For now it only shows one folder of your OneDrive, `LinuxClientTest`**
   (setup offers to create it). Showing the whole drive will be switched on
   after more testing. The folder is set in `~/.config/onedrive-ondemand/config.json`.

4. **Close all Nemo windows once** (or run `nemo -q`) so Nemo loads the
   OneDrive menu.

## Everyday use

Use `~/OneDrive` like any other folder. The tray icon (a cloud) tells you
if something needs attention. For scripts or troubleshooting there's also a
command:

```bash
onedrive-ondemand status                 # overall status
onedrive-ondemand status ~/OneDrive/x    # state of one file
onedrive-ondemand pin ~/OneDrive/Work    # always keep on this device
onedrive-ondemand unpin ~/OneDrive/Work
onedrive-ondemand free ~/OneDrive/Videos # free up space
onedrive-ondemand activity               # recent uploads and problems
onedrive-ondemand pause                  # pause uploads (and resume)
onedrive-ondemand settings --evict-after-days 60 --min-free-space 15
```

## Limitations

Linux has no equivalent of Windows' placeholder files, so some things work
differently. Read these:

- **`~/OneDrive` only shows files while the service is running.** If it isn't
  (for example right at login, or after a crash), the folder looks empty and
  is read-only. Your downloaded files are safe in
  `~/.local/share/onedrive-ondemand/cache` and reappear when it starts.
- **Offline:** downloaded and "always kept" files open and save normally;
  saves upload when you're back online. Opening an online-only file fails.
- **Deleting a file in `~/OneDrive` deletes it from OneDrive** (it goes to
  the OneDrive recycle bin for 30 days), just like on Windows.
- **Conflicts:** if the same file is changed on another device *and* here
  before your change uploads, this client keeps both: the other version
  is saved as `name (conflict copy <date>).ext` next to yours. This is
  **best effort**: if both changes happen within a few seconds the other
  version may only be in OneDrive's **version history** (onedrive.com →
  right-click the file → Version history).
- **Previews:** Nemo has to download a picture or video to draw its
  preview, so previews are off in `~/OneDrive` unless you allow them in
  setup or in the tray's Settings.
- Only **Nemo** shows emblems and the OneDrive menu; other programs just see
  normal files.
- One OneDrive account. OneDrive **Business** should work (rclone supports
  it) but hasn't been tested yet.
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
sign-in with the `rm -r` command it prints. Your files on OneDrive are never
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

## License

[GPL-3.0](LICENSE)
