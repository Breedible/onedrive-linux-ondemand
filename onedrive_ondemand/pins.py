"""Pin rules and the activity log, stored in SQLite (DESIGN.md §3).

A *rule* says what should happen to a file or folder, and everything inside it:

    pinned  "Always keep on this device": download it and never free it.
    online  "Free up space": used inside a pinned folder to exclude one item.
    local   Not pinned, even though a parent folder is (inside a pinned
            folder, this is what "unpinning" one file means).

The nearest rule on an item or one of its parent folders wins. With no rule,
an item is simply "locally available" if it happens to be downloaded.

Rules are keyed on OneDrive's permanent item ID, so they can follow renames
and moves. The path is only the last place we saw the item.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

PINNED, ONLINE, LOCAL = "pinned", "online", "local"
# Rule status: is the item where we think it is?
ACTIVE = "active"      # yes
INACTIVE = "inactive"  # moved outside the folder this client shows
DELETED = "deleted"    # in the OneDrive recycle bin (or gone); kept 30 days

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    item_id    TEXT PRIMARY KEY,   -- OneDrive's permanent ID for the item
    path       TEXT NOT NULL,      -- last known path, relative to the OneDrive folder
    state      TEXT NOT NULL,      -- 'pinned', 'online' or 'local'
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_seen  TEXT NOT NULL,      -- last time we confirmed the path
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    kind    TEXT NOT NULL,         -- e.g. 'uploaded', 'pinned', 'freed', 'error'
    path    TEXT,
    message TEXT NOT NULL
);
-- The OneDrive version of each file the last time we saw it in sync.
-- Used by the conflict guard (DESIGN.md §7).
CREATE TABLE IF NOT EXISTS synced (
    path        TEXT PRIMARY KEY,
    size        INTEGER,
    hash        TEXT,
    recorded_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_inside(path, folder):
    """True if `path` is `folder` itself or somewhere inside it. "" is the top."""
    if folder == "":
        return True
    return path == folder or path.startswith(folder + "/")


def parents_and_self(path):
    """"a/b/c" -> ["a/b/c", "a/b", "a", ""]: where a rule for `path` could be."""
    result = [path]
    parent = PurePosixPath(path).parent
    while str(parent) != ".":
        result.append(str(parent))
        parent = parent.parent
    if path != "":
        result.append("")
    return result


class Store:
    def __init__(self, db_path):
        # The daemon uses several threads; one lock keeps SQLite calls in order.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _run(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # --- rules -------------------------------------------------------------

    def set_rule(self, item_id, path, state):
        """Add or replace the rule for one item. Replaces any other rule at that path."""
        now = _now()
        with self._lock, self._db:
            self._db.execute("DELETE FROM rules WHERE path = ? AND status = 'active' AND item_id != ?",
                             (path, item_id))
            self._db.execute(
                "INSERT INTO rules (item_id, path, state, status, created_at, last_seen) "
                "VALUES (?, ?, ?, 'active', ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET path = excluded.path, state = excluded.state, "
                "status = 'active', last_seen = excluded.last_seen, deleted_at = NULL",
                (item_id, path, state, now, now))

    def remove_rule(self, path):
        """Remove the active rule exactly at `path`, if there is one."""
        self._run("DELETE FROM rules WHERE path = ? AND status = 'active'", (path,))

    def remove_rules_inside(self, folder):
        """Remove every active rule *inside* `folder` (not on the folder itself)."""
        if folder == "":
            self._run("DELETE FROM rules WHERE path != '' AND status = 'active'")
        else:
            # Escape LIKE wildcards in folder names such as "50%_off".
            pattern = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
            self._run("DELETE FROM rules WHERE path LIKE ? ESCAPE '\\' AND status = 'active'",
                      (pattern,))

    def rules(self, status=ACTIVE):
        """Rules with the given status (None = all), as dicts."""
        if status is None:
            rows = self._run("SELECT * FROM rules ORDER BY path")
        else:
            rows = self._run("SELECT * FROM rules WHERE status = ? ORDER BY path", (status,))
        return [dict(row) for row in rows]

    def rule_at(self, path):
        rows = self._run("SELECT * FROM rules WHERE path = ? AND status = 'active'", (path,))
        return dict(rows[0]) if rows else None

    def effective_rule(self, path, rules=None):
        """The rule that applies to `path`: on it, or on the nearest parent folder.

        `rules` lets callers pass a list they already loaded, to avoid
        hundreds of database queries when checking many files.
        """
        if rules is None:
            rules = self.rules()
        by_path = {rule["path"]: rule for rule in rules}
        for candidate in parents_and_self(path):
            if candidate in by_path:
                return by_path[candidate]
        return None

    def is_pinned(self, path, rules=None):
        rule = self.effective_rule(path, rules)
        return rule is not None and rule["state"] == PINNED

    def update_path(self, item_id, new_path):
        """An item was renamed or moved: remember its new place."""
        self._run("UPDATE rules SET path = ?, status = 'active', last_seen = ?, deleted_at = NULL "
                  "WHERE item_id = ?", (new_path, _now(), item_id))

    def confirm(self, item_id):
        self._run("UPDATE rules SET last_seen = ?, status = 'active', deleted_at = NULL "
                  "WHERE item_id = ?", (_now(), item_id))

    def set_status(self, item_id, status):
        if status == DELETED:
            # Keep the first deletion time, so the 30 days count from then.
            self._run("UPDATE rules SET status = ?, deleted_at = COALESCE(deleted_at, ?) "
                      "WHERE item_id = ?", (status, _now(), item_id))
        else:
            self._run("UPDATE rules SET status = ?, deleted_at = NULL WHERE item_id = ?",
                      (status, item_id))

    def forget_old_deleted(self, days=30):
        """Drop rules for items deleted more than `days` ago (the recycle bin keeps 30)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        self._run("DELETE FROM rules WHERE status = 'deleted' AND deleted_at < ?", (cutoff,))

    # --- activity log ------------------------------------------------------

    def log(self, kind, path, message):
        self._run("INSERT INTO activity (time, kind, path, message) VALUES (?, ?, ?, ?)",
                  (_now(), kind, path, message))
        # Keep the log small: the newest 1000 entries are plenty.
        self._run("DELETE FROM activity WHERE id <= (SELECT MAX(id) FROM activity) - 1000")

    def recent(self, limit=20, after_id=0):
        rows = self._run("SELECT * FROM activity WHERE id > ? ORDER BY id DESC LIMIT ?",
                         (after_id, limit))
        return [dict(row) for row in rows]

    # --- last synced versions (conflict guard) -----------------------------

    def set_synced(self, path, size, hash_):
        self._run("INSERT INTO synced (path, size, hash, recorded_at) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT(path) DO UPDATE SET size = excluded.size, hash = excluded.hash, "
                  "recorded_at = excluded.recorded_at", (path, size, hash_, _now()))

    def synced(self, path):
        rows = self._run("SELECT * FROM synced WHERE path = ?", (path,))
        return dict(rows[0]) if rows else None

    def close(self):
        self._db.close()
