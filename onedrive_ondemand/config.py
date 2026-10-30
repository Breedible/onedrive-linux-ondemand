"""Where our files live, and each account's settings (DESIGN.md §4 and §6).

Every OneDrive account you add gets its own folders, its own rclone config,
its own mount and its own two systemd services. An account is known by a
short id such as "personal" or "work" (DESIGN.md §13).
"""

import json
import os
import re
from pathlib import Path

APP_NAME = "onedrive-ondemand"

# The name of the remote inside each account's *own* rclone config file.
# Every account has its own file, so they can all use the same name. It never
# touches ~/.config/rclone/rclone.conf, so any remotes you already have are
# left alone.
REMOTE_NAME = "onedrive-ondemand"

# systemd "template" services: one copy runs per account, for example
# onedrive-ondemand-mount@personal.service. %i in the file is the account id.
MOUNT_TEMPLATE = "onedrive-ondemand-mount@.service"
DAEMON_TEMPLATE = "onedrive-ondemand-daemon@.service"


def _xdg(variable, fallback):
    """Return an XDG base folder, e.g. XDG_CONFIG_HOME or ~/.config."""
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME
# /run/user/<uid> is private to you and emptied at logout: a good place
# for the sockets our programs use to talk to each other.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP_NAME
ACCOUNTS_CONFIG_DIR = CONFIG_DIR / "accounts"
ACCOUNTS_DATA_DIR = DATA_DIR / "accounts"

SYSTEMD_USER_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"
NEMO_EXTENSION_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "nemo-python" / "extensions"
ICON_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor"
AUTOSTART_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "autostart"
APPLICATIONS_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "applications"
# Picture previews (DESIGN.md §8): our thumbnailer, and where previews are kept.
THUMBNAILER_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "thumbnailers"
THUMBNAIL_CACHE = _xdg("XDG_CACHE_HOME", ".cache") / "thumbnails"
GTK_BOOKMARKS = _xdg("XDG_CONFIG_HOME", ".config") / "gtk-3.0" / "bookmarks"

# Where version 0.1 (one account only) kept things. Setup moves them into
# the "personal" account the first time it runs (setup_wizard.migrate_old_layout).
OLD_SETTINGS_FILE = CONFIG_DIR / "config.json"
OLD_RCLONE_CONF = CONFIG_DIR / "rclone.conf"
OLD_CACHE_DIR = DATA_DIR / "cache"
OLD_STATE_DB = DATA_DIR / "state.db"
OLD_MOUNT_SERVICE = "onedrive-ondemand-mount.service"
OLD_DAEMON_SERVICE = "onedrive-ondemand-daemon.service"

DEFAULT_SETTINGS = {
    # The account's name, shown as "OneDrive - <name>" (like Windows does).
    "name": "Personal",
    # Where OneDrive appears in the file manager.
    "mount_path": "~/OneDrive - Personal",
    # Which OneDrive folder to show. "" would mean the whole drive, which
    # setup refuses during development (CLAUDE.md safety rule 1).
    "remote_folder": "LinuxClientTest",
    # Free files you haven't opened for this many days (0 = never).
    "evict_after_days": 30,
    # Keep at least this much of the disk free.
    "min_free_space_percent": 10,
    # Upload a saved file once it has been closed and untouched this long.
    "write_back_seconds": 5,
    # How often rclone asks OneDrive what changed.
    "poll_interval_minutes": 1,
    # Uploads and pin downloads wait while this is true (tray: Pause).
    "paused": False,
    # Show small picture previews in Nemo? They come from OneDrive's own
    # preview images, so the pictures themselves aren't downloaded.
    # None = not asked yet (setup asks once). DESIGN.md §8.
    "thumbnails": None,
}

# Account ids end up in file names and systemd service names, so keep them plain.
ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def load_settings(path):
    """Read a config.json. Missing settings get their default value."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            settings.update(json.load(f))
    except FileNotFoundError:
        pass
    return settings


def save_settings(settings, path):
    """Write a config.json, creating the folder if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    # Rename is atomic: a crash halfway never leaves a half-written file.
    os.replace(tmp, path)


def mount_path(settings):
    """The mount folder as an absolute Path (expands the ~)."""
    return Path(settings["mount_path"]).expanduser()


def display_name(settings):
    """"OneDrive - Personal", the name shown in menus and the file manager."""
    return f"OneDrive - {settings['name']}"


class Account:
    """One OneDrive account: where its files, sockets and services are."""

    def __init__(self, account_id):
        if not ACCOUNT_ID.match(account_id or ""):
            raise ValueError(f"Not a valid account id: {account_id!r}")
        self.id = account_id
        self.config_dir = ACCOUNTS_CONFIG_DIR / account_id
        self.data_dir = ACCOUNTS_DATA_DIR / account_id
        self.runtime_dir = RUNTIME_DIR / account_id

        self.settings_file = self.config_dir / "config.json"
        # Contains the sign-in token: never commit it (CLAUDE.md safety rule 4).
        self.rclone_conf = self.config_dir / "rclone.conf"
        # Deliberately not ~/.cache: cleaning tools treat that as disposable,
        # and pinned files live in here.
        self.cache_dir = self.data_dir / "cache"
        self.state_db = self.data_dir / "state.db"
        # Keep these short: Linux refuses unix sockets with paths over ~108 characters.
        self.rc_socket = self.runtime_dir / "rclone.sock"
        self.daemon_socket = self.runtime_dir / "daemon.sock"
        # "online" or "offline": which kind of mount is running (mount.py).
        self.mode_file = self.runtime_dir / "mode"

        self.mount_service = MOUNT_TEMPLATE.replace("@", f"@{account_id}")
        self.daemon_service = DAEMON_TEMPLATE.replace("@", f"@{account_id}")

    def __repr__(self):
        return f"Account({self.id!r})"

    def __eq__(self, other):
        return isinstance(other, Account) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    def settings(self):
        return load_settings(self.settings_file)

    def save_settings(self, settings):
        save_settings(settings, self.settings_file)

    def mount_path(self):
        return mount_path(self.settings())

    def display_name(self):
        return display_name(self.settings())

    def is_set_up(self):
        """True once the account has a sign-in saved."""
        return self.rclone_conf.exists()

    def mode(self):
        """"online", "offline", or None if the mount isn't running."""
        try:
            return self.mode_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


def accounts():
    """Every account that has been set up, sorted by id."""
    try:
        names = sorted(os.listdir(ACCOUNTS_CONFIG_DIR))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        if ACCOUNT_ID.match(name):
            account = Account(name)
            if account.is_set_up():
                found.append(account)
    return found


def account_for_path(path, known=None):
    """The account whose OneDrive folder contains `path`, or None."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    for account in (accounts() if known is None else known):
        mount = str(account.mount_path())
        if path == mount or path.startswith(mount + os.sep):
            return account
    return None


def find_account(text, known=None):
    """Look an account up by its id or its name ("work", "Work", "OneDrive - Work")."""
    text = (text or "").strip().lower()
    for account in (accounts() if known is None else known):
        settings = account.settings()
        if text in (account.id, settings["name"].lower(), display_name(settings).lower()):
            return account
    return None


def new_account_id(name, taken=()):
    """A free account id made from a name: "My Work!" -> "my-work" (or "my-work-2")."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower())[:24].strip("-") or "account"
    candidate, number = base, 2
    while candidate in taken:
        candidate = f"{base}-{number}"
        number += 1
    return candidate
