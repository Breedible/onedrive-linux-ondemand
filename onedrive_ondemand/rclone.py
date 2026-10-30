"""Everything that talks to the rclone program.

rclone does the hard work (showing OneDrive as a folder, downloading,
uploading). This module only builds its command lines and reads its output.
"""

import base64
import http.client
import json
import os
import re
import shutil
import socket
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


def mount_command(account, settings, rclone="rclone"):
    """The full `rclone mount` command line for one account, as a list for exec.

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
        "--config", str(account.rclone_conf),
        "--cache-dir", str(account.cache_dir),
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
        # Give up on a failed request after 3 tries instead of 10. Offline, this
        # makes opening a file that isn't downloaded fail after about half a
        # minute instead of hanging the program for 3+ minutes (tested
        # 2026-10-30, RESEARCH.md §10). Uploads are retried later anyway.
        "--low-level-retries", "3",
        # Remote control API on a private unix socket. Our daemon (milestone b)
        # uses it to read the upload queue. The socket's folder is mode 700,
        # so only you can reach it, which is why no password is needed.
        "--rc", "--rc-addr", f"unix://{account.rc_socket}", "--rc-no-auth",
        # Name shown for the mount in the file manager.
        "--volname", config.display_name(settings),
        # INFO logs every upload, which helps when something goes wrong.
        # Read them with: journalctl --user -u onedrive-ondemand-mount@<account>
        "--log-level", "INFO",
    ]


def offline_mount_command(account, settings, files_from, rclone="rclone"):
    """The mount used when OneDrive can't be reached as the mount starts (DESIGN.md §4).

    rclone only remembers folder listings in memory, so a normal mount that
    starts offline can't show *anything*, not even downloaded files (tested
    2026-10-30, RESEARCH.md §10). Instead we show the downloaded files
    straight from the cache folder:
    - read-only, so nothing can be saved where rclone wouldn't upload it,
    - only files that are fully downloaded (`files_from` lists them), because
      partly downloaded files are mostly empty space on disk.
    The daemon switches back to the normal mount once OneDrive is reachable.
    """
    cache = Path(account.cache_dir) / "vfs" / config.REMOTE_NAME / settings["remote_folder"]
    return [
        rclone, "mount", str(cache), str(config.mount_path(settings)),
        "--read-only",
        "--files-from", str(files_from),
        # No remotes are used, so no config file is needed.
        "--config", "/dev/null",
        # Same remote control socket, so the daemon can see the mount is alive.
        "--rc", "--rc-addr", f"unix://{account.rc_socket}", "--rc-no-auth",
        "--volname", config.display_name(settings) + " (offline)",
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


def write_rclone_conf(token, drive_id, drive_type, path):
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


def read_rclone_conf(path):
    """An account's rclone config as a dict of {setting: value}, or None if missing."""
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


def read_token(path):
    """The current token (a dict) from an account's rclone config, or None.

    rclone refreshes the token by itself and writes the new one back into
    this file, so always read it fresh rather than keeping a copy.
    """
    values = read_rclone_conf(path)
    if not values or "token" not in values:
        return None
    return json.loads(values["token"])


# --- Remote control (rc) API ---------------------------------------------
# Each running `rclone mount` answers JSON requests on a private unix socket
# (Account.rc_socket).
# See https://rclone.org/rc/ for the list of commands.

class RcError(Exception):
    """rclone isn't running, or it answered a request with an error."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection that goes through a unix socket instead of the network."""

    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def rc(socket_path, command, params=None, timeout=30):
    """Send one command to a running rclone mount and return its answer (a dict)."""
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("POST", "/" + command, body=json.dumps(params or {}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
    except OSError as error:
        raise RcError(f"rclone isn't reachable ({error})") from None
    finally:
        connection.close()
    try:
        answer = json.loads(body) if body else {}
    except ValueError:
        raise RcError(f"rclone sent an unreadable answer to {command}") from None
    if response.status != 200:
        raise RcError(answer.get("error", f"{command} failed with HTTP {response.status}"))
    return answer


def remote_fs(settings):
    """The rclone "fs" string for rc commands, e.g. "onedrive-ondemand:LinuxClientTest"."""
    return f"{config.REMOTE_NAME}:{settings['remote_folder']}"


def item_id_from_rclone(rclone_id):
    """rclone gives OneDrive IDs as "<driveID>#<itemID>"; Graph wants just the item ID."""
    return rclone_id.split("#", 1)[-1]
