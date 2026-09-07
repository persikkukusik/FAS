#!/usr/bin/env python3
"""
build.py -- slim, portable redistributable packager for AnimationStudio.

Produce a single-file portable binary for the current OS:

    Windows  ->  release/AnimationStudio.exe        (PyInstaller --onefile + UPX)
    Linux    ->  release/AnimationStudio-<arch>.AppImage (PyInstaller onedir -> squashfs)

What "slim" means here
----------------------
The app itself is ~1.5 MB of Python; PySide6 carries Qt.  A naive PyInstaller
build snapshots *every* Qt library + plugin from the PySide6 wheel (~200 MB).
This script throws away everything the app can never load:

  * every Qt module except Core/Gui/Widgets/Svg
    (this is a QtWidgets app -- no QML, WebEngine, Network, OpenGL,
    PrintSupport, Multimedia, ... => dozens of MB removed),
  * every Qt plugin except platform(xcb/windows) + svg image/icon formats,
  * SSL/TLS, sqlite, curses, tkinter, hashlib, decimal and other stdlib
    bits that are never imported (drops OpenSSL/LibreSSL ~8 MB + more),
  * Qt translator .qm files (the English fallback is compiled into Qt).

Reality check on the size target
--------------------------------
A 10 MB payload is physically impossible for any PySide6/PyQt program:
Qt alone ships its ICU Unicode tables (~30 MB) and Qt6Core+Gui+Widgets are
~25 MB of compiled C++ before CPython (~7 MB) and the PySide bindings are
added.  Realistic floors: ~28-40 MB compressed Windows .exe and
~45-70 MB Linux AppImage.  This script gets as close as Qt physically
allows, and prints a per-stage size breakdown so you can judge.

Usage
-----
    python build.py                 # build for the current OS
    python build.py --windows       # Windows .exe  (run ON a Windows box)
    python build.py --linux         # Linux AppImage
    python build.py --no-upx        # skip UPX compression
    python build.py --no-appimage   # Linux: stop after the dist dir
    python build.py --skip-env      # reuse an existing .buildvenv
    python build.py --smoke-test-only  # just re-test an existing build

Notes
-----
* PyInstaller cannot cross-compile: build the .exe on Windows and the
  AppImage on Linux (run this script on each platform you want to ship).
  On a Linux/macOS dev machine, use .github/workflows/build.yml instead --
  it builds the .exe natively on a windows-latest runner and uploads it
  as a downloadable artifact, no local Windows box or emulation needed.
* The script bootstraps its own isolated venv (`.buildvenv`), so your dev
  environment stays untouched.  Needs an internet connection on first run.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "AnimationStudio"
ROOT = Path(__file__).resolve().parent
APP_ENTRY = ROOT / "main.py"
ICON_SVG = ROOT / "icon.svg"

BUILD_VENV = ROOT / ".buildvenv"
BUILD_DIR = ROOT / ".build"
DIST_DIR = BUILD_DIR / "dist"
WORK_DIR = BUILD_DIR / "work"
RELEASE_DIR = ROOT / "release"
TOOLS_DIR = ROOT / ".tools"  # appimagetool cache (BUILD_DIR gets wiped each run)

APPIMAGE_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/"
    "continuous/appimagetool-x86_64.AppImage"
)


# --------------------------------------------------------------------------
# Excludes / plugin policy
# --------------------------------------------------------------------------

# PyInstaller `excludes`: stdlib + tooling modules the app never imports.
PYMOD_EXCLUDES = [
    "ssl", "_ssl", "hashlib", "_hashlib", "sqlite3", "_sqlite3", "multiprocessing",
    "tkinter", "turtle", "turtledemo", "idlelib", "test", "unittest", "pydoc",
    "doctest", "curses", "_curses", "dbm", "_dbm", "gdbm", "_gdbm", "email",
    "http", "urllib", "xmlrpc", "ftplib", "nntplib", "poplib", "smtplib",
    "telnetlib", "socketserver", "mailbox", "webbrowser", "netrc",
    "pstats", "cProfile", "profile", "trace", "lib2to3", "ensurepip",
    "distutils", "venv",
]

# PySide6 bindings the app does not use (it is QtWidgets-only).  Excluding them
# prevents the corresponding C++ Qt .so/.dll + plugins from being collected.
QT_MODULE_EXCLUDES = [
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic",
    "Qt3DRender", "QtBluetooth", "QtCharts", "QtDataVisualization",
    "QtDesigner", "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtHttpServer",
    "QtLocation", "QtMultimedia", "QtMultimediaWidgets", "QtNetwork",
    "QtNetworkAuth", "QtNfc", "QtOpenGL", "QtOpenGLWidgets", "QtPdf",
    "QtPdfWidgets", "QtPositioning", "QtQml", "QtQuick", "QtQuick3D",
    "QtQuickControls2", "QtQuickTest", "QtQuickWidgets", "QtRemoteObjects",
    "QtScxml", "QtSensors", "QtSerialBus", "QtSerialPort", "QtSpatialAudio",
    "QtSql", "QtStateMachine", "QtSvgWidgets", "QtTest", "QtTextToSpeech",
    "QtUiTools", "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick",
    "QtWebEngineWidgets", "QtWebSockets", "QtWebView", "QtXml",
]
QT_MODULE_EXCLUDES = [f"PySide6.{name}" for name in QT_MODULE_EXCLUDES]

# Qt C++ library names (lib/name forms on both platforms) for dropped modules.
QT_LIB_DROP_PATTERNS = [
    r"(?:lib)?Qt6?(?:3D|Bluetooth|Charts|DataVisualization|Designer|Help|HttpServer|"
    r"Location|Multimedia|Network|NetworkAuth|Nfc|OpenGL|OpenGLWidgets|Pdf|Positioning|"
    r"Qml|Quick|RemoteObjects|Scxml|Sensors|Serial\w+|Sql|StateMachine|SvgWidgets|Test|"
    r"TextToSpeech|UiTools|WebChannel|WebEngine|WebSockets|WebView|Xml)\w*",
    # OpenSSL / LibreSSL (only pulled in by dropped ssl module + Qt Network/TLS)
    r"(?:lib)?(?:ssl|crypto)[-\d]*\.(?:so|dll)",
]

# plugin subdirs under PySide6/qt-plugins that we drop entirely
QT_PLUGIN_DROP_DIRS = {
    "accessibility", "egldeviceintegrations", "generic", "iconengines2",
    "imageformats2", "inputmethod", "networkaccess", "networkinformation",
    "platforminputcontexts", "platformthemes", "printbackends",
    "printsupport", "sceneGraph", "sqldrivers", "styles", "tls",
    "wayland-decoration-client", "wayland-graphics-integration-client",
    "wayland-graphics-integration-server", "wayland-shell-integration",
    "xcbglintegrations", "qmllint", "qmltooling", "designer", "vectorimageformats",
}

# Strict allow-list for plugin dirs we DO keep (platform-aware, see below)
QT_PLUGIN_KEEP_WINDOWS = {
    "imageformats": {"qsvg", "qico"},
    "iconengines": {"qsvgicon"},
    "platforms": {"qwindows", "qoffscreen", "qminimal"},
}
QT_PLUGIN_KEEP_LINUX = {
    "imageformats": {"qsvg"},
    "iconengines": {"qsvgicon"},
    "platforms": {"qxcb", "qoffscreen", "qminimal"},
}

QT_TRANSLATION_DIRNAMES = {"translations", "qt-translations"}

# PySide6's embedded signature support (runs on first binding import) does a
# straight `import logging/argparse/gettext` at module scope; with a
# dead-code-stripped build those stdlib modules are otherwise absent.
SIGNATURE_BOOTSTRAP_IMPORTS = ["logging", "argparse", "gettext"]


def _plugin_keep() -> dict:
    return QT_PLUGIN_KEEP_WINDOWS if os.name == "nt" else QT_PLUGIN_KEEP_LINUX


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw)


def mb(bytes_: int) -> str:
    return f"{bytes_ / (1024 * 1024):.1f} MB"


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def ensure_build_env() -> None:
    vipy = venv_python(BUILD_VENV)
    if vipy.exists() and (BUILD_VENV / ".buildok").exists():
        log(f"reusing build venv {BUILD_VENV}")
        return
    log(f"creating build venv {BUILD_VENV} ...")
    if BUILD_VENV.exists():
        shutil.rmtree(BUILD_VENV, ignore_errors=True)
    run([sys.executable, "-m", "venv", str(BUILD_VENV)], check=True)
    pip = BUILD_VENV / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    run([str(pip), "install", "-q", "--upgrade", "pip"], check=True)
    # Pin Qt to the *Essentials* wheel (no WebEngine/Multimedia bloat) and
    # Pillow so we can cook a Windows .ico out of the SVG.
    run([str(pip), "install", "-q", "PySide6-Essentials", "PyInstaller", "Pillow"],
        check=True)
    (BUILD_VENV / ".buildok").write_text("ok\n")


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------

def render_icon_png(dest_dir: Path) -> Path | None:
    """Rasterize icon.svg -> 256x256 PNG using PySide6 itself (no converter)."""
    png = dest_dir / "icon.png"
    script = (
        "import os,sys\n"
        "os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "from PySide6.QtCore import QRect,QSize\n"
        "from PySide6.QtGui import QGuiApplication,QImage,QPainter,QColor\n"
        "from PySide6.QtSvg import QSvgRenderer\n"
        "app=QGuiApplication([])\n"
        f"img=QImage(256,256,QImage.Format_ARGB32)\n"
        f"img.fill(QColor(0,0,0,0))\n"
        f"r=QSvgRenderer(r'{ICON_SVG}')\n"
        "p=QPainter(img); r.render(p,QRect(0,0,256,256)); p.end()\n"
        f"img.save(r'{png}','PNG')\n"
    )
    try:
        run([str(venv_python(BUILD_VENV)), "-c", script], check=True)
        return png
    except Exception as e:  # noqa: BLE001
        log(f"icon render failed ({e}); continuing without icon")
        return None


def make_ico(png: Path | None, dest_dir: Path) -> Path | None:
    if os.name != "nt" or png is None:
        return None
    ico = dest_dir / "icon.ico"
    try:
        from PIL import Image
        Image.open(png).convert("RGBA").save(
            ico, "ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                   (128, 128), (256, 256)])
        return ico
    except Exception as e:  # noqa: BLE001
        log(f"ico render failed ({e}); continuing without icon")
        return None


# --------------------------------------------------------------------------
# PyInstaller spec generation
# --------------------------------------------------------------------------

_SPEC = r"""# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    [r"@@ENTRY@@"],
    pathex=[],
    binaries=[],
    datas=[@@DATAS@@],
    hiddenimports=@@HIDDENIMPORTS@@,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=@@EXCLUDES@@,
    noarchive=False,
)

