bl_info = {
    "name": "wisp-blend MCP",
    "author": "wisp-code@proton.me",
    "version": (0, 3, 0),
    "blender": (3, 6, 0),
    "location": "Edit > Preferences > Add-ons > wisp-blend MCP",
    "description": "TCP MCP server for wisp-blend",
    "category": "Development",
}

import bpy
import importlib
from . import server, picker

def reload_modules():
    """Reimport server and picker so a live edit is picked up
    without restarting Blender. Called from /reload on the C side."""
    global server, picker
    server = importlib.reload(server)
    picker = importlib.reload(picker)
    return {"server": True, "picker": True}

def _get_prefs(context):
    """Return the preferences object for this addon, or None if the
    addon entry isn't fully wired up yet. Safe to call from register(),
    from property update callbacks, and from draw()."""
    addons = context.preferences.addons
    pkg = addons.get(__package__)
    if not pkg:
        return None
    if not hasattr(pkg, "preferences"):
        return None
    return pkg.preferences


def _restart_server(port, public):
    """Called when the port or exposure changes while the server is running."""
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
    if self.running and not server.is_running():
        if not server.start(prefs.port, prefs.public):
            # Bind failed; revert the checkbox so the UI matches reality.
            self.running = False
    elif not self.running and server.is_running():
        server.stop()


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
        default=False,
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

        row = col.row(align=True)
        row.prop(self, "running", toggle=True,
                 icon='PAUSE' if server.is_running() else 'PLAY')
        row.label(text="Running" if server.is_running() else "Stopped")

        col.separator()
        col.prop(self, "port")
        col.prop(self, "public")

        col.separator()
        col.prop(self, "autostart")

        if server.is_running():
            col.separator()
            box = col.box()
            box.label(text="Listening on {:s}:{:d}".format(
                          server.current_host(), server.current_port()),
                      icon='INFO')
            if server.is_public():
                box.label(text="Reachable from other machines on this network",
                          icon='WORLD')


classes = (WispBlendMCPPreferences,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Preferences may not be wired up yet on first enable. Use the
    # safe accessor and bail quietly if they aren't ready.
    prefs = _get_prefs(bpy.context)
    if prefs:
        if prefs.autostart:
            server.start(prefs.port, prefs.public)
        prefs.running = server.is_running()


def unregister():
    server.stop()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
