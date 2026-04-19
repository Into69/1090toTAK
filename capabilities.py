"""
Runtime capability detection.
Imported early so every module can rely on a single source of truth.

pyModeS is now OPTIONAL — the AVR receiver falls back to the built-in
adsb_decoder when it is not available.  Only the RTL-SDR receiver still
benefits from pyModeS being present.
"""

try:
    import pyModeS  # noqa: F401
    from pyModeS import adsb  # noqa: F401  — verify the submodule we actually use
    HAS_PYMODES = True
except (ImportError, Exception):
    HAS_PYMODES = False

try:
    from receivers.rtlsdr_ctypes import RtlSdr  # noqa: F401 — built-in ctypes wrapper
    HAS_RTLSDR = True
except (ImportError, OSError):
    # Falls back to pyrtlsdr pip package if librtlsdr is not installed
    try:
        from rtlsdr import RtlSdr  # noqa: F401
        HAS_RTLSDR = True
    except ImportError:
        HAS_RTLSDR = False

def probe_gpsd(host: str = "127.0.0.1", port: int = 2947, timeout: float = 1.0) -> bool:
    """Return True if a gpsd socket is reachable at host:port."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
