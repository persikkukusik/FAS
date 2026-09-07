"""Regression tests for mask hit-testing and outline bugs.

Bug 1: a layer made into a mask (wrap *or* erase) keeps its *full* hitbox —
clicks land on the mask (or on the base it masks) even where nothing is
visible.  Both symbol/container masks and plain shape/path masks are affected.

Bug 2: when a mask is a *symbol* (container), the boolean "visible union" used
for outlines computes the erased/clipped hole from the container's own
(empty) local shape, so the object affected by the mask keeps its full shape
in the outline.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF

from core.model import SceneObject, Transform, Scene
from ui.render import render_scene_image


def rect(name, w=100, h=80, x=0.0, y=0.0, color="#cccccc"):
    return SceneObject(
        name=name,
        shape_type="rect",
        shape_data={"width": w, "height": h},
        transform=Transform(x=x, y=y),
        color=color,
    )


def circle(name, r=50, x=0.0, y=0.0, color="#cccccc"):
    return SceneObject(
        name=name,
        shape_type="circle",
        shape_data={"radius": r},
        transform=Transform(x=x, y=y),
        color=color,
    )


def container(name, children=None, x=0.0, y=0.0):
    obj = SceneObject(
        name=name,
        shape_type="container",
        transform=Transform(x=x, y=y),
    )
    obj.children = list(children or [])
    return obj


# --------------------------------------------------------------------------- #
# Bug 1: mask hitboxes must be clipped to the visible region
# --------------------------------------------------------------------------- #
class TestWrapMaskHitboxClipped:
    def test_wrap_mask_shape_not_hit_outside_base(self, stage):
        """A wrap-mask circle overlapping a base: clicks on the part of the
        mask that sticks out past the base must NOT select the mask."""
        base = rect("Base", 200, 200, x=300, y=300)
        # Circle sits on the base's lower edge: spans y 300+20..300+140.
        # The base reaches y = 400, so the band 100..140 is outside it.
        mask = circle("Mask", r=60, x=300, y=380)
        mask.is_mask = True
        mask.mask_mode = "wrap"
        stage.scene.objects = [base, mask]
        stage._obj_render_cache.clear()

        # Inside the intersection -> selectable.
        hit_visible = stage.hit_test(QPointF(300, 350))
        assert hit_visible is mask

        # Inside the mask but past the base's edge -> should be clipped away.
        hit_hidden = stage.hit_test(QPointF(300, 430))
        assert hit_hidden is not mask

    def test_wrap_mask_container_not_hit_outside_base(self, stage):
        """Same as above but the mask is a *symbol* with a child shape."""
        base = rect("Base", 200, 200, x=300, y=300)
        child = circle("Child", r=60, x=0, y=80)
        mask = container("MaskSymbol", children=[child], x=300, y=300)
        mask.is_mask = True
        mask.mask_mode = "wrap"
        stage.scene.objects = [base, mask]
        stage._obj_render_cache.clear()

        hit_visible = stage.hit_test(QPointF(300, 350))
        assert hit_visible is not None
        assert hit_visible.name == "Child"

        hit_hidden = stage.hit_test(QPointF(300, 430))
        assert hit_hidden is None


class TestEraseMaskHitboxClipped:
    def test_base_not_hit_in_erased_hole(self, stage):
        """A base rect with an erase-mask hole through its middle: clicking in
        the hole must not select the erased base."""
        base = rect("Base", 200, 200, x=300, y=300)
        eraser = circle("Eraser", r=40, x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        stage.scene.objects = [base, eraser]
        stage._obj_render_cache.clear()

        hit_hole = stage.hit_test(QPointF(300, 300))
        # The erased hole is empty space: neither the base nor the (invisible)
        # eraser is pickable there.
        assert hit_hole is None

        # Still-visible part of the base -> selectable.
        hit_base = stage.hit_test(QPointF(300, 350))
        assert hit_base is base

    def test_erase_mask_not_hit_outside_base(self, stage):
        """An erase-mask circle bigger than its base: clicks on the part of
        the eraser that hangs off the base select nothing."""
        base = rect("Base", 100, 100, x=300, y=300)
        eraser = circle("Eraser", r=100, x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        stage.scene.objects = [base, eraser]
        stage._obj_render_cache.clear()

        # The erased hole is empty space -> nothing is pickable there.
        hit_zone = stage.hit_test(QPointF(300, 300))
        assert hit_zone is None

        # Inside the eraser but past the base's edge -> clipped away.
        hit_off = stage.hit_test(QPointF(360, 300))
        assert hit_off is None

    def test_erase_symbol_base_not_hit_in_hole(self, stage):
        """Erase mask as a symbol: the affected base symbol's shape must be
        subtracted in the hit test too."""
        inner = rect("Wall", 200, 200, x=0, y=0)
        base_sym = container("Room", children=[inner], x=300, y=300)
        eraser_child = circle("Drill", r=40, x=0, y=0)
        eraser = container("Drill", children=[eraser_child], x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        stage.scene.objects = [base_sym, eraser]
        stage._obj_render_cache.clear()

        hit_hole = stage.hit_test(QPointF(300, 300))
        assert hit_hole is not base_sym
        assert hit_hole is not inner

        hit_visible = stage.hit_test(QPointF(300, 350))
        assert hit_visible is inner or hit_visible is base_sym


# --------------------------------------------------------------------------- #
# Bug 2: container/symbol erase masks must punch holes in outlines
# --------------------------------------------------------------------------- #
class TestSymbolMaskOutline:
    def test_parent_outline_accounts_for_erase_symbol(self, stage):
        """Two symbols (one a wrap source, one an erase mask) inside a parent:
        the parent's visible union used for the outline must punch the erase
        hole, not show the affected symbol's full shape."""
        wall = rect("Wall", 200, 200, x=0, y=0)
        base_sym = container("Room", children=[wall], x=300, y=300)

        drill = circle("Drill", r=40, x=0, y=0)
        erase_sym = container("Drill", children=[drill], x=300, y=300)
        erase_sym.is_mask = True
        erase_sym.mask_mode = "erase"

        parent = container("Stage", children=[base_sym, erase_sym])
        stage.scene.objects = [parent]
        stage._obj_render_cache.clear()

        visible = stage._subtree_visible_union(parent)
        # Centre of the parent was erased by the drill symbol -> not covered.
        assert not visible.contains(QPointF(300, 300))
        # An off-centre spot survives -> covered.
        assert visible.contains(QPointF(300, 360))

    def test_parent_outline_accounts_for_wrap_symbol(self, stage):
        """A wrap-mask symbol clips its own subtree to the base symbol's
        silhouette in the parent's outline."""
        wall = rect("Wall", 120, 120, x=0, y=0)
        base_sym = container("Room", children=[wall], x=300, y=300)

        frame_child = circle("Frame", r=80, x=0, y=0)
        wrap_sym = container("Frame", children=[frame_child], x=300, y=300)
        wrap_sym.is_mask = True
        wrap_sym.mask_mode = "wrap"

        parent = container("Stage", children=[base_sym, wrap_sym])
        stage.scene.objects = [parent]
        stage._obj_render_cache.clear()

        visible = stage._subtree_visible_union(parent)
        # The wrap symbol is confined to the base's rect (120x120) -> a point
        # inside the wrap circle but past the base's edge is not covered.
        assert not visible.contains(QPointF(300, 365))
        # Inside both -> covered.
        assert visible.contains(QPointF(300, 300))

