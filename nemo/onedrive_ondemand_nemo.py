"""Nemo extension for OneDrive On-Demand: status emblems and a right-click menu.

Nemo loads this file through nemo-python. It never touches files itself: it
asks the daemon (over its socket) for each file's state, and sends pin/free
requests to it. Every request has a short timeout, so Nemo can't freeze even
if the daemon hangs.

Emblems (icons in data/icons/.../emblems):
    onedrive-online   cloud           online-only
    onedrive-local    outline tick    locally available
    onedrive-pinned   solid tick      always keep on this device
    onedrive-syncing  arrows          uploading, or pin still downloading
    onedrive-error    red cross       upload keeps failing / pin download failed
"""

import os
import subprocess
import sys
import threading

# When run from a source checkout, setup replaces this with the repo folder so
# Python can find our package. (In the .deb the package is installed normally.)
SOURCE_DIR = "@SOURCE_DIR@"
if os.path.isdir(SOURCE_DIR) and SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

import gi  # noqa: E402

gi.require_version("Nemo", "3.0")
from gi.repository import GLib, GObject, Gio, Nemo  # noqa: E402

from onedrive_ondemand import config, ipc  # noqa: E402

EMBLEMS = {
    "online": "onedrive-online",
    "local": "onedrive-local",
    "pinned": "onedrive-pinned",
    "syncing": "onedrive-syncing",
    "error": "onedrive-error",
}
STATUS_TIMEOUT = 1.0      # seconds Nemo will wait for the daemon, at most
BATCH_DELAY_MS = 30       # collect requests this long, then ask for all at once
REFRESH_SECONDS = 3       # how often shown emblems are re-checked
MAX_TRACKED = 3000        # how many shown files we keep re-checking


class OneDriveExtension(GObject.GObject, Nemo.InfoProvider, Nemo.MenuProvider,
                        Nemo.NameAndDescProvider):
    def __init__(self):
        super().__init__()
        settings = config.load_settings()
        self.mount = str(config.mount_path(settings))
        self.waiting = []        # (file, handle, closure, path) waiting for a batch
        self.batch_timer = None
        self.shown = {}          # path -> (NemoFileInfo, state) for refreshing
        self.dirs = set()        # shown paths that are folders
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    # --- which files are ours ----------------------------------------------

    def path_of(self, file):
        """The local path of a Nemo file if it's inside ~/OneDrive, else None."""
        location = file.get_location()
        path = location.get_path() if location else None
        if path and path.startswith(self.mount + os.sep):
            return path
        return None

    # --- emblems --------------------------------------------------------------

    def update_file_info_full(self, provider, handle, closure, file):
        """Nemo asks about one file. We answer later, in a batch."""
        path = self.path_of(file)
        if path is None:
            return Nemo.OperationResult.COMPLETE
        if file.is_directory():
            self.dirs.add(path)
        self.waiting.append((file, handle, closure, path))
        if self.batch_timer is None:
            self.batch_timer = GLib.timeout_add(BATCH_DELAY_MS, self.answer_batch, provider)
        return Nemo.OperationResult.IN_PROGRESS

    def cancel_update(self, provider, handle):
        self.waiting = [w for w in self.waiting if w[1] != handle]

    def answer_batch(self, provider):
        self.batch_timer = None
        batch, self.waiting = self.waiting, []
        states = self.ask_states([w[3] for w in batch])
        for file, handle, closure, path in batch:
            state = states.get(path)
            if state in EMBLEMS:
                file.add_emblem(EMBLEMS[state])
            self.remember(path, file, state)
            Nemo.info_provider_update_complete_invoke(
                closure, provider, handle, Nemo.OperationResult.COMPLETE)
        return False  # run once

    def ask_states(self, paths):
        if not paths:
            return {}
        try:
            answer = ipc.request("status", timeout=STATUS_TIMEOUT, paths=paths,
                                 dirs=[p for p in paths if p in self.dirs])
        except (ipc.DaemonNotRunning, OSError, ValueError):
            return {}  # no daemon: just show no emblems
        return answer.get("states", {}) if answer.get("ok") else {}

    def remember(self, path, file, state):
        self.shown.pop(path, None)
        self.shown[path] = (file, state)
        while len(self.shown) > MAX_TRACKED:
            oldest = next(iter(self.shown))  # forget the oldest
            self.shown.pop(oldest)
            self.dirs.discard(oldest)

    def refresh(self):
        """Every few seconds, redraw emblems whose state changed."""
        if self.shown:
            paths = list(self.shown)
            states = self.ask_states(paths)
            for path in paths:
                if path in states and path in self.shown:
                    file, old = self.shown[path]
                    if states[path] != old:
                        self.shown[path] = (file, states[path])
                        file.invalidate_extension_info()  # Nemo asks us again
        return True  # keep the timer running

    # --- right-click menu -------------------------------------------------------

    def get_file_items(self, window, files):
        paths = [self.path_of(f) for f in files]
        if not paths or None in paths:
            return []  # only when everything selected is inside ~/OneDrive
        self.dirs.update(p for p, f in zip(paths, files) if f.is_directory())
        states = self.ask_states(paths)
        all_pinned = bool(states) and all(states.get(p) == "pinned" for p in paths)

        top = Nemo.MenuItem(name="OneDriveOnDemand::Top", label="OneDrive",
                            icon="emblem-onedrive-pinned")
        submenu = Nemo.Menu()
        top.set_submenu(submenu)

        if all_pinned:
            keep = Nemo.MenuItem(name="OneDriveOnDemand::Unpin",
                                 label="Stop always keeping on this device")
            keep.connect("activate", self.run_in_background, "unpin", paths, files)
        else:
            keep = Nemo.MenuItem(name="OneDriveOnDemand::Pin", label="Always keep on this device",
                                 icon="emblem-onedrive-pinned")
            keep.connect("activate", self.run_in_background, "pin", paths, files)
        submenu.append_item(keep)

        free = Nemo.MenuItem(name="OneDriveOnDemand::Free", label="Free up space",
                             icon="emblem-onedrive-online")
        free.connect("activate", self.run_in_background, "free", paths, files)
        submenu.append_item(free)

        if len(paths) == 1:
            web = Nemo.MenuItem(name="OneDriveOnDemand::Web", label="View online")
            web.connect("activate", self.run_in_background, "weburl", paths, files)
            submenu.append_item(web)
        return [top]

    def get_background_items(self, window, folder):
        return []

    def run_in_background(self, _item, cmd, paths, files):
        """Pinning can take a while (it measures the folder first), so don't block Nemo."""
        threading.Thread(target=self.do_action, args=(cmd, paths, files), daemon=True).start()

    def do_action(self, cmd, paths, files):
        for path in paths:
            try:
                answer = ipc.request(cmd, timeout=600, path=path)
            except ipc.DaemonNotRunning as error:
                notify("OneDrive", str(error))
                return
            if not answer.get("ok"):
                notify("OneDrive", answer.get("error", "Something went wrong."))
            elif cmd == "weburl":
                Gio.AppInfo.launch_default_for_uri(answer["url"], None)
        # Redraw the emblems of what we just changed (on Nemo's main thread).
        GLib.idle_add(lambda: [f.invalidate_extension_info() for f in files] and False)

    def get_name_and_desc(self):
        return ["OneDrive On-Demand:::Status emblems and Always keep / Free up space for ~/OneDrive"]


def notify(title, message):
    try:
        subprocess.Popen(["notify-send", "--icon=emblem-onedrive-error", title, message])
    except OSError:
        print(f"{title}: {message}", file=sys.stderr)
