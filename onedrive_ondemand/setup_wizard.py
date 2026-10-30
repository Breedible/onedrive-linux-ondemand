"""Setting things up: `setup`, `add-account`, `remove-account`, `sign-in`, `uninstall`.

- `setup` installs our services and Nemo extension, moves a version 0.1 setup
  into the new layout, and adds your first account if there is none yet.
  Safe to run again: it restarts and re-checks every account.
- `add-account` signs in to one more OneDrive account (for example a work
  account next to your personal one). Each account gets its own folder,
  such as ~/OneDrive - Work, like on Windows.
- `remove-account` signs out of one account and removes it from this computer.
  Your files on OneDrive are not touched.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from onedrive_ondemand import config, graph, ipc, mount, previews, rclone, thumbnailer

# The folder that holds the onedrive_ondemand package: the repo when running
# from a source checkout, or Python's system folder when installed from the .deb.
SOURCE_DIR = Path(__file__).resolve().parent.parent
# Installed from the .deb? Then the services, Nemo extension, icons and menu
# entries are already in /usr, and setup must not add per-user copies.
INSTALLED = not (SOURCE_DIR / "data").is_dir()
# The command that runs this program.
LAUNCHER = "/usr/bin/onedrive-ondemand" if INSTALLED else str(SOURCE_DIR / "bin" / "onedrive-ondemand")

# Per-user files that setup creates when running from a source checkout.
USER_FILES = [
    config.SYSTEMD_USER_DIR / config.MOUNT_TEMPLATE,
    config.SYSTEMD_USER_DIR / config.DAEMON_TEMPLATE,
    config.NEMO_EXTENSION_DIR / "onedrive_ondemand_nemo.py",
    config.AUTOSTART_DIR / "onedrive-ondemand-tray.desktop",
    config.APPLICATIONS_DIR / "onedrive-ondemand.desktop",
]
# Version 0.1 had one service of each kind instead of one per account.
OLD_USER_FILES = [
    config.SYSTEMD_USER_DIR / config.OLD_MOUNT_SERVICE,
    config.SYSTEMD_USER_DIR / config.OLD_DAEMON_SERVICE,
]
# Hidden file setup writes through the mount to check where uploads land.
PROBE_NAME = ".onedrive-ondemand-setup-check.txt"
# Settings that are the same for every account (the tray changes them for all).
SHARED_SETTINGS = ("evict_after_days", "min_free_space_percent", "thumbnails")
RCLONE_INSTALL = "curl -fsSL https://rclone.org/install.sh | sudo bash"


# --- small helpers -------------------------------------------------------------

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


def ask_text(question, default):
    """Ask for some text. Pressing Enter picks the default."""
    answer = input(f"{question} [{default}] ").strip()
    return answer or default


def systemctl(*args):
    """Run `systemctl --user ...`. Returns True if it succeeded."""
    return subprocess.run(["systemctl", "--user", *args], capture_output=True).returncode == 0


def run_in_terminal(*args):
    """Open a terminal window running `onedrive-ondemand <args>` (used by the tray and menu)."""
    command = shlex.join([LAUNCHER, *args])
    script = f"{command}; echo; read -p 'Press Enter to close this window. '"
    subprocess.Popen(["x-terminal-emulator", "-e", "bash", "-c", script],
                     start_new_session=True)


def check_name(name, others):
    """Return a problem with an account name, or None if it's fine."""
    if not name or len(name) > 40:
        return "Use a name of 1 to 40 characters."
    if "/" in name or name.startswith("."):
        return "The name can't contain '/' or start with '.'."
    if name.lower() in (other.lower() for other in others):
        return "Another account already has that name."
    return None


# --- requirements ----------------------------------------------------------------

