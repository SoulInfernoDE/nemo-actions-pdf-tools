#!/usr/bin/env bash
# Install (or with --uninstall remove) the Nemo action for the current user.
set -euo pipefail

NAME=pdf-password-remover
SRC=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
ACTIONS=$DATA_HOME/nemo/actions
LOCALES=$DATA_HOME/locale

do_uninstall() {
    rm -f "$ACTIONS/$NAME.nemo_action" "$ACTIONS/$NAME.py"
    rm -f "$LOCALES"/*/LC_MESSAGES/"$NAME".mo
    echo "Removed $NAME."
}

do_install() {
    mkdir -p "$ACTIONS"
    install -m 755 "$SRC/$NAME.py" "$ACTIONS/$NAME.py"

    if command -v msgfmt >/dev/null; then
        # Menu label and tooltip get their translations from the same .po files.
        msgfmt --desktop --template="$SRC/$NAME.nemo_action.in" -d "$SRC/po" \
            --keyword=Name --keyword=Comment -o "$ACTIONS/$NAME.nemo_action"
        while read -r lang; do
            [ -n "$lang" ] || continue
            mkdir -p "$LOCALES/$lang/LC_MESSAGES"
            msgfmt -o "$LOCALES/$lang/LC_MESSAGES/$NAME.mo" "$SRC/po/$lang.po"
        done < "$SRC/po/LINGUAS"
    else
        echo "Warning: msgfmt not found (package gettext), installing without translations." >&2
        install -m 644 "$SRC/$NAME.nemo_action.in" "$ACTIONS/$NAME.nemo_action"
    fi

    if ! /usr/bin/python3 -c 'import pikepdf' 2>/dev/null; then
        echo "Note: pikepdf is missing. Install it with: sudo apt install python3-pikepdf" >&2
    fi
    echo "Installed $NAME to $ACTIONS."
}

case "${1:-}" in
    --uninstall) do_uninstall ;;
    "") do_install ;;
    *) echo "Usage: $0 [--uninstall]" >&2; exit 2 ;;
esac
