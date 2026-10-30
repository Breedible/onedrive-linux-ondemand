"""Starting and stopping one account's mount. systemd runs these (data/*.service).

The mount folder (e.g. ~/OneDrive - Personal) has two lives:
- While rclone runs, your OneDrive files are shown "on top of" it.
- While rclone is stopped, it's an ordinary empty folder. We make it
  read-only then, so no program can save files into it by mistake
  (those files would be hidden as soon as the mount comes back).

fusermount refuses to mount onto a read-only folder ("user has no write
access to mountpoint", tested 2026-10-30), so we make the folder writable
just before mounting and read-only again after unmounting.

If OneDrive can't be reached when the mount starts (say, a laptop booting
without Wi-Fi), we start the read-only "offline mount" instead, which shows
the files that are already downloaded (rclone.offline_mount_command). The
daemon switches back once OneDrive is reachable again.
"""

import errno
import os
import subprocess
import sys
import time

from onedrive_ondemand import cache as cachemod
from onedrive_ondemand import config, graph, rclone

WRITABLE = 0o755
READ_ONLY = 0o555
# At login the network is often still connecting. Wait this long for it
# before falling back to the offline mount.
NETWORK_WAIT_SECONDS = 30


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


def wait_for_network(seconds=NETWORK_WAIT_SECONDS):
    """True as soon as Microsoft's servers can be reached; False after `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        if graph.reachable(timeout=3):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def write_downloaded_list(account, settings):
    """Write the list of fully downloaded files for the offline mount: (its path, how many)."""
    cache = cachemod.Cache(account.cache_dir, config.REMOTE_NAME, settings["remote_folder"],
                           config.mount_path(settings))
    files = []
    for rel in cache.cached_files():
        sidecar = cache.sidecar(rel)
        if sidecar is not None and cachemod.is_fully_cached(sidecar):
            files.append(rel)
    path = account.runtime_dir / "downloaded-files.txt"
    path.write_text("".join(f"{rel}\n" for rel in sorted(files)), encoding="utf-8")
    return path, len(files)


def start(account_id):
    """Prepare the mount folder, then replace this process with `rclone mount`."""
    account = config.Account(account_id)
    settings = account.settings()
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

    account.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(account.runtime_dir, 0o700)
    # A socket left behind by a crash would stop rclone's remote control starting.
    account.rc_socket.unlink(missing_ok=True)
    account.cache_dir.mkdir(parents=True, exist_ok=True)
    program = rclone.find_rclone() or "rclone"

    if wait_for_network():
        mode = "online"
        command = rclone.mount_command(account, settings, rclone=program)
    else:
        mode = "offline"
        files_from, count = write_downloaded_list(account, settings)
        print(f"OneDrive can't be reached. Showing the {count} downloaded file(s), read-only.")
        command = rclone.offline_mount_command(account, settings, files_from, rclone=program)
    account.mode_file.write_text(mode + "\n", encoding="utf-8")

    sys.stdout.flush()
    # exec (not run) so rclone keeps this process ID. systemd is waiting for
    # that process to say "ready", and stops it directly when you log out.
    os.execvp(command[0], command)


def stopped(account_id):
    """Clean up after rclone has stopped for any reason, even a crash."""
    account = config.Account(account_id)
    path = account.mount_path()

    if os.path.ismount(path) or is_stale_mount(path):
        lazy_unmount(path)
    account.rc_socket.unlink(missing_ok=True)
    account.mode_file.unlink(missing_ok=True)
    if path.is_dir() and not os.path.ismount(path):
        os.chmod(path, READ_ONLY)
