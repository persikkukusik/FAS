"""Shared relative-drag ("fake cursor") controller.

Reads unaccelerated raw mouse deltas from the hardware input devices via
``evdev`` when available (fastest, works on Wayland and X11), and transparently
falls back to measuring the (hidden + grabbed) real cursor's own movement when
``evdev`` is not installed or is denied by permissions.
"""
from __future__ import annotations

import select
import sys
import threading

from PySide6.QtCore import QPoint, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QCursor
from PySide6.QtCore import Qt


# --- Hardware Raw Input Listener ------------------------------------------- #

class RawMouseListener:
    """Captures raw mouse deltas from /dev/input via evdev.

    Deliberately a plain object, NOT a QObject: the deltas are accumulated in a
    lock-protected store read by the GUI thread, so there is no cross-thread Qt
    signal involved (which silently no-ops if the receiver's event loop / the
    package / permissions are missing).
    """

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self.use_evdev = False
        self._fds = []
        self._values = []
        self._ecodes = None
        self._lock = threading.Lock()
        self._dx = 0.0
        self._dy = 0.0
        self.reason: str | None = None

    def begin(self) -> None:
        """Probe evdev synchronously; if usable, start the raw read thread."""
        if self._probe_evdev():
            self.use_evdev = True
            self._running = True
            self._thread = threading.Thread(target=self._evdev_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for dev in self._fds:
                try:
                    dev.close()
                except Exception:
                    pass
            self._fds = []

    @property
    def available(self) -> bool:
        return self.use_evdev

    def _probe_evdev(self) -> bool:
        if not sys.platform.startswith("linux"):
            self.reason = "evdev is only available on Linux"
            return False
        try:
            import evdev
            from evdev import ecodes
        except Exception:
            self.reason = "evdev package not installed"
            return False
        try:
            paths = evdev.list_devices()
        except Exception as exc:
            self.reason = f"cannot list input devices: {exc}"
            return False
        if not paths:
            self.reason = "no input devices found"
            return False

        self._ecodes = ecodes
        fds, values = [], []
        try:
            for path in paths:
                try:
                    dev = evdev.InputDevice(path)
                except Exception:
                    continue
                caps = dev.capabilities()
                if ecodes.EV_REL in caps:
                    fds.append(dev)
                else:
                    try:
                        dev.close()
                    except Exception:
                        pass
            if not fds:
                self.reason = "no relative (mouse) input devices found"
                return False
            self._fds = fds
            return True
        except Exception as exc:
            self.reason = f"cannot open input devices: {exc}"
            try:
                for dev in fds:
                    dev.close()
            except Exception:
                pass
            return False

    def accumulate(self, dx: float, dy: float) -> None:
        with self._lock:
            self._dx += dx
            self._dy += dy

    def take(self) -> tuple[float, float]:
        """Flush and return accumulated deltas since the last read."""
        with self._lock:
            dx, dy = self._dx, self._dy
            self._dx = self._dy = 0.0
            return dx, dy

    def _evdev_loop(self) -> None:
        while self._running:
            try:
                with self._lock:
                    fds = list(self._fds)
                r, _, _ = select.select(fds, [], [], 0.05)
            except Exception:
                self._running = False
                return
            for dev in r:
                try:
                    for event in dev.read():
                        if event.type == self._ecodes.EV_REL:
                            if event.code == self._ecodes.REL_X:
                                self.accumulate(float(event.value), 0.0)
                            elif event.code == self._ecodes.REL_Y:
                                self.accumulate(0.0, float(event.value))
                except Exception:
                    continue


# --- Relative Drag Controller ---------------------------------------------- #

class RelativeDrag:
    """Relative-motion drag gesture powered by raw mouse deltas.

    Reads deltas from ``evdev`` hardware input when possible; otherwise falls
    back to the hidden + grabbed real cursor's own movement (which is the same
    sign/magnitude as the raw deltas, just subject to OS acceleration).
    """

    def __init__(self, widget):
        self.widget = widget
        self.active = False
        self.kind: str | None = None

        self._fake_pos = QPointF(0, 0)
        self._gesture_global = QPoint()
        self._last_global = None
        self._origin_fixed = False
        self._accumulated_dx = 0.0
        self._accumulated_dy = 0.0

        # Raw mouse input source
        self._listener = RawMouseListener()

    def begin(self, kind: str = "cross") -> None:
        """Lock the real cursor and begin accumulating mouse deltas."""
        w = self.widget
        g = QCursor.pos()
        local = w.mapFromGlobal(g)

        self._gesture_global = QPoint(g)
        self._last_global = QPoint(g)
        self._origin_fixed = False
        ww = max(w.width(), 1)
        wh = max(w.height(), 1)
        self._fake_pos = QPointF(local.x() % ww, local.y() % wh)
        self.kind = kind
        self._accumulated_dx = 0.0
        self._accumulated_dy = 0.0
        self.active = True

        # Start hardware input stream (falls back internally if evdev is absent)
        self._listener.begin()

        # Hide cursor (grab is handled by the caller so events keep flowing)
        w.setCursor(Qt.BlankCursor)

    @property
    def using_evdev(self) -> bool:
        return self._listener.available

    def delta(self, global_pos: QPoint | None = None) -> tuple[float, float] | None:
        """Poll and flush accumulated deltas since the last check.

        With evdev the raw hardware deltas are used and ``global_pos`` is
        ignored. Without evdev, the delta is measured from the hidden real
        cursor's successive positions. In both cases the real cursor is
        warped back to the gesture anchor on every poll, so it stays hidden
        inside the widget and can never escape and abort the gesture.
        """
        if not self.active:
            return None

        anchor = self._gesture_global

        if self._listener.available:
            dx, dy = self._listener.take()
        else:
            # Fallback: delta of the grabbed+hidden real cursor.
            if global_pos is None or self._last_global is None:
                return None
            now = QPoint(global_pos)
            if not self._origin_fixed:
                # First event after begin can carry a stale/pre-grab position;
                # fix the origin there so it can't register as a phantom jump,
                # and park the real cursor at the anchor.
                self._origin_fixed = True
                self._last_global = QPoint(anchor)
                QCursor.setPos(anchor)
                return None
            dx = now.x() - self._last_global.x()
            dy = now.y() - self._last_global.y()
            # Measure the next delta relative to the anchor (the real cursor
            # is snapped back there below).
            self._last_global = QPoint(anchor)

        # Contain the hidden real cursor at the gesture anchor so it can never
        # drift to the widget edge (or past it) and abort the drag.
        QCursor.setPos(anchor)

        self._accumulated_dx += dx
        self._accumulated_dy += dy

        if dx == 0.0 and dy == 0.0:
            return None

        # Wrap the fake cursor into the widget tile: its stored position is
        # always normalized to [0, width) x [0, height), so the grid around it
        # stays seamless and (mathematically) infinite no matter how far the
        # gesture travels - no unbounded coordinate drift and no need to tile
        # against a growing absolute position.
        ww = max(self.widget.width(), 1)
        wh = max(self.widget.height(), 1)
        self._fake_pos += QPointF(dx, dy)
        self._fake_pos = QPointF(
            self._fake_pos.x() % ww,
            self._fake_pos.y() % wh,
        )
        return dx, dy

    def paint(self, painter: QPainter) -> None:
        """Draw the fake-cursor glyph, tiled as an infinite wrap-around grid.

        The stored position (already normalized to the widget tile) is taken
        modulo the widget size, and the 9 copies (-width/0/+width by
        -height/0/+height) around it render the cursor entering from one edge
        as it exits the opposite one.
        """
        if self.kind is None or self.kind == "none":
            return
        w = self.widget
        ww = max(w.width(), 1)
        wh = max(w.height(), 1)
        bx = self._fake_pos.x() % ww
        by = self._fake_pos.y() % wh
        sizes = [0.0, -float(ww), float(ww)]
        os = [0.0, -float(wh), float(wh)]

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        outline = QPen(QColor(0, 0, 0, 180), 3)
        stroke = QPen(QColor(255, 255, 255), 1.4)

        for dx in sizes:
            for dy in os:
                x = bx + dx
                y = by + dy
                if self.kind == "hand":
                    r = 7.0
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(outline)
                    painter.drawEllipse(QPointF(x, y), r, r)
                    painter.setPen(stroke)
                    painter.drawEllipse(QPointF(x, y), r, r)
                else:  # cross
                    sz = 9.0
                    for pen in (outline, stroke):
                        painter.setPen(pen)
                        painter.drawLine(QPointF(x - sz, y), QPointF(x + sz, y))
                        painter.drawLine(QPointF(x, y - sz), QPointF(x, y + sz))
        painter.restore()

    def visible_pos(self) -> QPoint:
        """The widget-local wrapped position for restoring the cursor on end."""
        w = self.widget
        ww = max(w.width(), 1)
        wh = max(w.height(), 1)
        return QPoint(int(self._fake_pos.x() % ww), int(self._fake_pos.y() % wh))

    def end(self, restore_to_start: bool = False) -> None:
        """Stop listening to raw inputs and restore the real cursor."""
        if not self.active:
            return
        w = self.widget

        self._listener.stop()
        self._listener = RawMouseListener()

        if restore_to_start:
            QCursor.setPos(self._gesture_global)
        else:
            QCursor.setPos(w.mapToGlobal(self.visible_pos()))

        self.kind = None
        self.active = False
        w.setCursor(Qt.ArrowCursor)
