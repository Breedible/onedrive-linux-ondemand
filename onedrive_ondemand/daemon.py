"""The background daemon: pins, freeing space, file states, activity log.

It runs as its own systemd user service, separate from the rclone mount, so a
crash here never takes your files away (DESIGN.md §2). It talks to:
- rclone, over the rc socket (upload queue, item IDs),
- rclone's cache folder on disk (what's downloaded),
- Microsoft Graph (finding pinned items after a rename),
- the CLI, Nemo and the tray, over its own socket (ipc.py).

Threads:
- "uploads":  every 2 s, reads rclone's upload queue (activity log, pause).
- "pins-N":   two workers that download pinned files by reading them.
- "upkeep":   every 5 min: follow renames, re-scan pinned folders, free space.
"""

import os
import queue
import shutil
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from onedrive_ondemand import cache as cachemod
from onedrive_ondemand import config, graph, ipc, pins, rclone

QUEUE_POLL_SECONDS = 2
UPKEEP_SECONDS = 5 * 60
PIN_WORKERS = 2               # files downloaded at the same time for pins
ERROR_TRIES = 3               # an upload that failed this often counts as an error
HOLD_SECONDS = 365 * 24 * 3600  # "don't upload yet" while paused
READ_CHUNK = 1024 * 1024


# --- Decisions, kept separate so the tests can check them -------------------

@dataclass
class Candidate:
    """A downloaded file that could be freed."""
    rel: str
    atime: datetime      # last time it was opened
    atime_text: str      # the same, exactly as in the sidecar (checked again before freeing)
    disk: int            # bytes it really uses on disk


def choose_evictions(candidates, now, evict_after_days, free_bytes, total_bytes,
                     min_free_percent, timer_allowed=True):
    """Which files to free, in order (DESIGN.md §5).

    1. Timer rule: anything not opened for `evict_after_days` (0 = never).
       Skipped when `timer_allowed` is False (some pins couldn't be checked).
    2. Space rule: if less than `min_free_percent` of the disk is free, free
       the least recently opened files until it isn't.
    """
    chosen = []
    remaining = sorted(candidates, key=lambda c: c.atime)  # oldest first
    if timer_allowed and evict_after_days > 0:
        cutoff = now - timedelta(days=evict_after_days)
        old = [c for c in remaining if c.atime < cutoff]
        chosen.extend(old)
        free_bytes += sum(c.disk for c in old)
        remaining = [c for c in remaining if c.atime >= cutoff]
    needed = total_bytes * min_free_percent / 100
    for c in remaining:
        if free_bytes >= needed:
            break
        chosen.append(c)
        free_bytes += c.disk
    return chosen


def file_state(rel, is_dir, rule, sidecar, queue_item, pin_pending, pin_error):
    """The state shown for one item (DESIGN.md §3).

    Files:   "online", "local", "pinned", "syncing" or "error".
    Folders: "pinned", "syncing" (pinned, still downloading) or "none".
    """
    pinned = rule is not None and rule["state"] == pins.PINNED
    if is_dir:
        if pinned:
            return "syncing" if pin_pending else "pinned"
        return "none"
    if queue_item is not None:
        return "error" if queue_item.get("tries", 0) >= ERROR_TRIES else "syncing"
    if sidecar is not None and sidecar.get("Dirty"):
        return "syncing"
    full = sidecar is not None and cachemod.is_fully_cached(sidecar)
    if pinned:
        if pin_error:
            return "error"
        return "pinned" if full and not pin_pending else "syncing"
    return "local" if full else "online"


class UserError(Exception):
    """Something the user asked for can't be done; the message explains why."""