# --------------------------------------------------------------------------- #
# Bug 3: a symbol turned into a mask must keep clipping its own inner masked
# content (a nested mask previously REPLACED the inherited clip, so the inner
# mask's intersection - or the erased base - leaked past the parent symbol's
# boundary).
# --------------------------------------------------------------------------- #

def rgb(img, x, y):
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


class TestNestedMaskClip:
    """A symbol containing a base + mask, with the symbol itself a wrap mask
    sitting over an outer base O. The rendered content must stay inside O."""

    def _scene(self, inner_children):
        o = rect("O", 100, 100, x=256, y=256, color="#eeeeee")
        s = container("Symbol", inner_children, x=256, y=256)
        s.is_mask = True
        s.mask_mode = "wrap"
        sc = Scene()
        sc.objects = [o, s]
        return sc

    def test_inner_wrap_does_not_leak_outside_parent(self, app):
        inner_base = rect("B", 120, 120, x=0, y=0, color="#ff0000")
        inner_mask = circle("M", 70, x=0, y=0, color="#00ff00")
        inner_mask.is_mask = True
        inner_mask.mask_mode = "wrap"
        sc = self._scene([inner_base, inner_mask])

        img = render_scene_image(sc)
        # Past O's edge (x>306): the inner mask must have been clipped by the
        # the parent symbol's clip -> neither red nor green may show.
        assert rgb(img, 311, 256) == (255, 255, 255)
        # Inside O, the inner wrap still shows its intersection (green on top).
        assert rgb(img, 256, 240) == (0, 255, 0)

    def test_inner_erase_does_not_leak_outside_parent(self, app):
        inner_base = rect("B", 120, 120, x=0, y=0, color="#ff0000")
        eraser = circle("E", 30, x=0, y=0, color="#00ff00")
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        sc = self._scene([inner_base, eraser])

        img = render_scene_image(sc)
        # Past O's edge, the erased base must stay clipped -> no red.
        assert rgb(img, 311, 256) == (255, 255, 255)
        # Still inside O but not carved by the eraser -> base shows red.
        assert rgb(img, 256, 215) == (255, 0, 0)
        # Carved by the eraser -> outer base's gray shows, not red.
        assert rgb(img, 256, 256) != (255, 0, 0)


