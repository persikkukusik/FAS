from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
)


class DockSpec:
    def __init__(self, dock_id: str, name: str, factory, header_factory=None):
        self.dock_id = dock_id
        self.name = name
        self.factory = factory
        self.header_factory = header_factory


class DockManager:
    """Generic registry of every available dock (id -> name + factory).

    Each slot creates its own instances on demand, so the same dock can be
    shown in multiple slots at once and no widget is ever reparented between
    slots (which previously caused blank rendering).
    """

    def __init__(self, context):
        self.context = context
        self._specs: dict[str, DockSpec] = {}

    def register(self, dock_id: str, name: str, factory, header_factory=None) -> None:
        self._specs[dock_id] = DockSpec(dock_id, name, factory, header_factory)

    def specs(self) -> list[DockSpec]:
        return list(self._specs.values())

    def name(self, dock_id: str) -> str:
        return self._specs[dock_id].name

    def factory(self, dock_id: str):
        return self._specs[dock_id].factory

    def header_factory(self, dock_id: str):
        return self._specs[dock_id].header_factory


class _DockHeader(QWidget):
    HEIGHT = 28

    def __init__(self, dock_widget: "DockWidget"):
        super().__init__()
        self.dock_widget = dock_widget
        self.setFixedHeight(self.HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 8, 2)
        layout.setSpacing(6)

        self.menu_button = QPushButton("")
        self.menu_button.setFixedSize(22, 20)
        self.menu_button.setObjectName("dockMenuButton")
        self.menu_button.setCursor(Qt.PointingHandCursor)
        self.menu_button.setToolTip("Switch dock")
        self.menu_button.setFocusPolicy(Qt.NoFocus)
        # Open on *press* and drive the popup with the held button: the popup
        # stays up while LMB is held and the hovered option applies on release.
        self.menu_button.pressed.connect(self.dock_widget.show_dock_menu)
        layout.addWidget(self.menu_button)

        self.title_label = QLabel("")
        self.title_label.setStyleSheet("color: #c0c0c0; font-size: 11px;")
        layout.addWidget(self.title_label, 1)

        self._extras: list[QWidget] = []
        self._extras_parent = QWidget(self)
        self._extras_layout = QHBoxLayout(self._extras_parent)
        self._extras_layout.setContentsMargins(0, 0, 0, 0)
        self._extras_layout.setSpacing(6)
        layout.addWidget(self._extras_parent)

        self.setStyleSheet(
            """
            QPushButton#dockMenuButton {
                background-color: #4a4a4a;
                border: 1px solid #5a5a5a;
                border-radius: 3px;
            }
            QPushButton#dockMenuButton:hover {
                background-color: #5a5a5a;
            }
            QPushButton#dockMenuButton:pressed {
                background-color: #3a3a3a;
            }
            """
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(40, 40, 40))
        painter.setPen(QPen(QColor(60, 60, 60), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        painter.end()

    def set_extras(self, widgets: list[QWidget]) -> None:
        for w in self._extras:
            self._extras_layout.removeWidget(w)
            w.hide()
        self._extras = list(widgets)
        for w in self._extras:
            self._extras_layout.addWidget(w)
            w.show()

    def clear_extras(self) -> None:
        self.set_extras([])


class DockWidget(QWidget):
    """A dock slot: header with a switch button + content.

    This slot owns its own instances, so switching docks swaps content without
    sharing across slots and without forcing the content to resize.
    """

    dock_changed = Signal(str)

    def __init__(self, manager: DockManager, initial_dock_id: str):
        super().__init__()
        self.manager = manager
        self.current_dock_id: str | None = None
        self._current_widget: QWidget | None = None
        self._instances: dict[str, QWidget] = {}
        self._header_extras: dict[str, QWidget] = {}

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)

        self.header = _DockHeader(self)
        self._root.addWidget(self.header)

        self._content = QVBoxLayout()
        self._content.setContentsMargins(0, 0, 0, 0)
        self._content.setSpacing(0)
        self._content_box = QWidget(self)
        self._content_box.setLayout(self._content)
        self._root.addWidget(self._content_box, 1)

        self.set_dock(initial_dock_id)

    def current_widget(self) -> QWidget | None:
        return self._current_widget

    def current_extra(self) -> QWidget | None:
        return self._header_extras.get(self.current_dock_id)

    def _get_instance(self, dock_id: str) -> QWidget:
        if dock_id not in self._instances:
            self._instances[dock_id] = self.manager.factory(dock_id)(
                self.manager.context
            )
        return self._instances[dock_id]

    def _get_extra(self, dock_id: str) -> QWidget | None:
        header_factory = self.manager.header_factory(dock_id)
        if header_factory is None:
            return None
        if dock_id not in self._header_extras:
            self._header_extras[dock_id] = header_factory(self.manager.context)
        return self._header_extras[dock_id]

    def set_dock(self, dock_id: str) -> None:
        if dock_id == self.current_dock_id:
            return

        new_widget = self._get_instance(dock_id)
        if self._current_widget is not None and self._current_widget is not new_widget:
            self._content.removeWidget(self._current_widget)
            self._current_widget.hide()

        self._content.addWidget(new_widget, 1)
        new_widget.show()
        self._current_widget = new_widget
        self.current_dock_id = dock_id

        self.header.title_label.setText(self.manager.name(dock_id))
        self.header.menu_button.setToolTip(self.manager.name(dock_id))

        extra = self._get_extra(dock_id)
        self.header.set_extras([extra] if extra is not None else [])

        self.dock_changed.emit(dock_id)

    def show_dock_menu(self) -> None:
        from ui.menus import StripeMenu
        menu = StripeMenu()
        for spec in self.manager.specs():
            menu.add_action(
                spec.name,
                callback=lambda checked, dock_id=spec.dock_id: self.set_dock(dock_id),
                checkable=True,
                checked=spec.dock_id == self.current_dock_id,
            )

        global_pos = self.header.menu_button.mapToGlobal(
            self.header.menu_button.rect().bottomLeft()
        )
        menu.exec(global_pos, trigger_buttons=(Qt.LeftButton,))
