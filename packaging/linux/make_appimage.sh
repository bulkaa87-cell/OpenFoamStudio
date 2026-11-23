#!/bin/sh
# Make the Linux AppImage of OpenFOAM Studio from the PyInstaller folder.
#
#   sh packaging/linux/make_appimage.sh 0.2.5
#
# Input: "dist/OpenFOAM Studio/" and the icons in icons/.
# Output: dist/OpenFOAM-Studio-<version>-linux-x86_64.AppImage.
# appimagetool is downloaded once to build/ (it runs without FUSE through
# APPIMAGE_EXTRACT_AND_RUN). Build on the oldest Linux that you support:
# the AppImage needs the glibc of the build system or newer.
set -eu
VERSION="${1:?version}"
SRC="dist/OpenFOAM Studio"
APPDIR="build/AppDir"
OUT="dist/OpenFOAM-Studio-${VERSION}-linux-x86_64.AppImage"
TOOL="build/appimagetool-x86_64.AppImage"
test -d "$SRC"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications"
cp -R "$SRC" "$APPDIR/usr/lib/openfoam-studio"
cp packaging/linux/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp packaging/linux/openfoam-studio.desktop "$APPDIR/openfoam-studio.desktop"
cp packaging/linux/openfoam-studio.desktop "$APPDIR/usr/share/applications/"
cp icons/openfoam-studio-256.png "$APPDIR/openfoam-studio.png"
for n in 16 24 32 48 64 128 256 512; do
    dir="$APPDIR/usr/share/icons/hicolor/${n}x${n}/apps"
    mkdir -p "$dir"
    cp "icons/openfoam-studio-$n.png" "$dir/openfoam-studio.png"
done
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp icons/openfoam-studio.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
cp icons/openfoam-studio-256.png "$APPDIR/.DirIcon"
mkdir -p build
if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi
rm -f "$OUT"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" "$APPDIR" "$OUT"
echo "$OUT"
