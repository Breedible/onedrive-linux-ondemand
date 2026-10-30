# Design: onedrive-linux-ondemand

Status: **approved 2026-10-30** (see §12). Milestone (a) built 2026-10-30;
two small changes from testing are in §12. Research behind these
decisions is in [RESEARCH.md](RESEARCH.md).

## 1. What Linux can and cannot do (read this first)

Windows Files On-Demand uses a kernel filter driver (the "Cloud Files API")
that puts **placeholder files** on a normal NTFS disk. Each placeholder is a
real file entry whose contents are fetched when something opens it. Linux has
no such API. The closest equivalent is a **FUSE filesystem**: a program
(here, `rclone mount`) pretends to be a disk and answers file requests itself.

Consequences we have to live with:

| Windows | Our Linux version |
|---|---|
| `C:\Users\you\OneDrive` is a real folder on the disk. | `~/OneDrive` is a **mount point**. Files show there only while rclone is running. |
| If OneDrive.exe isn't running, local files are still there. | If the mount isn't running, `~/OneDrive` looks **empty**. Your downloaded and pinned data is safe on disk (see §4), just not visible in that folder. |
| Pin state is a file attribute stored in NTFS. | Pin state is kept in **our own small database**, because FUSE has nowhere to store it. |
| Every app sees the cloud and pin states. | Only **Nemo** shows emblems, via our extension. Other apps just see normal files. |
| Explorer knows not to download files just to draw thumbnails. | Nemo reads files to make thumbnails, so browsing a photo folder **downloads the photos**. See §8. |
| Built-in "keep both" conflict handling. | rclone uploads last-writer-wins. We add a best-effort guard (§7), and OneDrive's version history is the safety net. |

## 2. Architecture

Four small pieces of our own, with rclone doing the hard work:

