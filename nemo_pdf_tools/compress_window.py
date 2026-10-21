"""Dialog for compressing one or more PDF files with a live estimate."""

import os
import threading
from pathlib import Path

import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import compress
from .i18n import _, ngettext
from .ui_common import (
    UpdateBar,
    Worker,
    dim_label,
    error_text,
    format_size,
    load_config,
    notify,
    pil_to_pixbuf,
    save_config,
    section_label,
    show_in_file_manager,
)

ESTIMATE_DELAY_MS = 250
ZOOMS = ((96, "100 %"), (192, "200 %"), (384, "400 %"))

OUT_NEXT = "next"
OUT_FOLDER = "folder"
OUT_REPLACE = "replace"


def preset_labels():
    return {
        "lossless": (_("Lossless"), _("Only lossless optimisation: identical content, no image is changed visibly.")),
        "high": (_("High quality"), _("Images above 300 dpi are reduced, JPEG quality 88. Suitable for printing.")),
        "balanced": (_("Balanced"), _("Images above 200 dpi are reduced, JPEG quality 78. Good for screen and office printing.")),
        "strong": (_("Strong"), _("Images above 144 dpi are reduced, JPEG quality 62. Good for screen and e-mail.")),
        "maximum": (_("Maximum"), _("Images above 96 dpi are reduced, JPEG quality 45. Smallest files, visible losses.")),
    }


def quality_text(ssim):
    if ssim >= 0.985:
        return _("Visually lossless")
    if ssim >= 0.965:
        return _("Very good")
    if ssim >= 0.94:
        return _("Good")
    if ssim >= 0.90:
        return _("Acceptable")
    return _("Visible artefacts")


def resolution_text(dpi):
    if dpi >= 290:
        return _("print quality")
    if dpi >= 190:
        return _("good for printing")
    if dpi >= 140:
        return _("screen and office printing")
    if dpi >= 90:
        return _("screen viewing")
    return _("low, fine details get lost")


class CompareView(Gtk.DrawingArea):
    """Before/after image with a draggable divider."""

    def __init__(self, size=360):
        super().__init__()
        self.set_size_request(size, size)
        self.before = None
        self.after = None
        self.split = 0.5
        self.message = ""
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._move)
        self.connect("motion-notify-event", self._move)
        self.set_tooltip_text(_("Drag to compare the original (left) with the result (right)."))

    def set_images(self, before, after, message=""):
        self.before, self.after, self.message = before, after, message
        self.queue_draw()

    def _move(self, widget, event):
        width = self.get_allocated_width()
        if width:
            self.split = min(1.0, max(0.0, event.x / width))
            self.queue_draw()
        return True

    def _draw(self, widget, cr):
        width = self.get_allocated_width()
        height = self.get_allocated_height()
        ctx = self.get_style_context()
        Gtk.render_background(ctx, cr, 0, 0, width, height)
        if self.before is None or self.after is None:
            layout = self.create_pango_layout(self.message)
            layout.set_width(int((width - 24) * 1024))
            layout.set_alignment(Pango.Alignment.CENTER)
            _w, h = layout.get_pixel_size()
            Gtk.render_layout(ctx, cr, 12, (height - h) / 2, layout)
            return
        pw, ph = self.before.get_width(), self.before.get_height()
        x0 = (width - pw) / 2
        y0 = (height - ph) / 2
        cut = x0 + pw * self.split
        cr.save()
        cr.rectangle(x0, y0, cut - x0, ph)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, self.before, x0, y0)
        cr.paint()
        cr.restore()
        cr.save()
        cr.rectangle(cut, y0, x0 + pw - cut, ph)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, self.after, x0, y0)
        cr.paint()
        cr.restore()
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(2)
        cr.move_to(cut, y0)
        cr.line_to(cut, y0 + ph)
        cr.stroke()
        for text, right in ((_("Original"), False), (_("Result"), True)):
            layout = self.create_pango_layout(text)
            tw, th = layout.get_pixel_size()
            tx = x0 + pw - tw - 10 if right else x0 + 10
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.rectangle(tx - 4, y0 + 6, tw + 8, th + 4)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(tx, y0 + 8)
            PangoCairo.show_layout(cr, layout)


