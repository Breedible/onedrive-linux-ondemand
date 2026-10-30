"""Nemo extension for OneDrive On-Demand: status emblems and a right-click menu.

Nemo loads this file through nemo-python. It never touches files itself: it
asks the daemon of the account a file belongs to (over its socket) for each
file's state, and sends pin/free requests to it. Every request has a short
timeout, so Nemo can't freeze even if a daemon hangs.

It also tells the daemons which OneDrive folders Nemo windows are showing,
so picture previews can be deleted once you've left a folder (DESIGN.md §8).

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
STATUS_TIMEOUT = 1.0      # seconds Nemo will wait for a daemon, at most
BATCH_DELAY_MS = 30       # collect requests this long, then ask for all at once
REFRESH_SECONDS = 3       # how often shown emblems are re-checked
MAX_TRACKED = 3000        # how many shown files we keep re-checking
ACCOUNTS_SECONDS = 10     # how often we look for added or removed accounts
VIEWING_SECONDS = 30      # how often we tell the daemons which folders are shown


class OneDriveExtension(GObject.GObject, Nemo.InfoProvider, Nemo.MenuProvider,
                        Nemo.LocationWidgetProvider, Nemo.NameAndDescProvider):
    def __init__(self):
        super().__init__()
        self.mounts = {}         # mount folder -> its account's daemon socket
        self.load_accounts()
        self.waiting = []        # (file, handle, closure, path) waiting for a batch
        self.batch_timer = None
        self.shown = {}          # path -> (NemoFileInfo, state) for refreshing
        self.dirs = set()        # shown paths that are folders
        self.windows = {}        # id(window) -> folder that window shows
        self.last_viewing = (None, 0.0)  # what we last told the daemons, and when
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)
        GLib.timeout_add_seconds(ACCOUNTS_SECONDS, self.load_accounts)

    def load_accounts(self):
        """Find every account's folder (accounts can be added while Nemo runs)."""
        try:
            self.mounts = {str(a.mount_path()): a.daemon_socket for a in config.accounts()}
        except (OSError, ValueError):
            pass  # keep what we had
        return True  # keep the timer running

    # --- which files are ours ----------------------------------------------

    def mount_of(self, path):
        """The OneDrive folder `path` is in, or None."""
        for mount in self.mounts:
            if path == mount or path.startswith(mount + os.sep):
                return mount
        return None

    def path_of(self, file):
        """The local path of a Nemo file if it's inside a OneDrive folder, else None."""
        location = file.get_location()
        path = location.get_path() if location else None
        if path and self.mount_of(path) and path not in self.mounts:
            return path
        return None

    def request(self, path, cmd, timeout, **args):
        """Send a request to the daemon of the account `path` belongs to."""
        return ipc.request(self.mounts[self.mount_of(path)], cmd, timeout=timeout, **args)

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
        """States of many paths: one request per account."""
        by_mount = {}
        for path in paths:
            mount = self.mount_of(path)
            if mount:
                by_mount.setdefault(mount, []).append(path)
        states = {}
        for mount, some in by_mount.items():
            try:
                answer = ipc.request(self.mounts[mount], "status", timeout=STATUS_TIMEOUT,
                                     paths=some, dirs=[p for p in some if p in self.dirs])
            except (ipc.DaemonNotRunning, OSError, ValueError):
                continue  # no daemon: just show no emblems for that account
            if answer.get("ok"):
                states.update(answer.get("states", {}))
        return states

    def remember(self, path, file, state):
        self.shown.pop(path, None)
        self.shown[path] = (file, state)
        while len(self.shown) > MAX_TRACKED:
            oldest = next(iter(self.shown))  # forget the oldest
            self.shown.pop(oldest)
            self.dirs.discard(oldest)

    def refresh(self):
        """Every few seconds, redraw emblems whose state changed."""
        self.tell_viewing()
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

    # --- which folders are on screen (for picture previews) ---------------------

    def get_widget(self, uri, window):
        """Nemo calls this whenever a window (or tab) opens a folder.

        We only use it to know what's on screen; we don't add anything to the window.
        """
        key = id(window)
        if key not in self.windows:
            window.connect("destroy", lambda _w: self.windows.pop(key, None))
        self.windows[key] = Gio.File.new_for_uri(uri).get_path()
        self.tell_viewing(force=True)
        return None

    def tell_viewing(self, force=False):
        """Tell each daemon which of its folders are shown (when it changes, or every 30 s)."""
        shown = {folder for folder in self.windows.values() if folder and self.mount_of(folder)}
        last, when = self.last_viewing
        now = GLib.get_monotonic_time() / 1e6
        if not force and shown == last and now - when < VIEWING_SECONDS:
            return
        self.last_viewing = (shown, now)
        for mount, sock in self.mounts.items():
            folders = [f for f in shown if f == mount or f.startswith(mount + os.sep)]
            if folders:
                try:
                    ipc.request(sock, "viewing", timeout=STATUS_TIMEOUT, folders=folders)
                except (ipc.DaemonNotRunning, OSError, ValueError):
                    pass

    # --- right-click menu -------------------------------------------------------

    def get_file_items(self, window, files):
        paths = [self.path_of(f) for f in files]
        if not paths or None in paths:
            return []  # only when everything selected is inside a OneDrive folder
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
                answer = self.request(path, cmd, timeout=600, path=path)
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
        return ["OneDrive On-Demand:::Status emblems and Always keep / Free up space for your OneDrive folders"]


def notify(title, message):
    try:
        subprocess.Popen(["notify-send", "--icon=emblem-onedrive-error", title, message])
    except OSError:
        print(f"{title}: {message}", file=sys.stderr)
