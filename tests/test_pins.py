import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import pins


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pins.Store(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_folder_pin_covers_files_inside(self):
        self.store.set_rule("id1", "Projects", pins.PINNED)
        self.assertTrue(self.store.is_pinned("Projects/notes/a.txt"))
        self.assertTrue(self.store.is_pinned("Projects"))
        self.assertFalse(self.store.is_pinned("Projectswork/a.txt"))  # not a prefix match

    def test_nearest_rule_wins(self):
        self.store.set_rule("id1", "Projects", pins.PINNED)
        self.store.set_rule("id2", "Projects/big.mp4", pins.ONLINE)
        self.assertFalse(self.store.is_pinned("Projects/big.mp4"))
        self.assertTrue(self.store.is_pinned("Projects/a.txt"))

    def test_remove_rules_inside(self):
        self.store.set_rule("id1", "a", pins.PINNED)
        self.store.set_rule("id2", "a/b", pins.ONLINE)
        self.store.set_rule("id3", "ab", pins.PINNED)
        self.store.remove_rules_inside("a")
        self.assertEqual([r["path"] for r in self.store.rules()], ["a", "ab"])

    def test_like_wildcards_in_names(self):
        self.store.set_rule("id1", "50%_off/x", pins.PINNED)
        self.store.set_rule("id2", "50abcoff/x", pins.PINNED)
        self.store.remove_rules_inside("50%_off")
        self.assertEqual([r["path"] for r in self.store.rules()], ["50abcoff/x"])

    def test_rename_keeps_pin(self):
        self.store.set_rule("id1", "old.txt", pins.PINNED)
        self.store.update_path("id1", "folder/new.txt")
        self.assertTrue(self.store.is_pinned("folder/new.txt"))
        self.assertFalse(self.store.is_pinned("old.txt"))

    def test_deleted_rule_no_longer_applies(self):
        self.store.set_rule("id1", "a.txt", pins.PINNED)
        self.store.set_status("id1", pins.DELETED)
        self.assertFalse(self.store.is_pinned("a.txt"))
        self.assertEqual(len(self.store.rules(status=None)), 1)
        self.store.set_status("id1", pins.ACTIVE)
        self.assertTrue(self.store.is_pinned("a.txt"))

    def test_activity_log(self):
        self.store.log("uploaded", "a.txt", "Uploaded a.txt")
        self.store.log("freed", None, "Freed")
        self.assertEqual([e["kind"] for e in self.store.recent()], ["freed", "uploaded"])


if __name__ == "__main__":
    unittest.main()
