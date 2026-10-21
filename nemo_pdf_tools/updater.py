"""Occasional update check against the GitHub releases of this project.

Nothing runs on right-click: the check is only started when a tool window
opens, at most once per CHECK_INTERVAL, in the background and with a short
timeout. It sends one anonymous HTTPS request to the GitHub API.
"""

import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import DOMAIN, VERSION

REPO = "SoulInfernoDE/nemo-actions-pdf-tools"
API = f"https://api.github.com/repos/{REPO}"
CHECK_INTERVAL = 24 * 3600
SNOOZE = 7 * 24 * 3600
TIMEOUT = 5

STATE_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / DOMAIN / "update-check.json"
CONFIG_SECTION = "updates"


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def version_tuple(text):
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:3]) if numbers else ()


def is_newer(candidate, current=VERSION):
    return version_tuple(candidate) > version_tuple(current)


def enabled(config):
    return bool(config.get("auto_check", True))


def due(config, now=None):
    if not enabled(config):
        return False
    state = _load_state()
    now = now or time.time()
    if now < state.get("snoozed_until", 0):
        return False
    return now - state.get("last_check", 0) >= CHECK_INTERVAL


def _get_json(url):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{DOMAIN}/{VERSION}",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def latest_version():
    """Newest released version as text, or None. Raises OSError when offline."""
    try:
        release = _get_json(f"{API}/releases/latest")
        tag = release.get("tag_name")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        tag = None
    if not tag:
        # No release published yet: fall back to the newest version tag.
        tags = [t.get("name", "") for t in _get_json(f"{API}/tags")]
        tags = [t for t in tags if version_tuple(t)]
        tag = max(tags, key=version_tuple) if tags else None
    return tag


def check(force=False, config=None):
    """Return the newer version if there is one, else None."""
    if not force and not due(config or {}):
        return None
    state = _load_state()
    state["last_check"] = time.time()
    _save_state(state)
    tag = latest_version()
    if tag and is_newer(tag):
        state["latest"] = tag
        _save_state(state)
        return tag
    return None


def snooze():
    state = _load_state()
    state["snoozed_until"] = time.time() + SNOOZE
    _save_state(state)


def install(tag):
    """Download the release source and run its installer. Returns its output."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/tags/{tag}"
    with tempfile.TemporaryDirectory(prefix=f"{DOMAIN}-update-") as tmp:
        archive = Path(tmp) / "source.tar.gz"
        request = urllib.request.Request(url, headers={"User-Agent": f"{DOMAIN}/{VERSION}"})
        with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as f:
            f.write(response.read())
        target = Path(tmp) / "src"
        target.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
        roots = [p for p in target.iterdir() if (p / "install.sh").is_file()]
        if not roots:
            raise OSError("install.sh not found in the release archive")
        result = subprocess.run(["bash", str(roots[0] / "install.sh")], capture_output=True, text=True,
                                timeout=300)
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or result.stdout.strip())
        return result.stdout
