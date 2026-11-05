# OneDrive On-Demand for Linux

Use Microsoft OneDrive on Linux Mint the way you would on Windows: all your
files show up in a folder, but they only download when you open them. Your
disk doesn't fill up with files you never use.

> **Early version.** It works, but it's new. OneDrive's own recycle bin and
> version history (on onedrive.com) are your safety net while it matures.

## What it does

- **Your whole OneDrive in one folder**, for example `OneDrive - Personal`
  in your home folder. Files appear straight away, but take no space until
  you open them.
- **Open a file and it downloads.** Save it and the change uploads by
  itself. Changes made on your phone or another computer show up within
  about a minute.
- **Small icons on every file and folder** in the file manager show where
  things are:
  - ☁ **cloud**: only on OneDrive (opening it downloads it)
  - ✓ **outline tick**: downloaded to this computer
  - ✔ **solid tick**: always kept on this computer
  - ☁ with a small tick (folders): some of the files inside are downloaded
  - ⟳ **arrows**: uploading or downloading
  - ✖ **red cross**: something went wrong (the tray icon tells you what)
- **Right-click a file or folder → OneDrive** to:
  - **Always keep on this device**: download it now and keep it, for use
    without internet.
  - **Free up space**: remove the downloaded copy. It stays on OneDrive.
  - **View online**: open it on onedrive.com.
- **Picture previews without downloading.** Photos and videos show small
  previews made by OneDrive, so browsing a folder of photos doesn't download
  them.
- **Frees space by itself.** Files you haven't opened for 30 days are
  removed from this computer again (they stay on OneDrive). Files you chose
  to always keep are never removed.
- **Several accounts**, for example personal and work, each in its own
  folder.
- **A OneDrive icon in the panel** shows whether everything is up to date.
  Click it to see what's downloading or uploading. Right-click it to pause,
  change settings, add an account or check for updates.
- **Works offline.** Downloaded files keep opening and saving without
  internet. Your changes upload as soon as you're back online.

## Install

1. Download the `.deb` file from the
   [Releases](https://github.com/Breedible/onedrive-linux-ondemand/releases)
   page and double-click it to install. (Everything else it needs is
   installed along with it.)
2. Open the menu and start **OneDrive**. A window guides you through setup:
   - If a newer version of rclone (the program that does the actual
     transfers) is needed, it offers to install it. You'll be asked for
     your password.
   - Your web browser opens the Microsoft sign-in page. Sign in and allow
     access.
   - Give the account a name, such as "Personal". Your files appear in
     `OneDrive - Personal`.
3. Close all file manager windows once, so the file manager picks up the
   OneDrive icons and menu.

That's it. OneDrive now starts by itself every time you log in.

**Adding a work or school account:** right-click the OneDrive icon in the
panel → **Settings and accounts…** → **Add an account…**. If the sign-in
page signs you straight in to your personal account, choose **Use another
account**. Some workplaces have to approve the app first; if the sign-in page
says so, ask your IT department.

## Everyday use

Use your OneDrive folder like any other folder. Open, edit, copy, move and
delete files as usual. Keep an eye on the OneDrive icon in the panel: it
tells you when something needs your attention, such as signing in again.

**Updates:** the OneDrive icon tells you when a new version is out. You can
also right-click it → **Check for updates…**.

## Good to know

- **Deleting a file in the OneDrive folder deletes it from OneDrive**, just
  like on Windows. It goes to OneDrive's recycle bin (on onedrive.com) for
  30 days, so you can get it back.
- **Without internet**, files that aren't downloaded can't be opened. If
  you'll be offline, right-click the folders you need → **Always keep on
  this device** beforehand.
- **If the computer starts without internet**, the OneDrive folder shows
  only your downloaded files, and you can't change them, until the internet
  is back. Then it reconnects by itself.
- **The OneDrive folder looks empty while OneDrive isn't running** (for a
  moment after logging in, for example). Your files are safe and come back
  as soon as it starts.
- **If the same file is changed here and somewhere else at the same time**,
  you get both versions: the other one is saved next to yours as
  `name (conflict copy <date>)`. If both changes happen within seconds, the
  other version may only be in the file's version history on onedrive.com.
- If you don't use it for 90 days, Microsoft asks you to sign in again. The
  OneDrive icon shows **Sign in again** when that happens.
- The icons, previews and right-click menu are in the file manager (Nemo).
  Other programs simply see normal files.
- Work and school accounts should work, but haven't been tested much yet.

## Uninstall

Open a terminal and run:

```bash
onedrive-ondemand uninstall
sudo apt remove onedrive-ondemand
```

The first command stops everything and tells you if any change hasn't
uploaded yet. Your files on OneDrive are never touched.

## For developers

It's written in Python and built on [rclone](https://rclone.org/), which does
the downloading and uploading. This project adds the on-demand behaviour,
the file manager integration, previews and the panel icon.

- How it works: [docs/DESIGN.md](docs/DESIGN.md). What was tested, and why:
  [docs/RESEARCH.md](docs/RESEARCH.md).
- Notes for contributors (including safety rules for testing against a real
  OneDrive): [CLAUDE.md](CLAUDE.md).
- There's also a command line: `onedrive-ondemand --help`.

```bash
./bin/onedrive-ondemand setup                      # run from the source folder
python3 -m unittest discover -s tests -t .         # run the tests
./packaging/build-deb.sh                           # build the .deb
```

To publish a release, set `__version__` in `onedrive_ondemand/__init__.py`,
commit, then push a tag such as `v0.2.1`. GitHub builds the `.deb` and
publishes it, and everyone's panel icon offers the update within a day.

## License

[GPL-3.0](LICENSE)
