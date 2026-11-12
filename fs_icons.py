"""The logos of the programs: the window icon and the taskbar button.

The files are in icons/ (next to this module):
- <name>.svg              the logo (the source)
- <name>-<n>.png          16 .. 256 px (OpenFOAM Studio: to 1024 px), the window icon
- <name>.ico, <name>.icns  for a shortcut, an installer, an exe or a macOS app

tools/make_icons.py makes the OpenFOAM Studio files and the «Polars»
window files (polars-*) from their SVG files. The
MeshFix Studio files are copies from the MeshFix Studio repository
(packaging/icons/make_icons.py there). If the files
are missing (for example in an export without icons/), the icon is empty
and Qt shows its default icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtGui

ICON_DIR = Path(__file__).resolve().with_name("icons")
OFS = "openfoam-studio"
MESHFIX = "meshfix-studio"
POLARS = "polars"            # the «Polars» window (T45)
# Windows app ids: each program gets its own taskbar button.
OFS_APP_ID = "OpenFOAMStudio.OpenFOAMStudio"


def icon(name: str) -> QtGui.QIcon:
    """The logo <name> in each size of icons/ (empty when no file)."""
    result = QtGui.QIcon()
    for path in sorted(ICON_DIR.glob(f"{name}-*.png")):
        result.addFile(str(path))
    return result


def own_taskbar_button(app_id: str) -> None:
    """Windows: the programs start with pythonw.exe. Without an own app id
    the taskbar shows the button of Python with the icon of Python. With
    the id, the button shows the window icon. Call it before the first
    window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass
