# picker.py
# 2D viewport pixel -> exact 3D world point, using a GPU offscreen draw
# of the viewport's own depth buffer read back via GPUFrameBuffer.read_depth.
# Verified on Blender 3.6.23. The APIs used are stable in 3.6 through 4.x.
# No raycast, no scene render engine, no draw handlers.
#
#   P_world = inv(window_matrix @ view_matrix) @ NDC(px, py, depth)
#
# The screenshot tool returns the whole AREA (includes headers); the
# offscreen buffer is the WINDOW region only. `_area_to_region_offset`
# converts between the two coordinate spaces.
#
# This module exposes exactly the two pickers the MCP server calls:
#   pick_point(px_img, py_img)                      -> pick_3d_point
#   pick_points_batch(points, ...)                  -> pick_3d_points_batch
# Everything else is internal plumbing.
#
# ---------------------------------------------------------------------------
# CRITICAL NOTE ON read_depth  (this was the root-cause bug):
#
#   fb.read_depth(x, y, w, h) returns a gpu.types.Buffer that is
#   TWO-DIMENSIONAL, shape (h, w), with ROW 0 = BOTTOM (GL convention).
#
#   WRONG (silently produces a degenerate object array; unprojects to the
#          wrong point, but round-trips still "pass" because the same bad
#          array is used both ways):
#       arr = np.array(depth_buf, dtype=np.float32).reshape(h, w)[::-1]
#
#   RIGHT (documented access):
#       arr = np.array(depth_buf.to_list(), dtype=np.float32)   # (h, w), row0=bottom
#       arr = arr[::-1].copy()                                  # row0=top (image)
# ---------------------------------------------------------------------------

import bpy
import gpu
import numpy as np

from mathutils import Vector


_BG_DEPTH = 0.9999   # NDC depth at/above this means "no geometry"
_NORMAL_STEP = 6     # pixels between samples for the finite-diff normal


# ---------------------------------------------------------------------------
# Viewport plumbing
# ---------------------------------------------------------------------------

def _iter_view3d_areas(area_ui_type="VIEW_3D"):
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.ui_type != area_ui_type:
                continue
            for region in area.regions:
                if region.type != 'WINDOW':
                    continue
                space = area.spaces.active
                r3d = getattr(space, "region_3d", None)
                if r3d is None:
                    continue
                yield win, area, region, space, r3d


def _get_view3d(area_ui_type="VIEW_3D"):
    """Return a VIEW_3D entry.

    Preference order:
      1) the area under the mouse (interactive use),
      2) the LARGEST VIEW_3D WINDOW region (matches what the screenshot
         tool captures; deterministic in headless / scripted contexts where
         bpy.context.window / mouse position are unreliable).

    Scenes here can contain multiple VIEW_3D areas with *different* cameras;
    picking the wrong one silently reads depth from another view.
    """
    win = bpy.context.window
    if win is not None:
        mx = getattr(win, "mouse_x", None)
        my = getattr(win, "mouse_y", None)
        if mx is not None and my is not None:
            for _w, area, region, space, r3d in _iter_view3d_areas(area_ui_type):
                if _w != win:
                    continue
                x0, y0 = area.x, area.y
                x1, y1 = x0 + area.width, y0 + area.height
                if x0 <= mx < x1 and y0 <= my < y1:
                    return _w, area, region, space, r3d

    best = None
    for entry in _iter_view3d_areas(area_ui_type):
        _w, area, region, space, r3d = entry
        key = region.width * region.height
        if best is None or key > best[0]:
            best = (key, entry)

    if best is not None:
        return best[1]
    return None


def _area_to_region_offset(area, region):
    """Offset from screenshot (area) coords to WINDOW-region coords, in
    top-left image coordinates. Subtract (off_x, off_y_top) from a
    screenshot pixel to get the matching region pixel."""
    off_x = region.x - area.x
    off_y_top = (area.y + area.height) - (region.y + region.height)
    return off_x, off_y_top


# ---------------------------------------------------------------------------
# Offscreen depth buffer (GPU, read_depth)
# ---------------------------------------------------------------------------

