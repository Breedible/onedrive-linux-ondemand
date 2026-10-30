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
rclone 1.75.0 showing **(verify)**:

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
  **(verify that OneDrive change polling is active)**.

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
