"""Interface languages: Ukrainian (uk, the source language) and English (en).

How it works
------------
* The key of a translation is the Ukrainian source text itself. The code
  keeps the Ukrainian text where it is, so the code stays readable and the
  tests that look for Ukrainian labels work in the default language.
* The catalogues are in ``i18n/<lang>/<module>.json``: one JSON object
  ``{"Ukrainian text": "translation"}`` for each module of the program. All
  files of one language are merged at run time. One file for each module
  keeps merge conflicts small when several people change several modules.
* ``tr("Комірок: {n:,}", n=cells)`` translates a text with values. Use it in
  place of an f-string. Without arguments, ``tr(text)`` only looks the text
  up (it does not call ``str.format``), so it is also safe for a text that
  comes from data, for example a step title.
* ``install()`` translates the static texts of Qt widgets: it patches the
  text setters of Qt (``setText``, ``setToolTip``, ``showMessage``, …) and
  installs an event filter that translates each widget when it is shown
  (labels, buttons, group titles, tabs, menus, actions, combo-box items
  that have item data, tree headers, window titles). Thus a constant text
  in a constructor (``QLabel("Сітка")``) needs no code change.
* In Ukrainian, nothing is patched and ``tr`` only formats.

The language
------------
``FS_LANG`` (environment) wins, if it is set: tests and a quick look use it.
Then ``fs_settings.get("ui_language", "uk")`` («Файл → Налаштування»). Then
``uk``. The language is read one time; a change needs a restart.

The check
---------
``python tools/i18n_check.py`` lists each Ukrainian text in the code that
has no English entry, each f-string with Ukrainian text that is not wrapped
in ``tr()``, and each translation with other ``{placeholders}`` than its
source. See CLAUDE.md, «Interface languages».
"""

from __future__ import annotations

import json
import os
import string
from pathlib import Path

SOURCE = "uk"
LANGUAGES = {"uk": "Українська", "en": "English"}
I18N_DIR = Path(__file__).resolve().with_name("i18n")

_lang: str | None = None
_cat: dict[str, str] | None = None
_installed = False
_missing: set[str] = set()


# ---------------------------------------------------------------- language
def _from_settings() -> str | None:
    try:
        import fs_settings  # type: ignore  # «Файл → Налаштування» (T22)
    except ImportError:
        return None
    try:
        return fs_settings.get("ui_language", SOURCE)
    except Exception:
        return None


def language() -> str:
    """The interface language: "uk" or "en"."""
    global _lang
    if _lang is None:
        lang = (os.environ.get("FS_LANG") or "").strip().lower()
        if not lang:
            lang = (_from_settings() or SOURCE).strip().lower()
        _lang = lang if lang in LANGUAGES else SOURCE
    return _lang


def set_language(lang: str | None) -> None:
    """Select the language for this process (tests). None reads it again.

    Widgets that exist keep their texts. The Qt patches of ``install()``
    are installed only if the language is not the source language.
    """
    global _lang, _cat
    _lang = None if lang is None else (lang if lang in LANGUAGES else SOURCE)
    _cat = None


def load_catalogue(lang: str) -> dict[str, str]:
    """All entries of ``i18n/<lang>/*.json``, merged."""
    out: dict[str, str] = {}
    folder = I18N_DIR / lang
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k, v in data.items():
            # "" is a valid translation (a suffix that English does
            # not need); a missing entry is not.
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v
    return out


def _catalogue() -> dict[str, str]:
    global _cat
    if _cat is None:
        lang = language()
        _cat = {} if lang == SOURCE else load_catalogue(lang)
    return _cat


# ---------------------------------------------------------------- lookup
def lookup(text: str) -> str | None:
    """The translation of `text`, or None (also in Ukrainian)."""
    if not text or language() == SOURCE:
        return None
    hit = _catalogue().get(text)
    if hit is None and _DEBUG and _cyr(text):
        _note_missing(text)
    return hit


def _cyr(text: str) -> bool:
    return any(0x400 <= ord(c) <= 0x4FF for c in text)


def tr(_text: str, /, *args, **kw) -> str:
    """Translate `_text`; with arguments, also ``str.format`` them in.

    Use it in place of an f-string::

        tr("Комірок: {n:,}", n=cells)      # was f"Комірок: {cells:,}"
    """
    if not isinstance(_text, str):
        return _text
    out = _text
    if language() != SOURCE:
        out = _catalogue().get(_text, _text)
        if out is _text and _DEBUG and _cyr(_text):
            _note_missing(_text)
    if not args and not kw:
        return out
    if out is not _text or language() != SOURCE:
        # A value can be a Ukrainian text too (a model name, a part of a
        # sentence): translate it if the catalogue has it.
        args = tuple(_sub(a) if isinstance(a, str) else a for a in args)
        kw = {k: (_sub(v) if isinstance(v, str) else v)
              for k, v in kw.items()}
    try:
        return out.format(*args, **kw)
    except (KeyError, IndexError, ValueError):
        # A bad placeholder in a translation must not break the program:
        # show the source text.
        return _text.format(*args, **kw)


