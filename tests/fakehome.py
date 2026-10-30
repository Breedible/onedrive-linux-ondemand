"""Points all of config.py's folders at a temporary folder, so tests never touch your real setup."""

import tempfile
from pathlib import Path
from unittest import mock

from onedrive_ondemand import config


class FakeHome:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")  # short: sockets live in here
        root = Path(self.tmp.name)
        self.root = root
        names = {
            "CONFIG_DIR": root / "config",
            "DATA_DIR": root / "data",
            "RUNTIME_DIR": root / "run",
            "ACCOUNTS_CONFIG_DIR": root / "config" / "accounts",
            "ACCOUNTS_DATA_DIR": root / "data" / "accounts",
            "OLD_SETTINGS_FILE": root / "config" / "config.json",
            "OLD_RCLONE_CONF": root / "config" / "rclone.conf",
            "OLD_CACHE_DIR": root / "data" / "cache",
            "OLD_STATE_DB": root / "data" / "state.db",
            "THUMBNAILER_DIR": root / "thumbnailers",
            "THUMBNAIL_CACHE": root / "thumbnails",
            "GTK_BOOKMARKS": root / "bookmarks",
            "SYSTEMD_USER_DIR": root / "systemd",
        }
        self.patches = [mock.patch.object(config, name, value) for name, value in names.items()]
        for patch in self.patches:
            patch.start()

    def add_account(self, account_id, name, mount=None):
        """An account that looks set up: settings plus a (fake) rclone.conf."""
        account = config.Account(account_id)
        settings = dict(config.DEFAULT_SETTINGS, name=name,
                        mount_path=str(mount or self.root / f"OneDrive - {name}"))
        account.save_settings(settings)
        account.rclone_conf.write_text("[onedrive-ondemand]\ntype = onedrive\ndrive_id = d-" + account_id + "\n")
        return account

    def cleanup(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()
