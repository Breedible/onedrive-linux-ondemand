import os
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path
from unittest import mock

from onedrive_ondemand import config, daemon, previews, thumbnailer
from tests.fakehome import FakeHome


def png_with_uri(path, uri):
    """A tiny PNG that carries a Thumb::URI text, like the ones Nemo saves."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"tEXt", b"Thumb::URI\0" + uri.encode())
                     + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
                     + chunk(b"IEND", b""))


class CacheFilesTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.root = config.THUMBNAIL_CACHE

    def tearDown(self):
        self.home.cleanup()

    def test_uri_to_path(self):
        self.assertEqual(previews.uri_to_path("file:///home/me/OneDrive%20-%20Work/a%23b.jpg"),
                         "/home/me/OneDrive - Work/a#b.jpg")
        self.assertIsNone(previews.uri_to_path("sftp://server/x.jpg"))

    def test_standard_file_name(self):
        # From the freedesktop.org thumbnail spec: MD5 of the address.
        files = previews.preview_files("file:///home/jens/photos/me.png", self.root)
        self.assertEqual(files[0], self.root / "normal" / "c6ee772d9e49320e97ec29a7eb5b1697.png")
        self.assertIn(self.root / "fail" / "gnome-thumbnail-factory" /
                      "c6ee772d9e49320e97ec29a7eb5b1697.png", files)

    def test_delete_one(self):
        uri = "file:///home/me/OneDrive%20-%20Personal/a.jpg"
        for path in previews.preview_files(uri, self.root)[:2]:
            png_with_uri(path, uri)
        self.assertEqual(previews.delete(uri, self.root), 2)
        self.assertEqual(previews.delete(uri, self.root), 0)

    def test_png_text(self):
        path = self.root / "normal" / "x.png"
        png_with_uri(path, "file:///a.jpg")
        self.assertEqual(previews.png_text(path), "file:///a.jpg")
        path.write_bytes(b"not a png")
        self.assertIsNone(previews.png_text(path))

    def test_delete_all_under_only_touches_that_folder(self):
        ours = "file:///home/me/OneDrive%20-%20Personal/sub/a.jpg"
        similar = "file:///home/me/OneDrive%20-%20Personal%20old/b.jpg"
        other = "file:///home/me/Pictures/c.jpg"
        for uri in (ours, similar, other):
            png_with_uri(previews.preview_files(uri, self.root)[0], uri)
        png_with_uri(previews.preview_files(ours, self.root)[-1], ours)  # a "failed" marker
        self.assertEqual(previews.delete_all_under("/home/me/OneDrive - Personal", self.root), 2)
        left = sorted(p.name for p in self.root.rglob("*.png"))
        self.assertEqual(len(left), 2)


class TrackerTest(unittest.TestCase):
    def test_kept_while_folder_is_shown(self):
        tracker = previews.Tracker(grace=120)
        tracker.record("file:///od/photos/a.jpg", now=1000)
        tracker.viewing(["/od/photos"], now=1100)
        self.assertEqual(tracker.expired(now=1200), [])          # seen 100 s ago
        self.assertEqual(tracker.expired(now=1221), ["file:///od/photos/a.jpg"])
        self.assertEqual(tracker.expired(now=1300), [])          # forgotten once expired

    def test_without_viewing_reports_it_expires_after_the_grace(self):
        tracker = previews.Tracker(grace=120)
        tracker.record("file:///od/a.jpg", now=1000)
        tracker.viewing(["/od/elsewhere"], now=1100)
        self.assertEqual(tracker.expired(now=1121), ["file:///od/a.jpg"])

    def test_placeholders(self):
        tracker = previews.Tracker()
        tracker.record("file:///od/a.jpg", placeholder=True)
        tracker.record("file:///od/b.jpg")
        self.assertEqual(tracker.placeholders(), ["file:///od/a.jpg"])
        self.assertEqual(list(tracker.made), ["file:///od/b.jpg"])


class ThumbnailerTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.account = self.home.add_account("personal", "Personal")
        self.mount = self.account.mount_path()
        self.out = str(self.home.root / "out.png")

    def tearDown(self):
        self.home.cleanup()

    def test_expand(self):
        words = thumbnailer.expand("/usr/bin/t -s %s %u %o 100%%", "file:///a%20b.jpg", "/o.png", 128)
        self.assertEqual(words, ["/usr/bin/t", "-s", "128", "file:///a%20b.jpg", "/o.png", "100%"])
        self.assertEqual(thumbnailer.expand("t %i", "file:///a%20b.jpg", "o", 1), ["t", "/a b.jpg"])

    def test_finds_the_normal_thumbnailer_but_never_ours(self):
        folder = self.home.root / "share" / "thumbnailers"
        folder.mkdir(parents=True)
        (folder / "onedrive-ondemand.thumbnailer").write_text(
            "[Thumbnailer Entry]\nExec=ours %u\nMimeType=image/png;\n")
        (folder / "zz-pixbuf.thumbnailer").write_text(
            "[Thumbnailer Entry]\nExec=pixbuf -s %s %u %o\nMimeType=image/png;image/jpeg;\n")
        with mock.patch.dict(os.environ, {"XDG_DATA_DIRS": str(self.home.root / "share")}):
            self.assertEqual(thumbnailer.find_other_thumbnailer("image/png"), "pixbuf -s %s %u %o")
            self.assertIsNone(thumbnailer.find_other_thumbnailer("video/mp4"))

    def test_placeholder_is_a_valid_png(self):
        thumbnailer.write_placeholder(self.out, 128)
        data = Path(self.out).read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", data[16:24])
        self.assertEqual((width, height), (128, 128))

    def test_onedrive_file_is_never_handed_to_another_thumbnailer_when_daemon_is_down(self):
        # Handing over would make the normal thumbnailer read (download) the file.
        uri = (self.mount / "photo.jpg").as_uri()
        with mock.patch.object(thumbnailer, "hand_over") as hand_over:
            thumbnailer.main(uri, self.out, 128)
        hand_over.assert_not_called()
        self.assertTrue(Path(self.out).read_bytes().startswith(b"\x89PNG"))

    def test_onedrive_file_that_is_downloaded_uses_the_normal_thumbnailer(self):
        uri = (self.mount / "photo.jpg").as_uri()
        with mock.patch.object(thumbnailer.ipc, "request", return_value={"ok": True, "local": True}), \
                mock.patch.object(thumbnailer, "hand_over", return_value=True) as hand_over:
            thumbnailer.main(uri, self.out, 128)
        hand_over.assert_called_once()

    def test_errors_still_give_a_picture(self):
        uri = (self.mount / "photo.jpg").as_uri()
        broken = {"ok": True, "image": "bm90IGEgcGljdHVyZQ=="}  # "not a picture"
        with mock.patch.object(thumbnailer.ipc, "request", return_value=broken):
            thumbnailer.main(uri, self.out, 128)
        self.assertTrue(Path(self.out).read_bytes().startswith(b"\x89PNG"))

    def test_other_files_are_handed_over(self):
        with mock.patch.object(thumbnailer, "hand_over", return_value=True) as hand_over:
            with self.assertRaises(SystemExit) as done:
                thumbnailer.main("file:///home/me/Pictures/a.jpg", self.out, 128)
        self.assertEqual(done.exception.code, 0)
        hand_over.assert_called_once()

    def test_install_file(self):
        thumbnailer.install("/usr/bin/onedrive-ondemand")
        text = (config.THUMBNAILER_DIR / "onedrive-ondemand.thumbnailer").read_text()
        self.assertIn("Exec=/usr/bin/onedrive-ondemand thumbnail %u %o %s", text)
        self.assertIn("image/jpeg;", text)
        thumbnailer.uninstall()
        self.assertFalse((config.THUMBNAILER_DIR / "onedrive-ondemand.thumbnailer").exists())

    def test_command_line_entry_point(self):
        # The real command Nemo runs, end to end, for a file outside OneDrive
        # with no thumbnailer for its type: it must just fail (exit 1).
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.home.root / "cfg"),
                   XDG_DATA_HOME=str(self.home.root / "share"), XDG_DATA_DIRS="/nonexistent")
        result = subprocess.run([sys.executable, "-m", "onedrive_ondemand", "thumbnail",
                                 "file:///tmp/x.unknownkind", self.out, "128"],
                                env=env, capture_output=True,
                                cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(result.returncode, 1, result.stderr)


class DaemonPreviewTest(unittest.TestCase):
    """The daemon's side: never downloads, says "local" for downloaded files."""

    def setUp(self):
        self.home = FakeHome()
        self.account = self.home.add_account("personal", "Personal")
        self.daemon = daemon.Daemon(self.account)
        self.daemon.online = True
        self.mount = self.account.mount_path()

    def tearDown(self):
        self.daemon.store.close()
        self.home.cleanup()

    def ask(self, rel):
        return self.daemon.handle({"cmd": "preview", "path": str(self.mount / rel)})

    def test_downloaded_file_is_local(self):
        sidecar = self.daemon.cache.sidecar_file("a.jpg")
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text('{"Size": 5, "Rs": [{"Pos": 0, "Size": 5}]}')
        self.assertTrue(self.ask("a.jpg")["local"])

    def test_online_only_file_gets_onedrives_preview(self):
        with mock.patch.object(daemon.graph, "thumbnail", return_value=b"JPEG") as thumb, \
                mock.patch.object(self.daemon, "access_token", return_value="t"):
            answer = self.ask("sub/a.jpg")
        self.assertEqual(answer["image"], "SlBFRw==")
        self.assertEqual(thumb.call_args[0][1:], ("d-personal", "LinuxClientTest/sub/a.jpg"))
        self.assertEqual(len(self.daemon.previews.made), 1)

    def test_offline_gives_a_placeholder_without_asking_onedrive(self):
        self.daemon.online = False
        with mock.patch.object(daemon.graph, "thumbnail") as thumb:
            self.assertTrue(self.ask("a.jpg")["placeholder"])
        thumb.assert_not_called()

    def test_onedrive_error_gives_a_placeholder(self):
        error = daemon.graph.GraphError(500, "busy")
        with mock.patch.object(daemon.graph, "thumbnail", side_effect=error), \
                mock.patch.object(self.daemon, "access_token", return_value="t"):
            self.assertTrue(self.ask("a.jpg")["placeholder"])


if __name__ == "__main__":
    unittest.main()
