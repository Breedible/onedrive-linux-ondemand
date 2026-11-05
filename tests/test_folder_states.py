"""Folder emblems: a folder's state comes from the files inside it (DESIGN.md §3)."""

import json
import unittest

from onedrive_ondemand import daemon
from onedrive_ondemand.daemon import folder_contents_state
from tests.fakehome import FakeHome


class FolderContentsStateTest(unittest.TestCase):
    def test_nothing_downloaded(self):
        self.assertEqual(folder_contents_state(5, 0, 0, 0), "online")

    def test_everything_downloaded(self):
        self.assertEqual(folder_contents_state(5, 5, 0, 0), "local")

    def test_some_downloaded(self):
        self.assertEqual(folder_contents_state(5, 2, 0, 0), "partial")

    def test_empty_folder_counts_as_here(self):
        self.assertEqual(folder_contents_state(0, 0, 0, 0), "local")

    def test_uploads_and_errors_come_first(self):
        self.assertEqual(folder_contents_state(5, 2, 1, 0), "syncing")
        self.assertEqual(folder_contents_state(5, 2, 1, 1), "error")


class DaemonFolderStateTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.addCleanup(self.home.cleanup)
        account = self.home.add_account("personal", "Personal")
        self.daemon = daemon.Daemon(account)
        self.addCleanup(self.daemon.store.close)
        self.daemon.mount.mkdir(parents=True, exist_ok=True)

    def add(self, rel, downloaded):
        """A file in the (fake) mount; `downloaded` also puts all of it in the cache."""
        path = self.daemon.mount / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        if downloaded is not None:
            meta = self.daemon.cache.meta_root / rel
            meta.parent.mkdir(parents=True, exist_ok=True)
            cached = 10 if downloaded else 4
            meta.write_text(json.dumps({"Size": 10, "Rs": [{"Pos": 0, "Size": cached}],
                                        "Dirty": False}))

    def state(self, rel):
        """Ask like Nemo does, let the background pass run, then ask again."""
        first = self.daemon.state_of(rel, [], is_dir=True)
        self.daemon.folder_loop()
        return first, self.daemon.state_of(rel, [], is_dir=True)

    def test_states_of_folders(self):
        self.add("cloud/a.txt", None)
        self.add("cloud/b.txt", False)        # partly read: still online-only
        self.add("here/a.txt", True)
        self.add("here/sub/b.txt", True)
        self.add("mixed/a.txt", True)
        self.add("mixed/sub/b.txt", None)
        (self.daemon.mount / "empty").mkdir()
        self.assertEqual(self.state("cloud"), ("none", "online"))
        self.assertEqual(self.state("here")[1], "local")
        self.assertEqual(self.state("mixed")[1], "partial")
        self.assertEqual(self.state("mixed/sub")[1], "online")
        self.assertEqual(self.state("empty")[1], "local")

    def test_upload_waiting_inside(self):
        self.add("docs/a.txt", True)
        self.daemon.queue["docs/a.txt"] = {"tries": 0}
        self.assertEqual(self.state("docs")[1], "syncing")

    def test_pin_or_free_makes_it_check_again(self):
        self.add("docs/a.txt", None)
        self.assertEqual(self.state("docs")[1], "online")
        self.add("docs/a.txt", True)
        self.assertEqual(self.state("docs")[1], "online")  # still the remembered answer
        self.daemon.forget_folder_states()
        self.assertEqual(self.state("docs"), ("online", "local"))


if __name__ == "__main__":
    unittest.main()
