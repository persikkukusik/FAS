from __future__ import annotations

from .model import Scene, SceneObject, Transform


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _keyframe_velocity(keyframes: list, i: int) -> float:
    """Velocity (value / frame) at keyframe *i*, used as the Hermite tangent.

    The tangent is read from the *fixed* neighbouring interpolation modes,
    so the adaptive curve blends seamlessly out of and into them:

    * a linear neighbour contributes its constant slope
    * a constant neighbour contributes 0
    * an adaptive neighbour contributes nothing here

    When both neighbours are adaptive, the velocity is the central
    difference of the neighbouring keyframe values (a Catmull-Rom tangent).
    Every adaptive segment in a run then reads the *same* value at the
    shared keyframe, which keeps the whole run C¹-continuous (no velocity
    jump / stutter at adaptive→adaptive boundaries). Ends of a run rest at
    velocity 0, giving a smooth ease in/out.
    """
    n = len(keyframes)
    if n == 0:
        return 0.0

    left_mode = getattr(keyframes[i - 1], "interpolation", "adaptive") if i > 0 else None
    right_mode = getattr(keyframes[i], "interpolation", "adaptive") if i + 1 < n else None

    left_velocity: float | None = None
    right_velocity: float | None = None

    if left_mode in ("linear", "constant"):
        dt = keyframes[i].frame - keyframes[i - 1].frame
        if dt > 0:
            left_velocity = (
                (keyframes[i].value - keyframes[i - 1].value) / dt
                if left_mode == "linear"
                else 0.0
            )

    if right_mode in ("linear", "constant"):
        dt = keyframes[i + 1].frame - keyframes[i].frame
        if dt > 0:
            right_velocity = (
                (keyframes[i + 1].value - keyframes[i].value) / dt
                if right_mode == "linear"
                else 0.0
            )

    if left_velocity is not None and right_velocity is not None:
        if left_velocity == right_velocity:
            return left_velocity
        return (left_velocity + right_velocity) / 2.0

    if left_velocity is not None:
        return left_velocity
    if right_velocity is not None:
        return right_velocity

    # Both neighbours adaptive (or absent) -> central difference.
    if 0 < i < n - 1:
        dt = keyframes[i + 1].frame - keyframes[i - 1].frame
        if dt > 0:
            return (keyframes[i + 1].value - keyframes[i - 1].value) / dt
    return 0.0


def _hermite_value(
    p0: float, v0: float, p1: float, v1: float, dt: float, t: float
) -> float:
    """Evaluate a cubic Hermite spline at normalised time *t* ∈ [0, 1].

    The spline matches positions *p0*, *p1* and velocities *v0*, *v1*
    (value / frame) at the segment boundaries, with *dt* being the
    segment duration in frames.
    """
    x = t
    x2 = x * x
    x3 = x2 * x
    h00 = 2.0 * x3 - 3.0 * x2 + 1.0
    h10 = x3 - 2.0 * x2 + x
    h01 = -2.0 * x3 + 3.0 * x2
    h11 = x3 - x2
    return h00 * p0 + h10 * dt * v0 + h01 * p1 + h11 * dt * v1


def _interpolate_segment(keyframes: list, i: int, t: float) -> float:
    a = keyframes[i]
    b = keyframes[i + 1]
    if a.frame == b.frame:
        return a.value
    mode = getattr(a, "interpolation", "adaptive")
    if mode == "constant":
        return a.value
    if mode == "linear":
        return a.value + (b.value - a.value) * t
    # adaptive (or unknown → treat as adaptive)
    dt = b.frame - a.frame
    v0 = _keyframe_velocity(keyframes, i)
    v1 = _keyframe_velocity(keyframes, i + 1)
    return _hermite_value(a.value, v0, b.value, v1, dt, t)


def interpolate_channel(keyframes: list, frame: int) -> float | None:
    if not keyframes:
        return None
    if len(keyframes) == 1:
        return keyframes[0].value
    if frame <= keyframes[0].frame:
        return keyframes[0].value
    if frame >= keyframes[-1].frame:
        return keyframes[-1].value
    for i in range(len(keyframes) - 1):
        kf_a = keyframes[i]
        kf_b = keyframes[i + 1]
        if kf_a.frame <= frame <= kf_b.frame:
            if kf_a.frame == kf_b.frame:
                return kf_a.value
            t = (frame - kf_a.frame) / (kf_b.frame - kf_a.frame)
            return _interpolate_segment(keyframes, i, t)
    return keyframes[-1].value


def apply_interpolation(scene: Scene, frame: int) -> None:
    channels = {
        "position_x": "x",
        "position_y": "y",
        "rotation": "rotation",
        "scale_x": "scale_x",
        "scale_y": "scale_y",
    }
    for obj in scene.iter_objects():
        for ch_key, attr in channels.items():
            kfs = obj.get_keyframes(ch_key)
            if kfs:
                val = interpolate_channel(kfs, frame)
                if val is not None:
                    setattr(obj.transform, attr, val)


def set_keyframe_at_current_frame(obj: SceneObject, frame: int) -> None:
    t = obj.transform
    obj.set_keyframe(frame, "position_x", t.x)
    obj.set_keyframe(frame, "position_y", t.y)
    obj.set_keyframe(frame, "rotation", t.rotation)
    obj.set_keyframe(frame, "scale_x", t.scale_x)
    obj.set_keyframe(frame, "scale_y", t.scale_y)
