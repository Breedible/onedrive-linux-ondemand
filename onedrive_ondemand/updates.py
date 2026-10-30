"""Checking for and installing updates from GitHub (tray: "Check for updates").

Two ways this program can be installed, so two ways to update:
- From the .deb package: we look at the newest release on GitHub. If it's
  newer than this version, we download its .deb and install it with
  `pkexec apt-get install`, which asks for your password in a window.
- From a source folder (a `git clone`): we run `git pull` there.
After either one, the services and the tray icon are restarted so the new
version is used straight away.

Publishing a release (for the maintainer): see README.md, "Making a release".
"""

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from onedrive_ondemand import __version__, config

REPO = "Breedible/onedrive-linux-ondemand"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


class UpdateError(Exception):
    pass


def parse_version(text):
    """"v0.2.1" or "0.2.1" -> (0, 2, 1). None if it isn't a version number."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(latest, current=__version__):
    latest, current = parse_version(latest), parse_version(current)
    return latest is not None and current is not None and latest > current


def source_dir():
    """The git checkout we're running from, or None when installed from the .deb."""
    folder = Path(__file__).resolve().parent.parent
    return folder if (folder / ".git").exists() else None


def latest_release():
    """The newest GitHub release: {"version", "deb_url", "page", "notes"}, or None if there is none."""
    request = urllib.request.Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None  # no releases published yet
        raise UpdateError(f"GitHub answered with an error ({error.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise UpdateError(f"Couldn't reach GitHub ({error}).") from None
    deb = next((a["browser_download_url"] for a in data.get("assets", [])
                if a.get("name", "").endswith("_all.deb")), None)
    return {"version": data.get("tag_name", "").lstrip("v"), "deb_url": deb,
            "page": data.get("html_url"), "notes": data.get("body") or ""}


def git(*args, timeout=60):
    result = subprocess.run(["git", "-C", str(source_dir()), *args],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise UpdateError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def check():
    """Is there an update? Returns {"available": bool, "summary": text, ...}."""
    if source_dir() is not None:
        git("fetch", "--quiet", timeout=120)
        try:
            behind = int(git("rev-list", "--count", "HEAD..@{upstream}"))
        except (UpdateError, ValueError):
            raise UpdateError("This source folder doesn't follow a branch on GitHub, "
                              "so it can't be updated automatically.") from None
        if not behind:
            return {"available": False, "summary": "You have the latest version."}
        changes = git("log", "--format=- %s", "HEAD..@{upstream}").splitlines()
        return {"available": True, "kind": "source",
                "summary": f"{behind} new change(s) on GitHub:\n" + "\n".join(changes[:10])}

    release = latest_release()
    if release is None or not is_newer(release["version"]):
        return {"available": False, "summary": f"You have the latest version ({__version__})."}
    if not release["deb_url"]:
        return {"available": False,
                "summary": f"Version {release['version']} is out, but has no .deb file yet."}
    return {"available": True, "kind": "deb", "version": release["version"],
            "deb_url": release["deb_url"],
            "summary": f"Version {release['version']} is available (you have {__version__})."}


def install(update):
    """Install an update found by check(). Raises UpdateError if it didn't work."""
    if update["kind"] == "source":
        if git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("The source folder has changes of its own, so it wasn't updated. "
                              "Run 'git status' there to see them.")
        git("pull", "--ff-only", timeout=300)
    else:
        folder = Path(tempfile.mkdtemp(prefix="onedrive-ondemand-update-"))
        deb = folder / f"onedrive-ondemand_{update['version']}_all.deb"
        try:
            urllib.request.urlretrieve(update["deb_url"], deb)
        except (urllib.error.URLError, OSError) as error:
            raise UpdateError(f"Couldn't download the update ({error}).") from None
        # apt-get reads the file as root but from inside our folder: make sure it can.
        os.chmod(folder, 0o755)
        # pkexec shows a password window; apt-get then installs the new version.
        result = subprocess.run(["pkexec", "apt-get", "install", "-y", str(deb)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise UpdateError("Installing the update didn't work (or was cancelled).\n"
                              + result.stderr.strip()[-500:])
    restart_everything()


def restart_everything():
    """Restart every account's services so the new version runs.

    Restarting the mount closes files that are open from OneDrive, but
    nothing is lost: changes waiting to upload stay in the cache.
    """
    if source_dir() is not None:
        # A new version may come with changed services, Nemo extension or icons.
        from onedrive_ondemand import setup_wizard
        setup_wizard.install_shared_files()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    for account in config.accounts():
        for service in (account.mount_service, account.daemon_service):
            subprocess.run(["systemctl", "--user", "restart", service], capture_output=True)
