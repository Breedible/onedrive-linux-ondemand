"""Starting and stopping the mount. systemd runs these (see data/*.service).

The ~/OneDrive folder has two lives:
- While rclone runs, your OneDrive files are shown "on top of" it.
- While rclone is stopped, it's an ordinary empty folder. We make it
  read-only then, so no program can save files into it by mistake
  (those files would be hidden as soon as the mount comes back).

fusermount refuses to mount onto a read-only folder ("user has no write
access to mountpoint", tested 2026-10-30), so we make the folder writable
just before mounting and read-only again after unmounting.
"""

import errno
import os
import subprocess
import sys

from onedrive_ondemand import config, rclone

WRITABLE = 0o755
READ_ONLY = 0o555


def is_stale_mount(path):
    """True if rclone crashed and left a dead mount behind.

    The folder then gives "Transport endpoint is not connected" for everything.
    """
    try:
        os.stat(path)
    except OSError as error:
        return error.errno == errno.ENOTCONN
    return False


def lazy_unmount(path):
    """Detach a mount, even if it's dead or busy. Ignores "not mounted"."""
    subprocess.run(["fusermount3", "-uz", str(path)], capture_output=True)


def start():
    """Prepare ~/OneDrive, then replace this process with `rclone mount`."""
    settings = config.load_settings()
    path = config.mount_path(settings)

    if is_stale_mount(path):
        print(f"Clearing a crashed mount at {path}")
        lazy_unmount(path)
    if os.path.ismount(path):
        sys.exit(f"{path} is already mounted. Nothing to do.")

    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        # rclone would refuse anyway, but with a less helpful message.
        sys.exit(f"{path} is not empty: something saved files into it while OneDrive "
                 "wasn't mounted. Move those files somewhere else, then start the "
                 "service again.")
    os.chmod(path, WRITABLE)

    config.RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.RUNTIME_DIR, 0o700)
    # A socket left behind by a crash would stop rclone's remote control starting.
    config.RC_SOCKET.unlink(missing_ok=True)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    command = rclone.mount_command(settings, rclone=rclone.find_rclone() or "rclone")
    sys.stdout.flush()
    # exec (not run) so rclone keeps this process ID. systemd is waiting for
    # that process to say "ready", and stops it directly when you log out.
    os.execvp(command[0], command)


def stopped():
    """Clean up after rclone has stopped for any reason, even a crash."""
    settings = config.load_settings()
    path = config.mount_path(settings)

    if os.path.ismount(path) or is_stale_mount(path):
        lazy_unmount(path)
    config.RC_SOCKET.unlink(missing_ok=True)
    if path.is_dir() and not os.path.ismount(path):
        os.chmod(path, READ_ONLY)
