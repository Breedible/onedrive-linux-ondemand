# Design: onedrive-linux-ondemand

Status: **approved 2026-10-30** (see §12). Milestones (a)–(f) built
2026-10-30; changes found while testing are noted in §12 and in the sections.
Version 0.2 (2026-10-30, asked for by the maintainer after testing) adds
several accounts (§13), an offline fallback (§4), transfer progress in the
tray (§5), previews without downloading (§8) and updates (§14). Research
behind these decisions is in [RESEARCH.md](RESEARCH.md).

## 1. What Linux can and cannot do (read this first)

Windows Files On-Demand uses a kernel filter driver (the "Cloud Files API")
that puts **placeholder files** on a normal NTFS disk. Each placeholder is a
real file entry whose contents are fetched when something opens it. Linux has
no such API. The closest equivalent is a **FUSE filesystem**: a program
(here, `rclone mount`) pretends to be a disk and answers file requests itself.

Consequences we have to live with:

| Windows | Our Linux version |
|---|---|
| `C:\Users\you\OneDrive` is a real folder on the disk. | `~/OneDrive - Personal` (one per account, §13) is a **mount point**. Files show there only while rclone is running. |
| If OneDrive.exe isn't running, local files are still there. | If the mount isn't running, the folder looks **empty**. Your downloaded and pinned data is safe on disk (see §4), just not visible in that folder. |
| Pin state is a file attribute stored in NTFS. | Pin state is kept in **our own small database**, because FUSE has nowhere to store it. |
| Every app sees the cloud and pin states. | Only **Nemo** shows emblems, via our extension. Other apps just see normal files. |
| Explorer knows not to download files just to draw thumbnails. | Nemo reads files to make thumbnails, which would **download the photos**. Our own thumbnailer serves OneDrive's server-side previews instead. See §8. |
| Built-in "keep both" conflict handling. | rclone uploads last-writer-wins. We add a best-effort guard (§7), and OneDrive's version history is the safety net. |

## 2. Architecture

Four small pieces of our own, with rclone doing the hard work. Everything in
the picture except the CLI, tray and Nemo extension exists **once per
account** (§13): each account has its own rclone mount and its own daemon.

```
                    +--------------------------+
 Nemo  ──────────►  | ~/OneDrive - <name>      |  ◄── any app opens/saves files
                    +------------+-------------+
                                 │
                    +------------▼-------------+       Microsoft
                    |  rclone mount            | ◄───► OneDrive
                    |  --vfs-cache-mode full   |       (Graph API)
                    |  (systemd user service)  |
                    +---+-----------------▲----+
        cache files on  │                 │ rc API (unix socket):
        disk (§4)       │                 │ upload queue, stats
                    +---▼-----------------+----+
                    |  our daemon (Python)     |  pins, eviction, status,
                    |  (systemd user service)  |  conflict guard, activity log
                    +---▲----------▲-------▲---+
          unix socket   │          │       │
                  +-----+--+  +----+---+  ++----------+
                  |  CLI   |  |  tray  |  | Nemo      |
                  |        |  |  icon  |  | extension |
                  +--------+  +--------+  +-----------+
```

