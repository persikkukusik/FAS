import sys
sys.path.insert(0, "/home/persikkukusik/Documents/funny-animation-software")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QTimer, QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from ui.menus import StripeMenu, stripe_menu_open

app = QApplication.instance() or QApplication(sys.argv)
QTimer.singleShot(15000, app.quit)  # failsafe for the blocking exec loops

host = QWidget()
host.resize(500, 400)
host.move(0, 0)
host.show()
app.processEvents()

menu = StripeMenu("Test", host=host)
menu.add_action("Alpha", lambda *a: print("alpha"))
menu.add_action("Beta", lambda *a: print("beta"))
menu.add_action("Gamma", lambda *a: print("gamma"))


def mk(type_, gpos):
    return QMouseEvent(type_, QPointF(0, 0), QPointF(gpos), QPointF(gpos),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


def report(tag):
    print(f"{tag}: visible={menu.isVisible()} menu_open={stripe_menu_open()} "
          f"loop={'y' if menu._loop is not None else 'n'}")


out = None
pos = host.mapToGlobal(QPoint(30, 30))


def outside():
    g = menu.mapToGlobal(QPoint(0, 0))
    return g - QPoint(60, 60)


# --- run 1: bare LMB release OUTSIDE popup should close it ---
def run1_step():
    report("open-after")
    QApplication.sendEvent(host, mk(QEvent.MouseButtonRelease, outside()))


QTimer.singleShot(150, run1_step)
result1 = menu.exec(pos)
report("final-after-outside-release")
ok1 = (result1 is None) and (not menu.isVisible()) and (not stripe_menu_open())
print("PASS run1" if ok1 else "FAIL run1")

# --- run 2: release INSIDE popup keeps it open; then press outside closes ---
def run2_step():
    QApplication.sendEvent(host, mk(QEvent.MouseButtonRelease,
                                    menu.mapToGlobal(QPoint(10, 10))))


def run2_step2():
    report("after-inside-release (expect open)")
    QApplication.sendEvent(host, mk(QEvent.MouseButtonPress, outside()))


def run2_step3():
    report("final-after-outside-press (expect closed)")
    ok2a = menu.isVisible() and stripe_menu_open()
    print("PASS run2-inside-keeps" if ok2a else "FAIL run2-inside-keeps")
    app.quit()


QTimer.singleShot(150, run2_step)
QTimer.singleShot(300, run2_step2)
QTimer.singleShot(450, run2_step3)
result2 = menu.exec(pos)
ok2 = (result2 is None) and (not menu.isVisible()) and (not stripe_menu_open())
print("PASS run2-final" if ok2 else "FAIL run2-final")