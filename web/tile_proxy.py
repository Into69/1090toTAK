"""
Server-side tile proxy with disk cache.

Tiles are fetched from upstream providers and stored under:
  tile_cache/<source>/<z>/<x>/<y>.<ext>

The cache is permanent (no TTL) — map tiles don't change.
"""

import itertools
import logging
import os
import threading
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

# Root directory for cached tiles (next to main.py)
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "tile_cache")

# Round-robin counter for Google subdomain rotation (thread-safe)
_google_counter = itertools.cycle(range(4))
_google_lock = threading.Lock()

def _next_google_sub():
    with _google_lock:
        return next(_google_counter)

# Map source name → callable(z, x, y) → (url, headers)
def _upstream(source: str, z: int, x: int, y: int):
    s = {
        "osm":            (f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                           {"Referer": "https://www.openstreetmap.org/"}),
        "satellite":      (f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {}),
        "dark":           (f"https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", {}),
        "topo":           (f"https://a.tile.opentopomap.org/{z}/{x}/{y}.png", {}),
        "google_hybrid":  (f"https://mt{_next_google_sub()}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", {}),
        "google_roads":   (f"https://mt{_next_google_sub()}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}", {}),
        "google_terrain": (f"https://mt{_next_google_sub()}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}", {}),
        "esri_street":    (f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {}),
        "esri_topo":      (f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", {}),
        "esri_natgeo":    (f"https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}", {}),
    }.get(source)
    return s  # None if unknown source

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; 1090toTAK/1.0)",
}

_EXT_FOR_CT = {
    "image/png":  "png",
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/webp": "webp",
}


def _cache_path(source: str, z: int, x: int, y: int, ext: str) -> str:
    return os.path.join(CACHE_DIR, source, str(z), str(x), f"{y}.{ext}")


def _find_cached(source: str, z: int, x: int, y: int):
    """Return (path, ext) if any cached variant exists, else (None, None)."""
    base = os.path.join(CACHE_DIR, source, str(z), str(x))
    for ext in ("png", "jpg", "webp"):
        p = os.path.join(base, f"{y}.{ext}")
        if os.path.exists(p):
            return p, ext
    return None, None


def fetch_tile(source: str, z: int, x: int, y: int):
    """
    Return (bytes, content_type) for the requested tile, using disk cache.
    Raises ValueError for unknown sources, requests.RequestException on fetch failure.
    """
    # Validate z/x/y ranges to prevent path traversal
    if not (0 <= z <= 22 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise ValueError(f"Invalid tile coordinates: z={z} x={x} y={y}")

    cached_path, cached_ext = _find_cached(source, z, x, y)
    if cached_path:
        ct = f"image/{cached_ext}" if cached_ext != "jpg" else "image/jpeg"
        with open(cached_path, "rb") as f:
            return f.read(), ct

    upstream = _upstream(source, z, x, y)
    if upstream is None:
        raise ValueError(f"Unknown tile source: {source!r}")

    url, extra_headers = upstream
    headers = {**_HEADERS, **extra_headers}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
        ct = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()

    ext = _EXT_FOR_CT.get(ct, "png")
    path = _cache_path(source, z, x, y, ext)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

    return data, ct


def cache_stats() -> dict:
    """Return total tile count and bytes in the disk cache."""
    total_bytes = 0
    total_tiles = 0
    if not os.path.isdir(CACHE_DIR):
        return {"tiles": 0, "bytes": 0}
    for dirpath, _dirs, files in os.walk(CACHE_DIR):
        for fname in files:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, fname))
                total_tiles += 1
            except OSError:
                pass
    return {"tiles": total_tiles, "bytes": total_bytes}


def clear_cache() -> dict:
    """Delete all cached tiles. Returns stats before deletion."""
    import shutil
    stats = cache_stats()
    if os.path.isdir(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    return stats