| Piece | Job | Tech |
|---|---|---|
| **rclone mount** | Shows OneDrive at `~/OneDrive - <name>`, downloads on open, uploads on save, polls for remote changes. | rclone ≥ 1.75 from rclone.org. Runs as a `systemd --user` service (`onedrive-ondemand-mount@<account>`) with `Restart=on-failure`. |
| **daemon** | Remembers pins, downloads pinned items, frees space, works out each file's state, logs activity, reports transfers, serves previews. | Python 3 standard library only (sqlite3, socket, json, threading). Separate systemd user service (`onedrive-ondemand-daemon@<account>`), so a daemon crash **never** unmounts your files. |
| **CLI** | `onedrive-ondemand pin/unpin/free/status/setup`. Used for testing and scripting. | Python, talks to the daemon. |
| **Nemo extension** | Emblems and the "OneDrive" right-click submenu. | `nemo-python`. Asks the daemon, with a short timeout so Nemo never freezes. |
| **Tray icon** | Status of all accounts, transfer progress, pause/resume, recent activity, settings, accounts, updates. | GTK 3 + `XApp.StatusIcon` (Cinnamon's native tray API). |
| **Thumbnailer** | Previews of OneDrive photos without downloading them (§8). | Run by Nemo; asks the daemon. |

Why two services instead of one program: if our Python code crashes, open
files in your editor must keep working. rclone is mature, and our code is new.

## 3. The three file states

| State | Meaning | How we tell (from rclone's cache "sidecar" JSON) | Emblem |
|---|---|---|---|
| **Online-only** | Nothing (or only part of the file) is on disk. | No sidecar, or cached ranges cover less than the whole file. | ☁ cloud |
| **Locally available** | Fully on disk because you opened it. Can be freed. | Sidecar covers the full size, no pin. | ✓ outline tick |
| **Always keep on this device** | Pinned. Downloaded and never freed by us. | Pin rule applies **and** fully cached. | ✔ solid tick |
| *Syncing* | Upload pending, or pinned but still downloading. | `Dirty: true`, or in rclone's upload queue, or pin download in progress. | ↻ arrows |
| *Error* | Upload keeps failing, or pin download failed. | Upload queue `tries` above a limit, or a recorded download error. | ✖ red |

### How pins are stored

A SQLite file holds **rules**, not per-file flags:

```
rules(item_id TEXT PRIMARY KEY,      -- OneDrive's permanent ID for the item
      path TEXT,                     -- last known path, relative to the OneDrive root
      state TEXT,                    -- 'pinned', 'online' or 'local'
      created_at TEXT,
      last_seen TEXT)                -- last time we confirmed the path
```

- A file's effective state comes from the **nearest rule** on it or a parent
  folder. Pinning a folder pins everything inside, including files added
  later, which matches Windows.
- "Free up space" on a file inside a pinned folder adds an `online` rule
  for that file only.
- Unpinning a file inside a pinned folder adds a `local` rule: not pinned,
  but not freed straight away either (added in milestone b).
- "Locally available" is the default when no rule applies.

### Pins follow renames and moves

Every OneDrive file and folder has a **permanent item ID** that never changes
when it's renamed or moved, whether in Nemo, on the website or on Windows.
The rule is keyed on that ID, and the path is only a "last known location".

1. **When pinning,** the daemon asks rclone for the item's ID (rclone's
   `lsjson`/`operations/stat` returns an `ID` for OneDrive) and stores it.
2. **Checking paths:** before every eviction pass and every pinned-folder
   re-scan, the daemon checks that each rule's path still exists.
3. **If a path is gone,** the daemon looks the ID up directly in the
   Microsoft Graph API (`GET /me/drive/items/{id}`). That returns the item's
   current name and folder in one request. It uses the same sign-in token
   rclone already has, read from our own rclone config.
   - **Found:** update the rule's path. The item stays pinned.
   - **In the recycle bin / deleted:** the rule is marked "deleted" and
     kept for 30 days (in case you restore it), then removed.
   - **Moved outside the folder this client shows:** the rule is kept but
     inactive. It becomes active again if the item comes back.
4. **Safety:** if any pinned rule couldn't be checked (for example you're
   offline), the eviction pass **skips the timer rule entirely** and only
   frees space in a disk-nearly-full emergency. A just-renamed pinned file is
   therefore never freed by mistake.

**Verified for Personal (2026-10-30):** rclone returns `ID` as
`<driveID>#<itemID>`; Graph wants the part after `#`. Business is still to
check in milestone b.

## 4. Where your data physically lives

| What | Location | Notes |
|---|---|---|
| Downloaded and pinned file contents | `~/.local/share/onedrive-ondemand/accounts/<account>/cache/vfs/<remote>/<path>` | rclone's `--cache-dir`, deliberately **not** `~/.cache`. Cleaning tools (BleachBit, "clear cache") treat `~/.cache` as disposable. These are normal files at their real paths, stored *sparse* (only downloaded byte ranges use disk). |
| rclone's per-file bookkeeping | `…/<account>/cache/vfsMeta/<remote>/<path>` | Small JSON per file: which bytes are cached, whether an upload is pending. |
| Pin rules, activity log | `~/.local/share/onedrive-ondemand/accounts/<account>/state.db` | SQLite. Losing it loses pin choices, not files. |
| Settings | `~/.config/onedrive-ondemand/accounts/<account>/config.json` | Name, mount path, eviction timer, free-space limit, previews. |
| Sign-in token | `~/.config/onedrive-ondemand/accounts/<account>/rclone.conf` (mode 600) | Our **own** rclone config per account, separate from any `~/.config/rclone/rclone.conf` you already have. Never committed to git. |
| Picture previews | `~/.cache/thumbnails/` (the standard place) | Deleted again after you leave the folder (§8). |
| Logs | `journalctl --user -u 'onedrive-ondemand-*'` | |