```
                    +--------------------------+
 Nemo  ──────────►  | ~/OneDrive (FUSE mount)  |  ◄── any app opens/saves files
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
| **rclone mount** | Shows OneDrive at `~/OneDrive`, downloads on open, uploads on save, polls for remote changes. | rclone ≥ 1.75 from rclone.org. Runs as a `systemd --user` service with `Restart=on-failure`. |
| **daemon** | Remembers pins, downloads pinned items, frees space, works out each file's state, logs activity. | Python 3 standard library only (sqlite3, socket, json, threading). Separate systemd user service, so a daemon crash **never** unmounts your files. |
| **CLI** | `onedrive-ondemand pin/unpin/free/status/setup`. Used for testing and scripting. | Python, talks to the daemon. |
| **Nemo extension** | Emblems and the "OneDrive" right-click submenu. | `nemo-python`. Asks the daemon, with a short timeout so Nemo never freezes. |
| **Tray icon** | Status, pause/resume, recent activity, settings. | GTK 3 + `XApp.StatusIcon` (Cinnamon's native tray API). |

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
| Downloaded and pinned file contents | `~/.local/share/onedrive-ondemand/cache/vfs/<remote>/<path>` | rclone's `--cache-dir`, deliberately **not** `~/.cache`. Cleaning tools (BleachBit, "clear cache") treat `~/.cache` as disposable. These are normal files at their real paths, stored *sparse* (only downloaded byte ranges use disk). |
| rclone's per-file bookkeeping | `…/cache/vfsMeta/<remote>/<path>` | Small JSON per file: which bytes are cached, whether an upload is pending. |
| Pin rules, activity log | `~/.local/share/onedrive-ondemand/state.db` | SQLite. Losing it loses pin choices, not files. |
| Settings | `~/.config/onedrive-ondemand/config.json` | Eviction timer, free-space limit, mount path. |
| Sign-in token | `~/.config/onedrive-ondemand/rclone.conf` (mode 600) | Our **own** rclone config, separate from any `~/.config/rclone/rclone.conf` you already have. Never committed to git. |
| Logs | `journalctl --user -u onedrive-ondemand-mount -u onedrive-ondemand-daemon` | |

**What happens when things aren't running:**

- **Mount stopped (logged out, crashed, quit):** `~/OneDrive` is an empty
  folder. All downloaded/pinned data is still in the cache folder above.
  Edits that weren't uploaded yet are uploaded automatically the next time
  the mount starts. To stop programs accidentally saving into the empty
  folder (those files would be hidden once the mount comes back), the bare
  `~/OneDrive` folder is read-only while not mounted. fusermount can't mount
  onto a read-only folder, so the service makes it writable just before
  mounting and read-only again after rclone stops. rclone also refuses to
  mount over a non-empty folder.
- **Mount crashed hard:** the folder may show "Transport endpoint is not
  connected". The service restarts it automatically (it runs `fusermount -uz`
  first).
- **Daemon stopped:** files keep working normally. Pins aren't topped up,
  nothing is freed, and Nemo emblems disappear until it's back.
- **Offline:** cached and pinned files open and save normally, and saves
  queue for upload. Opening an online-only file fails with an I/O error.
  Folder listings come from rclone's directory cache.
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
end-to-end through `~/OneDrive` (two at a time), which makes rclone download
it fully. Pinned folders are re-scanned every 15 minutes (and after remote
changes) to catch new files. Before pinning a folder, the CLI/menu shows its
size and warns if it won't fit.

**Pause** (tray, `onedrive-ondemand pause`) uses rclone's
`vfs/queue-set-expiry`: while paused, the daemon pushes every waiting upload
a year into the future; resume sets them back to "now". An upload that has
already started finishes. Pin downloads also wait. Opening files still works.

## 6. Settings (defaults)

| Setting | Default |
|---|---|
| Mount path | `~/OneDrive` |
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

**Our guard (milestone f, experimental):** the daemon watches rclone's upload
queue. For each queued file it compares the OneDrive copy with the version
we last saw in sync (we record that from the sidecar while the file is clean,
because rclone blanks it once the file is modified). If OneDrive's copy
changed in the meantime, the daemon makes a **server-side copy** of the
OneDrive version, named `report (conflict copy 2026-10-30 1530).docx`,
before rclone uploads yours. That gives you both copies, and the tray shows a
notification. If rclone uploads before the guard runs (a short race), version
history is still the fallback. We will say "best effort" in the README, not
"guaranteed".

## 8. Other failure modes and limitations

| Situation | What happens / what we do |
|---|---|
| Nemo thumbnails | To show a small preview picture instead of a generic icon, Nemo reads the whole image/video, which **downloads it**. Setup asks once whether to allow that. First, milestone (c) checks whether Nemo's existing "Show thumbnails: Local files only" setting already treats the rclone mount as remote. If it does, nothing needs changing. |
| Network drop | rclone retries with back-off. Tray shows "Offline". Uploads resume automatically. |
| Sign-in expires (90 days unused, or password change) | Mount fails to talk to OneDrive. Tray shows "Sign in again", which re-runs the browser sign-in. |
| Throttling (HTTP 429) | rclone backs off automatically. Your own client ID reduces this (optional, later). |
| Disk full | Space rule frees unpinned files. If only pinned files remain, the tray warns you. |
| Deleting in `~/OneDrive` | **Deletes from OneDrive** (to the OneDrive recycle bin, kept 30 days), same as Windows. We never use permanent delete. |
| Invalid names (`:` `?` etc.), `a.txt` vs `A.txt` | rclone substitutes look-alike characters. Two names differing only by case can't both exist on OneDrive. |
| Huge folders (>50k items) | OneDrive limit. Listing may be slow or fail. |
| Two PCs (Linux + Windows dual boot) editing the same file | Treated as a remote change (§7). Keep the Linux cache on the Linux partition, not on a shared NTFS one. |
| Symlinks | Not supported by OneDrive. rclone can't store them. |
| OneDrive Business | rclone supports it (`drive_type business`). Setup will ask which kind. The design is account-agnostic, with one account in v1. |

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
  mount.py      prepares ~/OneDrive, starts rclone; cleans up after it stops
  setup_wizard.py  `setup`: sign-in, folder, service, upload-location check
  cache.py      reads sidecars, works out file state, frees one file
  pins.py       SQLite pin rules and "effective state" lookup
  daemon.py     main loop: eviction, pin downloads, status, activity
  ipc.py        JSON messages over a unix socket (daemon <-> CLI/tray/Nemo)
  cli.py        command-line interface
  tray.py       tray icon
nemo/onedrive_ondemand_nemo.py   Nemo extension
bin/            onedrive-ondemand launcher for running from the source folder
data/           emblem icons, systemd units, autostart .desktop file
packaging/      build-deb.sh
tests/          unit tests (fake cache folder, no network)
```

Requirements: Linux Mint 21+/22 (Python 3.10+), `python3-gi`,
`gir1.2-xapp-1.0`, `nemo-python`, `fuse3`, and rclone ≥ 1.75 installed from
rclone.org. No pip packages.

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
