from __future__ import annotations

import ast
import math
import operator as _op

from PySide6.QtCore import Qt, Signal, QRectF, QObject, QEvent
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QFontMetrics,
    QPainterPath,
    QCursor,
)
from PySide6.QtWidgets import QWidget, QSizePolicy, QApplication

from ui.theme import Theme

_DRAG_START_PIXELS = 2.0
_SHIFT_SCALE = 0.1

# Low-alpha (dx, dy, alpha) offsets drawn behind the number to fake a soft
# shadow/glow cheaply (a handful of extra drawText calls) instead of a real
# blur (which would need an offscreen pixmap pass per paint). Centered on
# the text itself rather than offset in a direction. Two rings - a tighter
# one and a wider one - read as softer than a single ring, without needing
# more than a few extra passes.
_SHADOW_PASSES = [
    (0, -1, 110),
    (-1, 0, 110),
    (1, 0, 110),
    (0, 1, 110),
    (0, -2, 60),
    (-2, 0, 60),
    (2, 0, 60),
    (0, 2, 60),
]

# Characters a NumericField accepts while editing so users can type math
# expressions (e.g. "0.5*2", "100/2", "sqrt(16)", "pi * r2"). Letters are
# allowed for function names and constants; an invalid expression is simply
# rejected on commit, leaving the previous value intact.
_EXPR_CHARS = set("+-*/%() .")


def _is_expr_char(ch: str) -> bool:
    return ch.isalnum() or ch in _EXPR_CHARS

# Safe arithmetic subset for expressions typed into a NumericField.
_CALC_OPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
    ast.USub: _op.neg,
    ast.UAdd: _op.pos,
}

_CALC_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}

