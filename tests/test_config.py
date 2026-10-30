import json
import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import config
from tests.fakehome import FakeHome


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "sub" / "config.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(config.load_settings(self.path), config.DEFAULT_SETTINGS)

    def test_save_then_load(self):
        settings = config.load_settings(self.path)
        settings["evict_after_days"] = 7
        config.save_settings(settings, self.path)
        self.assertEqual(config.load_settings(self.path)["evict_after_days"], 7)

    def test_new_defaults_fill_in_old_files(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"mount_path": "~/Cloud"}))
        settings = config.load_settings(self.path)
        self.assertEqual(settings["mount_path"], "~/Cloud")
        self.assertEqual(settings["remote_folder"], "LinuxClientTest")

    def test_mount_path_expands_home(self):
        self.assertEqual(config.mount_path({"mount_path": "~/OneDrive - Personal"}),
                         Path.home() / "OneDrive - Personal")

    def test_display_name(self):
        self.assertEqual(config.display_name({"name": "Work"}), "OneDrive - Work")


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()

    def tearDown(self):
        self.home.cleanup()

    def test_each_account_has_its_own_files_and_services(self):
        a, b = config.Account("personal"), config.Account("work")
        for attribute in ("rclone_conf", "cache_dir", "state_db", "rc_socket", "daemon_socket"):
            self.assertNotEqual(getattr(a, attribute), getattr(b, attribute))
        self.assertEqual(a.mount_service, "onedrive-ondemand-mount@personal.service")
        self.assertEqual(b.daemon_service, "onedrive-ondemand-daemon@work.service")

    def test_socket_paths_are_short_enough(self):
        # Linux unix socket paths are limited to 107 characters. The real
        # runtime folder is /run/user/<uid>/onedrive-ondemand.
        real_runtime = Path("/run/user/1000/onedrive-ondemand")
        longest = real_runtime / ("a" * 32) / "daemon.sock"
        self.assertLess(len(str(longest)), 108)

    def test_bad_ids_are_refused(self):
        for bad in ("", "Work", "../x", "a/b", "-x", "a" * 33):
            with self.assertRaises(ValueError):
                config.Account(bad)

    def test_accounts_lists_only_set_up_ones(self):
        self.home.add_account("work", "Work")
        self.home.add_account("personal", "Personal")
        config.Account("half").config_dir.mkdir(parents=True)  # no sign-in yet
        self.assertEqual([a.id for a in config.accounts()], ["personal", "work"])

    def test_account_for_path(self):
        work = self.home.add_account("work", "Work")
        personal = self.home.add_account("personal", "Personal")
        root = self.home.root
        self.assertEqual(config.account_for_path(root / "OneDrive - Work" / "a.txt"), work)
        self.assertEqual(config.account_for_path(root / "OneDrive - Personal"), personal)
        # A folder whose name merely starts the same isn't inside.
        self.assertIsNone(config.account_for_path(root / "OneDrive - Workshop" / "a.txt"))
        self.assertIsNone(config.account_for_path("/etc/hostname"))

    def test_find_account(self):
        work = self.home.add_account("work", "Work")
        for text in ("work", "Work", "OneDrive - Work", " WORK "):
            self.assertEqual(config.find_account(text), work)
        self.assertIsNone(config.find_account("school"))

    def test_new_account_id(self):
        self.assertEqual(config.new_account_id("Work"), "work")
        self.assertEqual(config.new_account_id("My Job!"), "my-job")
        self.assertEqual(config.new_account_id("Work", ["work", "work-2"]), "work-3")
        self.assertEqual(config.new_account_id("日本"), "account")
        for name in ("Work", "a" * 60, "x--y", "École"):
            config.Account(config.new_account_id(name))  # always a valid id

    def test_mode(self):
        account = config.Account("personal")
        self.assertIsNone(account.mode())
        account.runtime_dir.mkdir(parents=True)
        account.mode_file.write_text("offline\n")
        self.assertEqual(account.mode(), "offline")


if __name__ == "__main__":
    unittest.main()
