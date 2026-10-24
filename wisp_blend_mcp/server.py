# server.py
import bpy
import json
import socket
import traceback

HOST = "127.0.0.1"       # default; overwritten by start()
PORT = 9876              # default; overwritten by start()
MAX_REQUEST_BYTES = 10 * 1024 * 1024

_state = {"sock": None, "clients": [], "port": PORT, "host": HOST}


def _close_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


def _execute(code, strict_json):
    namespace = {"result": {}}
    try:
        exec(code, namespace)
    except Exception:
        return {"status": "error", "message": traceback.format_exc()}

    result = namespace.get("result", {})
    if not isinstance(result, dict):
        return {
            "status": "error",
            "message": "result must be a dict, got {:s}".format(type(result).__name__),
        }

    if strict_json:
        try:
            json.dumps(result)
        except (TypeError, ValueError) as ex:
            return {
                "status": "error",
                "message": "result is not JSON-serializable: {:s}".format(str(ex)),
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
        if request.get("type") != "execute":
            response = {"status": "error", "message": "Unknown request type"}
        else:
            code = request.get("code", "")
            strict_json = bool(request.get("strict_json", False))
            response = _execute(code, strict_json)

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