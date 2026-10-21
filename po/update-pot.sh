#!/usr/bin/env bash
# Regenerate the translation template and merge it into every .po file.
set -euo pipefail
cd "$(dirname "$0")/.."

pot=po/nemo-pdf-tools.pot
xgettext --language=Python --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 \
    --add-comments=TRANSLATORS --package-name=nemo-pdf-tools --sort-by-file \
    --output="$pot" nemo_pdf_tools/*.py extension/nemo-pdf-tools.py

while read -r lang; do
    [ -n "$lang" ] && msgmerge --quiet --update --backup=none "po/$lang.po" "$pot"
done < po/LINGUAS
