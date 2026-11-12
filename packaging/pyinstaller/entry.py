"""The entry point of the installed program (PyInstaller).

The same as «python foam_studio.py», with one more case: the 3D view tests
each graphics level in a child process
(`sys.executable -X utf8 <path>/fs_view3d.py --probe ...`, fs_view3d
`_run_probe`). In the installed program, sys.executable is this program,
so this file sends such a start to `fs_view3d._probe_main`.
"""

import os
import sys

os.environ.setdefault("QT_API", "pyside6")


def _probe_args(argv: list[str]) -> "list[str] | None":
    """The probe arguments, if `argv` is a start of the fs_view3d probe."""
    args = argv[1:]
    while args[:1] == ["-X"]:
        args = args[2:]
    if (len(args) >= 2 and os.path.basename(args[0]).startswith("fs_view3d")
            and args[1] == "--probe"):
        return args[2:]
    return None


if __name__ == "__main__":
    probe = _probe_args(sys.argv)
    if probe is not None:
        import fs_view3d
        sys.exit(fs_view3d._probe_main(probe))
    import foam_studio
    foam_studio.main()
