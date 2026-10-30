"""How the CLI, Nemo extension and tray icon talk to the daemon.

Each account's daemon listens on its own unix socket, in
/run/user/<uid>/onedrive-ondemand/<account>/ (a folder only you can open).
One JSON message per line; each connection carries one request and one answer:

    -> {"cmd": "status", "paths": ["/home/me/OneDrive/a.txt"]}
    <- {"ok": true, "states": {"/home/me/OneDrive/a.txt": "local"}}

Errors come back as {"ok": false, "error": "what went wrong"}.
"""

import json
import os
import socket
import socketserver
import threading

MAX_MESSAGE = 16 * 1024 * 1024  # 16 MB is plenty, even for thousands of paths


class DaemonNotRunning(Exception):
    pass


def request(socket_path, cmd, timeout=10, **args):
    """Send one request to a daemon (Account.daemon_socket) and return its answer (a dict).

    Raises DaemonNotRunning if nothing is listening.
    """
    message = json.dumps({"cmd": cmd, **args}).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(message)
            answer = _read_line(sock)
    except (FileNotFoundError, ConnectionRefusedError):
        raise DaemonNotRunning("The OneDrive On-Demand daemon isn't running.") from None
    except socket.timeout:
        raise DaemonNotRunning("The OneDrive On-Demand daemon didn't answer in time.") from None
    return json.loads(answer)


def _read_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_MESSAGE:
            raise ValueError("message too long")
    return data


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(MAX_MESSAGE)
        try:
            message = json.loads(line)
            answer = self.server.handle_message(message)
        except Exception as error:  # never let one bad request kill the server
            answer = {"ok": False, "error": str(error)}
        try:
            self.wfile.write(json.dumps(answer).encode() + b"\n")
        except BrokenPipeError:
            pass  # the asker gave up waiting (e.g. Nemo's short timeout)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(handle_message, socket_path):
    """Start answering requests in a background thread. Returns the server.

    `handle_message(dict) -> dict` is called for each request.
    """
    socket_path = str(socket_path)
    os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
    if os.path.exists(socket_path):
        os.unlink(socket_path)  # left behind by a crash
    server = _Server(socket_path, _Handler)
    os.chmod(socket_path, 0o600)
    server.handle_message = handle_message
    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    return server
