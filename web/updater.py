"""
Peer update helper — compare local .py/.html files against a remote
1090toTAK instance and download changed files.
"""
import hashlib
import os
import threading
import time
import logging

log = logging.getLogger(__name__)

_APP_DIR = os.path.normpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKIP_DIRS = {"__pycache__", "venv", "env", ".git", "node_modules", ".claude", "tiles_cache"}

# Shared state (read by /api/stats, written by check_for_updates)
_state: dict = {
    "available": False,
    "files": [],
    "checking": False,
    "last_check": 0.0,
    "error": None,
}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def app_files():
    """Yield (rel_path, abs_path) for every .py/.html file in the app."""
    for root, dirs, files in os.walk(_APP_DIR):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            if fn.endswith(".py") or fn.endswith(".html"):
                abs_path = os.path.join(root, fn)
                rel = os.path.relpath(abs_path, _APP_DIR).replace("\\", "/")
                yield rel, abs_path


def local_manifest() -> dict:
    """Return {rel_path: sha256_hex} for all local app files."""
    out = {}
    for rel, abs_path in app_files():
        with open(abs_path, "rb") as f:
            out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def safe_abs_path(rel_path: str) -> str:
    """Return abs path only if within APP_DIR and is .py/.html, else raise."""
    abs_path = os.path.normpath(os.path.join(_APP_DIR, rel_path))
    if not (abs_path.startswith(_APP_DIR + os.sep) or abs_path == _APP_DIR):
        raise ValueError("path escapes app directory")
    if not (abs_path.endswith(".py") or abs_path.endswith(".html")):
        raise ValueError("only .py and .html files are served")
    return abs_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fmt_error(exc, url: str = "") -> str:
    """Return a human-readable error string for network/IO exceptions."""
    import urllib.error
    import json as _json
    prefix = f"{url} — " if url else ""
    if isinstance(exc, urllib.error.HTTPError):
        return f"{prefix}HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, OSError):
            msg = reason.strerror or str(reason)
            return f"{prefix}Connection failed: {msg}"
        return f"{prefix}URL error: {reason}"
    if isinstance(exc, TimeoutError):
        return f"{prefix}Connection timed out"
    if isinstance(exc, _json.JSONDecodeError):
        return f"{prefix}Invalid JSON in server response"
    return f"{prefix}{type(exc).__name__}: {exc}"


def check_for_updates(host: str, port: int):
    """
    Fetch remote manifest, compare with local files, update shared state.
    Returns list of changed dicts or None on error.
    """
    import urllib.request
    import json as _json

    url = f"http://{host}:{port}/api/update/manifest"

    with _state_lock:
        _state["checking"] = True

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            remote = _json.loads(resp.read().decode())

        local = local_manifest()
        changed = []
        for entry in remote.get("files", []):
            p = entry.get("path", "")
            if not p:
                continue
            if local.get(p) != entry.get("hash"):
                changed.append({
                    "path": p,
                    "status": "modified" if p in local else "new",
                })

        with _state_lock:
            _state["available"] = bool(changed)
            _state["files"] = changed
            _state["last_check"] = time.time()
            _state["error"] = None
            _state["checking"] = False

        return changed

    except Exception as e:
        msg = _fmt_error(e, url)
        with _state_lock:
            _state["error"] = msg
            _state["checking"] = False
        log.warning("Update check failed: %s", msg)
        return None


def get_state() -> dict:
    with _state_lock:
        return dict(_state)