def _render_depth(space, region, r3d, W, H):
    """Draw the viewport into an offscreen buffer with its current
    shading and read the GL depth buffer.

    Returns an (H, W) float32 array in IMAGE coordinates (row 0 = top).
    """
    ofs = gpu.types.GPUOffScreen(W, H)
    with ofs.bind():
        fb = gpu.state.active_framebuffer_get()
        # CRITICAL: without an explicit clear, read_depth returns zeros.
        fb.clear(depth=1.0)
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set('LESS')
        ofs.draw_view3d(
            bpy.context.scene, bpy.context.view_layer,
            space, region,
            r3d.view_matrix, r3d.window_matrix,
        )
        depth_buf = fb.read_depth(0, 0, W, H)
        # Reset the state we touched (Blender state-restore is buggy
        # around offscreen binds).
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set('NONE')
    ofs.free()

    # The buffer is 2D (H, W), row 0 = bottom. to_list() is the documented
    # access; np.array(buffer) silently produces a degenerate array.
    raw = np.array(depth_buf.to_list(), dtype=np.float32)

    # Defensive shape normalisation (some builds could hand back flat or
    # transposed buffers).
    if raw.ndim == 1:
        raw = raw.reshape(H, W)
    if raw.shape != (H, W):
        if raw.shape == (W, H):
            raw = raw.T
        else:
            raw = raw.reshape(H, W)

    # GL row 0 = bottom -> flip so row 0 = top (image coordinates).
    return raw[::-1].copy()


# ---------------------------------------------------------------------------
# Unprojection
# ---------------------------------------------------------------------------

def _unproject(rx, ry, depth_arr, W, H, r3d):
    """Region pixel -> world Vector, or None at background."""
    xi = int(round(rx))
    yi = int(round(ry))
    if not (0 <= xi < W and 0 <= yi < H):
        return None
    d = float(depth_arr[yi, xi])   # depth_arr is image-ordered (row 0 = top)
    if d >= _BG_DEPTH:
        return None

    # GL convention: depth buffer row 0 is bottom, image row 0 is top.
    gx = xi
    gy = (H - 1) - yi
    nx = (gx + 0.5) / W * 2.0 - 1.0
    ny = (gy + 0.5) / H * 2.0 - 1.0
    nz = d * 2.0 - 1.0

    inv = (r3d.window_matrix @ r3d.view_matrix).inverted()
    v = inv @ Vector((nx, ny, nz, 1.0))
    if abs(v.w) < 1e-12:
        return None
    v /= v.w
    return Vector((v.x, v.y, v.z))


def _normal_at(rx, ry, depth_arr, W, H, r3d, step=_NORMAL_STEP):
    """Center point plus a true surface normal from finite differences
    of the reconstructed surface. Returns (point, normal) or
    (None, None). Falls back to the camera-facing direction when a
    tangent cannot be computed (silhouette or background neighbour)."""
    cP = _unproject(rx, ry, depth_arr, W, H, r3d)
    if cP is None:
        return None, None

    pR = _unproject(rx + step, ry, depth_arr, W, H, r3d)
    pL = _unproject(rx - step, ry, depth_arr, W, H, r3d)
    pD = _unproject(rx, ry + step, depth_arr, W, H, r3d)
    pU = _unproject(rx, ry - step, depth_arr, W, H, r3d)

    cam = r3d.view_matrix.inverted().translation

    if pR is not None and pL is not None and pD is not None and pU is not None:
        tx = (pR - pL)
        ty = (pD - pU)
        if tx.length > 1e-9 and ty.length > 1e-9:
            N = tx.normalized().cross(ty.normalized())
            if N.length > 1e-9:
                N.normalize()
                if N.dot(cam - cP) < 0:
                    N = -N
                return cP, N

    N = cam - cP
    if N.length < 1e-9:
        N = Vector((0.0, 0.0, 1.0))
    N.normalize()
    return cP, N


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def pick_point(px_img, py_img):
    """Screenshot pixel -> 3D world point from the depth buffer.

    Returns {point, normal, ndc_depth, pixel_image, pixel_region} or
    {"error": ...}."""
    try:
        found = _get_view3d()
        if found is None:
            return {"error": "no VIEW_3D area"}
        _win, area, region, space, r3d = found
        W, H = region.width, region.height
        off_x, off_y_top = _area_to_region_offset(area, region)

        rx = px_img - off_x
        ry = py_img - off_y_top

        depth_arr = _render_depth(space, region, r3d, W, H)

        cP, N = _normal_at(rx, ry, depth_arr, W, H, r3d)
        if cP is None:
            return {"error": "no geometry under that pixel (background)"}

        xi = int(round(rx))
        yi = int(round(ry))

        return {
            "point": [cP.x, cP.y, cP.z],
            "normal": [N.x, N.y, N.z],
            "ndc_depth": float(depth_arr[yi, xi]),
            "pixel_image": [px_img, py_img],
            "pixel_region": [rx, ry],
        }
    except Exception:
        import traceback
        return {"error": traceback.format_exc()}


