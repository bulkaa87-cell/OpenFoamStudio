"""Where the program keeps its files: program files and user data.

Two cases:

- Source copy (`python foam_studio.py`, «OpenFOAM Studio.bat»): the program
  folder is the repository folder, and the data (cases, projects, session,
  saved meshes) stays next to it, as before.
- Installed copy (PyInstaller, `sys.frozen`): the program folder is the
  install folder. The installer replaces it on an update and removes it on
  uninstall, so the data goes to the Documents folder of the user:
  Documents/OpenFOAM Studio/{cases,projects,session,meshes}. The settings
  file stays where fs_settings keeps it (%APPDATA%\\OpenFOAM Studio).

Environment:
    FS_DATA_DIR   a folder for the data (tests, portable use)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "OpenFOAM Studio"
FROZEN = bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """The folder of the program files (read only in an installed copy).

    Frozen: the folder of the bundled modules and data (PyInstaller
    `_MEIPASS`). Source: the folder of this file.
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent


def documents_dir() -> Path:
    """The Documents folder of the user (also when OneDrive moved it)."""
    try:
        from PySide6.QtCore import QStandardPaths
        loc = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation)
        if loc:
            return Path(loc)
    except Exception:                                      # noqa: BLE001
        pass
    return Path(os.path.expanduser("~")) / "Documents"


def data_dir() -> Path:
    """The folder for cases, projects, the session and saved meshes."""
    env = os.environ.get("FS_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if FROZEN:
        return documents_dir() / APP_NAME
    return Path(__file__).resolve().parent

