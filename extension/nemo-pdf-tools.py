"""Nemo extension: a "PDF files" submenu with the PDF tools.

Runs inside the Nemo process, so it stays tiny: it only decides which menu
entries fit the selection and starts the tools as separate processes.
"""

import gettext
import os
import shutil
import subprocess
from pathlib import Path

import gi

gi.require_version("Nemo", "3.0")
from gi.repository import GObject, Nemo  # noqa: E402

DOMAIN = "nemo-pdf-tools"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
# The installer replaces this with the real path.
LAUNCHER = "@LAUNCHER@"
if LAUNCHER.startswith("@"):
    LAUNCHER = str(DATA_HOME / "nemo-pdf-tools" / "pdf-tools")

IMAGE_TYPES = {"image/jpeg", "image/png", "image/tiff", "image/bmp", "image/gif", "image/webp", "image/jp2"}

# A translation object, not gettext.textdomain(): the Nemo process is shared
# with other extensions.
_ = gettext.translation(DOMAIN, str(DATA_HOME / "locale"), fallback=True).gettext


def _paths(files):
    paths = []
    for f in files:
        if f.get_uri_scheme() != "file" or f.is_directory():
            return None
        location = f.get_location()
        path = location.get_path() if location else None
        if not path:
            return None
        paths.append(path)
    return paths


def _run(_item, command, paths, *extra):
    subprocess.Popen(["/usr/bin/python3", LAUNCHER, command, "--gui", *extra, *paths],
                     start_new_session=True, stdin=subprocess.DEVNULL)


class PdfToolsMenu(GObject.GObject, Nemo.MenuProvider, Nemo.NameAndDescProvider):
    def get_name_and_desc(self):
        return ["PDF Tools:::" + _("Compress, merge, split and convert PDF files from the context menu")]

    def get_background_items(self, window, folder):
        return None

    def get_file_items(self, window, files):
        if not files or not os.path.exists(LAUNCHER):
            return None
        paths = _paths(files)
        if not paths:
            return None
        pdfs = [f.is_mime_type("application/pdf") for f in files]
        images = [f.get_mime_type() in IMAGE_TYPES for f in files]
        if not all(p or i for p, i in zip(pdfs, images)):
            return None

        top = Nemo.MenuItem(name="PdfTools::Menu", label=_("PDF files"), tip="", icon="application-pdf")
        menu = Nemo.Menu()
        top.set_submenu(menu)

        def add(name, label, tip, icon, command, *extra):
            item = Nemo.MenuItem(name="PdfTools::" + name, label=label, tip=tip, icon=icon)
            item.connect("activate", _run, command, paths, *extra)
            menu.append_item(item)
            return item

        if all(images):
            add("Images", _("Create PDF from images…"), _("Combine the images into one PDF file"),
                "insert-image-symbolic", "merge")
            return [top]
        if not all(pdfs):
            add("MergeMixed", _("Merge into one PDF…"), _("Combine the PDF files and images into one PDF file"),
                "list-add-symbolic", "merge")
            return [top]

        add("Compress", _("Compress…"), _("Make the files smaller without touching text and fonts"),
            "document-save-as-symbolic", "compress")
        if len(paths) > 1:
            add("Merge", _("Merge…"), _("Combine the files into one PDF in a chosen order"),
                "list-add-symbolic", "merge")
        add("Split", _("Split or extract pages…"), _("Save pages or page ranges as separate files"),
            "edit-cut-symbolic", "split")

        rotate = Nemo.MenuItem(name="PdfTools::Rotate", label=_("Rotate"), tip="", icon="object-rotate-right-symbolic")
        rotate_menu = Nemo.Menu()
        rotate.set_submenu(rotate_menu)
        for name, label, angle, icon in (
            ("Right", _("90° clockwise"), "90", "object-rotate-right-symbolic"),
            ("Left", _("90° counter-clockwise"), "-90", "object-rotate-left-symbolic"),
            ("Half", _("180°"), "180", "object-flip-vertical-symbolic"),
        ):
            item = Nemo.MenuItem(name="PdfTools::Rotate" + name, label=label,
                                 tip=_("Rotates all pages in place, without any quality loss"), icon=icon)
            item.connect("activate", _run, "rotate", paths, "--angle", angle)
            rotate_menu.append_item(item)
        menu.append_item(rotate)

        add("Export", _("Export as images…"), _("Save pages as PNG or JPEG, or extract the embedded images"),
            "image-x-generic-symbolic", "export")
        menu.append_item(Nemo.MenuItem.new_separator("PdfTools::Sep1"))
        add("Protect", _("Protect with password…"), _("Save an encrypted copy that needs a password to open"),
            "changes-prevent-symbolic", "protect")
        add("Unprotect", _("Remove password…"), _("Save a copy without password protection"),
            "changes-allow-symbolic", "unprotect")
        if shutil.which("pdfarranger"):
            menu.append_item(Nemo.MenuItem.new_separator("PdfTools::Sep2"))
            add("Arrange", _("Edit pages in PDF Arranger"), _("Reorder, rotate or delete single pages"),
                "view-paged-symbolic", "arrange")
        return [top]
