"""Alias of meshfix.methods (the shared package meshfix-core of MeshFix Studio).

The code is in the MeshFix Studio repository (see fs_shared.py and
CLAUDE.md, «Mesh repair: one code for two programs»). `import fs_repair` gives
the same module object as `import meshfix.methods`: a test can monkeypatch
it. Do not add code here; change the package and move the pin.
"""

import sys

import fs_i18n  # noqa: F401  — first: the catalogues and the language of the program
import fs_shared

sys.modules[__name__] = fs_shared.module("meshfix.methods")
