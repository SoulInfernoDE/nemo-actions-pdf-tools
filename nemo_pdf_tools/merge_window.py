"""Dialog for merging PDF files (and images) in a chosen order."""

import shutil
import subprocess
import sys
import threading
from pathlib import Path

from gi.repository import GdkPixbuf, GLib, Gtk

from . import operations
from .compress import unique_path
from .i18n import _, ngettext
from .ui_common import (
    UpdateBar,
    Worker,
    dim_label,
    error_text,
    format_size,
    image_thumbnail,
    load_config,
    open_file,
    render_thumbnail,
    save_config,
    section_label,
    show_in_file_manager,
    show_message,
)

COL_THUMB, COL_NAME, COL_RANGE, COL_PAGES, COL_PATH, COL_MTIME, COL_COUNT = range(7)
THUMB = 64


def launch_tool(command, paths):
    """Start another tool of this suite as its own process."""
    script = Path(sys.argv[0]).resolve()
    subprocess.Popen([sys.executable, str(script), command, "--gui", *map(str, paths)])


class MergeWindow(Gtk.Window):
    def __init__(self, paths):
        self.images_only = bool(paths) and all(operations.is_image(p) for p in paths)
        title = _("Create PDF from images") if self.images_only else _("Merge PDF files")
        super().__init__(title=title)
        self.set_default_size(760, 620)
        self.set_icon_name("application-pdf")
        self.worker = Worker()
        self.cancel_event = threading.Event()
        self.config = load_config("merge")
        self.result_path = None
        self._build()
        self.add_files(paths)
        first = Path(paths[0]) if paths else Path.home()
        default_name = first.parent.name if self.images_only else _("merged")
        self.name_entry.set_text(default_name + ".pdf")
        self.folder_button.set_filename(str(first.parent))
        self.connect("delete-event", self._on_close)

    def _build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)
        self.add(outer)
        outer.pack_start(UpdateBar(), False, False, 0)
        outer.pack_start(dim_label(_("Drag the rows into the right order. Page ranges like “1-3, 5, 8-” "
                                     "take only those pages; empty means all pages.")), False, False, 0)

        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str, float, int)
        self.store.connect("row-deleted", lambda *_a: self._update())
        self.tree = Gtk.TreeView(model=self.store, reorderable=True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        thumb = Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), pixbuf=COL_THUMB)
        thumb.set_min_width(THUMB + 8)
        self.tree.append_column(thumb)
        name = Gtk.TreeViewColumn(_("File"), Gtk.CellRendererText(ellipsize=3), markup=COL_NAME)
        name.set_expand(True)
        self.tree.append_column(name)
        pages = Gtk.CellRendererText(editable=True, placeholder_text=_("all"))
        pages.connect("edited", self._on_range_edited)
        col = Gtk.TreeViewColumn(_("Pages"), pages, text=COL_RANGE)
        col.set_min_width(110)
        self.tree.append_column(col)
        self.tree.append_column(Gtk.TreeViewColumn(_("of"), Gtk.CellRendererText(), text=COL_PAGES))
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.tree)
        frame = Gtk.Frame()
        frame.add(scroller)

        tools = Gtk.Box(spacing=0)
        tools.get_style_context().add_class("linked")

        def tool(icon, tip, handler):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect("clicked", handler)
            tools.pack_start(b, False, False, 0)
            return b

        tool("list-add-symbolic", _("Add files…"), self._on_add)
        tool("list-remove-symbolic", _("Remove selected"), self._on_remove)
        tool("go-up-symbolic", _("Move up"), lambda *_a: self._move(-1))
        tool("go-down-symbolic", _("Move down"), lambda *_a: self._move(1))
        sort_button = Gtk.MenuButton()
        sort_button.set_image(Gtk.Image.new_from_icon_name("view-sort-ascending-symbolic", Gtk.IconSize.BUTTON))
        sort_button.set_tooltip_text(_("Sort"))
        menu = Gtk.Menu()
        for label, key, reverse in ((_("Name A–Z"), "name", False), (_("Name Z–A"), "name", True),
                                    (_("Oldest first"), "date", False), (_("Newest first"), "date", True)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _i, k=key, r=reverse: self._sort(k, r))
            menu.append(item)
        menu.show_all()
        sort_button.set_popup(menu)
        tools.pack_start(sort_button, False, False, 0)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        list_box.pack_start(frame, True, True, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(tools, False, False, 0)
        self.summary = dim_label(wrap=False)
        row.pack_end(self.summary, False, False, 0)
        list_box.pack_start(row, False, False, 0)
        outer.pack_start(list_box, True, True, 0)

        outer.pack_start(section_label(_("Options")), False, False, 0)
        self.checks = {}
        options = (
            ("bookmarks", _("Add a bookmark for every file"), True),
            ("keep_outlines", _("Keep the bookmarks of the files"), True),
            ("duplex_blank", _("Insert a blank page after files with an odd page count (double-sided printing)"), False),
            ("fit_a4", _("Place images on A4 pages"), True),
        )
        grid = Gtk.Grid(row_spacing=4, column_spacing=12, margin_start=12)
        for n, (key, text, default) in enumerate(options):
            check = Gtk.CheckButton(label=text)
            check.set_active(bool(self.config.get(key, default)))
            grid.attach(check, n % 2, n // 2, 1, 1)
            self.checks[key] = check
        outer.pack_start(grid, False, False, 0)

        out = Gtk.Box(spacing=8)
        out.pack_start(section_label(_("Save as")), False, False, 0)
        self.name_entry = Gtk.Entry(width_chars=28)
        out.pack_start(self.name_entry, True, True, 0)
        out.pack_start(Gtk.Label(label=_("in")), False, False, 0)
        self.folder_button = Gtk.FileChooserButton(title=_("Choose target folder"),
                                                   action=Gtk.FileChooserAction.SELECT_FOLDER)
        out.pack_start(self.folder_button, False, False, 0)
        outer.pack_start(out, False, False, 0)

        self.info = Gtk.InfoBar(show_close_button=False)
        self.info.set_no_show_all(True)
        self.info_label = Gtk.Label(xalign=0, wrap=True)
        self.info.get_content_area().add(self.info_label)
        self.info_label.show()
        self.info.add_button(_("Open"), 1)
        self.info.add_button(_("Show in folder"), 2)
        self.info.add_button(_("Compress…"), 3)
        self.info.connect("response", self._on_info)
        outer.pack_start(self.info, False, False, 0)

        bottom = Gtk.Box(spacing=8)
        if shutil.which("pdfarranger"):
            arrange = Gtk.Button(label=_("Edit single pages in PDF Arranger…"))
            arrange.set_tooltip_text(_("Opens the files in this order in PDF Arranger, where single pages "
                                       "can be moved, rotated or deleted."))
            arrange.connect("clicked", self._on_arrange)
            bottom.pack_start(arrange, False, False, 0)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.progress.set_no_show_all(True)
        bottom.pack_start(self.progress, True, True, 0)
        self.go_button = Gtk.Button.new_with_mnemonic(_("Create _PDF") if self.images_only else _("_Merge"))
        self.go_button.get_style_context().add_class("suggested-action")
        self.go_button.connect("clicked", self._on_merge)
        self.close_button = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        self.close_button.connect("clicked", lambda *_a: self._on_close())
        bottom.pack_end(self.go_button, False, False, 0)
        bottom.pack_end(self.close_button, False, False, 0)
        outer.pack_start(bottom, False, False, 0)

    # -- list -------------------------------------------------------------------

    def add_files(self, paths):
        for path in paths:
            path = Path(path)
            if not path.is_file():
                continue
            markup = "<b>%s</b>\n<small>%s</small>" % (GLib.markup_escape_text(path.name),
                                                      GLib.markup_escape_text(format_size(path.stat().st_size)))
            it = self.store.append([None, markup, "", "…", str(path), path.stat().st_mtime, 0])
            ref = Gtk.TreeRowReference.new(self.store, self.store.get_path(it))
            self._load_info(path, ref)
        self._update()

    def _load_info(self, path, ref):
        def job():
            count = operations.page_count(path)
            thumb = image_thumbnail(path, THUMB) if operations.is_image(path) else render_thumbnail(path, THUMB)
            return count, thumb

        def done(result):
            if not ref.valid():
                return
            it = self.store.get_iter(ref.get_path())
            count, thumb = result
            self.store[it][COL_PAGES] = str(count)
            self.store[it][COL_COUNT] = count
            self.store[it][COL_THUMB] = thumb
            self._update()

        def failed(exc):
            if ref.valid():
                it = self.store.get_iter(ref.get_path())
                self.store[it][COL_PAGES] = "!"
                self.store[it][COL_NAME] += "\n<small>⚠ %s</small>" % GLib.markup_escape_text(error_text(exc))

        self.worker.submit(job, done, failed)

    def _update(self):
        files = len(self.store)
        pages = 0
        for row in self.store:
            if row[COL_COUNT]:
                try:
                    pages += len(operations.parse_ranges(row[COL_RANGE], row[COL_COUNT]))
                except ValueError:
                    pass
        self.summary.set_text(ngettext("{n} file", "{n} files", files).format(n=files) + ", " +
                              ngettext("{n} page", "{n} pages", pages).format(n=pages))
        self.go_button.set_sensitive(files > 0)

    def _on_range_edited(self, _renderer, path, text):
        row = self.store[path]
        try:
            if row[COL_COUNT]:
                operations.parse_ranges(text, row[COL_COUNT])
        except ValueError:
            show_message(self, Gtk.MessageType.WARNING, _("Invalid page range: {value}").format(value=text),
                         _("Use page numbers from 1 to {n}, for example “1-3, 5, 8-”.").format(n=row[COL_COUNT]))
            return
        row[COL_RANGE] = text.strip()
        self._update()

    def _selected_paths(self):
        _model, rows = self.tree.get_selection().get_selected_rows()
        return rows

    def _on_add(self, _button):
        dialog = Gtk.FileChooserDialog(title=_("Add files"), transient_for=self, action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL, _("_Add"), Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        folder = self.folder_button.get_filename()
        if folder:
            dialog.set_current_folder(folder)
        flt = Gtk.FileFilter()
        flt.set_name(_("PDF files and images"))
        flt.add_mime_type("application/pdf")
        flt.add_pixbuf_formats()
        dialog.add_filter(flt)
        if dialog.run() == Gtk.ResponseType.OK:
            self.add_files(dialog.get_filenames())
        dialog.destroy()

    def _on_remove(self, _button):
        for path in reversed(self._selected_paths()):
            self.store.remove(self.store.get_iter(path))

    def _move(self, step):
        rows = self._selected_paths()
        if not rows:
            return
        indexes = [p.get_indices()[0] for p in rows]
        order = list(range(len(self.store)))
        for i in (indexes if step < 0 else reversed(indexes)):
            j = i + step
            if 0 <= j < len(order) and j not in indexes:
                order[i], order[j] = order[j], order[i]
                indexes[indexes.index(i)] = j
        self.store.reorder(order)

    def _sort(self, key, reverse):
        col = COL_MTIME if key == "date" else COL_PATH
        rows = sorted(range(len(self.store)),
                      key=lambda i: (self.store[i][col] if key == "date"
                                     else GLib.utf8_collate_key_for_filename(Path(self.store[i][col]).name, -1)),
                      reverse=reverse)
        self.store.reorder(rows)

    def _sources(self):
        return [operations.Source(Path(row[COL_PATH]), row[COL_RANGE]) for row in self.store]

    # -- actions ------------------------------------------------------------------

    def _on_arrange(self, _button):
        paths = [row[COL_PATH] for row in self.store]
        subprocess.Popen(["pdfarranger", *paths])
        self._on_close()

    def _target(self):
        name = self.name_entry.get_text().strip() or _("merged")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        name = name.replace("/", "_")
        folder = Path(self.folder_button.get_filename() or Path.home())
        return unique_path(folder / name)

    def _on_merge(self, _button):
        sources = self._sources()
        if not sources:
            return
        options = {key: check.get_active() for key, check in self.checks.items()}
        save_config("merge", options)
        target = self._target()
        self.go_button.set_sensitive(False)
        self.info.hide()
        self.progress.show()

        def job():
            return operations.merge(sources, target, progress=lambda f: GLib.idle_add(self.progress.set_fraction, f),
                                    cancel=self.cancel_event.is_set, **options)

        def done(path):
            self.go_button.set_sensitive(True)
            self.progress.hide()
            if path is None:
                return
            self.result_path = Path(path)
            self.info.set_message_type(Gtk.MessageType.INFO)
            self.info_label.set_text(_("Saved as “{name}” ({size}).").format(
                name=self.result_path.name, size=format_size(self.result_path.stat().st_size)))
            self.info.show()
            self.close_button.set_label(_("_Close"))
            self.close_button.set_use_underline(True)

        def failed(exc):
            self.go_button.set_sensitive(True)
            self.progress.hide()
            show_message(self, Gtk.MessageType.ERROR, _("The files could not be merged."), error_text(exc))

        self.worker.submit(job, done, failed)

    def _on_info(self, _bar, response):
        if self.result_path is None:
            return
        if response == 1:
            open_file(self.result_path)
        elif response == 2:
            show_in_file_manager([self.result_path])
        elif response == 3:
            launch_tool("compress", [self.result_path])

    def _on_close(self, *_args):
        self.cancel_event.set()
        self.worker.stop()
        Gtk.main_quit()
        return True
