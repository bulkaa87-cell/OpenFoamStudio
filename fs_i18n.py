"""Interface languages: English (en, the language of the code) and Ukrainian
(uk). More languages can come later.

How it works
------------
* The texts in the code are English (gettext / Qt style). The key of a
  translation is the English text: ``tr("Refinement level")``.
* The catalogues are in ``i18n/<lang>/<module>.json``: one JSON object
  ``{"English text": "translation"}`` for each module. English needs no
  catalogue. A new language is a new folder ``i18n/<lang>/`` and an entry in
  `LANGUAGES` (and in `PLURAL_RULES` if its plural forms are not English).
* ``tr("Cells: {n:,}", n=cells)`` translates a text with values. Use it in
  place of an f-string. Without arguments, ``tr(text)`` only looks the text
  up (no ``str.format``), so it is also safe for a text that comes from data.
* The same English text with two meanings: ``tr("Level", ctx="mesh")``. The
  catalogue key is then ``"mesh\\u0004Level"``.
* Plural nouns: ``plural(n, "layer", "layers")``. The catalogue key is
  ``"layer|layers"``, the value is a list of the forms of that language
  (Ukrainian: three forms, see `PLURAL_RULES`).
* ``tr()`` and ``plural()`` return a `TrStr`: a ``str`` that also knows its
  key and its values, so it can make itself again in another language.
  ``TrStr + str`` keeps this (``join`` and f-strings do not).
* ``install()`` patches the text setters of Qt (``setText``, ``setToolTip``,
  menus, tabs, combo items with item data, ...) and the constructors that
  take a text (``QLabel("Mesh")``). Each widget keeps the key of each text
  it shows (a dynamic property; an item keeps it in an item data role).
  An event filter translates each widget when it is shown.
* ``set_language(lang)`` switches the language LIVE: it translates each
  widget again from its keys, and then calls the functions registered with
  ``on_language_changed()`` (and emits ``notifier().changed``). A window
  that composes texts from parts registers such a function and makes those
  texts again.

Texts of older versions
-----------------------
Before T28 (2026-11-11) the source texts were Ukrainian. Files and runs of
those versions keep Ukrainian texts (a status line of a run on a server,
the STL units of a project). The reverse map {translation: key} of the
catalogues finds the key of such a text, so ``tr()`` and the Qt setters
show it in the current language. The same map finds the key of a text that
a widget showed in the old language before a live change.

The language
------------
``FS_LANG`` (environment) wins, if it is set: tests and a quick look use it.
Then ``fs_settings.get("ui_language")`` («File → Settings»). If the person
never selected a language, the default of fs_settings is the language of
the operating system (Ukrainian -> uk, else en).

The check
---------
``python tools/i18n_check.py`` lists each text in tr() and in the Qt calls
without a Ukrainian entry, and each translation with other
``{placeholders}`` than its source. See CLAUDE.md, «Interface languages».
"""

from __future__ import annotations

import json
import os
import string
import weakref
from pathlib import Path

SOURCE = "en"                   # the language of the texts in the code
LANGUAGES = {"uk": "Українська", "en": "English"}
I18N_DIR = Path(__file__).resolve().with_name("i18n")
CTX_SEP = "\x04"                # "ctx\x04text": a key with a context
PLURAL_SEP = "|"                # "one|many": the key of a plural entry


def _plural_en(n: int) -> int:
    return 0 if n == 1 else 1


def _plural_uk(n: int) -> int:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return 0
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return 1
    return 2


# The index of the plural form for n, for each language (default: English).
PLURAL_RULES = {"en": _plural_en, "uk": _plural_uk}

_lang: str | None = None
_cats: dict[str, dict] = {}         # lang -> {key: text or [forms]}
_rev: dict[str, str] | None = None  # translation -> key (all languages)
_keyset: set[str] | None = None     # the keys without a context
_installed = False
_missing: set[str] = set()
_listeners: list = []


# ---------------------------------------------------------------- language
def system_language() -> str:
    """The language of the operating system: "uk" for Ukrainian, else "en".

    FS_SYSTEM_LANG overrides it (tests/_isolate.py: the tests do not depend
    on the language of the computer)."""
    forced = (os.environ.get("FS_SYSTEM_LANG") or "").strip().lower()
    if forced:
        return forced if forced in LANGUAGES else SOURCE
    try:
        if os.name == "nt":
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "uk" if (lid & 0x3FF) == 0x22 else "en"
    except Exception:                                      # noqa: BLE001
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        val = (os.environ.get(var) or "").lower()
        if val:
            return "uk" if val.startswith("uk") else "en"
    try:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "uk" if loc.startswith(("uk", "ukrainian")) else "en"
    except Exception:                                      # noqa: BLE001
        return "en"


def env_language() -> str:
    """FS_LANG of the environment ("" if not set): it overrides the setting."""
    lang = (os.environ.get("FS_LANG") or "").strip().lower()
    return lang if lang in LANGUAGES else ""


def _from_settings() -> str | None:
    try:
        import fs_settings  # type: ignore  # «Файл → Налаштування» (T22)
    except ImportError:
        return None
    try:
        return fs_settings.get("ui_language")
    except Exception:                                      # noqa: BLE001
        return None


def _norm(lang) -> str:
    lang = (lang or "").strip().lower()
    return lang if lang in LANGUAGES else SOURCE


def language() -> str:
    """The interface language: a key of LANGUAGES."""
    global _lang
    if _lang is None:
        lang = env_language() or _from_settings() or system_language()
        _lang = _norm(lang)
    return _lang


