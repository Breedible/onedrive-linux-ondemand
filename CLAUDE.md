# Notes for AI coding assistants (and human contributors)

## Project
A Files On-Demand style OneDrive client for Linux Mint (Cinnamon / Nemo),
built on `rclone mount --vfs-cache-mode full`. Read these first:

- `docs/RESEARCH.md`: what rclone can and can't do, and why we didn't fork
  another project.
- `docs/DESIGN.md`: architecture, pin storage, eviction, failure modes,
  milestones. **This design is approved; follow it.** If something in it
  turns out to be wrong when tested, explain why and propose a change before
  building around it.

## Current status
- Design approved 2026-10-30.
- **Milestone (a) done 2026-10-30:** `onedrive-ondemand setup` (browser
  sign-in, our own rclone config, mount of `LinuxClientTest` as the
  `onedrive-ondemand-mount` systemd user service). Verification results are in
  `docs/RESEARCH.md` §5.
- **Milestone (b) done 2026-10-30:** daemon (`onedrive-ondemand-daemon`
  service) + CLI `pin`, `unpin`, `free`, `status`, `activity`, `pause`,
  `resume`, `settings`. Results in `docs/RESEARCH.md` §6.
- **Milestone (c) done 2026-10-30:** Nemo extension `nemo/onedrive_ondemand_nemo.py`
  (emblems, OneDrive submenu, View online), emblem icons, thumbnail choice.
  Needs the `nemo-python` package.
- **Milestone (d) done 2026-10-30:** tray icon (`onedrive_ondemand/tray.py`):
  status, open folder, view online, pause/resume, recent activity,
  settings dialog, sign in again, notifications for problems. Autostart
  entry and "OneDrive" menu entry installed by setup.
- **Milestone (e) done 2026-10-30:** `packaging/build-deb.sh` builds
  `dist/onedrive-ondemand_<version>_all.deb` (system-wide services, Nemo
  extension, icons, menu + autostart). `setup` notices when it runs from the
  installed package and removes per-user copies from a source-folder setup.
  `onedrive-ondemand uninstall`. README covers install/uninstall/limitations.
- Next: **milestone (f)**: conflict guard.
- Live tests go in `LinuxClientTest/_dev-tests/`. Other folders in there are the
  maintainer's own test files: only ever read them, never change them.
- Milestones run in order a → f. After each one: explain in plain language
  how to test it and what is still missing, then commit.
- Run tests with: `python3 -m unittest discover -s tests -t .`
- Run from the source folder with: `./bin/onedrive-ondemand <command>`
- Build the package with: `./packaging/build-deb.sh`

## How to work on this repo
- The maintainer is a **beginner coder**. Keep code simple and readable,
  comment the non-obvious parts, and avoid clever abstractions.
- Say plainly when the maintainer has to do something themselves (sign in
  in the browser, approve permissions, enter a password for `sudo`).
- Python 3.10+ standard library plus PyGObject (`gi`) only. No pip packages.
- Line endings are LF (enforced by `.gitattributes`).
- Ask before anything outward-facing (pushing, publishing releases).

## Safety rules (non-negotiable)
1. During development, rclone may only see the `LinuxClientTest` folder:
   mount it by path (`onedrive-ondemand:LinuxClientTest`). **Don't use
   `root_folder_id` for this: uploads ignore it** (RESEARCH.md §5). Never
   point it at the whole drive except with `--read-only`.
2. Never run `rclone sync`, `bisync`, `delete`, `purge`, `cleanup`, or use
   `--onedrive-hard-delete`.
3. Test cache, eviction and pin logic against a fake cache folder in unit
   tests before it touches a real mount.
4. Never commit `rclone.conf`, tokens, or anything from
   `~/.config/onedrive-ondemand/`.
