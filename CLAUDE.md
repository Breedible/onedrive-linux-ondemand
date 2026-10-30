# Notes for AI coding assistants (and human contributors)

## Project
A Files On-Demand style OneDrive client for Linux Mint (Cinnamon / Nemo),
built on `rclone mount --vfs-cache-mode full`. Read these first:

- `docs/RESEARCH.md`: what rclone can and can't do, and why we didn't fork
  another project.
- `docs/DESIGN.md`: architecture, pin storage, eviction, failure modes,
  milestones. **This design is approved; follow it.** If something in it
  turns out to be wrong when tested, explain why and propose a change before
  building around it.

> **If `HANDOFF.md` exists in this repo, read it and do its tasks first.**

## Current status
- Design approved 2026-10-30. **No code yet.**
- Next: **milestone (a)**: `setup` (browser sign-in, our own rclone config
  restricted to the `LinuxClientTest` folder) and the mount running as a
  `systemd --user` service. Also verify every item marked *(verify)* in the
  docs, and record the results in `docs/RESEARCH.md`.
- Milestones run in order a → f. After each one: explain in plain language
  how to test it and what is still missing, then commit.

## How to work on this repo
- The maintainer is a **beginner coder**. Keep code simple and readable,
  comment the non-obvious parts, and avoid clever abstractions.
- Say plainly when the maintainer has to do something themselves (sign in
  in the browser, approve permissions, enter a password for `sudo`).
- Python 3.10+ standard library plus PyGObject (`gi`) only. No pip packages.
- Line endings are LF (enforced by `.gitattributes`).
- Ask before anything outward-facing (pushing, publishing releases).

## Safety rules (non-negotiable)
1. During development, rclone may only see the `LinuxClientTest` folder
   (set as the remote root). Never point it at the whole drive except with
   `--read-only`.
2. Never run `rclone sync`, `bisync`, `delete`, `purge`, `cleanup`, or use
   `--onedrive-hard-delete`.
3. Test cache, eviction and pin logic against a fake cache folder in unit
   tests before it touches a real mount.
4. Never commit `rclone.conf`, tokens, or anything from
   `~/.config/onedrive-ondemand/`.
