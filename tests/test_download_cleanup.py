"""Old copies of files changed elsewhere, and files Nemo downloaded only to tell their type."""

import json
import os
import time
import unittest
from unittest import mock

from onedrive_ondemand import cache as cachemod
from onedrive_ondemand import daemon
from tests.fakehome import FakeHome

WHEN = 1793897309                    # 2026-11-05 16:48:29 UTC
WHEN_TEXT = "2026-11-05 16:48:29 +0000 UTC"


class DownloadedVersionTest(unittest.TestCase):
    def test_reads_size_and_time(self):
        self.assertEqual(cachemod.downloaded_version({"Fingerprint": f"36,{WHEN_TEXT},e5b0"}), (36, WHEN))

    def test_fractions_and_other_time_zones(self):
        sidecar = {"Fingerprint": "10,2026-11-05 18:48:29.919735802 +0200 SAST,abc"}
        self.assertEqual(cachemod.downloaded_version(sidecar), (10, WHEN))

    def test_unknown(self):
        self.assertIsNone(cachemod.downloaded_version({"Fingerprint": ""}))
        self.assertIsNone(cachemod.downloaded_version({}))


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.addCleanup(self.home.cleanup)
        account = self.home.add_account("personal", "Personal")
        self.d = daemon.Daemon(account)
        self.addCleanup(self.d.store.close)
        self.d.mount.mkdir(parents=True, exist_ok=True)
        self.d.rclone_ok, self.d.online = True, True
        patch = mock.patch.object(daemon.cachemod, "open_files", return_value=set())
        self.open_files = patch.start()
        self.addCleanup(patch.stop)

    def add(self, rel, size=36, cached=None, fingerprint=None, onedrive=(36, WHEN), dirty=False, age=0):
        """A file shown in the mount as OneDrive has it, and a cached copy of it."""
        shown = self.d.mount / rel
        shown.parent.mkdir(parents=True, exist_ok=True)
        shown.write_bytes(b"x" * onedrive[0])
        os.utime(shown, (onedrive[1], onedrive[1]))
        cached = size if cached is None else cached
        data, meta = self.d.cache.data_file(rel), self.d.cache.sidecar_file(rel)
        for path in (data, meta):
            path.parent.mkdir(parents=True, exist_ok=True)
        data.write_bytes(b"y" * cached)
        meta.write_text(json.dumps({
            "Size": size, "Rs": [{"Pos": 0, "Size": cached}] if cached else None, "Dirty": dirty,
            "ATime": "2026-11-05T18:00:00+02:00",
            "Fingerprint": fingerprint if fingerprint is not None else f"{size},{WHEN_TEXT},abc"}))
        if age:
            os.utime(meta, (time.time() - age, time.time() - age))

    def cached(self, rel):
        return self.d.cache.sidecar(rel) is not None

    # --- changed on another device -------------------------------------------

    def test_old_copy_of_a_changed_file_is_removed(self):
        self.add("a.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727")
        self.add("same.txt")
        self.assertEqual(self.d.remove_outdated_copies(), 1)
        self.assertFalse(self.cached("a.txt"))
        self.assertTrue(self.cached("same.txt"))
        self.assertIn("a.txt", self.d.store.recent()[0]["message"])

    def test_own_changes_and_open_files_are_kept(self):
        self.add("mine.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727", dirty=True)
        self.add("open.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727")
        self.open_files.return_value = {"open.txt"}
        self.assertEqual(self.d.remove_outdated_copies(), 0)
        self.assertTrue(self.cached("mine.txt") and self.cached("open.txt"))

    def test_nothing_happens_offline_or_without_a_known_version(self):
        self.add("a.txt", size=18, fingerprint="")
        self.assertEqual(self.d.remove_outdated_copies(), 0)
        self.add("b.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727")
        self.d.online = False
        self.assertEqual(self.d.remove_outdated_copies(), 0)

    def test_never_twice_for_the_same_version(self):
        self.add("a.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727")
        self.assertEqual(self.d.remove_outdated_copies(), 1)
        # Downloaded again, but (say) rclone records the time differently:
        self.add("a.txt", size=18, fingerprint=f"18,2026-11-05 16:48:19 +0000 UTC,7727")
        self.assertEqual(self.d.remove_outdated_copies(), 0)

    # --- downloaded only to tell the type ---------------------------------------

    def test_type_check_downloads_are_freed(self):
        self.add("pics/old.png", age=3600)              # downloaded long before
        self.d.cmd_viewing({"folders": [str(self.d.mount / "pics")]})
        self.add("pics/small.png")                       # read in full by the type check
        self.add("pics/big.png", size=9_000_000, cached=600_000, onedrive=(9_000_000, WHEN))
        self.add("pics/partly.png", size=9_000_000, cached=65_536, onedrive=(9_000_000, WHEN))
        self.add("pics/open.png")
        self.open_files.return_value = {"pics/open.png"}
        self.d.undo_type_checks()                        # too early: nothing yet
        self.assertTrue(self.cached("pics/small.png"))
        when, before = self.d.just_opened["pics"]
        self.d.just_opened["pics"] = (when - daemon.TYPE_CHECK_SECONDS, before)
        self.d.undo_type_checks()
        self.assertEqual({rel: self.cached(f"pics/{rel}") for rel in
                          ("old.png", "small.png", "big.png", "partly.png", "open.png")},
                         {"old.png": True, "small.png": False, "big.png": True,
                          "partly.png": False, "open.png": True})

    def test_only_in_the_first_minute(self):
        self.d.cmd_viewing({"folders": [str(self.d.mount / "pics")]})
        when, before = self.d.just_opened["pics"]
        self.d.just_opened["pics"] = (when - daemon.TYPE_CHECK_WINDOW - 1, before)
        self.d.undo_type_checks()
        self.add("pics/opened-later.png")                # you opened it after the minute
        self.d.undo_type_checks()
        self.assertTrue(self.cached("pics/opened-later.png"))
        self.d.cmd_viewing({"folders": []})              # left the folder
        self.assertEqual(self.d.just_opened, {})


if __name__ == "__main__":
    unittest.main()
