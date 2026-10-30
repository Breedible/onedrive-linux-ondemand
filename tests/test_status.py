import unittest
from unittest import mock

from onedrive_ondemand import daemon, status, updates
from tests.fakehome import FakeHome

BASE = {"mounted": True, "online": True, "queued": 0, "pinning": 0, "transfers": []}


def info(**changes):
    return dict(BASE, **changes)


class SummaryTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(status.summarize(info()), (status.OK, "Up to date"))
        self.assertEqual(status.summarize(None)[0], status.ERROR)
        self.assertEqual(status.summarize(info(mounted=False))[0], status.ERROR)
        self.assertEqual(status.summarize(info(online=False))[0], status.OFFLINE)
        self.assertEqual(status.summarize(info(offline_mode=True))[0], status.OFFLINE)
        self.assertEqual(status.summarize(info(paused=True, queued=2)),
                         (status.PAUSED, "Paused (2 upload(s) waiting)"))

    def test_downloads_count_as_syncing(self):
        down = {"name": "a.jpg", "direction": "download", "size": 10, "bytes": 5, "percentage": 50}
        self.assertEqual(status.summarize(info(transfers=[down])),
                         (status.SYNCING, "Downloading 1 file(s)"))
        self.assertEqual(status.summarize(info(queued=1, pinning=3)),
                         (status.SYNCING, "Uploading 1 file(s), downloading 3 file(s)"))

    def test_combine_picks_the_most_important(self):
        self.assertEqual(status.combine([(status.OK, ""), (status.SYNCING, "")]), status.SYNCING)
        self.assertEqual(status.combine([(status.OFFLINE, ""), (status.ERROR, "")]), status.ERROR)
        self.assertEqual(status.combine([]), status.ERROR)


class TransfersTest(unittest.TestCase):
    def test_direction(self):
        stats = {"transferring": [
            {"name": "up.txt", "size": 100, "bytes": 50, "percentage": 50, "speedAvg": 10, "eta": 5},
            {"name": "down.jpg", "size": 200, "bytes": 20, "percentage": 10, "speed": 7, "eta": None},
            {"name": "queued-but-downloading.txt", "size": 1, "bytes": 0, "percentage": 0}]}
        queue = {"up.txt": {"uploading": True}, "queued-but-downloading.txt": {"uploading": False}}
        result = daemon.transfers_from_stats(stats, queue)
        self.assertEqual([t["direction"] for t in result], ["upload", "download", "download"])
        self.assertEqual(result[0]["speed"], 10)
        self.assertEqual(result[1]["speed"], 7)

    def test_nothing_transferring(self):
        self.assertEqual(daemon.transfers_from_stats({}, {}), [])
        self.assertEqual(daemon.transfers_from_stats({"transferring": None}, {}), [])


class DaemonActivityTest(unittest.TestCase):
    def setUp(self):
        self.home = FakeHome()
        self.account = self.home.add_account("personal", "Personal")
        self.daemon = daemon.Daemon(self.account)

    def tearDown(self):
        self.daemon.store.close()
        self.home.cleanup()

    def kinds(self):
        return [e["kind"] for e in self.daemon.store.recent(50)]

    def test_offline_download_is_explained_once(self):
        self.daemon.online = False
        down = [{"name": "a.jpg", "direction": "download"}]
        self.daemon.note_downloads(set(), down)
        self.daemon.note_downloads({"a.jpg"}, down)
        self.assertEqual(self.kinds(), ["offline"])

    def test_finished_download_is_logged(self):
        self.daemon.online = True
        sidecar = self.daemon.cache.sidecar_file("a.jpg")
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text('{"Size": 5, "Rs": [{"Pos": 0, "Size": 5}]}')
        self.daemon.note_downloads({"a.jpg"}, [])
        self.assertEqual(self.kinds(), ["downloaded"])

    def test_going_offline_and_back(self):
        self.daemon.set_online(True)
        self.daemon.set_online(False)
        self.daemon.set_online(True)
        self.assertEqual(self.kinds(), ["online", "offline"])  # newest first

    def test_settings_keep_changes_made_by_others(self):
        # Setup renames the account while the daemon runs; a later pause
        # must not write the old name back.
        settings = self.account.settings()
        settings["name"] = "Home"
        self.account.save_settings(settings)
        self.daemon.handle({"cmd": "settings", "set": {"evict_after_days": 3}})
        saved = self.account.settings()
        self.assertEqual((saved["name"], saved["evict_after_days"]), ("Home", 3))

    def test_offline_mode_refuses_free(self):
        self.account.runtime_dir.mkdir(parents=True)
        self.account.mode_file.write_text("offline")
        answer = self.daemon.handle({"cmd": "free", "path": str(self.account.mount_path() / "a")})
        self.assertFalse(answer["ok"])
        self.assertIn("offline", answer["error"])


