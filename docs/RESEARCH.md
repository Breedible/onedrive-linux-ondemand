# Research findings (October 2026)

Short summary of what was checked before designing anything. Items marked
**(verify)** come from docs or other people's experiments and must be
confirmed on a real Linux Mint machine during milestone (a).

## 1. rclone (the engine)

Latest release: **v1.75.2 (2026-10-09)**. Linux Mint's own `apt` package is
much older, so we install rclone from rclone.org instead.

What `rclone mount --vfs-cache-mode full` gives us:

| Need | rclone behaviour |
|---|---|
| Files visible but not downloaded | Yes. Directory listings come from OneDrive; file data is fetched on first read and stored in a *sparse* cache file. |
| Partial downloads | Yes. Only the byte ranges actually read are stored. |
| Uploads | A changed file is uploaded after it is closed and idle for `--vfs-write-back` (default 5 s). Failed uploads retry with back-off. |
| Survives restart | Files written but not yet uploaded are uploaded "next time rclone is run with the same flags". |
| Open files | "open files cannot be evicted from the cache". |
| Eviction | Only global rules: `--vfs-cache-max-age` (since last access, default 1 h), `--vfs-cache-max-size`, `--vfs-cache-min-free-space`. Least-recently-used first. |
| **Per-file pinning** | **Not supported.** There is no rc command to pin, prefetch or evict a single file. `vfs/forget` only clears the *directory listing* cache, not file data. |
| Cache location | `--cache-dir` (default `~/.cache/rclone`). Data in `vfs/<remote>/<path>`, a small JSON "sidecar" per file in `vfsMeta/<remote>/<path>`. |
| Remote control | `rclone mount --rc` exposes `vfs/queue` (upload queue), `vfs/stats`, `core/stats`, `vfs/forget`, `vfs/refresh`. |

The OneDriveUI project's research notes (see §4) describe experiments on
rclone 1.75.0 showing (all verified, see §5):

- The sidecar JSON tells us a file's state: `Rs` = which byte ranges are
  cached (full / partial / none) and `Dirty: true` = local change not yet
  uploaded.
- Reading a file end-to-end through the mount fully downloads it (this is
  how we "pin").
- Deleting both the cache file and its sidecar while rclone runs is detected
  ("detected external removal of cache file") and rclone recovers. rclone
  1.75.0's changelog also says it now "recreates the cache file if it was
  removed". This is how we "free up space" for one file.

### Conflicts

rclone's VFS does **not** create "keep both" conflict copies. Since a 2021
fix it will not throw away a locally modified file when the remote changes,
but when it uploads, the local version replaces the remote one
(last-writer-wins). OneDrive keeps the previous version in its **version
history**, so the other edit is recoverable but not obvious. See the design
doc for how we add a best-effort guard on top.

## 2. Microsoft OneDrive / Graph API limits (via rclone docs)

- Max file size 250 GiB; full path must be under 400 characters.
- ~50,000 items per folder is fine; listing errors around 100,000.
- Case-insensitive names (`a.txt` and `A.txt` cannot coexist).
- Characters `" * : < > ? \ |` are not allowed; rclone substitutes look-alike
  Unicode characters automatically.
- Throttling: Microsoft returns HTTP 429; rclone backs off automatically. A
  personal client ID reduces throttling risk.
- **Refresh tokens expire after 90 days of non-use** → you have to sign in
  again if the client isn't run for 3 months.
- Every upload creates a new version that counts against quota.
- Deletes go to the OneDrive recycle bin (unless `--onedrive-hard-delete`,
  which we will never use).
- Remote-change detection: rclone polls (`--poll-interval`, default 1 min)
  (verified: §5).

### Own client ID

rclone ships a shared client ID, which works but may be throttled when many
people use it. Registering your own app needs an **Azure account**: with a
personal Microsoft account you sign up for a free Azure subscription (with
identity verification), which creates a directory you can register apps in.
For a public open-source project the client ID is not a secret anyway.
**Decision:** start with rclone's built-in ID; add an optional "use my own
client ID" setting with a walkthrough later.

## 3. Alternatives evaluated

| Project | What it is | Verdict |
|---|---|---|
| **abraunegg/onedrive** (D, GPL-3, v2.5.12 Oct 2026, very active, ~13k ★) | Mature full **sync** client: Personal, Business, SharePoint, real-time monitor, safe conflict backups. | Excellent, but its README lists Files On-Demand under "What's missing". Everything is downloaded, so the three-state model is impossible. Wrong shape for this project. |
| **onedriver** (Go, GPL-3, v0.15.0 Jan 2026, ~2.5k ★) | A real OneDrive filesystem: on-demand download, offline read-only mode, local wins on conflict. | Closest in spirit, but: Go not Python, no "keep on this device" pinning, no Nemo emblems, files loaded into memory (poor for big files), occasional hangs, slow release cadence (one release in ~2.5 years). Forking means learning Go and a FUSE codebase. |
| **OneDriveUI** (Python/Qt, created Sep 2026, 2 ★, last push 2026-09-05) | Ambitious Windows-11 clone on rclone with pinning via its own pin table. | Same core idea as ours, and its research notes are valuable. But: alpha (its own README says tests fail, "don't trust it with anything you cannot lose"), its notes mention a real file being deleted during development, very large codebase, Nautilus/GNOME only (no Nemo), Qt not GTK, inactive since early September, and **no LICENSE file in the repo** (the README says MIT), so we cannot safely copy code. |
| **rclone mount** directly | The engine. | Use it. Lacks pinning and conflict copies, which we add on top. |

