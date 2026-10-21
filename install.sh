#!/usr/bin/env bash
# Install (or with --uninstall remove) the PDF tools for the current user.
set -euo pipefail

NAME=nemo-pdf-tools
SRC=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
APP_DIR=$DATA_HOME/$NAME
EXT_DIR=$DATA_HOME/nemo-python/extensions
LOCALES=$DATA_HOME/locale
BIN_DIR=$HOME/.local/bin
COMPLETIONS=${BASH_COMPLETION_USER_DIR:-$DATA_HOME/bash-completion}/completions

# Files of the former standalone "Remove PDF password" action, which is now
# part of this suite.
OLD_ACTIONS=("$DATA_HOME/nemo/actions/pdf-password-remover.nemo_action" "$DATA_HOME/nemo/actions/pdf-password-remover.py")

do_uninstall() {
    rm -rf "${APP_DIR:?}"
    rm -f "$EXT_DIR/$NAME.py"
    rm -f "$LOCALES"/*/LC_MESSAGES/"$NAME".mo
    rm -f "$COMPLETIONS/pdf-tools"
    if [ -L "$BIN_DIR/pdf-tools" ]; then
        rm -f "$BIN_DIR/pdf-tools"
    fi
    echo "Removed $NAME. Restart Nemo (nemo -q) to update the menu."
}

do_install() {
    mkdir -p "$APP_DIR" "$EXT_DIR"
    rm -rf "${APP_DIR:?}/nemo_pdf_tools"
    cp -r "$SRC/nemo_pdf_tools" "$APP_DIR/"
    find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
    install -m 755 "$SRC/pdf-tools" "$APP_DIR/pdf-tools"
    sed "s|@LAUNCHER@|$APP_DIR/pdf-tools|" "$SRC/extension/$NAME.py" > "$EXT_DIR/$NAME.py"

    if command -v msgfmt >/dev/null; then
        while read -r lang; do
            [ -n "$lang" ] || continue
            mkdir -p "$LOCALES/$lang/LC_MESSAGES"
            msgfmt -o "$LOCALES/$lang/LC_MESSAGES/$NAME.mo" "$SRC/po/$lang.po"
        done < "$SRC/po/LINGUAS"
    else
        echo "Warning: msgfmt not found (package gettext), installing without translations." >&2
    fi

    mkdir -p "$BIN_DIR"
    if [ ! -e "$BIN_DIR/pdf-tools" ] || [ -L "$BIN_DIR/pdf-tools" ]; then
        ln -sf "$APP_DIR/pdf-tools" "$BIN_DIR/pdf-tools"
    fi
    mkdir -p "$COMPLETIONS"
    install -m 644 "$SRC/completion/pdf-tools.bash" "$COMPLETIONS/pdf-tools"

    for old in "${OLD_ACTIONS[@]}"; do
        if [ -e "$old" ]; then
            rm -f "$old"
            echo "Removed the old standalone action $(basename "$old"); it is now in the PDF files menu."
        fi
    done

    missing=()
    /usr/bin/python3 -c 'import pikepdf' 2>/dev/null || missing+=(python3-pikepdf)
    /usr/bin/python3 -c 'import img2pdf' 2>/dev/null || missing+=(python3-img2pdf)
    /usr/bin/python3 -c 'import numpy, PIL' 2>/dev/null || missing+=(python3-numpy python3-pil)
    /usr/bin/python3 -c 'import gi; gi.require_version("Poppler", "0.18")' 2>/dev/null || missing+=(gir1.2-poppler-0.18)
    [ -d /usr/share/nemo-python ] || missing+=(nemo-python)
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Note: missing packages. Install them with: sudo apt install ${missing[*]}" >&2
    fi
    echo "Installed $NAME to $APP_DIR."
    echo "Restart Nemo once (nemo -q, then open it again) to see the \"PDF files\" menu."
}

case "${1:-}" in
    --uninstall) do_uninstall ;;
    "") do_install ;;
    *) echo "Usage: $0 [--uninstall]" >&2; exit 2 ;;
esac
