"""Tests the Nemo extension's logic with stand-in Nemo files and a fake daemon."""

import importlib.util
import unittest
from pathlib import Path

try:
    import gi
    gi.require_version("Nemo", "3.0")
    from gi.repository import Gio, Nemo  # noqa: F401
    HAVE_NEMO = True
except (ImportError, ValueError):
    HAVE_NEMO = False

SOURCE = Path(__file__).resolve().parent.parent / "nemo" / "onedrive_ondemand_nemo.py"


class FakeFile:
    """Stands in for Nemo.FileInfo."""

    def __init__(self, path, is_dir=False):
        self.path, self.dir, self.emblems, self.invalidated = path, is_dir, [], 0

    def get_location(self):
        return Gio.File.new_for_path(self.path)

    def is_directory(self):
        return self.dir

    def add_emblem(self, name):
        self.emblems.append(name)

    def invalidate_extension_info(self):
        self.invalidated += 1


@unittest.skipUnless(HAVE_NEMO, "Nemo's GObject introspection data isn't installed")
class ExtensionTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("onedrive_ondemand_nemo", SOURCE)
        self.ext = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ext)
        self.states = {}
        self.requests = []

        def fake_request(cmd, timeout, **args):
            self.requests.append((cmd, args))
            return {"ok": True, "states": {p: self.states.get(p, "online") for p in args["paths"]}}

        self.ext.ipc.request = fake_request
        self.completed = []
        self.ext.Nemo.info_provider_update_complete_invoke = \
            lambda closure, provider, handle, result: self.completed.append(handle)
        self.e = self.ext.OneDriveExtension()
        self.e.mount = "/home/me/OneDrive"

    def test_batches_and_emblems(self):
        self.states = {"/home/me/OneDrive/a": "pinned", "/home/me/OneDrive/d": "none"}
        files = [FakeFile("/home/me/OneDrive/a"), FakeFile("/home/me/OneDrive/b"),
                 FakeFile("/home/me/OneDrive/d", is_dir=True), FakeFile("/etc/hostname")]
        results = [self.e.update_file_info_full(None, i, None, f) for i, f in enumerate(files)]
        self.assertEqual(results[3], Nemo.OperationResult.COMPLETE)  # not ours: answered at once
        self.e.answer_batch(None)
        self.assertEqual(len(self.requests), 1)                       # one request for all
        self.assertEqual(self.requests[0][1]["dirs"], ["/home/me/OneDrive/d"])
        self.assertEqual([f.emblems for f in files],
                         [["onedrive-pinned"], ["onedrive-online"], [], []])
        self.assertEqual(self.completed, [0, 1, 2])

    def test_refresh_redraws_only_changed(self):
        a, b = FakeFile("/home/me/OneDrive/a"), FakeFile("/home/me/OneDrive/b")
        for i, f in enumerate((a, b)):
            self.e.update_file_info_full(None, i, None, f)
        self.e.answer_batch(None)
        self.states = {"/home/me/OneDrive/a": "syncing"}
        self.e.refresh()
        self.assertEqual((a.invalidated, b.invalidated), (1, 0))

    def test_no_daemon_means_no_emblems(self):
        def not_running(*_a, **_k):
            raise self.ext.ipc.DaemonNotRunning("down")
        self.ext.ipc.request = not_running
        f = FakeFile("/home/me/OneDrive/a")
        self.e.update_file_info_full(None, 0, None, f)
        self.e.answer_batch(None)
        self.assertEqual(f.emblems, [])
        self.assertEqual(self.completed, [0])

    def test_menu(self):
        self.states = {"/home/me/OneDrive/a": "pinned"}
        items = self.e.get_file_items(None, [FakeFile("/home/me/OneDrive/a")])
        labels = [i.get_property("label") for i in items[0].get_property("menu").get_items()]
        self.assertEqual(labels, ["Stop always keeping on this device", "Free up space", "View online"])
        self.assertEqual(self.e.get_file_items(None, [FakeFile("/tmp/x")]), [])


if __name__ == "__main__":
    unittest.main()