Version 0.1 kept everything directly in `~/.config/onedrive-ondemand` and
`~/.local/share/onedrive-ondemand`, with the files in `~/OneDrive`. `setup`
moves that (by renaming, so nothing is copied or lost) into the `personal`
account, shown in `~/OneDrive - Personal`.

**What happens when things aren't running:**

- **Mount stopped (logged out, crashed, quit):** the account's folder is
  empty. All downloaded/pinned data is still in the cache folder above.
  Edits that weren't uploaded yet are uploaded automatically the next time
  the mount starts. To stop programs accidentally saving into the empty
  folder (those files would be hidden once the mount comes back), the bare
  folder is read-only while not mounted. fusermount can't mount
  onto a read-only folder, so the service makes it writable just before
  mounting and read-only again after rclone stops. rclone also refuses to
  mount over a non-empty folder.
- **Mount crashed hard:** the folder may show "Transport endpoint is not
  connected". The service restarts it automatically (it runs `fusermount -uz`
  first).
- **Daemon stopped:** files keep working normally. Pins aren't topped up,
  nothing is freed, and Nemo emblems disappear until it's back.
- **Offline (the network drops while running):** cached and pinned files
  open and save normally, and saves queue for upload. Opening an
  online-only file fails with an I/O error, after about half a minute to a
  minute (`--low-level-retries 3`; with rclone's default of 10 the opening
  program hung for over 3 minutes, RESEARCH.md §10). The daemon logs why, and
  the tray shows a notification. Folder listings come from rclone's
  directory cache, which **only lives in memory** and only holds folders
  that were opened since the mount started. So the daemon reads every
  folder listing (`vfs/refresh recursive=true`) after the mount starts and
  every 15 minutes, so that all folders can still be opened offline.
- **Offline when the mount starts** (a laptop booting without Wi-Fi): the
  normal mount couldn't show anything at all, not even downloaded files,
  because it has no folder listings yet (tested, RESEARCH.md §10). So the
  mount service waits up to 30 s for the network, then instead mounts the
  **cache folder itself, read-only**, filtered to fully downloaded files
  (`--files-from`). Partly downloaded files are hidden: they are mostly
  empty space on disk. It's read-only because anything saved there wouldn't
  be uploaded. Changes that were waiting to upload are shown in their new
  version (the cache holds it) and upload once the normal mount is back.
  When the daemon sees the network is back, it restarts the mount service
  as soon as no program has a file from it open (restarting closes them).
  Pin, free and eviction wait until then.
- **Do not edit files inside the cache folder directly.** rclone owns it.

## 5. Eviction ("freeing space")

rclone's own evictor can't respect pins, so we **turn it almost off** and do
it ourselves:

- rclone runs with `--vfs-cache-max-age 87600h` (10 years, effectively never)
  and **no** max size.
- As an emergency backstop only, rclone keeps `--vfs-cache-min-free-space 1G`.
  If the disk is nearly full **and** our daemon isn't freeing space, rclone
  may evict anything not open, including pinned files. They're re-downloaded
  later. A full disk is worse.

Our daemon runs a check every 5 minutes:

1. **Candidates** = files fully or partly cached that are **not** pinned,
   **not** dirty (pending upload), **not** in rclone's upload queue, and
   **not** open by any program (checked via `/proc/*/fd`).
2. **Timer rule:** free any candidate not accessed for `evict_after_days`
   (default **30**, `0` = never; setting in the tray).
3. **Space rule:** if free disk space is below `min_free_space` (default
   **10 %** of the disk), free candidates **least-recently-used first**
   until it isn't.
4. **To free a file:** re-read its sidecar immediately before acting. If it
   became dirty or was accessed, skip it. Otherwise delete the cache file
   and its sidecar. rclone detects this and treats the file as online-only
   again (verified, RESEARCH.md §5). Note that rclone writes a sidecar's
   ranges a few seconds after a file is closed.

"Free up space" from the menu does steps 1 and 4 immediately for the chosen
files. It refuses, with a clear message, for files that are open or still
uploading.

**Pinning** works the other way: the daemon reads each pinned file
end-to-end through the mount (two at a time), which makes rclone download
it fully. Pinned folders are re-scanned every 15 minutes (and after remote
changes) to catch new files. Before pinning a folder, the CLI/menu shows its
size and warns if it won't fit.

