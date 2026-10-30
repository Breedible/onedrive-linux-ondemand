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
    "outside": "not inside a OneDrive folder",
}


def local_time(text):
    """The daemon logs in UTC; show your own clock time instead."""
    return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ask_daemon(account, cmd, **args):
    """Send a request to an account's daemon; print the error and exit if it fails."""
    from onedrive_ondemand import ipc
    try:
        answer = ipc.request(account.daemon_socket, cmd, timeout=600, **args)
    except ipc.DaemonNotRunning as error:
        sys.exit(f"{account.display_name()}: {error}\n"
                 f"Start it with:  systemctl --user start {account.daemon_service}")
    if not answer.get("ok"):
        sys.exit(f"Error: {answer.get('error')}")
    return answer


def chosen_accounts(args):
    """The accounts a command is about: the one given with --account, or all of them."""
    from onedrive_ondemand import config
    accounts = config.accounts()
    if not accounts:
        sys.exit("No OneDrive account is set up yet. Run:  onedrive-ondemand setup")
    if args.account:
        account = config.find_account(args.account, accounts)
        if account is None:
            names = ", ".join(a.settings()["name"] for a in accounts)
            sys.exit(f"There's no account called {args.account!r}. Accounts: {names}")
        return [account]
    return accounts


def account_of(path, accounts):
    from onedrive_ondemand import config
    account = config.account_for_path(path, accounts)
    if account is None:
        sys.exit(f"{path} isn't inside a OneDrive folder.")
    return account


