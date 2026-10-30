"""The `onedrive-ondemand` command."""

import argparse

from onedrive_ondemand import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="onedrive-ondemand",
        description="Files On-Demand style OneDrive client for Linux Mint.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("setup", help="sign in to OneDrive and start the mount")
    # These two are run by the systemd service, not by people.
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


if __name__ == "__main__":
    main()
