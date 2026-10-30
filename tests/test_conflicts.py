import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from onedrive_ondemand import conflicts, pins
from tests.fakecache import FakeCache

WHEN = datetime(2026, 10, 30, 15, 30)


class HelpersTest(unittest.TestCase):
    def test_parse_fingerprint(self):
        self.assertEqual(conflicts.parse_fingerprint("10,2026-10-30 14:05:49 +0000 UTC,AbC1"), (10, "abc1"))
        self.assertIsNone(conflicts.parse_fingerprint(""))       # after an editor's save-by-rename
        self.assertIsNone(conflicts.parse_fingerprint("10,x,"))  # no hash

    def test_is_conflict(self):
        self.assertTrue(conflicts.is_conflict((10, "aa"), (12, "bb")))
        self.assertFalse(conflicts.is_conflict((10, "aa"), (10, "aa")))
        self.assertFalse(conflicts.is_conflict(None, (10, "bb")))   # unknown: let it upload
        self.assertFalse(conflicts.is_conflict((10, "aa"), None))   # new or deleted on OneDrive
        self.assertFalse(conflicts.is_conflict((10, "aa"), (10, "")))

    def test_conflict_name(self):
        self.assertEqual(conflicts.conflict_name("a/report.docx", WHEN),
                         "a/report (conflict copy 2026-10-30 1530).docx")
        self.assertEqual(conflicts.conflict_name("notes", WHEN), "notes (conflict copy 2026-10-30 1530)")
        self.assertEqual(conflicts.conflict_name("x.tar.gz", WHEN), "x (conflict copy 2026-10-30 1530).tar.gz")
        taken = {"r (conflict copy 2026-10-30 1530).txt"}
        self.assertEqual(conflicts.conflict_name("r.txt", WHEN, taken.__contains__),
                         "r (conflict copy 2026-10-30 1530 2).txt")


class FakeDaemon:
    def __init__(self, fake, store):
        self.fs = "remote:Test"
        self.cache = fake.cache
        self.store = store
        self.settings = {"paused": False}
        self.expiry = {}

    def set_expiry(self, item, seconds):
        self.expiry[item["id"]] = seconds


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCache()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pins.Store(Path(self.tmp.name) / "state.db")
        self.daemon = FakeDaemon(self.fake, self.store)
        self.guard = conflicts.ConflictGuard(self.daemon)
        self.remote = {}     # path -> (size, hash) currently on "OneDrive"
        self.copies = []

        def fake_rc(command, params=None, **_):
            if command == "operations/stat":
                version = self.remote.get(params["remote"])
                if version is None:
                    return {"item": None}
                return {"item": {"Size": version[0], "IsDir": False, "Hashes": {"quickxor": version[1]}}}
            if command == "operations/copyfile":
                self.copies.append(params["dstRemote"])
                return {}
            raise AssertionError(command)
        self.real_rc = conflicts.rclone.rc
        conflicts.rclone.rc = fake_rc

    def tearDown(self):
        conflicts.rclone.rc = self.real_rc
        self.store.close()
        self.tmp.cleanup()
        self.fake.cleanup()

    def edited_file(self, fingerprint):
        _, meta = self.fake.add("doc.txt", size=20, dirty=True)
        meta.write_text(meta.read_text().replace('"Fingerprint": "20,2026-10-30 13:22:38 +0000 UTC,abc"',
                                                 f'"Fingerprint": "{fingerprint}"'))

    def test_changed_elsewhere_makes_a_copy_then_uploads(self):
        self.edited_file("10,2026-10-30 13:00:00 +0000 UTC,aaaa")
        self.remote["doc.txt"] = (12, "bbbb")
        self.guard.check("doc.txt", {"id": 7})
        self.assertEqual(len(self.copies), 1)
        self.assertIn("doc (conflict copy", self.copies[0])
        self.assertEqual(self.daemon.expiry[7], 0)   # released
        self.assertEqual(self.store.recent(1)[0]["kind"], "conflict")

    def test_unchanged_elsewhere_just_uploads(self):
        self.edited_file("10,2026-10-30 13:00:00 +0000 UTC,aaaa")
        self.remote["doc.txt"] = (10, "aaaa")
        self.guard.check("doc.txt", {"id": 7})
        self.assertEqual(self.copies, [])
        self.assertEqual(self.daemon.expiry[7], 0)

    def test_save_by_rename_uses_recorded_baseline(self):
        self.edited_file("")                          # the editor replaced the file
        self.store.set_synced("doc.txt", 10, "aaaa")  # what we last saw in sync
        self.remote["doc.txt"] = (12, "bbbb")
        self.guard.check("doc.txt", {"id": 7})
        self.assertEqual(len(self.copies), 1)

    def test_no_baseline_means_no_copy(self):
        self.edited_file("")
        self.remote["doc.txt"] = (12, "bbbb")
        self.guard.check("doc.txt", {"id": 7})
        self.assertEqual(self.copies, [])
        self.assertEqual(self.daemon.expiry[7], 0)

    def test_paused_uploads_stay_held(self):
        self.daemon.settings["paused"] = True
        self.edited_file("10,x,aaaa")
        self.remote["doc.txt"] = (10, "aaaa")
        self.guard.check("doc.txt", {"id": 7})
        self.assertNotIn(7, self.daemon.expiry)

    def test_new_queue_items_are_held_once(self):
        self.edited_file("10,x,aaaa")
        self.remote["doc.txt"] = (10, "aaaa")
        done = threading.Event()
        self.guard.check = lambda rel, item: done.set()
        self.guard({"doc.txt": {"id": 7, "uploading": False}}, set())
        self.assertEqual(self.daemon.expiry[7], conflicts.HOLD_SECONDS)
        self.assertTrue(done.wait(2))
        self.daemon.expiry.clear()
        self.guard({"doc.txt": {"id": 7, "uploading": False}}, set())  # same item again
        self.assertEqual(self.daemon.expiry, {})

    def test_scan_records_clean_downloads(self):
        self.fake.add("clean.txt", size=20)
        self.guard.scan_sidecars()
        self.assertEqual(self.store.synced("clean.txt")["hash"], "abc")


if __name__ == "__main__":
    unittest.main()