**Progress** (tray, left-click): every 2 s the daemon reads rclone's
`core/stats`. Its `transferring` list has every file being downloaded
(opening a file, pins, all go through rclone's VFS downloader, which
registers each one) and uploaded, with size, bytes done, speed and time
left. A name in the upload queue marked `uploading` is an upload;
everything else is a download. The tray shows them with progress bars,
like the OneDrive flyout on Windows, and its arrows spin while anything is
transferring. Finished downloads go into Recent activity.

**Pause** (tray, `onedrive-ondemand pause`) uses rclone's
`vfs/queue-set-expiry`: while paused, the daemon pushes every waiting upload
a year into the future; resume sets them back to "now". An upload that has
already started finishes. Pin downloads also wait. Opening files still works.

## 6. Settings (defaults)

| Setting | Default |
|---|---|
| Account name | `Personal` for a personal account, `Work` otherwise |
| Mount path | `~/OneDrive - <name>` |
| OneDrive folder to show | whole drive (`""`), **or `LinuxClientTest` during development** |
| Free unused files after | 30 days (0 = never) |
| Keep at least this much disk free | 10 % |
| Upload delay after save | 5 s |
| Check for remote changes every | 1 min |

## 7. Conflicts: honest version

**Baseline (rclone alone):** if a file changes on OneDrive *and* locally
before the local edit is uploaded, rclone keeps your local edit and then
uploads it over the remote one. The other edit isn't lost, because OneDrive
keeps it in **version history** (web → right-click → Version history). But
you wouldn't notice.

**Our guard (milestone f, built 2026-10-30):** for every file that appears
in rclone's upload queue, the daemon holds the upload (`vfs/queue-set-expiry`,
at most 2 minutes) and compares OneDrive's current version with the version
your edit started from. If OneDrive's copy changed in the meantime, it makes
a **server-side copy** of the OneDrive version, named
`report (conflict copy 2026-10-30 1530).docx`, then lets your upload go. You
get both copies, and the tray shows a notification.

The "version your edit started from" is the hash in rclone's sidecar
fingerprint, which rclone **keeps** while the change waits to upload
(verified; an earlier version of this document wrongly assumed it was
blanked). Editors that save by writing a temporary file and renaming it
(gedit, many others) leave an empty fingerprint, so the daemon also
records the last in-sync version of each file (after each upload and
whenever rclone downloads a file) and uses that instead. If neither is
known, or the check fails (offline), the upload goes ahead as plain rclone
would, and version history remains the fallback. The README says "best
effort", not "guaranteed".

## 8. Other failure modes and limitations

| Situation | What happens / what we do |
|---|---|
| Nemo thumbnails | To show a preview, Nemo would read the whole picture/video/PDF, which **downloads it**: GIO reports the rclone mount as **local**, so "Local files only" doesn't protect it. **Version 0.2:** our own thumbnailer (`thumbnailer.py`), installed in `~/.local/share/thumbnailers/`, which Cinnamon prefers over the system's (checked, RESEARCH.md §11). For a OneDrive file that isn't fully downloaded, the daemon fetches OneDrive's own **medium** preview (at most 176 px) from Graph (`/drives/{id}/root:/{path}:/thumbnails/0/medium`) and the file itself is never read. A fully downloaded file goes to the normal thumbnailer (it reads from disk). Files outside OneDrive go straight to the normal thumbnailer. **It must always write a picture for OneDrive files:** if a thumbnailer fails, Cinnamon reads the file itself (checked). So offline or on any error it writes a plain grey square. Nemo saves previews in `~/.cache/thumbnails`; the Nemo extension tells the daemon which folders are on screen (`LocationWidgetProvider`), and the daemon deletes each preview 2 minutes after its folder was last shown (and all of them when it starts, or when previews are turned off). Turning previews **off** still uses the per-folder switch (`metadata::show-thumbnails=false`). |
| Network drop | rclone retries with back-off. The daemon notices within 15 s (a quick connection test to Microsoft) and the tray shows "Offline". Uploads resume automatically. See §4. |
| Sign-in expires (90 days unused, or password change) | Mount fails to talk to OneDrive. Tray shows "Sign in again", which re-runs the browser sign-in. |
| Throttling (HTTP 429) | rclone backs off automatically. Your own client ID reduces this (optional, later). |
| Disk full | Space rule frees unpinned files. If only pinned files remain, the tray warns you. |
| Deleting in a OneDrive folder | **Deletes from OneDrive** (to the OneDrive recycle bin, kept 30 days), same as Windows. We never use permanent delete. |
| Invalid names (`:` `?` etc.), `a.txt` vs `A.txt` | rclone substitutes look-alike characters. Two names differing only by case can't both exist on OneDrive. |
| Huge folders (>50k items) | OneDrive limit. Listing may be slow or fail. |
| Two PCs (Linux + Windows dual boot) editing the same file | Treated as a remote change (§7). Keep the Linux cache on the Linux partition, not on a shared NTFS one. |
| Symlinks | Not supported by OneDrive. rclone can't store them. |
| OneDrive Business | rclone supports it (`drive_type business`); setup finds out the kind from Graph when you sign in. Several accounts at once since 0.2 (§13). Not yet tested with a real Business account. Some organisations require an admin to approve rclone's app before anyone can sign in. |

