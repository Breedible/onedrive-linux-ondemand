"""Our thumbnailer: makes picture previews without downloading the pictures (DESIGN.md §8).

Cinnamon's file manager (Nemo) makes previews by running a "thumbnailer"
program listed in a .thumbnailer file. Setup installs ours in
~/.local/share/thumbnailers/, which Cinnamon prefers over the system's own
(checked with cinnamon-desktop 6.0, RESEARCH.md §11). Nemo runs:

    onedrive-ondemand thumbnail <file address> <output.png> <size>

- For a file outside OneDrive, we hand the job straight to the normal
  thumbnailer for that kind of file, so nothing changes there.
- For a file in OneDrive, we ask the daemon. It either sends OneDrive's own
  small preview (the file isn't downloaded), or says the file is already on
  this computer, in which case the normal thumbnailer can read it.

IMPORTANT: for OneDrive files we must ALWAYS write a picture. If a
thumbnailer fails, Cinnamon reads the whole file itself to make a preview
(checked 2026-10-30), which would download it. So when nothing else works
(offline, say) we write a plain stand-in picture instead.
"""

import base64
import configparser
import glob
import mimetypes
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

from onedrive_ondemand import config, ipc, previews

# Our own .thumbnailer file. Skipped when looking for the normal thumbnailer
# to hand a file to.
OUR_FILE_NAME = "onedrive-ondemand.thumbnailer"
# The kinds of file we make previews for: photos, videos and PDFs, which
# Nemo would otherwise (partly) download to preview. OneDrive has previews
# for all of them.
MIME_TYPES = [
    "image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp", "image/tiff",
    "image/heic", "image/heif", "image/avif", "image/svg+xml",
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo",
    "video/mpeg", "video/3gpp", "video/x-m4v",
    "application/pdf",
]


def find_other_thumbnailer(mime_type):
    """The command of the normal thumbnailer for this kind of file, or None."""
    folders = [str(config.THUMBNAILER_DIR)]
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    folders += [os.path.join(d, "thumbnailers") for d in data_dirs.split(":") if d]
    for folder in folders:
        for path in sorted(glob.glob(os.path.join(folder, "*.thumbnailer"))):
            if os.path.basename(path) == OUR_FILE_NAME:
                continue
            entry = configparser.ConfigParser(interpolation=None)
            try:
                entry.read(path, encoding="utf-8")
                section = entry["Thumbnailer Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            types = [t.strip() for t in section.get("MimeType", "").split(";")]
            if mime_type in types and section.get("Exec"):
                return section["Exec"]
    return None


def expand(command, uri, out, size):
    """Fill in a thumbnailer's %u %i %o %s placeholders. Returns a list for subprocess."""
    path = previews.uri_to_path(uri) or uri
    values = {"u": uri, "i": path, "o": out, "s": str(size), "%": "%"}
    words = []
    for word in command.split():
        result, i = "", 0
        while i < len(word):
            if word[i] == "%" and i + 1 < len(word) and word[i + 1] in values:
                result += values[word[i + 1]]
                i += 2
            else:
                result += word[i]
                i += 1
        words.append(result)
    return words


def guess_type(path):
    mime_type, _encoding = mimetypes.guess_type(path, strict=False)
    if mime_type is None:
        # Some types (like HEIC photos) are only known to the desktop's own list.
        try:
            from gi.repository import Gio
            mime_type, _uncertain = Gio.content_type_guess(path, None)
        except (ImportError, ValueError):
            pass
    return mime_type


def hand_over(uri, out, size):
    """Let the normal thumbnailer make the preview. True if it worked."""
    path = previews.uri_to_path(uri) or ""
    command = find_other_thumbnailer(guess_type(path) or "")
    if not command:
        return False
    try:
        result = subprocess.run(expand(command, uri, out, size), timeout=60,
                                stdin=subprocess.DEVNULL, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def write_preview(jpeg, out, size):
    """Save OneDrive's preview (usually a JPEG) as the PNG Nemo expects, at most `size` pixels."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    loader = GdkPixbuf.PixbufLoader()
    loader.write(jpeg)
    loader.close()
    picture = loader.get_pixbuf()
    width, height = picture.get_width(), picture.get_height()
    if max(width, height) > size:
        scale = size / max(width, height)
        picture = picture.scale_simple(max(1, round(width * scale)), max(1, round(height * scale)),
                                       GdkPixbuf.InterpType.BILINEAR)
    try:
        picture.savev(out, "png", [], [])
    except GLib.Error as error:
        raise OSError(str(error)) from None


def write_placeholder(out, size):
    """Write a plain stand-in picture (a light grey square with a darker frame).

    Uses only the standard library, so it works even if something else is broken.
    """
    size = max(16, min(int(size), 256))
    border, fill = (150, 150, 150), (228, 228, 228)
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG: each row starts with a "no filter" byte
        for x in range(size):
            edge = x < 2 or y < 2 or x >= size - 2 or y >= size - 2
            row += bytes(border if edge else fill)
        rows.append(bytes(row))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows)))
           + chunk(b"IEND", b""))
    with open(out, "wb") as f:
        f.write(png)


def make_onedrive_preview(account, uri, out, size):
    """A preview for a file inside OneDrive. Never reads a file that isn't downloaded."""
    path = previews.uri_to_path(uri)
    try:
        answer = ipc.request(account.daemon_socket, "preview", timeout=30, path=path, uri=uri)
    except (ipc.DaemonNotRunning, OSError, ValueError):
        answer = {}
    if answer.get("ok") and answer.get("local") and hand_over(uri, out, size):
        return  # already on this computer: the normal thumbnailer read it from disk
    if answer.get("ok") and answer.get("image"):
        try:
            write_preview(base64.b64decode(answer["image"]), out, size)
            return
        except Exception as error:  # a picture we can't read: use the stand-in
            print(f"Couldn't use OneDrive's preview: {error}", file=sys.stderr)
    write_placeholder(out, size)


def main(uri, out, size):
    path = previews.uri_to_path(uri)
    account = config.account_for_path(path) if path else None
    if account is None:
        # Not in OneDrive: behave exactly like the normal thumbnailer.
        sys.exit(0 if hand_over(uri, out, size) else 1)
    try:
        make_onedrive_preview(account, uri, out, size)
    except Exception as error:  # whatever happens, never let Nemo read the file itself
        print(f"Preview failed: {error}", file=sys.stderr)
        write_placeholder(out, size)


def install(launcher, folder=None):
    """Install our .thumbnailer file (done whenever previews are turned on)."""
    text = ("# Installed by onedrive-ondemand: previews of OneDrive files without\n"
            "# downloading them. Other files are handed to the normal thumbnailer.\n"
            "[Thumbnailer Entry]\n"
            f"TryExec={launcher}\n"
            f"Exec={launcher} thumbnail %u %o %s\n"
            f"MimeType={';'.join(MIME_TYPES)};\n")
    folder = Path(folder or config.THUMBNAILER_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / OUR_FILE_NAME).write_text(text, encoding="utf-8")


def uninstall(folder=None):
    (Path(folder or config.THUMBNAILER_DIR) / OUR_FILE_NAME).unlink(missing_ok=True)