def show_info(account):
    from onedrive_ondemand.daemon import human
    from onedrive_ondemand.status import summarize
    info = ask_daemon(account, "info")
    print(f"{info['name']}: {summarize(info)[1]}")
    print(f"  OneDrive folder:   {info['folder']}  ->  {info['mount_path']}")
    print(f"  Waiting to upload: {info['queued']} ({info['uploading']} uploading now)")
    print(f"  Pin downloads:     {info['pinning']} waiting")
    for t in info.get("transfers", []):
        arrow = "↑" if t["direction"] == "upload" else "↓"
        print(f"  {arrow} {t['name']}  {t['percentage']}% of {human(t['size'])}")
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
    parser.add_argument("--account", metavar="NAME",
                        help="only this account (its name, e.g. Personal or Work)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("setup", help="install everything and add your first account")
    commands.add_parser("add-account", help="sign in to another OneDrive account (e.g. work)")
    p = commands.add_parser("remove-account", help="sign out of an account and remove it here")
    p.add_argument("name", help="the account's name, e.g. Work")
    commands.add_parser("accounts", help="list the accounts that are set up")
    p = commands.add_parser("sign-in", help="sign in again to an account")
    p.add_argument("name", nargs="?", help="the account's name (needed if there are several)")
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
    p = commands.add_parser("update", help="check GitHub for a newer version")
    p.add_argument("--install", action="store_true", help="install it if there is one")
    commands.add_parser("tray", help="show the tray icon")
    commands.add_parser("open", help="open the OneDrive folders (and start the tray icon)")
    commands.add_parser("uninstall", help="stop everything and undo setup (keeps your files)")
    # These are run by the systemd services and Nemo, not by people.
    p = commands.add_parser("daemon", help="(used by the service) run one account's daemon")
    p.add_argument("account_id")
    p = commands.add_parser("mount", help="(used by the service) start one account's mount")
    p.add_argument("account_id")
    p = commands.add_parser("mount-stopped", help="(used by the service) clean up after it stops")
    p.add_argument("account_id")
    p = commands.add_parser("thumbnail", help="(used by Nemo) make a picture preview")
    p.add_argument("uri")
    p.add_argument("output")
    p.add_argument("size", type=int)

    args = parser.parse_args()

    # Imports are inside the branches so each command only loads what it needs.
    if args.command == "setup":
        from onedrive_ondemand import setup_wizard
        setup_wizard.run()
    elif args.command == "add-account":
        from onedrive_ondemand import setup_wizard
        setup_wizard.check_requirements()
        setup_wizard.install_shared_files()
        setup_wizard.add_account()
        setup_wizard.restart_tray()
    elif args.command == "remove-account":
        from onedrive_ondemand import config, setup_wizard
        account = config.find_account(args.name)
        if account is None:
            sys.exit(f"There's no account called {args.name!r}.")
        setup_wizard.remove_account(account)
    elif args.command == "accounts":
        from onedrive_ondemand import config
        for account in config.accounts():
            print(f"{account.display_name():30}  {account.mount_path()}")
        if not config.accounts():
            print("No account is set up yet. Run:  onedrive-ondemand setup")
    elif args.command == "sign-in":
        from onedrive_ondemand import setup_wizard
        args.account = args.name or args.account
        accounts = chosen_accounts(args)
        if len(accounts) > 1:
            sys.exit("Say which account, e.g.:  onedrive-ondemand sign-in Work")
        setup_wizard.sign_in_again(accounts[0])
        print(f"Signed in again to {accounts[0].display_name()}.")
    elif args.command == "thumbnail":
        from onedrive_ondemand import thumbnailer
        thumbnailer.main(args.uri, args.output, args.size)
    elif args.command == "mount":
        from onedrive_ondemand import mount
        mount.start(args.account_id)
    elif args.command == "mount-stopped":
        from onedrive_ondemand import mount
        mount.stopped(args.account_id)
    elif args.command == "open":
        import subprocess
        from onedrive_ondemand import config, setup_wizard
        accounts = config.accounts()
        if not accounts:
            # Not set up yet (e.g. just installed the .deb): run setup in a terminal.
            setup_wizard.run_in_terminal("setup")
            return
        setup_wizard.start_tray()
        for account in accounts:
            subprocess.Popen(["xdg-open", str(account.mount_path())])
    elif args.command == "uninstall":
        from onedrive_ondemand import setup_wizard
        setup_wizard.uninstall()
    elif args.command == "tray":
        from onedrive_ondemand import tray
        tray.main()
    elif args.command == "daemon":
        from onedrive_ondemand import daemon
        daemon.main(args.account_id)
    elif args.command == "update":
        from onedrive_ondemand import updates
        try:
            update = updates.check()
            print(update["summary"])
            if update["available"] and args.install:
                updates.install(update)
                print("Updated. The services were restarted.")
        except updates.UpdateError as error:
            sys.exit(str(error))
    elif args.command == "status":
        accounts = chosen_accounts(args)
        if not args.paths:
            for account in accounts:
                show_info(account)
            return
        for path in (os.path.abspath(p) for p in args.paths):
            states = ask_daemon(account_of(path, accounts), "status", paths=[path])["states"]
            print(f"{STATE_NAMES.get(states[path], states[path]):35}  {path}")
    elif args.command in ("pin", "unpin", "free"):
        accounts = chosen_accounts(args)
        for path in (os.path.abspath(p) for p in args.paths):
            extra = {"force": True} if getattr(args, "force", False) else {}
            answer = ask_daemon(account_of(path, accounts), args.command, path=path, **extra)
            print(answer["message"])
            for line in answer.get("skipped", [])[:10]:
                print("  skipped: " + line)
    elif args.command == "activity":
        entries = []
        for account in chosen_accounts(args):
            name = account.settings()["name"]
            for entry in ask_daemon(account, "activity", limit=args.n)["activity"]:
                entries.append((entry["time"], name, entry["message"]))
        for when, name, message in sorted(entries)[-args.n:]:
            print(f"{local_time(when)}  [{name}] {message}")
    elif args.command in ("pause", "resume"):
        for account in chosen_accounts(args):
            print(f"{account.display_name()}: {ask_daemon(account, args.command)['message']}")
    elif args.command == "settings":
        changes = {}
        if args.evict_after_days is not None:
            changes["evict_after_days"] = args.evict_after_days
        if args.min_free_space is not None:
            changes["min_free_space_percent"] = args.min_free_space
        for account in chosen_accounts(args):
            settings = ask_daemon(account, "settings", set=changes)["settings"]
        days = settings["evict_after_days"]
        print(f"Free unused files: {'never' if days == 0 else f'after {days} days'}")
        print(f"Keep free:         {settings['min_free_space_percent']:g}% of the disk")


if __name__ == "__main__":
    main()
