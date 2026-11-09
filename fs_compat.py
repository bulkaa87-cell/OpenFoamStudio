"""Обхід несумісності six + PySide6. Імпортувати ПЕРШИМ.

PySide6 при імпорті ставить import-hook (shibokensupport.feature), який на
кожному новоімпортованому модулі кличе inspect.getsource. Псевдопакет
six.moves подається як namespace-модуль, і його repr вимагає атрибута _path
у завантажувача — а в six._SixMetaPathImporter його немає. Наслідок:

    AttributeError: '_SixMetaPathImporter' object has no attribute '_path'

Падає будь-який імпорт six.moves після PySide6, зокрема matplotlib через
matplotlib.category -> dateutil.parser -> dateutil.tz.

Порядок імпортів як обхідний шлях ненадійний: досить, щоб PySide6 завантажив
хтось інший раніше. Тому додаємо відсутній атрибут напряму.
"""

from __future__ import annotations


def apply() -> bool:
    """Повертає True, якщо шим застосовано або він не потрібен."""
    try:
        import six
    except Exception:
        return True
    cls = getattr(six, "_SixMetaPathImporter", None)
    if cls is None:
        return True
    if not hasattr(cls, "_path"):
        try:
            cls._path = []
        except Exception:
            return False
    return True


apply()