def rclone_problem():
    """Why rclone can't be used (missing or too old), or None if it's fine."""
    if not rclone.find_rclone():
        return "rclone isn't installed."
    version = rclone.installed_version()
    if version is None or version < rclone.MIN_VERSION:
        have = ".".join(map(str, version)) if version else "an unknown version"
        need = ".".join(map(str, rclone.MIN_VERSION))
        return f"rclone {have} is installed, but {need} or newer is needed."
    return None


def check_requirements():
    """Stop with a clear message if something is missing. Offers to install rclone."""
    problem = rclone_problem()
    if problem:
        print(problem)
        print("OneDrive On-Demand uses rclone to talk to OneDrive. Mint's own rclone")
        print("package is too old, so it has to come from rclone.org.")
        if not shutil.which("curl"):
            sys.exit("Install curl first (sudo apt install curl), then run setup again.")
        if not ask("Install the latest rclone from rclone.org now? (You'll be asked for "
                   "your password.)", True):
            sys.exit("Setup stopped. Install rclone with:\n  " + RCLONE_INSTALL)
        # rclone's own installer; it exits with 3 if the newest version is already there.
        subprocess.run(["bash", "-c", RCLONE_INSTALL])
        problem = rclone_problem()
        if problem:
            sys.exit(f"{problem} Install it from https://rclone.org/downloads/ and run setup again.")
        print("rclone is installed.\n")
    if not shutil.which("fusermount3"):
        sys.exit("FUSE isn't installed. Run:  sudo apt install fuse3")


def check_mount_folder(path):
    """The mount folder must be empty (or not exist yet)."""
    if os.path.ismount(path):
        sys.exit(f"{path} is still mounted by something else. Unmount it first:\n"
                 f"  fusermount3 -u '{path}'")
    if path.is_dir() and any(path.iterdir()):
        sys.exit(f"{path} isn't empty. Move its contents somewhere else first, "
                 "because OneDrive will be shown in this folder.")


def nemo_python_installed():
    return subprocess.run(["dpkg-query", "-W", "-f=${Status}", "nemo-python"],
                          capture_output=True, text=True).stdout.startswith("install ok")


# --- signing in ------------------------------------------------------------------

def browser_sign_in():
    """Browser sign-in. Returns (token JSON text, the drive's details from Microsoft)."""
    token = rclone.authorize()
    drive = graph.my_drive(json.loads(token)["access_token"])
    return token, drive


def sign_in_again(account):
    """Browser sign-in for an existing account (tray: "Sign in again"; CLI: sign-in).

    Must be the same OneDrive account as before. Restarts the account's
    services so rclone picks up the new token.
    """
    values = rclone.read_rclone_conf(account.rclone_conf) or {}
    token, drive = browser_sign_in()
    if values.get("drive_id") and drive["id"] != values["drive_id"]:
        raise RuntimeError(f"That's a different OneDrive account than {account.display_name()}. "
                           "To add it, use 'Add an account' instead.")
    systemctl("stop", account.daemon_service)
    systemctl("stop", account.mount_service)
    rclone.write_rclone_conf(token, drive["id"], drive["driveType"], account.rclone_conf)
    systemctl("start", account.mount_service)
    systemctl("start", account.daemon_service)


def fresh_access_token(account):
    """A working access token from an account's rclone config.

    Access tokens only last about an hour. `rclone about` (which only reads
    your storage quota) makes rclone renew the token and save it if needed.
    """
    subprocess.run(["rclone", "about", f"{config.REMOTE_NAME}:", "--config", str(account.rclone_conf)],
                   capture_output=True, timeout=120)
    return rclone.read_token(account.rclone_conf)["access_token"]


def ensure_folder(access_token, folder_name):
    """Find the folder to show at the top of your OneDrive, creating it if you agree."""
    try:
        item = graph.find_top_item(access_token, folder_name)
        if item is None:
            print(f"There is no folder called '{folder_name}' in this OneDrive yet.")
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


