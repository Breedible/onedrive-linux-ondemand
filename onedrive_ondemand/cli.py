"""The `onedrive-ondemand` command."""

import argparse
import os
import sys
from datetime import datetime

from onedrive_ondemand import __version__

STATE_NAMES = {
    "online": "online-only (not on this device)",
    "local": "locally available",
    "pinned": "always kept on this device",
    "syncing": "syncing",
    "error": "error",
    "none": "folder",
    "outside": "not inside the OneDrive folder",
}


def local_time(text):
    """The daemon logs in UTC; show your own clock time instead."""
    return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ask_daemon(cmd, **args):
    """Send a request to the daemon; print the error and exit if it fails."""
    from onedrive_ondemand import ipc
    try:
        answer = ipc.request(cmd, timeout=600, **args)
    except ipc.DaemonNotRunning as error:
        sys.exit(f"{error}\nStart it with:  systemctl --user start onedrive-ondemand-daemon")
    if not answer.get("ok"):
        sys.exit(f"Error: {answer.get('error')}")
    return answer


def show_info():
    from onedrive_ondemand.daemon import human
    from onedrive_ondemand.status import summarize
    info = ask_daemon("info")
    print(summarize(info)[1])
    print(f"  OneDrive folder:   {info['folder']}  ->  {info['mount_path']}")
    print(f"  Waiting to upload: {info['queued']} ({info['uploading']} uploading now)")
    print(f"  Pin downloads:     {info['pinning']} waiting")
    print(f"  Downloaded:        {human(info['cache_bytes'])} on disk "
          f"({human(info['disk_free'])} of {human(info['disk_total'])} free)")
    settings = info["settings"]
    days = settings["evict_after_days"]
    print(f"  Free unused files: {'never' if days == 0 else f'after {days} days'}, "
          f"keep {settings['min_free_space_percent']:g}% of the disk free")
    if info["pinned"]:
        print("  Always kept:       " + ", ".join(info["pinned"]))


def main():
    parser = argparse.ArgumentParser(
        prog="onedrive-ondemand",
        description="Files On-Demand style OneDrive client for Linux Mint.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("setup", help="sign in to OneDrive and start everything")
    p = commands.add_parser("status", help="overall status, or the state of the given files")
    p.add_argument("paths", nargs="*")
    p = commands.add_parser("pin", help="always keep files/folders on this device")
    p.add_argument("paths", nargs="+")
    p.add_argument("--force", action="store_true", help="pin even if it may not fit")
    p = commands.add_parser("unpin", help="stop always keeping files/folders")
    p.add_argument("paths", nargs="+")
    p = commands.add_parser("free", help="free up space: make files online-only")
    p.add_argument("paths", nargs="+")
    p = commands.add_parser("activity", help="recent uploads, downloads and problems")
    p.add_argument("-n", type=int, default=20, help="how many lines (default 20)")
    commands.add_parser("pause", help="pause uploads and pin downloads")
    commands.add_parser("resume", help="resume after pause")
    p = commands.add_parser("settings", help="show or change when files are freed")
    p.add_argument("--evict-after-days", type=int, metavar="DAYS",
                   help="free files not opened for this many days (0 = never)")
    p.add_argument("--min-free-space", type=float, metavar="PERCENT",
                   help="keep at least this much of the disk free")
    commands.add_parser("tray", help="show the tray icon")
    commands.add_parser("open", help="open the OneDrive folder (and start the tray icon)")
    # These are run by the systemd services, not by people.
    commands.add_parser("daemon", help="(used by the service) run the background daemon")
    commands.add_parser("mount", help="(used by the service) start rclone mount")
    commands.add_parser("mount-stopped", help="(used by the service) clean up after it stops")

    args = parser.parse_args()

    # Imports are inside the branches so each command only loads what it needs.
    if args.command == "setup":
        from onedrive_ondemand import setup_wizard
        setup_wizard.run()
    elif args.command == "mount":
        from onedrive_ondemand import mount
        mount.start()
    elif args.command == "mount-stopped":
        from onedrive_ondemand import mount
        mount.stopped()
    elif args.command == "open":
        import subprocess
        from onedrive_ondemand import config, setup_wizard
        setup_wizard.start_tray(sys.argv[0] if os.path.isabs(sys.argv[0]) else "onedrive-ondemand")
        subprocess.Popen(["xdg-open", str(config.mount_path(config.load_settings()))])
    elif args.command == "tray":
        from onedrive_ondemand import tray
        tray.main()
    elif args.command == "daemon":
        from onedrive_ondemand import daemon
        daemon.main()
    elif args.command == "status":
        if not args.paths:
            show_info()
            return
        paths = [os.path.abspath(p) for p in args.paths]
        states = ask_daemon("status", paths=paths)["states"]
        for path in paths:
            print(f"{STATE_NAMES.get(states[path], states[path]):35}  {path}")
    elif args.command in ("pin", "unpin", "free"):
        for path in args.paths:
            extra = {"force": True} if getattr(args, "force", False) else {}
            answer = ask_daemon(args.command, path=os.path.abspath(path), **extra)
            print(answer["message"])
            for line in answer.get("skipped", [])[:10]:
                print("  skipped: " + line)
    elif args.command == "activity":
        for entry in reversed(ask_daemon("activity", limit=args.n)["activity"]):
            print(f"{local_time(entry['time'])}  {entry['message']}")
    elif args.command in ("pause", "resume"):
        print(ask_daemon(args.command)["message"])
    elif args.command == "settings":
        changes = {}
        if args.evict_after_days is not None:
            changes["evict_after_days"] = args.evict_after_days
        if args.min_free_space is not None:
            changes["min_free_space_percent"] = args.min_free_space
        settings = ask_daemon("settings", set=changes)["settings"]
        days = settings["evict_after_days"]
        print(f"Free unused files: {'never' if days == 0 else f'after {days} days'}")
        print(f"Keep free:         {settings['min_free_space_percent']:g}% of the disk")


if __name__ == "__main__":
    main()
