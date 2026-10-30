"""Everything that talks to the rclone program.

rclone does the hard work (showing OneDrive as a folder, downloading,
uploading). This module only builds its command lines and reads its output.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from onedrive_ondemand import config

MIN_VERSION = (1, 75, 0)

# Commands and flags that can delete or overwrite cloud data in bulk.
# We never pass them to rclone (CLAUDE.md safety rule 2). Tests check this.
FORBIDDEN = ("sync", "bisync", "delete", "purge", "cleanup", "--onedrive-hard-delete")


def find_rclone():
    """Path to the rclone program, or None if it isn't installed."""
    return shutil.which("rclone")


def parse_version(text):
    """Turn the first line of `rclone version` into a tuple like (1, 75, 2)."""
    match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def installed_version(rclone="rclone"):
    """The installed rclone version as a tuple, or None if it can't be run."""
    try:
        result = subprocess.run([rclone, "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(result.stdout)


def mount_command(settings, rclone="rclone", rclone_conf=config.RCLONE_CONF,
                  cache_dir=config.CACHE_DIR, rc_socket=config.RC_SOCKET):
    """The full `rclone mount` command line, as a list for subprocess/exec.

    Each flag is explained, because they are the heart of how the client behaves.
    """
    folder = settings["remote_folder"]
    if not folder:
        # CLAUDE.md safety rule 1: never the whole drive during development.
        raise ValueError("remote_folder is empty: showing the whole drive isn't supported yet")
    return [
        rclone, "mount",
        # Our remote plus the folder to show, e.g. "onedrive-ondemand:LinuxClientTest".
        # rclone builds every path (listings *and* uploads) inside this folder.
        # (We first tried rclone's root_folder_id option instead, but on
        # 2026-10-30 uploads ignored it and landed at the top of the drive.
        # See docs/RESEARCH.md.)
        f"{config.REMOTE_NAME}:{folder}",
        str(config.mount_path(settings)),
        # Use our own config file, never ~/.config/rclone/rclone.conf.
        "--config", str(rclone_conf),
        "--cache-dir", str(cache_dir),
        # Download files on demand and keep what was read on disk.
        "--vfs-cache-mode", "full",
        # Never free files by age: our daemon does that, because it knows
        # about pins (DESIGN.md §5). 87600h is 10 years.
        "--vfs-cache-max-age", "87600h",
        # Emergency backstop only: if the disk is nearly full, rclone may free
        # any closed file, pinned or not. A full disk is worse.
        "--vfs-cache-min-free-space", "1G",
        # Upload a saved file once it has been closed and idle this long.
        "--vfs-write-back", f"{settings['write_back_seconds']}s",
        # Ask OneDrive for changes this often. Changes found this way also
        # refresh the folder listings below.
        "--poll-interval", f"{settings['poll_interval_minutes']}m",
        # Keep folder listings for a long time. This is safe because the
        # polling above updates any folder that changes on OneDrive, and it
        # means folders still list while you're offline.
        "--dir-cache-time", "1000h",
        # Remote control API on a private unix socket. Our daemon (milestone b)
        # uses it to read the upload queue. The socket's folder is mode 700,
        # so only you can reach it, which is why no password is needed.
        "--rc", "--rc-addr", f"unix://{rc_socket}", "--rc-no-auth",
        # Name shown for the mount in the file manager.
        "--volname", "OneDrive",
        # INFO logs every upload, which helps when something goes wrong.
        # Read them with: journalctl --user -u onedrive-ondemand-mount
        "--log-level", "INFO",
    ]


def authorize():
    """Run `rclone authorize onedrive`: opens the Microsoft sign-in page.

    Returns the token as a JSON string, ready to go into rclone.conf.
    rclone's own messages (like the sign-in link) still show in the terminal.
    """
    result = subprocess.run(["rclone", "authorize", "onedrive"],
                            stdout=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError("Sign-in was cancelled or failed (see the messages above).")
    return parse_authorize_output(result.stdout)


def parse_authorize_output(output):
    """Pull the token out of `rclone authorize` output.

    rclone prints the result between two marker lines:
        Paste the following into your remote machine --->
        <result>
        <---End paste
    The result is either the token JSON itself or (newer rclone versions)
    a base64-encoded JSON blob with the token inside. We handle both.
    """
    match = re.search(r"--->\s*(.+?)\s*<---End paste", output, re.DOTALL)
    if not match:
        raise RuntimeError("Couldn't find the sign-in token in rclone's output.")
    blob = match.group(1).strip()

    if blob.startswith("{"):
        data = json.loads(blob)
    else:
        padded = blob + "=" * (-len(blob) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))

    # A blob may wrap the token: {"token": "<token JSON>"}.
    if "access_token" not in data and "token" in data:
        token = data["token"]
        data = json.loads(token) if isinstance(token, str) else token

    if "access_token" not in data or "refresh_token" not in data:
        raise RuntimeError("rclone's sign-in result doesn't contain a usable token.")
    return json.dumps(data, separators=(",", ":"))


def write_rclone_conf(token, drive_id, drive_type, path=config.RCLONE_CONF):
    """Write our own rclone config file, readable only by you (mode 600).

    It contains the sign-in token, so it must never be committed to git.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = (
        f"[{config.REMOTE_NAME}]\n"
        "type = onedrive\n"
        f"token = {token}\n"
        f"drive_id = {drive_id}\n"
        f"drive_type = {drive_type}\n"
    )
    tmp = path.with_suffix(".tmp")
    # Create the file with mode 600 from the start, so the token is never
    # readable by other users, not even for a moment.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_rclone_conf(path=config.RCLONE_CONF):
    """Our rclone config as a dict of {setting: value}, or None if missing."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return None
    values = {}
    for line in lines:
        if " = " in line:
            key, value = line.split(" = ", 1)
            values[key] = value
    return values


def read_token(path=config.RCLONE_CONF):
    """The current token (a dict) from our rclone config, or None.

    rclone refreshes the token by itself and writes the new one back into
    this file, so always read it fresh rather than keeping a copy.
    """
    values = read_rclone_conf(path)
    if not values or "token" not in values:
        return None
    return json.loads(values["token"])