def check_upload_location(account, mount_path, folder_id):
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
            access_token = rclone.read_token(account.rclone_conf)["access_token"]
            names = [child["name"] for child in graph.list_children(access_token, folder_id)]
            if PROBE_NAME in names:
                # Deleting through the mount moves it to the OneDrive recycle bin.
                probe.unlink()
                return True
            if graph.find_top_item(access_token, PROBE_NAME) is not None:
                return False
        except (graph.GraphError, ValueError, TypeError, OSError):
            pass  # e.g. caught rclone mid-way through saving a new token; try again
    return False


# --- installing our files ------------------------------------------------------------

def install_shared_files():
    """Services, Nemo extension, icons and menu entries (copies only from a source checkout)."""
    for path in OLD_USER_FILES:
        path.unlink(missing_ok=True)
    if INSTALLED:
        remove_user_files()  # left over from running from a source checkout
        systemctl("daemon-reload")
        return
    config.SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    for name in (config.MOUNT_TEMPLATE, config.DAEMON_TEMPLATE):
        text = (SOURCE_DIR / "data" / name).read_text(encoding="utf-8")
        text = text.replace("@SOURCE_DIR@", str(SOURCE_DIR))
        text = text.replace("@PYTHON@", sys.executable)
        (config.SYSTEMD_USER_DIR / name).write_text(text, encoding="utf-8")
    systemctl("daemon-reload")

    text = (SOURCE_DIR / "nemo" / "onedrive_ondemand_nemo.py").read_text(encoding="utf-8")
    text = text.replace("@SOURCE_DIR@", str(SOURCE_DIR))
    config.NEMO_EXTENSION_DIR.mkdir(parents=True, exist_ok=True)
    (config.NEMO_EXTENSION_DIR / "onedrive_ondemand_nemo.py").write_text(text, encoding="utf-8")
    shutil.copytree(SOURCE_DIR / "data" / "icons" / "hicolor", config.ICON_DIR, dirs_exist_ok=True)

    icon = str(config.ICON_DIR / "scalable" / "status" / "onedrive-ondemand-ok.svg")
    for name, folder in (("onedrive-ondemand-tray.desktop", config.AUTOSTART_DIR),
                         ("onedrive-ondemand.desktop", config.APPLICATIONS_DIR)):
        text = (SOURCE_DIR / "data" / name).read_text(encoding="utf-8")
        text = text.replace("@LAUNCHER@", LAUNCHER).replace("@ICON@", icon)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")


def remove_user_files():
    """Delete the per-user copies of our services, Nemo extension and menu entries."""
    for path in USER_FILES + OLD_USER_FILES:
        path.unlink(missing_ok=True)


