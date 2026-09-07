import os
import sys

if getattr(sys, 'frozen', False):
    # Determine directory for frozen executable
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))

    # PyInstaller places the Qt plugins under different names/paths depending
    # on the PySide6 version, so probe the likely candidates and point Qt at
    # whichever actually exists. Without this, QApplication cannot find the
    # xcb/windows platform plugin in a frozen build.
    for rel in ('PySide6/Qt/plugins', 'PySide6/qt-plugins', 'PySide6/plugins',
                '_internal/PySide6/Qt/plugins',
                '_internal/PySide6/qt-plugins', '_internal/PySide6/plugins'):
        candidate = os.path.join(base_dir, rel)
        if os.path.isdir(candidate):
            os.environ['QT_PLUGIN_PATH'] = candidate
            break
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon, QPalette, QColor

from core.model import Scene, SceneObject, Transform
from core.animation import set_keyframe_at_current_frame
from ui.main_window import MainWindow


def create_default_scene() -> Scene:
    scene = Scene(start_frame=0, end_frame=500, fps=60)

    # Symbols are containers: they group shapes (and other Symbols) for
    # organisation. Everything actually drawable is a shape/path. Moving a
    # container moves its whole subtree as one unit.
    scene_container = scene.add_symbol("Scene")

    def in_container(container, obj: SceneObject) -> SceneObject:
        container.children.append(obj)
        return obj

    # --- Shapes group ----------------------------------------------------
    shapes = SceneObject(name="Shapes", shape_type="container")
    scene_container.children.append(shapes)

    circle = SceneObject(
        name="Circle",
        shape_type="circle",
        shape_data={"radius": 50},
        transform=Transform(x=150, y=200),
        color="#4488cc",
    )
    in_container(shapes, circle)

    # Pre-built animation: circle bounces across with a spin
    circle.transform.x, circle.transform.y, circle.transform.rotation = 150, 200, 0
    set_keyframe_at_current_frame(circle, 0)
    circle.transform.x, circle.transform.y, circle.transform.rotation = 380, 200, 180
    set_keyframe_at_current_frame(circle, 30)
    circle.transform.x, circle.transform.y, circle.transform.rotation = 150, 200, 360
    set_keyframe_at_current_frame(circle, 60)

    rect = SceneObject(
        name="Rectangle",
        shape_type="rect",
        shape_data={"width": 120, "height": 80},
        transform=Transform(x=180, y=340),
        color="#cc4444",
    )
    in_container(shapes, rect)

    triangle = SceneObject(
        name="Triangle",
        shape_type="polygon",
        shape_data={"points": [(0, -40), (35, 25), (-35, 25)]},
        transform=Transform(x=350, y=340),
        color="#44aa44",
    )
    in_container(shapes, triangle)

    # --- Character container (a Symbol holding two shapes) ---------------
    character = SceneObject(name="Character", shape_type="container")
    scene_container.children.append(character)

    head = SceneObject(
        name="Head",
        shape_type="circle",
        shape_data={"radius": 20},
        transform=Transform(x=0, y=-25),
        color="#ffcc88",
    )
    body = SceneObject(
        name="Body",
        shape_type="rect",
        shape_data={"width": 30, "height": 40},
        transform=Transform(x=0, y=15),
        color="#4488cc",
    )
    character.children = [head, body]
    character.transform = Transform(x=420, y=180)

    # --- Car container: nested Symbols inside the scene -------------------
    # A Symbol inside a Symbol - containers nest freely.
    car = SceneObject(name="Car", shape_type="container")
    scene_container.children.append(car)

    chassis = SceneObject(
        name="Chassis",
        shape_type="rect",
        shape_data={"width": 140, "height": 55},
        transform=Transform(x=0, y=0),
        color="#8a8a7a",
    )
    wheel_fl = SceneObject(
        name="Wheel FL",
        shape_type="circle",
        shape_data={"radius": 14},
        transform=Transform(x=-55, y=32),
        color="#222222",
    )
    wheel_fr = SceneObject(
        name="Wheel FR",
        shape_type="circle",
        shape_data={"radius": 14},
        transform=Transform(x=55, y=32),
        color="#222222",
    )
    turret = SceneObject(
        name="Turret",
        shape_type="circle",
        shape_data={"radius": 12},
        transform=Transform(x=-20, y=-32),
        color="#c8933a",
    )
    car.children = [chassis, wheel_fl, wheel_fr, turret]
    car.transform = Transform(x=200, y=120, rotation=-6)

    set_keyframe_at_current_frame(car, 0)
    car.transform.x, car.transform.y = 320, 260
    set_keyframe_at_current_frame(car, 60)
    car.transform.x, car.transform.y = 200, 120
    set_keyframe_at_current_frame(car, 120)

    set_keyframe_at_current_frame(turret, 0)
    turret.transform.rotation = 180
    set_keyframe_at_current_frame(turret, 30)
    turret.transform.rotation = 360
    set_keyframe_at_current_frame(turret, 60)

    return scene


def main():
    # Install a global exception hook so any error thrown inside a Qt slot
    # (which Qt would otherwise swallow, leaving the UI stuck) is printed to
    # stderr instead of silently freezing the app.
    def _excepthook(etype, value, tb):
        import traceback
        print("\n=== UNHANDLED EXCEPTION (UI may have frozen) ===", file=sys.stderr)
        traceback.print_exception(etype, value, tb)
        print("===============================================\n", file=sys.stderr)
    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    icon_path = Path(base_dir) / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(45, 45, 45))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.AlternateBase, QColor(40, 40, 40))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(50, 50, 50))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.Highlight, QColor(60, 100, 160))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    scene = create_default_scene()
    window = MainWindow(scene)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
