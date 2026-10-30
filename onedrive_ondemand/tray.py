"""The tray icon (Cinnamon's XApp.StatusIcon): status, pause, activity, settings.

Like the Nemo extension, it only talks to the daemon. Closing it ("Hide this
icon") changes nothing about syncing; the services keep running.
"""

import fcntl
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gio, GLib, Gtk, XApp  # noqa: E402

from onedrive_ondemand import config, ipc, status  # noqa: E402

UPDATE_SECONDS = 2
ACTIVITY_ITEMS = 10
# Activity kinds worth a desktop notification (not every upload!).
NOTIFY_KINDS = {"error", "conflict"}


def notify(title, message, icon="onedrive-ondemand-error"):
    try:
        subprocess.Popen(["notify-send", "--app-name=OneDrive", f"--icon={icon}", title, message])
    except OSError:
        pass


def icon_path(name):
    """Full path of one of our icons.

    Cinnamon's panel only notices icons added to the theme after it restarts,
    so we hand it the file itself (XApp accepts a path as the icon name).
    """
    for base in (config.ICON_DIR, Path("/usr/share/icons/hicolor")):
        path = base / "scalable" / "status" / f"{name}.svg"
        if path.exists():
            return str(path)
    return name


def local_time(text):
    return datetime.fromisoformat(text).astimezone().strftime("%H:%M")