def pick_points_batch(points, area_ui_type="VIEW_3D", max_distance=0.0,
                      max_points=200):
    """Batch version of pick_point: one offscreen pass, N pixel queries.

    Every point is resolved from the viewport's GPU depth buffer by
    unprojecting its NDC depth through the camera. No raycasting.

    Args:
        points: list of [px, py] screenshot-pixel pairs, or a list of
            dicts with keys "px" and "py".
        area_ui_type: same convention as get_screenshot.
        max_distance: if > 0, hits whose world-space distance from the
            camera exceeds this are reported as miss ("max_distance").
        max_points: hard cap; error if exceeded.

    Returns:
        {"count": N, "hit_count": h, "results": [...]}
        results[i] always corresponds to points[i], even on miss.
    """
    try:
        # Normalise input into a list of (px, py) float pairs.
        if not isinstance(points, list):
            return {"error": "points must be a list of [px, py] pairs"}
        if len(points) == 0:
            return {"count": 0, "hit_count": 0, "results": []}
        if len(points) > max_points:
            return {"error": "points exceeds max_points=%d (got %d)" %
                              (max_points, len(points))}

        parsed = []
        for i, p in enumerate(points):
            if isinstance(p, dict):
                px, py = p.get("px"), p.get("py")
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                px, py = p[0], p[1]
            else:
                return {"error": "point[%d] is not [px, py]" % i}
            try:
                parsed.append((float(px), float(py)))
            except (TypeError, ValueError):
                return {"error": "point[%d] coordinates are not numbers" % i}

        found = _get_view3d(area_ui_type)
        if found is None:
            return {"error": "no %s area" % area_ui_type}
        _win, area, region, space, r3d = found
        W, H = region.width, region.height
        off_x, off_y_top = _area_to_region_offset(area, region)

        # ONE render for the whole batch.
        depth_arr = _render_depth(space, region, r3d, W, H)

        cam = r3d.view_matrix.inverted().translation

        # All point resolution comes from the depth buffer only:
        # unproject each pixel's NDC depth through the camera. No
        # raycasting, no per-mesh BVH pass, no scene.ray_cast. The
        # object/face that owns a pixel is not reported, because
        # determining it would require a ray cast.
        results = []
        hit_count = 0
        for i, (px_img, py_img) in enumerate(parsed):
            rx = px_img - off_x
            ry = py_img - off_y_top
            xi = int(round(rx))
            yi = int(round(ry))

            entry = {
                "index": i,
                "px": px_img,
                "py": py_img,
                "hit": False,
            }

            if not (0 <= xi < W and 0 <= yi < H):
                entry["reason"] = "invalid_uv"
                results.append(entry)
                continue

            d = float(depth_arr[yi, xi])
            if d >= _BG_DEPTH:
                entry["reason"] = "no_hit"
                results.append(entry)
                continue

            # Unproject the depth point.
            P = _unproject(rx, ry, depth_arr, W, H, r3d)
            if P is None:
                entry["reason"] = "no_hit"
                results.append(entry)
                continue

            # Normal from finite differences.
            _cP, N = _normal_at(rx, ry, depth_arr, W, H, r3d)
            if N is None:
                N = Vector((0.0, 0.0, 1.0))

            d_world = (P - cam).length
            if max_distance > 0.0 and d_world > max_distance:
                entry["reason"] = "max_distance"
                results.append(entry)
                continue

            entry["hit"] = True
            entry["point"] = [P.x, P.y, P.z]
            entry["normal"] = [N.x, N.y, N.z]
            entry["ndc_depth"] = d
            entry["distance"] = float(d_world)
            results.append(entry)
            hit_count += 1

        return {
            "count": len(parsed),
            "hit_count": hit_count,
            "results": results,
        }
    except Exception:
        import traceback
        return {"error": traceback.format_exc()}
