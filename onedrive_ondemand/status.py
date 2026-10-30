"""Short status summaries of the daemons' "info" answers, shared by the CLI and tray."""

# Icon names (data/icons/.../status/onedrive-ondemand-<key>.svg)
OK, SYNCING, PAUSED, OFFLINE, ERROR = "ok", "syncing", "paused", "offline", "error"
# When several accounts are set up, the tray icon shows the most important state.
IMPORTANCE = [ERROR, OFFLINE, PAUSED, SYNCING, OK]


def summarize(info):
    """Return (icon key, one line of text) for one daemon's info dict, or for None."""
    if info is None:
        return ERROR, "Not running."
    if not info.get("mounted"):
        return ERROR, "Not connected (the mount isn't running)."
    if info.get("signin_needed"):
        return ERROR, "Sign in again: OneDrive no longer accepts the saved sign-in."
    if info.get("offline_mode"):
        if info.get("waiting_to_reconnect"):
            return OFFLINE, "Back online: reconnects once no file from it is open."
        return OFFLINE, "Offline: only downloaded files are shown, read-only."
    if info.get("paused"):
        waiting = info.get("queued", 0)
        return PAUSED, f"Paused ({waiting} upload(s) waiting)" if waiting else "Paused"
    if info.get("online") is False:
        return OFFLINE, "Offline: downloaded files still work, uploads wait."
    if info.get("upload_errors") or info.get("pin_errors"):
        problems = info.get("upload_errors", 0) + info.get("pin_errors", 0)
        return ERROR, f"{problems} problem(s): see Recent activity"
    transfers = info.get("transfers") or []
    downloads = sum(1 for t in transfers if t["direction"] == "download")
    # Pin downloads that are queued but haven't started don't show as transfers yet.
    downloads = max(downloads, info.get("pinning", 0))
    if info.get("queued") or downloads:
        parts = []
        if info.get("queued"):
            parts.append(f"uploading {info['queued']} file(s)")
        if downloads:
            parts.append(f"downloading {downloads} file(s)")
        text = ", ".join(parts)
        return SYNCING, text[0].upper() + text[1:]
    return OK, "Up to date"


def combine(results):
    """The tray icon for several accounts: [(icon key, text), ...] -> the most important key."""
    keys = [key for key, _text in results] or [ERROR]
    return min(keys, key=IMPORTANCE.index)