def plural(n: int, one: str, few: str, many: str) -> str:
    """The noun form for `n`: ``plural(n, "шар", "шари", "шарів")``.

    Ukrainian selects one of three forms. English has two: the translation
    of `one` for n == 1, else the translation of `many`.
    """
    if language() == SOURCE:
        n10, n100 = abs(n) % 10, abs(n) % 100
        if n10 == 1 and n100 != 11:
            return one
        if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
            return few
        return many
    return tr(one) if abs(n) == 1 else tr(many)


def localized(path):
    """The file of `path` in the interface language, if there is one:
    SLOVNYK.html -> SLOVNYK.en.html. Else `path` itself."""
    lang = language()
    if lang == SOURCE:
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
# FS_I18N_DEBUG=1: write each Ukrainian text that reached a widget without a
# translation to i18n/missing-<lang>.txt. It finds texts that the code
# composes from parts, which the static check cannot see.
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
def _sub(text):
    if isinstance(text, str) and text:
        hit = lookup(text)
        if hit is not None:
            return hit
    return text


def _patch(cls, name: str, text_args: tuple[int, ...] | None = None,
           need_data: bool = False) -> None:
    """Replace ``cls.name`` with a version that translates its str args.

    `text_args`: positions of the text arguments (None = each str arg).
    `need_data`: QComboBox.addItem: translate only when item data is given.
    The code reads ``currentText()`` of a combo box without data as a value
    (units, media), so such an item must keep its source text.
    """
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(self, *args, **kw):
        if args:
            strs = [i for i, a in enumerate(args) if isinstance(a, str)]
            if need_data and not (strs and len(args) > strs[0] + 1):
                return orig(self, *args, **kw)
            if need_data:
                pick = strs[:1]     # the text; item data stays as it is
            elif text_args is None:
                pick = strs
            else:
                pick = [i for i in text_args if i in strs]
            if pick:
                args = list(args)
                for i in pick:
                    args[i] = _sub(args[i])
        return orig(self, *args, **kw)

    wrapper._fs_i18n = True
    wrapper.__name__ = name
    setattr(cls, name, wrapper)


def _patch_static(cls, name: str) -> None:
    orig = cls.__dict__.get(name)
    if orig is None or getattr(orig, "_fs_i18n", False):
        return

    def wrapper(*args, **kw):
        args = [_sub(a) if isinstance(a, str) else a for a in args]
        for k in ("caption", "title", "text", "label", "filter"):
            if isinstance(kw.get(k), str):
                kw[k] = _sub(kw[k])
        return orig(*args, **kw)

    wrapper._fs_i18n = True
    setattr(cls, name, staticmethod(wrapper))


def _patch_qt() -> None:
    from PySide6 import QtGui, QtWidgets as W

    for cls, names in (
        (W.QWidget, ("setToolTip", "setWindowTitle", "setStatusTip",
                     "setWhatsThis")),
        (W.QLabel, ("setText",)),
        (W.QAbstractButton, ("setText",)),
        (W.QGroupBox, ("setTitle",)),
        (W.QLineEdit, ("setPlaceholderText",)),
        (W.QPlainTextEdit, ("setPlaceholderText", "appendPlainText",
                            "setPlainText")),
        (W.QTextEdit, ("setPlaceholderText", "setHtml", "setPlainText",
                       "append")),
        (W.QStatusBar, ("showMessage",)),
        (W.QProgressDialog, ("setLabelText", "setCancelButtonText")),
        (W.QMessageBox, ("setText", "setInformativeText",
                         "setDetailedText")),
        (W.QMenu, ("setTitle", "addMenu", "addAction", "addSection")),
        (W.QMenuBar, ("addMenu", "addAction")),
        (W.QToolBar, ("addAction",)),
        (W.QTabWidget, ("setTabText", "setTabToolTip", "addTab",
                        "insertTab")),
        (W.QTabBar, ("setTabText", "setTabToolTip", "addTab", "insertTab")),
        (W.QTreeWidgetItem, ("setText", "setToolTip")),
        (W.QListWidgetItem, ("setText", "setToolTip")),
        (W.QTableWidgetItem, ("setText", "setToolTip")),
        (W.QComboBox, ("setItemText",)),
        (W.QSpinBox, ("setPrefix", "setSuffix")),
        (W.QDoubleSpinBox, ("setPrefix", "setSuffix")),
        (W.QAbstractSpinBox, ("setSpecialValueText",)),
        (W.QDockWidget, ("setWindowTitle",)),
        (W.QFormLayout, ("addRow", "insertRow")),
        (QtGui.QAction, ("setText", "setToolTip", "setStatusTip",
                         "setIconText", "setWhatsThis")),
    ):
        for name in names:
            _patch(cls, name)
    _patch(W.QComboBox, "addItem", need_data=True)
    _patch(W.QComboBox, "insertItem", need_data=True)
    _patch(W.QTreeWidget, "setHeaderLabels")
    _patch(W.QTableWidget, "setHorizontalHeaderLabels")
    for name in ("information", "warning", "critical", "question", "about"):
        _patch_static(W.QMessageBox, name)
    for name in ("getOpenFileName", "getOpenFileNames", "getSaveFileName",
                 "getExistingDirectory"):
        _patch_static(W.QFileDialog, name)
    for name in ("getText", "getInt", "getDouble", "getItem",
                 "getMultiLineText"):
        _patch_static(W.QInputDialog, name)