## 4. Recommendation: start fresh, on rclone

Start a new, small Python project that **drives rclone** rather than forking:

1. abraunegg and onedriver would need fundamental changes (on-demand in
   one, pinning plus Nemo plus a language switch in the other).
2. OneDriveUI is the right idea, but it is too big and unfinished to learn
   from as a beginner, targets a different desktop, and its licence status is
   unclear. We **reuse its documented findings** (credited) and verify them
   ourselves, without copying code.
3. Small and readable is a requirement. Our own code is only the thin layer
   rclone lacks: pin list, eviction, status, Nemo, tray.

## 5. Verified on the maintainer's machine (milestone a, 2026-10-30)

Linux Mint 22.3 Cinnamon, Python 3.12.3, rclone **v1.75.2** (the `.deb`
from rclone.org), fuse3 3.14, personal OneDrive account. VFS behaviour was
first tested against a local folder (no OneDrive involved), then on the real
mount of `LinuxClientTest`.

| Claim | Result |
|---|---|
| Sidecar JSON shows cached ranges (`Rs`) and pending upload (`Dirty`) | **Confirmed.** A 100 KB read of a 3 MB file gave `Rs: [{Pos: 0, Size: 1044480}]` (rclone reads ahead about 1 MB). A full read gave one range covering the whole size. A local edit sets `Dirty: true`, then `false` once uploaded. **Catch:** `Rs` is written a few seconds *after* the file is closed; straight after a read it can still be `null`. The daemon must not treat a just-read file as online-only. |
| Cache files are sparse | **Confirmed.** After the partial read the cache file used 252 KB of disk for an apparent size of 4.9 MB. |
| Reading end-to-end fully downloads (how we pin) | **Confirmed.** The cache file was byte-identical to the original. |
| Deleting cache file + sidecar while mounted is detected and recovers (how we free one file) | **Confirmed.** Log: `vfs cache: detected external removal of cache file`. The next read re-downloaded it correctly. |
| Edits not yet uploaded survive a restart | **Confirmed.** Stopped rclone with an edit still queued (`--vfs-write-back 1h`); on the next start it was queued again and uploaded. |
| rclone unmounts by itself on SIGTERM | **Confirmed.** So `systemctl --user stop` unmounts cleanly. |
| Upload after save | **Confirmed** on OneDrive: queued, then `upload succeeded try #1` about 5 s later. `vfs/queue` lists the waiting file with `tries` and `delay`. |
| OneDrive change polling is active | **Confirmed.** A file created directly on OneDrive (Graph API, not through the mount) appeared in `~/OneDrive` after **34 s**, with `--poll-interval 1m` and `--dir-cache-time 1000h`, so polling (not cache expiry) refreshed the listing. |
| rc API on a unix socket | **Works** with `--rc-addr unix:///run/user/1000/onedrive-ondemand/rclone.sock`. **Catch:** Linux limits socket paths to about 108 characters; a longer path fails with `bind: invalid argument`. |
| Service restarts after a crash | **Confirmed.** After `kill -9` of rclone, systemd restarted it within about 10 s and the folder was mounted again. |
| Item ID format (needed for milestone b) | rclone's `operations/stat` returns `ID` as **`<driveID>#<itemID>`**, e.g. `0123456789ABCDEF#0123456789ABCDEF!s0a1b2c...` (made-up example). Graph's `GET /me/drive/items/{id}` accepts the part **after `#`**. Personal account only so far; Business not yet tested. |

### Surprises (and what we changed)

1. **fusermount refuses a read-only mount folder.** Mounting onto a `555`
   folder fails: `fusermount3: user has no write access to mountpoint`.
   The design's "make the bare `~/OneDrive` read-only" still works, but the
   service now makes the folder writable just before mounting and read-only
   again after rclone stops (`mount.py`). Confirmed: when stopped, saving
   into `~/OneDrive` gives "Permission denied".
2. **`root_folder_id` does not restrict uploads.** Setup first set
   `root_folder_id` in our rclone config to the `LinuxClientTest` folder's ID.
   Listings respected it, but **a new file saved through the mount was
   uploaded to the top level of the drive.** (We moved it back; nothing else
   was affected.) Probably rclone builds upload paths from the drive root and
   only uses the folder ID for listings. **Fix:** mount the folder by path,
   `onedrive-ondemand:LinuxClientTest`, and drop `root_folder_id`. Verified:
   new files, new subfolders, nested files and edits all landed inside
   `LinuxClientTest`, and nothing appeared at the top level. Setup now also
   saves a hidden test file through the mount on every run and checks with
   Graph where it arrived. If it's not in the chosen folder, setup turns the
   mount off.
   **Honest limit:** neither approach is a hard lock. The sign-in token can
   reach the whole OneDrive (Microsoft has no "one folder only" permission for
   this kind of app). The protection is that rclone only ever gets paths
   inside the folder, and the setup check catches it if that fails.
