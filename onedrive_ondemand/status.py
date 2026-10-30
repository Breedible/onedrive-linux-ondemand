"""One short summary of the daemon's "info" answer, shared by the CLI and tray."""

# Icon names (data/icons/.../status/onedrive-ondemand-<key>.svg)
OK, SYNCING, PAUSED, OFFLINE, ERROR = "ok", "syncing", "paused", "offline", "error"


def summarize(info):
    """Return (icon key, one line of text) for the daemon's info dict, or for None."""
    if info is None:
        return ERROR, "The OneDrive On-Demand daemon isn't running."
    if not info.get("mounted"):
        return ERROR, "OneDrive isn't mounted."
    if info.get("signin_needed"):
        return ERROR, "Sign in again: OneDrive no longer accepts the saved sign-in."
    if info.get("paused"):
        waiting = info.get("queued", 0)
        return PAUSED, f"Paused ({waiting} upload(s) waiting)" if waiting else "Paused"
    if info.get("online") is False:
        return OFFLINE, "Offline: downloaded files still work, uploads wait."
    if info.get("upload_errors") or info.get("pin_errors"):
        problems = info.get("upload_errors", 0) + info.get("pin_errors", 0)
        return ERROR, f"{problems} problem(s): see Recent activity"
    if info.get("queued") or info.get("pinning"):
        parts = []
        if info.get("queued"):
            parts.append(f"{info['queued']} upload(s)")
        if info.get("pinning"):
            parts.append(f"{info['pinning']} download(s)")
        return SYNCING, "Syncing: " + ", ".join(parts)
    return OK, "Up to date"
