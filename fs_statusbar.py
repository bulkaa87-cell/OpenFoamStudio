"""Рядок стану, у якому тексти НІКОЛИ не лягають один на один.

Звідки це (T13b, 2026-11-10). На знімку користувача тимчасове повідомлення
«Режим новачка увімкнено…» було намальоване поверх рядка «Re … | M … |
комірок …». Причина в Qt: `QStatusBar.showMessage()` ховає звичайні
віджети рядка лише тоді, коли вони ВИДИМІ в цю мить. Вікно ще не показане
(повідомлення йде з `__init__`, коли `act_rookie.setChecked()` вмикає режим
новачка), тож мітку не сховано, а `show()` вікна потім показав її разом із
повідомленням. Вісім секунд два тексти лежали один на одному.

Друга половина: звичайна QLabel вимагає ширини під УВЕСЬ свій текст. Довгий
текст у вузькому вікні або розпирав вікно, або не вміщався, а повідомлення
Qt просто обрізав без «…».

Тут:
- `StatusText` — мітка, що скорочує текст трикрапкою, коли місця мало, а
  повний текст показує в підказці. Її мінімум — одна «…», тож вона не
  розпирає вікно.
- `StatusBar` — сам ховає звичайні мітки, поки є повідомлення, і показує
  назад, коли воно зникло, хоч би коли його показали. Повідомлення
  малює сам, скороченим, лише в місці лівіше від постійних міток.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from fs_i18n import tr

Qt = QtCore.Qt
_NO_LIMIT = 16777215          # QWIDGETSIZE_MAX


class StatusText(QtWidgets.QLabel):
    """Однорядкова мітка рядка стану, що скорочує текст трикрапкою.

    `text()` повертає ПОВНИЙ текст. Коли текст скорочено, підказка
    показує повний текст, а під ним — підказку, задану через `setToolTip`.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self._tip = ""
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Preferred)
        self.setText(text)

    # --- текст і підказка ------------------------------------------------
    def setText(self, text: str) -> None:                  # noqa: N802
        text = "" if text is None else tr(str(text))
        if text == self._full and super().text():
            return
        self._full = text
        self._fit()
        self.updateGeometry()

    def text(self) -> str:
        return self._full

    def setToolTip(self, tip: str) -> None:                 # noqa: N802
        self._tip = tr(tip or "")
        self._fit()

    def is_elided(self) -> bool:
        return super().text() != self._full

    # --- розмір ----------------------------------------------------------
    def _extra(self) -> int:
        m = self.contentsMargins()
        # +4: запас на округлення elidedText, інакше текст рівно у свою
        # ширину все одно втрачав кінець («(4 з …»).
        return m.left() + m.right() + 2 * self.margin() + 4

    def sizeHint(self):                                     # noqa: N802
        fm = self.fontMetrics()
        h = super().sizeHint().height()
        return QtCore.QSize(fm.horizontalAdvance(self._full) + self._extra(), h)

    def minimumSizeHint(self):                              # noqa: N802
        if not self._full:
            return QtCore.QSize(0, super().minimumSizeHint().height())
        fm = self.fontMetrics()
        h = super().minimumSizeHint().height()
        return QtCore.QSize(fm.horizontalAdvance("…") + self._extra(), h)

    def _fit(self) -> None:
        width = self.contentsRect().width() - 2 * self.margin()
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight,
                                              max(0, width))
        if super().text() != shown:
            super().setText(shown)
        if shown != self._full:
            tip = self._full + (f"\n\n{self._tip}" if self._tip else "")
        else:
            tip = self._tip
        super().setToolTip(tip)

    def resizeEvent(self, event):                           # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def changeEvent(self, event):                           # noqa: N802
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange,
                            QtCore.QEvent.StyleChange):
            self.updateGeometry()
            self._fit()


