"""Where our files live, and the user's settings (DESIGN.md §4 and §6)."""

import json
import os
from pathlib import Path

APP_NAME = "onedrive-ondemand"

# The name of the remote inside *our own* rclone config file. It never
# touches ~/.config/rclone/rclone.conf, so any remotes you already have
# are left alone.
REMOTE_NAME = "onedrive-ondemand"

# The systemd user services: one runs `rclone mount`, one runs our daemon.
MOUNT_SERVICE = "onedrive-ondemand-mount.service"
DAEMON_SERVICE = "onedrive-ondemand-daemon.service"


def _xdg(variable, fallback):
    """Return an XDG base folder, e.g. XDG_CONFIG_HOME or ~/.config."""
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME
# /run/user/<uid> is private to you and emptied at logout: a good place
# for the socket our programs use to talk to rclone.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP_NAME

SETTINGS_FILE = CONFIG_DIR / "config.json"
RCLONE_CONF = CONFIG_DIR / "rclone.conf"
# Deliberately not ~/.cache: cleaning tools treat that as disposable,
# and pinned files live in here.
CACHE_DIR = DATA_DIR / "cache"
# Keep this path short: Linux refuses unix sockets with paths over ~108 characters.
RC_SOCKET = RUNTIME_DIR / "rclone.sock"

SYSTEMD_USER_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"
NEMO_EXTENSION_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "nemo-python" / "extensions"
ICON_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor"

DEFAULT_SETTINGS = {
    # Where OneDrive appears in the file manager.
    "mount_path": "~/OneDrive",
    # Which OneDrive folder to show. "" would mean the whole drive, which
    # setup refuses during development (CLAUDE.md safety rule 1).
    "remote_folder": "LinuxClientTest",
    # Free files you haven't opened for this many days (0 = never). Milestone b.
    "evict_after_days": 30,
    # Keep at least this much of the disk free. Milestone b.
    "min_free_space_percent": 10,
    # Upload a saved file once it has been closed and untouched this long.
    "write_back_seconds": 5,
    # How often rclone asks OneDrive what changed.
    "poll_interval_minutes": 1,
    # Uploads and pin downloads wait while this is true (tray: Pause).
    "paused": False,
    # Show picture previews in Nemo for ~/OneDrive? Each preview downloads the
    # whole picture. None = not asked yet (setup asks once). DESIGN.md §8.
    "thumbnails": None,
}


def load_settings(path=SETTINGS_FILE):
    """Read config.json. Missing settings get their default value."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            settings.update(json.load(f))
    except FileNotFoundError:
        pass
    return settings


def save_settings(settings, path=SETTINGS_FILE):
    """Write config.json, creating the folder if needed."""
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
