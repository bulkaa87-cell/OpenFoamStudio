#!/bin/sh
# Check the system libraries of OpenFOAM Studio (AppImage start) and offer
# to install the missing ones from the internet with the package manager.
#
#   sh check_deps.sh <program folder>      (AppRun calls it)
#   OFS_SKIP_DEPS=1                        no check
#   OFS_DEPS_DRY_RUN=1                     print the plan, install nothing
#
# The program takes these libraries from the system (host_libs.txt): an
# old copy in the program stops the graphics driver. If one is missing,
# Qt or the 3D view cannot start. The .deb and .rpm packages require the
# same libraries, so apt and dnf install them with the program.
#
# Exit code 0 always: the program starts also when the person says no.

[ "${OFS_SKIP_DEPS:-}" = "1" ] && exit 0
PROG="${1:?program folder}"
INT="$PROG/_internal"

# soname | Debian / Ubuntu package (alternatives with "/") | Arch package
TABLE='libstdc++.so.6|libstdc++6|gcc-libs
libgcc_s.so.1|libgcc-s1|gcc-libs
libexpat.so.1|libexpat1|expat
libz.so.1|zlib1g|zlib
libX11.so.6|libx11-6|libx11
libX11-xcb.so.1|libx11-xcb1|libx11
libxcb.so.1|libxcb1|libxcb
libxkbcommon.so.0|libxkbcommon0|libxkbcommon
libxkbcommon-x11.so.0|libxkbcommon-x11-0|libxkbcommon-x11
libxcb-cursor.so.0|libxcb-cursor0|xcb-util-cursor
libGL.so.1|libgl1|libglvnd
libEGL.so.1|libegl1|libglvnd
libfontconfig.so.1|libfontconfig1|fontconfig
libfreetype.so.6|libfreetype6|freetype2
libdbus-1.so.3|libdbus-1-3|dbus
libglib-2.0.so.0|libglib2.0-0t64/libglib2.0-0|glib2
libXrender.so.1|libxrender1|libxrender
libXext.so.6|libxext6|libxext
libXi.so.6|libxi6|libxi
libvulkan.so.1|libvulkan1|vulkan-icd-loader'

have_lib() {
    if command -v ldconfig >/dev/null 2>&1 || [ -x /sbin/ldconfig ]; then
        LDC=$(command -v ldconfig 2>/dev/null || echo /sbin/ldconfig)
        "$LDC" -p 2>/dev/null | grep -q "^[[:space:]]*$1 " && return 0
    fi
    for d in /lib64 /usr/lib64 /lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu \
             /usr/lib /lib; do
        [ -e "$d/$1" ] && return 0
    done
    [ -e "$INT/$1" ]
}

# 1. The libraries of the table.
MISSING=""
OLDIFS="$IFS"
IFS='
'
for row in $TABLE; do
    so="${row%%|*}"
    have_lib "$so" || MISSING="$MISSING $so"
done
IFS="$OLDIFS"
# 2. Other libraries that the Qt platform plugins, Qt and the OpenGL
#    module of VTK need (ldd shows them as «not found»).
for f in "$INT"/PySide6/Qt/plugins/platforms/libqxcb.so \
         "$INT"/PySide6/Qt/plugins/platforms/libqwayland*.so \
         "$INT"/PySide6/Qt/lib/libQt6Gui.so.6 \
         "$INT"/libvtkRenderingOpenGL2*.so*; do
    [ -e "$f" ] || continue
    for so in $(LD_LIBRARY_PATH="$INT:$INT/PySide6/Qt/lib" ldd "$f" 2>/dev/null \
                | awk '/not found/ {print $1}'); do
        case " $MISSING " in *" $so "*) ;; *) MISSING="$MISSING $so" ;; esac
    done
done
MISSING="${MISSING# }"
[ -z "$MISSING" ] && exit 0

# The packages for the missing libraries.
pkg_of() {   # soname, column (2 = deb, 3 = arch)
    printf '%s\n' "$TABLE" | awk -F'|' -v s="$1" -v c="$2" '$1 == s {print $c}'
}
CMD=""
if command -v apt-get >/dev/null 2>&1; then
    PKGS=""
    for so in $MISSING; do
        for p in $(pkg_of "$so" 2 | tr '/' ' '); do
            if apt-cache show "$p" >/dev/null 2>&1; then
                PKGS="$PKGS $p"; break
            fi
        done
    done
    [ -n "$PKGS" ] && CMD="apt-get install -y$PKGS"
elif command -v dnf >/dev/null 2>&1; then
    CMD="dnf install -y"
    for so in $MISSING; do CMD="$CMD $so()(64bit)"; done
elif command -v zypper >/dev/null 2>&1; then
    CMD="zypper --non-interactive install"
    for so in $MISSING; do CMD="$CMD $so()(64bit)"; done
elif command -v pacman >/dev/null 2>&1; then
    PKGS=""
    for so in $MISSING; do
        p=$(pkg_of "$so" 3)
        [ -n "$p" ] && PKGS="$PKGS $p"
    done
    [ -n "$PKGS" ] && CMD="pacman -S --noconfirm --needed$PKGS"
fi

TITLE="OpenFOAM Studio"
# The two interface languages of the program: Ukrainian for a Ukrainian
# system, else English.
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
    uk*) T_MISSING="Бракує системних бібліотек:"
         T_BY_HAND="Встановіть їх менеджером пакетів вашої системи."
         T_ASK="Завантажити й встановити їх зараз?" ;;
    *)   T_MISSING="These system libraries are missing:"
         T_BY_HAND="Install them with the package manager of your system."
         T_ASK="Download and install them now?" ;;
esac
TEXT="$T_MISSING $MISSING"
if [ -z "$CMD" ]; then
    TEXT="$TEXT

$T_BY_HAND"
    if command -v zenity >/dev/null 2>&1; then
        zenity --warning --title="$TITLE" --text="$TEXT" 2>/dev/null
    elif command -v kdialog >/dev/null 2>&1; then
        kdialog --title "$TITLE" --sorry "$TEXT" 2>/dev/null
    else
        echo "$TEXT" >&2
    fi
    exit 0
fi
ASK="$TEXT

$T_ASK
($CMD)"
if [ "${OFS_DEPS_DRY_RUN:-}" = "1" ]; then
    echo "missing: $MISSING"
    echo "command: $CMD"
    exit 0
fi
YES=1
if command -v zenity >/dev/null 2>&1; then
    zenity --question --title="$TITLE" --width=480 --text="$ASK" 2>/dev/null || YES=0
elif command -v kdialog >/dev/null 2>&1; then
    kdialog --title "$TITLE" --yesno "$ASK" 2>/dev/null || YES=0
elif [ -t 0 ]; then
    printf '%s [y/N] ' "$ASK"
    read -r ans
    case "$ans" in y|Y|yes|Yes) ;; *) YES=0 ;; esac
else
    command -v notify-send >/dev/null 2>&1 && notify-send "$TITLE" "$TEXT"
    YES=0
fi
[ "$YES" = "1" ] || exit 0
if command -v pkexec >/dev/null 2>&1 && [ "$(id -u)" != "0" ]; then
    # shellcheck disable=SC2086
    pkexec $CMD || true
elif [ "$(id -u)" = "0" ]; then
    # shellcheck disable=SC2086
    $CMD || true
elif [ -t 0 ]; then
    # shellcheck disable=SC2086
    sudo $CMD || true
fi
exit 0
