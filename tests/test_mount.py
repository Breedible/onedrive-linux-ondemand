import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import config, setup_wizard


class UnitTemplateTest(unittest.TestCase):
    def test_template_has_placeholders_and_notify(self):
        text = setup_wizard.UNIT_TEMPLATE.read_text()
        self.assertIn("@SOURCE_DIR@", text)
        self.assertIn("@PYTHON@", text)
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