def set_language(lang: str | None) -> None:
    """Select the language. None reads it again (FS_LANG, the setting).

    If the language changes and widgets exist, they are translated again
    (live) and the listeners of ``on_language_changed`` are called.
    """
    global _lang
    old = _lang
    _lang = None if lang is None else _norm(lang)
    new = language()
    if old is not None and new != old:
        _changed(new)


def on_language_changed(fn) -> None:
    """Call ``fn(lang)`` after each live change of the language. A bound
    method is kept as a weak reference (the window can go away)."""
    try:
        ref = weakref.WeakMethod(fn)
    except TypeError:
        ref = (lambda f=fn: f)
    _listeners.append(ref)


def _changed(lang: str) -> None:
    if _installed:
        retranslate_all()
    for ref in list(_listeners):
        fn = ref()
        if fn is None:
            _listeners.remove(ref)
            continue
        try:
            fn(lang)
        except RuntimeError:            # the C++ object is already gone
            _listeners.remove(ref)
    n = _notifier_obj
    if n is not None:
        try:
            n.changed.emit(lang)
        except RuntimeError:
            pass


_notifier_obj = None


def notifier():
    """A QObject with the signal ``changed(str)`` (the new language)."""
    global _notifier_obj
    if _notifier_obj is None:
        from PySide6 import QtCore

        class _Notifier(QtCore.QObject):
            changed = QtCore.Signal(str)

        _notifier_obj = _Notifier()
    return _notifier_obj


# ---------------------------------------------------------------- catalogues
def load_catalogue(lang: str) -> dict:
    """All entries of ``i18n/<lang>/*.json``, merged."""
    out: dict = {}
    folder = I18N_DIR / lang
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k, v in data.items():
            # "" is a valid translation (a suffix that the language does
            # not need); a missing entry is not.
            if isinstance(k, str) and (isinstance(v, str) or (
                    isinstance(v, list) and v
                    and all(isinstance(x, str) for x in v))):
                out[k] = v
    return out


def catalogue(lang: str | None = None) -> dict:
    lang = lang or language()
    if lang == SOURCE:
        return {}
    cat = _cats.get(lang)
    if cat is None:
        cat = _cats[lang] = load_catalogue(lang)
    return cat


def _other_languages():
    return [lang for lang in LANGUAGES if lang != SOURCE]


def _reverse() -> dict[str, str]:
    """{translation: key} of all languages (old Ukrainian keys, data)."""
    global _rev, _keyset
    if _rev is None:
        rev: dict[str, str] = {}
        keys: set[str] = set()
        for lang in _other_languages():
            for k, v in catalogue(lang).items():
                if CTX_SEP not in k and not isinstance(v, list):
                    keys.add(k)
                if isinstance(v, list):
                    continue
                if v and v != _text_of(k):
                    rev.setdefault(v, k)
        _rev, _keyset = rev, keys
    return _rev


def _keys() -> set[str]:
    _reverse()
    return _keyset or set()


def reload() -> None:
    """Read the catalogues again (tests, a new catalogue file)."""
    global _rev, _keyset
    _cats.clear()
    _rev = _keyset = None


def _text_of(key: str) -> str:
    """The English text of a key (without its context)."""
    return key.rsplit(CTX_SEP, 1)[-1]


def _cyr(text: str) -> bool:
    return any(0x400 <= ord(c) <= 0x4FF for c in text)


def key_of(text) -> str | None:
    """The catalogue key that `text` shows, or None.

    `text` can be a key (English), or a translation of a key in any
    language (the reverse map): a Ukrainian source text of a module that is
    not converted yet, a text that a widget showed in the old language, a
    Ukrainian text from data of an older version.
    """
    if isinstance(text, TrStr):
        return text.key
    if not isinstance(text, str) or not text:
        return None
    if text in _keys():
        return text
    return _reverse().get(text)


def _render(key: str, lang: str) -> str:
    if lang == SOURCE:
        return _text_of(key)
    hit = catalogue(lang).get(key)
    if hit is None:
        if _DEBUG:
            _note_missing(key)
        return _text_of(key)
    if isinstance(hit, list):
        return hit[0]
    return hit


def lookup(text: str) -> str | None:
    """The translation of `text` into the current language, or None if
    `text` is not a known text (or the translation is the same)."""
    key = key_of(text)
    if key is None:
        return None
    out = _render(key, language())
    return None if out == text else out


# ---------------------------------------------------------------- TrStr
class TrStr(str):
    """A translated text that can make itself again in another language.

    ``key``: the catalogue key (or None for a text that is not in the
    catalogue). ``lang``: the language of this text. ``again()``: the same
    text in the current language.
    """

    def __new__(cls, text: str, make, key: str | None, lang: str):
        obj = str.__new__(cls, text)
        obj._make = make
        obj.key = key
        obj.lang = lang
        return obj

    def again(self) -> "TrStr":
        if self.lang == language():
            return self
        return self._make()

    def map(self, fn) -> "TrStr":
        """``fn(self)`` that a live change of the language makes again:
        ``tr(title).map(lambda s: s.split(",")[0])``."""
        return TrStr(fn(str.__str__(self)),
                     lambda: self.again().map(fn),
                     self.key if self.key is not None else None, self.lang)

    def __reduce__(self):               # pickle and copy: a plain str
        return (str, (str(self),))

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return _join([self, other])

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return _join([other, self])


def _again(v):
    return v.again() if isinstance(v, TrStr) else v


def _join(parts) -> TrStr:
    lang = language()
    parts = [p.again() if isinstance(p, TrStr) and p.lang != lang else p
             for p in parts]
    keyed = any(isinstance(p, TrStr) and p.key is not None for p in parts)
    out = TrStr("".join(str.__str__(p) for p in parts),
                lambda: _join(parts), "+" if keyed else None, lang)
    out.parts = parts
    return out


