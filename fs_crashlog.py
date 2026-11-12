"""Crash notes: a text record of fatal errors, in crash.log next to settings.json.

The program starts with pythonw.exe, which has no console. Without this
module, these texts go nowhere:

- the text of a qFatal from Qt (for example "QThread: Destroyed while thread
  'run' is still running"); after it, Qt calls abort() and Windows shows only
  "c0000409 in Qt6Core.dll" (T26);
- the Python stacks of all threads at a native crash (faulthandler);
- the traceback of an uncaught Python exception (sys.excepthook,
  threading.excepthook).

`install()` starts the record, `note()` adds a line, `finish()` marks a
normal end. `last_crash()` tells if the previous start of the program ended
with a fatal record, so that the window can tell the person once.

The record is for diagnostics. Its lines are not translated (like a log of a
tool); the notice in the window is translated (foam_studio).
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path

LOG_NAME = "crash.log"
MAX_BYTES = 1_000_000          # bigger: the old file becomes crash.log.1
MAX_WARNINGS = 50              # Qt warnings per start (fatal texts: all)

START = "=== start"
END = "=== end"
FATAL = "FATAL"                # a line that starts with this marks a crash
# faulthandler writes these at a native crash or a fatal Python error.
_NATIVE = ("Windows fatal exception", "Fatal Python error")

_fh = None
_path: Path | None = None
_lock = threading.RLock()
_warnings = 0
_prev_excepthook = None
_prev_thread_hook = None


def log_path() -> Path:
    """crash.log in the folder of the settings file (FS_SETTINGS_DIR in tests)."""
    import fs_settings
    return fs_settings.path().parent / LOG_NAME


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write(text: str) -> None:
    """Write lines with time and process id. Never raises."""
    fh = _fh
    if fh is None:
        return
    try:
        with _lock:
            for line in text.rstrip("\n").split("\n"):
                fh.write(f"{_stamp()} [{os.getpid()}] {line}\n")
            fh.flush()
    except Exception:                                   # noqa: BLE001
        pass


def note(text: str) -> None:
    """Add a line to the record (for example: a thread did not stop)."""
    _write(text)


def _dump_stacks() -> None:
    """The Python stacks of all threads, straight to the file."""
    fh = _fh
    if fh is None:
        return
    try:
        with _lock:
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
    except Exception:                                   # noqa: BLE001
        pass


def _qt_handler(mode, context, message) -> None:
    """Qt message handler: fatal and critical texts always, warnings capped.

    Qt calls abort() right after a fatal message returns, so the text and the
    stacks must be on the disk before this function returns.
    """
    global _warnings
    from PySide6 import QtCore
    try:
        msg = str(message)
    except Exception:                                   # noqa: BLE001
        msg = "?"
    # The default handler wrote to stderr; keep that for a console start.
    err = sys.stderr
    if err is not None:
        try:
            err.write(msg + "\n")
            err.flush()
        except Exception:                               # noqa: BLE001
            pass
    where = ""
    try:
        if context is not None and context.file:
            where = f" ({context.file}:{context.line}, {context.function})"
    except Exception:                                   # noqa: BLE001
        pass
    if mode == QtCore.QtMsgType.QtFatalMsg:
        _write(f"{FATAL} (Qt): {msg}{where}")
        _dump_stacks()
    elif mode == QtCore.QtMsgType.QtCriticalMsg:
        _write(f"Qt critical: {msg}{where}")
    elif mode == QtCore.QtMsgType.QtWarningMsg:
        if _warnings < MAX_WARNINGS:
            _warnings += 1
            _write(f"Qt warning: {msg}{where}")


def _excepthook(etype, value, tb) -> None:
    _write("Uncaught exception:\n" + "".join(
        traceback.format_exception(etype, value, tb)))
    hook = _prev_excepthook or sys.__excepthook__
    try:
        hook(etype, value, tb)
    except Exception:                                   # noqa: BLE001
        pass


def _thread_hook(args) -> None:
    name = getattr(args.thread, "name", "?")
    _write(f"Uncaught exception in thread {name}:\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value,
                                   args.exc_traceback)))
    hook = _prev_thread_hook
    if hook is not None:
        try:
            hook(args)
        except Exception:                               # noqa: BLE001
            pass


def last_crash(path: Path | None = None) -> str:
    """The fatal line of the last start in the record, or "".

    Read it BEFORE `install()` writes the start of this run. Only the last
    start counts: an old crash is told once, at the next start.
    """
    p = Path(path) if path else log_path()
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    last = 0
    for i, line in enumerate(lines):
        if START in line:
            last = i
    found = ""
    for line in lines[last:]:
        body = line.split("] ", 1)[1] if "] " in line[:40] else line
        if body.startswith(FATAL):
            return body.strip()
        if body.startswith(_NATIVE):
            found = found or body.strip()
        elif body.startswith(END):
            # faulthandler also writes an exception that a driver catches
            # itself; when the run then ended normally, it was not a crash.
            found = ""
    return found


def install(path: Path | None = None) -> Path | None:
    """Open the record and connect the handlers. Returns the file, or None."""
    global _fh, _path, _prev_excepthook, _prev_thread_hook
    if _fh is not None:
        return _path
    p = Path(path) if path else log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > MAX_BYTES:
            old = p.with_name(p.name + ".1")
            try:
                old.unlink(missing_ok=True)
                p.rename(old)
            except OSError:
                pass
        _fh = open(p, "a", encoding="utf-8")
    except OSError:
        _fh = None
        return None
    _path = p
    _write(f"{START} {sys.executable} {' '.join(sys.argv)}")
    try:
        faulthandler.enable(file=_fh, all_threads=True)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from PySide6 import QtCore
        QtCore.qInstallMessageHandler(_qt_handler)
    except Exception:                                   # noqa: BLE001
        pass
    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    _prev_thread_hook = threading.excepthook
    threading.excepthook = _thread_hook
    return p


def finish() -> None:
    """Mark a normal end of this start."""
    _write(END)
