"""The report window of the pre-run (fs_prerun.Report).

For each value: what the pre-run measured, the current value, the proposed
value (the person can edit it) and why. The person selects the values to
change, then starts the main run, or starts it without changes, or stops.
Nothing changes without a click: the window only returns the values.
"""

from __future__ import annotations

from PySide6 import QtWidgets

import fs_i18n
from fs_i18n import tr

fs_i18n.install()

# Readable on a light and on a dark palette.
_COLORS = {"bad": "#d84343", "warn": "#d08a1c", "info": "#3a9cc0",
           "ok": "#3fa35b"}


def _fmt(v) -> str:
    if isinstance(v, bool) or v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class PreRunDialog(QtWidgets.QDialog):
    """`action` after exec(): "apply", "skip" or "" (cancel)."""

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.action = ""
        self._edits: dict[str, tuple] = {}     # key -> (check box, editor)
        self.setWindowTitle(tr("Pre-run Results"))
        self.setMinimumWidth(720)
        v = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(report.headline)
        head.setWordWrap(True)
        color = {"diverged": _COLORS["bad"], "failed": _COLORS["bad"],
                 "warn": _COLORS["warn"]}.get(report.outcome, _COLORS["ok"])
        head.setStyleSheet(f"font-size:14px; font-weight:600; color:{color};")
        v.addWidget(head)
        if report.can_start:
            intro = QtWidgets.QLabel(
                "Select the values to change. You can edit each proposed "
                "value. The main run starts with the values of this window.")
            intro.setWordWrap(True)
            v.addWidget(intro)

        body = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(body)
        rows.setContentsMargins(0, 0, 0, 0)
        changes = [r for r in report.rows if r.change]
        infos = [r for r in report.rows if not r.change]
        if changes:
            rows.addWidget(self._section(tr("Proposed changes")))
            for r in changes:
                rows.addWidget(self._change_row(r))
        if infos:
            rows.addWidget(self._section(tr("Measured")))
            for r in infos:
                rows.addWidget(self._info_row(r))
        if report.notes:
            rows.addWidget(self._section(tr("Notes")))
            for n in report.notes:
                lab = QtWidgets.QLabel("• " + n)
                lab.setWordWrap(True)
                rows.addWidget(lab)
        if not changes and report.can_start:
            lab = QtWidgets.QLabel(
                "The pre-run proposes no changes: the main run can start "
                "with the current values.")
            lab.setWordWrap(True)
            rows.addWidget(lab)
        rows.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        box = QtWidgets.QDialogButtonBox()
        if report.can_start:
            self.btn_apply = box.addButton(
                tr("Apply and Start Main Run"), QtWidgets.QDialogButtonBox.AcceptRole)
            self.btn_skip = box.addButton(
                tr("Start Without Changes"), QtWidgets.QDialogButtonBox.ActionRole)
            self.btn_cancel = box.addButton(
                tr("Cancel"), QtWidgets.QDialogButtonBox.RejectRole)
            self.btn_apply.clicked.connect(lambda: self._done("apply"))
            self.btn_skip.clicked.connect(lambda: self._done("skip"))
            self.btn_apply.setDefault(True)
            if not changes:
                self.btn_apply.setText(tr("Start Main Run"))
        else:
            self.btn_cancel = box.addButton(
                tr("Close"), QtWidgets.QDialogButtonBox.RejectRole)
        self.btn_cancel.clicked.connect(self.reject)
        v.addWidget(box)
        self.resize(780, min(760, 260 + 110 * len(report.rows)
                             + 40 * len(report.notes)))

    # --- pieces -----------------------------------------------------------

    @staticmethod
    def _section(text) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setStyleSheet("font-weight:600; margin-top:8px;")
        return lab

    @staticmethod
    def _frame(level: str) -> tuple[QtWidgets.QFrame, QtWidgets.QGridLayout]:
        f = QtWidgets.QFrame()
        f.setFrameShape(QtWidgets.QFrame.StyledPanel)
        f.setStyleSheet(
            "QFrame { border-left: 3px solid %s; }"
            " QLabel, QCheckBox { border: none; }" % _COLORS.get(level, "#888"))
        g = QtWidgets.QGridLayout(f)
        g.setContentsMargins(8, 6, 8, 6)
        g.setColumnStretch(0, 1)
        return f, g

    @staticmethod
    def _small(text) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setWordWrap(True)
        f = lab.font()
        f.setPointSizeF(f.pointSizeF() * 0.95)
        lab.setFont(f)
        return lab

    def _change_row(self, r) -> QtWidgets.QFrame:
        f, g = self._frame(r.level)
        chk = QtWidgets.QCheckBox(r.title)
        chk.setChecked(True)
        chk.setStyleSheet("font-weight:600;")
        if r.kind == "int":
            ed = QtWidgets.QSpinBox()
            ed.setRange(int(r.lo), int(r.hi))
            ed.setSingleStep(max(1, int(r.step)))
            ed.setValue(int(r.proposed))
        else:
            ed = QtWidgets.QDoubleSpinBox()
            ed.setDecimals(r.decimals)
            ed.setRange(r.lo, r.hi)
            ed.setSingleStep(r.step)
            ed.setValue(float(r.proposed))
        ed.setMinimumWidth(90)
        chk.toggled.connect(ed.setEnabled)
        g.addWidget(chk, 0, 0)
        g.addWidget(QtWidgets.QLabel(tr("now {v}", v=_fmt(r.current))), 0, 1)
        g.addWidget(QtWidgets.QLabel("→"), 0, 2)
        g.addWidget(ed, 0, 3)
        g.addWidget(self._small(tr("Measured: {m}", m=r.measured)), 1, 0, 1, 4)
        g.addWidget(self._small(r.why), 2, 0, 1, 4)
        self._edits[r.key] = (chk, ed)
        return f

    def _info_row(self, r) -> QtWidgets.QFrame:
        f, g = self._frame(r.level)
        title = QtWidgets.QLabel(r.title)
        title.setStyleSheet("font-weight:600;")
        g.addWidget(title, 0, 0)
        if r.current is not None:
            g.addWidget(QtWidgets.QLabel(tr("now {v}", v=_fmt(r.current))),
                        0, 1)
        g.addWidget(self._small(r.measured), 1, 0, 1, 2)
        if r.why:
            g.addWidget(self._small(r.why), 2, 0, 1, 2)
        return f

    # --- result -----------------------------------------------------------

    def _done(self, action: str) -> None:
        self.action = action
        self.accept()

    def values(self) -> dict:
        """key -> value of the selected rows (only after "apply")."""
        if self.action != "apply":
            return {}
        return {k: ed.value() for k, (chk, ed) in self._edits.items()
                if chk.isChecked()}

    def set_checked(self, key: str, on: bool) -> None:
        """For the tests: select or clear one row."""
        self._edits[key][0].setChecked(on)

    def set_value(self, key: str, value) -> None:
        """For the tests: type a value into one row."""
        self._edits[key][1].setValue(value)
