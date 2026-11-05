"""``DotGrid``: the sentence grid as one painted QML item."""

from __future__ import annotations

import math
from typing import Any, cast

from PySide6.QtCore import Property, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickItem, QQuickPaintedItem

QML_URI = "TomecastNative"
_registered = False


class DotGrid(QQuickPaintedItem):
    """One dot per piece of text: done, the current one, and those still waiting."""

    totalChanged = Signal()
    doneChanged = Signal()
    columnsChanged = Signal()
    dotSizeChanged = Signal()
    gapChanged = Signal()
    doneColorChanged = Signal()
    currentColorChanged = Signal()
    waitColorChanged = Signal()
    completeChanged = Signal()
    descriptionChanged = Signal()

    def __init__(self, parent: QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._total = 0
        self._done = 0
        self._columns = 70
        self._dot_size = 5.0
        self._gap = 3.0
        self._done_color = QColor("#F2F2F2")
        self._current_color = QColor("#F2F2F2")
        self._wait_color = QColor("#3A3A3C")
        self._complete = False
        self.setAntialiasing(True)
        self._resize()

    # -- geometry -----------------------------------------------------------------------------

    def rows(self) -> int:
        return math.ceil(self._total / self._columns) if self._total > 0 else 0

    def _resize(self) -> None:
        pitch = self._dot_size + self._gap
        rows = self.rows()
        cols = min(self._columns, self._total) if self._total > 0 else 0
        self.setImplicitWidth(max(0.0, cols * pitch - self._gap) if cols else 0.0)
        self.setImplicitHeight(max(0.0, rows * pitch - self._gap) if rows else 0.0)

    # -- properties ---------------------------------------------------------------------------

    def _get_total(self) -> int:
        return self._total

    def _set_total(self, value: int) -> None:
        value = max(0, value)
        if value != self._total:
            self._total = value
            self._resize()
            self.update()
            self.totalChanged.emit()
            self.descriptionChanged.emit()

    def _get_done(self) -> int:
        return self._done

    def _set_done(self, value: int) -> None:
        value = max(0, value)
        if value != self._done:
            self._done = value
            self.update()
            self.doneChanged.emit()
            self.descriptionChanged.emit()

    def _get_columns(self) -> int:
        return self._columns

    def _set_columns(self, value: int) -> None:
        value = max(1, value)
        if value != self._columns:
            self._columns = value
            self._resize()
            self.update()
            self.columnsChanged.emit()

    def _get_dot_size(self) -> float:
        return self._dot_size

    def _set_dot_size(self, value: float) -> None:
        if value != self._dot_size:
            self._dot_size = value
            self._resize()
            self.update()
            self.dotSizeChanged.emit()

    def _get_gap(self) -> float:
        return self._gap

    def _set_gap(self, value: float) -> None:
        if value != self._gap:
            self._gap = value
            self._resize()
            self.update()
            self.gapChanged.emit()

    def _get_done_color(self) -> QColor:
        return self._done_color

    def _set_done_color(self, value: QColor) -> None:
        if value != self._done_color:
            self._done_color = QColor(value)
            self.update()
            self.doneColorChanged.emit()

    def _get_current_color(self) -> QColor:
        return self._current_color

    def _set_current_color(self, value: QColor) -> None:
        if value != self._current_color:
            self._current_color = QColor(value)
            self.update()
            self.currentColorChanged.emit()

    def _get_wait_color(self) -> QColor:
        return self._wait_color

    def _set_wait_color(self, value: QColor) -> None:
        if value != self._wait_color:
            self._wait_color = QColor(value)
            self.update()
            self.waitColorChanged.emit()

    def _get_complete(self) -> bool:
        return self._complete

    def _set_complete(self, value: bool) -> None:
        if value != self._complete:
            self._complete = value
            self.update()
            self.completeChanged.emit()

    def _get_description(self) -> str:
        return f"{self._done} of {self._total} pieces voiced"

    total = Property(int, _get_total, _set_total, notify=totalChanged)
    done = Property(int, _get_done, _set_done, notify=doneChanged)
    columns = Property(int, _get_columns, _set_columns, notify=columnsChanged)
    dotSize = Property(float, _get_dot_size, _set_dot_size, notify=dotSizeChanged)
    gap = Property(float, _get_gap, _set_gap, notify=gapChanged)
    doneColor = Property(QColor, _get_done_color, _set_done_color, notify=doneColorChanged)
    currentColor = Property(
        QColor, _get_current_color, _set_current_color, notify=currentColorChanged
    )
    waitColor = Property(QColor, _get_wait_color, _set_wait_color, notify=waitColorChanged)
    complete = Property(bool, _get_complete, _set_complete, notify=completeChanged)
    description = Property(str, _get_description, notify=descriptionChanged)

    # -- painting -----------------------------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        total = self._total
        if total <= 0:
            return
        pitch = self._dot_size + self._gap
        done = total if self._complete else min(self._done, total)
        done_path, current_path, wait_path = QPainterPath(), QPainterPath(), QPainterPath()
        for index in range(total):
            row, col = divmod(index, self._columns)
            rect = QRectF(col * pitch, row * pitch, self._dot_size, self._dot_size)
            if index < done:
                done_path.addEllipse(rect)
            elif index == done and not self._complete:
                current_path.addEllipse(rect)
            else:
                wait_path.addEllipse(rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for path, color in (
            (done_path, self._done_color),
            (current_path, self._current_color),
            (wait_path, self._wait_color),
        ):
            if not path.isEmpty():
                painter.fillPath(path, color)


def register_types() -> None:
    """Register ``DotGrid`` under ``TomecastNative`` 1.0. Safe to call more than once."""
    global _registered
    if _registered:
        return
    # PySide6 6.12 wants a str name at runtime although its stubs say bytes.
    qmlRegisterType(DotGrid, QML_URI, 1, 0, cast(Any, "DotGrid"))
    _registered = True
