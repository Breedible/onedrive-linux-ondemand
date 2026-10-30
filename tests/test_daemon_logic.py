import unittest
from datetime import datetime, timedelta, timezone

from onedrive_ondemand import graph
from onedrive_ondemand.daemon import Candidate, choose_evictions, file_state, human

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
GB = 1024 ** 3


def cand(rel, days_ago, disk=GB):
    t = NOW - timedelta(days=days_ago)
    return Candidate(rel, t, t.isoformat(), disk)


class EvictionTest(unittest.TestCase):
    def test_timer_rule(self):
        chosen = choose_evictions([cand("old", 40), cand("new", 2)], NOW, 30,
                                  free_bytes=500 * GB, total_bytes=1000 * GB, min_free_percent=10)
        self.assertEqual([c.rel for c in chosen], ["old"])

    def test_timer_never(self):
        chosen = choose_evictions([cand("old", 400)], NOW, 0, 500 * GB, 1000 * GB, 10)
        self.assertEqual(chosen, [])

    def test_space_rule_frees_oldest_first_until_enough(self):
        files = [cand("a", 3), cand("b", 1), cand("c", 2)]
        # 1000 GB disk, want 10% = 100 GB free, have 98.5 GB: need 1.5 GB more.
        chosen = choose_evictions(files, NOW, 30, int(98.5 * GB), 1000 * GB, 10)
        self.assertEqual([c.rel for c in chosen], ["a", "c"])

    def test_unchecked_pins_skip_timer_but_not_emergency(self):
        files = [cand("old", 40), cand("x", 1)]
        chosen = choose_evictions(files, NOW, 30, 500 * GB, 1000 * GB, 10, timer_allowed=False)
        self.assertEqual(chosen, [])
        chosen = choose_evictions(files, NOW, 30, 99 * GB, 1000 * GB, 10, timer_allowed=False)
        self.assertEqual([c.rel for c in chosen], ["old"])


class StateTest(unittest.TestCase):
    full = {"Size": 10, "Rs": [{"Pos": 0, "Size": 10}], "Dirty": False}
    part = {"Size": 10, "Rs": [{"Pos": 0, "Size": 4}], "Dirty": False}
    pin = {"state": "pinned"}

    def state(self, sidecar=None, rule=None, queue_item=None, pending=False, error=None, is_dir=False):
        return file_state("x", is_dir, rule, sidecar, queue_item, pending, error)

    def test_files(self):
        self.assertEqual(self.state(), "online")
        self.assertEqual(self.state(self.part), "online")
        self.assertEqual(self.state(self.full), "local")
        self.assertEqual(self.state(self.full, self.pin), "pinned")
        self.assertEqual(self.state(self.part, self.pin), "syncing")
        self.assertEqual(self.state(dict(self.full, Dirty=True)), "syncing")
        self.assertEqual(self.state(self.full, queue_item={"tries": 0}), "syncing")
        self.assertEqual(self.state(self.full, queue_item={"tries": 5}), "error")
        self.assertEqual(self.state(None, self.pin, error="I/O error"), "error")
        self.assertEqual(self.state(self.full, {"state": "online"}), "local")

    def test_folders(self):
        self.assertEqual(self.state(is_dir=True), "none")
        self.assertEqual(self.state(rule=self.pin, is_dir=True), "pinned")
        self.assertEqual(self.state(rule=self.pin, pending=True, is_dir=True), "syncing")

    def test_human(self):
        self.assertEqual(human(512), "512 bytes")
        self.assertEqual(human(1536), "1.5 KB")
        self.assertEqual(human(3 * GB), "3.0 GB")


class GraphPathTest(unittest.TestCase):
    def item(self, parent, name):
        return {"name": name, "parentReference": {"path": parent}}

    def test_inside_folder(self):
        it = self.item("/drive/root:/LinuxClientTest/Projects/term 1 notes", "a b.docx")
        self.assertEqual(graph.path_in_folder(it, "LinuxClientTest"), "Projects/term 1 notes/a b.docx")

    def test_the_folder_itself_and_outside(self):
        self.assertEqual(graph.path_in_folder(self.item("/drive/root:", "LinuxClientTest"),
                                              "LinuxClientTest"), "")
        self.assertIsNone(graph.path_in_folder(self.item("/drive/root:", "other.txt"), "LinuxClientTest"))
        self.assertIsNone(graph.path_in_folder(self.item("/drive/root:/LinuxClientTestX", "a"),
                                               "LinuxClientTest"))

    def test_business_style_path(self):
        it = self.item("/drives/b!abc/root:/Docs", "a.txt")
        self.assertEqual(graph.path_in_folder(it, ""), "Docs/a.txt")


if __name__ == "__main__":
    unittest.main()