class StatusBar(QtWidgets.QStatusBar):
    """QStatusBar, у якого повідомлення не лягає поверх міток.

    Звичайні віджети (`addWidget`) ховаються, поки показане повідомлення,
    і вертаються, коли воно зникло. Повідомлення займає лише місце лівіше
    від першого видимого постійного віджета і скорочується трикрапкою.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._normal: list[QtWidgets.QWidget] = []
        self._perm: list[QtWidgets.QWidget] = []
        # Віджет, який код сам сховав (setVisible(False)), не повертаємо.
        self._held: set[int] = set()
        self.messageChanged.connect(self._sync)

    def addWidget(self, widget, stretch: int = 0):          # noqa: N802
        self._normal.append(widget)
        super().addWidget(widget, stretch)
        self._sync(self.currentMessage())

    def insertWidget(self, index, widget, stretch: int = 0):  # noqa: N802
        self._normal.append(widget)
        r = super().insertWidget(index, widget, stretch)
        self._sync(self.currentMessage())
        return r

    def addPermanentWidget(self, widget, stretch: int = 0):  # noqa: N802
        self._perm.append(widget)
        super().addPermanentWidget(widget, stretch)

    def insertPermanentWidget(self, index, widget, stretch: int = 0):  # noqa: N802
        self._perm.append(widget)
        return super().insertPermanentWidget(index, widget, stretch)

    def removeWidget(self, widget):                         # noqa: N802
        for lst in (self._normal, self._perm):
            if widget in lst:
                lst.remove(widget)
        self._held.discard(id(widget))
        super().removeWidget(widget)

    def _sync(self, message: str) -> None:
        """Звичайні мітки — сховані, поки є повідомлення. Завжди, а не лише
        коли вони видимі в мить `showMessage` (так робить сам Qt)."""
        for w in self._normal:
            if message:
                if not w.isHidden() and id(w) not in self._held:
                    w.setVisible(False)
                    self._held.add(id(w))
            elif id(w) in self._held:
                self._held.discard(id(w))
                w.setVisible(True)
        self._share()
        self.update()

    def _share(self) -> None:
        """Поки є повідомлення, постійні мітки разом беруть не більше
        половини рядка (кожна — пропорційно своєму тексту), щоб для
        повідомлення лишалося місце. Без повідомлення — без обмежень."""
        message = self.currentMessage()
        shown = [w for w in self._perm if not w.isHidden()]
        need = sum(w.sizeHint().width() for w in shown)
        budget = self.width() // 2
        for w in self._perm:
            if not message or w not in shown:
                cap = _NO_LIMIT
            elif need <= budget:
                cap = w.sizeHint().width()
            else:
                cap = max(w.minimumSizeHint().width(),
                          budget * w.sizeHint().width() // max(1, need))
            if w.maximumWidth() != cap:
                w.setMaximumWidth(cap)

    def resizeEvent(self, event):                           # noqa: N802
        super().resizeEvent(event)
        if self.currentMessage():
            self._share()

    def message_rect(self) -> QtCore.QRect:
        """Місце для повідомлення: від лівого краю до першої видимої
        постійної мітки (так само рахує й Qt)."""
        rtl = self.layoutDirection() == Qt.RightToLeft
        left, right = 6, self.width() - 12
        for w in self._perm:
            if w.isVisibleTo(self) and not w.isHidden():
                g = w.geometry()
                if rtl:
                    left = max(left, g.right() + 3)
                else:
                    right = min(right, g.left() - 2)
                break
        return QtCore.QRect(left, 0, max(0, right - left), self.height())

    def paintEvent(self, event):                            # noqa: N802
        message = self.currentMessage()
        if not message:
            super().paintEvent(event)
            return
        # Те саме, що QStatusBar::paintEvent, але текст скорочений і лише
        # у своєму місці.
        p = QtGui.QPainter(self)
        opt = QtWidgets.QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QtWidgets.QStyle.PE_PanelStatusBar, opt, p, self)
        for w in self._perm:
            if w.isVisible():
                opt.rect = w.geometry().adjusted(-2, -1, 2, 1)
                self.style().drawPrimitive(QtWidgets.QStyle.PE_FrameStatusBarItem,
                                           opt, p, w)
        rect = self.message_rect()
        text = self.fontMetrics().elidedText(message, Qt.ElideRight, rect.width())
        p.setPen(self.palette().windowText().color())
        p.drawText(rect, Qt.AlignLeading | Qt.AlignVCenter | Qt.TextSingleLine, text)
        p.end()
        tip = message if text != message else ""
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def message_shown(self) -> str:
        """Текст повідомлення так, як його намальовано (зі скороченням)."""
        message = self.currentMessage()
        if not message:
            return ""
        return self.fontMetrics().elidedText(message, Qt.ElideRight,
                                             self.message_rect().width())