_CALC_CONSTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _eval_expr(node):
    """Evaluate an AST node using only the whitelisted arithmetic above."""
    if isinstance(node, ast.Expression):
        return _eval_expr(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return float(node.value)
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_eval_expr(node.left), _eval_expr(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_eval_expr(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_FUNCS:
        return _CALC_FUNCS[node.func.id](*[_eval_expr(arg) for arg in node.args])
    if isinstance(node, ast.Name) and node.id in _CALC_CONSTS:
        return _CALC_CONSTS[node.id]
    raise ValueError(f"unsupported expression: {ast.dump(node)}")


class _CommitOnClickAway(QObject):
    """Commits active NumericField edits when the left button is pressed on
    any widget other than the field itself.

    Clicking on empty dock space (or any non-focusable surface) never moves
    focus, so the editing field would not receive a focusOutEvent. This
    filter catches those presses before they are dispatched.
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            for field in tuple(NumericField._active_edits):
                if obj is field:
                    continue
                field._commit()
        return False


class NumericField(QWidget):
    """A compact, reusable integer/float property field.

    Looks like a rounded gray box with a light outline and a centered
    number. Interactions:

      * a plain click enters text mode - type a value, Enter commits,
        Escape cancels;
      * holding the left button and dragging horizontally scrubs the
        value to follow the mouse (right = up, left = down);
      * holding Shift while scrubbing scales the mouse delta by 0.1 so a
        horizontal drag can go from coarse to fine within one drag.

    The drag is incremental (it accumulates deltas), so toggling Shift
    mid-drag continues smoothly from the current value instead of jumping.
    """

    valueChanged = Signal(float)

    # Mark the start/end of a scrub interaction so the panel can snapshot
    # the scene once per drag instead of once per movement tick.
    scrub_started = Signal()
    scrub_finished = Signal()

    # Active in-progress edits, used by the click-away filter.
    _active_edits: set["NumericField"] = set()
    _click_away_filter: _CommitOnClickAway | None = None

    def __init__(
        self,
        variant: str = "float",
        minimum: float | None = None,
        maximum: float | None = None,
        decimals: int = 2,
        step: float = 1.0,
        scrub_step: float | None = None,
    ):
        super().__init__()
        self._variant = variant  # "int" or "float"
        self._minimum = float(minimum) if minimum is not None else None
        self._maximum = float(maximum) if maximum is not None else None
        self._decimals = max(0, int(decimals))
        self._step = max(step, 0.0) or 1.0
        self._scrub_step = (
            scrub_step
            if scrub_step is not None
            else (1.0 if variant == "int" else self._step)
        )

        self._value = 0.0

        # text-edit state
        self._editing = False
        self._text = ""
        self._caret = 0
        self._anchor: int | None = None
        self._caret_visible = True

        # scrub state
        self._press_global_x = 0.0
        self._press_global_y = 0.0
        self._last_x = 0.0
        self._last_value = 0.0
        self._scrubbing = False
        self._pressed = False

        # undo-friendly scrub reporting - True between scrub_started/finished
        self._scrub_reported = False

        self._cursor_locked = False  # True while scrubbing hides the cursor
        self._hovered = False

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        font = self.font()
        font.setPointSize(11)
        font.setBold(True)
        self.setFont(font)
        self.setMinimumHeight(20)
        self.setMaximumHeight(22)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        self._apply_bounds()

    @property
    def has_bounds(self) -> bool:
        """Returns True only when both lower and upper limits are set."""
        return self._minimum is not None and self._maximum is not None


    # ------------------------------------------------------------------ #
    # Public value API
    # ------------------------------------------------------------------ #
    def value(self):
        if self._variant == "int":
            return int(round(self._value))
        return self._value

    def setValue(self, value: float) -> None:
        self._value = self._clamp(float(value))
        # Removed: mandatory rounding of self._value for "int" variants here.
        # Floating-point values are kept internally so fractional step increments accumulate.
        self.update()
        self.valueChanged.emit(self.value())


    def setRange(self, minimum: float | None, maximum: float | None) -> None:
        self._minimum = float(minimum) if minimum is not None else None
        self._maximum = float(maximum) if maximum is not None else None
        self._apply_bounds()



    def setDecimals(self, decimals: int) -> None:
        self._decimals = max(0, int(decimals))
        self.update()

    def setScrubStep(self, step: float) -> None:
        self._scrub_step = max(float(step), 0.0)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _clamp(self, v: float) -> float:
        if self._minimum is not None:
            v = max(self._minimum, v)
        if self._maximum is not None:
            v = min(self._maximum, v)
        return v

    def _apply_bounds(self) -> None:
        self._value = self._clamp(self._value)
        if self._variant == "int":
            self._value = float(round(self._value))
        self.update()

    def _number_string(self) -> str:
        if self._variant == "int":
            return str(int(round(self._value)))
        s = f"{self._value:.{self._decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s

    def _parse_text(self, text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        try:
            v = float(text)
        except (ValueError, TypeError):
            try:
                tree = ast.parse(text, mode="eval")
                v = _eval_expr(tree)
            except (ValueError, TypeError, ZeroDivisionError, OverflowError,
                    SyntaxError, KeyError, AttributeError):
                return None
        if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            return None
        v = float(v)
        if self._variant == "int":
            v = float(round(v))
        return v

    def _begin_edit(self, select_all: bool = True) -> None:
        if self._editing:
            self._commit()
            return
        self._editing = True
        self._text = self._number_string()
        if select_all:
            self._caret = len(self._text)
            self._anchor = 0
        else:
            self._caret = len(self._text)
            self._anchor = None
        self._caret_visible = True
        self.__class__._activate(self)
        self.update()

    def _commit(self) -> None:
        if not self._editing:
            return
        self._editing = False
        parsed = self._parse_text(self._text)
        if parsed is not None:
            self.setValue(parsed)
        self._text = ""
        self._caret = 0
        self._anchor = None
        self.__class__._deactivate(self)
        self.update()

    def _cancel(self) -> None:
        if not self._editing:
            return
        self._editing = False
        self._text = ""
        self._caret = 0
        self._anchor = None
        self.__class__._deactivate(self)
        self.update()

    @classmethod
    def _activate(cls, field: "NumericField") -> None:
        cls._active_edits.add(field)
        if cls._click_away_filter is None:
            app = QApplication.instance()
            if app is not None:
                cls._click_away_filter = _CommitOnClickAway()
                app.installEventFilter(cls._click_away_filter)

    @classmethod
    def _deactivate(cls, field: "NumericField") -> None:
        cls._active_edits.discard(field)

    # ------------------------------------------------------------------ #
    # Text-edit helpers
    # ------------------------------------------------------------------ #
    def _has_selection(self) -> bool:
        return self._anchor is not None and self._anchor != self._caret

    def _selected_range(self) -> tuple[int, int] | None:
        if not self._has_selection():
            return None
        return tuple(sorted((self._anchor, self._caret)))

    def _delete_selection(self) -> bool:
        rng = self._selected_range()
        if not rng:
            return False
        start, end = rng
        self._text = self._text[:start] + self._text[end:]
        self._caret = start
        self._anchor = None
        return True

    def _insert_text(self, chunk: str) -> None:
        rng = self._selected_range()
        if rng:
            start, end = rng
            self._text = self._text[:start] + chunk + self._text[end:]
            self._caret = start + len(chunk)
            self._anchor = None
        else:
            self._text = self._text[: self._caret] + chunk + self._text[self._caret:]
            self._caret += len(chunk)

    def _char_index_at_x(self, x: float) -> int:
        text = self._text
        if not text:
            return 0
        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(text)
        start = (self.width() - width) / 2
        best_index = 0
        best_dist = abs(x - start)
        acc = start
        for i, ch in enumerate(text):
            acc += metrics.horizontalAdvance(ch)
            dist = abs(x - acc)
            if dist < best_dist:
                best_dist = dist
                best_index = i + 1
        return best_index

    def _place_caret_at(self, x: float) -> None:
        self._caret = self._char_index_at_x(x)
        self._anchor = None

    def _nudge(self, delta: float) -> None:
        self.setValue(self._value + delta)

    # ------------------------------------------------------------------ #
    # Scrub reporting (undo/redo grouping)
    # ------------------------------------------------------------------ #
    def _start_scrub_reporting(self) -> None:
        if not self._scrub_reported:
            self._scrub_reported = True
            self.scrub_started.emit()

    def _finish_scrub_reporting(self) -> None:
        if self._scrub_reported:
            self._scrub_reported = False
            self.scrub_finished.emit()

    # ------------------------------------------------------------------ #
    # Mouse
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._pressed = True
        self._scrubbing = False
        self._press_global_x = event.globalPosition().x()
        self._press_global_y = event.globalPosition().y()
        self._last_x = self._press_global_x
        self._last_value = self._value
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._pressed or not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        x = event.globalPosition().x()
        if x == self._last_x:
            event.accept()
            return
        if not self._scrubbing:
            if abs(x - self._press_global_x) < _DRAG_START_PIXELS:
                return
            if self._editing:
                self._commit()
            self._scrubbing = True
            self._last_x = self._press_global_x
            self._last_value = self._value
            self._start_scrub_reporting()
            QApplication.setOverrideCursor(Qt.BlankCursor)
            self._cursor_locked = True
            self.grabMouse()
        dx = x - self._last_x
        factor = _SHIFT_SCALE if (event.modifiers() & Qt.ShiftModifier) else 1.0
        dx_eff = dx * self._scrub_step * factor

        # Directly adjust the continuous float accumulator
        self.setValue(self._value + dx_eff)
        self.update()

        QCursor.setPos(int(self._press_global_x), int(self._press_global_y))
        if QCursor.pos().x() == int(self._press_global_x):
            self._last_x = float(int(self._press_global_x))
        else:
            self._last_x = x
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        was_pressed = self._pressed
        was_scrubbing = self._scrubbing
        self._pressed = False
        self._scrubbing = False
        if was_scrubbing:
            self.releaseMouse()
            if self._cursor_locked:
                QApplication.restoreOverrideCursor()
                self._cursor_locked = False
            self._finish_scrub_reporting()
        if was_pressed and not was_scrubbing:
            self.setFocus(Qt.MouseFocusReason)
            if self._editing:
                self._place_caret_at(event.position().x())
            else:
                self._begin_edit(select_all=True)
        event.accept()

    # ------------------------------------------------------------------ #
    # Keyboard
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event):
        key = event.key()
        if self._editing:
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._commit()
                return
            if key == Qt.Key_Escape:
                self._cancel()
                return
            if key == Qt.Key_A and (event.modifiers() & Qt.ControlModifier):
                self._caret = len(self._text)
                self._anchor = 0
                self.update()
                return
            if key == Qt.Key_C and (event.modifiers() & Qt.ControlModifier):
                rng = self._selected_range()
                if rng:
                    start, end = rng
                    QApplication.clipboard().setText(self._text[start:end])
                return
            if key == Qt.Key_X and (event.modifiers() & Qt.ControlModifier):
                rng = self._selected_range()
                if rng:
                    start, end = rng
                    QApplication.clipboard().setText(self._text[start:end])
                    self._delete_selection()
                    self.update()
                return
            if key == Qt.Key_V and (event.modifiers() & Qt.ControlModifier):
                pasted = "".join(
                    ch for ch in QApplication.clipboard().text() if _is_expr_char(ch)
                )
                if pasted:
                    self._insert_text(pasted)
                    self.update()
                return
            if key == Qt.Key_Left:
                if event.modifiers() & Qt.ShiftModifier:
                    if self._anchor is None:
                        self._anchor = self._caret
                    self._caret = max(0, self._caret - 1)
                else:
                    if self._has_selection():
                        self._caret = min(self._anchor, self._caret)
                    else:
                        self._caret = max(0, self._caret - 1)
                    self._anchor = None
                self._caret_visible = True
                self.update()
                return
            if key == Qt.Key_Right:
                if event.modifiers() & Qt.ShiftModifier:
                    if self._anchor is None:
                        self._anchor = self._caret
                    self._caret = min(len(self._text), self._caret + 1)
                else:
                    if self._has_selection():
                        self._caret = max(self._anchor, self._caret)
                    else:
                        self._caret = min(len(self._text), self._caret + 1)
                    self._anchor = None
                self._caret_visible = True
                self.update()
                return
            if key in (Qt.Key_Home, Qt.Key_End):
                self._caret = 0 if key == Qt.Key_Home else len(self._text)
                self._anchor = None
                self.update()
                return
            if key == Qt.Key_Backspace:
                if self._has_selection():
                    self._delete_selection()
                elif self._caret > 0:
                    self._text = self._text[: self._caret - 1] + self._text[self._caret:]
                    self._caret -= 1
                self.update()
                return
            if key == Qt.Key_Delete:
                if self._has_selection():
                    self._delete_selection()
                elif self._caret < len(self._text):
                    self._text = self._text[: self._caret] + self._text[self._caret + 1 :]
                self.update()
                return
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._commit()
                factor = _SHIFT_SCALE if (event.modifiers() & Qt.ShiftModifier) else 1.0
                self._nudge(self._step * factor * (1 if key == Qt.Key_Up else -1))
                return
            text = event.text()
            if text and _is_expr_char(text):
                self._insert_text(text)
                self.update()
                return
            event.ignore()
            return

        # not editing
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.setFocus(Qt.OtherFocusReason)
            self._begin_edit(select_all=True)
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            factor = _SHIFT_SCALE if (event.modifiers() & Qt.ShiftModifier) else 1.0
            self._nudge(self._step * factor * (1 if key == Qt.Key_Up else -1))
            return
        text = event.text()
        if text and _is_expr_char(text):
            self.setFocus(Qt.OtherFocusReason)
            self._begin_edit(select_all=True)
            self._insert_text(text)
            self.update()
            return
        event.ignore()

    def focusOutEvent(self, event):
        if self._editing:
            self._commit()
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if self._editing:
            event.ignore()
            return
        steps = event.angleDelta().y()
        if steps == 0:
            event.ignore()
            return
        factor = _SHIFT_SCALE if (event.modifiers() & Qt.ShiftModifier) else 1.0
        self._nudge(self._step * factor * (1 if steps > 0 else -1))
        event.accept()

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #
    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        rect = QRectF(1, 1, w - 2, h - 2)

        bg = QColor("#333333") if self._editing else QColor("#3a3a3a")
        border = self._border_color()
        if not self.isEnabled():
            bg = QColor("#333333")
            border = QColor("#3f3f3f")

        # Draw base control outline & background
        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 5, 5)

        painter.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, 5, 5)
        painter.setClipPath(clip_path)

        # Render accent fill bar if realistic bounds are defined
        if (
            self._minimum is not None
            and self._maximum is not None
            and -1e11 < self._minimum < self._maximum < 1e11
            and self._minimum != self._maximum
        ):
            fraction = (self._value - self._minimum) / (self._maximum - self._minimum)
            fraction = max(0.0, min(1.0, fraction))

            fill_width = rect.width() * fraction
            if fill_width > 0:
                fill_rect = QRectF(rect.x(), rect.y(), fill_width, rect.height())
                painter.fillRect(fill_rect, Theme.ACCENT)

        text = self._text if self._editing else self._number_string()
        color = (
            QColor("#707070")
            if not self.isEnabled()
            else (QColor("#e0e0e0") if not self._editing else Theme.ACCENT)
        )

        metrics = QFontMetrics(self.font())
        width = metrics.horizontalAdvance(text)
        asc = metrics.ascent()
        desc = metrics.descent()
        x = 1 + (w - 2 - width) / 2
        y = 1 + (h - 2 - (asc + desc)) / 2 + asc

        boundaries = [x]
        acc = x
        for ch in text:
            acc += metrics.horizontalAdvance(ch)
            boundaries.append(acc)

        # Selection band, drawn behind the text as a translucent accent
        # highlight. No longer needs to special-case the fill bar underneath.
        if self._editing and self._has_selection():
            sel_start, sel_end = sorted((self._anchor, self._caret))
            sel_x = boundaries[sel_start]
            sel_w = boundaries[sel_end] - sel_x
            sel_rect = QRectF(sel_x, y - asc, sel_w, asc + desc)
            sel_fill = QColor(255, 255, 255, 70)
            painter.fillRect(sel_rect, sel_fill)

        # Number text always keeps its normal colour, with a soft shadow/glow
        # behind it so it stays legible wherever the accent fill bar happens
        # to sit underneath it. A real Gaussian blur would need an offscreen
        # pixmap pass per paint, which is overkill for a couple of glyphs and
        # adds up fast across many of these fields repainting during a scrub
        # drag - so instead this fakes softness cheaply with a handful of
        # low-alpha passes centered on the text. Still just a few extra
        # drawText calls.
        if text:
            for dx, dy, alpha in _SHADOW_PASSES:
                painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
                painter.drawText(int(x) + dx, int(y) + dy, text)

            painter.setPen(QPen(color, 1))
            painter.drawText(int(x), int(y), text)

        if self._editing and self._caret_visible:
            caret_x = boundaries[self._caret]
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.drawLine(int(caret_x), int(y - asc), int(caret_x), int(y + desc))

        painter.restore()
        painter.end()

    def _border_color(self) -> QColor:
        if self._scrubbing:
            return Theme.ACCENT
        if self._editing:
            return Theme.ACCENT
        if self._hovered:
            return QColor("#7a7a7a") if self.isEnabled() else QColor("#3f3f3f")
        return QColor("#5a5a5a") if self.isEnabled() else QColor("#3f3f3f")
