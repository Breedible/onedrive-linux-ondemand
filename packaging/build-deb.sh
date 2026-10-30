#!/bin/sh
# Builds dist/onedrive-ondemand_<version>_all.deb from this source folder.
# Usage:  ./packaging/build-deb.sh
# Needs only dpkg-deb (always on Mint). No root needed.
set -eu

cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import onedrive_ondemand; print(onedrive_ondemand.__version__)")
PKG=onedrive-ondemand
ROOT=build/deb
rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" dist

# The Python package (no tests, no __pycache__).
PYDIR="$ROOT/usr/lib/python3/dist-packages/onedrive_ondemand"
mkdir -p "$PYDIR"
cp onedrive_ondemand/*.py "$PYDIR/"

# The command.
mkdir -p "$ROOT/usr/bin"
cat > "$ROOT/usr/bin/onedrive-ondemand" <<'SH'
#!/bin/sh
exec python3 -m onedrive_ondemand "$@"
SH
chmod 755 "$ROOT/usr/bin/onedrive-ondemand"

# systemd user service templates (one copy runs per account): installed for
# every user, started by `setup`.
mkdir -p "$ROOT/usr/lib/systemd/user"
for unit in onedrive-ondemand-mount@.service onedrive-ondemand-daemon@.service; do
    # No PYTHONPATH needed: the package is in Python's normal place.
    sed -e '/^Environment=PYTHONPATH=/d' -e 's|@PYTHON@|/usr/bin/python3|g' \
        "data/$unit" > "$ROOT/usr/lib/systemd/user/$unit"
done

# Nemo extension (its @SOURCE_DIR@ line then simply does nothing).
mkdir -p "$ROOT/usr/share/nemo-python/extensions"
cp nemo/onedrive_ondemand_nemo.py "$ROOT/usr/share/nemo-python/extensions/"

# Icons.
mkdir -p "$ROOT/usr/share/icons"
cp -r data/icons/hicolor "$ROOT/usr/share/icons/"

# Menu entry and tray autostart (until setup has run, the tray offers to run it).
mkdir -p "$ROOT/usr/share/applications" "$ROOT/etc/xdg/autostart"
for pair in "onedrive-ondemand.desktop:usr/share/applications" \
            "onedrive-ondemand-tray.desktop:etc/xdg/autostart"; do
    name=${pair%%:*}; dest=${pair#*:}
    sed -e 's|@LAUNCHER@|/usr/bin/onedrive-ondemand|' -e 's|@ICON@|onedrive-ondemand-ok|' \
        "data/$name" > "$ROOT/$dest/$name"
done

# Documentation and licence.
DOC="$ROOT/usr/share/doc/$PKG"
mkdir -p "$DOC"
cp README.md docs/DESIGN.md docs/RESEARCH.md "$DOC/"
cat > "$DOC/copyright" <<TXT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: onedrive-linux-ondemand
Source: https://github.com/Breedible/onedrive-linux-ondemand

Files: *
Copyright: onedrive-linux-ondemand contributors
License: GPL-3.0+
 On Debian-based systems the full text is in /usr/share/common-licenses/GPL-3.
TXT

SIZE=$(du -sk "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<TXT
Package: $PKG
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-xapp-1.0, gir1.2-gdkpixbuf-2.0, nemo-python, fuse3, libnotify-bin, curl, pkexec | policykit-1
Recommends: git
Maintainer: Breedible <312606118+Breedible@users.noreply.github.com>
Homepage: https://github.com/Breedible/onedrive-linux-ondemand
Description: Files On-Demand style OneDrive client for Linux Mint
 Shows your OneDrive in ~/OneDrive - Personal without downloading everything:
 files download when you open them. Right-click in Nemo to always keep
 files on this device or to free up space, with status emblems and a
 tray icon, and several accounts (e.g. personal and work) side by side.
 Built on rclone mount. Setup installs rclone 1.75 or newer from
 rclone.org if needed (the version in Mint's own repositories is too old).
TXT

# After installing: say what to do next. (Setup itself can't run here: it
# needs your own desktop session and a browser to sign in.)
cat > "$ROOT/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    echo ""
    echo "OneDrive On-Demand is installed."
    echo "Next: open 'OneDrive' from the menu (or run: onedrive-ondemand setup)."
    echo "If you already use it, run 'onedrive-ondemand setup' once to restart it."
    echo ""
fi
SH

# Files Python compiles at run time shouldn't be left behind on removal.
cat > "$ROOT/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
rm -rf /usr/lib/python3/dist-packages/onedrive_ondemand/__pycache__
SH
chmod 755 "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postinst"

# Standard permissions: files 644, folders and programs 755.
chmod -R u=rwX,go=rX "$ROOT"
chmod 755 "$ROOT/usr/bin/onedrive-ondemand" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postinst"

dpkg-deb --root-owner-group --build "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
echo "Built dist/${PKG}_${VERSION}_all.deb"
