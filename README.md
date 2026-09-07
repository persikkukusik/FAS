# Animation Studio - Prototype

A tiny 2D vector/cutout animation prototype built with Python + PySide6 (Qt6).

This prototype tests whether a **Flash-like timeline + Blender-style G/R/S keyboard transforms** workflow feels good to animate with.

## Requirements

- Python 3.10+
- PySide6

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install PySide6
```

## Run

```bash
./run.sh
```

or

```bash
python main.py
```

## Controls

| Key | Action |
|-----|--------|
| Click | Select an object on the stage |
| `G` | Move (Blender-style translate) |
| `R` | Rotate |
| `S` | Scale |
| `G X` / `G Y` | Constrain move to X/Y axis |
| Click | Confirm current transform |
| `Esc` | Cancel current transform |
| `I` | Insert a keyframe for the selected object at the playhead |
| `Space` | Play / pause (loops between start and end frames) |
| `Alt` + wheel | Step the playhead (wheel up = back, wheel down = forward) |
| `Shift` + wheel | Zoom the viewport in/out (centered on the cursor) |
| `Shift` + middle-drag | Pan the viewport |
| `Delete` | Remove the selected object |
| `Ctrl+Z` | Undo |
| `Ctrl+Shift+Z` | Redo |

## Quick Start

1. **Select** a shape on the stage.
2. Press **`G`**, move the mouse, **click** to place it.
3. Press **`I`** to insert a keyframe.
4. Move the playhead (click the ruler in the timeline).
5. Press **`G`** again, move the shape, click.
6. Press **`I`**.
7. Move the playhead between the two frames — the shape interpolates.
8. Press **`Space`** to play.

## Outliner

The left panel lists scene objects. Each row has:
- **Visibility toggle** (click the dot/line on the left) — immediately hides/shows the object.
- **Mask toggle** (click the `M` on the right) — makes the object a non-destructive clipping mask for the object directly above it.

## SVG Import

**File → Import SVG...** imports simple SVG files (rect, circle, polygon, simple path). This is intentionally basic — the goal is to demonstrate the bring-external-vector-artwork-in workflow.

## Architecture

```
core/        # Scene model, transforms, animation/keyframes (Qt-free)
rendering/   # Rendering helpers, SVG import
ui/          # Qt widgets: stage, outliner, timeline, main window
main.py      # App entry point
```

## Scope / Limitations

This is a prototype, not a production app. It deliberately omits: audio, bones/IK, onion skinning, drawing tools, plugins, scripting, compositing, effects, multiple scenes, advanced easing, 3D, collaboration, and cloud features.
