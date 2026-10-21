#!/usr/bin/env bash
# Regenerate the translation template and merge it into every .po file.
set -euo pipefail
cd "$(dirname "$0")/.."

pot=po/pdf-password-remover.pot
xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 \
    --add-comments=TRANSLATORS --package-name=pdf-password-remover \
    --output="$pot" pdf-password-remover.py
xgettext --language=Desktop --keyword=Name --keyword=Comment \
    --join-existing --output="$pot" pdf-password-remover.nemo_action.in

while read -r lang; do
    [ -n "$lang" ] && msgmerge --quiet --update --backup=none "po/$lang.po" "$pot"
done < po/LINGUAS
