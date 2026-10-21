"""Translation setup shared by all modules."""

import gettext
import os
from pathlib import Path

from . import DOMAIN


def locale_dir():
    # A "locale" folder next to the package wins, so a build in the source
    # tree can be tried without installing.
    local = Path(__file__).resolve().parent.parent / "locale"
    if local.is_dir():
        return local
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "locale"


_translation = gettext.translation(DOMAIN, locale_dir(), fallback=True)
_ = _translation.gettext
ngettext = _translation.ngettext
