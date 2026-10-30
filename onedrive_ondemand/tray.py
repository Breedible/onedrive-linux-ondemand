"""The tray icon (Cinnamon's XApp.StatusIcon): status, progress, pause, accounts, updates.

- Left-click: a small window with what's downloading and uploading right
  now (with progress bars) and recent activity, like the OneDrive flyout
  on Windows.
- Right-click: the menu (open folders, pause, settings, accounts, updates).
- While anything is transferring, the icon's arrows spin.

Like the Nemo extension, it only talks to the daemons (one per account).
Closing it ("Hide this icon") changes nothing about syncing; the services
keep running.
"""

import fcntl
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, XApp  # noqa: E402

from onedrive_ondemand import config, ipc, status  # noqa: E402

UPDATE_SECONDS = 2
SPIN_MS = 200               # time between frames of the spinning "syncing" icon
SPIN_FRAMES = 4
ACTIVITY_ITEMS = 10
UPDATE_CHECK_FIRST_SECONDS = 120          # first check for updates after start-up
UPDATE_CHECK_EVERY_SECONDS = 24 * 3600    # then once a day
# Activity kinds worth a desktop notification (not every upload!).
NOTIFY_KINDS = {"error", "conflict", "offline"}


def notify(title, message, icon="onedrive-ondemand-error"):
    try:
        subprocess.Popen(["notify-send", "--app-name=OneDrive", f"--icon={icon_path(icon)}",
                          title, message])
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


def human(n):
    from onedrive_ondemand.daemon import human as daemon_human
    return daemon_human(n)


def ask_all(accounts, cmd, timeout=10, **args):
    """Send the same request to every account's daemon. Returns the error texts."""
    problems = []
    for account in accounts:
        try:
            answer = ipc.request(account.daemon_socket, cmd, timeout=timeout, **args)
            if not answer.get("ok"):
                problems.append(f"{account.display_name()}: {answer.get('error')}")
        except (ipc.DaemonNotRunning, OSError, ValueError) as error:
            problems.append(f"{account.display_name()}: {error}")
    return problems