# --- The daemon ---------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.settings = config.load_settings()
        self.mount = config.mount_path(self.settings)
        self.fs = rclone.remote_fs(self.settings)
        self.cache = cachemod.Cache(config.CACHE_DIR, config.REMOTE_NAME,
                                    self.settings["remote_folder"], self.mount)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = pins.Store(config.DATA_DIR / "state.db")

        self.lock = threading.Lock()
        self.upkeep_lock = threading.Lock()   # only one upkeep pass at a time
        self.stopping = threading.Event()
        self.upkeep_now = threading.Event()   # set to run an upkeep pass early
        self.queue = {}                # rclone's upload queue: path -> item
        self.reported_upload_errors = set()
        self.rclone_ok = False         # is the mount's rc socket answering?
        self.online = None             # can rclone reach OneDrive? (None = unknown)
        self.signin_needed = False     # OneDrive rejected the saved sign-in
        self.pin_jobs = queue.Queue()
        self.pin_pending = set()       # files queued or downloading for a pin
        self.pin_errors = {}           # path -> error message
        self.last_upkeep = None
        self.cache_bytes = 0
        self.upload_hooks = []         # functions called with the queue on every poll

    # --- helpers -----------------------------------------------------------

    def to_rel(self, path):
        """Turn an absolute path inside ~/OneDrive into a path relative to it."""
        path = os.path.abspath(os.path.expanduser(path))
        mount = str(self.mount)
        if path == mount:
            return ""
        if not path.startswith(mount + os.sep):
            raise UserError(f"{path} isn't inside {mount}.")
        return cachemod.clean_relative(path[len(mount) + 1:])

    def abs_path(self, rel):
        return self.mount / rel if rel else self.mount

    def item_id(self, rel):
        """OneDrive's permanent ID for an item, asked from rclone."""
        item = rclone.rc("operations/stat", {"fs": self.fs, "remote": rel}).get("item")
        if not item or not item.get("ID"):
            raise UserError(f"Couldn't find {rel} on OneDrive (is it uploaded yet?).")
        return rclone.item_id_from_rclone(item["ID"])

    def access_token(self):
        """A working Graph token, read from our rclone config.

        rclone renews the token when *it* needs to. If ours is about to expire,
        a cheap rc call (`operations/about`, which only reads the quota) makes
        rclone renew and save it.
        """
        token = rclone.read_token() or {}
        expiry = cachemod.parse_time(token.get("expiry"))
        if expiry is None or expiry < datetime.now(timezone.utc) + timedelta(minutes=2):
            try:
                rclone.rc("operations/about", {"fs": self.fs}, timeout=60)
            except rclone.RcError:
                pass
            token = rclone.read_token() or {}
        if "access_token" not in token:
            raise graph.GraphError(0, "not signed in")
        return token["access_token"]

    def is_dir(self, rel):
        try:
            return self.abs_path(rel).is_dir()
        except OSError:
            return False

    def walk_files(self, rel):
        """All files at or under `rel`, read through the mount (folder listings only)."""
        top = self.abs_path(rel)
        if not top.is_dir():
            yield rel
            return
        for folder, _dirs, files in os.walk(top):
            for name in files:
                yield str((Path(folder) / name).relative_to(self.mount))

    def save_settings(self):
        config.save_settings(self.settings)

    # --- state of files ----------------------------------------------------

    def state_of(self, rel, rules, is_dir=None):
        if is_dir is None:
            is_dir = self.is_dir(rel)  # may have to ask OneDrive for a folder listing
        rule = self.store.effective_rule(rel, rules)
        with self.lock:
            queue_item = self.queue.get(rel)
            if is_dir:
                prefix = rel + "/" if rel else ""
                pending = any(p.startswith(prefix) for p in self.pin_pending)
            else:
                pending = rel in self.pin_pending
            error = self.pin_errors.get(rel)
        sidecar = None if is_dir else self.cache.sidecar(rel)
        return file_state(rel, is_dir, rule, sidecar, queue_item, pending, error)

    # --- requests from the CLI, Nemo and tray -------------------------------

    def handle(self, message):
        cmd = message.get("cmd")
        handlers = {
            "status": self.cmd_status, "info": self.cmd_info,
            "pin": self.cmd_pin, "unpin": self.cmd_unpin, "free": self.cmd_free,
            "activity": self.cmd_activity, "pause": self.cmd_pause, "resume": self.cmd_resume,
            "settings": self.cmd_settings, "upkeep": self.cmd_upkeep,
            "weburl": self.cmd_weburl,
        }
        if cmd not in handlers:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        try:
            answer = handlers[cmd](message)
        except UserError as error:
            return {"ok": False, "error": str(error)}
        except (rclone.RcError, graph.GraphError, cachemod.CacheError, OSError) as error:
            return {"ok": False, "error": str(error)}
        answer.setdefault("ok", True)
        return answer

    def cmd_status(self, message):
        rules = self.store.rules()
        states = {}
        # Nemo already knows which paths are folders; it tells us, which saves
        # a possibly slow look through the mount.
        dirs = set(message["dirs"]) if "dirs" in message else None
        for path in message.get("paths", []):
            try:
                is_dir = (path in dirs) if dirs is not None else None
                states[path] = self.state_of(self.to_rel(path), rules, is_dir)
            except (UserError, cachemod.CacheError):
                states[path] = "outside"
        return {"states": states}

    def cmd_info(self, message):
        with self.lock:
            uploading = sum(1 for item in self.queue.values() if item.get("uploading"))
            upload_errors = sum(1 for item in self.queue.values()
                                if item.get("tries", 0) >= ERROR_TRIES)
            info = {
                "mounted": os.path.ismount(self.mount),
                "rclone": self.rclone_ok,
                "online": self.online,
                "signin_needed": self.signin_needed,
                "paused": self.settings.get("paused", False),
                "queued": len(self.queue),
                "uploading": uploading,
                "upload_errors": upload_errors,
                "pinning": len(self.pin_pending),
                "pin_errors": len(self.pin_errors),
                "cache_bytes": self.cache_bytes,
                "last_upkeep": self.last_upkeep,
            }
        usage = shutil.disk_usage(config.CACHE_DIR if config.CACHE_DIR.exists() else Path.home())
        info.update(disk_free=usage.free, disk_total=usage.total,
                    mount_path=str(self.mount), folder=self.settings["remote_folder"],
                    pinned=[r["path"] for r in self.store.rules() if r["state"] == pins.PINNED],
                    settings={k: self.settings[k] for k in ("evict_after_days", "min_free_space_percent")})
        return info

    def cmd_pin(self, message):
        rel = self.to_rel(message["path"])
        if rel == "":
            raise UserError("Pin the folders inside OneDrive instead of the whole thing.")
        if not self.abs_path(rel).exists():
            raise UserError(f"{rel} doesn't exist.")
        is_dir = self.is_dir(rel)

        # Will it fit? Count what still has to be downloaded.
        needed = 0
        for file_rel in self.walk_files(rel):
            sidecar = self.cache.sidecar(file_rel)
            size = os.stat(self.abs_path(file_rel)).st_size
            needed += size - (cachemod.cached_bytes(sidecar) if sidecar else 0)
        usage = shutil.disk_usage(config.CACHE_DIR)
        keep_free = usage.total * self.settings["min_free_space_percent"] / 100
        if needed > usage.free - keep_free and not message.get("force"):
            raise UserError(
                f"{rel} needs {human(needed)} more, but only {human(max(0, usage.free - keep_free))} "
                f"can be used (keeping {self.settings['min_free_space_percent']}% of the disk free).")

        item_id = self.item_id(rel)
        if is_dir:
            # Pinning a folder pins everything in it, like on Windows.
            self.store.remove_rules_inside(rel)
        self.store.set_rule(item_id, rel, pins.PINNED)
        count = self.queue_pin_downloads(rel)
        self.store.log("pinned", rel, f"Always keep on this device: {rel} ({human(needed)} to download)")
        return {"message": f"Pinned {rel}. Downloading {count} file(s), {human(needed)}."}

    def cmd_unpin(self, message):
        rel = self.to_rel(message["path"])
        rule = self.store.rule_at(rel)
        if rule and rule["state"] == pins.PINNED:
            self.store.remove_rule(rel)
            self.store.remove_rules_inside(rel)
        if self.store.is_pinned(rel):
            # Still pinned through a parent folder: add an exception for this item.
            self.store.set_rule(self.item_id(rel), rel, pins.LOCAL)
        self.store.log("unpinned", rel, f"No longer always kept: {rel}")
        return {"message": f"{rel} is no longer pinned. Downloaded files stay until space is needed."}

    def cmd_free(self, message):
        rel = self.to_rel(message["path"])
        if rel == "" and not message.get("everything"):
            raise UserError("Choose a file or folder inside OneDrive.")
        # 1. Make sure no pin brings it straight back.
        rule = self.store.rule_at(rel)
        if rule:
            self.store.remove_rule(rel)
        if self.is_dir(rel):
            self.store.remove_rules_inside(rel)
        if self.store.is_pinned(rel):
            self.store.set_rule(self.item_id(rel), rel, pins.ONLINE)

        # 2. Remove the downloaded copies, skipping anything busy.
        open_now = cachemod.open_files(self.mount, self.cache.data_root)
        with self.lock:
            queued = set(self.queue)
            pending = set(self.pin_pending)
        freed_bytes, freed, skipped = 0, 0, []
        for file_rel in list(self.cache.cached_files(rel)):
            if file_rel in open_now:
                skipped.append(f"{file_rel} (open in a program)")
            elif file_rel in queued:
                skipped.append(f"{file_rel} (still uploading)")
            elif file_rel in pending:
                skipped.append(f"{file_rel} (downloading for a pin)")
            else:
                try:
                    freed_bytes += self.cache.free(file_rel)
                    freed += 1
                except cachemod.CacheError as error:
                    skipped.append(str(error))
        if freed:
            self.store.log("freed", rel, f"Freed up space: {rel} ({freed} file(s), {human(freed_bytes)})")
        if skipped and not freed and not self.is_dir(rel):
            raise UserError("Can't free it yet: " + skipped[0])
        return {"message": f"Freed {freed} file(s), {human(freed_bytes)}.", "skipped": skipped}

    def cmd_activity(self, message):
        return {"activity": self.store.recent(message.get("limit", 20), message.get("after", 0))}

    def cmd_pause(self, message):
        self.settings["paused"] = True
        self.save_settings()
        self.hold_uploads()
        self.store.log("paused", None, "Paused: uploads and pin downloads wait until you resume.")
        return {"message": "Paused. Opening files still works; uploads wait."}

    def cmd_resume(self, message):
        self.settings["paused"] = False
        self.save_settings()
        self.release_uploads()
        self.store.log("resumed", None, "Resumed.")
        return {"message": "Resumed."}

    def cmd_settings(self, message):
        changes = message.get("set", {})
        if "evict_after_days" in changes:
            days = int(changes["evict_after_days"])
            if days < 0:
                raise UserError("Days can't be negative (0 means never).")
            self.settings["evict_after_days"] = days
        if "min_free_space_percent" in changes:
            percent = float(changes["min_free_space_percent"])
            if not 0 <= percent <= 50:
                raise UserError("Keep-free space must be between 0 and 50 %.")
            self.settings["min_free_space_percent"] = percent
        if changes:
            self.save_settings()
            self.upkeep_now.set()
        return {"settings": {k: self.settings[k] for k in ("evict_after_days", "min_free_space_percent")}}

    def cmd_weburl(self, message):
        """The onedrive.com address of a file or folder (Nemo: "View online")."""
        rel = self.to_rel(message["path"])
        item = graph.item_by_id(self.access_token(), self.item_id(rel))
        if item is None or "webUrl" not in item:
            raise UserError(f"Couldn't find {rel} on OneDrive.")
        return {"url": item["webUrl"]}

    def cmd_upkeep(self, message):
        """Run the 5-minute pass right now and wait for the result (for testing)."""
        return {"result": self.upkeep()}

    # --- upload queue --------------------------------------------------------

    def poll_queue(self):
        try:
            items = rclone.rc("vfs/queue", timeout=10).get("queue", [])
            self.rclone_ok = True
        except rclone.RcError:
            self.rclone_ok = False
            return
        current = {item["name"]: item for item in items}
        with self.lock:
            finished = set(self.queue) - set(current)
            self.queue = current
        for rel in finished:
            sidecar = self.cache.sidecar(rel)
            if sidecar is not None and not sidecar.get("Dirty"):
                self.store.log("uploaded", rel, f"Uploaded {rel}")
            self.reported_upload_errors.discard(rel)
        for rel, item in current.items():
            if item.get("tries", 0) >= ERROR_TRIES and rel not in self.reported_upload_errors:
                self.reported_upload_errors.add(rel)
                self.store.log("error", rel, f"Upload of {rel} keeps failing; rclone keeps retrying.")
        if self.settings.get("paused"):
            self.hold_uploads(current.values())
        for hook in self.upload_hooks:
            hook(current, finished)

    def set_expiry(self, item, seconds):
        try:
            rclone.rc("vfs/queue-set-expiry", {"id": item["id"], "expiry": seconds})
        except rclone.RcError:
            pass  # it may have just finished uploading

    def hold_uploads(self, items=None):
        """Push every waiting upload a year into the future (rclone's own pause)."""
        if items is None:
            with self.lock:
                items = list(self.queue.values())
        for item in items:
            if not item.get("uploading") and item.get("expiry", 0) < HOLD_SECONDS / 2:
                self.set_expiry(item, HOLD_SECONDS)

    def release_uploads(self):
        with self.lock:
            items = list(self.queue.values())
        for item in items:
            if not item.get("uploading"):
                self.set_expiry(item, 0)

    # --- pins ---------------------------------------------------------------

    def queue_pin_downloads(self, rel):
        """Queue every not-yet-downloaded file under a pinned item. Returns how many."""
        rules = self.store.rules()
        count = 0
        for file_rel in self.walk_files(rel):
            if not self.store.is_pinned(file_rel, rules):
                continue  # an exception inside the pinned folder
            sidecar = self.cache.sidecar(file_rel)
            if sidecar is not None and cachemod.is_fully_cached(sidecar):
                continue
            with self.lock:
                if file_rel in self.pin_pending:
                    continue
                self.pin_pending.add(file_rel)
            self.pin_jobs.put(file_rel)
            count += 1
        return count

    def pin_worker(self):
        while not self.stopping.is_set():
            try:
                rel = self.pin_jobs.get(timeout=1)
            except queue.Empty:
                continue
            while self.settings.get("paused") and not self.stopping.is_set():
                time.sleep(2)
            try:
                if self.store.is_pinned(rel):
                    self.download(rel)
                    with self.lock:
                        self.pin_errors.pop(rel, None)
            except OSError as error:
                with self.lock:
                    self.pin_errors[rel] = str(error)
                self.store.log("error", rel, f"Couldn't download pinned file {rel}: {error}")
            finally:
                with self.lock:
                    self.pin_pending.discard(rel)

    def download(self, rel):
        """Read a file end-to-end through the mount, which makes rclone download it all."""
        with open(self.abs_path(rel), "rb") as f:
            while f.read(READ_CHUNK):
                if self.stopping.is_set():
                    return

    def check_rules(self):
        """Make sure every rule still points at its item; follow renames (DESIGN.md §3).

        Returns True only if every rule could be checked.
        """
        self.store.forget_old_deleted()
        if not self.rclone_ok:
            return False
        all_checked = True
        token = None
        folder = self.settings["remote_folder"]
        for rule in self.store.rules(status=None):
            try:
                if rule["status"] == pins.ACTIVE and os.path.lexists(self.abs_path(rule["path"])):
                    if self.item_id(rule["path"]) == rule["item_id"]:
                        self.store.confirm(rule["item_id"])
                        continue
                # Not where we last saw it: ask OneDrive where it is now.
                token = token or self.access_token()
                item = graph.item_by_id(token, rule["item_id"])
                if item is None:
                    if rule["status"] != pins.DELETED:
                        self.store.set_status(rule["item_id"], pins.DELETED)
                        self.store.log("deleted", rule["path"],
                                       f"{rule['path']} was deleted on OneDrive. Its pin is kept "
                                       "for 30 days in case you restore it.")
                    continue
                new_path = graph.path_in_folder(item, folder)
                if new_path is None:
                    if rule["status"] != pins.INACTIVE:
                        self.store.set_status(rule["item_id"], pins.INACTIVE)
                        self.store.log("moved", rule["path"],
                                       f"{rule['path']} was moved outside {folder}; its pin is paused.")
                elif new_path != rule["path"] or rule["status"] != pins.ACTIVE:
                    self.store.update_path(rule["item_id"], new_path)
                    self.store.log("renamed", new_path,
                                   f"Pin followed a rename: {rule['path']} → {new_path}")
                else:
                    self.store.confirm(rule["item_id"])
            except (UserError, rclone.RcError, graph.GraphError, OSError) as error:
                # Couldn't check (offline?). Then we must not free anything by timer.
                print(f"Couldn't check rule for {rule['path']}: {error}", flush=True)
                all_checked = False
        return all_checked

    # --- the 5-minute pass ---------------------------------------------------

    def check_online(self):
        """Can rclone reach OneDrive, and is the sign-in still accepted?"""
        try:
            rclone.rc("operations/about", {"fs": self.fs}, timeout=60)
            self.online, self.signin_needed = True, False
        except rclone.RcError as error:
            text = str(error).lower()
            # Expired or revoked sign-in (e.g. unused for 90 days, password changed).
            expired = any(word in text for word in ("invalid_grant", "unauthorized", "401", "aadsts"))
            if expired and not self.signin_needed:
                self.store.log("error", None, "OneDrive no longer accepts the saved sign-in. "
                                              "Sign in again (tray menu, or: onedrive-ondemand setup).")
            self.signin_needed = expired
            self.online = False if self.rclone_ok else None

    def online_loop(self):
        """Check the connection every minute (the full upkeep pass is every 5)."""
        self.check_online()

    def upkeep(self):
        """Follow renames, top up pins, free space. Returns a short summary."""
        with self.upkeep_lock:
            return self._upkeep()

    def _upkeep(self):
        self.check_online()

        all_checked = self.check_rules()
        queued = 0
        if self.rclone_ok:
            for rule in self.store.rules():
                if rule["state"] == pins.PINNED:
                    try:
                        queued += self.queue_pin_downloads(rule["path"])
                    except OSError:
                        all_checked = False
        freed = self.evict(all_checked) if self.rclone_ok else []
        self.cache_bytes = sum(self.cache.disk_bytes(rel) for rel in self.cache.cached_files())
        self.last_upkeep = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {"rules_checked": all_checked, "pin_downloads_queued": queued,
                "freed": len(freed), "online": self.online}

    def evict(self, timer_allowed):
        rules = self.store.rules()
        open_now = cachemod.open_files(self.mount, self.cache.data_root)
        with self.lock:
            busy = set(self.queue) | self.pin_pending
        candidates = []
        for rel in self.cache.cached_files():
            if rel in busy or rel in open_now or self.store.is_pinned(rel, rules):
                continue
            sidecar = self.cache.sidecar(rel)
            if sidecar is None or sidecar.get("Dirty"):
                continue
            atime = cachemod.parse_time(sidecar.get("ATime"))
            if atime is None:
                continue
            candidates.append(Candidate(rel, atime, sidecar["ATime"], self.cache.disk_bytes(rel)))

        usage = shutil.disk_usage(config.CACHE_DIR)
        chosen = choose_evictions(candidates, datetime.now(timezone.utc),
                                  self.settings["evict_after_days"], usage.free, usage.total,
                                  self.settings["min_free_space_percent"], timer_allowed)
        freed, total = [], 0
        for c in chosen:
            try:
                total += self.cache.free(c.rel, expected_atime=c.atime_text)
                freed.append(c.rel)
            except cachemod.CacheError as error:
                print(f"Not freeing {c.rel}: {error}", flush=True)
        if freed:
            self.store.log("freed", None,
                           f"Freed up space: {len(freed)} file(s) not opened recently ({human(total)})")
        return freed

    # --- running -------------------------------------------------------------

    def run(self):
        config.RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        server = ipc.serve(self.handle)
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self.stopping.set())

        threads = [threading.Thread(target=self.loop, args=(self.poll_queue, QUEUE_POLL_SECONDS),
                                    name="uploads", daemon=True),
                   threading.Thread(target=self.upkeep_loop, name="upkeep", daemon=True),
                   threading.Thread(target=self.loop, args=(self.online_loop, 60),
                                    name="online", daemon=True)]
        threads += [threading.Thread(target=self.pin_worker, name=f"pins-{n}", daemon=True)
                    for n in range(PIN_WORKERS)]
        for thread in threads:
            thread.start()
        print(f"Daemon running. Socket: {ipc.DAEMON_SOCKET}", flush=True)
        self.stopping.wait()
        server.shutdown()
        server.server_close()
        ipc.DAEMON_SOCKET.unlink(missing_ok=True)
        self.store.close()

    def loop(self, function, seconds):
        while not self.stopping.is_set():
            try:
                function()
            except Exception as error:  # keep running; log and try again next time
                print(f"{function.__name__} failed: {error!r}", flush=True)
            self.stopping.wait(seconds)

    def upkeep_loop(self):
        # Give rclone a moment to start before the first pass.
        self.stopping.wait(20)
        while not self.stopping.is_set():
            try:
                print(f"Upkeep: {self.upkeep()}", flush=True)
            except Exception as error:
                print(f"Upkeep failed: {error!r}", flush=True)
            self.upkeep_now.wait(UPKEEP_SECONDS)
            self.upkeep_now.clear()


def human(n):
    """1536 -> "1.5 KB"."""
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            break
        n /= 1024
    return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"


def main():
    Daemon().run()
