#!/usr/bin/python3
"""Open a dialog, wait, save a screenshot of it and close it.

Used for the README pictures and for checking the layout.
Usage: screenshot.py COMMAND OUT.png DELAY_SECONDS FILES...
Set LANGUAGE=de for the German screenshots.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from nemo_pdf_tools import dialogs  # noqa: E402
from nemo_pdf_tools.compress_window import CompressWindow  # noqa: E402
from nemo_pdf_tools.merge_window import MergeWindow  # noqa: E402

WINDOWS = {
    "compress": CompressWindow,
    "merge": MergeWindow,
    "split": dialogs.SplitWindow,
    "export": dialogs.ExportWindow,
    "protect": dialogs.ProtectWindow,
}


def main():
    command, out, delay, *files = sys.argv[1:]
    click = None
    if command.endswith("+go"):
        # "compress+go": press the main button after half the delay.
        command, click = command[:-3], True
    window = WINDOWS[command](files)
    window.show_all()
    if click:
        GLib.timeout_add(int(float(delay) * 500), lambda: window.go_button.clicked() and False)

    def shoot():
        gdk_window = window.get_window()
        # Include the window frame when the window manager provides one.
        toplevel = gdk_window.get_toplevel()
        x, y, w, h = toplevel.get_frame_extents().x, toplevel.get_frame_extents().y, \
            toplevel.get_frame_extents().width, toplevel.get_frame_extents().height
        root = Gdk.get_default_root_window()
        pixbuf = Gdk.pixbuf_get_from_window(root, x, y, w, h)
        pixbuf.savev(out, "png", [], [])
        print("saved", out, w, h)
        Gtk.main_quit()
        return False

    GLib.timeout_add(int(float(delay) * 1000), shoot)
    Gtk.main()


if __name__ == "__main__":
    main()
