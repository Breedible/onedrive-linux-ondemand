import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onedrive_ondemand import config, mount, setup_wizard
from tests.fakehome import FakeHome


class UnitTemplateTest(unittest.TestCase):
    def read(self, name):
        return (setup_wizard.SOURCE_DIR / "data" / name).read_text()

    def test_templates_have_placeholders(self):
        for name in (config.MOUNT_TEMPLATE, config.DAEMON_TEMPLATE):
            text = self.read(name)
            self.assertIn("@SOURCE_DIR@", text)
            self.assertIn("@PYTHON@", text)
            self.assertIn("%i", text)  # one copy per account

    def test_mount_unit_waits_for_rclone_and_cleans_up(self):
        text = self.read(config.MOUNT_TEMPLATE)
        self.assertIn("Type=notify", text)
        self.assertIn("ExecStopPost=@PYTHON@ -m onedrive_ondemand mount-stopped %i", text)


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


class InstalledModeTest(unittest.TestCase):
    def test_source_checkout_is_not_installed(self):
        # The repo has a data/ folder next to the package; the .deb doesn't.
        self.assertFalse(setup_wizard.INSTALLED)
        self.assertTrue(setup_wizard.LAUNCHER.endswith("bin/onedrive-ondemand"))


class DownloadedListTest(unittest.TestCase):
    """The offline mount only shows files that are fully downloaded."""

    def test_list(self):
        home = FakeHome()
        try:
            account = home.add_account("personal", "Personal")
            settings = account.settings()
            meta = account.cache_dir / "vfsMeta" / "onedrive-ondemand" / "LinuxClientTest"
            for rel, size, cached in (("a.txt", 10, 10), ("sub/b.jpg", 10, 4), ("empty", 0, 0)):
                path = meta / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                ranges = [{"Pos": 0, "Size": cached}] if cached else None
                path.write_text(json.dumps({"Size": size, "Rs": ranges, "Dirty": False}))
            account.runtime_dir.mkdir(parents=True)
            path, count = mount.write_downloaded_list(account, settings)
            self.assertEqual(path.read_text().splitlines(), ["a.txt", "empty"])
            self.assertEqual(count, 2)
        finally:
            home.cleanup()


class NetworkWaitTest(unittest.TestCase):
    def test_gives_up_after_the_wait(self):
        with mock.patch.object(mount.graph, "reachable", return_value=False), \
                mock.patch.object(mount.time, "sleep"):
            self.assertFalse(mount.wait_for_network(seconds=0))

    def test_online_straight_away(self):
        with mock.patch.object(mount.graph, "reachable", return_value=True):
            self.assertTrue(mount.wait_for_network())


if __name__ == "__main__":
    unittest.main()
