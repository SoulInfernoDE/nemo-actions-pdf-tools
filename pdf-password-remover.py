#!/usr/bin/python3
"""Nemo action: remove the password from PDF files.

For every selected PDF an unprotected copy named "<name>_unprotected.pdf" is
written next to the original. The original file is never modified.

Uses the system Python on purpose (not whatever "python3" is first in PATH),
because pikepdf and the GTK bindings come from distribution packages.
"""

import gettext
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    import pikepdf
except ImportError:
    pikepdf = None

DOMAIN = "pdf-password-remover"
APP_NAME = "PDF Password Remover"
OUTPUT_SUFFIX = "_unprotected"


def _locale_dir():
    # A "locale" folder next to the script wins, so a build in the source
    # tree can be tried without installing.
    local = Path(__file__).resolve().parent / "locale"
    if local.is_dir():
        return local
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "locale"


_translation = gettext.translation(DOMAIN, _locale_dir(), fallback=True)
_ = _translation.gettext
ngettext = _translation.ngettext


def show_message(kind, primary, secondary=""):
    dialog = Gtk.MessageDialog(message_type=kind, buttons=Gtk.ButtonsType.OK, text=primary)
    dialog.set_title(APP_NAME)
    if secondary:
        dialog.format_secondary_text(secondary)
    dialog.run()
    dialog.destroy()


def notify(summary, body):
    try:
        subprocess.run(
            ["notify-send", "--app-name", APP_NAME, "--icon", "application-pdf", summary, body],
            check=False,
        )
    except FileNotFoundError:
        show_message(Gtk.MessageType.INFO, summary, body)


def ask_password(pdf, retry):
    """Show a password prompt for one file. Returns None if cancelled."""
    dialog = Gtk.Dialog(title=APP_NAME)
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
    label.set_markup(
        GLib.markup_escape_text(_("Password for:")) + "\n<b>%s</b>" % GLib.markup_escape_text(pdf.name)
    )
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


def output_path(pdf):
    """Return a free path for the unprotected copy, never overwriting anything."""
    candidate = pdf.with_name(f"{pdf.stem}{OUTPUT_SUFFIX}.pdf")
    counter = 2
    while candidate.exists():
        candidate = pdf.with_name(f"{pdf.stem}{OUTPUT_SUFFIX}-{counter}.pdf")
        counter += 1
    return candidate


def save_decrypted(pdf_doc, target):
    # Write to a temporary file in the target folder first, so a failure
    # never leaves a half-written PDF behind.
    fd, tmp = tempfile.mkstemp(prefix=".pdf-password-remover-", suffix=".pdf", dir=target.parent)
    os.close(fd)
    try:
        pdf_doc.save(tmp, encryption=False)
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class Result:
    DONE = "done"
    NOT_ENCRYPTED = "not_encrypted"
    SKIPPED = "skipped"


def process(pdf, known_passwords):
    """Decrypt one file. Passwords that worked before are tried silently first."""
    try:
        doc = pikepdf.open(pdf)
    except pikepdf.PasswordError:
        doc = None
        for password in known_passwords:
            try:
                doc = pikepdf.open(pdf, password=password)
                break
            except pikepdf.PasswordError:
                pass
        retry = False
        while doc is None:
            password = ask_password(pdf, retry)
            if password is None:
                return Result.SKIPPED, None
            try:
                doc = pikepdf.open(pdf, password=password)
                if password not in known_passwords:
                    known_passwords.insert(0, password)
            except pikepdf.PasswordError:
                retry = True

    with doc:
        # Files with only an owner password (printing/copying restrictions)
        # open without a password and are still worth decrypting.
        if not doc.is_encrypted:
            return Result.NOT_ENCRYPTED, None
        target = output_path(pdf)
        save_decrypted(doc, target)
        return Result.DONE, target


def main(argv):
    if pikepdf is None:
        show_message(
            Gtk.MessageType.ERROR,
            _("The Python library pikepdf is missing."),
            _("Install it with:") + "\nsudo apt install python3-pikepdf",
        )
        return 1

    files = [Path(arg) for arg in argv if arg]
    if not files:
        return 0

    known_passwords = []
    done, not_encrypted, skipped, failed = [], [], [], []

    for pdf in files:
        try:
            result, target = process(pdf, known_passwords)
        except Exception as exc:  # noqa: BLE001 - report every failure to the user
            failed.append((pdf, exc))
            continue
        if result == Result.DONE:
            done.append(target)
        elif result == Result.NOT_ENCRYPTED:
            not_encrypted.append(pdf)
        else:
            skipped.append(pdf)

    if done:
        if len(done) == 1:
            notify(_("Password removed"), _("Saved as “{name}”").format(name=done[0].name))
        else:
            notify(
                _("Password removed"),
                ngettext(
                    "{count} unprotected copy saved", "{count} unprotected copies saved", len(done)
                ).format(count=len(done)),
            )

    if not_encrypted:
        names = "\n".join(p.name for p in not_encrypted)
        show_message(Gtk.MessageType.INFO, _("These files are not password protected:"), names)

    if failed:
        details = "\n".join(f"{p.name}: {exc}" for p, exc in failed)
        show_message(Gtk.MessageType.ERROR, _("The password could not be removed from these files:"), details)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
