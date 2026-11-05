"""Folder emblems: a folder's state comes from the files inside it (DESIGN.md §3)."""

import json
import unittest
from unittest import mock

from onedrive_ondemand import daemon, graph
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


class ItemByPathTest(unittest.TestCase):
    def test_missing_folder_is_none(self):
        with mock.patch.object(graph, "_request", side_effect=graph.GraphError(404, "not found")):
            self.assertIsNone(graph.item_by_path("t", "d", "Pictures"))

    def test_asks_for_the_quoted_path(self):
        with mock.patch.object(graph, "_request", return_value={"size": 5}) as request:
            self.assertEqual(graph.item_by_path("t", "d1", "/My Pictures/2024/")["size"], 5)
        self.assertIn("/drives/d1/root:/My%20Pictures/2024?", request.call_args[0][2])


class DaemonFolderStateTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.addCleanup(self.home.cleanup)
        account = self.home.add_account("personal", "Personal")
        self.daemon = daemon.Daemon(account)
        self.addCleanup(self.daemon.store.close)
        self.daemon.mount.mkdir(parents=True, exist_ok=True)
        # Folder sizes "on OneDrive": every file made by add() is 10 bytes.
        self.sizes = {}
        patch = mock.patch.object(self.daemon, "folder_size", side_effect=self.ask_size)
        self.asked = patch.start()
        self.addCleanup(patch.stop)

    def ask_size(self, rel):
        return self.sizes.get(rel)

    def add(self, rel, downloaded):
        """A file in the (fake) mount; `downloaded` also puts all of it in the cache."""
        path = self.daemon.mount / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        parts = rel.split("/")
        for n in range(1, len(parts)):
            folder = "/".join(parts[:n])
            self.sizes[folder] = self.sizes.get(folder, 0) + 10
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
        self.assertEqual(self.state("cloud"), ("none", "online"))
        self.assertEqual(self.state("here")[1], "local")
        self.assertEqual(self.state("mixed")[1], "partial")
        self.assertEqual(self.state("mixed/sub")[1], "online")

    def test_nothing_downloaded_needs_no_network(self):
        self.add("cloud/a.txt", None)
        (self.daemon.mount / "empty").mkdir()
        self.assertEqual(self.state("cloud")[1], "online")
        self.assertEqual(self.state("empty")[1], "online")
        self.asked.assert_not_called()

    def test_offline_keeps_the_last_answer(self):
        self.add("mixed/a.txt", True)
        self.add("mixed/b.txt", None)
        self.assertEqual(self.state("mixed")[1], "partial")
        self.daemon.online = False
        self.daemon.forget_folder_states()
        self.asked.reset_mock()
        self.assertEqual(self.state("mixed"), ("partial", "partial"))
        self.asked.assert_not_called()

    def test_offline_mount_counts_files(self):
        self.add("here/a.txt", True)
        with mock.patch.object(self.daemon, "offline_mode", return_value=True):
            self.assertEqual(self.state("here")[1], "local")
        self.asked.assert_not_called()

    def test_upload_waiting_inside(self):
        self.add("docs/a.txt", True)
        self.daemon.queue["docs/a.txt"] = {"tries": 0}
        self.assertEqual(self.state("docs")[1], "syncing")

    def test_pin_or_free_makes_it_check_again(self):
        self.add("docs/a.txt", None)
        self.assertEqual(self.state("docs")[1], "online")
        self.sizes.clear()
        self.add("docs/a.txt", True)
        self.assertEqual(self.state("docs")[1], "online")  # still the remembered answer
        self.daemon.forget_folder_states()
        self.assertEqual(self.state("docs"), ("online", "local"))


if __name__ == "__main__":
    unittest.main()