class ReconnectTest(unittest.TestCase):
    """Started offline, now back online: switch to the normal mount when it's safe."""

    def setUp(self):
        self.home = FakeHome()
        self.account = self.home.add_account("personal", "Personal")
        self.account.runtime_dir.mkdir(parents=True)
        self.account.mode_file.write_text("offline")
        self.daemon = daemon.Daemon(self.account)
        self.daemon.online = True
        self.daemon.last_online_check = daemon.time.monotonic()

    def tearDown(self):
        self.daemon.store.close()
        self.home.cleanup()

    def test_restarts_the_mount_when_no_file_is_open(self):
        with mock.patch.object(daemon.graph, "reachable", return_value=True), \
                mock.patch.object(daemon.cachemod, "open_files", return_value=set()), \
                mock.patch.object(daemon.subprocess, "run") as run:
            self.daemon.online_loop()
        self.assertEqual(run.call_args[0][0],
                         ["systemctl", "--user", "restart", "onedrive-ondemand-mount@personal.service"])

    def test_waits_while_a_file_is_open(self):
        with mock.patch.object(daemon.graph, "reachable", return_value=True), \
                mock.patch.object(daemon.cachemod, "open_files", return_value={"a.txt"}), \
                mock.patch.object(daemon.subprocess, "run") as run:
            self.daemon.online_loop()
        run.assert_not_called()
        self.assertTrue(self.daemon.cmd_info({})["waiting_to_reconnect"])

    def test_still_offline_does_nothing(self):
        with mock.patch.object(daemon.graph, "reachable", return_value=False), \
                mock.patch.object(daemon.subprocess, "run") as run:
            self.daemon.online_loop()
        run.assert_not_called()
        self.assertFalse(self.daemon.online)


class UpdatesTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updates.parse_version("v0.2.1"), (0, 2, 1))
        self.assertIsNone(updates.parse_version("latest"))
        self.assertTrue(updates.is_newer("v0.10.0", "0.9.9"))
        self.assertFalse(updates.is_newer("0.2.0", "0.2.0"))
        self.assertFalse(updates.is_newer("nonsense", "0.2.0"))

    def test_no_release_yet(self):
        error = updates.urllib.error.HTTPError(updates.RELEASES_URL, 404, "Not Found", {}, None)
        with mock.patch.object(updates.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(updates.latest_release())

    def test_deb_check(self):
        release = {"version": "9.0.0", "deb_url": "https://x/y_all.deb", "page": "", "notes": ""}
        with mock.patch.object(updates, "source_dir", return_value=None), \
                mock.patch.object(updates, "latest_release", return_value=release):
            found = updates.check()
        self.assertTrue(found["available"])
        self.assertEqual(found["kind"], "deb")
        old = dict(release, version="0.0.1")
        with mock.patch.object(updates, "source_dir", return_value=None), \
                mock.patch.object(updates, "latest_release", return_value=old):
            self.assertFalse(updates.check()["available"])


if __name__ == "__main__":
    unittest.main()
