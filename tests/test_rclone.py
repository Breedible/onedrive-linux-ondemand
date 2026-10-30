import base64
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import config, rclone

TOKEN = {"access_token": "a", "token_type": "Bearer", "refresh_token": "r",
         "expiry": "2026-10-30T16:00:00+02:00"}


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rclone.parse_version("rclone v1.75.2\n- os/version: x"), (1, 75, 2))

    def test_parse_distro_build(self):
        self.assertEqual(rclone.parse_version("rclone v1.60.1-DEV"), (1, 60, 1))

    def test_parse_garbage(self):
        self.assertIsNone(rclone.parse_version("command not found"))


class MountCommandTest(unittest.TestCase):
    def setUp(self):
        self.account = config.Account("personal")
        self.cmd = rclone.mount_command(self.account, dict(config.DEFAULT_SETTINGS))

    def value_of(self, flag):
        return self.cmd[self.cmd.index(flag) + 1]

    def test_mounts_only_the_chosen_folder(self):
        self.assertEqual(self.cmd[1:3], ["mount", "onedrive-ondemand:LinuxClientTest"])

    def test_whole_drive_is_refused(self):
        settings = dict(config.DEFAULT_SETTINGS, remote_folder="")
        with self.assertRaises(ValueError):
            rclone.mount_command(self.account, settings)

    def test_uses_our_config(self):
        self.assertEqual(self.value_of("--config"), str(self.account.rclone_conf))

    def test_cache_settings_match_design(self):
        self.assertEqual(self.value_of("--vfs-cache-mode"), "full")
        self.assertEqual(self.value_of("--vfs-cache-max-age"), "87600h")
        self.assertEqual(self.value_of("--vfs-cache-min-free-space"), "1G")
        self.assertNotIn("--vfs-cache-max-size", self.cmd)
        self.assertEqual(self.value_of("--cache-dir"), str(self.account.cache_dir))

    def test_fails_fast_when_offline(self):
        self.assertEqual(self.value_of("--low-level-retries"), "3")

    def test_rc_is_on_a_unix_socket(self):
        self.assertEqual(self.value_of("--rc-addr"), f"unix://{self.account.rc_socket}")

    def test_shown_with_the_account_name(self):
        self.assertEqual(self.value_of("--volname"), "OneDrive - Personal")

    def test_never_uses_dangerous_commands(self):
        for word in rclone.FORBIDDEN:
            self.assertNotIn(word, self.cmd)


class OfflineMountCommandTest(unittest.TestCase):
    def setUp(self):
        self.account = config.Account("work")
        settings = dict(config.DEFAULT_SETTINGS, name="Work")
        self.cmd = rclone.offline_mount_command(self.account, settings, "/run/list.txt")

    def test_shows_the_cache_read_only(self):
        expected = self.account.cache_dir / "vfs" / "onedrive-ondemand" / "LinuxClientTest"
        self.assertEqual(self.cmd[1:3], ["mount", str(expected)])
        self.assertIn("--read-only", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("--files-from") + 1], "/run/list.txt")

    def test_never_talks_to_onedrive(self):
        self.assertNotIn("onedrive-ondemand:", " ".join(self.cmd))
        for word in rclone.FORBIDDEN:
            self.assertNotIn(word, self.cmd)


class AuthorizeOutputTest(unittest.TestCase):
    def wrap(self, blob):
        return f"Paste the following into your remote machine --->\n{blob}\n<---End paste\n"

    def test_plain_json(self):
        result = rclone.parse_authorize_output(self.wrap(json.dumps(TOKEN)))
        self.assertEqual(json.loads(result), TOKEN)

    def test_base64_blob_with_token_inside(self):
        inner = {"token": json.dumps(TOKEN)}
        blob = base64.urlsafe_b64encode(json.dumps(inner).encode()).decode().rstrip("=")
        self.assertEqual(json.loads(rclone.parse_authorize_output(self.wrap(blob))), TOKEN)

    def test_no_markers(self):
        with self.assertRaises(RuntimeError):
            rclone.parse_authorize_output("something went wrong")

    def test_token_without_refresh_token_is_rejected(self):
        with self.assertRaises(RuntimeError):
            rclone.parse_authorize_output(self.wrap('{"access_token": "a"}'))


class RcloneConfTest(unittest.TestCase):
    def test_written_private_and_readable_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new" / "rclone.conf"
            rclone.write_rclone_conf(json.dumps(TOKEN), "drive1", "personal", path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            text = path.read_text()
            self.assertIn("[onedrive-ondemand]", text)
            self.assertNotIn("root_folder_id", text)
            self.assertEqual(rclone.read_token(path), TOKEN)
            self.assertEqual(rclone.read_rclone_conf(path)["drive_type"], "personal")


if __name__ == "__main__":
    unittest.main()