class Tray:
    def __init__(self):
        self.accounts = []
        self.infos = {}               # account id -> daemon's info answer (None = not running)
        self.activity = {}            # account id -> newest activity entries
        self.last_activity_id = {}    # account id -> newest activity id we've seen
        self.signing_in = set()
        self.key = status.OK          # the icon state shown now
        self.spin_frame = 0
        self.spin_timer = None
        self.update = None            # an update found by the daily check
        self.told_about_update = None
        self.window = None            # the progress window, while it's open
        self.shown_activity = None    # what the "Recent activity" submenu shows now

        self.icon = XApp.StatusIcon()
        self.icon.set_name("onedrive-ondemand")
        self.icon.set_icon_name(icon_path("onedrive-ondemand-ok"))
        self.icon.set_tooltip_text("OneDrive")
        # Only the right button opens the menu; the left one sends "activate".
        self.icon.connect("activate", lambda *_args: self.on_left_click())

        self.refresh()
        if not self.accounts:
            notify("OneDrive", "OneDrive isn't set up yet. Click the OneDrive icon in the "
                               "panel to set it up.", "onedrive-ondemand-ok")
        GLib.timeout_add_seconds(UPDATE_SECONDS, self.refresh)
        GLib.timeout_add_seconds(UPDATE_CHECK_FIRST_SECONDS, self.check_for_updates_quietly)

    # --- regular update ---------------------------------------------------------

    def refresh(self):
        accounts = config.accounts()
        if [a.id for a in accounts] != [a.id for a in self.accounts] or not hasattr(self, "menu"):
            self.accounts = accounts
            self.build_menu()
            self.shown_activity = None  # the new menu's activity list is still empty

        results = []
        for account in self.accounts:
            try:
                info = ipc.request(account.daemon_socket, "info", timeout=1)
                info = info if info.get("ok") else None
            except (ipc.DaemonNotRunning, OSError, ValueError):
                info = None
            self.infos[account.id] = info
            results.append(status.summarize(info))
            if info is not None:
                self.update_activity(account)

        if not self.accounts:
            key, text = status.ERROR, "OneDrive isn't set up yet."
        else:
            key = status.combine(results)
            if len(results) == 1:
                text = results[0][1]
            else:
                text = "; ".join(f"{a.settings()['name']}: {r[1]}" for a, r in zip(self.accounts, results))
        self.set_icon(key)
        self.icon.set_tooltip_text(f"OneDrive: {text}")
        self.update_menu(results)
        if self.window is not None:
            self.fill_window()
        return True  # keep the timer running

    def set_icon(self, key):
        """Show the state's icon; while syncing, spin the arrows."""
        if key != self.key or self.spin_timer is None and key == status.SYNCING:
            self.key = key
            if key == status.SYNCING and self.spin_timer is None:
                self.spin_timer = GLib.timeout_add(SPIN_MS, self.spin)
            if key != status.SYNCING:
                self.icon.set_icon_name(icon_path(f"onedrive-ondemand-{key}"))

    def spin(self):
        if self.key != status.SYNCING:
            self.spin_timer = None
            self.icon.set_icon_name(icon_path(f"onedrive-ondemand-{self.key}"))
            return False  # stop the animation
        self.spin_frame = (self.spin_frame + 1) % SPIN_FRAMES
        self.icon.set_icon_name(icon_path(f"onedrive-ondemand-syncing-{self.spin_frame + 1}"))
        return True

    def update_activity(self, account):
        try:
            entries = ipc.request(account.daemon_socket, "activity", timeout=1,
                                  limit=ACTIVITY_ITEMS).get("activity", [])
        except (ipc.DaemonNotRunning, OSError, ValueError):
            return
        last = self.last_activity_id.get(account.id)
        if last is not None:
            for entry in reversed(entries):
                if entry["id"] > last and entry["kind"] in NOTIFY_KINDS:
                    title = account.display_name() if len(self.accounts) > 1 else "OneDrive"
                    notify(title, entry["message"])
        self.last_activity_id[account.id] = entries[0]["id"] if entries else 0
        self.activity[account.id] = entries

    # --- menu (right-click) ---------------------------------------------------------

    def item(self, label, callback=None, menu=None):
        item = Gtk.MenuItem(label=label)
        if callback:
            item.connect("activate", lambda _item: callback())
        else:
            item.set_sensitive(False)
        (menu or self.menu).append(item)
        return item

    def build_menu(self):
        """Make the menu. Rebuilt whenever an account is added or removed."""
        self.menu = Gtk.Menu()
        self.status_items = []
        self.signin_items = {}
        if not self.accounts:
            self.item("OneDrive isn't set up yet")
            self.menu.append(Gtk.SeparatorMenuItem())
            self.item("Set up OneDrive…", self.set_up)
        else:
            for _account in self.accounts:
                self.status_items.append(self.item("OneDrive"))
            self.menu.append(Gtk.SeparatorMenuItem())
            self.item("Show progress and activity…", self.toggle_window)
            for account in self.accounts:
                label = "Open folder" if len(self.accounts) == 1 else f"Open {account.display_name()}"
                self.item(label, lambda a=account: self.open_folder(a))
            for account in self.accounts:
                label = "View online" if len(self.accounts) == 1 else f"View {account.display_name()} online"
                self.item(label, lambda a=account: self.view_online(a))
            self.menu.append(Gtk.SeparatorMenuItem())
            self.pause_item = self.item("Pause syncing", self.toggle_pause)
            self.activity_item = Gtk.MenuItem(label="Recent activity")
            self.activity_menu = Gtk.Menu()
            self.activity_item.set_submenu(self.activity_menu)
            self.menu.append(self.activity_item)
            self.item("Settings and accounts…", self.show_settings)
            for account in self.accounts:
                self.signin_items[account.id] = self.item(
                    f"Sign in again to {account.display_name()}…", lambda a=account: self.sign_in_again(a))
        self.update_item = self.item("Check for updates…", self.check_for_updates)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item("Hide this icon", Gtk.main_quit)
        self.menu.show_all()
        for item in self.signin_items.values():
            item.hide()
        self.icon.set_secondary_menu(self.menu)

    def update_menu(self, results):
        if not self.accounts:
            return
        for account, item, (_key, text) in zip(self.accounts, self.status_items, results):
            item.set_label(text if len(self.accounts) == 1 else f"{account.display_name()}: {text}")
        infos = [i for i in self.infos.values() if i is not None]
        paused = bool(infos) and all(i.get("paused") for i in infos)
        self.pause_item.set_label("Resume syncing" if paused else "Pause syncing")
        self.pause_item.set_sensitive(bool(infos))
        for account in self.accounts:
            info = self.infos.get(account.id)
            self.signin_items[account.id].set_visible(bool(info and info.get("signin_needed")))
        if self.update:
            self.update_item.set_label("Install update…")

        entries = self.all_activity()
        if entries == self.shown_activity:
            return  # unchanged: don't redraw a menu you might have open
        self.shown_activity = entries
        for child in self.activity_menu.get_children():
            self.activity_menu.remove(child)
        if not entries:
            self.item("Nothing yet", menu=self.activity_menu)
        for when, name, message in entries[:ACTIVITY_ITEMS]:
            prefix = f"[{name}] " if len(self.accounts) > 1 else ""
            self.item(f"{local_time(when)}  {prefix}{message}", menu=self.activity_menu)
        self.activity_menu.show_all()

    def all_activity(self):
        """Newest first: (time, account name, message) from every account."""
        entries = []
        for account in self.accounts:
            name = account.settings()["name"]
            entries += [(e["time"], name, e["message"]) for e in self.activity.get(account.id, [])]
        return sorted(entries, reverse=True)

    # --- progress window (left-click) -----------------------------------------------

    def on_left_click(self):
        if not self.accounts:
            self.set_up()
        else:
            self.toggle_window()

    def toggle_window(self):
        if self.window is not None:
            self.window.destroy()
            return
        self.window = Gtk.Window(title="OneDrive")
        self.window.set_default_size(420, -1)
        self.window.set_position(Gtk.WindowPosition.MOUSE)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_icon_name("onedrive-ondemand-ok")
        self.window.connect("destroy", self.window_closed)
        self.window.connect("key-press-event", self.window_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.window.add(outer)
        self.window_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.pack_start(self.window_body, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        for label, callback in (("Open folder", lambda: [self.open_folder(a) for a in self.accounts]),
                                ("Pause / resume", self.toggle_pause),
                                ("Settings", self.show_settings)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _b, c=callback: c())
            buttons.pack_start(button, True, True, 0)
        outer.pack_end(buttons, False, False, 0)
        self.fill_window()
        self.window.show_all()

    def window_closed(self, _window):
        self.window = None

    def window_key(self, _window, event):
        if event.keyval == Gdk.KEY_Escape:
            self.window.destroy()

    def fill_window(self):
        """Redraw the window's contents from the newest info (every 2 seconds)."""
        for child in self.window_body.get_children():
            self.window_body.remove(child)

        def label(text, css=None):
            widget = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=55)
            if css:
                widget.get_style_context().add_class(css)
            self.window_body.pack_start(widget, False, False, 0)

        for account in self.accounts:
            info = self.infos.get(account.id)
            heading = Gtk.Label(xalign=0)
            heading.set_markup(f"<b>{GLib.markup_escape_text(account.display_name())}</b>: "
                               f"{GLib.markup_escape_text(status.summarize(info)[1])}")
            self.window_body.pack_start(heading, False, False, 0)
            if info is None:
                continue
            for t in info.get("transfers", []):
                self.window_body.pack_start(self.transfer_row(t), False, False, 0)
            waiting = info.get("pinning", 0) - sum(1 for t in info.get("transfers", [])
                                                   if t["direction"] == "download")
            if waiting > 0:
                label(f"{waiting} more file(s) waiting to download (Always keep on this device)",
                      "dim-label")
            if info.get("queued") and not info.get("uploading"):
                label(f"{info['queued']} change(s) waiting to upload", "dim-label")

        self.window_body.pack_start(Gtk.Separator(), False, False, 4)
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Recent activity</b>")
        self.window_body.pack_start(heading, False, False, 0)
        entries = self.all_activity()[:8]
        if not entries:
            label("Nothing yet", "dim-label")
        for when, name, message in entries:
            prefix = f"[{name}] " if len(self.accounts) > 1 else ""
            label(f"{local_time(when)}  {prefix}{message}")
        self.window_body.show_all()

    def transfer_row(self, t):
        """One file being transferred: name, a progress bar and "1.2 MB of 3.4 MB · 500 KB/s"."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        arrow = "↑ Uploading" if t["direction"] == "upload" else "↓ Downloading"
        name = Gtk.Label(label=f"{arrow} {t['name']}", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        row.pack_start(name, False, False, 0)
        bar = Gtk.ProgressBar()
        bar.set_fraction(min(1.0, (t.get("percentage") or 0) / 100))
        row.pack_start(bar, False, False, 0)
        details = f"{human(t['bytes'])} of {human(t['size'])}"
        if t.get("speed"):
            details += f" · {human(t['speed'])}/s"
        if t.get("eta"):
            minutes, seconds = divmod(int(t["eta"]), 60)
            details += f" · {minutes} min {seconds} s left" if minutes else f" · {seconds} s left"
        small = Gtk.Label(label=details, xalign=0)
        small.get_style_context().add_class("dim-label")
        row.pack_start(small, False, False, 0)
        return row

    # --- actions ------------------------------------------------------------------

    def set_up(self):
        from onedrive_ondemand import setup_wizard
        setup_wizard.run_in_terminal("setup")

    def open_folder(self, account):
        Gio.AppInfo.launch_default_for_uri(account.mount_path().as_uri(), None)

    def view_online(self, account):
        url = "https://onedrive.live.com/"
        try:
            answer = ipc.request(account.daemon_socket, "weburl", timeout=10,
                                 path=str(account.mount_path()))
            url = answer.get("url", url)
        except (ipc.DaemonNotRunning, OSError, ValueError):
            pass
        Gio.AppInfo.launch_default_for_uri(url, None)

    def toggle_pause(self):
        infos = [i for i in self.infos.values() if i is not None]
        cmd = "resume" if infos and all(i.get("paused") for i in infos) else "pause"
        for problem in ask_all(self.accounts, cmd):
            notify("OneDrive", problem)
        self.refresh()

    def sign_in_again(self, account):
        if account.id in self.signing_in:
            return
        self.signing_in.add(account.id)
        notify("OneDrive", f"A Microsoft sign-in page for {account.display_name()} is opening "
                           "in your browser.", "onedrive-ondemand-ok")

        def work():
            from onedrive_ondemand import setup_wizard
            try:
                setup_wizard.sign_in_again(account)
                GLib.idle_add(notify, "OneDrive", "Signed in again.", "onedrive-ondemand-ok")
            except Exception as error:  # show any failure to the user
                GLib.idle_add(notify, "OneDrive: sign-in failed", str(error))
            finally:
                self.signing_in.discard(account.id)
        threading.Thread(target=work, daemon=True).start()

    # --- updates ------------------------------------------------------------------

    def check_for_updates_quietly(self):
        """The daily check: only says something if there is an update."""
        def work():
            from onedrive_ondemand import updates
            try:
                found = updates.check()
            except updates.UpdateError:
                return
            GLib.idle_add(self.quiet_check_done, found)
        threading.Thread(target=work, daemon=True).start()
        GLib.timeout_add_seconds(UPDATE_CHECK_EVERY_SECONDS, self.check_for_updates_quietly)
        return False  # this timer is done; the line above set up the next one

    def quiet_check_done(self, found):
        if found["available"]:
            self.update = found
            if self.told_about_update != found["summary"]:
                self.told_about_update = found["summary"]
                notify("OneDrive: update available", found["summary"].splitlines()[0] +
                       " Right-click the OneDrive icon and choose Install update.",
                       "onedrive-ondemand-ok")

    def check_for_updates(self):
        def work():
            from onedrive_ondemand import updates
            try:
                found = updates.check()
            except updates.UpdateError as error:
                GLib.idle_add(self.message, "Couldn't check for updates", str(error))
                return
            GLib.idle_add(self.offer_update, found)
        threading.Thread(target=work, daemon=True).start()

    def message(self, title, text):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK,
                                   text=title)
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def offer_update(self, found):
        if not found["available"]:
            self.update = None
            self.update_item.set_label("Check for updates…")
            self.message("OneDrive is up to date", found["summary"])
            return
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.YES_NO, text="Install the update now?")
        dialog.format_secondary_text(
            found["summary"] + "\n\nOneDrive restarts while updating, so close files you have "
            "open from OneDrive first. Changes waiting to upload are kept."
            + ("\nYou'll be asked for your password." if found["kind"] == "deb" else ""))
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return

        def work():
            from onedrive_ondemand import updates
            try:
                updates.install(found)
            except updates.UpdateError as error:
                GLib.idle_add(self.message, "The update didn't work", str(error))
                return
            GLib.idle_add(self.restart_self)
        threading.Thread(target=work, daemon=True).start()

    def restart_self(self):
        notify("OneDrive", "Updated.", "onedrive-ondemand-ok")
        # Start the new version of the tray in place of this one.
        os.execv(sys.executable, [sys.executable, "-m", "onedrive_ondemand", "tray"])

    # --- settings and accounts -------------------------------------------------------

    def show_settings(self):
        from onedrive_ondemand import setup_wizard

        dialog = Gtk.Dialog(title="OneDrive settings", modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin=16)
        dialog.get_content_area().add(grid)
        row = 0

        def heading(text):
            nonlocal row
            widget = Gtk.Label(xalign=0)
            widget.set_markup(f"<b>{text}</b>")
            grid.attach(widget, 0, row, 3, 1)
            row += 1

        heading("Accounts")
        for account in self.accounts:
            grid.attach(Gtk.Label(label=account.display_name(), xalign=0), 0, row, 1, 1)
            folder = Gtk.Label(label=str(account.mount_path()), xalign=0)
            folder.get_style_context().add_class("dim-label")
            grid.attach(folder, 1, row, 1, 1)
            sign_out = Gtk.Button(label="Sign out…")
            sign_out.connect("clicked", lambda _b, a=account: (
                dialog.response(Gtk.ResponseType.CANCEL),
                setup_wizard.run_in_terminal("remove-account", a.settings()["name"])))
            grid.attach(sign_out, 2, row, 1, 1)
            row += 1
        add = Gtk.Button(label="Add an account…")
        add.connect("clicked", lambda _b: (dialog.response(Gtk.ResponseType.CANCEL),
                                           setup_wizard.run_in_terminal("add-account")))
        grid.attach(add, 0, row, 1, 1)
        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60,
                         label="Each account gets its own folder, like on Windows. Signing out "
                               "doesn't change your files on OneDrive.")
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, row + 1, 3, 1)
        row += 2

        settings = self.accounts[0].settings() if self.accounts else dict(config.DEFAULT_SETTINGS)
        heading("Space on this computer")
        days = Gtk.SpinButton.new_with_range(0, 3650, 1)
        days.set_value(settings["evict_after_days"])
        grid.attach(Gtk.Label(label="Free files not opened for (days, 0 = never):", xalign=0), 0, row, 2, 1)
        grid.attach(days, 2, row, 1, 1)
        row += 1
        percent = Gtk.SpinButton.new_with_range(0, 50, 1)
        percent.set_value(settings["min_free_space_percent"])
        grid.attach(Gtk.Label(label="Keep at least this much of the disk free (%):", xalign=0), 0, row, 2, 1)
        grid.attach(percent, 2, row, 1, 1)
        row += 1
        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60,
                         label="Files you chose to always keep on this device are never freed.")
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, row, 3, 1)
        row += 1

        heading("Previews")
        previews = Gtk.CheckButton(label="Show small previews of photos, videos and PDFs")
        previews.set_active(bool(settings.get("thumbnails")))
        grid.attach(previews, 0, row, 3, 1)
        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60,
                         label="Previews are OneDrive's own small pictures, so your files aren't "
                               "downloaded for them. They're deleted again a couple of minutes "
                               "after you leave a folder.")
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, row + 1, 3, 1)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK and self.accounts:
            show = previews.get_active()
            for problem in ask_all(self.accounts, "settings", set={
                    "evict_after_days": int(days.get_value()),
                    "min_free_space_percent": percent.get_value(),
                    "thumbnails": show}):
                notify("OneDrive", f"Couldn't save: {problem}")
            if show != bool(settings.get("thumbnails")):
                setup_wizard.apply_thumbnail_choice(show, self.accounts)
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
