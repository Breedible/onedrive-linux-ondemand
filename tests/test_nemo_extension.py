"""Tests the Nemo extension's logic with stand-in Nemo files and a fake daemon."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

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


class FakeWindow:
    """Stands in for a Nemo window: only needs the "destroy" signal."""

    def connect(self, signal, callback):
        self.on_destroy = callback

    def close(self):
        self.on_destroy(self)


@unittest.skipUnless(HAVE_NEMO, "Nemo's GObject introspection data isn't installed")
class ExtensionTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("onedrive_ondemand_nemo", SOURCE)
        self.ext = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ext)
        self.states = {}
        self.requests = []

        def fake_request(sock, cmd, timeout, **args):
            self.requests.append((sock, cmd, args))
            if cmd == "viewing":
                return {"ok": True}
            return {"ok": True, "states": {p: self.states.get(p, "online") for p in args["paths"]}}

        # mock.patch puts the real functions back after each test, so other
        # tests (which share these modules) aren't affected.
        for target, name, value in (
                (self.ext.ipc, "request", fake_request),
                (self.ext.config, "accounts", lambda: []),
                (self.ext.Nemo, "info_provider_update_complete_invoke",
                 lambda closure, provider, handle, result: self.completed.append(handle))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.completed = []
        self.e = self.ext.OneDriveExtension()
        # Two accounts, each with its own daemon socket.
        self.e.mounts = {"/home/me/OneDrive": "personal.sock", "/home/me/OneDrive - Work": "work.sock"}

    def test_batches_and_emblems(self):
        self.states = {"/home/me/OneDrive/a": "pinned", "/home/me/OneDrive/d": "none"}
        files = [FakeFile("/home/me/OneDrive/a"), FakeFile("/home/me/OneDrive/b"),
                 FakeFile("/home/me/OneDrive/d", is_dir=True), FakeFile("/etc/hostname")]
        results = [self.e.update_file_info_full(None, i, None, f) for i, f in enumerate(files)]
        self.assertEqual(results[3], Nemo.OperationResult.COMPLETE)  # not ours: answered at once
        self.e.answer_batch(None)
        self.assertEqual(len(self.requests), 1)                       # one request for all
        self.assertEqual(self.requests[0][2]["dirs"], ["/home/me/OneDrive/d"])
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
        patch = mock.patch.object(self.ext.ipc, "request", not_running)
        patch.start()
        self.addCleanup(patch.stop)
        f = FakeFile("/home/me/OneDrive/a")
        self.e.update_file_info_full(None, 0, None, f)
        self.e.answer_batch(None)
        self.assertEqual(f.emblems, [])
        self.assertEqual(self.completed, [0])

    def test_one_request_per_account(self):
        files = [FakeFile("/home/me/OneDrive/a"), FakeFile("/home/me/OneDrive - Work/b"),
                 FakeFile("/home/me/OneDrive - Workshop/c")]  # not an account folder
        for i, f in enumerate(files):
            self.e.update_file_info_full(None, i, None, f)
        self.e.answer_batch(None)
        asked = sorted((sock, args["paths"]) for sock, _cmd, args in self.requests)
        self.assertEqual(asked, [("personal.sock", ["/home/me/OneDrive/a"]),
                                 ("work.sock", ["/home/me/OneDrive - Work/b"])])
        self.assertEqual(files[2].emblems, [])

    def test_account_folder_itself_gets_no_emblem(self):
        self.assertIsNone(self.e.path_of(FakeFile("/home/me/OneDrive - Work", is_dir=True)))

    def test_tells_daemons_which_folders_are_shown(self):
        window = FakeWindow()
        self.assertIsNone(self.e.get_widget("file:///home/me/OneDrive%20-%20Work/photos", window))
        self.e.get_widget("file:///home/me/Music", FakeWindow())
        viewing = [(sock, args["folders"]) for sock, cmd, args in self.requests if cmd == "viewing"]
        self.assertEqual(viewing[-1], ("work.sock", ["/home/me/OneDrive - Work/photos"]))
        window.close()
        self.requests.clear()
        self.e.tell_viewing(force=True)
        self.assertEqual([r for r in self.requests if r[1] == "viewing"], [])

    def test_menu(self):
        self.states = {"/home/me/OneDrive/a": "pinned"}
        items = self.e.get_file_items(None, [FakeFile("/home/me/OneDrive/a")])
        labels = [i.get_property("label") for i in items[0].get_property("menu").get_items()]
        self.assertEqual(labels, ["Stop always keeping on this device", "Free up space", "View online"])
        self.assertEqual(self.e.get_file_items(None, [FakeFile("/tmp/x")]), [])


if __name__ == "__main__":
    unittest.main()
