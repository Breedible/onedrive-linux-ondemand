import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import config, setup_wizard


class UnitTemplateTest(unittest.TestCase):
    def test_templates_have_placeholders(self):
        for name in (config.MOUNT_SERVICE, config.DAEMON_SERVICE):
            text = (setup_wizard.SOURCE_DIR / "data" / name).read_text()
            self.assertIn("@SOURCE_DIR@", text)
            self.assertIn("@PYTHON@", text)

    def test_mount_unit_waits_for_rclone_and_cleans_up(self):
        text = (setup_wizard.SOURCE_DIR / "data" / config.MOUNT_SERVICE).read_text()
        self.assertIn("Type=notify", text)
        self.assertIn("ExecStopPost=", text)


class MountFolderTest(unittest.TestCase):
    def test_non_empty_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "stray.txt").write_text("x")
            with self.assertRaises(SystemExit):
                setup_wizard.check_mount_folder(Path(folder))

    def test_empty_or_missing_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as folder:
            setup_wizard.check_mount_folder(Path(folder))
            setup_wizard.check_mount_folder(Path(folder) / "not-yet")


if __name__ == "__main__":
    unittest.main()


class InstalledModeTest(unittest.TestCase):
    def test_source_checkout_is_not_installed(self):
        # The repo has a data/ folder next to the package; the .deb doesn't.
        self.assertFalse(setup_wizard.INSTALLED)
