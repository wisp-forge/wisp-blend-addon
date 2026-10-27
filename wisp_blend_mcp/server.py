# server.py
import bpy
import builtins
import json
import socket
import time
import traceback

HOST = "127.0.0.1"       # default; overwritten by start()
PORT = 9876              # default; overwritten by start()
MAX_REQUEST_BYTES = 10 * 1024 * 1024

_state = {
    "sock": None,
    "clients": [],
    "port": PORT,
    "host": HOST,
    "requests": 0,
    "last_request": 0.0,
}


def _close_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sandbox: restricted builtins + restricted imports for model-supplied code.
# ---------------------------------------------------------------------------

_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bin", "bool", "callable", "chr", "dict", "dir",
    "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "id", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "setattr", "slice", "sorted", "str", "sum", "tuple", "type",
    "zip",
    "Exception", "ArithmeticError", "AssertionError", "AttributeError",
    "IndexError", "KeyError", "LookupError", "NotImplementedError",
    "RuntimeError", "StopIteration", "TypeError", "ValueError",
    "ZeroDivisionError",
)

_ALLOWED_IMPORTS = frozenset({
    "bpy", "bmesh", "mathutils", "math", "gpu", "json", "base64",
    "tempfile", "os", "time", "numpy", "random", "re", "itertools",
    "collections", "functools", "sys",
})

_real_import = builtins.__import__


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORTS:
        raise ImportError("import of {:s} is not permitted".format(name))
    return _real_import(name, globals, locals, fromlist, level)


def _safe_builtins():
    d = {name: getattr(builtins, name)
         for name in _SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    d["__import__"] = _safe_import
    return d


_SAVE_CALLS = (
    "save_mainfile",
    "save_as_mainfile",
    "save_homefile",
    "save_userpref",
    "save_version",
)

_PROTECTED = (
    "objects", "meshes", "materials", "collections", "images",
    "node_groups", "cameras", "lights", "textures", "curves",
)


def _snapshot():
    snap = {}
    for name in _PROTECTED:
        coll = getattr(bpy.data, name, None)
        if coll is None:
            continue
        try:
            snap[name] = set(coll.keys())
        except Exception:
            pass
    return snap


def _check_removed(before):
    after = _snapshot()
    removed = {}
    for name, names in before.items():
        gone = names - after.get(name, set())
        if gone:
            removed[name] = sorted(gone)
    return removed


def _execute(code, strict_json, trusted=False):
    if not isinstance(code, str):
        return {"status": "error", "message": "code must be a string"}

    if any(name in code for name in _SAVE_CALLS):
        return {
            "status": "error",
            "message": "saving .blend files is not permitted. "
                       "Ask the user to save manually.",
        }

    before = _snapshot()
    # Setup the sandbox
    safe_builtins = _safe_builtins()
    if trusted:
        # Allow the screenshot tool to use open() for its temp files
        safe_builtins["open"] = open

    namespace = {"result": {}, "__builtins__": safe_builtins}
    try:
        exec(code, namespace)
    except Exception:
        return {"status": "error", "message": traceback.format_exc()}

    removed = _check_removed(before)
    if removed:
        return {
            "status": "error",
            "message": "code removed pre-existing data-blocks: " +
                       json.dumps(removed, default=str) +
                       ". The turn was rejected. Undo to restore, then "
                       "retry without deleting pre-existing data.",
        }

    result = namespace.get("result", {})
    if not isinstance(result, dict):
        return {
            "status": "error",
            "message": "result must be a dict, got {:s}".format(
                type(result).__name__),
        }

    if strict_json:
        try:
            json.dumps(result)
        except (TypeError, ValueError) as ex:
            return {
                "status": "error",
                "message": "result is not JSON-serializable: {:s}".format(
                    str(ex)),
            }
    else:
        result = json.loads(json.dumps(result, default=repr))

    return {"status": "ok", "result": result}


def _encode(response):
    return (json.dumps(response) + "\0").encode("utf-8")


def _handle(client):
    buf = client["buf"]
    if b"\0" not in buf:
        return False

    data = bytes(buf[:buf.index(b"\0")])
    try:
        request = json.loads(data)
    except Exception:
        response = {"status": "error", "message": "invalid JSON"}
    else:
        if not isinstance(request, dict):
            response = {"status": "error",
                        "message": "request must be an object"}
        elif request.get("type") != "execute":
            response = {"status": "error", "message": "Unknown request type"}
        else:
            _state["requests"] += 1
            _state["last_request"] = time.time()
            code = request.get("code", "")
            strict_json = bool(request.get("strict_json", False))
            trusted = bool(request.get("trusted", False))
            response = _execute(code, strict_json, trusted)

    try:
        client["conn"].sendall(_encode(response))
    except OSError:
        pass
    return True


def tick():
    sock = _state["sock"]
    if sock is None:
        return None

    while True:
        try:
            conn, _ = sock.accept()
            conn.setblocking(False)
            _state["clients"].append({"conn": conn, "buf": bytearray()})
        except BlockingIOError:
            break
        except OSError:
            break

    for client in list(_state["clients"]):
        conn = client["conn"]
        try:
            chunk = conn.recv(4096)
        except BlockingIOError:
            continue
        except OSError:
            _close_conn(conn)
            _state["clients"].remove(client)
            continue

        if not chunk:
            _close_conn(conn)
            _state["clients"].remove(client)
            continue

        client["buf"].extend(chunk)

        if len(client["buf"]) > MAX_REQUEST_BYTES:
            _close_conn(conn)
            _state["clients"].remove(client)
            continue

        if _handle(client):
            _close_conn(conn)
            _state["clients"].remove(client)

    return 0.05


def stop():
    if _state["sock"] is not None:
        _close_conn(_state["sock"])
        _state["sock"] = None
    for client in _state["clients"]:
        _close_conn(client["conn"])
    _state["clients"].clear()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    print("[wisp-blend] MCP server stopped")


def start(port=None, public=False):
    if _state["sock"] is not None:
        print("[wisp-blend] already running on {:s}:{:d}".format(
            _state["host"], _state["port"]))
        return False

    if port is None:
        port = PORT
    host = "0.0.0.0" if public else "127.0.0.1"
    _state["port"] = port
    _state["host"] = host

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as ex:
        print("[wisp-blend] bind failed on {:s}:{:d}: {:s}".format(
            host, port, str(ex)))
        sock.close()
        return False
    sock.listen(5)
    sock.setblocking(False)
    _state["sock"] = sock

    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    bpy.app.timers.register(tick, first_interval=0.05, persistent=True)
    print("[wisp-blend] listening on {:s}:{:d}".format(host, port))
    return True


def is_running():
    return _state["sock"] is not None


def current_port():
    return _state["port"]


def current_host():
    return _state["host"]


def is_public():
    return _state["host"] == "0.0.0.0"


def stats():
    return {
        "running": _state["sock"] is not None,
        "host": _state["host"],
        "port": _state["port"],
        "public": _state["host"] == "0.0.0.0",
        "clients": len(_state["clients"]),
        "requests": _state["requests"],
        "last_request": _state["last_request"],
    }