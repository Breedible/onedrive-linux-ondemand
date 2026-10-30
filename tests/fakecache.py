"""Builds a fake rclone cache folder for tests (no rclone, no network)."""

import json
import os
import tempfile
from pathlib import Path

from onedrive_ondemand.cache import Cache


class FakeCache:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache_dir = root / "cache"
        self.mount = root / "OneDrive"
        self.mount.mkdir()
        self.cache = Cache(self.cache_dir, "onedrive-ondemand", "LinuxClientTest", self.mount)

    def add(self, rel, size=100, cached=None, dirty=False, atime="2026-10-30T15:22:38.919735802+02:00"):
        """Pretend rclone has `cached` bytes (default: all) of a `size`-byte file."""
        cached = size if cached is None else cached
        data = self.cache.data_root / rel
        meta = self.cache.meta_root / rel
        data.parent.mkdir(parents=True, exist_ok=True)
        meta.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(b"x" * cached)
        sidecar = {"ModTime": atime, "ATime": atime, "Size": size,
                   "Rs": [{"Pos": 0, "Size": cached}] if cached else None,
                   "Fingerprint": f"{size},2026-10-30 13:22:38 +0000 UTC,abc", "Dirty": dirty}
        meta.write_text(json.dumps(sidecar))
        return data, meta

    def cleanup(self):
        self.tmp.cleanup()