class Tray:
    def __init__(self):
        self.settings = config.load_settings()
        self.mount = config.mount_path(self.settings)
        self.info = None
        self.last_activity_id = None   # newest activity we've seen
        self.signing_in = False

        self.icon = XApp.StatusIcon()
        self.icon.set_name("onedrive-ondemand")
        self.icon.set_icon_name(icon_path("onedrive-ondemand-ok"))
        self.icon.set_tooltip_text("OneDrive")
        self.menu = self.build_menu()
        self.icon.set_primary_menu(self.menu)
        self.icon.set_secondary_menu(self.menu)

        self.update()
        GLib.timeout_add_seconds(UPDATE_SECONDS, self.update)

    # --- menu -------------------------------------------------------------------

    def item(self, label, callback=None, menu=None):
        item = Gtk.MenuItem(label=label)
        if callback:
            item.connect("activate", lambda _item: callback())
        else:
            item.set_sensitive(False)
        (menu or self.menu).append(item)
        return item

    def build_menu(self):
        self.menu = Gtk.Menu()
        self.status_item = self.item("OneDrive")
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item("Open OneDrive folder", self.open_folder)
        self.item("View online", self.view_online)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.pause_item = self.item("Pause syncing", self.toggle_pause)
        self.activity_item = Gtk.MenuItem(label="Recent activity")
        self.activity_menu = Gtk.Menu()
        self.activity_item.set_submenu(self.activity_menu)
        self.menu.append(self.activity_item)
        self.item("Settings…", self.show_settings)
        self.signin_item = self.item("Sign in again…", self.sign_in_again)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item("Hide this icon", Gtk.main_quit)
        self.menu.show_all()
        self.signin_item.hide()
        return self.menu

    # --- regular update ---------------------------------------------------------

    def update(self):
        try:
            self.info = ipc.request("info", timeout=1)
            if not self.info.get("ok"):
                self.info = None
        except (ipc.DaemonNotRunning, OSError, ValueError):
            self.info = None

        key, text = status.summarize(self.info)
        self.icon.set_icon_name(icon_path(f"onedrive-ondemand-{key}"))
        self.icon.set_tooltip_text(f"OneDrive: {text}")
        self.status_item.set_label(text)
        paused = bool(self.info and self.info.get("paused"))
        self.pause_item.set_label("Resume syncing" if paused else "Pause syncing")
        self.pause_item.set_sensitive(self.info is not None)
        self.signin_item.set_visible(bool(self.info and self.info.get("signin_needed")))
        if self.info is not None:
            self.update_activity()
        return True  # keep the timer running

    def update_activity(self):
        try:
            entries = ipc.request("activity", timeout=1, limit=ACTIVITY_ITEMS).get("activity", [])
        except (ipc.DaemonNotRunning, OSError, ValueError):
            return
        newest = entries[0]["id"] if entries else 0
        if self.last_activity_id is not None:
            for entry in reversed(entries):
                if entry["id"] > self.last_activity_id and entry["kind"] in NOTIFY_KINDS:
                    notify("OneDrive", entry["message"])
        if newest == self.last_activity_id:
            return
        self.last_activity_id = newest
        for child in self.activity_menu.get_children():
            self.activity_menu.remove(child)
        if not entries:
            self.item("Nothing yet", menu=self.activity_menu)
        for entry in entries:
            self.item(f"{local_time(entry['time'])}  {entry['message']}", menu=self.activity_menu)
        self.activity_menu.show_all()

    # --- actions ------------------------------------------------------------------

    def open_folder(self):
        Gio.AppInfo.launch_default_for_uri(self.mount.as_uri(), None)

    def view_online(self):
        url = "https://onedrive.live.com/"
        try:
            answer = ipc.request("weburl", timeout=10, path=str(self.mount))
            url = answer.get("url", url)
        except (ipc.DaemonNotRunning, OSError, ValueError):
            pass
        Gio.AppInfo.launch_default_for_uri(url, None)

    def toggle_pause(self):
        cmd = "resume" if self.info and self.info.get("paused") else "pause"
        try:
            ipc.request(cmd, timeout=10)
        except ipc.DaemonNotRunning as error:
            notify("OneDrive", str(error))
        self.update()

    def sign_in_again(self):
        if self.signing_in:
            return
        self.signing_in = True
        notify("OneDrive", "A Microsoft sign-in page is opening in your browser.",
               icon="onedrive-ondemand-ok")

        def work():
            from onedrive_ondemand import setup_wizard
            try:
                setup_wizard.sign_in_again()
                GLib.idle_add(notify, "OneDrive", "Signed in again.", "onedrive-ondemand-ok")
            except Exception as error:  # show any failure to the user
                GLib.idle_add(notify, "OneDrive: sign-in failed", str(error))
            finally:
                self.signing_in = False
        threading.Thread(target=work, daemon=True).start()

    def show_settings(self):
        try:
            current = ipc.request("settings", timeout=5)["settings"]
        except (ipc.DaemonNotRunning, OSError, ValueError, KeyError):
            notify("OneDrive", "The daemon isn't running, so settings can't be changed now.")
            return
        settings = config.load_settings()

        dialog = Gtk.Dialog(title="OneDrive settings", modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin=16)
        dialog.get_content_area().add(grid)

        days = Gtk.SpinButton.new_with_range(0, 3650, 1)
        days.set_value(current["evict_after_days"])
        grid.attach(Gtk.Label(label="Free files not opened for (days, 0 = never):", xalign=0), 0, 0, 1, 1)
        grid.attach(days, 1, 0, 1, 1)

        percent = Gtk.SpinButton.new_with_range(0, 50, 1)
        percent.set_value(current["min_free_space_percent"])
        grid.attach(Gtk.Label(label="Keep at least this much of the disk free (%):", xalign=0), 0, 1, 1, 1)
        grid.attach(percent, 1, 1, 1, 1)

        previews = Gtk.CheckButton(label="Show picture previews in the OneDrive folder "
                                         "(downloads each picture)")
        previews.set_active(bool(settings.get("thumbnails")))
        grid.attach(previews, 0, 2, 2, 1)

        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60,
                         label="Files you chose to always keep on this device are never freed.")
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, 3, 2, 1)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                ipc.request("settings", timeout=10, set={
                    "evict_after_days": int(days.get_value()),
                    "min_free_space_percent": percent.get_value()})
            except (ipc.DaemonNotRunning, OSError, ValueError) as error:
                notify("OneDrive", f"Couldn't save: {error}")
            if previews.get_active() != bool(settings.get("thumbnails")):
                from onedrive_ondemand.setup_wizard import apply_thumbnail_choice
                settings = config.load_settings()
                settings["thumbnails"] = previews.get_active()
                config.save_settings(settings)
                apply_thumbnail_choice(previews.get_active(), self.mount)
        dialog.destroy()


def main():
    # Only one tray icon at a time: hold a lock while running.
    config.RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(config.RUNTIME_DIR / "tray.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("The OneDrive tray icon is already running.")
        sys.exit(0)
    Tray()
    Gtk.main()