# ---------------------------------------------------------------- pruning
# PyInstaller's PySide6 hook snapshots every Qt lib + plugin from the wheel.
# We drop everything a QtWidgets-only app can never dlopen.  This is the
# single biggest lever for keeping the bundle small.
import re as _re
import os as _os

_lib_pat = _re.compile("@@LIB_PAT@@")
_plugin_keep = @@PLUGIN_KEEP@@
_plugin_drop_dirs = @@PLUGIN_DROP_DIRS@@
_trans_dirs = @@TRANSLATION_DIRS@@
_allowed_plugin_dirs = set(_plugin_keep) | {"platforms"}

def _stem(fn):
    s = _os.path.basename(fn)
    s = _re.sub(r"^lib", "", s, flags=_re.I)
    return _re.sub(r"\.(so(\.\d+)*|dll)$", "", s, flags=_re.I)

def _should_drop(dest, name):
    if _lib_pat.search(dest) or _lib_pat.search(name):
        return True
    d = dest.replace("\\", "/").lower()
    m = _re.search(r"(?:qt-plugins|plugins)/([\w-]+)/", d)
    if m:
        sub = m.group(1)
        if sub not in _allowed_plugin_dirs:
            return True
        keep = _plugin_keep.get(sub)
        if keep is not None and _stem(name) not in keep:
            return True
    for tdir in _trans_dirs:
        if f"{tdir}/" in d:
            return True
    return False