def _setter(obj, get: str, put: str) -> None:
    try:
        text = getattr(obj, get)()
    except (AttributeError, RuntimeError):
        return
    hit = _sub(text)
    if hit != text:
        getattr(obj, put)(hit)


def translate_action(act) -> None:
    for get, put in (("text", "setText"), ("toolTip", "setToolTip"),
                     ("statusTip", "setStatusTip"),
                     ("whatsThis", "setWhatsThis")):
        _setter(act, get, put)
    menu = act.menu() if hasattr(act, "menu") else None
    if menu is not None:
        _setter(menu, "title", "setTitle")


def translate_widget(w) -> None:
    """Translate the own texts of one widget (not of its children)."""
    from PySide6 import QtWidgets as W

    if language() == SOURCE:
        return
    for get, put in (("toolTip", "setToolTip"), ("statusTip", "setStatusTip"),
                     ("whatsThis", "setWhatsThis"),
                     ("windowTitle", "setWindowTitle")):
        _setter(w, get, put)
    if isinstance(w, (W.QLabel, W.QAbstractButton)):
        _setter(w, "text", "setText")
    elif isinstance(w, W.QGroupBox):
        _setter(w, "title", "setTitle")
    elif isinstance(w, W.QLineEdit):
        _setter(w, "placeholderText", "setPlaceholderText")
    elif isinstance(w, (W.QPlainTextEdit, W.QTextEdit)):
        _setter(w, "placeholderText", "setPlaceholderText")
    elif isinstance(w, (W.QSpinBox, W.QDoubleSpinBox)):
        _setter(w, "prefix", "setPrefix")
        _setter(w, "suffix", "setSuffix")
        _setter(w, "specialValueText", "setSpecialValueText")
    elif isinstance(w, W.QTabBar):
        for i in range(w.count()):
            for get, put in (("tabText", "setTabText"),
                             ("tabToolTip", "setTabToolTip")):
                text = getattr(w, get)(i)
                hit = _sub(text)
                if hit != text:
                    getattr(w, put)(i, hit)
    elif isinstance(w, W.QComboBox):
        for i in range(w.count()):
            if w.itemData(i) is None:
                continue        # the text is the value (see _patch)
            text = w.itemText(i)
            hit = _sub(text)
            if hit != text:
                w.setItemText(i, hit)
            tip = w.itemData(i, 3)          # Qt.ToolTipRole
            if isinstance(tip, str):
                hit = _sub(tip)
                if hit != tip:
                    w.setItemData(i, hit, 3)
    elif isinstance(w, W.QTreeWidget):
        head = w.headerItem()
        for c in range(w.columnCount()):
            text = head.text(c)
            hit = _sub(text)
            if hit != text:
                head.setText(c, hit)
    elif isinstance(w, W.QTableWidget):
        for c in range(w.columnCount()):
            item = w.horizontalHeaderItem(c)
            if item is not None:
                hit = _sub(item.text())
                if hit != item.text():
                    item.setText(hit)
    if isinstance(w, W.QAbstractButton) and hasattr(w, "defaultAction"):
        act = w.defaultAction()
        if act is not None:
            translate_action(act)
    for act in w.actions():
        translate_action(act)


def translate_tree(root) -> None:
    """Translate `root` and all its child widgets."""
    from PySide6 import QtWidgets as W

    if language() == SOURCE or root is None:
        return
    translate_widget(root)
    for w in root.findChildren(W.QWidget):
        translate_widget(w)


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
    """Translate the Qt interface into the selected language.

    Call it one time, before the windows are built (MainWindow does this).
    In Ukrainian it does nothing.
    """
    global _installed, _filter
    if language() == SOURCE:
        return
    from PySide6 import QtWidgets

    if not _installed:
        _patch_qt()
        _installed = True
    app = app or QtWidgets.QApplication.instance()
    if app is not None and _filter is None:
        _filter = _make_filter()
        app.installEventFilter(_filter)
