"""A few direct calls to the Microsoft Graph API (OneDrive's web API).

rclone covers almost everything, but setup needs to look up your drive's ID
and the ID of the test folder. Milestone b also uses this module to find
pinned items after a rename (DESIGN.md §3), and to fetch the small preview
pictures OneDrive makes of photos (DESIGN.md §8).
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

GRAPH_URL = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    def __init__(self, status, message):
        super().__init__(f"OneDrive returned an error ({status}): {message}")
        self.status = status


def _request(access_token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        GRAPH_URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", {}).get("message", error.reason)
        except ValueError:
            message = error.reason
        raise GraphError(error.code, message) from None


def my_drive(access_token):
    """Your OneDrive's details. We need 'id' and 'driveType'."""
    return _request(access_token, "GET", "/me/drive")


def find_top_item(access_token, name):
    """The file or folder with this name at your OneDrive's top level, or None."""
    path = "/me/drive/root:/" + urllib.parse.quote(name)
    try:
        return _request(access_token, "GET", path)
    except GraphError as error:
        if error.status == 404:
            return None
        raise


def list_children(access_token, folder_id):
    """Names and IDs of the items in a folder (first 999; fine for the test folder)."""
    path = f"/me/drive/items/{folder_id}/children?$select=name,id&$top=999"
    return _request(access_token, "GET", path)["value"]


def root_item(access_token):
    """The top-level folder of your OneDrive."""
    return _request(access_token, "GET", "/me/drive/root")


def create_top_folder(access_token, name):
    """Create an empty folder at the top level of your OneDrive."""
    # Address the top folder by its real ID rather than the "root" shortcut:
    # on 2026-10-30 a personal account answered "Item not found" to
    # POST /me/drive/root/children.
    root_id = root_item(access_token)["id"]
    body = {
        "name": name,
        "folder": {},
        # Never replace or rename something that already exists.
        "@microsoft.graph.conflictBehavior": "fail",
    }
    return _request(access_token, "POST", f"/me/drive/items/{root_id}/children", body)


def diagnose(access_token):
    """Try a few read-only requests and report which ones work.

    Only status codes and error messages are returned, never the token.
    """
    results = []
    for path in ("/me/drive", "/me/drive/root", "/me/drive/root/children?$top=1"):
        try:
            _request(access_token, "GET", path)
            results.append(f"GET {path}: OK")
        except GraphError as error:
            results.append(f"GET {path}: {error}")
    return results


def item_by_id(access_token, item_id):
    """Look an item up by its permanent ID. None if it's gone (deleted or never existed)."""
    path = f"/me/drive/items/{urllib.parse.quote(item_id)}?$select=id,name,parentReference,deleted,folder,webUrl"
    try:
        item = _request(access_token, "GET", path)
    except GraphError as error:
        if error.status == 404:
            return None
        raise
    return None if "deleted" in item else item


def path_in_folder(item, remote_folder):
    """Where an item is, relative to the folder this client shows.

    Returns e.g. "Projects/notes/notes.docx", "" for the shown folder itself,
    or None if the item is somewhere outside it.
    """
    parent = item.get("parentReference", {}).get("path")
    if parent is None or "root:" not in parent:
        return None
    # parent looks like "/drive/root:/LinuxClientTest/Projects" (personal) or
    # "/drives/<id>/root:/..." (business). Checked 2026-10-30: names with
    # spaces come back as-is, not percent-encoded, so we don't decode.
    parent = parent.split("root:", 1)[1].strip("/")
    full = f"{parent}/{item['name']}".strip("/")
    if not remote_folder:
        return full
    if full == remote_folder:
        return ""
    if full.startswith(remote_folder + "/"):
        return full[len(remote_folder) + 1:]
    return None


def reachable(timeout=5):
    """True if this computer can open a connection to Microsoft's servers.

    A quick check (a few seconds at most) used to tell "offline" from "online".
    It only opens and closes a connection; nothing is sent.
    """
    for host in ("graph.microsoft.com", "login.microsoftonline.com"):
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def thumbnail(access_token, drive_id, path, size="medium"):
    """OneDrive's own small preview picture of a file, as JPEG bytes, or None.

    `path` is the file's full path in the drive, e.g. "LinuxClientTest/a.jpg".
    OneDrive makes these previews on its servers, so the file itself is never
    downloaded. "medium" fits in 176x176 pixels (DESIGN.md §8).
    Returns None if OneDrive has no preview for this kind of file.
    """
    quoted = urllib.parse.quote(path.strip("/"))
    try:
        info = _request(access_token, "GET",
                        f"/drives/{urllib.parse.quote(drive_id)}/root:/{quoted}:/thumbnails/0/{size}")
    except GraphError as error:
        if error.status in (400, 404):
            return None
        raise
    url = info.get("url")
    if not url:
        return None
    # The address is a ready-signed link: it must NOT get our sign-in token,
    # because it points at a different server.
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read(5 * 1024 * 1024)  # previews are small; 5 MB is a hard cap
    except urllib.error.URLError as error:
        raise GraphError(0, f"couldn't fetch the preview ({error})") from None
