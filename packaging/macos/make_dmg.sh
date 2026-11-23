#!/bin/sh
# Make the macOS disk image of OpenFOAM Studio from the PyInstaller bundle.
#
#   sh packaging/macos/make_dmg.sh 0.2.5 arm64     (or x86_64)
#
# Input: "dist/OpenFOAM Studio.app". Output:
# dist/OpenFOAM-Studio-<version>-macos-<arch>.dmg.
#
# The app gets an ad-hoc signature (codesign -s -): free, no Apple account.
# Apple silicon does not start an app without a signature. The app is not
# notarized: macOS asks one time at the first start (System Settings >
# Privacy & Security > Open Anyway, or right click > Open).
set -eu
VERSION="${1:?version}"
ARCH="${2:-$(uname -m)}"
APP="dist/OpenFOAM Studio.app"
OUT="dist/OpenFOAM-Studio-${VERSION}-macos-${ARCH}.dmg"
test -d "$APP"
codesign --force --deep --sign - "$APP"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
# hdiutil fails sometimes with "Resource busy" on the CI runners: try again.
n=0
until hdiutil create -volname "OpenFOAM Studio" -srcfolder "$STAGE" -ov -format ULMO "$OUT"; do
    n=$((n + 1))
    if [ "$n" -ge 5 ]; then
        exit 1
    fi
    sleep 10
done
rm -rf "$STAGE"
echo "$OUT"