def _atoms(v):
    parts = getattr(v, "parts", None) if isinstance(v, TrStr) else None
    if parts is None:
        yield v
        return
    for p in parts:
        yield from _atoms(p)


def _nth_line(text: str, i: int) -> str:
    rows = text.split("\n")
    return rows[i] if i < len(rows) else ""


def lines(text) -> list:
    """The lines of `text` (``text.split("\\n")``), and each line keeps the
    keys of its parts: ``lines(shown(label))`` gives lines that a live
    change of the language can make again."""
    out, cur = [], []

    def close():
        keyed = any(isinstance(c, TrStr) and c.key is not None for c in cur)
        out.append(_join(cur) if keyed else "".join(map(str.__str__, cur)))

    for atom in _atoms(text):
        segs = str.__str__(atom).split("\n")
        keyed = isinstance(atom, TrStr) and atom.key is not None
        for i, seg in enumerate(segs):
            if i:
                close()
                cur = []
            if keyed and len(segs) > 1:
                cur.append(atom.map(lambda t, i=i: _nth_line(t, i)))
            elif keyed:
                cur.append(atom)
            elif seg:
                cur.append(seg)
    close()
    return out


def shown(widget, getter: str = "text", setter: str = "setText"):
    """The text that `widget` shows, as the TrStr that set it (if the
    widget kept its key), else as the plain text."""
    now = getattr(widget, getter)()
    h = _holder(widget, False)
    entry = h.d.get(setter) if h is not None else None
    if entry is not None and entry[1] == now:
        return entry[0]
    return now


def join(sep: str, parts) -> TrStr:
    """``sep.join(parts)`` that keeps the keys of the parts (a TrStr), so
    that a live change of the language makes the whole text again."""
    out = []
    for i, p in enumerate(parts):
        if i:
            out.append(sep)
        out.append(p)
    return _join(out)


def _value(v):
    """A value for a {placeholder}: a TrStr in the current language, a
    known text translated, anything else as it is."""
    if isinstance(v, TrStr):
        return v.again()
    if isinstance(v, str) and v:
        key = key_of(v)
        if key is not None:
            return _render(key, language())
    return v


def tr(_text, /, *args, ctx: str | None = None, **kw):
    """Translate `_text`; with arguments, also ``str.format`` them in.

    Use it in place of an f-string::

        tr("Cells: {n:,}", n=cells)         # was f"Cells: {cells:,}"
        tr("Level", ctx="mesh")             # a second meaning of "Level"

    A {value} that is a known text (or a TrStr) is translated too.
    """
    if not isinstance(_text, str):
        return _text
    if isinstance(_text, TrStr) and not args and not kw and ctx is None:
        return _text.again()
    lang = language()
    key = (ctx + CTX_SEP + _text) if ctx else _text
    if not ctx:
        found = key_of(_text)
        if found is not None:
            key = found
        elif _DEBUG and lang != SOURCE and (_text in _keys() or _cyr(_text)):
            _note_missing(_text)
    out = _render(key, lang) if (key in _keys() or CTX_SEP in key
                                 or key != _text) else _text
    if args or kw:
        vals = tuple(_value(a) for a in args)
        kvals = {k: _value(v) for k, v in kw.items()}
        try:
            out = out.format(*vals, **kvals)
        except (KeyError, IndexError, ValueError):
            # A bad placeholder in a translation must not break the
            # program: show the source text.
            out = _text_of(key).format(*vals, **kvals)
    # A text with values is made again also if only a value is known.
    known = key if (key in _keys() or CTX_SEP in key or key != _text
                    or args or kw) else None
    return TrStr(out, lambda: tr(_text, *args, ctx=ctx, **kw), known, lang)


def plural(n, one: str, many: str, ctx: str | None = None, **kw):
    """The form for `n`: ``plural(n, "{n} layer", "{n} layers")``.

    The catalogue entry ``"{n} layer|{n} layers": ["{n} шар", "{n} шари",
    "{n} шарів"]`` gives the forms of a language; `PLURAL_RULES` selects one
    of them. ``{n}`` and the other ``{placeholders}`` are formatted in.
    """
    lang = language()
    key = one + PLURAL_SEP + many
    if ctx:
        key = ctx + CTX_SEP + key
    rule = PLURAL_RULES.get(lang, _plural_en)
    idx = rule(abs(int(n)))
    forms = catalogue(lang).get(key) if lang != SOURCE else None
    if isinstance(forms, list):
        out = forms[min(idx, len(forms) - 1)]
    else:
        if lang != SOURCE and _DEBUG:
            _note_missing(key)
        out = one if _plural_en(abs(int(n))) == 0 else many
    if "{" in out:
        vals = {k: _value(v) for k, v in kw.items()}
        try:
            out = out.format(n=n, **vals)
        except (KeyError, IndexError, ValueError):
            pass
    return TrStr(out, lambda: plural(n, one, many, ctx=ctx, **kw), key, lang)


def localized(path):
    """The file of `path` in the interface language, if there is one:
    SLOVNYK.html -> SLOVNYK.en.html. Else `path` itself.

    The help files are written in Ukrainian (the plain name) and translated
    into <name>.<lang>.<ext>."""
    lang = language()
    if lang == "uk":
        return path
    p = Path(path)
    other = p.with_name(f"{p.stem}.{lang}{p.suffix}")
    return other if other.is_file() else path


