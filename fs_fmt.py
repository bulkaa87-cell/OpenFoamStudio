"""Numbers, sizes, times for the person, in the interface language (T51, K9).

One place for the formats that were written again in many modules
(fs_library, fs_prerun, fs_queue, fs_timesdlg, fs_meshfix_app, the main
window). No Qt.

- `count(n)`: a whole number with groups of three digits (en «12,345»,
  uk «12 345»).
- `decimal(x, digits)`: a decimal number (en «1.25», uk «1,25»).
- `bytes_text(n)`, `duration(s)`, `stamp(t)`, `ago(s)`: a size, a time
  span, a date and time, «how long ago».

Put the result into `tr()` as a value (`tr("{n} cells", n=count(n))`). Do
not change a translated text afterwards (`.replace(",", " ")` makes a plain
`str`, and the text then stays in the old language after a live change of
the language).
"""

from __future__ import annotations

import time

import fs_i18n
from fs_i18n import tr

EMPTY = "—"


def _uk() -> bool:
    return fs_i18n.language() == "uk"


def count(n) -> str:
    """A whole number with groups of three digits."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if _uk() else text


def decimal(x: float, digits: int = 1) -> str:
    """A decimal number with `digits` digits after the point (uk: a comma)."""
    text = f"{float(x):,.{int(digits)}f}"
    if _uk():
        text = text.replace(",", " ").replace(".", ",")
    return text


def bytes_text(n) -> str:
    """A size in bytes: B, kB, MB, GB (powers of 1000)."""
    n = float(n or 0)
    if n <= 0:
        return EMPTY
    if n >= 1e9:
        return tr("{v} GB", v=decimal(n / 1e9, 1))
    if n >= 1e6:
        return tr("{v} MB", v=decimal(n / 1e6, 1 if n < 1e7 else 0))
    if n >= 1e3:
        return tr("{v} kB", v=decimal(n / 1e3, 0))
    return tr("{v} B", v=count(n))


def duration(s) -> str:
    """A time span: ms, s, min, h."""
    s = float(s or 0)
    if s <= 0:
        return EMPTY
    if s < 0.1:
        # A fast time step (2 ms on a small mesh): «0.00 s» said nothing.
        ms = s * 1000
        return tr("{v} ms", v=decimal(ms, 1 if ms < 10 else 0))
    if s < 10:
        return tr("{v} s", v=decimal(s, 1 if s >= 1 else 2))
    if s < 90:
        return tr("{v} s", v=decimal(s, 0))
    if s < 3600:
        return tr("{v} min", v=decimal(s / 60, 1))
    return tr("{v} h", v=decimal(s / 3600, 1))


def stamp(t, empty: str = EMPTY) -> str:
    """A date and time (local), «YYYY-MM-DD HH:MM»; `empty` for none."""
    if not t:
        return empty
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))


def ago(seconds) -> str:
    """How long ago, in short words."""
    seconds = int(seconds)
    if seconds < 0:
        return tr("some time ago")
    if seconds < 90:
        return tr("just now")
    if seconds < 5400:
        return tr("{v} min ago", v=seconds // 60)
    return tr("{v} h ago", v=seconds // 3600)
