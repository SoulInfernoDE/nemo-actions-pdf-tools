"""Command line entry point: pdf-tools COMMAND [--gui] FILES..."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, VERSION
from .i18n import _

COMMANDS = ("compress", "merge", "split", "rotate", "export", "protect", "unprotect", "arrange", "update")


def _gui(command, files, args):
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("nemo-pdf-tools")
    from .ui_common import run_app

    if command == "compress":
        from .compress_window import CompressWindow

        run_app(CompressWindow(files))
    elif command == "merge":
        from .merge_window import MergeWindow

        run_app(MergeWindow(files))
    elif command in ("split", "export", "protect"):
        from . import dialogs

        cls = {"split": dialogs.SplitWindow, "export": dialogs.ExportWindow, "protect": dialogs.ProtectWindow}
        run_app(cls[command](files))
    elif command == "unprotect":
        from . import dialogs

        return dialogs.unprotect(files)
    elif command == "rotate":
        from . import dialogs

        return dialogs.rotate(files, args.angle)
    return 0


def _cli_compress(files, args):
    from . import compress

    settings = compress.settings_for_strength(args.strength, verify=not args.no_verify)
    status = 0
    for path in files:
        doc = compress.Document(path)
        doc.analyze()
        folder = Path(args.output_dir) if args.output_dir else path.parent
        target = compress.unique_path(folder / f"{path.stem}_compressed.pdf")
        result = doc.compress(settings, target)
        if result.status == "ok":
            print(f"{path} -> {result.path}: {result.original_size} -> {result.size} bytes")
        else:
            print(f"{path}: {result.status}")
        doc.close()
    return status


def _update(args):
    from . import updater
    from .ui_common import load_config, save_config

    if args.auto:
        config = load_config(updater.CONFIG_SECTION)
        config["auto_check"] = args.auto == "on"
        save_config(updater.CONFIG_SECTION, config)
        print(_("Automatic update check: {state}").format(state=_("on") if args.auto == "on" else _("off")))
        return 0
    try:
        tag = updater.check(force=True)
    except OSError as exc:
        print(_("The update check failed: {error}").format(error=exc), file=sys.stderr)
        return 1
    if not tag:
        print(_("{name} {version} is up to date.").format(name=APP_NAME, version=VERSION))
        return 0
    print(_("Installing version {version}…").format(version=tag.lstrip("v")))
    print(updater.install(tag))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pdf-tools", description=f"{APP_NAME} {VERSION}")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--gui", action="store_true", help="show the dialog (used by the Nemo menu)")
    parser.add_argument("--angle", type=int, default=90, help="rotate: 90, -90 or 180")
    parser.add_argument("--strength", type=int, default=50, help="compress: 0 (lossless) to 100")
    parser.add_argument("--output-dir", help="compress: folder for the results")
    parser.add_argument("--no-verify", action="store_true", help="compress: skip the visual check")
    parser.add_argument("-o", "--output", type=Path, help="merge: output file")
    parser.add_argument("--auto", choices=("on", "off"), help="update: switch the daily background check")
    args = parser.parse_intermixed_args(argv)
    files = [f for f in args.files if f.exists()]

    if args.command == "update":
        return _update(args)

    if args.command == "arrange":
        if not shutil.which("pdfarranger"):
            print(_("PDF Arranger is not installed."), file=sys.stderr)
            return 1
        subprocess.Popen(["pdfarranger", *map(str, files)])
        return 0
    if args.gui or args.command in ("unprotect", "protect", "export"):
        return _gui(args.command, files, args)
    if args.command == "compress":
        return _cli_compress(files, args)
    if args.command == "merge":
        from . import operations

        out = args.output or Path("merged.pdf")
        operations.merge([operations.Source(f) for f in files], out)
        print(out)
        return 0
    if args.command == "rotate":
        from . import operations

        for f in files:
            operations.rotate(f, args.angle)
        return 0
    if args.command == "split":
        from . import operations

        for f in files:
            for written in operations.split(f, operations.SPLIT_EACH, "", f.parent):
                print(written)
        return 0
    return _gui(args.command, files, args)
