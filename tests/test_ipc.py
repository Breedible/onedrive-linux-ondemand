import tempfile
import unittest
from pathlib import Path

from onedrive_ondemand import ipc


class IpcTest(unittest.TestCase):
    def test_round_trip_and_errors(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            sock = Path(folder) / "d.sock"

            def handle(message):
                if message["cmd"] == "boom":
                    raise RuntimeError("broken")
                return {"ok": True, "echo": message["value"]}

            server = ipc.serve(handle, sock)
            try:
                self.assertEqual(ipc.request("echo", sock, value="héllo")["echo"], "héllo")
                answer = ipc.request("boom", sock)
                self.assertFalse(answer["ok"])
                self.assertIn("broken", answer["error"])
            finally:
                server.shutdown()
                server.server_close()

    def test_not_running(self):
        with self.assertRaises(ipc.DaemonNotRunning):
            ipc.request("info", Path("/tmp/does-not-exist.sock"))


if __name__ == "__main__":
    unittest.main()