for _toc in (a.binaries, a.datas):
    _toc[:] = [_e for _e in _toc if not _should_drop(_e[0], _e[0] or _e[1])]

# ---------------------------------------------------------------- build
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=@@EXCLUDE_BINARIES@@,
    name=@@APP_NAME@@,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=@@UPX@@,
    console=@@CONSOLE@@,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    @@ICON_PARAM@@
)

if @@EXCLUDE_BINARIES@@:
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name=@@APP_NAME@@,
    )
"""


def site_plugins_dir() -> Path | None:
    """Path to the PySide6/Qt/plugins dir inside the build venv (if any)."""
    if os.name == "nt":
        base = BUILD_VENV / "Lib" / "site-packages"
    else:
        cands = list((BUILD_VENV / "lib").glob("python*/site-packages"))
        base = cands[0] if cands else None
    if base is None:
        return None
    plugins = base / "PySide6" / "Qt" / "plugins"
    return plugins if plugins.is_dir() else None


def plugin_datas() -> list[tuple[str, str]]:
    """Explicit data entries for the handful of Qt plugins the app needs.

    PyInstaller's PySide6 hook does not always collect the Qt plugins
    (it didn't on Linux with PySide6 6.11), so we add the required ones
    (platform + svg icon/image plugins) ourselves.
    """
    out: list[tuple[str, str]] = []
    src_root = site_plugins_dir()
    if src_root is None:
        return out
    for subdir, stems in _plugin_keep().items():
        src_dir = src_root / subdir
        if not src_dir.is_dir():
            continue
        for f in src_dir.iterdir():
            stem = f.name
            stem = re.sub(r"^lib", "", stem, flags=re.I)
            stem = re.sub(r"\.(so(\.\d+)*|dll)$", "", stem, flags=re.I)
            if stem in stems:
                out.append((str(f), f"PySide6/Qt/plugins/{subdir}"))
    return out


def make_spec(gui_console: bool, onedir: bool, use_upx: bool,
              png: Path | None, ico: Path | None) -> Path:
    # 'QtOpenGLWidgets' pattern is a superset of 'QtOpenGL' so the earlier
    # substring in the alternation would win; keep patterns as-is (all are
    # independent via word boundary guards).
    lib_pat = "|".join(f"(?:{p})" for p in QT_LIB_DROP_PATTERNS)
    excludes = repr([*PYMOD_EXCLUDES, *QT_MODULE_EXCLUDES])
    icon_param = f"icon={ascii(str(ico))}," if ico else "icon=None,"

    datas = [f"(r'{ICON_SVG}', '.')"]
    datas += [f"(r'{src}', '{dest}')" for src, dest in plugin_datas()]

    spec = (_SPEC
            .replace("@@ENTRY@@", str(APP_ENTRY))
            .replace("@@DATAS@@", ", ".join(datas))
            .replace("@@EXCLUDES@@", excludes)
            .replace("@@HIDDENIMPORTS@@", repr(SIGNATURE_BOOTSTRAP_IMPORTS))
            .replace("@@LIB_PAT@@", lib_pat)
            .replace("@@PLUGIN_KEEP@@", repr(_plugin_keep()))
            .replace("@@PLUGIN_DROP_DIRS@@", repr(QT_PLUGIN_DROP_DIRS))
            .replace("@@TRANSLATION_DIRS@@", repr(list(QT_TRANSLATION_DIRNAMES)))
            .replace("@@EXCLUDE_BINARIES@@", repr(onedir))
            .replace("@@APP_NAME@@", ascii(APP_NAME))
            .replace("@@UPX@@", repr(use_upx))
            .replace("@@CONSOLE@@", repr(gui_console))
            .replace("@@ICON_PARAM@@", icon_param))
    spec_path = BUILD_DIR / f"{APP_NAME}.spec"
    spec_path.write_text(spec)
    return spec_path


# --------------------------------------------------------------------------
# Build / prune / package
# --------------------------------------------------------------------------

def run_pyinstaller(spec_path: Path, onedir: bool) -> Path:
    cmd = [
        str(venv_python(BUILD_VENV)), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        str(spec_path),
    ]
    run(cmd, cwd=ROOT, check=True)
    exe_name = APP_NAME + (".exe" if os.name == "nt" else "")
    exe = (DIST_DIR / APP_NAME / exe_name) if onedir else (DIST_DIR / exe_name)
    if not exe.exists():
        raise SystemExit(f"PyInstaller produced no executable at {exe}")
    return exe


def prune_onedir(app_dir: Path) -> None:
    """Linux-only post-build prune.

    The PySide6 hook + bindepend snapshot a huge amount of *unused* system
    Qt/GTK stack into `_internal/` (gtk-3, glycin, a second ICU copy, ...).
    We delete everything that is not reachable from the files the app
    actually loads, by computing the DT_NEEDED closure from the anchors.
    """
    internal = app_dir / "_internal"
    if not internal.is_dir():
        return

    # --- remove Qt C++ libs of modules the app never loads ------------------
    junk_qt = {"libQt6EglFSDeviceIntegration.so.6", "libQt6EglFsKmsSupport.so.6",
               "libQt6WaylandClient.so.6", "libQt6WlShellIntegration.so.6"}
    qtlib = internal / "PySide6" / "Qt" / "lib"
    if qtlib.is_dir():
        for f in list(qtlib.iterdir()):
            if f.is_file() and f.name in junk_qt:
                f.unlink(missing_ok=True)
                log(f"  pruned Qt lib {f.name}")
        # also drop their dangling root symlinks
        for f in internal.iterdir():
            if f.is_symlink() and f.resolve() not in {q.resolve() for q in qtlib.iterdir()}:
                f.unlink(missing_ok=True)

    # --- map NEEDED-name -> real file (deduped via resolve()) ---------------
    name2path: dict[str, Path] = {}
    for p in internal.rglob("*"):
        if not p.is_file():
            continue
        real = p.resolve()
        for key in (p.name, real.name):
            name2path.setdefault(key, real)

    # --- anchors: things we certainly need ----------------------------------
    keep: set[Path] = set()
    queue = []

    def add(path: Path):
        real = path.resolve()
        if real not in keep:
            keep.add(real)
            queue.append(real)

    add(internal / "base_library.zip")
    if (internal / "icon.svg").exists():
        add(internal / "icon.svg")
    # the bootloader dlopens libpython at startup
    for libp in internal.glob("libpython*.so*"):
        add(libp)
    for sub in ("python3.14", "shiboken6"):
        d = internal / sub
        if d.is_dir():
            for f in d.rglob("*"):
                if f.is_file():
                    add(f)
    ps = internal / "PySide6"
    if ps.is_dir():
        for f in ps.iterdir():
            if f.is_file():
                add(f)
        qt_root = ps / "Qt"
        if qt_root.is_dir():
            for lib in (qt_root / "lib").glob("*"):
                if lib.is_file():
                    add(lib)
            plugins_root = qt_root / "plugins"
            if plugins_root.is_dir():
                for f in plugins_root.rglob("*"):
                    if f.is_file():
                        add(f)

    # --- BFS over DT_NEEDED --------------------------------------------------
    while queue:
        f = queue.pop()
        if f.suffix in {".zip", ".svg"}:
            continue
        try:
            out = subprocess.run(["patchelf", "--print-needed", str(f)],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                continue
        except Exception:  # noqa: BLE001
            continue
        for name in out.stdout.splitlines():
            name = name.strip()
            if name and name in name2path:
                add(name2path[name])

    # --- delete unreachable ELF/data files at _internal root ----------------
    removed_bytes = 0
    for f in list(internal.iterdir()):
        if not f.is_file() or f.is_symlink():
            continue
        real = f.resolve()
        if real in keep:
            continue
        # keep non-binary app data we didn't anchor explicitly
        if f.name in ("base_library.zip", "icon.svg"):
            continue
        try:
            with open(f, "rb") as fh:
                magic = fh.read(4)
        except OSError:
            continue
        if magic != b"\x7fELF":
            continue
        removed_bytes += f.stat().st_size
        f.unlink(missing_ok=True)
        log(f"  removed orphan {f.name}")

    # --- drop root symlinks that now point at nothing -----------------------
    for f in internal.iterdir():
        if f.is_symlink() and not f.exists():
            f.unlink(missing_ok=True)
            log(f"  removed dangling symlink {f.name}")

    if removed_bytes:
        log(f"  orphan cleanup freed {removed_bytes/1e6:.1f} MB")


# --------------------------------------------------------------------------
# Linux AppImage
# --------------------------------------------------------------------------

def ensure_appimagetool() -> Path:
    tool = TOOLS_DIR / ("appimagetool-x86_64.AppImage")
    if tool.exists():
        return tool
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    log(f"downloading {APPIMAGE_URL}")
    run(["curl", "-L", "--fail", "-o", str(tool), APPIMAGE_URL], check=True)
    tool.chmod(0o755)
    return tool


def build_appimage(app_dir: Path, png: Path | None) -> Path:
    tool = ensure_appimagetool()
    appdir = BUILD_DIR / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir, ignore_errors=True)

    # Mirrors the PyInstaller dist layout 1:1 inside the AppDir root.
    shutil.copytree(app_dir, appdir)

    (appdir / "AppRun").write_text(
        "#!/bin/sh\n"
        "set -e\n"
        'HERE="$(dirname "$(readlink -f "$0")")"\n'
        f'exec "$HERE/{APP_NAME}" "$@"\n')
    (appdir / "AppRun").chmod(0o755)

    (appdir / f"{APP_NAME}.desktop").write_text(
        "[Desktop Entry]\n"
        f"Name={APP_NAME}\n"
        "Comment=Tiny 2D vector animation editor\n"
        f"Exec={APP_NAME}\n"
        "Type=Application\n"
        "Categories=Graphics;\n"
        f"Icon={APP_NAME}\n"
        "Terminal=false\n")

    if png and png.exists():
        shutil.copy(png, appdir / f"{APP_NAME}.png")

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    out = RELEASE_DIR / f"{APP_NAME}-x86_64.AppImage"
    out.unlink(missing_ok=True)

    log("packing AppImage ...")
    attempts = [
        ["--comp", "xz"],                     # best ratio; "--mksquashfs-opt -Xbcj x86" crashes mksquashfs
        [],                                   # default (zstd) as a safe fallback
    ]
    for extra in attempts:
        try:
            run([str(tool), "--appimage-extract-and-run", *extra, str(appdir)],
                cwd=BUILD_DIR, check=True, stdout=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            log(f"  mksquashfs failed with {extra or 'default opts'}; trying next")
    else:
        raise SystemExit("AppImage build failed (mksquashfs)")

    cand = BUILD_DIR / f"{APP_NAME}-x86_64.AppImage"
    if cand.exists():
        shutil.move(str(cand), out)
    if not out.exists():
        raise SystemExit("AppImage build failed: no output produced")
    return out


# --------------------------------------------------------------------------
# Smoke tests
# --------------------------------------------------------------------------

def smoke_test(exe: Path, label: str) -> None:
    """Run the built app a few seconds. Timeout(exit 124) == event loop ran;
    SIGSEGV == a prune cut something actually needed."""
    for platform in ("offscreen", "xcb"):
        env = dict(os.environ, QT_QPA_PLATFORM=platform)
        if exe.name.endswith(".AppImage"):
            env["APPIMAGE_EXTRACT_AND_RUN"] = "1"  # avoids needing FUSE
        base = ["xvfb-run", "-a"] if shutil.which("xvfb-run") else []
        cmd = base + [str(exe)]
        try:
            p = run(cmd, env=env, timeout=10,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if p.returncode in (124,):       # we killed it -- it was running
                log(f"  [{label}/{platform}] OK (ran the event loop)")
            elif p.returncode == 0:
                log(f"  [{label}/{platform}] exited cleanly (OK)")
            else:
                log(f"  [{label}/{platform}] exit {p.returncode}")
                log("    stderr: " + p.stderr.decode(errors="replace").strip()[-800:])
        except subprocess.TimeoutExpired:
            log(f"  [{label}/{platform}] OK (ran the event loop)")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Slim portable packager for AnimationStudio.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--windows", dest="target", action="store_const", const="windows")
    p.add_argument("--linux", dest="target", action="store_const", const="linux")
    p.add_argument("--no-upx", action="store_true")
    p.add_argument("--no-appimage", action="store_true")
    p.add_argument("--skip-env", action="store_true")
    p.add_argument("--smoke-test-only", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    target = args.target or ("windows" if os.name == "nt" else "linux")
    if target == "windows" and os.name != "nt":
        raise SystemExit(
            "--windows requested but this is not a Windows host, and "
            "PyInstaller cannot cross-compile.\n"
            "Build the .exe on an actual Windows machine/VM, or use the "
            "provided .github/workflows/build.yml, which builds it on a "
            "windows-latest GitHub Actions runner and uploads it as an "
            "artifact.")
    log(f"target platform: {target}")
    log("honest size note: PySide6 apps are floored by Qt itself -- the goal "
        "here is the smallest *safe* build (no QML/WebEngine/Network/OpenGL), "
        "not a fictional 10 MB.")

    if args.smoke_test_only:
        exe = (DIST_DIR / APP_NAME / (APP_NAME + (".exe" if os.name == "nt" else "")))
        log(f"smoke-testing {exe}")
        smoke_test(exe, "existing-build")
        return 0

    if not args.skip_env:
        ensure_build_env()

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    png = render_icon_png(BUILD_DIR) if ICON_SVG.exists() else None
    ico = make_ico(png, BUILD_DIR)

    onedir = target == "linux"
    console = True                       # show stderr on Linux; windowed on Win
    use_upx = target == "windows" and not args.no_upx and bool(shutil.which("upx"))
    if target == "windows" and not args.no_upx and not use_upx:
        log("UPX not found on PATH; building without it")

    spec = make_spec(gui_console=console, onedir=onedir, use_upx=use_upx,
                     png=png, ico=ico)
    log("running PyInstaller ...")
    exe = run_pyinstaller(spec, onedir=onedir)

    if onedir:
        app_dir = DIST_DIR / APP_NAME
        before = dir_size(app_dir)
        prune_onedir(app_dir)
        after = dir_size(app_dir)
        log(f"dist size: {mb(before)} -> {mb(after)} (removed {mb(before - after)})")
        smoke_test(exe, "dist")
        if args.no_appimage:
            return 0
        appimage = build_appimage(app_dir, png)
        smoke_test(appimage, "appimage")
        final = (("Linux dist dir", app_dir), ("Linux AppImage", appimage))
    else:
        final = (("Windows portable .exe", exe),)

    log("=" * 62)
    log("BUILD COMPLETE")
    for name, path in final:
        size = dir_size(path) if path.is_dir() else path.stat().st_size
        log(f"  {name:<18} -> {path}")
        log(f"  {'':<18}    size {mb(size)}")
    log("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())