## 9. Development safety rules

1. rclone is only ever given **one throwaway folder**, `LinuxClientTest`,
   in your OneDrive: the mount is `onedrive-ondemand:LinuxClientTest`, so
   every path it lists or uploads is inside that folder. Every setup run
   saves a test file through the mount and checks with Graph that it arrived
   there; if not, setup turns the mount off. (`root_folder_id` was tried
   first and is **not** safe: uploads ignored it. See RESEARCH.md §5.)
2. The first time we look at the whole drive, the mount uses `--read-only`.
3. The eviction and pin code is tested first against a **fake cache folder**
   on disk (unit tests), before it ever touches a real mount.
4. We never use `rclone sync`, `bisync`, `delete`, `purge`, or
   `--onedrive-hard-delete`.
5. Backstop: the OneDrive recycle bin and version history (30 days).

## 10. Code layout (planned)

```
onedrive_ondemand/
  config.py     paths and settings
  rclone.py     builds the rclone command line; sign-in; our rclone.conf; rc client
  graph.py      a few direct Microsoft Graph calls (drive, folder, item lookup)
  mount.py      prepares the folder, starts rclone (or the offline mount); cleans up after it stops
  setup_wizard.py  `setup`: sign-in, folder, service, upload-location check
  cache.py      reads sidecars, works out file state, frees one file
  pins.py       SQLite pin rules and "effective state" lookup
  daemon.py     main loop: eviction, pin downloads, status, activity
  conflicts.py  conflict guard (§7)
  ipc.py        JSON messages over a unix socket (daemon <-> CLI/tray/Nemo)
  cli.py        command-line interface
  status.py     one-line status summary shared by CLI and tray
  tray.py       tray icon, progress window, settings and accounts dialog
  previews.py   which previews we made; deleting them again (§8)
  thumbnailer.py  the thumbnailer Nemo runs (§8)
  updates.py    checking for and installing updates (§14)
nemo/onedrive_ondemand_nemo.py   Nemo extension (batched, 1 s timeout: Nemo never waits long)
bin/            onedrive-ondemand launcher for running from the source folder
data/           icons, systemd unit templates (one copy per account), autostart and menu .desktop files
packaging/      build-deb.sh (dpkg-deb, no root needed)
tests/          unit tests (fake cache folder, fake home folder, no network)
.github/workflows/release.yml   builds the .deb and publishes a release for a v* tag
```

Requirements: Linux Mint 21+/22 (Python 3.10+), `python3-gi`,
`gir1.2-xapp-1.0`, `gir1.2-gdkpixbuf-2.0`, `nemo-python`, `fuse3`, `curl`,
`pkexec`, and rclone ≥ 1.75 installed from rclone.org. No pip packages.
The `.deb` doesn't depend on an `rclone` package: rclone.org's own install
script doesn't register with dpkg, so `rclone (>= 1.75)` as a dependency
would make apt try (and fail) to use Mint's old one. Setup checks the
version instead and offers to run rclone.org's installer.

**Packaging:** a `.deb` built by a simple script. **No Flatpak:** a Flatpak
sandbox can't create a system FUSE mount or install a Nemo extension, so it
would add work without working properly.

## 11. Milestones

| # | Deliverable | How you'll test it |
|---|---|---|
| **a** | `setup` (browser sign-in, creates our rclone config pointed at `LinuxClientTest`) + mount as a systemd user service. Verify the rclone facts marked *(verify)*. | Sign in, see the test folder in `~/OneDrive`, open/edit a file, see it change on onedrive.com. |
| **b** | Daemon + CLI: `pin`, `unpin`, `free`, `status`, eviction loop, pins following renames. | `onedrive-ondemand pin file`, check `status`, pull the network cable and open it. `free` it and watch disk usage drop. Rename a pinned file locally and on the website, and check it's still pinned. |
| **c** | Nemo extension: emblems + OneDrive submenu. | Right-click → OneDrive → Always keep on this device, watch the emblem change. |
| **d** | Tray icon: status, pause/resume, recent activity, eviction settings. | Pause, edit a file, confirm it doesn't upload, resume. |
| **e** | Autostart, `.deb`, README install/uninstall/limitations. | Install the .deb, reboot, everything starts on its own. |
| **f** | Conflict guard (§7). | Edit the same file on the web and locally, get both copies. |

