"""Program settings: one JSON file per user, not per program folder.

These are the settings of the PROGRAM (interface language, rookie mode,
folders, machines, 3D backend, mesh store). The settings of a CASE stay in
the panels and in the project file.

The file is in the user config folder, not in the program folder:
- Windows: %APPDATA%\\OpenFOAM Studio\\settings.json
- Linux / macOS: the Qt generic config folder (~/.config, ~/Library/Preferences)
  + "OpenFOAM Studio/settings.json"
The user runs a stable copy and a development copy of the program side by
side. Both copies read and write the same file.

API (other modules depend on these names, e.g. fs_i18n reads
`get("ui_language", "uk")`):
    get(key, default=None)   value, or `default`, or the default of DEFAULTS
    set(key, value)          store; listeners get (key, value) on a change
    update(mapping)          many keys, one write
    reset(keys=None)         back to DEFAULTS
    subscribe(fn) / unsubscribe(fn)

Before this module, the values were in QSettings (registry, scope
"OpenFOAM Studio"/"ui") and in session.json. `migrate()` copies them ONE time.
It does not delete them: the older copy of the program still reads them. Each
`set()` of such a key also writes the old QSettings value (`bind_qsettings`),
so the older copy sees the change.

Environment:
    FS_SETTINGS       full path of the settings file (portable use, one test)
    FS_SETTINGS_DIR   a folder for ALL settings of this process and of its
                      child processes: settings.json, and the QSettings of Qt
                      (as INI files, not the Windows registry). The tests set
                      it (tests/_isolate.py), so that a test run does not
                      change the settings of the user.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

APP_NAME = "OpenFOAM Studio"
FILE_NAME = "settings.json"
FORMAT = 1
MIGRATED = "_migrated"          # marker: the one-time migration is done

LANGUAGES = ("uk", "en")
VIEW3D_LEVELS = ("auto", "gpu", "soft", "vtk", "cpu")

def _system_language() -> str:
    try:
        from fs_i18n import system_language
        return system_language()
    except Exception:                                      # noqa: BLE001
        return "en"


DEFAULTS: dict = {
    # Загальне
    # Without a setting: the language of the operating system (T28).
    "ui_language": _system_language(),
    "rookie": True,
    "restore_session": True,
    "cases_dir": "",            # "" = <program folder>/cases
    "projects_dir": "",         # "" = <program folder>/projects
    # Машини
    "ssh_host": "",
    "check_machines_on_start": True,
    # T44: notifications about the runs (tray message + line in the window),
    # and the connection to the server runs again by itself.
    "notify": True,
    "auto_reconnect": True,
    "server_wizard_offered": False,     # installed copy: asked once (T34)
    # {ssh host: "docker|image" or "podman|image"} as JSON: the servers that
    # run OpenFOAM in a container (the server wizard writes it).
    "ssh_containers": "",
    "dist_on": False,
    "dist_machines": "",        # JSON text of fs_backend.machines_to_json
    "dist_key": "~/.ssh/id_ed25519",
    "dist_user": "",            # "" = the user of the server login
    "dist_net": "",
    # The run queue on a server (T46, fs_queue): its server ("" = ssh_host),
    # and whether it is set up there (then an angle series goes to it).
    "queue_host": "",
    "queue_on": False,
    # 3D
    "view3d_backend": "auto",
    # Сітка
    "mesh_auto_fetch": False,
    "mesh_keep": 12,
}

CHOICES = {"ui_language": LANGUAGES, "view3d_backend": VIEW3D_LEVELS}
LIMITS = {"mesh_keep": (1, 200)}

# Settings key -> QSettings key (scope SETTINGS_ORG / SETTINGS_APP of
# foam_studio). Read by `migrate`, written by `set` for the older copy.
LEGACY_QSETTINGS = {
    "rookie": "rookie",
    "restore_session": "session",
    "ssh_host": "ssh_host",
    "dist_on": "dist_on",
    "dist_machines": "dist_machines",
    "dist_key": "dist_key",
    "dist_user": "dist_user",
    "dist_net": "dist_net",
    "projects_dir": "projects_dir",
}
# Keys from <program folder>/session.json.
LEGACY_SESSION = {"cases_dir": "cases_dir"}

_RELOAD_EVERY = 1.0             # s: how often get() looks at the file time

_path: Path | None = None
_data: dict | None = None
_mtime: float | None = None
_checked = 0.0
_listeners: list = []
_legacy: tuple[str, str] | None = None


# --------------------------------------------------------------------------
# Where the file is
# --------------------------------------------------------------------------

def default_dir() -> Path:
    """Config folder of this user for the program."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        return Path(base) / APP_NAME
    loc = ""
    try:
        from PySide6.QtCore import QStandardPaths
        loc = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.GenericConfigLocation)
    except Exception:                                      # noqa: BLE001
        loc = ""
    if not loc:
        if sys.platform == "darwin":
            loc = os.path.join(os.path.expanduser("~"), "Library", "Preferences")
        else:
            loc = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
                os.path.expanduser("~"), ".config")
    return Path(loc) / APP_NAME