def placeholders(text: str) -> set[str]:
    """Names (or positions) of the ``{fields}`` in a format string."""
    out = set()
    try:
        for _lit, name, spec, _conv in string.Formatter().parse(text):
            if name is not None:
                out.add(name.split(".")[0].split("[")[0])
                if spec:
                    out |= placeholders(spec)
    except ValueError:
        out.add("<bad format>")
    return out


# ---------------------------------------------------------------- debug
# FS_I18N_DEBUG=1: write each key that has no translation in the current
# language to i18n/missing-<lang>.txt.
_DEBUG = bool(os.environ.get("FS_I18N_DEBUG"))


def _note_missing(text: str) -> None:
    if text in _missing:
        return
    _missing.add(text)
    try:
        with open(I18N_DIR / f"missing-{language()}.txt", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(text, ensure_ascii=False) + "\n")
    except OSError:
        pass


def missing_seen() -> set[str]:
    return set(_missing)


# ---------------------------------------------------------------- Qt
# Each widget keeps the keys of its texts: {slot: (TrStr, shown)} in the
# dynamic property PROP (an item: in the data role ROLE). `shown` is what
# the getter gave after the text was set: if the getter gives another text
# later, the code changed the text in a way that the patches do not see,
# and the key is not used any more.
PROP = "_fs_tr"
ROLE = 0x0100 + 0x7E1               # Qt.UserRole + 2017
_ORIG: dict[tuple[type, str], object] = {}


class _Keys:
    """The keys of the texts of one object (a plain Python object, so that
    Qt keeps it as it is in a property or an item data role)."""

    __slots__ = ("d",)

    def __init__(self):
        self.d: dict = {}


def _holder(obj, make: bool, col: int | None = None):
    # QTreeWidgetItem: data(column, role); a list or table item: data(role)
    tree = col is not None and type(obj).__name__ == "QTreeWidgetItem" or (
        col is not None and hasattr(obj, "columnCount"))
    try:
        if col is None:
            h = obj.property(PROP)
        elif tree:
            h = obj.data(col, ROLE)
        else:
            h = obj.data(ROLE)
    except (AttributeError, RuntimeError, TypeError):
        return None
    if isinstance(h, _Keys) or not make:
        return h if isinstance(h, _Keys) else None
    h = _Keys()
    try:
        if col is None:
            obj.setProperty(PROP, h)
        elif tree:
            obj.setData(col, ROLE, h)
        else:
            obj.setData(ROLE, h)
    except (AttributeError, RuntimeError, TypeError):
        return None
    return h


def _as_tr(text):
    """`text` in the current language as a TrStr, or None if it is not a
    known text (then the widget shows it as it is)."""
    if isinstance(text, TrStr):
        if text.key is None and _COMPOSE:
            _note_composed(text)
        return text.again() if text.key is not None else None
    key = key_of(text)
    if key is None:
        if _COMPOSE and isinstance(text, str):
            _note_composed(text)
        return None
    ctx, _, plain = key.rpartition(CTX_SEP)
    return tr(plain, ctx=ctx or None)


# FS_I18N_COMPOSE=<file>: each plain text that reaches a Qt setter and
# contains an English key (two words or more) that has a translation: the
# code composed it without tr() (an f-string, str.join), and a live change of
# the language, or the language uk itself, cannot translate it. One line per
# place in the code (file:line of the caller) goes to the file.
_COMPOSE = os.environ.get("FS_I18N_COMPOSE")
_composed_seen: set = set()
_long_keys: list | None = None


