<p align="center">
  <img src="wisp-blend.png" alt="wisp-relay" width="50%">
</p>

<h1 align="center">wisp-blend MCP</h1>

<p align="center">
  <strong>A TCP MCP server inside Blender — execute Python, pick 3D points from pixels.</strong>
</p>

---

## What it is

`wisp-blend MCP` is a Blender add-on that exposes a small TCP server speaking a
minimal JSON protocol. An external agent (or you, with `nc`) connects to the
port, sends a block of Python, and gets back a JSON dict. That is the whole
contract.

It ships with one extra convenience on top of plain `exec()`: a **pixel-to-3D
picker** that resolves a screenshot coordinate to an exact world-space point
and surface normal using the viewport's own GPU depth buffer. No raycasting,
no scene render, no draw handlers — it reads the depth that Blender already
computed and unprojects it through the camera.

---

## Install

1. Download or clone this repository.
2. In Blender: **Edit → Preferences → Add-ons → Install…**, pick the
   `.zip` (or point Blender at the folder).
3. Enable **wisp-blend MCP**.
4. Open its preferences panel to configure the port and start the server.

Requires **Blender 3.6.0 or newer** (tested on 3.6.23 and 4.x).

---

## Preferences

| Setting | Default | Notes |
|---|---|---|
| **Port** | `9876` | 1024–65535. Changing it while running restarts the listener. |
| **Listen on all interfaces** | off | Binds `0.0.0.0` instead of `127.0.0.1`. Changing it while running restarts the listener. |
| **Start on Blender launch** | off | Server starts automatically when the add-on is enabled. |
| **Start** | — | Toggle button. Reverts to off if the bind fails. |

The panel shows the active address and port when the server is running, plus a
`WORLD` badge if it is reachable from other machines.

**Security:** the server executes arbitrary Python inside Blender. That is the
point, but it means you should leave it on `127.0.0.1` unless you deliberately
trust every host that can route to this machine.

---

## Protocol

NUL-terminated JSON. One request per connection, one response, then the
server closes the socket.

**Request**

```json
{"type": "execute", "code": "<python source>", "strict_json": false}
```

- `type` — must be `"execute"`. Anything else returns an error.
- `code` — the Python source to run.
- `strict_json` — when `true`, the result dict must already be
  JSON-serializable; otherwise the server falls back to `repr()` on
  non-serializable values instead of failing.

**Response**

```json
{"status": "ok", "result": { ... }}
```
```json
{"status": "error", "message": "traceback..."}
```

Inside the executed code, assign whatever you want returned to a variable
called `result`. It must be a `dict`.

```python
import bpy
result = {"objects": [o.name for o in bpy.data.objects]}
```

---

## The picker

`picker.py` is imported by the server module and is available to any code you
send. It answers the question *"which world-space point is under this pixel in
the screenshot?"* using the viewport depth buffer.

### `pick_point(px_img, py_img)`

Single point. Screenshot pixel coordinates (top-left origin, as returned by
Blender's screenshot operator).

Returns on hit:

```python
{
    "point":       [x, y, z],
    "normal":      [nx, ny, nz],
    "ndc_depth":   float,
    "pixel_image": [px, py],
    "pixel_region":[rx, ry],
}
```

Returns on miss:

```python
{"error": "no geometry under that pixel (background)"}
```

### `pick_points_batch(points, area_ui_type="VIEW_3D", max_distance=0.0, max_points=200)`

Batch version. One offscreen depth render for the whole set, N pixel queries.

- `points` — list of `[px, py]` pairs or `{"px": .., "py": ..}` dicts.
- `max_distance` — if `> 0`, hits farther than this from the camera are
  reported as miss with reason `"max_distance"`.
- `max_points` — hard cap (default 200).

Returns:

```python
{
    "count": N,
    "hit_count": h,
    "results": [
        {
            "index":  i,
            "px":     px, "py": py,
            "hit":    True,
            "point":  [x, y, z],
            "normal": [nx, ny, nz],
            "ndc_depth": d,
            "distance":  d_world,
        },
        # or, on miss:
        {
            "index":  i,
            "px":     px, "py": py,
            "hit":    False,
            "reason": "invalid_uv" | "no_hit" | "max_distance",
        },
    ],
}
```

`results[i]` always corresponds to `points[i]`, even on miss.

### How it works

1. Find a `VIEW_3D` area. The one under the mouse wins; otherwise the
   largest WINDOW region (deterministic in headless / scripted contexts).
2. Draw the viewport into a `GPUOffScreen` with its current shading and read
   the GL depth buffer.
3. Flip the buffer from GL row-0-bottom to image row-0-top.
4. For each pixel, unproject NDC depth through `window_matrix @ view_matrix`.
5. Compute the surface normal from finite differences of the reconstructed
   surface; fall back to the camera-facing direction at silhouettes.

The pixel the user clicked is a screenshot pixel, which is in **area** space
(including headers). The depth buffer is in **WINDOW region** space.
`_area_to_region_offset()` handles the translation.

---

## Example: pick two corners of a wall from a screenshot

```python
import bpy
from wisp_blend_mcp import picker

# 1. Take a viewport screenshot
bpy.ops.screen.screenshot_area(filepath="/tmp/shot.png")

# 2. Ask the picker for two known image pixels
r = picker.pick_points_batch([[412, 330], [780, 330]], max_distance=50.0)

if "error" in r:
    result = {"error": r["error"]}
else:
    result = {
        "hits":   [x for x in r["results"] if x["hit"]],
        "misses": [x for x in r["results"] if not x["hit"]],
    }
```

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Add-on registration, preferences, port / public / start toggles. |
| `server.py` | TCP listener, per-connection JSON framing, `exec()` sandbox, timer tick. |
| `picker.py` | GPU depth-buffer pixel → world-point unprojection. |

`/reload` on the C side reimports `server` and `picker` so you can edit them
without restarting Blender.

---

## Notes and limitations

- The picker reports the **world-space point and surface normal**, not the
  owning object or face. Determining those would require a raycast, which the
  picker deliberately avoids.
- Background pixels (NDC depth ≥ `0.9999`) are reported as miss.
- With multiple `VIEW_3D` areas open on different cameras, the "largest
  region" rule picks the one whose depth buffer matches what the screenshot
  operator captured. If you need a specific viewport, keep the target area
  under the mouse when calling.
- `MAX_REQUEST_BYTES` is **10 MB**. Larger requests close the connection
  without a response.
- The server is single-threaded and driven by `bpy.app.timers`. Long-running
  Python blocks the Blender UI.

---

## License

MIT License. See `LICENSE` for details.
