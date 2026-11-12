"""The shared packages of MeshFix Studio: meshfix (meshfix-core) and foamview.

OpenFOAM Studio repairs and checks a mesh, and draws the «3D» tab, with the
packages of the MeshFix Studio repository
(https://github.com/bulkaa87-cell/MeshFixStudio). requirements.txt pins
their version (PINNED below is the same version). The modules fs_meshfix,
fs_repair, fs_winfix and fs_i18n are aliases of the package modules: do not
change them here, change the package (CLAUDE.md, «Mesh repair: one code
for two programs»).

Older versions of OpenFOAM Studio built meshfix.pyd and foamview.pyd into
the program folder. Such a file has the name of the package and hides it.
package() therefore skips each folder of sys.path that has such a file.
"""

from __future__ import annotations

import importlib
import os
import sys

# The versions of requirements.txt. A different installed version gives a
# note in the log (version_note()), not an error.
PINNED = {"meshfix": "0.3.1", "foamview": "0.3.1"}

_SUFFIXES = (".pyd", ".so")


def _old_native_in(folder: str, name: str) -> bool:
    """True if `folder` has an old native module `name`.pyd / `name`.so."""
    base = folder or os.getcwd()
    try:
        return any(os.path.isfile(os.path.join(base, name + s))
                   for s in _SUFFIXES)
    except (OSError, ValueError):
        return False


def package(name: str):
    """Import the package `name` (meshfix or foamview), also when an old
    `name`.pyd of an older version is on sys.path. Raises ImportError with
    the install command when the package is missing."""
    mod = sys.modules.get(name)
    if mod is not None and hasattr(mod, "__path__"):
        return mod
    if mod is not None:                  # the old single native module
        del sys.modules[name]
    saved = list(sys.path)
    sys.path[:] = [p for p in saved if not _old_native_in(p, name)]
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        dist = "meshfix-core" if name == "meshfix" else name
        raise ImportError(
            f"The package {dist} is missing (the shared code of MeshFix "
            f"Studio). Install it: python -m pip install -r requirements.txt"
            f" ({exc})") from exc
    finally:
        added = [p for p in sys.path if p not in saved]
        sys.path[:] = saved + added


def module(name: str):
    """Import `name` (for example "meshfix.repair") through package()."""
    package(name.split(".")[0])
    return importlib.import_module(name)


def native(name: str):
    """The native module of a package (meshfix._native, foamview._native),
    or None when the package or its native module is missing."""
    try:
        return module(name + "._native")
    except ImportError:
        return None


def version_note() -> str:
    """'' when the installed packages have the pinned versions, else a
    short English note for the log (not shown in a dialog)."""
    out = []
    for name, want in PINNED.items():
        try:
            have = getattr(package(name), "__version__", "?")
        except ImportError:
            have = None
        if have != want:
            out.append(f"{name} {have or 'missing'} (pinned {want})")
    return ", ".join(out)