def path() -> Path:
    """The settings file: FS_SETTINGS, then FS_SETTINGS_DIR, then the default."""
    env = os.environ.get("FS_SETTINGS", "").strip()
    if env:
        return Path(env)
    env = os.environ.get("FS_SETTINGS_DIR", "").strip()
    if env:
        return Path(env) / FILE_NAME
    if _path is not None:
        return _path
    return default_dir() / FILE_NAME


def configure(file: str | os.PathLike | None) -> None:
    """Use another file (None: the default). Clears the cache."""
    global _path, _data, _mtime
    _path = Path(file) if file else None
    _data, _mtime = None, None


def bind_qsettings(org: str, app: str) -> None:
    """Old QSettings scope of the program window.

    The window has scope "ui". Tests use another scope (e.g. "ui-tests") to
    keep the settings of the user. For such a scope, the settings file also
    goes to a test file in the temp folder, if FS_SETTINGS is not set.
    """
    global _legacy
    _legacy = (str(org), str(app))
    if app != "ui" and not (os.environ.get("FS_SETTINGS")
                            or os.environ.get("FS_SETTINGS_DIR")):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in app)
        configure(Path(tempfile.gettempdir()) / "OpenFOAM Studio tests"
                  / f"settings-{safe}.json")
    elif app == "ui":
        configure(None)


# --------------------------------------------------------------------------
# Read and write
# --------------------------------------------------------------------------

