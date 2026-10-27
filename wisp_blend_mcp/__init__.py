bl_info = {
    "name": "wisp-blend MCP",
    "author": "wisp-code@proton.me",
    "version": (0, 3, 1),
    "blender": (3, 6, 0),
    "location": "Edit > Preferences > Add-ons > wisp-blend MCP",
    "description": "TCP MCP server for wisp-blend",
    "category": "Development",
}

import bpy
import importlib
import time
import webbrowser
from . import server, picker

HTTP_UI_URL = "http://127.0.0.1:35680"


def reload_modules():
    global server, picker
    server = importlib.reload(server)
    picker = importlib.reload(picker)
    return {"server": True, "picker": True}


def _get_prefs(context):
    addons = context.preferences.addons
    pkg = addons.get(__package__)
    if not pkg:
        return None
    if not hasattr(pkg, "preferences"):
        return None
    return pkg.preferences


def _safe_stats():
    """Call server.stats() without ever letting a broken server module
    blank out the panel. Returns a dict with an _error key on failure."""
    try:
        return server.stats()
    except Exception as ex:
        return {
            "running": False,
            "host": "—",
            "port": 0,
            "public": False,
            "clients": 0,
            "requests": 0,
            "last_request": 0.0,
            "_error": "{:s}: {:s}".format(type(ex).__name__, str(ex)),
        }


def _restart_server(port, public):
    if server.is_running():
        server.stop()
        server.start(port, public)


def _update_port(self, context):
    prefs = _get_prefs(context)
    if not prefs:
        return
    if server.is_running():
        _restart_server(self.port, prefs.public)


def _update_public(self, context):
    prefs = _get_prefs(context)
    if not prefs:
        return
    if server.is_running():
        _restart_server(prefs.port, self.public)


def _update_running(self, context):
    prefs = _get_prefs(context)
    if not prefs:
        return
    try:
        if self.running and not server.is_running():
            if not server.start(prefs.port, prefs.public):
                self.running = False
        elif not self.running and server.is_running():
            server.stop()
    except Exception as ex:
        print("[wisp-blend] toggle failed:", ex)
        self.running = False


class WispBlendMCPPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    port: bpy.props.IntProperty(
        name="Port",
        description="TCP port the MCP server listens on",
        default=9876,
        min=1024,
        max=65535,
        update=_update_port,
    )

    public: bpy.props.BoolProperty(
        name="Listen on all interfaces",
        description="Listen on 0.0.0.0 instead of 127.0.0.1 so another "
                    "machine on the network can connect",
        default=False,
        update=_update_public,
    )

    autostart: bpy.props.BoolProperty(
        name="Start on Blender launch",
        description="Start the MCP server automatically when Blender opens",
        default=True,
    )

    running: bpy.props.BoolProperty(
        name="Start",
        description="Start or stop the MCP server",
        default=False,
        update=_update_running,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        st = _safe_stats()

        row = col.row(align=True)
        row.prop(self, "running", toggle=True,
                 icon='PAUSE' if st.get("running") else 'PLAY')
        row.label(text="Running" if st.get("running") else "Stopped")

        if st.get("_error"):
            col.separator()
            box = col.box()
            box.label(text="Server module error:", icon='ERROR')
            box.label(text=st["_error"])

        col.separator()
        col.prop(self, "port")
        col.prop(self, "public")
        col.separator()
        col.prop(self, "autostart")

        if st.get("running"):
            col.separator()
            box = col.box()
            box.label(text="Listening on {:s}:{:d}".format(
                          st.get("host", "—"), st.get("port", 0)),
                      icon='INFO')
            box.label(
                text="Client: {:s}".format(
                    "connected" if st.get("clients") else "waiting"),
                icon='CHECKMARK' if st.get("clients") else 'TIME')
            if st.get("last_request"):
                age = max(0.0, time.time() - st["last_request"])
                box.label(text="Last request: {:.0f}s ago".format(age),
                          icon='SORTTIME')
            if st.get("public"):
                box.label(text="Reachable from other machines on this network",
                          icon='WORLD')


class WISP_OT_open_web_ui(bpy.types.Operator):
    bl_idname = "wisp.open_web_ui"
    bl_label = "Open web UI"
    bl_description = "Open the wisp-blend web console in your browser"

    def execute(self, context):
        webbrowser.open(HTTP_UI_URL)
        return {"FINISHED"}


class WISP_PT_panel(bpy.types.Panel):
    bl_label = "Wisp-blend"
    bl_idname = "WISP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Wisp"

    def draw(self, context):
        layout = self.layout
        st = _safe_stats()

        if st.get("_error"):
            box = layout.box()
            box.label(text="Server module error:", icon='ERROR')
            box.label(text=st["_error"])
            return

        col = layout.column(align=True)
        row = col.row(align=True)
        row.label(text="", icon="CHECKMARK" if st.get("running") else "ERROR")
        row.label(text="Server  {:s}:{:d}".format(
            st.get("host", "—"), st.get("port", 0)))

        row = col.row(align=True)
        row.label(text="", icon="CHECKMARK" if st.get("clients") else "TIME")
        row.label(text="Client  {:s}".format(
            "connected" if st.get("clients") else "waiting"))

        col.separator()
        row = col.row(align=True)
        row.enabled = st.get("running", False)
        row.operator("wisp.open_web_ui", icon="WORLD")


classes = (
    WispBlendMCPPreferences,
    WISP_OT_open_web_ui,
    WISP_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    prefs = _get_prefs(bpy.context)
    if prefs:
        if prefs.autostart:
            try:
                server.start(prefs.port, prefs.public)
            except Exception as ex:
                print("[wisp-blend] autostart failed:", ex)
                
        try:
            prefs.running = server.is_running()
        except Exception:
            prefs.running = False


def unregister():
    try:
        server.stop()
    except Exception:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)