import json
import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import config


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
        self.assertEqual(config.mount_path({"mount_path": "~/OneDrive"}),
                         Path.home() / "OneDrive")

    def test_socket_path_is_short_enough(self):
        # Linux unix socket paths are limited to 107 characters.
        self.assertLess(len(str(config.RC_SOCKET)), 108)


if __name__ == "__main__":
    unittest.main()