def _note_composed(text: str) -> None:
    global _long_keys
    if language() == SOURCE or not text:
        return
    if _long_keys is None:
        cat = catalogue(language())
        _long_keys = sorted(
            (k for k, v in cat.items() if isinstance(v, str)
             and CTX_SEP not in k and v != k and " " in k.strip()
             and len(k) >= 10 and "{" not in k), key=len, reverse=True)
    hit = next((k for k in _long_keys if k in text), None)
    if hit is None:
        return
    import traceback
    where = "?"
    for fr in reversed(traceback.extract_stack()[:-1]):
        if not fr.filename.endswith("fs_i18n.py"):
            where = f"{os.path.basename(fr.filename)}:{fr.lineno}"
            break
    if (where, hit) in _composed_seen:
        return
    _composed_seen.add((where, hit))
    try:
        with open(_COMPOSE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps([where, hit, text[:160]],
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def _remember(obj, slot, ts, getter=None, col=None, *gargs) -> None:
    """Keep (ts, shown) for `slot` of `obj`, or forget the slot (ts None)."""
    h = _holder(obj, ts is not None, col)
    if h is None:
        return
    if ts is None:
        h.d.pop(slot, None)
        return
    shown = ts
    if getter is not None:
        try:
            shown = getattr(obj, getter)(*gargs)
        except (AttributeError, RuntimeError, TypeError):
            pass
    h.d[slot] = (ts, shown)


def _wrap_setter(cls, name: str, getter: str | None, index: bool = False):
    """``obj.name(text)`` (or ``obj.name(i, text)`` if `index`): translate
    a known text and keep its key."""
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return
    _ORIG[(cls, name)] = orig

    def wrapper(self, *args, **kw):
        pos = 1 if index else 0
        if len(args) > pos and isinstance(args[pos], str):
            ts = _as_tr(args[pos])
            args = list(args)
            if ts is not None:
                args[pos] = ts
            res = orig(self, *args, **kw)
            if index:
                _remember_indexed(self, name, args[0], ts)
            else:
                _remember(self, name, ts, getter)
            return res
        return orig(self, *args, **kw)

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


def _wrap_item_setter(cls, name: str, getter: str):
    """QTreeWidgetItem.setText(col, text) and the like: the key goes into
    the item data role of that column."""
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return
    _ORIG[(cls, name)] = orig
    has_col = cls.__name__ == "QTreeWidgetItem"

    def wrapper(self, *args, **kw):
        pos = 1 if has_col else 0
        if len(args) > pos and isinstance(args[pos], str):
            ts = _as_tr(args[pos])
            args = list(args)
            if ts is not None:
                args[pos] = ts
            res = orig(self, *args, **kw)
            col = args[0] if has_col else 0
            gargs = (col,) if has_col else ()
            _remember(self, name, ts, getter, col, *gargs)
            return res
        return orig(self, *args, **kw)

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


def _remember_indexed(obj, name, i, ts) -> None:
    """A tab text or a combo item text: the key goes to the tab page or
    into the item data role."""
    from PySide6 import QtWidgets as W
    if isinstance(obj, W.QTabWidget):
        page = obj.widget(i)
        if page is not None:
            _remember(page, "tab:" + name, ts)
    elif isinstance(obj, W.QComboBox):
        h = obj.itemData(i, ROLE)
        if not isinstance(h, _Keys):
            if ts is None:
                return
            h = _Keys()
            obj.setItemData(i, h, ROLE)
        if ts is None:
            h.d.pop(name, None)
        else:
            h.d[name] = (ts, ts)
    elif isinstance(obj, W.QTabBar):
        _remember(obj, f"tab{i}:" + name, ts)


def _wrap_maker(cls, name: str, text_pos, put: str, kind: str = "obj"):
    """A call that makes an object with a text and returns it (addMenu,
    addAction, addTab, addItem, ...). `text_pos(args)` gives the position
    of the text argument or None. `put` is the setter of that text on the
    result (or the indexed setter for kind "tab" / "combo")."""
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return
    _ORIG[(cls, name)] = orig

    def wrapper(self, *args, **kw):
        pos = text_pos(args)
        if pos is None or not isinstance(args[pos], str):
            return orig(self, *args, **kw)
        ts = _as_tr(args[pos])
        if ts is not None:
            args = list(args)
            args[pos] = ts
        res = orig(self, *args, **kw)
        if ts is None:
            return res
        try:
            if kind == "obj" and res is not None:
                _remember(res, put, ts, _GETTER.get(put))
            elif kind == "tab":
                i = res if isinstance(res, int) else self.count() - 1
                _remember_indexed(self, put, i, ts)
            elif kind == "combo":
                i = (args[0] if name == "insertItem" else self.count() - 1)
                _remember_indexed(self, put, i, ts)
            elif kind == "list":
                row = args[0] if name == "insertItem" else self.count() - 1
                item = self.item(row)
                if item is not None:
                    _remember(item, "setText", ts, "text", 0)
            elif kind == "form":
                _remember_form_label(self, args, ts)
        except (RuntimeError, TypeError, AttributeError):
            pass
        return res

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


def _remember_form_label(lay, args, ts) -> None:
    from PySide6 import QtWidgets as W
    field = None
    for a in args:
        if isinstance(a, (W.QWidget, W.QLayout)):
            field = a
    if field is None:
        return
    lbl = lay.labelForField(field)
    if lbl is not None:
        _remember(lbl, "setText", ts, "text")


def _wrap_ctor(cls, slots):
    """A constructor with a text (``QLabel("Mesh")``): set the text again
    through the setter, so that it is translated and its key is kept.
    `slots`: [(position or keyword, setter)]."""
    orig = cls.__dict__.get("__init__")
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(self, *args, **kw):
        orig(self, *args, **kw)
        for where, put in slots:
            if isinstance(where, int):
                v = args[where] if len(args) > where else None
            else:
                v = kw.get(where)
            if put == "__items__":
                if isinstance(v, (list, tuple)):
                    for c, t in enumerate(v):
                        if isinstance(t, str) and t:
                            _ORIG_CALL(self, type(self), "setText", c, t)
                continue
            if isinstance(v, str) and v:
                _ORIG_CALL(self, cls, put, v)
                break

    wrapper._fs_i18n = True
    wrapper.__name__ = "__init__"
    setattr(cls, "__init__", wrapper)


def _ORIG_CALL(obj, cls, put, *args):           # noqa: N802
    """Call the patched setter `put` of the base class `cls` (not a Python
    override of a subclass: its __init__ is not done yet)."""
    for klass in type(obj).__mro__:
        fn = klass.__dict__.get(put)
        if fn is not None and getattr(fn, "_fs_i18n", False):
            try:
                fn(obj, *args)
            except (RuntimeError, TypeError):
                pass
            return


# setter -> getter, for the texts of one object
_GETTER = {
    "setText": "text", "setToolTip": "toolTip", "setStatusTip": "statusTip",
    "setWhatsThis": "whatsThis", "setWindowTitle": "windowTitle",
    "setTitle": "title", "setPlaceholderText": "placeholderText",
    "setPrefix": "prefix", "setSuffix": "suffix",
    "setSpecialValueText": "specialValueText", "setIconText": "iconText",
    "setInformativeText": "informativeText",
    "setDetailedText": "detailedText", "setLabelText": "labelText",
    "setHtml": "toHtml", "setPlainText": "toPlainText",
}


def _patch_static(cls, name: str) -> None:
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(*args, **kw):
        args = [_shown(a) for a in args]
        for k in ("caption", "title", "text", "label", "filter"):
            if isinstance(kw.get(k), str):
                kw[k] = _shown(kw[k])
        return orig(*args, **kw)

    wrapper._fs_i18n = True
    setattr(cls, name, staticmethod(wrapper))


def _shown(v):
    if isinstance(v, str):
        ts = _as_tr(v)
        if ts is not None:
            return ts
    return v


def _patch_qt() -> None:
    from PySide6 import QtGui, QtWidgets as W

    for cls, names in (
        (W.QWidget, ("setToolTip", "setWindowTitle", "setStatusTip",
                     "setWhatsThis")),
        (W.QLabel, ("setText",)),
        (W.QAbstractButton, ("setText",)),
        (W.QGroupBox, ("setTitle",)),
        (W.QLineEdit, ("setPlaceholderText",)),
        (W.QPlainTextEdit, ("setPlaceholderText", "setPlainText")),
        (W.QTextEdit, ("setPlaceholderText", "setHtml", "setPlainText")),
        (W.QProgressDialog, ("setLabelText",)),
        (W.QMessageBox, ("setText", "setInformativeText",
                         "setDetailedText")),
        (W.QMenu, ("setTitle",)),
        (W.QSpinBox, ("setPrefix", "setSuffix")),
        (W.QDoubleSpinBox, ("setPrefix", "setSuffix")),
        (W.QAbstractSpinBox, ("setSpecialValueText",)),
        (QtGui.QAction, ("setText", "setToolTip", "setStatusTip",
                         "setIconText", "setWhatsThis")),
    ):
        for name in names:
            _wrap_setter(cls, name, _GETTER.get(name))
    # texts that are not kept: log lines, a transient message
    for cls, name in ((W.QPlainTextEdit, "appendPlainText"),
                      (W.QTextEdit, "append"),
                      (W.QProgressDialog, "setCancelButtonText")):
        _wrap_plain(cls, name)
    _wrap_status(W.QStatusBar)
    for cls in (W.QTabWidget, W.QTabBar):
        _wrap_setter(cls, "setTabText", None, index=True)
        _wrap_setter(cls, "setTabToolTip", None, index=True)
    _wrap_setter(W.QComboBox, "setItemText", None, index=True)
    for cls in (W.QTreeWidgetItem, W.QListWidgetItem, W.QTableWidgetItem):
        _wrap_item_setter(cls, "setText", "text")
        _wrap_item_setter(cls, "setToolTip", "toolTip")

    def last_str(args):
        for i in range(len(args) - 1, -1, -1):
            if isinstance(args[i], str):
                return i
        return None

    def first_str(args):
        for i, a in enumerate(args):
            if isinstance(a, str):
                return i
        return None

    for cls in (W.QMenu, W.QMenuBar, W.QToolBar):
        _wrap_maker(cls, "addAction", first_str, "setText")
    for cls in (W.QMenu, W.QMenuBar):
        _wrap_maker(cls, "addMenu", first_str, "setTitle")
    _wrap_maker(W.QMenu, "addSection", first_str, "setText")
    _wrap_maker(W.QTabWidget, "addTab", last_str, "setTabText", "tab")
    _wrap_maker(W.QTabWidget, "insertTab", last_str, "setTabText", "tab")
    _wrap_maker(W.QTabBar, "addTab", last_str, "setTabText", "tab")
    _wrap_maker(W.QTabBar, "insertTab", last_str, "setTabText", "tab")

    def combo_text(args):
        # addItem(text, data) / addItem(icon, text, data) / insertItem(i, ..)
        # Only with item data: an item without data is a value that the
        # code reads with currentText(), so it keeps its text.
        strs = [i for i, a in enumerate(args) if isinstance(a, str)]
        if strs and len(args) > strs[0] + 1:
            return strs[0]
        return None

    _wrap_maker(W.QComboBox, "addItem", combo_text, "setItemText", "combo")
    _wrap_maker(W.QComboBox, "insertItem", combo_text, "setItemText",
                "combo")
    _wrap_maker(W.QListWidget, "addItem", first_str, "setText", "list")
    _wrap_maker(W.QListWidget, "insertItem",
                lambda a: 1 if len(a) > 1 and isinstance(a[1], str) else None,
                "setText", "list")
    _wrap_maker(W.QFormLayout, "addRow", first_str, "setText", "form")
    _wrap_maker(W.QFormLayout, "insertRow",
                lambda a: 1 if len(a) > 1 and isinstance(a[1], str) else None,
                "setText", "form")
    _wrap_header(W.QTreeWidget, "setHeaderLabels")
    _wrap_header(W.QTableWidget, "setHorizontalHeaderLabels")
    _wrap_header(W.QTableWidget, "setVerticalHeaderLabels")

    for cls, slots in (
        (W.QLabel, [(0, "setText"), ("text", "setText")]),
        (W.QPushButton, [(0, "setText"), (1, "setText"), ("text", "setText")]),
        (W.QCheckBox, [(0, "setText"), ("text", "setText")]),
        (W.QRadioButton, [(0, "setText"), ("text", "setText")]),
        (W.QGroupBox, [(0, "setTitle"), ("title", "setTitle")]),
        (W.QMenu, [(0, "setTitle"), ("title", "setTitle")]),
        (W.QDockWidget, [(0, "setWindowTitle"), ("title", "setWindowTitle")]),
        (QtGui.QAction, [(0, "setText"), (1, "setText"), ("text", "setText")]),
        (W.QListWidgetItem, [(0, "setText"), (1, "setText")]),
        (W.QTableWidgetItem, [(0, "setText"), (1, "setText")]),
        (W.QTreeWidgetItem, [(0, "__items__"), (1, "__items__")]),
    ):
        _wrap_ctor(cls, slots)

    for name in ("information", "warning", "critical", "question", "about"):
        _patch_static(W.QMessageBox, name)
    for name in ("getOpenFileName", "getOpenFileNames", "getSaveFileName",
                 "getExistingDirectory"):
        _patch_static(W.QFileDialog, name)
    for name in ("getText", "getInt", "getDouble", "getItem",
                 "getMultiLineText"):
        _patch_static(W.QInputDialog, name)


def _wrap_plain(cls, name: str) -> None:
    """Translate a known text, keep no key."""
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(self, *args, **kw):
        if args and isinstance(args[0], str):
            args = (_shown(args[0]),) + tuple(args[1:])
        return orig(self, *args, **kw)

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


def _wrap_status(cls) -> None:
    """QStatusBar.showMessage: translate, and keep the key while the
    message is shown (a live change shows it again in the new language)."""
    orig = cls.__dict__.get("showMessage")
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(self, *args, **kw):
        if args and isinstance(args[0], str):
            ts = _as_tr(args[0])
            if ts is not None:
                args = (ts,) + tuple(args[1:])
            res = orig(self, *args, **kw)
            timeout = args[1] if len(args) > 1 else kw.get("timeout", 0)
            _remember(self, "showMessage", ts, "currentMessage")
            h = _holder(self, False)
            if h is not None and ts is not None:
                h.d["timeout"] = timeout
            return res
        return orig(self, *args, **kw)

    wrapper._fs_i18n = True
    wrapper.__name__ = "showMessage"
    setattr(cls, "showMessage", wrapper)


def _redo_status(bar) -> None:
    h = _holder(bar, False)
    entry = h.d.get("showMessage") if h is not None else None
    if entry is None:
        return
    ts, shown = entry
    if bar.currentMessage() != shown:
        h.d.pop("showMessage", None)
        return
    if ts.lang != language():
        bar.showMessage(ts.again(), int(h.d.get("timeout") or 0))


def _wrap_header(cls, name: str) -> None:
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(self, labels, *args, **kw):
        tss = [(_as_tr(t) if isinstance(t, str) else None) for t in labels]
        shown = [ts if ts is not None else t for ts, t in zip(tss, labels)]
        res = orig(self, shown, *args, **kw)
        try:
            from PySide6 import QtWidgets as W
            for c, ts in enumerate(tss):
                if isinstance(self, W.QTreeWidget):
                    item, col = self.headerItem(), c
                elif name == "setVerticalHeaderLabels":
                    item, col = self.verticalHeaderItem(c), 0
                else:
                    item, col = self.horizontalHeaderItem(c), 0
                if item is not None:
                    gargs = (col,) if isinstance(self, W.QTreeWidget) else ()
                    _remember(item, "setText", ts, "text", col, *gargs)
        except RuntimeError:
            pass
        return res

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


# ---------------------------------------------------------------- translate
def _redo(obj, slot: str, getter: str | None, *, col=None, gargs=(),
          setter_args=(), fallback: bool = True) -> None:
    """Show the text of `slot` of `obj` in the current language.

    The kept key wins. Without a key (a constructor text that came in
    before install(), a text from Qt itself), the shown text is looked up.
    """
    h = _holder(obj, False, col)
    entry = h.d.get(slot) if h is not None else None
    try:
        now = getattr(obj, getter)(*gargs) if getter else None
    except (AttributeError, RuntimeError, TypeError):
        return
    if entry is not None:
        ts, shown = entry
        if getter and now != shown:
            h.d.pop(slot, None)             # the code changed the text
            entry = None
        else:
            if ts.lang == language():
                return
            new = ts.again()
    if entry is None:
        if not fallback or not isinstance(now, str) or not now:
            return
        new = _as_tr(now)
        if new is None:
            return
    try:
        getattr(obj, slot)(*setter_args, new)
    except (AttributeError, RuntimeError, TypeError):
        pass


def translate_action(act) -> None:
    for put in ("setText", "setStatusTip", "setWhatsThis"):
        _redo(act, put, _GETTER[put])
    # Without an own value, Qt makes the tool tip and the icon text from
    # the text: then only a kept key is used (a value set here would stop
    # that).
    for put in ("setToolTip", "setIconText"):
        _redo(act, put, _GETTER[put], fallback=False)
    menu = act.menu() if hasattr(act, "menu") else None
    if menu is not None:
        _redo(menu, "setTitle", "title")


def _redo_indexed(w, i: int, put: str, getter: str) -> None:
    """A tab text or a combo item text."""
    from PySide6 import QtWidgets as W
    if isinstance(w, W.QTabWidget):
        page = w.widget(i)
        h = _holder(page, False) if page is not None else None
        entry = h.d.get("tab:" + put) if h is not None else None
    elif isinstance(w, W.QComboBox):
        h = w.itemData(i, ROLE)
        entry = h.d.get(put) if isinstance(h, _Keys) else None
    else:
        h = _holder(w, False)
        entry = h.d.get(f"tab{i}:" + put) if h is not None else None
    now = getattr(w, getter)(i)
    if entry is not None:
        ts, shown = entry
        if now != str.__str__(ts) and now != shown:
            entry = None
        elif ts.lang == language():
            return
        else:
            new = ts.again()
    if entry is None:
        if not now:
            return
        new = _as_tr(now)
        if new is None:
            return
    if str.__str__(new) != now or entry is not None:
        getattr(w, put)(i, new)


def _items_of_tree(tree):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        if it is None:
            continue
        yield it
        stack.extend(it.child(i) for i in range(it.childCount()))


def _redo_item(item, columns: int, tree: bool) -> None:
    for c in range(columns):
        gargs = (c,) if tree else ()
        sargs = (c,) if tree else ()
        _redo(item, "setText", "text", col=c if tree else 0, gargs=gargs,
              setter_args=sargs)
        _redo(item, "setToolTip", "toolTip", col=c if tree else 0,
              gargs=gargs, setter_args=sargs)


def translate_widget(w) -> None:
    """Show the own texts of one widget (not of its children) in the
    current language."""
    from PySide6 import QtWidgets as W

    hook = getattr(w, "retranslate_ui", None)
    if callable(hook):
        # The widget makes its own texts (they are composed): it only
        # needs the hook (a live change), and its actions.
        if _live is not None:
            _live.append(hook)
        for act in w.actions():
            translate_action(act)
        return
    for put in ("setToolTip", "setStatusTip", "setWhatsThis",
                "setWindowTitle"):
        _redo(w, put, _GETTER[put])
    if isinstance(w, (W.QLabel, W.QAbstractButton)):
        _redo(w, "setText", "text")
    elif isinstance(w, W.QGroupBox):
        _redo(w, "setTitle", "title")
    elif isinstance(w, W.QLineEdit):
        _redo(w, "setPlaceholderText", "placeholderText")
    elif isinstance(w, (W.QPlainTextEdit, W.QTextEdit)):
        _redo(w, "setPlaceholderText", "placeholderText")
        h = _holder(w, False)
        if h is not None:
            for put in ("setHtml", "setPlainText"):
                if put in h.d:
                    _redo(w, put, _GETTER[put])
    elif isinstance(w, (W.QSpinBox, W.QDoubleSpinBox)):
        _redo(w, "setPrefix", "prefix")
        _redo(w, "setSuffix", "suffix")
        _redo(w, "setSpecialValueText", "specialValueText")
    elif isinstance(w, W.QMenu):
        _redo(w, "setTitle", "title")
    elif isinstance(w, W.QMessageBox):
        for put in ("setText", "setInformativeText"):
            _redo(w, put, _GETTER[put])
    elif isinstance(w, W.QTabWidget):
        for i in range(w.count()):
            _redo_indexed(w, i, "setTabText", "tabText")
            _redo_indexed(w, i, "setTabToolTip", "tabToolTip")
    elif isinstance(w, W.QTabBar):
        parent = w.parent()
        if not isinstance(parent, W.QTabWidget):
            for i in range(w.count()):
                _redo_indexed(w, i, "setTabText", "tabText")
                _redo_indexed(w, i, "setTabToolTip", "tabToolTip")
    elif isinstance(w, W.QComboBox):
        for i in range(w.count()):
            if w.itemData(i) is None and not isinstance(
                    w.itemData(i, ROLE), _Keys):
                continue        # the text is the value (see combo_text)
            _redo_indexed(w, i, "setItemText", "itemText")
            tip = w.itemData(i, 3)          # Qt.ToolTipRole
            if isinstance(tip, str) and tip:
                ts = _as_tr(tip)
                if ts is not None and str.__str__(ts) != tip:
                    w.setItemData(i, ts, 3)
    elif isinstance(w, W.QTreeWidget):
        head = w.headerItem()
        n = w.columnCount()
        if head is not None:
            _redo_item(head, n, True)
        for it in _items_of_tree(w):
            _redo_item(it, n, True)
    elif isinstance(w, W.QTableWidget):
        for c in range(w.columnCount()):
            item = w.horizontalHeaderItem(c)
            if item is not None:
                _redo_item(item, 1, False)
        for r in range(w.rowCount()):
            item = w.verticalHeaderItem(r)
            if item is not None:
                _redo_item(item, 1, False)
            for c in range(w.columnCount()):
                item = w.item(r, c)
                if item is not None:
                    _redo_item(item, 1, False)
    elif isinstance(w, W.QListWidget):
        for r in range(w.count()):
            _redo_item(w.item(r), 1, False)
    elif isinstance(w, W.QStatusBar):
        _redo_status(w)
    if isinstance(w, W.QAbstractButton) and hasattr(w, "defaultAction"):
        act = w.defaultAction()
        if act is not None:
            translate_action(act)
    for act in w.actions():
        translate_action(act)


# The retranslate_ui hooks of the widgets, while retranslate_all runs (a
# live change); they run after all widgets have their new texts.
_live: list | None = None


def translate_tree(root) -> None:
    """Translate `root` and all its child widgets."""
    from PySide6 import QtWidgets as W

    if root is None:
        return
    translate_widget(root)
    for w in root.findChildren(W.QWidget):
        translate_widget(w)


def retranslate_all() -> None:
    """Show each widget of the program in the current language (a live
    change of the language)."""
    global _live
    from PySide6 import QtWidgets as W
    app = W.QApplication.instance()
    if app is None:
        return
    _live = hooks = []
    try:
        for w in app.allWidgets():
            try:
                translate_widget(w)
            except RuntimeError:
                pass
    finally:
        _live = None
    for hook in hooks:
        try:
            hook()
        except RuntimeError:
            pass


def _make_filter():
    from PySide6 import QtCore

    class _ShowFilter(QtCore.QObject):
        """Translates each widget when Qt shows it."""

        def eventFilter(self, obj, event):              # noqa: N802
            try:
                if event.type() == QtCore.QEvent.Show and obj.isWidgetType():
                    translate_widget(obj)
            except RuntimeError:
                pass            # the C++ object is already gone
            return False

    return _ShowFilter()


_filter = None


def install(app=None) -> None:
    """Patch Qt so that the interface shows the selected language, and a
    live change of the language can translate each widget again.

    Call it one time, before the windows are built (MainWindow does this).
    """
    global _installed, _filter
    from PySide6 import QtWidgets

    if not _installed:
        _patch_qt()
        _installed = True
    app = app or QtWidgets.QApplication.instance()
    if app is not None and _filter is None:
        _filter = _make_filter()
        app.installEventFilter(_filter)
