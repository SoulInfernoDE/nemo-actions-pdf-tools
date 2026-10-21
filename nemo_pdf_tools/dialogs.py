"""Smaller tools: split, export, protect, remove password, rotate."""

import threading
from pathlib import Path

import pikepdf
from gi.repository import GLib, Gtk

from . import APP_NAME, operations
from .compress import DocumentError, open_pdf, unique_path
from .i18n import _, ngettext
from .ui_common import (
    UpdateBar,
    Worker,
    dim_label,
    error_text,
    load_config,
    notify,
    save_config,
    section_label,
    show_in_file_manager,
    show_message,
)


def files_label(paths):
    if len(paths) == 1:
        return Path(paths[0]).name
    return ngettext("{n} file", "{n} files", len(paths)).format(n=len(paths))


class ToolWindow(Gtk.Window):
    """Common frame: content area, progress bar, Cancel/action buttons."""

    def __init__(self, title, paths, action_label):
        super().__init__(title=title)
        self.set_icon_name("application-pdf")
        self.set_resizable(False)
        self.paths = [Path(p) for p in paths]
        self.worker = Worker()
        self.cancel_event = threading.Event()
        self.written = []
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=14)
        self.add(outer)
        outer.pack_start(UpdateBar(), False, False, 0)
        head = Gtk.Label(xalign=0)
        head.set_markup("<big><b>%s</b></big>" % GLib.markup_escape_text(files_label(self.paths)))
        head.set_ellipsize(3)
        head.set_max_width_chars(60)
        outer.pack_start(head, False, False, 0)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.pack_start(self.content, True, True, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_no_show_all(True)
        outer.pack_start(self.progress, False, False, 0)
        bottom = Gtk.Box(spacing=8, margin_top=6)
        self.show_button = Gtk.Button(label=_("Show in folder"))
        self.show_button.set_no_show_all(True)
        self.show_button.connect("clicked", lambda *_a: show_in_file_manager(self.written))
        bottom.pack_start(self.show_button, False, False, 0)
        self.go_button = Gtk.Button.new_with_mnemonic(action_label)
        self.go_button.get_style_context().add_class("suggested-action")
        self.go_button.connect("clicked", lambda *_a: self.start())
        self.go_button.set_can_default(True)
        self.close_button = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        self.close_button.connect("clicked", lambda *_a: self.close_window())
        bottom.pack_end(self.go_button, False, False, 0)
        bottom.pack_end(self.close_button, False, False, 0)
        outer.pack_start(bottom, False, False, 0)
        self.connect("delete-event", lambda *_a: self.close_window())
        self.connect("realize", lambda *_a: self.go_button.grab_default())

    def output_row(self, default_subfolder=True, label=None):
        """Folder chooser plus optional "create subfolder" check."""
        box = Gtk.Box(spacing=8)
        box.pack_start(section_label(label or _("Save in")), False, False, 0)
        self.folder_button = Gtk.FileChooserButton(title=_("Choose target folder"),
                                                   action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_button.set_filename(str(self.paths[0].parent))
        box.pack_start(self.folder_button, True, True, 0)
        self.content.pack_start(box, False, False, 0)
        self.subfolder = None
        if default_subfolder is not None:
            self.subfolder = Gtk.CheckButton(label=_("Create a subfolder for each file"))
            self.subfolder.set_active(default_subfolder)
            self.content.pack_start(self.subfolder, False, False, 0)

    def target_dir(self, path):
        folder = Path(self.folder_button.get_filename() or path.parent)
        if self.subfolder is not None and self.subfolder.get_active():
            folder = unique_path(folder / path.stem)
        return folder

    def start(self):
        """Run self.run_one(path, progress) for every file on the worker."""
        if not self.validate():
            return
        self.go_button.set_sensitive(False)
        self.progress.show()
        errors = []
        count = len(self.paths)
        for n, path in enumerate(self.paths):
            def job(path=path, n=n):
                def progress(fraction):
                    GLib.idle_add(self._set_progress, (n + fraction) / count, path.name)
                return self.run_one(path, progress)

            def done(result):
                if result:
                    self.written.extend(result if isinstance(result, list) else [result])

            def failed(exc, path=path):
                errors.append(f"{path.name}: {error_text(exc)}")

            self.worker.submit(job, done, failed)
        self.worker.submit(lambda: None, lambda _r: self.finished(errors))

    def _set_progress(self, fraction, name):
        self.progress.set_fraction(fraction)
        self.progress.set_text(name)
        return False

    def finished(self, errors):
        self.progress.set_fraction(1.0)
        self.progress.set_text(self.done_text())
        if errors:
            show_message(self, Gtk.MessageType.ERROR, _("Some files could not be processed."), "\n".join(errors))
        if self.written:
            self.show_button.show()
        self.close_button.set_label(_("_Close"))
        self.close_button.set_use_underline(True)
        self.go_button.set_sensitive(True)

    def done_text(self):
        return ngettext("{n} file written", "{n} files written", len(self.written)).format(n=len(self.written))

    def validate(self):
        return True

    def run_one(self, path, progress):
        raise NotImplementedError

    def close_window(self):
        self.cancel_event.set()
        self.worker.stop()
        Gtk.main_quit()
        return True


# --- split -------------------------------------------------------------------

class SplitWindow(ToolWindow):
    def __init__(self, paths):
        super().__init__(_("Split PDF"), paths, _("_Split"))
        config = load_config("split")
        self.content.pack_start(section_label(_("Split into")), False, False, 0)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=12)
        self.each = Gtk.RadioButton.new_with_label(None, _("single pages"))
        self.every = Gtk.RadioButton.new_with_label_from_widget(self.each, _("parts of"))
        self.every_spin = Gtk.SpinButton.new_with_range(1, 10000, 1)
        self.every_spin.set_value(config.get("every", 2))
        self.ranges = Gtk.RadioButton.new_with_label_from_widget(self.each, _("page ranges:"))
        self.ranges_entry = Gtk.Entry(placeholder_text=_("e.g. 1-3; 4-10; 11-"), width_chars=26)
        self.bookmarks = Gtk.RadioButton.new_with_label_from_widget(self.each, _("chapters (top-level bookmarks)"))
        grid.attach(self.each, 0, 0, 3, 1)
        grid.attach(self.every, 0, 1, 1, 1)
        grid.attach(self.every_spin, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label=_("pages each"), xalign=0), 2, 1, 1, 1)
        grid.attach(self.ranges, 0, 2, 1, 1)
        grid.attach(self.ranges_entry, 1, 2, 2, 1)
        grid.attach(self.bookmarks, 0, 3, 3, 1)
        self.content.pack_start(grid, False, False, 0)
        self.content.pack_start(dim_label(_("Separate the files with “;”. Within one file, “1-3, 7” takes "
                                            "pages 1 to 3 and 7. To extract pages, enter a single range.")),
                                False, False, 0)
        self.preview = dim_label()
        self.content.pack_start(self.preview, False, False, 0)
        self.output_row(default_subfolder=bool(config.get("subfolder", True)))
        {"every": self.every, "ranges": self.ranges, "bookmarks": self.bookmarks}.get(
            config.get("mode"), self.each).set_active(True)
        self.every_spin.connect("value-changed", lambda *_a: (self.every.set_active(True), self._update()))
        self.ranges_entry.connect("changed", lambda *_a: (self.ranges.set_active(True), self._update()))
        for b in (self.each, self.every, self.ranges, self.bookmarks):
            b.connect("toggled", lambda *_a: self._update())
        self.counts = {}
        self.has_outline = {}
        for path in self.paths:
            try:
                with open_pdf(path) as pdf:
                    self.counts[path] = len(pdf.pages)
                    with pdf.open_outline() as outline:
                        self.has_outline[path] = len(outline.root) > 0
            except (DocumentError, pikepdf.PdfError, pikepdf.OutlineStructureError):
                pass
        if not any(self.has_outline.values()):
            self.bookmarks.set_sensitive(False)
            self.bookmarks.set_tooltip_text(_("The file has no bookmarks."))
        self._update()

    def mode(self):
        if self.every.get_active():
            return operations.SPLIT_EVERY, str(int(self.every_spin.get_value()))
        if self.ranges.get_active():
            return operations.SPLIT_RANGES, self.ranges_entry.get_text()
        if self.bookmarks.get_active():
            return operations.SPLIT_BOOKMARKS, ""
        return operations.SPLIT_EACH, ""

    def _update(self):
        mode, value = self.mode()
        total = 0
        try:
            for path, count in self.counts.items():
                if mode == operations.SPLIT_EACH:
                    total += count
                elif mode == operations.SPLIT_EVERY:
                    total += -(-count // max(1, int(value)))
                elif mode == operations.SPLIT_RANGES:
                    parts = [p for p in value.replace("\n", ";").split(";") if p.strip()]
                    for part in parts:
                        operations.parse_ranges(part, count)
                    total += len(parts)
                else:
                    total = None
                    break
        except ValueError as exc:
            self.preview.set_text(_("Invalid page range: {value}").format(value=str(exc)))
            self.go_button.set_sensitive(False)
            return
        self.go_button.set_sensitive(total is None or total > 0)
        if total is None:
            self.preview.set_text(_("One file per chapter."))
        else:
            self.preview.set_text(ngettext("Creates {n} file.", "Creates {n} files.", total).format(n=total))

    def run_one(self, path, progress):
        mode, value = self.mode()
        return operations.split(path, mode, value, self.target_dir(path), progress, self.cancel_event.is_set)

    def start(self):
        mode, value = self.mode()
        save_config("split", {"mode": mode, "every": int(self.every_spin.get_value()),
                              "subfolder": self.subfolder.get_active()})
        super().start()


# --- export ------------------------------------------------------------------

class ExportWindow(ToolWindow):
    def __init__(self, paths):
        super().__init__(_("Export as images"), paths, _("_Export"))
        config = load_config("export")
        self.render = Gtk.RadioButton.new_with_label(None, _("Save pages as images"))
        self.extract = Gtk.RadioButton.new_with_label_from_widget(
            self.render, _("Extract embedded images in their original quality"))
        self.content.pack_start(self.render, False, False, 0)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, margin_start=28)
        self.format = Gtk.ComboBoxText()
        self.format.append("png", _("PNG (lossless)"))
        self.format.append("jpeg", _("JPEG (smaller)"))
        self.format.set_active_id(config.get("format", "png"))
        self.dpi = Gtk.ComboBoxText()
        for dpi, text in ((72, _("72 dpi (screen)")), (150, _("150 dpi")), (300, _("300 dpi (print)")),
                          (600, _("600 dpi"))):
            self.dpi.append(str(dpi), text)
        self.dpi.set_active_id(str(config.get("dpi", 150)))
        self.pages = Gtk.Entry(placeholder_text=_("all pages"), width_chars=16)
        grid.attach(Gtk.Label(label=_("Format"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.format, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Resolution"), xalign=0), 0, 1, 1, 1)
        grid.attach(self.dpi, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label=_("Pages"), xalign=0), 0, 2, 1, 1)
        grid.attach(self.pages, 1, 2, 1, 1)
        self.content.pack_start(grid, False, False, 0)
        self.content.pack_start(self.extract, False, False, 0)
        self.render.connect("toggled", lambda b: grid.set_sensitive(b.get_active()))
        if config.get("mode") == "extract":
            self.extract.set_active(True)
        self.output_row(default_subfolder=bool(config.get("subfolder", True)))

    def validate(self):
        if self.render.get_active() and self.pages.get_text().strip():
            for path in self.paths:
                try:
                    with open_pdf(path) as pdf:
                        operations.parse_ranges(self.pages.get_text(), len(pdf.pages))
                except ValueError as exc:
                    show_message(self, Gtk.MessageType.WARNING, error_text(exc))
                    return False
                except DocumentError:
                    pass
        save_config("export", {"mode": "render" if self.render.get_active() else "extract",
                               "format": self.format.get_active_id(), "dpi": int(self.dpi.get_active_id()),
                               "subfolder": self.subfolder.get_active()})
        return True

    def run_one(self, path, progress):
        folder = self.target_dir(path)
        if self.extract.get_active():
            return operations.extract_images(path, folder, progress=progress, cancel=self.cancel_event.is_set)
        return operations.render_pages(path, folder, self.format.get_active_id(), int(self.dpi.get_active_id()),
                                       self.pages.get_text(), progress=progress, cancel=self.cancel_event.is_set)

    def done_text(self):
        return ngettext("{n} image saved", "{n} images saved", len(self.written)).format(n=len(self.written))


# --- protect -----------------------------------------------------------------

class ProtectWindow(ToolWindow):
    def __init__(self, paths):
        super().__init__(_("Protect PDF with password"), paths, _("_Protect"))
        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        self.password = Gtk.Entry(visibility=False, width_chars=30, activates_default=True)
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.repeat = Gtk.Entry(visibility=False, width_chars=30, activates_default=True)
        self.repeat.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        grid.attach(Gtk.Label(label=_("Password"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.password, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Repeat"), xalign=0), 0, 1, 1, 1)
        grid.attach(self.repeat, 1, 1, 1, 1)
        show = Gtk.CheckButton.new_with_mnemonic(_("_Show password"))
        show.connect("toggled", lambda b: (self.password.set_visibility(b.get_active()),
                                           self.repeat.set_visibility(b.get_active())))
        grid.attach(show, 1, 2, 1, 1)
        self.content.pack_start(grid, False, False, 0)
        self.content.pack_start(section_label(_("Allow after opening")), False, False, 4)
        self.allow = {}
        for key, text in (("print", _("Printing")), ("copy", _("Copying text and images")),
                          ("edit", _("Editing, comments and form filling"))):
            check = Gtk.CheckButton(label=text, active=True, margin_start=12)
            self.allow[key] = check
            self.content.pack_start(check, False, False, 0)
        self.content.pack_start(dim_label(_("Encrypted with AES-256. A protected copy is saved; "
                                            "the original stays as it is.")), False, False, 0)
        self.output_row(default_subfolder=None)

    def validate(self):
        if not self.password.get_text():
            show_message(self, Gtk.MessageType.WARNING, _("Please enter a password."))
            return False
        if self.password.get_text() != self.repeat.get_text():
            show_message(self, Gtk.MessageType.WARNING, _("The passwords do not match."))
            return False
        return True

    def run_one(self, path, progress):
        # TRANSLATORS: appended to the file name of password protected copies
        suffix = _("_protected")
        target = unique_path(self.target_dir(path) / f"{path.stem}{suffix}.pdf")
        progress(0.5)
        restricted = not all(c.get_active() for c in self.allow.values())
        # With restrictions, a separate random owner password keeps users
        # from simply lifting them with the open password.
        owner = GLib.uuid_string_random() if restricted else ""
        return operations.protect(path, target, self.password.get_text(), owner,
                                  self.allow["print"].get_active(), self.allow["copy"].get_active(),
                                  self.allow["edit"].get_active())


# --- remove password ------------------------------------------------------------

def ask_password(path, retry):
    """Password prompt for one file. Returns None if skipped."""
    dialog = Gtk.Dialog(title=APP_NAME)
    dialog.set_icon_name("application-pdf")
    dialog.add_button(_("_Skip"), Gtk.ResponseType.CANCEL)
    ok = dialog.add_button(_("_Remove password"), Gtk.ResponseType.OK)
    ok.get_style_context().add_class("suggested-action")
    dialog.set_default_response(Gtk.ResponseType.OK)
    dialog.set_resizable(False)
    box = dialog.get_content_area()
    box.set_spacing(8)
    box.set_border_width(12)
    if retry:
        error = Gtk.Label(xalign=0)
        error.set_markup("<b>%s</b>" % GLib.markup_escape_text(_("Wrong password. Please try again.")))
        error.get_style_context().add_class("error")
        box.add(error)
    label = Gtk.Label(xalign=0)
    label.set_markup(GLib.markup_escape_text(_("Password for:")) + "\n<b>%s</b>" % GLib.markup_escape_text(path.name))
    label.set_line_wrap(True)
    label.set_max_width_chars(50)
    box.add(label)
    entry = Gtk.Entry(visibility=False, activates_default=True, width_chars=36)
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    box.add(entry)
    show = Gtk.CheckButton.new_with_mnemonic(_("_Show password"))
    show.connect("toggled", lambda button: entry.set_visibility(button.get_active()))
    box.add(show)
    dialog.show_all()
    response = dialog.run()
    password = entry.get_text()
    dialog.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration()
    return password if response == Gtk.ResponseType.OK else None


def unprotect(paths):
    """Save unprotected copies; passwords that worked are tried on later files."""
    known = []
    done, not_encrypted, skipped, failed = [], [], [], []
    for path in map(Path, paths):
        try:
            try:
                pdf = pikepdf.open(path)
            except pikepdf.PasswordError:
                pdf = None
                for password in known:
                    try:
                        pdf = pikepdf.open(path, password=password)
                        break
                    except pikepdf.PasswordError:
                        pass
                retry = False
                while pdf is None:
                    password = ask_password(path, retry)
                    if password is None:
                        break
                    try:
                        pdf = pikepdf.open(path, password=password)
                        known.insert(0, password)
                    except pikepdf.PasswordError:
                        retry = True
                if pdf is None:
                    skipped.append(path)
                    continue
            with pdf:
                # Files with only an owner password (printing/copying
                # restrictions) open without a password and still count.
                if not pdf.is_encrypted:
                    not_encrypted.append(path)
                    continue
                # TRANSLATORS: appended to the file name of unprotected copies
                suffix = _("_unprotected")
                target = unique_path(path.with_name(f"{path.stem}{suffix}.pdf"))
                operations._save_atomic(pdf, target, encryption=False)
                done.append(target)
        except Exception as exc:  # noqa: BLE001 - report every failure
            failed.append(f"{path.name}: {error_text(exc)}")

    if done:
        if len(done) == 1:
            notify(_("Password removed"), _("Saved as “{name}”").format(name=done[0].name))
        else:
            notify(_("Password removed"), ngettext("{count} unprotected copy saved",
                                                   "{count} unprotected copies saved", len(done)).format(count=len(done)))
    if not_encrypted:
        names = "\n".join(p.name for p in not_encrypted)
        show_message(None, Gtk.MessageType.INFO,
                     ngettext("This file is not password protected:", "These files are not password protected:",
                              len(not_encrypted)), names)
    if failed:
        show_message(None, Gtk.MessageType.ERROR, _("Some files could not be processed."), "\n".join(failed))
    return 1 if failed else 0


# --- rotate ------------------------------------------------------------------

def rotate(paths, angle):
    failed = []
    for path in map(Path, paths):
        try:
            operations.rotate(path, angle)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{path.name}: {error_text(exc)}")
    if failed:
        show_message(None, Gtk.MessageType.ERROR, _("Some files could not be rotated."), "\n".join(failed))
        return 1
    return 0