# --------------------------------------------------------------------------- #
# Bug 4: an erase mask must carve the base's HITBOX, not just its pixels.
# The hole sinks down to every descendant of the base (the renderer clips the
# whole subtree to the base-minus-holes), so nothing inside the erased region
# may be picked. A locked eraser is the clearest case: it still carves on
# screen but is not itself selectable, so the hole must show up as empty.
# --------------------------------------------------------------------------- #
class TestEraseHitboxHoles:
    def test_plain_base_not_pickable_through_hole_when_eraser_locked(self, stage):
        base = rect("Base", 200, 200, x=300, y=300)
        eraser = circle("Eraser", r=40, x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        eraser.locked = True
        stage.scene.objects = [base, eraser]
        stage._obj_render_cache.clear()

        assert stage.hit_test(QPointF(315, 315)) is None
        assert stage.hit_test(QPointF(300, 350)) is base

    def test_container_child_not_pickable_through_hole(self, stage):
        wall = rect("Wall", 200, 200, x=0, y=0)
        room = container("Room", children=[wall], x=300, y=300)
        eraser = circle("Eraser", r=40, x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        eraser.locked = True
        stage.scene.objects = [room, eraser]
        stage._obj_render_cache.clear()

        # The hole carved out of Room's surface swallows its child's hitbox.
        assert stage.hit_test(QPointF(315, 315)) is None
        # Away from the hole, the child is still reachable.
        assert stage.hit_test(QPointF(300, 350)) is wall

    def test_eraser_unlocked_hole_is_empty(self, stage):
        base = rect("Base", 200, 200, x=300, y=300)
        eraser = circle("Eraser", r=40, x=300, y=300)
        eraser.is_mask = True
        eraser.mask_mode = "erase"
        stage.scene.objects = [base, eraser]
        stage._obj_render_cache.clear()

        # Hovering/clicking the erased hole reacts to nothing at all.
        assert stage.hit_test(QPointF(315, 315)) is None
        assert stage.hit_test(QPointF(300, 350)) is base