3. **Graph oddity:** the first `POST /me/drive/root/children` (create
   `LinuxClientTest`) answered `404 Item not found`, but the folder **was**
   created. Setup now addresses the top folder by its real ID instead of the
   `root` shortcut, and with `conflictBehavior: fail` a retry never makes a
   duplicate. (Not re-tested yet, since the folder now exists.)
4. **`rclone authorize onedrive`** (v1.75.2) printed the token JSON between
   the `--->` / `<---End paste` markers, and the sign-in worked first time.

## 6. Milestone (b) results (2026-10-30)

Tested live in `LinuxClientTest/_dev-tests` (never in the maintainer's own
files):

- **Pin a folder:** an online-only folder of 3 files (6.7 MB) showed
  "syncing", then "always kept" about 10 s later.
- **Free up space** on a folder and on one file inside a pinned folder: the
  cache files went; the exception (`online` rule) held through the next
  upkeep pass.
- **Pins follow renames:** a pinned file and a pinned folder renamed through
  `~/OneDrive`, and a pinned file renamed directly on OneDrive, were all
  followed (Graph lookup by item ID), including an exception rule inside the
  renamed folder. A rename made on OneDrive shows up in `~/OneDrive` after
  about 12 s; rclone treats it as a new file, so the pin downloads it again.
- **Deleted pinned file:** detected (Graph 404) and its rule kept as
  "deleted" for 30 days.
- **Pause:** with uploads held via `vfs/queue-set-expiry`, an edit stayed
  local for 15 s (OneDrive still had the old size), then uploaded within
  10 s of resume.
- **Eviction respects pins:** with "keep 99 % free" forced, the upkeep pass
  freed 4 unpinned files and kept the pinned ones.
- Graph's `parentReference.path` returns names with spaces as-is (not
  percent-encoded).
- Not tested yet: pulling the network cable (offline behaviour), and a
  OneDrive Business account.

## 7. Milestone (c) results (2026-10-30)

- **Nemo emblem names:** Nemo shows the icon `emblem-<name>` for
  `add_emblem("<name>")` (nemo-file.c, `nemo_file_get_emblem_icons`).
- **nemo-python** supports `update_file_info_full` (async answers), so the
  extension collects requests for 30 ms and asks the daemon once per batch.
  500 status lookups took 0.03 s.
- Right after the mount restarts, a status request can take over 1 s
  because the folder listing has to come from OneDrive. Nemo tells the
  extension which items are folders, and it passes that on so the daemon
  doesn't have to look through the mount.
- **Thumbnails:** `gio info -f ~/OneDrive` gives `filesystem::type: fuse`,
  `filesystem::remote: FALSE`. GLib only counts `cifs coda fuse.sshfs
  ncpfs nfs4` (and a few others) as remote, and rclone's Linux mount
  rejects `-o subtype=...` ("-o/--option not supported with this FUSE
  backend"), so we can't pose as sshfs. Nemo's per-folder
  `metadata::show-thumbnails` switch, with `inherit-show-thumbnails`, does
  the job (nemo-file.c, `nemo_file_should_show_thumbnail`). The metadata
  survives a remount.

## 8. Milestone (d) results (2026-10-30)

- `XApp.StatusIcon` registers on D-Bus (`org.x.StatusIcon.*`) and shows in
  Cinnamon's `xapp-status` applet. **Catch:** the panel only finds icons
  added to the theme *after* Cinnamon started once Cinnamon restarts. Until
  then the icon showed as a blank placeholder. Passing the SVG's full path as
  the icon name works straight away (other apps do the same).
- Checked through D-Bus: the icon and tooltip follow the daemon
  (Paused ↔ Up to date) within the 2 s update.
- Pause from the tray menu worked (simulated click via XTest).
- One tray at a time: a second start sees the lock in
  `/run/user/<uid>/onedrive-ondemand/tray.lock` and exits.

## Sources

- rclone mount docs: https://rclone.org/commands/rclone_mount/
- rclone OneDrive backend: https://rclone.org/onedrive/
- rclone rc API: https://rclone.org/rc/
- rclone changelog: https://rclone.org/changelog/
- rclone feature overview: https://rclone.org/overview/
- abraunegg/onedrive: https://github.com/abraunegg/onedrive
- onedriver: https://github.com/jstaf/onedriver
- OneDriveUI: https://github.com/Danielxpj/OneDriveUI (see `docs/research/rclone-mount-vfs.md`)
- rclone forum, VFS conflict discussion: https://forum.rclone.org/t/mount-over-sftp-and-vfs-cache-mode-full-causing-data-corruption/19844
- Entra supported account types: https://learn.microsoft.com/en-us/troubleshoot/azure/entra/entra-id/app-integration/error-code-aadsts50020-user-account-identity-provider-does-not-exist
