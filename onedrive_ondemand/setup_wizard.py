"""`onedrive-ondemand setup`: sign in, create our rclone config, start the mount.

Safe to run again: it stops the mount, optionally signs in again, and
reinstalls the service.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from onedrive_ondemand import config, graph, rclone

# The folder that holds the onedrive_ondemand package (the repo, when
# running from a source checkout). systemd needs it to find our code.
SOURCE_DIR = Path(__file__).resolve().parent.parent
# Hidden file setup writes through the mount to check where uploads land.
PROBE_NAME = ".onedrive-ondemand-setup-check.txt"


def ask(question, default):
    """Ask a yes/no question. Pressing Enter picks the default."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def systemctl(*args):
    """Run `systemctl --user ...`. Returns True if it succeeded."""
    return subprocess.run(["systemctl", "--user", *args], capture_output=True).returncode == 0


def check_requirements():
    """Stop with a clear message if rclone or FUSE is missing or too old."""
    if not rclone.find_rclone():
        sys.exit("rclone isn't installed. Install it from https://rclone.org/downloads/ "
                 "(Mint's own package is too old).")
    version = rclone.installed_version()
    if version is None or version < rclone.MIN_VERSION:
        have = ".".join(map(str, version)) if version else "unknown"
        need = ".".join(map(str, rclone.MIN_VERSION))
        sys.exit(f"rclone {have} is too old; version {need} or newer is needed. "
                 "Install the current one from https://rclone.org/downloads/")
    if not shutil.which("fusermount3"):
        sys.exit("FUSE isn't installed. Run:  sudo apt install fuse3")


def check_mount_folder(path):
    """The mount folder must be empty (or not exist yet)."""
    if os.path.ismount(path):
        sys.exit(f"{path} is still mounted by something else. Unmount it first:\n"
                 f"  fusermount3 -u {path}")
    if path.is_dir() and any(path.iterdir()):
        sys.exit(f"{path} isn't empty. Move its contents somewhere else first, "
                 "because OneDrive will be shown in this folder.")


def sign_in():
    """Browser sign-in, then save the token and drive details in our rclone config."""
    print("\nA Microsoft sign-in page will open in your browser.")
    print("Sign in and allow access. If the browser doesn't open, copy the")
    print("http://127.0.0.1:53682/... link that rclone prints below.\n")
    token = rclone.authorize()
    access_token = json.loads(token)["access_token"]

    drive = graph.my_drive(access_token)
    drive_type = drive["driveType"]  # "personal", "business" or "documentLibrary"
    owner = drive.get("owner", {}).get("user", {}).get("displayName", "you")
    print(f"\nSigned in. OneDrive of {owner} ({drive_type}).")

    rclone.write_rclone_conf(token, drive["id"], drive_type)
    print(f"Saved the sign-in to {config.RCLONE_CONF} (only you can read it).")


def keep_sign_in():
    """Reuse the saved sign-in. Tidies configs written by older versions."""
    values = rclone.read_rclone_conf()
    if "root_folder_id" in values:
        # Older setup used root_folder_id, which uploads ignored (RESEARCH.md §5).
        rclone.write_rclone_conf(values["token"], values["drive_id"], values["drive_type"])
    print("Keeping the existing sign-in.")


def fresh_access_token():
    """A working access token from our rclone config.

    Access tokens only last about an hour. `rclone about` (which only reads
    your storage quota) makes rclone renew the token and save it if needed.
    """
    subprocess.run(["rclone", "about", f"{config.REMOTE_NAME}:", "--config", str(config.RCLONE_CONF)],
                   capture_output=True, timeout=120)
    return rclone.read_token()["access_token"]


def ensure_folder(access_token, folder_name):
    """Find the folder to show at the top of your OneDrive, creating it if you agree."""
    try:
        item = graph.find_top_item(access_token, folder_name)
        if item is None:
            print(f"There is no folder called '{folder_name}' in your OneDrive yet.")
            if not ask(f"Create an empty '{folder_name}' folder now?", True):
                sys.exit("Setup stopped.")
            item = graph.create_top_folder(access_token, folder_name)
            print(f"Created '{folder_name}'.")
    except graph.GraphError as error:
        print(f"\n{error}\nChecking which OneDrive requests work:")
        for line in graph.diagnose(access_token):
            print("  " + line)
        sys.exit("Setup stopped.")
    if "folder" not in item:
        sys.exit(f"'{folder_name}' in your OneDrive is a file, not a folder.")
    return item


