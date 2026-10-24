#!/usr/bin/env bash
# Install (or with --uninstall remove) the PDF tools for the current user.
# Missing system packages are installed with apt first (asks for the sudo
# password); --no-deps skips that step.
set -euo pipefail

NAME=nemo-pdf-tools
SRC=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
APP_DIR=$DATA_HOME/$NAME
EXT_DIR=$DATA_HOME/nemo-python/extensions
LOCALES=$DATA_HOME/locale
BIN_DIR=$HOME/.local/bin
COMPLETIONS=${BASH_COMPLETION_USER_DIR:-$DATA_HOME/bash-completion}/completions

# System packages the tools need (names on Debian, Ubuntu and Linux Mint).
PACKAGES=(
    nemo-python          # the "PDF files" submenu
    python3-gi gir1.2-gtk-3.0 python3-gi-cairo python3-cairo
    gir1.2-poppler-0.18  # previews, visual check, page export
    python3-pikepdf      # PDF handling
    python3-pil python3-numpy
    python3-img2pdf      # images to PDF
    gettext              # builds the translations
    libnotify-bin        # notifications
)

# Files of the former standalone "Remove PDF password" action, which is now
# part of this suite.
OLD_ACTIONS=("$DATA_HOME/nemo/actions/pdf-password-remover.nemo_action" "$DATA_HOME/nemo/actions/pdf-password-remover.py")

missing_packages() {
    local p
    for p in "${PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || echo "$p"
    done
}

install_dependencies() {
    if ! command -v dpkg-query >/dev/null || ! command -v apt-get >/dev/null; then
        echo "No apt found: please install the equivalents of: ${PACKAGES[*]}" >&2
        return
    fi
    local missing
    mapfile -t missing < <(missing_packages)
    [ ${#missing[@]} -gt 0 ] || return 0
    echo "Installing missing packages: ${missing[*]}"
    local sudo=()
    [ "$(id -u)" -eq 0 ] || sudo=(sudo)
    if ! "${sudo[@]}" apt-get install -y "${missing[@]}"; then
        # Package lists can be outdated on a fresh system.
        "${sudo[@]}" apt-get update && "${sudo[@]}" apt-get install -y "${missing[@]}" || {
            echo "Could not install: ${missing[*]}" >&2
            echo "Install them with: sudo apt install ${missing[*]}" >&2
        }
    fi
}

check_runtime() {
    # Final check that also covers systems without apt.
    local problems=()
    /usr/bin/python3 -c 'import pikepdf' 2>/dev/null || problems+=(pikepdf)
    /usr/bin/python3 -c 'import img2pdf' 2>/dev/null || problems+=(img2pdf)
    /usr/bin/python3 -c 'import numpy, PIL' 2>/dev/null || problems+=("numpy/Pillow")
    /usr/bin/python3 -c 'import gi, cairo; gi.require_version("Gtk", "3.0"); gi.require_version("Poppler", "0.18")' \
        2>/dev/null || problems+=("GTK 3/Poppler/cairo bindings")
    [ -d /usr/share/nemo-python ] || problems+=(nemo-python)
    if [ ${#problems[@]} -gt 0 ]; then
        echo "Warning: still missing: ${problems[*]}. The tools will not work until they are installed." >&2
    fi
}

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
    local with_deps=$1
    if [ "$with_deps" = yes ]; then
        install_dependencies
    fi

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

    check_runtime
    echo "Installed $NAME to $APP_DIR."
    echo "Restart Nemo once (nemo -q, then open it again) to see the \"PDF files\" menu."
}

case "${1:-}" in
    --uninstall) do_uninstall ;;
    --no-deps) do_install no ;;
    "") do_install yes ;;
    *) echo "Usage: $0 [--no-deps | --uninstall]" >&2; exit 2 ;;
esac
