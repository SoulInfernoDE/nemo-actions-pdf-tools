"""GTK helpers shared by all dialogs."""

import json
import os
import queue
import subprocess
import threading
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from . import APP_NAME, DOMAIN, updater  # noqa: E402
from .compress import DocumentError  # noqa: E402
from .i18n import _  # noqa: E402

try:
    gi.require_version("Poppler", "0.18")
    import cairo
    from gi.repository import Poppler
except (ImportError, ValueError):
    Poppler = None

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / DOMAIN / "settings.json"


def load_config(section):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        value = data.get(section, {})
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(section, values):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[section] = values
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass


def format_size(n):
    return GLib.format_size(max(0, int(n)))


def error_text(exc):
    """A user-facing explanation for an exception from the engines."""
    if isinstance(exc, DocumentError):
        if exc.reason == "password":
            return _("The PDF is password protected. Remove the password first.")
        if exc.reason == "damaged":
            return _("The file is damaged or not a PDF.")
        if exc.reason == "signed":
            return _("The PDF is digitally signed; changing it would break the signature.")
        if exc.reason == "verify":
            return _("The result did not pass the visual check, so nothing was written.")
        if exc.reason == "io":
            return exc.detail
    if isinstance(exc, ValueError):
        return _("Invalid page range: {value}").format(value=str(exc))
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc) or exc.__class__.__name__


def show_message(parent, kind, primary, secondary=""):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.OK, text=primary)
    dialog.set_title(APP_NAME)
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def notify(summary, body=""):
    try:
        subprocess.Popen(["notify-send", "--app-name", APP_NAME, "--icon", "application-pdf", summary, body])
    except OSError:
        pass


def show_in_file_manager(paths):
    """Open the folder with the files selected (Nemo implements FileManager1)."""
    uris = [Gio.File.new_for_path(str(p)).get_uri() for p in paths]
    if not uris:
        return
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                      "org.freedesktop.FileManager1", "ShowItems",
                      GLib.Variant("(ass)", (uris, "")), None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        folder = Gio.File.new_for_path(str(Path(paths[0]).parent)).get_uri()
        Gio.AppInfo.launch_default_for_uri(folder, None)


def open_file(path):
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
    except GLib.Error:
        pass


def pil_to_pixbuf(im):
    im = im.convert("RGB")
    data = GLib.Bytes.new(im.tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(data, GdkPixbuf.Colorspace.RGB, False, 8,
                                           im.width, im.height, im.width * 3)


def render_thumbnail(path, size=96, page_index=0):
    """First page as a pixbuf, or None. Safe to call from a worker thread."""
    if Poppler is None:
        return None
    try:
        doc = Poppler.Document.new_from_file(Gio.File.new_for_path(str(path)).get_uri(), None)
        page = doc.get_page(page_index)
        w, h = page.get_size()
        scale = size / max(w, h)
        width, height = max(1, int(w * scale)), max(1, int(h * scale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        ctx = cairo.Context(surface)
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.scale(scale, scale)
        page.render(ctx)
        surface.flush()
        return surface_to_pixbuf(surface, width, height)
    except Exception:
        return None


def surface_to_pixbuf(surface, width, height):
    from PIL import Image

    im = Image.frombuffer("RGB", (width, height), bytes(surface.get_data()), "raw", "BGRX",
                          surface.get_stride(), 1)
    return pil_to_pixbuf(im)


def image_thumbnail(path, size=96):
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
    except GLib.Error:
        return None


class Worker:
    """Runs jobs one after another on a background thread.

    Results are handed back to the GTK main loop with GLib.idle_add.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, job, on_done=None, on_error=None):
        self._queue.put((job, on_done, on_error))

    def _loop(self):
        while True:
            job, on_done, on_error = self._queue.get()
            if job is None:
                return
            try:
                result = job()
            except Exception as exc:  # noqa: BLE001 - every failure goes to the UI
                if on_error is None:
                    traceback.print_exc()
                else:
                    GLib.idle_add(on_error, exc)
            else:
                if on_done is not None:
                    GLib.idle_add(on_done, result)

    def stop(self):
        self._queue.put((None, None, None))


def output_folder_chooser(title):
    chooser = Gtk.FileChooserButton(title=title, action=Gtk.FileChooserAction.SELECT_FOLDER)
    return chooser


def section_label(text):
    label = Gtk.Label(xalign=0)
    label.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
    return label


def dim_label(text="", wrap=True, max_chars=64):
    label = Gtk.Label(label=text, xalign=0)
    label.get_style_context().add_class("dim-label")
    label.set_line_wrap(wrap)
    if wrap:
        label.set_max_width_chars(max_chars)
    return label


def button_row(*buttons):
    box = Gtk.Box(spacing=6, halign=Gtk.Align.END)
    for b in buttons:
        box.pack_start(b, False, False, 0)
    return box


def run_app(window):
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


class UpdateBar(Gtk.InfoBar):
    """Offers an update when the occasional background check finds one."""

    def __init__(self):
        super().__init__(show_close_button=False)
        self.set_no_show_all(True)
        self.tag = None
        self.label = Gtk.Label(xalign=0, wrap=True)
        self.get_content_area().add(self.label)
        self.update_button = self.add_button(_("Update now"), 1)
        self.later_button = self.add_button(_("Later"), 2)
        self.connect("response", self._on_response)
        config = load_config(updater.CONFIG_SECTION)
        if updater.due(config):
            threading.Thread(target=self._check, args=(config,), daemon=True).start()

    def _check(self, config):
        try:
            tag = updater.check(config=config)
        except Exception:  # noqa: BLE001 - offline or GitHub unreachable: try again another day
            return
        if tag:
            GLib.idle_add(self._found, tag)

    def _found(self, tag):
        self.tag = tag
        self.set_message_type(Gtk.MessageType.INFO)
        self.label.set_text(_("A new version of the PDF tools is available: {version}").format(
            version=tag.lstrip("v")))
        self.show_all()
        return False

    def _on_response(self, _bar, response):
        if response == 2:
            updater.snooze()
            self.hide()
            return
        if response == 1 and self.tag:
            self.update_button.set_sensitive(False)
            self.later_button.set_sensitive(False)
            self.label.set_text(_("Updating…"))
            threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        try:
            updater.install(self.tag)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._installed, str(exc))
        else:
            GLib.idle_add(self._installed, None)

    def _installed(self, error):
        self.update_button.hide()
        self.later_button.hide()
        if error:
            self.set_message_type(Gtk.MessageType.ERROR)
            self.label.set_text(_("The update failed: {error}").format(error=error))
        else:
            self.label.set_text(_("Updated to {version}. Tools use it from the next start; the menu after "
                                  "restarting Nemo.").format(version=self.tag.lstrip("v")))
        self.add_button(_("_Close"), 2).show()
        return False
