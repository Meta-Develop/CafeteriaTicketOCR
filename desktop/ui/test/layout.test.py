"""Run through gui-sandbox with an existing PyGObject/WebKitGTK environment.

Uses ephemeral WebKit state and software rendering. No camera, backend, network,
daily browser profile, or Tauri binary is used. No packages are installed.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

assert os.environ.get("GDK_BACKEND") == "x11" and not os.environ.get("WAYLAND_DISPLAY")
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2

fixture = json.loads(subprocess.check_output(
    ["node", str(Path(__file__).with_suffix(".cjs"))], text=True, timeout=5
))
# Xvfb defaults to 100 dpi; use CSS's 96 dpi for exact viewport assertions.
Gtk.Settings.get_default().set_property("gtk-xft-dpi", 96 * 1024)
view = WebKit2.WebView.new_with_context(WebKit2.WebContext.new_ephemeral())
view.get_settings().set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
window = Gtk.Window(title="Frontend layout check")
window.set_default_size(1280, 860)
window.add(view)
status = 1
sizes = [(1280, 860), (1024, 768), (760, 600), (729, 576), (375, 740)]
checks = []
expected_size = None


def measure():
    view.evaluate_javascript(fixture["expression"], -1, None, None, None, finish, None)
    return False


def next_size():
    global expected_size
    if sizes:
        expected_size = sizes.pop(0)
        window.resize(*expected_size)
        GLib.timeout_add(150, measure)
    else:
        global status
        assert len(checks) == 25
        print("PASS isolated WebKitGTK layout (production HTML/CSS; camera and Tauri not used):", json.dumps(checks))
        status = 0
        Gtk.main_quit()


def finish(widget, task, _data):
    try:
        result = json.loads(widget.evaluate_javascript_finish(task).to_string())
        assert len(result) == 5
        assert all(abs(row["width"] - expected_size[0]) <= 1 and abs(row["height"] - expected_size[1]) <= 1 for row in result), (expected_size, result[0])
        checks.extend(result)
        next_size()
    except Exception as error:
        print("FAIL isolated WebKitGTK layout:", error, file=sys.stderr)
        Gtk.main_quit()


def loaded(widget, event):
    if event == WebKit2.LoadEvent.FINISHED:
        next_size()


def timeout():
    print("FAIL: isolated WebKitGTK layout timed out", file=sys.stderr)
    Gtk.main_quit()
    return False


view.connect("load-changed", loaded)
window.show_all()
view.load_html(fixture["html"], "about:blank")
GLib.timeout_add_seconds(20, timeout)
try:
    Gtk.main()
finally:
    window.destroy()
sys.exit(status)
