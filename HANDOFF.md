# Handoff: Windows planning session → Linux Mint session

Written 2026-10-30 by the Claude Code session that did the research and
design on the maintainer's Windows install. You are the Claude Code session
on the maintainer's **Linux Mint (Cinnamon)** install, same machine,
dual-boot. Do the tasks below **in order**. Talk to the maintainer in plain
language: they're a beginner coder.

## Two files called CLAUDE.md: don't mix them up

| Name used below | Where | What goes in it |
|---|---|---|
| **PC CLAUDE.md** | Already exists on this Linux install. The maintainer had an earlier session create it to record what was changed on the computer. Find it: check `~/.claude/CLAUDE.md` first, then `~/CLAUDE.md`, then search the home folder. If you can't find it, ask. | **Only general changes made to this PC** (packages installed or removed, mounts created or removed, services enabled, config files changed). |
| **repo CLAUDE.md** | `CLAUDE.md` in this repository | Project notes for this repo (status, design, coding rules). |

## Task 1: Read the PC CLAUDE.md and add the maintainer's rule

1. Read the PC CLAUDE.md completely. It may already describe how the old
   OneDrive mount (Task 2) was set up.
2. Add this rule to it, in the maintainer's words, near the top:

   > **Rule:** Only write into this file information about general work done
   > on this PC (for example: "unmounted OneDrive", "installed rclone").
   > Do not go into detail about project work, such as building the
   > onedrive-linux-ondemand repo. Every Claude Code session must follow
   > this rule.

3. From now on, record every PC-level change you make in it. For example:
   packages installed, the old mount removed, the new mount's systemd
   service enabled. **No** project details: no milestones, design decisions
   or code descriptions. Those go in the repo.

## Task 2: Find and completely remove the old OneDrive mount

The maintainer previously connected OneDrive to the file manager "through
some inferior way" and doesn't remember how. Our new client replaces it.

**Find out how it was done.** Check, for example:
- the PC CLAUDE.md;
- `mount | grep -i -E 'onedrive|rclone|fuse'` and `findmnt -t fuse,fuse3,fuse.rclone`;
- `rclone listremotes` and `~/.config/rclone/rclone.conf`;
- `systemctl --user list-units --all | grep -i -E 'onedrive|rclone'`, and `systemctl list-units` for system-wide ones;
- `~/.config/systemd/user/`, `~/.config/autostart/`, `/etc/fstab`, `crontab -l`;
- installed packages: `dpkg -l | grep -i -E 'onedrive|rclone|insync'`, `flatpak list`, `snap list`;
- other clients: `~/.config/onedrive/` (abraunegg), `~/.config/onedriver/`;
- Nemo sidebar bookmarks: `~/.config/gtk-3.0/bookmarks`.

Tell the maintainer what you found **before** removing anything.

**Remove it SAFELY. Deleting the wrong thing here deletes cloud files.**
1. **Never** run `rm` (or let anything delete files) inside a folder that is
   still mounted or still being synced. Deleting files there deletes them
   **from OneDrive**.
2. If it's an rclone mount with a VFS cache, first make sure no uploads are
   pending (check `vfs/queue` if rc is on, or the logs). Unsaved edits live
   only in the cache.
3. If it's a sync client (local copy of files), let it finish syncing, then
   stop and disable it **before** touching the folder.
4. Order: stop/disable the service or autostart → unmount
   (`fusermount -u <path>`) → only then remove config, units, bookmarks and
   packages.
5. If an old local folder still contains real files after unmounting,
   **don't delete it.** Move it to e.g. `~/OneDrive-old-backup` and ask the
   maintainer what to do with it.
6. Removing an rclone remote (`rclone config delete <name>`) only forgets
   the login. It doesn't touch cloud data. Remove only OneDrive-related
   remotes and leave any others.
7. Afterwards `~/OneDrive` must either not exist or be an empty, unmounted
   folder, ready for the new client.
8. Record what you removed in the PC CLAUDE.md (general terms only).

## Task 3: Set up the tools (you run the commands, not the maintainer)

The maintainer asked that **you run these yourself**. Claude Code is already
installed as the desktop app, so skip installing it.

- If `sudo` asks for a password, the maintainer has to type it. Use a
  terminal they can see, or ask them to run just that one command.
  **Never** ask them to paste a password into the chat.

1. `cat /etc/linuxmint/info` to confirm the Mint version (expected 22.x).
2. `sudo apt install git gh`, skipping either if already installed.
3. `gh auth login --web --git-protocol https`. The maintainer completes the
   browser step and signs in as GitHub user **Breedible**. This is needed
   so you can push.
4. If the repo isn't cloned yet: clone it to
   `~/Code/onedrive-linux-ondemand`.
5. In the repo, set the commit identity to the GitHub no-reply address, so
   the maintainer's personal email stays out of the public history:
   ```bash
   git config user.name "Breedible"
   git config user.email "312606118+Breedible@users.noreply.github.com"
   ```
6. Record installed packages in the PC CLAUDE.md.

## Task 4: Start the project

1. Read the repo's `CLAUDE.md`, `docs/DESIGN.md` and `docs/RESEARCH.md`.
   The design is **approved**; follow it.
2. Start **milestone (a)**: install rclone ≥ 1.75 from rclone.org (Mint's
   apt version is too old), then `fuse3`. Build `setup` (Microsoft sign-in
   in the browser, our own rclone config restricted to the
   `LinuxClientTest` folder) and the mount as a `systemd --user` service.
3. Verify each item marked *(verify)* in the docs and record the results in
   `docs/RESEARCH.md`.
4. Tell the maintainer how to test milestone (a) and what's still missing.
   Ask before pushing.

## Things the maintainer has decided (don't re-ask)

- Open source, GPL-3.0, public repo `Breedible/onedrive-linux-ondemand`.
- Build fresh on rclone. Don't fork abraunegg/onedrive, onedriver or OneDriveUI.
- No spare Microsoft account. Develop against a `LinuxClientTest` folder in
  their real OneDrive, using the safety rules in the repo CLAUDE.md.
- Defaults: free unused files after 30 days, keep 10 % of the disk free.
- **Pins must follow renames and moves** (DESIGN.md §3), built in milestone (b).
- Nemo thumbnails: a one-time setup question (DESIGN.md §8).
- No Flatpak. Ship a `.deb`.

## When done

When Tasks 1–3 are finished, tell the maintainer this file can be
deleted from the repo (it's in the public git history anyway, and it
contains nothing secret).
