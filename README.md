# onedrive-linux-ondemand

A Linux desktop client for Microsoft OneDrive that aims to behave like the
Windows client's **Files On-Demand**, built for Linux Mint (Cinnamon / Nemo).

> **Status: early design stage. Nothing here is usable yet.**
> Do not point this at OneDrive data you care about.

## Goals

- One folder, `~/OneDrive`, showing all your OneDrive files.
- Three per-file states, switchable from the Nemo right-click menu:
  - **Online-only** – visible, uses no disk space, downloads when opened.
  - **Locally available** – downloaded because you opened it; can be freed
    automatically when space is needed or after a time you choose.
  - **Always keep on this device** – pinned; never freed automatically.
- Local edits upload automatically; remote changes appear locally.
- Status emblems in Nemo, a tray icon, start at login.

Linux has no equivalent of Windows "placeholder files", so some of this
behaves differently. See [docs/DESIGN.md](docs/DESIGN.md) for the plan and
its honest limitations once it lands.

## License

[GPL-3.0](LICENSE)