## 12. Decisions from review (2026-10-30)

1. **Mint version:** a recent install of the Cinnamon edition, so almost
   certainly Mint 22.x (Ubuntu 24.04 base, Python 3.12). Confirm with
   `cat /etc/linuxmint/info` at the start of milestone (a).
2. **Defaults approved:** free unused files after 30 days, keep 10 % disk free.
3. **Thumbnails:** a one-time setup question, after checking whether Nemo
   already treats the mount as remote (§8).
4. **Pins must follow renames/moves:** in scope now, via OneDrive item IDs
   (§3), built in milestone (b).
5. **Changes after milestone (a) testing (2026-10-30, approved):**
   - Restrict to the test folder by mounting it by path, not via
     `root_folder_id`, plus a setup check of where uploads land (§9).
   - Make `~/OneDrive` writable just before mounting and read-only again
     after unmounting, because fusermount can't mount onto a read-only
     folder (§4).
6. **Version 0.2 (2026-10-30, asked for by the maintainer):** several
   accounts with one folder each, named like Windows (§13); transfer
   progress in the tray (§5); offline fixes (§4); previews from OneDrive's
   servers instead of downloading (§8); a guided `.deb` install and
   updates from GitHub (§14). Safety rule 1 still applies to every account:
   each one only shows its `LinuxClientTest` folder.

## 13. Several accounts (version 0.2)

Each account is a short id (`personal`, `work`) with its own:

- folder, `~/OneDrive - <name>` (like Windows' "OneDrive - Personal" and
  "OneDrive - Contoso"), also added to Nemo's sidebar,
- rclone config with its own sign-in, cache folder, pin database and
  sockets (`config.Account` has every path),
- pair of services from systemd **templates**:
  `onedrive-ondemand-mount@<id>.service` and
  `onedrive-ondemand-daemon@<id>.service` (`%i` is the id).

Running one daemon per account (instead of one daemon for all) keeps the
daemon code almost unchanged and keeps accounts independent: a problem with
one can't stop the other. The tray, Nemo extension and CLI find the right
daemon from the folder a file is in (`config.account_for_path`).

- **Add** (`add-account`, or Settings → Add an account): browser sign-in,
  Graph `/me/drive` tells personal from business, refuses an account that's
  already added (same drive ID), asks for a name, then does the same checks
  as setup (folder exists, uploads land in `LinuxClientTest`).
- **Sign out** (`remove-account`, or Settings → Sign out): like Windows'
  "Unlink this PC". Refuses (unless you confirm) while changes are waiting
  to upload. Stops and disables the services, then deletes that account's
  sign-in, settings, pins and downloaded copies. Nothing on OneDrive changes.
- Eviction and preview settings are the same for every account (the tray
  changes them for all).
- Adding a second account in the browser: Microsoft may sign you straight in
  to the first one. Setup says to pick "Use another account" or open the
  link in a private window.

## 14. Installing and updating (version 0.2)

- **First run after installing the `.deb`:** the tray starts at the next
  login (or from the menu → OneDrive). With no account set up it shows a
  notification and a "Set up OneDrive…" item; clicking it opens setup in a
  terminal window. Setup checks rclone (offering rclone.org's installer if
  it's missing or older than 1.75), then guides through sign-in, the
  account's name and previews. Version 0.1 users run `setup` once to move to
  the new layout (§4).
- **Updates:** the tray checks once a day, and on request ("Check for
  updates…"). Installed from the `.deb`: compares this version with the
  newest GitHub release, downloads its `.deb` and installs it with
  `pkexec apt-get install` (a password window). Running from a `git clone`:
  `git fetch` + `git pull --ff-only` (refused if the folder has its own
  changes). Either way it then restarts every account's services and the
  tray. Restarting the mount closes files open from OneDrive, so the tray
  says to close them first; changes waiting to upload are kept in the cache.
- **Releases:** pushing a `v*` tag makes GitHub Actions run the tests, build
  the `.deb` and publish a release (`.github/workflows/release.yml`).
