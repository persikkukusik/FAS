"""Custom popup menu + menu bar with StripeShader hover highlighting.

Replaces Qt's native QMenu/QMenuBar (whose ``:selected`` hover is a flat
stylesheet colour that can't be custom-painted) everywhere in the app so the
hovered entry animates with the same scrolling stripe pattern used by the
outliner/timeline selection effects.

Public API mirrors the parts of QMenu the app actually uses:

    menu = StripeMenu()
    menu.add_action("Wrap", callback, checkable=True, checked=..., data=...)
    menu.add_action("Mode", callback, description="What this option does")
    menu.add_section("Interpolation Mode")
    menu.add_separator()
    chosen = menu.exec(global_point)   # blocking, returns chosen action or None

``StripeMenuBar`` is a drop-in for ``QMenuBar``:

    bar = StripeMenuBar()
    file_menu = bar.add_menu("&File")
    file_menu.add_action("&Open...", callback, shortcut="Ctrl+O")

When an action carries a ``description``, hover over it pops up a small
floating tooltip beside the menu (right side, or left when there's no room).
No description means no tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer, QEventLoop, QObject, QEvent
from PySide6.QtGui import (QColor, QPainter, QFontMetrics, QPen, QPainterPath,
                           QKeySequence, QShortcut, QCursor)
from PySide6.QtWidgets import QWidget, QApplication

from ui.theme import Theme
from ui.stripes import StripeShader

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


class StripeMenuAction:
    """A single entry in a :class:`StripeMenu`."""

    __slots__ = (
        "text", "callback", "checkable", "checked", "data",
        "shortcut_text", "section", "separator", "enabled", "description",
    )

    def __init__(self, text="", callback=None, checkable=False, checked=False,
                 data=None, shortcut_text="", section=False, separator=False,
                 enabled=True, description=""):
        self.text = text
        self.callback = callback
        self.checkable = checkable
        self.checked = checked
        self.data = data
        self.shortcut_text = shortcut_text
        self.section = section
        self.separator = separator
        self.enabled = enabled
        self.description = description


def _paint_tilted_accent(painter: QPainter, rect: QRectF, clip: QPainterPath):
    """Fill `rect` with a half-transparent accent wash tilted 45 degrees.

    The fill is rotated to run along the same diagonal as the stripe pattern,
    so the hovered entry gets an accent-tinted backdrop while the (animated)
    stripes still read on top of it.
    """
    painter.save()
    painter.setClipPath(clip)
    painter.translate(rect.center())
    painter.rotate(-45.0)
    color = QColor(Theme.ACCENT)
    color.setAlpha(110)
    d = rect.width() * 1.5 + rect.height()
    painter.fillRect(QRectF(-d / 2, -d / 2, d, d), color)
    painter.restore()


class _DismissOnOutsideClick(QObject):
    """Dismisses any open StripeMenu when the mouse is pressed or released on a
    widget that isn't the menu itself, without consuming the click.

    The menu is rendered as an embedded child of the app window (no mouse grab)
    so it stays hoverable and doesn't block clicks on the rest of the app - but
    we still want it to close when the user interacts elsewhere. This filter
    fires before the event is handled by the target widget and returns False so
    it still reaches the intended control.

    A press anywhere outside the menu closes it. A press-and-hold drag that
    ends with the release outside the menu (or a release that started inside
    the popup and left it) also closes it instantly, so the menu never stays
    open while the user neither holds the button nor points at the popup.
    """

    def __init__(self, menu):
        super().__init__()
        self._menu = menu

    def eventFilter(self, obj, event):
        m = self._menu
        if event.type() == QEvent.KeyRelease and not event.isAutoRepeat() \
                and m._trigger_key is not None and event.key() == m._trigger_key:
            # Release of a held key (e.g. T for interpolation): apply the row
            # under the pointer, or cancel if the pointer is elsewhere.
            m._release_from_global(QCursor.pos())
        elif event.type() == QEvent.MouseButtonRelease \
                and event.button() in m._trigger_buttons:
            # Release of the held button that opened this trigger popup.
            m._release_from_global(event.globalPosition().toPoint())
        elif event.type() == QEvent.MouseButtonPress and obj is not m:
            m._finish()
        elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            top_left = m.mapToGlobal(QPoint(0, 0))
            rect = QRect(top_left, m.size())
            if not rect.contains(event.globalPosition().toPoint()):
                m._finish()
        elif event.type() == QEvent.WindowDeactivate and obj is m.window():
            # The window that hosts the embedded menu has lost activation
            # (e.g. the user switched to another app/window): close the menu.
            m._finish()
        return False


# Stack of StripeMenu instances currently blocked in ``exec``.
_OPEN_MENUS: list["StripeMenu"] = []


def stripe_menu_open() -> bool:
    """True while at least one StripeMenu popup is displaying.

    Widgets that grab keyboard focus on mouse-hover (stage/outliner/timeline)
    are meant to skip that grab while a popup is open, otherwise hovering over
    them would yank focus - and with it the open menu's keyboard handling and
    (previously) even its close-on-focus-out behaviour.
    """
    return bool(_OPEN_MENUS)


class _DescriptionPopup(QWidget):
    """Small floating tooltip that shows an action's description next to a StripeMenu.

    Rendered the SAME way as the menu popup itself: a plain child widget
    embedded inside the hosting window (never its own top-level window), so it
    can never appear as a separate task-bar entry. It is transparent for the
    mouse so it never blocks clicks, and must be parented to the hosting
    window (not the menu) before being shown, otherwise Qt clips any part that
    sticks out beyond the menu's own bounds.
    """

    PAD_X = 10
    PAD_Y = 6
    GAP = 8
    MIN_W = 80
    MAX_W = 300
    CORNER = 4

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._text = ""

    def set_description(self, text: str):
        self._text = text
        if not text:
            self.hide()
            return
        font = self.font()
        font.setPointSize(10)
        fm = QFontMetrics(font)
        constrained_w = self.MAX_W - 2 * self.PAD_X
        text_rect = fm.boundingRect(
            QRect(0, 0, constrained_w, 10000),
            Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
            text,
        )
        w = self.PAD_X * 2 + max(text_rect.width(), self.MIN_W - 2 * self.PAD_X)
        h = self.PAD_Y * 2 + text_rect.height()
        self.setFixedSize(int(w), int(h))
        self.update()

    def paintEvent(self, event):
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.setBrush(QColor(30, 30, 30))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), self.CORNER, self.CORNER)
        font = self.font()
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QColor(200, 200, 200))
        text_rect = self.rect().adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, self._text)
        painter.end()


class StripeMenu(QWidget):
    """A borderless popup menu that is embedded inside the app window.

    The popup is rendered as a child widget of the relevant top-level window
    rather than as its own top-level window, so it can never show up as a
    separate entry in the task bar. The hovered row is filled with the
    scrolling StripeShader instead of a flat colour - animated like any other
    stripe surface in the app.
    """

    ROW_HEIGHT = 30
    H_PAD = 12
    CHECK_W = 20
    SHORTCUT_GAP = 28
    SEP_H = 0
    SECTION_H = 24
    CORNER = 6

    def __init__(self, title="", parent=None, shortcut_parent=None, host=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._actions: list[StripeMenuAction] = []
        self._hover_index = -1
        self._result: StripeMenuAction | None = None
        # Whether the current left press already activated a row (guards the
        # paired release from firing the action a second time).
        self._press_activated = False
        # Held "trigger" that keeps this popup in "apply on release" mode.
        # While the key/button is held the menu stays up; on its release the
        # row under the pointer is activated (or the menu is cancelled if the
        # pointer isn't over an actionable row). Both stay None/empty for plain
        # click-to-open popups.
        self._trigger_key = None
        self._trigger_buttons: tuple = ()
        # Own a private event loop so ``exec`` blocks without spinning.
        self._loop: QEventLoop | None = None

        # Widget inside whose window the popup is embedded. When None the host
        # is resolved at ``exec`` time from the widget under the popup point /
        # the active window.
        self._host = host

        # Where registered action shortcuts get parented. A hidden Qt.Tool
        # top-level window never becomes the active window, so QShortcuts
        # parented to it are unreachable - they must live on a widget that is
        # part of the visible main window (e.g. the StripeMenuBar).
        self._shortcut_parent = shortcut_parent

        # App-level filter that dismisses any open menu when the mouse is pressed
        # on a widget other than the menu itself, WITHOUT consuming the click so
        # the target widget still receives it.
        self._click_away_filter = None

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._tick)

        self._width = 220
        _font = self.font()
        _font.setBold(True)
        _font.setPointSize(11)
        self._text_metrics = QFontMetrics(_font)
        self._shortcuts: list[QShortcut] = []
        self._desc_popup = _DescriptionPopup()
        self._desc_popup.hide()

    # -- construction ------------------------------------------------------
    def add_action(self, text="", callback=None, checkable=False, checked=False,
                   data=None, shortcut_text="", description="") -> StripeMenuAction:
        action = StripeMenuAction(text=text, callback=callback, checkable=checkable,
                                  checked=checked, data=data, shortcut_text=shortcut_text,
                                  description=description)
        self._actions.append(action)
        if shortcut_text and callback is not None:
            seq = QKeySequence(shortcut_text)
            if not seq.isEmpty():
                sc = QShortcut(seq, self._shortcut_parent or self)
                sc.setContext(Qt.ApplicationShortcut)
                if checkable:
                    sc.activated.connect(lambda a=action: self._invoke(a, toggle=True))
                else:
                    sc.activated.connect(lambda a=action: self._invoke(a, toggle=False))
                self._shortcuts.append(sc)
        return action

    def _invoke(self, action: StripeMenuAction, toggle: bool) -> None:
        if toggle:
            action.checked = not action.checked
        action.callback(action.checked if action.checkable else True)

    def add_section(self, text: str) -> None:
        self._actions.append(StripeMenuAction(text=text, section=True))

    def add_separator(self) -> None:
        self._actions.append(StripeMenuAction(separator=True))

    def clear(self) -> None:
        self._actions.clear()
        self._hover_index = -1
        self._result = None
    # -- layout ------------------------------------------------------------
    def item_height(self, action: StripeMenuAction) -> int:
        if action.separator:
            return self.SEP_H
        if action.section:
            return self.SECTION_H
        return self.ROW_HEIGHT

    def total_height(self) -> int:
        return sum(self.item_height(a) for a in self._actions)

    def _index_at(self, pos_x: float, pos_y: float) -> int:
        if pos_y < 0 or pos_x < 0 or pos_x > self.width():
            return -1
        acc = 0.0
        for i, a in enumerate(self._actions):
            acc += self.item_height(a)
            if pos_y < acc:
                if a.separator or a.section or not a.enabled:
                    return -1
                return i
        return -1

    def _action_rect(self, index: int) -> QRectF:
        y = 0.0
        for i in range(index):
            y += self.item_height(self._actions[i])
        return QRectF(0, y, self.width(), self.item_height(self._actions[index]))

    def _update_description_popup(self) -> None:
        """Show or hide the description tooltip based on the current hover.

        The tooltip is embedded INSIDE the same hosting window as the menu
        (a plain child widget, never its own top-level window - just like the
        menu popup itself), placed to the right of the menu in window-local
        coordinates, or to the left when there's no room, aligned with the
        hovered row and clamped to the window bounds.
        """
        if (self._hover_index >= 0
                and self._hover_index < len(self._actions)):
            action = self._actions[self._hover_index]
            if action.description and not action.separator and not action.section and action.enabled:
                row = self._action_rect(self._hover_index)
                self._desc_popup.set_description(action.description)

                # Embed into the menu's window (the host), never into the menu
                # itself: a child widget positioned outside its parent's bounds
                # is clipped by Qt, so the popup would be cut off right of the
                # menu column. As a sibling of the menu inside the host it
                # renders exactly like the menu does.
                window = self.window()
                if window is None:
                    window = self
                if self._desc_popup.parent() is not window:
                    self._desc_popup.setParent(window)
                    self._desc_popup.setWindowFlags(Qt.Widget)

                # Menu's top-left in window-local coordinates.
                menu_local = window.mapFromGlobal(self.mapToGlobal(QPoint(0, 0)))
                pw = self._desc_popup.width()
                ph = self._desc_popup.height()

                popup_x = menu_local.x() + self.width() + _DescriptionPopup.GAP
                if popup_x + pw > window.width():
                    popup_x = menu_local.x() - _DescriptionPopup.GAP - pw
                if popup_x < 0:
                    popup_x = 0

                popup_y = menu_local.y() + int(row.y())
                if popup_y + ph > window.height():
                    popup_y = max(0, window.height() - ph)

                self._desc_popup.move(popup_x, popup_y)
                self._desc_popup.raise_()
                self._desc_popup.show()
                return
        self._desc_popup.hide()

    # -- display -----------------------------------------------------------
    def _resolve_host(self, pos: QPoint):
        """Return the top-level widget the popup should be embedded in."""
        if self._host is not None:
            return self._host
        app = QApplication.instance()
        if app is not None:
            under = app.widgetAt(pos)
            if under is not None:
                top = under.window()
                if top is not None and top is not self and top.isVisible():
                    return top
            active = QApplication.activeWindow()
            if active is not None and active is not self and active.isVisible():
                return active
            for w in app.topLevelWidgets():
                if w is not self and w.isVisible():
                    return w
        return None

    def _embed(self, host, pos: QPoint) -> None:
        """Reparent the menu into `host` and place it at `pos` (global),
        clamped so it stays inside the window."""
        if self.parentWidget() is not host:
            self.setParent(host)
        if self.windowFlags() != Qt.Widget:
            self.setWindowFlags(Qt.Widget)
        local = host.mapFromGlobal(pos)
        x = int(max(0, min(local.x(), host.width() - self.width())))
        y = int(local.y())
        if y + self.height() > host.height():
            y = int(max(0, local.y() - self.height()))
        y = int(max(0, min(y, host.height() - self.height())))
        self.move(x, y)

    def exec(self, pos: QPoint, trigger_key=None, trigger_buttons=()) -> StripeMenuAction | None:
        """Blocking popup at global `pos`. Returns the chosen action or None.

        The menu is embedded as a child of the app window (never its own
        top-level window), so it can't appear in the task bar.

        ``trigger_key`` (a ``Qt.Key_*``) and/or ``trigger_buttons`` (an
        iterable of ``Qt.MouseButton``) describe a "hold to drive" trigger:
        the popup stays open while it is held, the row under the pointer is
        activated when it is released, and releasing it anywhere else cancels
        the menu. This is what makes holding T / RMB / a bar button behave
        like a single fast gesture.
        """
        self._hover_index = -1
        self._desc_popup.hide()
        self._result = None
        self._press_activated = False
        self._trigger_key = trigger_key
        self._trigger_buttons = tuple(trigger_buttons or ())
        self._width = max(180, self._measure_width())
        self.resize(self._width, self.total_height())

        host = self._resolve_host(pos)
        if host is not None:
            self._embed(host, pos)
        self.show()
        self.raise_()
        if host is not None:
            self.setFocus()
        self._anim_timer.start()

        app = QApplication.instance()
        if app is not None:
            self._click_away_filter = _DismissOnOutsideClick(self)
            app.installEventFilter(self._click_away_filter)

        loop = QEventLoop(self)
        self._loop = loop
        _OPEN_MENUS.append(self)
        try:
            loop.exec()
        finally:
            _OPEN_MENUS.remove(self)
        self._loop = None
        self._anim_timer.stop()
        self._trigger_key = None
        self._trigger_buttons = ()
        if app is not None and self._click_away_filter is not None:
            app.removeEventFilter(self._click_away_filter)
            self._click_away_filter = None
        return self._result

    def _measure_width(self) -> int:
        font = self.font()
        font.setBold(True)
        font.setPointSize(11)
        fm = QFontMetrics(font)
        w = 40
        for a in self._actions:
            if a.separator:
                continue
            if a.section:
                font_s = self.font()
                font_s.setBold(True)
                font_s.setPointSizeF(font_s.pointSizeF() - 0.5)
                fm = QFontMetrics(font_s)
            text_w = fm.horizontalAdvance(a.text.replace("&", ""))
            total = self.H_PAD + (self.CHECK_W if a.checkable else 0) + self.H_PAD + text_w
            if a.shortcut_text:
                total += self.SHORTCUT_GAP + fm.horizontalAdvance(a.shortcut_text)
            if a.section:
                total = self.H_PAD * 2 + text_w
            w = max(w, total)
        return w

    # -- interaction -------------------------------------------------------
    def _activate(self, index: int) -> None:
        if self._result is not None:
            # already activated (e.g. the paired release after a press that
            # fired, or the app filter plus the widget handler both running):
            # never fire the action twice.
            return
        action = self._actions[index]
        if action.separator or action.section or not action.enabled:
            return
        if action.checkable:
            action.checked = not action.checked
        self._result = action
        if action.callback is not None:
            action.callback(action.checked if action.checkable else True)
        self._finish()

    def _finish(self):
        self._desc_popup.hide()
        self.hide()
        if self._loop is not None:
            self._loop.quit()

    def mouseMoveEvent(self, event):
        idx = self._index_at(event.position().x(), event.position().y())
        if idx != self._hover_index:
            self._hover_index = idx
            self._update_description_popup()
            self.update()
        super().mouseMoveEvent(event)

    def _tick(self):
        # While a hold-trigger drives this popup, the hover highlight must
        # track the global cursor even when the pointer events never reach
        # this widget (a held button gives the mouse grab to the trigger
        # widget, which swallows every move). Polling the cursor each anim
        # frame keeps the highlighted row identical to the one that will be
        # activated on release.
        if self._trigger_key is not None or self._trigger_buttons:
            local = self.mapFromGlobal(QCursor.pos())
            idx = self._index_at(local.x(), local.y())
            if idx != self._hover_index:
                self._hover_index = idx
                self._update_description_popup()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._finish()
        elif event.button() == Qt.LeftButton:
            # An explicit left press selects the row immediately. This covers
            # popups that were opened without holding the button (right-click,
            # keyboard, or a release that landed inside), where the user presses
            # to choose. The release is guarded below so it doesn't double-fire.
            idx = self._index_at(event.position().x(), event.position().y())
            action = self._actions[idx] if idx >= 0 else None
            actionable = (action is not None and not action.separator
                          and not action.section and action.enabled)
            self._press_activated = actionable
            if actionable:
                self._activate(idx)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self._press_activated:
            idx = self._index_at(event.position().x(), event.position().y())
            if idx >= 0:
                self._activate(idx)
        super().mouseReleaseEvent(event)

    # Called by the StripeMenuBar when it holds the implicit mouse grab (a
    # left-press on a bar button) so the drag can still track/activate entries
    # in the open menu even though no explicit mouse grab is set on this window.
    def _update_hover_from_global(self, global_pos) -> None:
        local = self.mapFromGlobal(global_pos)
        idx = self._index_at(local.x(), local.y())
        if idx != self._hover_index:
            self._hover_index = idx
            self._update_description_popup()
            self.update()

    def _release_from_global(self, global_pos) -> None:
        """Apply the row under `global_pos`, otherwise cancel the menu.

        This is the "release" half of a hold trigger and of the menu bar's
        implicit drag: releasing over an actionable entry activates it, and
        releasing anywhere else dismisses the popup so it never lingers.
        """
        if self._loop is None or self._result is not None:
            return
        local = self.mapFromGlobal(global_pos)
        idx = self._index_at(local.x(), local.y())
        if idx >= 0:
            action = self._actions[idx]
            if not action.separator and not action.section and action.enabled:
                self._activate(idx)
                return
        self._finish()

    def leaveEvent(self, event):
        self._hover_index = -1
        self._update_description_popup()
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        interactive = [i for i, a in enumerate(self._actions)
                       if not a.separator and not a.section and a.enabled]
        if not interactive:
            self._finish()
            return
        if event.key() in (Qt.Key_Up, Qt.Key_K):
            pos = interactive.index(self._hover_index) if self._hover_index in interactive else -1
            self._hover_index = interactive[(pos - 1) % len(interactive)]
            self._update_description_popup()
            self.update()
        elif event.key() in (Qt.Key_Down, Qt.Key_J):
            pos = interactive.index(self._hover_index) if self._hover_index in interactive else -1
            self._hover_index = interactive[(pos + 1) % len(interactive)]
            self._update_description_popup()
            self.update()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self._hover_index >= 0:
                self._activate(self._hover_index)
        elif event.key() in (Qt.Key_Escape, Qt.Key_Left):
            self._finish()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        # Intentionally does NOT close the menu: many widgets in the app grab
        # focus on hover (stage/outliner/timeline), so losing focus is not a
        # reason to dismiss. Dismissal comes from presses elsewhere, the armed
        # action, Escape/Right click, or the host window deactivating.
        super().focusOutEvent(event)

    def enterEvent(self, event):
        # Re-assert keyboard focus so arrow-key navigation keeps working even
        # after a sibling dock/widget stole focus on hover.
        if self.isVisible():
            self.setFocus()
        super().enterEvent(event)

    def hideEvent(self, event):
        self._anim_timer.stop()
        super().hideEvent(event)

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setPen(QPen(QColor(68, 68, 68), 1))
        painter.setBrush(QColor(43, 43, 43))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), self.CORNER, self.CORNER)

        y = 0.0
        for i, action in enumerate(self._actions):
            h = self.item_height(action)

            if action.separator:
                # Purely visual: the separator takes 0 pixels of height and is
                # drawn as a hairline on the shared boundary between the rows
                # above and below, only when there is content on both sides.
                if (any(not a.separator for a in self._actions[:i])
                        and any(not a.separator for a in self._actions[i + 1:])):
                    painter.setPen(QPen(QColor(80, 80, 80), 1))
                    painter.drawLine(0, int(y), self.width(), int(y))
                y += h
                continue

            if action.section:
                font = self.font()
                font.setBold(True)
                font.setPointSizeF(font.pointSizeF() - 0.5)
                painter.setFont(font)
                painter.setPen(QColor(150, 150, 150))
                painter.drawText(
                    QRectF(0, y, self.width(), h),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "  " + action.text.replace("&", ""),
                )
                y += h
                continue

            row = QRectF(0, y, self.width(), h)
            hovered = i == self._hover_index
            if hovered:
                self._paint_hover(painter, row)

            font = self.font()
            font.setBold(True)
            font.setPointSize(11)
            painter.setFont(font)
            text_color = QColor(224, 224, 224) if action.enabled else QColor(130, 130, 130)

            x = self.H_PAD
            if action.checkable:
                self._paint_check(painter, QRectF(x, y, self.CHECK_W, h), action.checked, on_accent=hovered)
                x += self.CHECK_W

            text_rect = QRectF(x, y, self.width() - x - self.H_PAD - self._shortcut_w(action), h)
            text_str = action.text.replace("&", "")
            if text_str:
                if hovered and action.enabled:
                    for dx, dy, alpha in _SHADOW_PASSES:
                        painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
                        painter.drawText(text_rect.adjusted(dx, dy, dx, dy), Qt.AlignLeft | Qt.AlignVCenter, text_str)
                painter.setPen(QPen(text_color, 1))
                painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text_str)

            if action.shortcut_text:
                sw = self._text_metrics.horizontalAdvance(action.shortcut_text)
                sc_rect = QRectF(self.width() - self.H_PAD - sw, y, sw, h)
                if hovered and action.enabled:
                    for dx, dy, alpha in _SHADOW_PASSES:
                        painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
                        painter.drawText(sc_rect.adjusted(dx, dy, dx, dy), Qt.AlignRight | Qt.AlignVCenter, action.shortcut_text)
                painter.setPen(QPen(QColor(160, 160, 160) if not hovered or not action.enabled else text_color, 1))
                painter.drawText(sc_rect, Qt.AlignRight | Qt.AlignVCenter, action.shortcut_text)

            y += h

        painter.end()

    def _shortcut_w(self, action: StripeMenuAction) -> float:
        if not action.shortcut_text:
            return 0.0
        return self.SHORTCUT_GAP + self._text_metrics.horizontalAdvance(action.shortcut_text)

    def _paint_check(self, painter: QPainter, rect: QRectF, checked: bool, on_accent: bool = False):
        if not checked:
            return
        c = rect.center()
        s = 4.0
        color = QColor(0, 0, 0) if on_accent else Theme.ACCENT
        painter.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(c.x() - s, c.y())
        path.lineTo(c.x() - s * 0.3, c.y() + s)
        path.lineTo(c.x() + s, c.y() - s)
        painter.drawPath(path)

    def _paint_hover(self, painter: QPainter, row: QRectF):
        region = QPainterPath()
        region.addRoundedRect(row, self.CORNER - 1, self.CORNER - 1)
        _paint_tilted_accent(painter, row, region)
        shader = StripeShader(color=Theme.ACCENT)
        shader.paint(painter, region, zoom=1.0)
        painter.setPen(QPen(QColor(Theme.ACCENT.red(), Theme.ACCENT.green(), Theme.ACCENT.blue(), 120), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(row, self.CORNER - 1, self.CORNER - 1)


# --------------------------------------------------------------------------- #
# Menu bar (top bar: File / Edit / Render)
# --------------------------------------------------------------------------- #
class StripeMenuBar(QWidget):
    """A horizontal bar of top-level menu buttons, each opening a StripeMenu.

    Replaces QMenuBar so the dropdowns (and their hover highlights) are all
    custom-painted with the StripeShader.
    """

    BTN_H_PAD = 12
    HEIGHT = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFixedHeight(self.HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self._titles: list[str] = []
        self._menus: dict[str, StripeMenu] = {}
        self._hover_index = -1
        self._open_index = -1
        self._pending_switch = -1
        self._switching = False
        self._drag_active = False

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self.update)

    def _sync_anim_timer(self) -> None:
        if self._hover_index >= 0 or self._open_index >= 0:
            self._anim_timer.start()
        else:
            self._anim_timer.stop()

    # -- construction ------------------------------------------------------
    def add_menu(self, title: str) -> StripeMenu:
        clean = title.replace("&", "")
        menu = StripeMenu(title=clean, shortcut_parent=self)
        self._menus[clean] = menu
        self._titles.append(clean)
        self.update()
        return menu

    def menu(self, title: str) -> StripeMenu | None:
        return self._menus.get(title.replace("&", ""))

    # -- hit test ----------------------------------------------------------
    def _title_rects(self):
        rects = []
        x = 0
        fm = QFontMetrics(self.font())
        for t in self._titles:
            w = fm.horizontalAdvance(t) + 2 * self.BTN_H_PAD
            rects.append((x, w))
            x += w
        return rects

    def _index_at(self, pos_x: float) -> int:
        for i, (x, w) in enumerate(self._title_rects()):
            if x <= pos_x <= x + w:
                return i
        return -1

    def _finish_current_menu(self) -> None:
        if self._open_index < 0:
            return
        self._menus[self._titles[self._open_index]]._finish()

    def _open_title_at(self, idx: int) -> None:
        self._pending_switch = -1
        self._open_index = idx
        self._sync_anim_timer()
        self.update()
        while idx >= 0:
            title = self._titles[idx]
            menu = self._menus[title]
            left, _w = self._title_rects()[idx]
            global_bottom_left = self.mapToGlobal(QPoint(int(left), self.height()))
            menu.exec(global_bottom_left, trigger_buttons=(Qt.LeftButton,))
            self._open_index = -1
            self.update()
            idx = self._pending_switch
            self._pending_switch = -1
            if idx >= 0:
                self._open_index = idx
                self._sync_anim_timer()
                self.update()
        cursor_pos = self.mapFromGlobal(self.cursor().pos())
        if 0 <= cursor_pos.y() <= self.height():
            self._hover_index = self._index_at(cursor_pos.x())
        else:
            self._hover_index = -1
        self._sync_anim_timer()
        self.update()

    # -- interaction -------------------------------------------------------
    def mouseMoveEvent(self, event):
        idx = self._index_at(event.position().x())
        if idx != self._hover_index:
            self._hover_index = idx
            self._sync_anim_timer()
            self.update()
            if self._open_index >= 0 and idx >= 0 and idx != self._open_index and not self._switching:
                self._switching = True
                self._pending_switch = idx
                self._finish_current_menu()
                self._switching = False
        self._forward_drag_move(event.globalPosition())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self._index_at(event.position().x())
            if idx >= 0:
                self._drag_active = True
                if self._open_index >= 0:
                    if idx == self._open_index:
                        self._pending_switch = -1
                    else:
                        self._pending_switch = idx
                    self._finish_current_menu()
                else:
                    self._open_title_at(idx)
                if self._open_index < 0:
                    self._drag_active = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_active:
            self._drag_active = False
            self._forward_drag_release(event.globalPosition())
        super().mouseReleaseEvent(event)

    def _forward_drag_move(self, global_pos) -> None:
        if self._drag_active and self._open_index >= 0:
            self._menus[self._titles[self._open_index]]._update_hover_from_global(global_pos)

    def _forward_drag_release(self, global_pos) -> None:
        if self._open_index >= 0:
            self._menus[self._titles[self._open_index]]._release_from_global(global_pos)

    def leaveEvent(self, event):
        self._hover_index = -1
        self._sync_anim_timer()
        self.update()
        super().leaveEvent(event)

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(38, 38, 38))
        font = self.font()
        font.setBold(True)
        font.setPointSize(11)
        painter.setFont(font)
        for i, (x, w) in enumerate(self._title_rects()):
            active = i == self._open_index
            hovered = i == self._hover_index and self._open_index < 0
            if active or hovered:
                rect = QRectF(x, 0, w, self.height())
                region = QPainterPath()
                region.addRect(rect)
                _paint_tilted_accent(painter, rect, region)
                shader = StripeShader(color=Theme.ACCENT)
                shader.paint(painter, region, zoom=1.0)
            text_rect = QRectF(QPoint(int(x), 0), QPoint(int(x + w), self.height()))
            if active or hovered:
                for dx, dy, alpha in _SHADOW_PASSES:
                    painter.setPen(QPen(QColor(0, 0, 0, alpha), 1))
                    painter.drawText(text_rect.adjusted(dx, dy, dx, dy), Qt.AlignHCenter | Qt.AlignVCenter, self._titles[i])
            painter.setPen(QPen(QColor(230, 230, 230), 1))
            painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._titles[i])
        painter.end()
