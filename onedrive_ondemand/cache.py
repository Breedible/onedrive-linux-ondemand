"""Reading rclone's cache folder: which files are on disk, and freeing them.

rclone keeps two trees side by side (DESIGN.md §4):

    cache/vfs/<remote>/<folder>/<path>        the downloaded bytes (sparse files)
    cache/vfsMeta/<remote>/<folder>/<path>    a small JSON "sidecar" per file

A sidecar looks like this (verified with rclone 1.75.2, RESEARCH.md §5):

    {"ModTime": "...", "ATime": "2026-10-30T15:22:38.919735802+02:00",
     "Size": 104, "Rs": [{"Pos": 0, "Size": 104}],
     "Fingerprint": "104,2026-10-30 13:22:38 +0000 UTC,<hash>", "Dirty": false}

"Rs" lists the byte ranges that are on disk, and "Dirty" means a local change
hasn't been uploaded yet. rclone writes "Rs" a few seconds after a file is
closed, so a file that was just read may briefly look not downloaded.

SAFETY: this module deletes files, but only inside the cache folder.
Deleting anything inside ~/OneDrive would delete it from OneDrive.
`free()` refuses any path that isn't safely inside the cache.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class CacheError(Exception):
    pass


def parse_time(text):
    """Parse rclone's timestamps, which have nanoseconds (Python only takes 6 digits)."""
    if not text:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def cached_bytes(sidecar):
    """How many bytes of the file are on disk, according to its sidecar."""
    return sum(r.get("Size", 0) for r in (sidecar.get("Rs") or []))


def is_fully_cached(sidecar):
    """True if every byte of the file is on disk."""
    size = sidecar.get("Size", 0)
    if size == 0:
        return True
    # rclone merges overlapping ranges, so adding them up is enough.
    return cached_bytes(sidecar) >= size


def clean_relative(rel):
    """Check a path relative to the OneDrive folder and return it in a standard form.

    Refuses absolute paths and ".." so nothing can point outside the cache.
    """
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts:
        raise CacheError(f"Unsafe path: {rel!r}")
    text = str(path)
    return "" if text == "." else text


class Cache:
    def __init__(self, cache_dir, remote_name, remote_folder, mount_path):
        # rclone stores a remote like "onedrive-ondemand:LinuxClientTest" under
        # vfs/onedrive-ondemand/LinuxClientTest/ (checked on disk 2026-10-30).
        sub = Path(remote_name) / remote_folder if remote_folder else Path(remote_name)
        self.data_root = Path(cache_dir) / "vfs" / sub
        self.meta_root = Path(cache_dir) / "vfsMeta" / sub
        self.mount_path = Path(mount_path)

    # --- reading -----------------------------------------------------------

    def data_file(self, rel):
        return self.data_root / clean_relative(rel)

    def sidecar_file(self, rel):
        return self.meta_root / clean_relative(rel)

    def sidecar(self, rel):
        """The sidecar for a file as a dict, or None if rclone has nothing cached."""
        try:
            with open(self.sidecar_file(rel), encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, ValueError):
            # ValueError: rclone may be halfway through rewriting it.
            return None
        return data if isinstance(data, dict) else None

    def cached_files(self, under=""):
        """Every file rclone has a sidecar for (paths relative to the OneDrive folder).

        `under` limits it to one folder, e.g. "Projects/notes".
        """
        top = self.meta_root / clean_relative(under) if under else self.meta_root
        if top.is_file():
            yield clean_relative(under)
            return
        for folder, _dirs, files in os.walk(top):
            for name in files:
                yield str((Path(folder) / name).relative_to(self.meta_root))

    def disk_bytes(self, rel):
        """Disk space really used by a cached file (it's sparse, so this can be small)."""
        try:
            return os.stat(self.data_file(rel)).st_blocks * 512
        except OSError:
            return 0

    # --- freeing -----------------------------------------------------------

    def _check_inside(self, path, root):
        """Refuse to delete anything that isn't strictly inside `root`."""
        real = Path(os.path.realpath(path))
        real_root = Path(os.path.realpath(root))
        real_mount = Path(os.path.realpath(self.mount_path))
        if real == real_root or not real.is_relative_to(real_root):
            raise CacheError(f"Refusing to delete {path}: it isn't inside the cache.")
        if real == real_mount or real.is_relative_to(real_mount):
            raise CacheError(f"Refusing to delete {path}: it is inside the OneDrive folder.")
        return real

    def free(self, rel, expected_atime=None):
        """Remove one file's downloaded copy, so it becomes online-only again.

        The file stays on OneDrive. rclone notices the removal and downloads it
        again next time it's opened (verified, RESEARCH.md §5).

        Re-reads the sidecar first and refuses if the file has a change waiting
        to upload, or (if `expected_atime` is given) was opened in the meantime.
        Returns the number of bytes of disk freed.
        """
        rel = clean_relative(rel)
        if not rel:
            raise CacheError("Refusing to free the whole cache at once.")
        sidecar = self.sidecar(rel)
        if sidecar is None:
            return 0
        if sidecar.get("Dirty"):
            raise CacheError(f"{rel} has changes that haven't uploaded yet.")
        if expected_atime is not None and sidecar.get("ATime") != expected_atime:
            raise CacheError(f"{rel} was opened just now.")

        data = self.data_file(rel)
        meta = self.sidecar_file(rel)
        freed = self.disk_bytes(rel)
        for path, root in ((data, self.data_root), (meta, self.meta_root)):
            if os.path.lexists(path):
                real = self._check_inside(path, root)
                if not real.is_file() or os.path.islink(path):
                    raise CacheError(f"Refusing to delete {path}: not a normal file.")
        # Delete the data first: a sidecar without data just means "not cached".
        for path in (data, meta):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return freed


def open_files(mount_path, data_root):
    """Paths (relative to the OneDrive folder) that some program has open right now.

    Looks at every process's open files in /proc. Catches files opened through
    ~/OneDrive and rclone's own open cache files.
    """
    mount = os.path.realpath(mount_path) + os.sep
    data = os.path.realpath(data_root) + os.sep
    found = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # the process ended, or isn't ours
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            for prefix in (mount, data):
                if target.startswith(prefix):
                    found.add(target[len(prefix):])
    return found


def now():
    return datetime.now(timezone.utc)
