"""Conflict guard: keep both versions when a file changed in two places (DESIGN.md §7).

The problem: you edit a file here, and before your change uploads, the same
file was also changed on OneDrive (another PC, the website, a phone). rclone
would simply upload yours over theirs.

What the guard does, for every file that appears in rclone's upload queue:
1. Hold the upload for a moment (rclone's vfs/queue-set-expiry).
2. Compare OneDrive's current version with the version your edit started
   from (its "baseline").
3. If OneDrive's version changed in between, copy it on OneDrive to
   "name (conflict copy 2026-10-30 1530).ext", so both versions are kept.
4. Let the upload go.

Where the baseline comes from (checked with rclone 1.75.2, RESEARCH.md §9):
- Normally rclone's sidecar keeps the fingerprint of the OneDrive version
  ("size,modtime,quickxorhash") even while your change waits to upload.
- But many editors save by writing a temporary file and renaming it over the
  original, which leaves an empty fingerprint. For those, we use our own
  record of the last version we saw in sync (the `synced` table), updated
  after every upload and whenever rclone downloads a file.

This is best effort: if the guard can't decide (no baseline, offline), the
upload goes ahead as rclone would do anyway, and OneDrive's version history
still has the other version.
"""

import os
import threading
import time
from datetime import datetime
from pathlib import PurePosixPath

from onedrive_ondemand import rclone

HOLD_SECONDS = 120     # an upload waits at most this long for the check
SCAN_SECONDS = 15      # how often new downloads are recorded as baselines


def parse_fingerprint(text):
    """rclone's "size,modtime,hash" fingerprint -> (size, hash), or None if empty."""
    parts = (text or "").split(",")
    if len(parts) != 3 or not parts[2]:
        return None
    try:
        return int(parts[0]), parts[2].lower()
    except ValueError:
        return None


def is_conflict(baseline, remote):
    """True if OneDrive's version (size, hash) differs from the one the edit started from.

    Only a real hash difference counts; anything unknown means "no conflict".
    """
    if baseline is None or remote is None or not baseline[1] or not remote[1]:
        return False
    return baseline[1] != remote[1]


def conflict_name(rel, when, exists=lambda name: False):
    """"a/report.docx" -> "a/report (conflict copy 2026-10-30 1530).docx" (unused name)."""
    path = PurePosixPath(rel)
    stamp = when.strftime("%Y-%m-%d %H%M")
    # Keep double extensions like ".tar.gz" together.
    suffix = "".join(path.suffixes[-2:]) if path.suffixes[-2:] == [".tar", ".gz"] else path.suffix
    stem = path.name[: len(path.name) - len(suffix)] if suffix else path.name
    number = 1
    while True:
        extra = f" {number}" if number > 1 else ""
        candidate = str(path.with_name(f"{stem} (conflict copy {stamp}{extra}){suffix}"))
        if not exists(candidate):
            return candidate
        number += 1


class ConflictGuard:
    """Plugs into the daemon's upload-queue poll (Daemon.upload_hooks)."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.checked = {}        # path -> queue item id we've already checked
        self.last_scan = 0.0     # time.monotonic() of the last sidecar scan
        self.last_scan_wall = 0.0  # the same moment as a file time (time.time())

    def __call__(self, queue, finished):
        for rel in finished:
            threading.Thread(target=self.record_remote, args=(rel,), daemon=True).start()
        for rel, item in queue.items():
            if item.get("uploading") or self.checked.get(rel) == item["id"]:
                continue
            self.checked[rel] = item["id"]
            self.daemon.set_expiry(item, HOLD_SECONDS)  # hold it while we check
            threading.Thread(target=self.check, args=(rel, item), daemon=True).start()
        for rel in list(self.checked):
            if rel not in queue:
                del self.checked[rel]
        if time.monotonic() - self.last_scan > SCAN_SECONDS:
            self.scan_sidecars()

    # --- baselines ------------------------------------------------------------

    def remote_version(self, rel):
        """OneDrive's current (size, quickxor hash) of a file, or None if it isn't there."""
        answer = self.daemon.rc("operations/stat", {"fs": self.daemon.fs, "remote": rel,
                                               "opt": {"showHash": True}})
        item = answer.get("item")
        if not item or item.get("IsDir"):
            return None
        return item.get("Size"), (item.get("Hashes") or {}).get("quickxor", "").lower()

    def record_remote(self, rel):
        """After our upload, OneDrive's version is ours: remember it as in sync."""
        try:
            version = self.remote_version(rel)
        except rclone.RcError:
            return
        if version and version[1]:
            self.daemon.store.set_synced(rel, version[0], version[1])

    def scan_sidecars(self):
        """Record the fingerprint of every clean file rclone (re)downloaded since the last scan."""
        started = time.time()
        meta_root = self.daemon.cache.meta_root
        for folder, _dirs, files in os.walk(meta_root):
            for name in files:
                path = os.path.join(folder, name)
                try:
                    if os.stat(path).st_mtime < self.last_scan_wall:
                        continue  # unchanged since the last scan
                except OSError:
                    continue
                rel = os.path.relpath(path, meta_root)
                sidecar = self.daemon.cache.sidecar(rel)
                if sidecar and not sidecar.get("Dirty"):
                    version = parse_fingerprint(sidecar.get("Fingerprint"))
                    if version:
                        self.daemon.store.set_synced(rel, version[0], version[1])
        self.last_scan = time.monotonic()
        self.last_scan_wall = started

    def baseline(self, rel):
        """The OneDrive version this edit started from, as (size, hash), or None."""
        sidecar = self.daemon.cache.sidecar(rel)
        version = parse_fingerprint(sidecar.get("Fingerprint")) if sidecar else None
        if version:
            return version
        row = self.daemon.store.synced(rel)
        return (row["size"], row["hash"]) if row else None

    # --- the check --------------------------------------------------------------

    def check(self, rel, item):
        try:
            remote = self.remote_version(rel)
            if is_conflict(self.baseline(rel), remote):
                self.keep_other_version(rel)
        except rclone.RcError as error:
            print(f"Conflict check for {rel} failed ({error}); uploading anyway.", flush=True)
        finally:
            if not self.daemon.settings.get("paused"):
                self.daemon.set_expiry(item, 0)  # upload now

    def keep_other_version(self, rel):
        """Copy OneDrive's version to a "conflict copy" name, on OneDrive itself."""
        def exists(name):
            return self.daemon.rc("operations/stat", {"fs": self.daemon.fs, "remote": name}).get("item") is not None

        name = conflict_name(rel, datetime.now(), exists)
        self.daemon.rc("operations/copyfile", {"srcFs": self.daemon.fs, "srcRemote": rel,
                                           "dstFs": self.daemon.fs, "dstRemote": name}, timeout=300)
        self.daemon.store.log("conflict", rel,
                              f"{rel} was changed here and somewhere else. Both are kept: "
                              f"the other version is now {PurePosixPath(name).name}")
