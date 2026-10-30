import os
import unittest

from onedrive_ondemand import cache
from tests.fakecache import FakeCache


class StateTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCache()
        self.c = self.fake.cache

    def tearDown(self):
        self.fake.cleanup()

    def test_full_partial_and_missing(self):
        self.fake.add("full.txt", size=100)
        self.fake.add("part.bin", size=1000, cached=100)
        self.assertTrue(cache.is_fully_cached(self.c.sidecar("full.txt")))
        self.assertFalse(cache.is_fully_cached(self.c.sidecar("part.bin")))
        self.assertIsNone(self.c.sidecar("missing.txt"))

    def test_empty_file_counts_as_full(self):
        self.fake.add("empty.txt", size=0, cached=0)
        self.assertTrue(cache.is_fully_cached(self.c.sidecar("empty.txt")))

    def test_null_ranges_right_after_reading(self):
        # rclone writes "Rs" a few seconds after close; until then it's null.
        self.assertEqual(cache.cached_bytes({"Size": 10, "Rs": None}), 0)

    def test_half_written_sidecar_is_ignored(self):
        _, meta = self.fake.add("a.txt")
        meta.write_text('{"Size": 1')
        self.assertIsNone(self.c.sidecar("a.txt"))

    def test_cached_files_lists_everything(self):
        self.fake.add("a.txt")
        self.fake.add("Projects/notes/b.txt")
        self.assertEqual(sorted(self.c.cached_files()), sorted(["a.txt", "Projects/notes/b.txt"]))
        self.assertEqual(list(self.c.cached_files("Projects")), ["Projects/notes/b.txt"])
        self.assertEqual(list(self.c.cached_files("a.txt")), ["a.txt"])

    def test_parse_nanosecond_time(self):
        t = cache.parse_time("2026-10-30T15:22:38.919735802+02:00")
        self.assertEqual((t.hour, t.microsecond, t.utcoffset().total_seconds()), (15, 919735, 7200))


class FreeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCache()
        self.c = self.fake.cache

    def tearDown(self):
        self.fake.cleanup()

    def test_free_removes_data_and_sidecar(self):
        data, meta = self.fake.add("Projects/a.txt")
        self.c.free("Projects/a.txt")
        self.assertFalse(data.exists())
        self.assertFalse(meta.exists())

    def test_refuses_file_waiting_to_upload(self):
        data, _ = self.fake.add("a.txt", dirty=True)
        with self.assertRaises(cache.CacheError):
            self.c.free("a.txt")
        self.assertTrue(data.exists())

    def test_refuses_if_opened_since_we_looked(self):
        data, _ = self.fake.add("a.txt")
        with self.assertRaises(cache.CacheError):
            self.c.free("a.txt", expected_atime="2020-01-01T00:00:00+00:00")
        self.assertTrue(data.exists())

    def test_refuses_paths_that_escape(self):
        for bad in ("../../OneDrive/a.txt", "/etc/passwd", ""):
            with self.assertRaises(cache.CacheError):
                self.c.free(bad)

    def test_refuses_symlink_into_onedrive_folder(self):
        # Even if something odd is in the cache, never delete inside ~/OneDrive.
        precious = self.fake.mount / "precious.txt"
        precious.write_text("keep me")
        self.fake.add("link.txt")
        data = self.c.data_file("link.txt")
        data.unlink()
        os.symlink(precious, data)
        with self.assertRaises(cache.CacheError):
            self.c.free("link.txt")
        self.assertTrue(precious.exists())

    def test_never_touches_the_mount_folder(self):
        # A cache placed inside the mount folder (misconfiguration) must not be emptied.
        inside = cache.Cache(self.fake.mount / "cache", "r", "f", self.fake.mount)
        (inside.data_root).mkdir(parents=True)
        (inside.meta_root).mkdir(parents=True)
        (inside.data_root / "a.txt").write_text("x")
        (inside.meta_root / "a.txt").write_text('{"Size": 1, "Rs": [{"Pos": 0, "Size": 1}]}')
        with self.assertRaises(cache.CacheError):
            inside.free("a.txt")
        self.assertTrue((inside.data_root / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
