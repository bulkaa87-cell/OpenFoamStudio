#!/bin/sh
# Make the .deb and .rpm packages of OpenFOAM Studio from the PyInstaller
# folder (the same folder as the AppImage).
#
#   sh packaging/linux/make_packages.sh 0.2.7 [deb] [rpm]
#
# Input: "dist/OpenFOAM Studio/" and the icons in icons/.
# Output: dist/OpenFOAM-Studio-<version>-linux-x86_64.deb and .rpm.
# Tools: dpkg-deb (deb), rpmbuild (rpm; Ubuntu: apt-get install rpm).
#
# Why packages: a browser removes the execute bit of a downloaded file, and
# GNOME then opens an AppImage in «Disks» («Restore Disk Image»). A .deb or
# .rpm opens in the software centre with «Install», and the program then is
# in the menu.
#
# The layout: /opt/openfoam-studio (the program), /usr/bin/openfoam-studio
# (start script), the menu entry and the icons. PyInstaller copies the
# libraries into the program (as for the AppImage), thus the packages ask
# only for the C library. Build on the oldest Linux that you support.
set -eu
VERSION="${1:?version}"
shift
KINDS="${*:-deb rpm}"
SRC="dist/OpenFOAM Studio"
NAME="openfoam-studio"
ROOT="build/pkgroot"
BASE="dist/OpenFOAM-Studio-${VERSION}-linux-x86_64"
SUMMARY="External aerodynamics with OpenFOAM: geometry, mesh, run, results"
URL="https://github.com/bulkaa87-cell/OpenFoamStudio"
test -d "$SRC"

# The files of both packages, at their install paths.
rm -rf "$ROOT"
mkdir -p "$ROOT/opt" "$ROOT/usr/bin" "$ROOT/usr/share/applications"
cp -R "$SRC" "$ROOT/opt/$NAME"
cat > "$ROOT/usr/bin/$NAME" <<'EOF'
#!/bin/sh
exec "/opt/openfoam-studio/OpenFOAM Studio" "$@"
EOF
chmod 755 "$ROOT/usr/bin/$NAME"
cp packaging/linux/openfoam-studio.desktop "$ROOT/usr/share/applications/$NAME.desktop"
for n in 16 24 32 48 64 128 256 512; do
    dir="$ROOT/usr/share/icons/hicolor/${n}x${n}/apps"
    mkdir -p "$dir"
    cp "icons/openfoam-studio-$n.png" "$dir/$NAME.png"
done
mkdir -p "$ROOT/usr/share/icons/hicolor/scalable/apps"
cp icons/openfoam-studio.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/$NAME.svg"
chmod -R u+rwX,go+rX,go-w "$ROOT"
find "$ROOT/usr/share" -type f -exec chmod 644 {} +

for kind in $KINDS; do
case "$kind" in
deb)
    DEB="build/deb"
    rm -rf "$DEB"
    mkdir -p "$DEB"
    cp -a "$ROOT/." "$DEB/"
    mkdir -p "$DEB/DEBIAN"
    SIZE=$(du -sk "$DEB" | cut -f1)
    cat > "$DEB/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Architecture: amd64
Maintainer: OpenFOAM Studio <bulkaa87-cell@users.noreply.github.com>
Installed-Size: $SIZE
Depends: libc6 (>= 2.35)
Section: science
Priority: optional
Homepage: $URL
Description: $SUMMARY
 A desktop program for external aerodynamics: it checks and repairs the
 geometry, makes the mesh, runs OpenFOAM and shows the results.
EOF
    rm -f "$BASE.deb"
    dpkg-deb --root-owner-group -Zxz --build "$DEB" "$BASE.deb"
    echo "$BASE.deb"
    ;;
rpm)
    TOP="$(pwd)/build/rpm"
    rm -rf "$TOP"
    mkdir -p "$TOP/SPECS" "$TOP/SOURCES" "$TOP/BUILD" "$TOP/RPMS" "$TOP/SRPMS"
    cp -a "$ROOT" "$TOP/SOURCES/root"
    cat > "$TOP/SPECS/$NAME.spec" <<EOF
# The program is built already (PyInstaller): copy it, change nothing.
%define debug_package %{nil}
%global __os_install_post %{nil}
%global _build_id_links none
%global __brp_mangle_shebangs %{nil}
%global __brp_check_rpaths %{nil}
%global _binaries_in_noarch_packages_terminate_build 0

Name:           $NAME
Version:        $VERSION
Release:        1
Summary:        $SUMMARY
License:        GPL-3.0-only
URL:            $URL
ExclusiveArch:  x86_64
# The libraries are in the program (PyInstaller): no automatic requires.
AutoReqProv:    no
Requires:       glibc >= 2.35
Recommends:     podman

%description
A desktop program for external aerodynamics: it checks and repairs the
geometry, makes the mesh, runs OpenFOAM and shows the results.

%install
mkdir -p %{buildroot}
cp -a %{_sourcedir}/root/. %{buildroot}/

%files
/opt/$NAME
/usr/bin/$NAME
/usr/share/applications/$NAME.desktop
/usr/share/icons/hicolor/*/apps/$NAME.*

%changelog
* $(LC_ALL=C date -u +"%a %b %d %Y") OpenFOAM Studio <bulkaa87-cell@users.noreply.github.com> - $VERSION-1
- Release $VERSION ($URL/releases)
EOF
    rpmbuild --quiet -bb --target x86_64 --define "_topdir $TOP" "$TOP/SPECS/$NAME.spec"
    rm -f "$BASE.rpm"
    cp "$TOP"/RPMS/x86_64/*.rpm "$BASE.rpm"
    echo "$BASE.rpm"
    ;;
*)
    echo "unknown package kind: $kind" >&2
    exit 2
    ;;
esac
done