class DocState:
    def __init__(self, path):
        self.path = Path(path)
        self.size = self.path.stat().st_size if self.path.exists() else 0
        self.doc = None
        self.error = None
        self.estimate = None
        self.result = None
        self.iter = None


class CompressWindow(Gtk.Window):
    def __init__(self, paths):
        super().__init__(title=_("Compress PDF files"))
        self.set_default_size(1000, 720)
        self.set_icon_name("application-pdf")
        self.worker = Worker()
        self.cancel_event = threading.Event()
        self.generation = 0
        self.estimate_source = None
        self.running = False
        self.updating = False
        self.custom = False
        self.states = [DocState(p) for p in paths]
        self.config = load_config("compress")
        self._build()
        self._load_settings()
        self.connect("delete-event", self._on_close)
        for state in self.states:
            self._analyze(state)

    # -- layout ---------------------------------------------------------------

    def _build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12)
        self.add(outer)
        outer.pack_start(UpdateBar(), False, False, 0)

        # File list
        self.store = Gtk.ListStore(str, str, str, str, str, int)
        for n, state in enumerate(self.states):
            state.iter = self.store.append([state.path.name, "", format_size(state.size), "…", _("Analysing…"), n])
        self.tree = Gtk.TreeView(model=self.store, headers_visible=True)
        for col, title, expand in ((0, _("File"), True), (1, _("Pages"), False), (2, _("Size"), False),
                                   (3, _("Estimated"), False), (4, _("Status"), True)):
            renderer = Gtk.CellRendererText(ellipsize=3 if col in (0, 4) else 0)
            column = Gtk.TreeViewColumn(title, renderer, text=col)
            column.set_expand(expand)
            column.set_resizable(True)
            self.tree.append_column(column)
        self.tree.get_selection().connect("changed", lambda *_a: self._schedule_estimate())
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_min_content_height(min(170, 34 + 26 * len(self.states)))
        scroller.add(self.tree)
        frame = Gtk.Frame()
        frame.add(scroller)
        outer.pack_start(frame, False, True, 0)
        if self.states:
            self.tree.get_selection().select_path(Gtk.TreePath.new_first())

        body = Gtk.Box(spacing=18)
        outer.pack_start(body, True, True, 0)

        # Left column: settings and live information
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.pack_start(left, True, True, 0)

        left.pack_start(section_label(_("Compression level")), False, False, 0)
        presets = Gtk.Box(spacing=0)
        presets.get_style_context().add_class("linked")
        self.preset_buttons = {}
        group = None
        labels = preset_labels()
        for key, strength in compress.PRESETS:
            label, tip = labels[key]
            button = Gtk.RadioButton.new_with_label_from_widget(group, label)
            button.set_mode(False)
            button.set_tooltip_text(tip)
            group = group or button
            button.connect("toggled", self._on_preset, strength)
            presets.pack_start(button, True, True, 0)
            self.preset_buttons[strength] = button
        # Hidden member of the group, active while the level is custom.
        self.custom_button = Gtk.RadioButton.new_from_widget(group)
        left.pack_start(presets, False, False, 0)

        scale_box = Gtk.Box(spacing=8)
        scale_box.pack_start(Gtk.Label(label=_("Strength")), False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_digits(0)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale.connect("format-value", lambda _s, v: "%d %%" % v)
        for _key, strength in compress.PRESETS:
            self.scale.add_mark(strength, Gtk.PositionType.BOTTOM, None)
        self.scale.connect("value-changed", self._on_scale)
        scale_box.pack_start(self.scale, True, True, 0)
        left.pack_start(scale_box, False, False, 0)

        self.level_hint = dim_label()
        left.pack_start(self.level_hint, False, False, 0)

        expander = Gtk.Expander(label=_("Advanced settings"))
        grid = Gtk.Grid(row_spacing=6, column_spacing=10, margin_top=6, margin_start=12)
        expander.add(grid)
        self.dpi_spin = Gtk.SpinButton.new_with_range(50, 1200, 10)
        self.dpi_spin.connect("value-changed", self._on_custom)
        self.quality_spin = Gtk.SpinButton.new_with_range(10, 100, 1)
        self.quality_spin.connect("value-changed", self._on_custom)
        grid.attach(Gtk.Label(label=_("Reduce images above"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.dpi_spin, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("dpi"), xalign=0), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("JPEG quality"), xalign=0), 0, 1, 1, 1)
        grid.attach(self.quality_spin, 1, 1, 1, 1)
        self.checks = {}
        options = (
            ("grayscale", _("Store colour images without colour as grayscale")),
            ("remove_extras", _("Remove page thumbnails and private editor data")),
            ("remove_metadata", _("Remove document properties (title, author, …)")),
            ("linearize", _("Optimise for fast web view")),
            ("verify", _("Check every changed page visually afterwards")),
        )
        for row, (key, text) in enumerate(options, start=2):
            check = Gtk.CheckButton(label=text)
            check.connect("toggled", lambda *_a: self._schedule_estimate())
            grid.attach(check, 0, row, 3, 1)
            self.checks[key] = check
        self.checks["verify"].set_tooltip_text(
            _("Renders original and result and compares them. Images that would change a page "
              "visibly are left untouched."))
        left.pack_start(expander, False, False, 0)

        left.pack_start(Gtk.Separator(), False, False, 4)
        left.pack_start(section_label(_("Expected result")), False, False, 0)
        info = Gtk.Grid(row_spacing=6, column_spacing=12)
        self.size_label = Gtk.Label(xalign=0)
        self.saving_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self.saving_bar.set_size_request(160, -1)
        self.quality_label = Gtk.Label(xalign=0, wrap=True)
        self.quality_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self.resolution_label = Gtk.Label(xalign=0, wrap=True)
        self.images_label = Gtk.Label(xalign=0, wrap=True)
        rows = (
            (_("File size"), self.size_label, self.saving_bar),
            (_("Image quality"), self.quality_label, self.quality_bar),
            (_("Resolution"), self.resolution_label, None),
            (_("Images"), self.images_label, None),
        )
        for row, (title, value, bar) in enumerate(rows):
            name = dim_label(title, wrap=False)
            name.set_valign(Gtk.Align.START)
            info.attach(name, 0, row, 1, 1)
            info.attach(value, 1, row, 1 if bar else 2, 1)
            value.set_hexpand(True)
            if bar:
                bar.set_valign(Gtk.Align.CENTER)
                info.attach(bar, 2, row, 1, 1)
        left.pack_start(info, False, False, 0)
        self.warning_label = Gtk.Label(xalign=0, wrap=True)
        self.warning_label.set_no_show_all(True)
        left.pack_start(self.warning_label, False, False, 0)
        left.pack_start(dim_label(_("Text, fonts and vector graphics are never changed, so letters cannot go "
                                    "missing. Only images are optimised.")), False, False, 0)

        # Right column: preview
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.pack_start(right, False, False, 0)
        head = Gtk.Box(spacing=8)
        head.pack_start(section_label(_("Preview")), False, False, 0)
        self.zoom_combo = Gtk.ComboBoxText()
        for dpi, label in ZOOMS:
            self.zoom_combo.append(str(dpi), label)
        self.zoom_combo.set_tooltip_text(_("Zoom level of the preview, like in a PDF viewer"))
        self.zoom_combo.connect("changed", lambda *_a: self._schedule_estimate())
        head.pack_end(self.zoom_combo, False, False, 0)
        right.pack_start(head, False, False, 0)
        self.compare = CompareView()
        frame = Gtk.Frame()
        frame.add(self.compare)
        right.pack_start(frame, False, False, 0)
        self.preview_caption = dim_label()
        self.preview_caption.set_max_width_chars(44)
        right.pack_start(self.preview_caption, False, False, 0)

        # Output
        outer.pack_start(Gtk.Separator(), False, False, 0)
        out_box = Gtk.Box(spacing=12)
        out_box.pack_start(section_label(_("Save")), False, False, 0)
        self.out_next = Gtk.RadioButton.new_with_label(None, _("Next to the original"))
        self.out_folder = Gtk.RadioButton.new_with_label_from_widget(self.out_next, _("In folder:"))
        self.folder_button = Gtk.FileChooserButton(title=_("Choose target folder"),
                                                   action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_button.connect("file-set", lambda *_a: self.out_folder.set_active(True))
        self.out_replace = Gtk.RadioButton.new_with_label_from_widget(self.out_next, _("Replace original"))
        self.out_replace.set_tooltip_text(_("The original is moved to the trash, so it can be restored."))
        for w in (self.out_next, self.out_folder, self.folder_button, self.out_replace):
            out_box.pack_start(w, False, False, 0)
        outer.pack_start(out_box, False, False, 0)

        bottom = Gtk.Box(spacing=8)
        self.progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
        self.progress.set_no_show_all(True)
        bottom.pack_start(self.progress, True, True, 0)
        self.open_button = Gtk.Button(label=_("Show in folder"))
        self.open_button.set_no_show_all(True)
        self.open_button.connect("clicked", self._on_show)
        self.close_button = Gtk.Button.new_with_mnemonic(_("_Cancel"))
        self.close_button.connect("clicked", lambda *_a: self._on_close())
        self.go_button = Gtk.Button.new_with_mnemonic(_("_Compress"))
        self.go_button.get_style_context().add_class("suggested-action")
        self.go_button.connect("clicked", self._on_compress)
        bottom.pack_end(self.go_button, False, False, 0)
        bottom.pack_end(self.close_button, False, False, 0)
        bottom.pack_end(self.open_button, False, False, 0)
        outer.pack_start(bottom, False, False, 0)

    # -- settings -------------------------------------------------------------

    def _load_settings(self):
        c = self.config
        self.updating = True
        for key, check in self.checks.items():
            default = key in ("grayscale", "remove_extras", "verify")
            check.set_active(bool(c.get(key, default)))
        self.zoom_combo.set_active_id(str(c.get("zoom", 192)))
        if self.zoom_combo.get_active_id() is None:
            self.zoom_combo.set_active_id("192")
        mode = c.get("output", OUT_NEXT)
        folder = c.get("folder")
        if mode == OUT_FOLDER and folder and Path(folder).is_dir():
            self.folder_button.set_filename(folder)
        elif self.states:
            self.folder_button.set_filename(str(self.states[0].path.parent))
        {OUT_FOLDER: self.out_folder, OUT_REPLACE: self.out_replace}.get(mode, self.out_next).set_active(True)
        self.updating = False
        self.custom = bool(c.get("custom", False))
        self.scale.set_value(int(c.get("strength", 50)))
        self._on_scale(self.scale)
        if self.custom:
            self.dpi_spin.set_value(int(c.get("dpi", 200)))
            self.quality_spin.set_value(int(c.get("quality", 78)))
            self._on_custom(None)

    def _save_settings(self):
        values = {key: check.get_active() for key, check in self.checks.items()}
        values.update(
            strength=int(self.scale.get_value()),
            custom=self.custom,
            dpi=int(self.dpi_spin.get_value()),
            quality=int(self.quality_spin.get_value()),
            zoom=int(self.zoom_combo.get_active_id() or 192),
            output=self._output_mode(),
            folder=self.folder_button.get_filename() or "",
        )
        save_config("compress", values)

    def settings(self):
        strength = int(self.scale.get_value())
        options = {key: check.get_active() for key, check in self.checks.items()}
        if strength == 0 and not self.custom:
            return compress.Settings(**options)
        return compress.Settings(max_dpi=int(self.dpi_spin.get_value()),
                                 jpeg_quality=int(self.quality_spin.get_value()), **options)

    def _on_preset(self, button, strength):
        if button.get_active() and not self.updating:
            self.custom = False
            self.scale.set_value(strength)

    def _on_scale(self, scale):
        if self.updating:
            return
        strength = int(scale.get_value())
        dpi, quality = compress.strength_values(strength)
        self.updating = True
        self.custom = False
        if dpi is not None:
            self.dpi_spin.set_value(dpi)
            self.quality_spin.set_value(quality)
        self.dpi_spin.set_sensitive(strength > 0)
        self.quality_spin.set_sensitive(strength > 0)
        (self.preset_buttons.get(strength) or self.custom_button).set_active(True)
        self.updating = False
        self._update_hint()
        self._schedule_estimate()

    def _on_custom(self, _spin):
        if self.updating:
            return
        self.custom = True
        self.updating = True
        self.custom_button.set_active(True)
        self.updating = False
        self._update_hint()
        self._schedule_estimate()

    def _update_hint(self):
        s = self.settings()
        if s.lossless:
            self.level_hint.set_text(_("Lossless: only the file structure and the image encoding are optimised."))
        else:
            self.level_hint.set_text(
                _("Images above {dpi} dpi are reduced to {dpi} dpi, photos are stored as JPEG with quality {q}.")
                .format(dpi=s.max_dpi, q=s.jpeg_quality))

    def _output_mode(self):
        if self.out_folder.get_active():
            return OUT_FOLDER
        if self.out_replace.get_active():
            return OUT_REPLACE
        return OUT_NEXT

    # -- analysis and live estimate --------------------------------------------

    def _analyze(self, state):
        def job():
            state.doc = compress.Document(state.path)
            state.doc.analyze(self.cancel_event.is_set)
            return state

        def done(_state):
            self.store[state.iter][1] = str(state.doc.page_count)
            self.store[state.iter][4] = ""
            self._schedule_estimate(now=True)

        def failed(exc):
            state.error = exc
            self.store[state.iter][3] = "–"
            self.store[state.iter][4] = error_text(exc)
            self._update_summary()

        self.worker.submit(job, done, failed)

    def _selected_state(self):
        model, it = self.tree.get_selection().get_selected()
        if it is None:
            return self.states[0] if self.states else None
        return self.states[model[it][5]]

    def _schedule_estimate(self, now=False):
        if self.updating or self.running:
            return
        if self.estimate_source:
            GLib.source_remove(self.estimate_source)
        self.estimate_source = GLib.timeout_add(1 if now else ESTIMATE_DELAY_MS, self._start_estimate)

    def _start_estimate(self):
        self.estimate_source = None
        self.generation += 1
        gen = self.generation
        settings = self.settings()
        zoom = int(self.zoom_combo.get_active_id() or 192)
        selected = self._selected_state()
        stale = lambda: gen != self.generation or self.cancel_event.is_set()  # noqa: E731
        order = sorted((s for s in self.states if s.doc is not None and s.doc.analyzed),
                       key=lambda s: s is not selected)
        if selected is not None and selected.doc is None and selected.error is None:
            self.compare.set_images(None, None, _("Analysing…"))
        for state in order:
            def job(state=state):
                if stale():
                    return None
                try:
                    return state.doc.estimate(settings, stale, zoom, preview=state is selected)
                except compress.Cancelled:
                    return None

            def done(est, state=state):
                if est is None or gen != self.generation:
                    return
                state.estimate = est
                self.store[state.iter][3] = "≈ " + format_size(est.size)
                if state is self._selected_state():
                    self._show_preview(state, est)
                self._update_summary()

            self.worker.submit(job, done, lambda exc: print(exc))
        return False

    def _show_preview(self, state, est):
        p = est.preview
        if p is None:
            text = _("No image in this file changes visibly at this level.")
            if state.doc and not state.doc.images:
                text = _("This file contains no images. Only its structure is optimised.")
            self.compare.set_images(None, None, text)
            self.preview_caption.set_text("")
            return
        self.compare.set_images(pil_to_pixbuf(p.before), pil_to_pixbuf(p.after))
        parts = []
        if p.page is not None:
            parts.append(_("Page {n}").format(n=p.page + 1))
        if p.dpi_before and p.dpi_after:
            parts.append(_("{a} → {b} dpi").format(a=round(p.dpi_before), b=round(p.dpi_after)))
        parts.append(_("most detailed area of the largest image"))
        self.preview_caption.set_text(" · ".join(parts))

    def _update_summary(self):
        ready = [s for s in self.states if s.estimate is not None]
        pending = [s for s in self.states if s.estimate is None and s.error is None]
        if not ready:
            self.size_label.set_text(_("Calculating…") if pending else "–")
            return
        original = sum(s.size for s in ready)
        size = sum(s.estimate.size for s in ready)
        saving = max(0.0, 1.0 - size / original) if original else 0.0
        text = _("{old} → ≈ {new} (−{pct} %)").format(old=format_size(original), new=format_size(size),
                                                     pct=round(saving * 100))
        if size >= original:
            text = _("{old} → no noticeable saving").format(old=format_size(original))
        if pending:
            text += " · " + _("still analysing")
        self.size_label.set_text(text)
        self.saving_bar.set_value(min(1.0, saving))

        settings = self.settings()
        ssims = [(s.estimate.ssim, s.estimate.changed) for s in ready if s.estimate.ssim is not None]
        if settings.lossless:
            self.quality_label.set_text(_("Lossless, identical content"))
            self.quality_bar.set_value(1.0)
        elif ssims:
            weight = sum(max(1, c) for _s, c in ssims)
            ssim = sum(s * max(1, c) for s, c in ssims) / weight
            self.quality_label.set_text(_("{label} (SSIM {value})").format(label=quality_text(ssim),
                                                                         value="%.3f" % ssim))
            self.quality_bar.set_value(min(1.0, max(0.0, (ssim - 0.85) / 0.15)))
        else:
            self.quality_label.set_text(_("Unchanged, no photo is re-encoded"))
            self.quality_bar.set_value(1.0)

        dpis = [s.estimate.min_dpi for s in ready if s.estimate.min_dpi]
        if dpis:
            low = min(dpis)
            self.resolution_label.set_text(_("at least {dpi} dpi, {label}").format(dpi=round(low),
                                                                                  label=resolution_text(low)))
        else:
            self.resolution_label.set_text(_("unchanged"))

        total = sum(s.estimate.image_count for s in ready)
        changed = sum(s.estimate.changed for s in ready)
        down = sum(s.estimate.downsampled for s in ready)
        gray = sum(s.estimate.to_gray for s in ready)
        parts = [ngettext("{n} image", "{n} images", total).format(n=total)]
        if total:
            parts.append(ngettext("{n} optimised", "{n} optimised", changed).format(n=changed))
            if down:
                parts.append(ngettext("{n} reduced in resolution", "{n} reduced in resolution", down).format(n=down))
            if gray:
                parts.append(ngettext("{n} turned into grayscale", "{n} turned into grayscale", gray).format(n=gray))
        if any(s.estimate and not s.estimate.exact for s in ready):
            parts.append(_("estimated from a sample"))
        self.images_label.set_text(", ".join(parts))

        warnings = []
        docs = [s.doc for s in self.states if s.doc is not None]
        if any(d.signed for d in docs):
            warnings.append(_("A digital signature will become invalid."))
        if any(d.pdfa for d in docs):
            warnings.append(_("PDF/A files keep their metadata; check archival conformance afterwards."))
        self.warning_label.set_markup("\n".join("⚠ " + GLib.markup_escape_text(w) for w in warnings))
        self.warning_label.set_visible(bool(warnings))

    # -- compression ------------------------------------------------------------

    def _target_for(self, state, mode):
        # TRANSLATORS: appended to the file name of compressed copies
        suffix = _("_compressed")
        if mode == OUT_FOLDER:
            folder = Path(self.folder_button.get_filename() or state.path.parent)
        else:
            folder = state.path.parent
        return compress.unique_path(folder / f"{state.path.stem}{suffix}.pdf")

    def _on_compress(self, _button):
        todo = [s for s in self.states if s.doc is not None and s.result is None]
        if not todo:
            return
        self._save_settings()
        self.running = True
        self.generation += 1
        settings = self.settings()
        mode = self._output_mode()
        self.go_button.set_sensitive(False)
        self.progress.show()
        self.progress.set_fraction(0)
        self.written = []
        count = len(todo)
        for n, state in enumerate(todo):
            target = self._target_for(state, mode)

            def job(state=state, target=target, n=n):
                def progress(fraction, phase):
                    GLib.idle_add(self._progress, n, count, fraction, phase, state)
                result = state.doc.compress(settings, target, progress, self.cancel_event.is_set)
                if result.status == "ok" and mode == OUT_REPLACE:
                    os.chmod(result.path, state.path.stat().st_mode & 0o7777)
                    original = Gio.File.new_for_path(str(state.path))
                    original.trash(None)
                    Path(result.path).rename(state.path)
                    result.path = state.path
                return result

            def done(result, state=state):
                state.result = result
                if result.status == "ok":
                    self.written.append(result.path)
                    text = _("Saved: {size} (−{pct} %)").format(
                        size=format_size(result.size), pct=round(100 * (1 - result.size / result.original_size)))
                    if result.fallback:
                        text += " · " + _("lossless only, images could not be changed safely")
                    elif result.skipped_images:
                        text += " · " + ngettext("{n} image kept as it was", "{n} images kept as they were",
                                                 result.skipped_images).format(n=result.skipped_images)
                elif result.status == "not_smaller":
                    text = _("Already optimal, no file written")
                else:
                    text = _("Cancelled")
                self.store[state.iter][4] = text

            def failed(exc, state=state):
                state.result = compress.CompressResult("error", state.size)
                text = _("Cancelled") if isinstance(exc, compress.Cancelled) else error_text(exc)
                self.store[state.iter][4] = text

            self.worker.submit(job, done, failed)
        self.worker.submit(lambda: None, lambda _r: self._finished())

    def _progress(self, n, count, fraction, phase, state):
        names = {"prepare": _("Preparing"), "images": _("Optimising images"), "save": _("Saving"),
                 "verify": _("Checking pages")}
        self.store[state.iter][4] = names.get(phase, "") + "…"
        self.progress.set_fraction((n + fraction) / count)
        self.progress.set_text(_("{name} ({n} of {count})").format(name=state.path.name, n=n + 1, count=count))
        return False

    def _finished(self):
        self.running = False
        self.progress.set_fraction(1.0)
        before = sum(s.size for s in self.states if s.result and s.result.status == "ok")
        after = sum(s.result.size for s in self.states if s.result and s.result.status == "ok")
        if self.written:
            self.progress.set_text(_("Done: {old} → {new}").format(old=format_size(before), new=format_size(after)))
            self.open_button.show()
            if not self.is_active():
                notify(_("PDF files compressed"), _("{old} → {new}").format(old=format_size(before),
                                                                           new=format_size(after)))
        else:
            self.progress.set_text(_("Done, no file was written"))
        self.close_button.set_label(_("_Close"))
        self.close_button.set_use_underline(True)

    def _on_show(self, _button):
        show_in_file_manager(self.written)

    def _on_close(self, *_args):
        self.cancel_event.set()
        if not self.running:
            self._save_settings()
        self.worker.stop()
        Gtk.main_quit()
        return True
