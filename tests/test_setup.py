import json
import os
import unittest
from pathlib import Path
from unittest import mock

from onedrive_ondemand import config, setup_wizard
from tests.fakehome import FakeHome


class MigrationTest(unittest.TestCase):
    """Moving a version 0.1 setup (one account, ~/OneDrive) into "OneDrive - Personal"."""

    def setUp(self):
        self.home = FakeHome()
        self.calls = []
        self.patches = [mock.patch.object(setup_wizard, "systemctl",
                                          side_effect=lambda *a: self.calls.append(a) or True)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.home.cleanup()

    def make_old_setup(self, mount):
        config.OLD_RCLONE_CONF.parent.mkdir(parents=True)
        config.OLD_RCLONE_CONF.write_text("[onedrive-ondemand]\ntoken = secret\n")
        config.OLD_SETTINGS_FILE.write_text(json.dumps({"mount_path": str(mount),
                                                        "evict_after_days": 9}))
        cached = config.OLD_CACHE_DIR / "vfs" / "onedrive-ondemand" / "LinuxClientTest" / "a.txt"
        cached.parent.mkdir(parents=True)
        cached.write_text("downloaded")
        config.OLD_STATE_DB.write_text("pins")
        mount.mkdir()
        os.chmod(mount, 0o555)  # how the old mount service left it

    def test_everything_moves(self):
        old_mount = self.home.root / "OneDrive"
        self.make_old_setup(old_mount)
        with mock.patch.object(setup_wizard.Path, "home", return_value=self.home.root):
            account = setup_wizard.migrate_old_layout()
        self.assertEqual(account.id, "personal")
        self.assertIn("secret", account.rclone_conf.read_text())
        settings = account.settings()
        self.assertEqual(settings["name"], "Personal")
        self.assertEqual(settings["mount_path"], "~/OneDrive - Personal")
        self.assertEqual(settings["evict_after_days"], 9)
        self.assertEqual((account.cache_dir / "vfs" / "onedrive-ondemand" / "LinuxClientTest"
                          / "a.txt").read_text(), "downloaded")
        self.assertEqual(account.state_db.read_text(), "pins")
        for old in (config.OLD_RCLONE_CONF, config.OLD_SETTINGS_FILE, config.OLD_CACHE_DIR,
                    config.OLD_STATE_DB, old_mount):
            self.assertFalse(old.exists(), old)
        self.assertIn(("disable", "--now", config.OLD_MOUNT_SERVICE), self.calls)
        self.assertEqual(config.accounts(), [account])

    def test_custom_mount_path_is_kept(self):
        self.make_old_setup(self.home.root / "Cloud")
        account = setup_wizard.migrate_old_layout()
        self.assertEqual(account.mount_path(), self.home.root / "Cloud")

    def test_nothing_to_move(self):
        self.assertIsNone(setup_wizard.migrate_old_layout())
        self.assertEqual(self.calls, [])

    def test_runs_only_once(self):
        self.make_old_setup(self.home.root / "Cloud")
        setup_wizard.migrate_old_layout()
        self.assertIsNone(setup_wizard.migrate_old_layout())


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()

    def tearDown(self):
        self.home.cleanup()

    def test_check_name(self):
        self.assertIsNone(setup_wizard.check_name("Work", ["Personal"]))
        self.assertIsNotNone(setup_wizard.check_name("work", ["Work"]))
        self.assertIsNotNone(setup_wizard.check_name("a/b", []))
        self.assertIsNotNone(setup_wizard.check_name(".hidden", []))
        self.assertIsNotNone(setup_wizard.check_name("", []))

    def test_bookmarks(self):
        config.GTK_BOOKMARKS.write_text("file:///home/me/Music\n")
        folder = Path("/home/me/OneDrive - Work")
        setup_wizard.add_bookmark(folder, "OneDrive - Work")
        setup_wizard.add_bookmark(folder, "OneDrive - Work")  # not twice
        self.assertEqual(config.GTK_BOOKMARKS.read_text().splitlines(),
                         ["file:///home/me/Music", "file:///home/me/OneDrive%20-%20Work OneDrive - Work"])
        setup_wizard.remove_bookmark(folder)
        self.assertEqual(config.GTK_BOOKMARKS.read_text(), "file:///home/me/Music\n")

    def test_remove_folder_refuses_outside(self):
        with self.assertRaises(RuntimeError):
            setup_wizard._remove_folder(self.home.root, config.ACCOUNTS_DATA_DIR)
        with self.assertRaises(RuntimeError):
            setup_wizard._remove_folder(config.ACCOUNTS_DATA_DIR, config.ACCOUNTS_DATA_DIR)

    def test_remove_account_deletes_only_that_account(self):
        work = self.home.add_account("work", "Work")
        personal = self.home.add_account("personal", "Personal")
        work.cache_dir.mkdir(parents=True)
        mount = work.mount_path()
        mount.mkdir()
        with mock.patch.object(setup_wizard, "systemctl", return_value=True), \
                mock.patch.object(setup_wizard, "set_nemo_previews"), \
                mock.patch("builtins.print"):
            setup_wizard.remove_account(work, assume_yes=True)
        self.assertEqual(config.accounts(), [personal])
        self.assertFalse(work.data_dir.exists())
        self.assertFalse(mount.exists())

    def test_remove_account_stops_for_unuploaded_changes(self):
        work = self.home.add_account("work", "Work")
        meta = work.cache_dir / "vfsMeta" / "onedrive-ondemand" / "LinuxClientTest" / "a.txt"
        meta.parent.mkdir(parents=True)
        meta.write_text('{"Dirty": true}')
        with mock.patch.object(setup_wizard, "systemctl", return_value=True), \
                mock.patch.object(setup_wizard, "ask", return_value=False), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                setup_wizard.remove_account(work, assume_yes=True)
        self.assertTrue(work.rclone_conf.exists())


if __name__ == "__main__":
    unittest.main()
