"""Picture previews: keeping track of them, and deleting them afterwards (DESIGN.md §8).

How previews work:
1. Nemo wants a preview of ~/OneDrive - Personal/photo.jpg and runs our
   thumbnailer (thumbnailer.py) instead of the normal one.
2. Our thumbnailer asks the daemon, which fetches OneDrive's own small
   preview (176 pixels) from Microsoft. The photo itself is never downloaded.
3. Nemo saves the preview in the standard place for previews,
   ~/.cache/thumbnails/<size>/<md5 of the file's address>.png.
4. When you've left the folder for a couple of minutes, the daemon deletes
   those preview files again (Tracker below). Coming back fetches them again,
   which is quick because they're small.

This module only ever deletes files inside ~/.cache/thumbnails.
"""

import hashlib
import os
import struct
import time
import urllib.parse
from pathlib import Path

from onedrive_ondemand import config

# The sub-folders previews can be saved in (one per size, plus "failed" markers).
SIZE_FOLDERS = ("normal", "large", "x-large", "xx-large")
FAIL_FOLDER = Path("fail") / "gnome-thumbnail-factory"

GRACE_SECONDS = 120   # keep previews this long after you leave their folder


def uri_to_path(uri):
    """"file:///home/me/a%20b.jpg" -> "/home/me/a b.jpg" (None for other kinds of address)."""
    if not uri.startswith("file://"):
        return None
    return urllib.parse.unquote(urllib.parse.urlsplit(uri).path)


def preview_files(uri, root=None):
    """Every place a preview of this file could be saved.

    The standard (freedesktop.org) rule: the file name is the MD5 of the
    file's address exactly as the file manager wrote it.
    """
    name = hashlib.md5(uri.encode()).hexdigest() + ".png"
    root = Path(root or config.THUMBNAIL_CACHE)
    return [root / folder / name for folder in SIZE_FOLDERS] + [root / FAIL_FOLDER / name]


def delete(uri, root=None):
    """Delete the saved previews of one file. Returns how many files were removed."""
    removed = 0
    for path in preview_files(uri, root):
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def png_text(path, keyword="Thumb::URI"):
    """Read one text field from a PNG file (the standard keeps the file's address there).

    PNG files are a list of "chunks"; text is kept in tEXt chunks before the
    picture data (IDAT). Returns None if it isn't there.
    """
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return None
                length, kind = struct.unpack(">I4s", header)
                if kind in (b"IDAT", b"IEND") or length > 1024 * 1024:
                    return None
                data = f.read(length)
                f.read(4)  # checksum
                if kind == b"tEXt" and b"\0" in data:
                    key, value = data.split(b"\0", 1)
                    if key.decode("latin-1") == keyword:
                        return value.decode("latin-1")
    except OSError:
        return None


def delete_all_under(folder, root=None):
    """Delete every saved preview of a file inside `folder` (e.g. the OneDrive folder).

    Used when the daemon starts (to tidy up after the last run) and when you
    turn previews off. Returns how many files were removed.
    """
    folder = os.path.abspath(folder)
    root = root or config.THUMBNAIL_CACHE
    removed = 0
    for sub in list(SIZE_FOLDERS) + [str(FAIL_FOLDER)]:
        try:
            names = os.listdir(Path(root) / sub)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".png"):
                continue
            path = Path(root) / sub / name
            uri = png_text(path)
            target = uri_to_path(uri) if uri else None
            if target and (target == folder or target.startswith(folder + os.sep)):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


class Tracker:
    """Remembers which previews we made, and which folders Nemo is showing.

    A preview is deleted GRACE_SECONDS after both of these are true:
    it was made, and its folder was last reported as being looked at.
    """

    def __init__(self, grace=GRACE_SECONDS):
        self.grace = grace
        self.made = {}      # uri -> (folder, made_at, placeholder)
        self.viewed = {}    # folder -> last time Nemo said it was showing it

    def record(self, uri, placeholder=False, now=None):
        path = uri_to_path(uri)
        if path is None:
            return
        self.made[uri] = (os.path.dirname(path), now or time.time(), placeholder)

    def viewing(self, folders, now=None):
        now = now or time.time()
        for folder in folders:
            self.viewed[os.path.abspath(folder)] = now

    def expired(self, now=None):
        """Previews whose time is up. They are forgotten here; the caller deletes them."""
        now = now or time.time()
        done = []
        for uri, (folder, made_at, _placeholder) in list(self.made.items()):
            last_seen = max(made_at, self.viewed.get(folder, 0))
            if now - last_seen > self.grace:
                done.append(uri)
                del self.made[uri]
        # Forget folders nobody has looked at for a long time.
        for folder, when in list(self.viewed.items()):
            if now - when > 10 * self.grace:
                del self.viewed[folder]
        return done

    def placeholders(self):
        """Previews that are only a stand-in picture (made while offline). Forgets them."""
        found = [uri for uri, (_f, _t, placeholder) in self.made.items() if placeholder]
        for uri in found:
            del self.made[uri]
        return found