def _flag(value, default: bool) -> bool:
    """QSettings gives a bool or the text «false», for different platforms."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def coerce(key: str, value):
    """Value of the type of the default. A bad value gives the default."""
    default = DEFAULTS.get(key)
    if default is None:
        return value
    try:
        if isinstance(default, bool):
            value = _flag(value, default)
        elif isinstance(default, int):
            value = int(float(value))
            lo, hi = LIMITS.get(key, (value, value))
            value = min(max(value, lo), hi)
        elif isinstance(default, str):
            value = "" if value is None else str(value)
    except (TypeError, ValueError):
        return default
    if key in CHOICES:
        value = str(value).strip().lower()
        if value not in CHOICES[key]:
            return default
    return value


def _read_file() -> dict:
    p = path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_mtime() -> float | None:
    try:
        return path().stat().st_mtime
    except OSError:
        return None


def _load(force: bool = False) -> dict:
    """The cache; read again when the file changed (other copy of the program)."""
    global _data, _mtime, _checked
    now = time.monotonic()
    if _data is not None and not force and now - _checked < _RELOAD_EVERY:
        return _data
    _checked = now
    mt = _file_mtime()
    if _data is None or force or mt != _mtime:
        _data = _read_file()
        _mtime = mt
    return _data


def _write(changes: dict, removed: tuple = ()) -> None:
    """Read the file again, apply only OUR changes, write it atomically.

    Two copies of the program can be open: a write of the full cache would
    remove what the other copy wrote in the meantime.
    """
    global _data, _mtime, _checked
    data = _read_file()
    data.update(changes)
    for k in removed:
        data.pop(k, None)
    data["format"] = FORMAT
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, p)
            break
        except PermissionError:                 # Windows: file open elsewhere
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)
    _data = data
    _mtime = _file_mtime()
    _checked = time.monotonic()


def get(key: str, default=None):
    data = _load()
    if key in data:
        return coerce(key, data[key])
    if default is not None:
        return default
    return DEFAULTS.get(key)


def all_values() -> dict:
    """All known keys with their current values."""
    return {k: get(k) for k in DEFAULTS}


def update(values: dict) -> dict:
    """Store many keys with one write. Returns {key: new value} of the changes."""
    changed = {}
    for key, value in values.items():
        value = coerce(key, value)
        if get(key) != value:
            changed[key] = value
    if not changed:
        return {}
    try:
        _write(changed)
    except OSError as exc:
        print(f"fs_settings: cannot write {path()}: {exc}", file=sys.stderr)
        # Keep the values for this run at least.
        _load().update(changed)
    for key, value in changed.items():
        _mirror(key, value)
        _notify(key, value)
    return changed


def set(key: str, value) -> bool:                          # noqa: A001
    """Store one key. True when the value changed."""
    return bool(update({key: value}))


def reset(keys=None) -> dict:
    """Back to DEFAULTS (all keys or `keys`). Returns the changes."""
    keys = list(DEFAULTS) if keys is None else list(keys)
    return update({k: DEFAULTS[k] for k in keys if k in DEFAULTS})


def subscribe(fn) -> None:
    """fn(key, value) after each change made by this process."""
    if fn not in _listeners:
        _listeners.append(fn)


def unsubscribe(fn) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _notify(key: str, value) -> None:
    for fn in list(_listeners):
        try:
            fn(key, value)
        except Exception as exc:                           # noqa: BLE001
            print(f"fs_settings: listener {fn!r} failed on {key}: {exc}",
                  file=sys.stderr)


# --------------------------------------------------------------------------
# Old places: QSettings and session.json
# --------------------------------------------------------------------------

def _qsettings():
    if _legacy is None:
        return None
    try:
        from PySide6 import QtCore
    except ImportError:
        return None
    return QtCore.QSettings(*_legacy)


def _legacy_value(key: str, value):
    """Value in the format that the older program writes to QSettings."""
    if key == "dist_on":
        return "1" if value else "0"
    return value


def _mirror(key: str, value) -> None:
    qkey = LEGACY_QSETTINGS.get(key)
    if not qkey:
        return
    st = _qsettings()
    if st is not None:
        st.setValue(qkey, _legacy_value(key, value))


def migrate(app_dir: str | os.PathLike | None = None) -> dict:
    """One-time copy of the old values. Old values are NOT deleted.

    Keys already in the file are not changed. Returns the copied values.
    """
    data = _load(force=True)
    if data.get(MIGRATED):
        return {}
    found: dict = {}
    st = _qsettings()
    if st is not None:
        for key, qkey in LEGACY_QSETTINGS.items():
            if key in data or not st.contains(qkey):
                continue
            raw = st.value(qkey)
            if raw is None or (isinstance(raw, str) and not raw.strip()
                               and not isinstance(DEFAULTS[key], str)):
                continue
            found[key] = coerce(key, raw)
    if app_dir is not None:
        try:
            sess = json.loads((Path(app_dir) / "session.json").read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            sess = {}
        if isinstance(sess, dict):
            for key, skey in LEGACY_SESSION.items():
                val = sess.get(skey)
                if key not in data and key not in found and val:
                    found[key] = coerce(key, val)
    changes = dict(found)
    changes[MIGRATED] = time.strftime("%Y-%m-%d %H:%M")
    try:
        _write(changes)
    except OSError as exc:
        print(f"fs_settings: cannot write {path()}: {exc}", file=sys.stderr)
        _load().update(changes)
    return found


# --------------------------------------------------------------------------
# Helpers for other modules
# --------------------------------------------------------------------------

def view3d_forced() -> tuple[str, str]:
    """(level, source) of the 3D backend that the user forces, or ("", "").

    The environment variable FS_3D has priority (tests, gpucheck, a manual
    start). Then the setting "view3d_backend"; "auto" forces nothing.
    """
    env = os.environ.get("FS_3D", "").strip().lower()
    if env:
        return env, "FS_3D"
    lv = get("view3d_backend")
    if lv and lv != "auto":
        return lv, "settings"
    return "", ""


def redirect_qsettings(folder: str | os.PathLike) -> bool:
    """QSettings of this process go to INI files in `folder`, not to the
    registry. False when Qt is not available.

    The constructor QSettings(organization, application) always uses the
    native format (the registry on Windows); setDefaultFormat() does not
    change it. Thus QtCore.QSettings is replaced by a subclass that makes
    such an object with IniFormat. Code that looks up QtCore.QSettings at
    the time of the call (foam_studio, the tests) gets the subclass.
    """
    try:
        from PySide6 import QtCore
    except ImportError:
        return False
    base = getattr(QtCore.QSettings, "_fs_base", QtCore.QSettings)
    base.setDefaultFormat(base.IniFormat)
    for scope in (base.UserScope, base.SystemScope):
        base.setPath(base.IniFormat, scope, str(folder))
    if base is QtCore.QSettings:
        class QSettings(base):                             # noqa: N801
            _fs_base = base

            def __init__(self, *args, **kw):
                if (1 <= len(args) <= 3 and isinstance(args[0], str)
                        and (len(args) < 2 or isinstance(args[1], str))):
                    org = args[0]
                    app = args[1] if len(args) > 1 else ""
                    rest = args[2:]
                    super().__init__(base.IniFormat, base.UserScope, org, app,
                                     *rest, **kw)
                else:
                    super().__init__(*args, **kw)
        QtCore.QSettings = QSettings
    return True


# FS_SETTINGS_DIR also covers child processes (they import this module
# through foam_studio before they make a QSettings object).
if os.environ.get("FS_SETTINGS_DIR", "").strip():
    redirect_qsettings(os.environ["FS_SETTINGS_DIR"].strip())
