"""Interface languages (alias): the engine is meshfix.i18n.

The translation engine of OpenFOAM Studio is the module meshfix.i18n of the
shared package meshfix-core (MeshFix Studio, see fs_shared.py). This module
is an alias: ``import fs_i18n`` gives the same module object, thus a test
can monkeypatch it. Do not add code here; change the package.

This alias adds two things for OpenFOAM Studio:
- the catalogues of the program, ``i18n/<lang>/<module>.json`` next to the
  modules (``add_catalogue_dir``: their entries win over the entries of
  the packages);
- the language setting «File → Settings» (``fs_settings.get("ui_language")``,
  ``set_language_source``). FS_LANG (environment) still wins.

The rules for texts are in CLAUDE.md, «Interface languages», and in the
docstring of meshfix.i18n.
"""

from __future__ import annotations

import sys
from pathlib import Path

import fs_shared

_engine = fs_shared.module("meshfix.i18n")


def _from_settings():
    try:
        import fs_settings  # type: ignore  # «Файл → Налаштування» (T22)
    except ImportError:
        return None
    return fs_settings.get("ui_language")


_engine.add_catalogue_dir(Path(__file__).resolve().with_name("i18n"))
_engine.set_language_source(_from_settings)
sys.modules[__name__] = _engine