def start_tray():
    """Start the tray icon in the background (it exits by itself if already running)."""
    subprocess.Popen([LAUNCHER, "tray"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def restart_tray():
    """Start the tray icon, replacing one that's already running (e.g. an older version)."""
    subprocess.run(["pkill", "-f", "onedrive_ondemand tray$"], capture_output=True)
    time.sleep(0.5)
    start_tray()


def add_bookmark(path, name):
    """Add the folder to the file manager's sidebar (GTK bookmarks), like on Windows."""
    line = f"{Path(path).as_uri()} {name}"
    try:
        lines = config.GTK_BOOKMARKS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    if any(old.split(" ", 1)[0] == Path(path).as_uri() for old in lines):
        return
    config.GTK_BOOKMARKS.parent.mkdir(parents=True, exist_ok=True)
    config.GTK_BOOKMARKS.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")


def remove_bookmark(path):
    try:
        lines = config.GTK_BOOKMARKS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    kept = [line for line in lines if line.split(" ", 1)[0] != Path(path).as_uri()]
    if kept != lines:
        config.GTK_BOOKMARKS.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")


# --- picture previews (DESIGN.md §8) ----------------------------------------------------

def set_nemo_previews(show, mount_path):
    """Turn Nemo's picture previews on or off for one OneDrive folder only.

    Nemo has a per-folder "Show Thumbnails" switch, stored as
    metadata::show-thumbnails; with inherit-show-thumbnails on, sub-folders
    follow the folder above them.
    """
    if show:
        subprocess.run(["gio", "set", "-t", "unset", str(mount_path), "metadata::show-thumbnails"],
                       capture_output=True)
    else:
        subprocess.run(["gsettings", "set", "org.nemo.preferences", "inherit-show-thumbnails", "true"],
                       capture_output=True)
        subprocess.run(["gio", "set", str(mount_path), "metadata::show-thumbnails", "false"],
                       capture_output=True)


def apply_thumbnail_choice(show, accounts=None):
    """Turn picture previews on or off for the given accounts (default: all).

    Previews come from our thumbnailer, which uses OneDrive's own small
    preview pictures, so turning them on doesn't download your photos. The
    order matters: our thumbnailer must be in place *before* Nemo is allowed
    to make previews, or Nemo would read (download) the files itself.
    """
    accounts = config.accounts() if accounts is None else accounts
    if show:
        thumbnailer.install(LAUNCHER)
    for account in accounts:
        set_nemo_previews(show, account.mount_path())
        if not show:
            try:
                ipc.request(account.daemon_socket, "previews_off", timeout=30)
            except (ipc.DaemonNotRunning, OSError, ValueError):
                previews.delete_all_under(account.mount_path())
    # Our thumbnailer is only needed while some account shows previews.
    if not any(a.settings().get("thumbnails") for a in config.accounts()):
        thumbnailer.uninstall()


def save_setting(account, key, value):
    """Change one setting. Goes through the account's daemon if it runs, so the
    daemon doesn't later write its own (older) copy of the settings back."""
    try:
        if ipc.request(account.daemon_socket, "settings", timeout=10, set={key: value}).get("ok"):
            return
    except (ipc.DaemonNotRunning, OSError, ValueError):
        pass
    settings = account.settings()
    settings[key] = value
    account.save_settings(settings)


def ask_about_previews(account):
    show = account.settings().get("thumbnails")
    if show is None:
        print("\nNemo can show small previews of photos, videos and PDFs. They come from")
        print("OneDrive's own preview pictures, so your files are NOT downloaded for this.")
        print("Previews are deleted again a couple of minutes after you leave a folder.")
        show = ask("Show previews in the OneDrive folder?", True)
        save_setting(account, "thumbnails", show)
    apply_thumbnail_choice(show, [account])


# --- moving a version 0.1 setup -----------------------------------------------------------

def migrate_old_layout():
    """Move a single-account setup from version 0.1 into the "personal" account.

    Version 0.1 kept its sign-in, settings, cache and pins straight in
    ~/.config/onedrive-ondemand and ~/.local/share/onedrive-ondemand, and
    showed the files in ~/OneDrive. Now each account has its own folders,
    and the files appear in ~/OneDrive - Personal (like on Windows).
    Downloaded files, pins and changes waiting to upload all come along:
    everything is just moved (renamed), not copied.
    Returns the account, or None if there was nothing to move.
    """
    if not config.OLD_RCLONE_CONF.exists():
        return None
    account = config.Account("personal")
    if account.is_set_up():
        print(f"Note: an old setup is still in {config.CONFIG_DIR}, but the 'personal' "
              "account already exists, so it was left alone.")
        return None

    print("Moving your existing OneDrive to the new layout (one folder per account)...")
    old_settings = config.load_settings(config.OLD_SETTINGS_FILE)
    old_mount = config.mount_path(old_settings)
    # Stopping the mount is safe: changes that haven't uploaded yet stay in
    # the cache and upload when the new mount starts.
    systemctl("disable", "--now", config.OLD_DAEMON_SERVICE)
    systemctl("disable", "--now", config.OLD_MOUNT_SERVICE)
    for _ in range(30):
        if not os.path.ismount(old_mount):
            break
        time.sleep(1)
    else:
        sys.exit(f"{old_mount} is still mounted. Close the programs using files in it, "
                 "then run setup again.")
    for path in OLD_USER_FILES:
        path.unlink(missing_ok=True)
    systemctl("daemon-reload")

    account.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    account.data_dir.mkdir(parents=True, exist_ok=True)
    settings = dict(old_settings, name="Personal")
    if old_mount == Path.home() / "OneDrive":
        settings["mount_path"] = "~/OneDrive - Personal"
    account.save_settings(settings)
    config.OLD_SETTINGS_FILE.unlink(missing_ok=True)
    if config.OLD_CACHE_DIR.exists():
        os.replace(config.OLD_CACHE_DIR, account.cache_dir)
    for suffix in ("", "-wal", "-shm"):  # SQLite may keep two helper files next to it
        old = Path(str(config.OLD_STATE_DB) + suffix)
        if old.exists():
            os.replace(old, Path(str(account.state_db) + suffix))
    # Last, so that if anything above fails, running setup again carries on.
    os.replace(config.OLD_RCLONE_CONF, account.rclone_conf)

    new_mount = config.mount_path(settings)
    if old_mount != new_mount and old_mount.is_dir() and not os.path.ismount(old_mount):
        os.chmod(old_mount, 0o755)  # the old mount service made it read-only
        try:
            old_mount.rmdir()  # only works if it's empty, which it should be
            print(f"Removed the old, empty {old_mount} folder.")
        except OSError:
            print(f"Left {old_mount} in place because it isn't empty.")
    print(f"Done: your files will now appear in {new_mount}")
    return account


# --- accounts -------------------------------------------------------------------------

def wait_for_mount(mount_path, seconds=60):
    """Wait for the folder to show OneDrive. Returns True once it does."""
    for _ in range(seconds):
        if os.path.ismount(mount_path):
            return True
        time.sleep(1)
    return False


def start_and_check(account):
    """(Re)start one account's services and check that uploads land in the right place."""
    settings = account.settings()
    folder = settings["remote_folder"]
    name = config.display_name(settings)
    if not folder:
        # CLAUDE.md safety rule 1: never the whole drive during development.
        sys.exit("Showing the whole drive isn't supported yet. "
                 f"Set \"remote_folder\" in {account.settings_file}.")
    mount_path = config.mount_path(settings)

    systemctl("stop", account.daemon_service)
    systemctl("stop", account.mount_service)
    check_mount_folder(mount_path)
    folder_item = ensure_folder(fresh_access_token(account), folder)
    mount_path.mkdir(parents=True, exist_ok=True)

    print(f"Starting {name}...")
    for service in (account.mount_service, account.daemon_service):
        systemctl("enable", service)
        systemctl("restart", service)
    if not wait_for_mount(mount_path):
        print(f"\nThe mount didn't start. See what went wrong with:\n"
              f"  journalctl --user -u {account.mount_service} -n 30")
        sys.exit(1)
    if account.mode() == "offline":
        print("OneDrive can't be reached right now, so only downloaded files are shown "
              "(read-only) until you're back online.")
        return

    print(f"Checking that files saved in {mount_path} upload into '{folder}'...")
    if not check_upload_location(account, mount_path, folder_item["id"]):
        # Safety first: turn the mount off so nothing else goes to the wrong place.
        systemctl("disable", "--now", account.daemon_service)
        systemctl("disable", "--now", account.mount_service)
        sys.exit(f"\nSTOPPED: a test file saved in {mount_path} did not arrive in '{folder}' "
                 "on OneDrive, so this account's mount has been turned off.\n"
                 f"If '{PROBE_NAME}' is at the top of that OneDrive (onedrive.com), "
                 "you can delete it.")
    add_bookmark(mount_path, name)
    print(f"{name} is ready: {mount_path}")


def add_account():
    """Sign in to one more OneDrive account and show it in its own folder."""
    existing = config.accounts()
    drives = {}
    for account in existing:
        drive_id = (rclone.read_rclone_conf(account.rclone_conf) or {}).get("drive_id")
        drives[drive_id] = account

    print("\nA Microsoft sign-in page will open in your browser.")
    print("Sign in and allow access. If the browser doesn't open, copy the")
    print("http://127.0.0.1:53682/... link that rclone prints below.")
    if existing:
        print("Adding a different account than one you already use? If the page signs you")
        print("straight in to the wrong one, choose 'Use another account', or open the")
        print("link in a private window.")
    print()
    token, drive = browser_sign_in()
    if drive["id"] in drives:
        sys.exit(f"\nThat account is already set up as {drives[drive['id']].display_name()}.")

    drive_type = drive["driveType"]  # "personal", "business" or "documentLibrary"
    owner = drive.get("owner", {}).get("user", {}).get("displayName", "you")
    kind = "personal" if drive_type == "personal" else "work or school"
    print(f"\nSigned in: the {kind} OneDrive of {owner}.")

    names = [a.settings()["name"] for a in existing]
    default = "Personal" if drive_type == "personal" else "Work"
    if default.lower() in (n.lower() for n in names):
        default = owner if owner != "you" else default + " 2"
    while True:
        name = ask_text("What should this account be called? Its folder will be "
                        "'OneDrive - <name>'.", default)
        problem = check_name(name, names)
        if not problem:
            break
        print(problem)

    account = config.Account(config.new_account_id(name, [a.id for a in existing]))
    settings = dict(config.DEFAULT_SETTINGS, name=name, mount_path=f"~/OneDrive - {name}")
    if existing:  # keep the shared settings the same for every account
        first = existing[0].settings()
        settings.update({key: first[key] for key in SHARED_SETTINGS})
    check_mount_folder(config.mount_path(settings))

    account.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    account.save_settings(settings)
    rclone.write_rclone_conf(token, drive["id"], drive_type, account.rclone_conf)
    print(f"Saved the sign-in to {account.rclone_conf} (only you can read it).")

    start_and_check(account)
    ask_about_previews(account)
    return account


def dirty_cache_files(account):
    """Files in an account's cache whose change hasn't been uploaded yet."""
    meta_root = account.cache_dir / "vfsMeta"
    for folder, _dirs, files in os.walk(meta_root):
        for name in files:
            path = Path(folder) / name
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("Dirty"):
                    yield str(path.relative_to(meta_root))
            except (OSError, ValueError):
                continue


def _remove_folder(path, inside):
    """shutil.rmtree, but only for a folder strictly inside `inside` (our own folders)."""
    real, root = Path(os.path.realpath(path)), Path(os.path.realpath(inside))
    if real == root or not real.is_relative_to(root):
        raise RuntimeError(f"Refusing to delete {path}: it isn't inside {inside}.")
    shutil.rmtree(real, ignore_errors=True)


def remove_account(account, assume_yes=False):
    """Sign out of one account and remove it from this computer (like "Unlink this PC").

    Stops its services, then deletes its sign-in, settings, pins and the
    downloaded copies of its files. Nothing on OneDrive is changed.
    """
    name = account.display_name()
    mount_path = account.mount_path()
    print(f"This signs out of {name} and removes it from this computer:")
    print(f"  - {mount_path} stops showing your files,")
    print("  - downloaded copies and 'always keep' choices for it are deleted here.")
    print("Your files on OneDrive itself are not changed.")
    if not assume_yes and not ask(f"Remove {name}?", False):
        sys.exit("Nothing was changed.")

    # Give pending uploads a chance first.
    try:
        waiting = ipc.request(account.daemon_socket, "info", timeout=5).get("queued", 0)
    except (ipc.DaemonNotRunning, OSError, ValueError):
        waiting = 0
    if waiting:
        print(f"{waiting} change(s) are still waiting to upload.")
        if not ask("Remove the account anyway? Those changes would be lost.", False):
            sys.exit("Nothing was changed. Try again once uploads have finished "
                     "(see the tray icon).")
    systemctl("disable", "--now", account.daemon_service)
    systemctl("disable", "--now", account.mount_service)
    dirty = list(dirty_cache_files(account))
    if dirty and not waiting:
        print(f"{len(dirty)} file(s) have changes that were never uploaded, e.g. {dirty[0]}")
        if not ask("Remove the account anyway? Those changes would be lost.", False):
            systemctl("enable", "--now", account.mount_service)
            systemctl("enable", "--now", account.daemon_service)
            sys.exit("Nothing was changed. The changes upload while OneDrive is running.")
    if os.path.ismount(mount_path):
        mount.lazy_unmount(mount_path)

    remove_bookmark(mount_path)
    set_nemo_previews(True, mount_path)  # remove our per-folder setting
    previews.delete_all_under(mount_path)
    _remove_folder(account.data_dir, config.ACCOUNTS_DATA_DIR)
    _remove_folder(account.config_dir, config.ACCOUNTS_CONFIG_DIR)
    if mount_path.is_dir() and not os.path.ismount(mount_path):
        os.chmod(mount_path, 0o755)
        try:
            mount_path.rmdir()  # only if empty
        except OSError:
            pass
    if not any(a.settings().get("thumbnails") for a in config.accounts()):
        thumbnailer.uninstall()
    print(f"Signed out of {name}.")


def uninstall():
    """Stop everything and remove what setup added. Your files and settings stay.

    Downloaded files (the cache) and the sign-ins are kept, because the cache
    may hold changes that haven't been uploaded yet. The message says how to
    remove them once you're sure.
    """
    accounts = config.accounts()
    for account in accounts:
        systemctl("disable", "--now", account.daemon_service)
        systemctl("disable", "--now", account.mount_service)
    subprocess.run(["pkill", "-f", "onedrive_ondemand tray$"], capture_output=True)
    if not INSTALLED:
        remove_user_files()
    systemctl("daemon-reload")
    thumbnailer.uninstall()
    dirty = []
    for account in accounts:
        mount_path = account.mount_path()
        if mount_path.is_dir() and not os.path.ismount(mount_path):
            os.chmod(mount_path, 0o755)  # the mount service made it read-only
        set_nemo_previews(True, mount_path)  # remove our "no previews" folder setting
        previews.delete_all_under(mount_path)
        remove_bookmark(mount_path)
        dirty += [f"{account.display_name()}: {rel}" for rel in dirty_cache_files(account)]

    print("OneDrive On-Demand is stopped and won't start at login any more.")
    print("Your files on OneDrive are untouched.")
    if dirty:
        print(f"\nWARNING: {len(dirty)} file(s) have changes that were never uploaded, e.g.:")
        for line in dirty[:5]:
            print("  " + line)
        print("Run  onedrive-ondemand setup  again to upload them before deleting anything.")
    else:
        print("\nNo changes are waiting to upload. To also delete the downloaded copies,")
        print("your settings and the sign-ins, run:")
        print(f"  rm -r {config.DATA_DIR} {config.CONFIG_DIR}")
    if INSTALLED:
        print("\nTo remove the program itself:  sudo apt remove onedrive-ondemand")


def run():
    print("OneDrive On-Demand setup\n")
    check_requirements()
    migrate_old_layout()
    install_shared_files()

    accounts = config.accounts()
    if not accounts:
        print("Let's add your OneDrive account.")
        add_account()
    else:
        for account in accounts:
            print()
            start_and_check(account)
            ask_about_previews(account)

    restart_tray()
    if not nemo_python_installed():
        print("\nNemo emblems and the OneDrive right-click menu need one more package:\n"
              "  sudo apt install nemo-python")
    print("\nDone. OneDrive starts by itself every time you log in, and the OneDrive")
    print("icon in the panel shows what it's doing.")
    print("Close all Nemo windows once (or run:  nemo -q) so it loads the OneDrive menu.")
    print("To add another account (e.g. work):  onedrive-ondemand add-account")