def check_upload_location(mount_path, folder_id):
    """Save a small file through the mount and ask OneDrive where it landed.

    This guards against the problem found on 2026-10-30, when uploads went
    to the top of the drive instead of the chosen folder (RESEARCH.md §5).
    Returns True only if the file arrived inside the chosen folder.
    """
    probe = mount_path / PROBE_NAME
    probe.write_text("Test file written by onedrive-ondemand setup. Safe to delete.\n")
    for _ in range(20):  # about a minute
        time.sleep(3)
        try:
            # Read the token each time: rclone may have just renewed it.
            access_token = rclone.read_token()["access_token"]
            names = [child["name"] for child in graph.list_children(access_token, folder_id)]
            if PROBE_NAME in names:
                # Deleting through the mount moves it to the OneDrive recycle bin.
                probe.unlink()
                return True
            if graph.find_top_item(access_token, PROBE_NAME) is not None:
                return False
        except (graph.GraphError, ValueError, TypeError):
            pass  # e.g. caught rclone mid-way through saving a new token; try again
    return False


def install_services():
    """Write the systemd units from their templates and start them at every login."""
    config.SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    for name in (config.MOUNT_SERVICE, config.DAEMON_SERVICE):
        text = (SOURCE_DIR / "data" / name).read_text(encoding="utf-8")
        text = text.replace("@SOURCE_DIR@", str(SOURCE_DIR))
        text = text.replace("@PYTHON@", sys.executable)
        (config.SYSTEMD_USER_DIR / name).write_text(text, encoding="utf-8")
    systemctl("daemon-reload")
    for name in (config.MOUNT_SERVICE, config.DAEMON_SERVICE):
        systemctl("enable", name)
        systemctl("restart", name)


def wait_for_mount(mount_path, seconds=30):
    """Wait for the folder to show OneDrive. Returns True once it does."""
    for _ in range(seconds):
        if os.path.ismount(mount_path):
            return True
        time.sleep(1)
    return False


def run():
    print("OneDrive On-Demand setup\n")
    check_requirements()

    settings = config.load_settings()
    folder = settings["remote_folder"]
    if not folder:
        # CLAUDE.md safety rule 1: never the whole drive during development.
        sys.exit("Showing the whole drive isn't supported yet. "
                 f"Set \"remote_folder\" in {config.SETTINGS_FILE}.")
    mount_path = config.mount_path(settings)

    # If setup ran before, stop everything so we can change things safely.
    systemctl("stop", config.DAEMON_SERVICE)
    systemctl("stop", config.MOUNT_SERVICE)
    check_mount_folder(mount_path)

    if config.RCLONE_CONF.exists() and not ask("You're already signed in. Sign in again?", False):
        keep_sign_in()
    else:
        sign_in()
    folder_item = ensure_folder(fresh_access_token(), folder)

    config.save_settings(settings)
    mount_path.mkdir(parents=True, exist_ok=True)

    print("\nStarting the services...")
    install_services()
    if not wait_for_mount(mount_path):
        print(f"\nThe mount didn't start. See what went wrong with:\n"
              f"  journalctl --user -u {config.MOUNT_SERVICE} -n 30")
        sys.exit(1)

    print(f"Checking that files saved in {mount_path} upload into '{folder}'...")
    if not check_upload_location(mount_path, folder_item["id"]):
        # Safety first: turn the mount off so nothing else goes to the wrong place.
        systemctl("disable", "--now", config.DAEMON_SERVICE)
        systemctl("disable", "--now", config.MOUNT_SERVICE)
        sys.exit(f"\nSTOPPED: a test file saved in {mount_path} did not arrive in '{folder}' "
                 "on OneDrive, so the mount has been turned off.\n"
                 f"If '{PROBE_NAME}' is at the top of your OneDrive (onedrive.com), "
                 "you can delete it.")

    print(f"\nDone. Your OneDrive folder '{folder}' is now at {mount_path}")
    print("It starts by itself every time you log in.")
