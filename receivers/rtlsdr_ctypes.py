"""
Built-in ctypes wrapper for librtlsdr — replaces the pyrtlsdr pip package.

Provides an RtlSdr class with the same interface used by RTLSDRReceiver:
  - RtlSdr(device_index=0)
  - .sample_rate, .center_freq, .gain (property setters)
  - .set_freq_correction(ppm)
  - .read_samples_async(callback, num_samples)
  - .cancel_read_async()
  - .close()

Requires librtlsdr to be installed on the system:
  - Windows: rtlsdr.dll on PATH or next to this file
  - Linux:   apt install librtlsdr-dev   (provides librtlsdr.so)
  - macOS:   brew install librtlsdr
"""

import ctypes
import ctypes.util
import logging
import platform
import sys
import os
import threading
import numpy as np

log = logging.getLogger(__name__)

# ── Load the native library ──────────────────────────────────────────────────

def _find_librtlsdr():
    """Locate librtlsdr shared library."""
    system = platform.system()

    # Try names in order of likelihood
    if system == "Windows":
        names = ["rtlsdr.dll", "librtlsdr.dll", "rtlsdr"]
    elif system == "Darwin":
        names = ["librtlsdr.dylib", "rtlsdr"]
    else:
        names = ["librtlsdr.so", "librtlsdr.so.0", "rtlsdr"]

    # Check directory containing this file (bundled DLL scenario)
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)
    for name in names:
        for d in (here, project_root):
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                return candidate

    # ctypes.util.find_library (searches system paths / ldconfig)
    path = ctypes.util.find_library("rtlsdr")
    if path:
        return path

    # Last resort: try loading by bare name (relies on OS search path)
    return names[0]


def _load_lib():
    path = _find_librtlsdr()
    try:
        lib = ctypes.CDLL(path)
        # Quick sanity check — this function must exist
        lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
        return lib
    except OSError as e:
        raise ImportError(
            f"Cannot load librtlsdr ({path}): {e}\n"
            "Install the library:\n"
            "  Windows: download rtlsdr.dll from https://osmocom.org/projects/rtl-sdr\n"
            "  Linux:   sudo apt install librtlsdr-dev\n"
            "  macOS:   brew install librtlsdr"
        ) from e


_lib = _load_lib()

# ── C types and function signatures ──────────────────────────────────────────

# Opaque device handle: rtlsdr_dev_t*
_p_rtlsdr_dev = ctypes.c_void_p

# Async read callback: typedef void(*rtlsdr_read_async_cb_t)(unsigned char *buf, uint32_t len, void *ctx)
_read_async_cb_t = ctypes.CFUNCTYPE(
    None,                    # return void
    ctypes.POINTER(ctypes.c_ubyte),  # buf
    ctypes.c_uint32,         # len
    ctypes.c_void_p,         # ctx
)


def _setup_signatures():
    """Declare argument/return types for the librtlsdr functions we use."""
    # rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index) -> int
    _lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
    _lib.rtlsdr_open.restype = ctypes.c_int

    # rtlsdr_close(rtlsdr_dev_t *dev) -> int
    _lib.rtlsdr_close.argtypes = [_p_rtlsdr_dev]
    _lib.rtlsdr_close.restype = ctypes.c_int

    # rtlsdr_set_sample_rate(dev, rate) -> int
    _lib.rtlsdr_set_sample_rate.argtypes = [_p_rtlsdr_dev, ctypes.c_uint32]
    _lib.rtlsdr_set_sample_rate.restype = ctypes.c_int

    # rtlsdr_set_center_freq(dev, freq) -> int
    _lib.rtlsdr_set_center_freq.argtypes = [_p_rtlsdr_dev, ctypes.c_uint32]
    _lib.rtlsdr_set_center_freq.restype = ctypes.c_int

    # rtlsdr_set_tuner_gain_mode(dev, manual) -> int
    _lib.rtlsdr_set_tuner_gain_mode.argtypes = [_p_rtlsdr_dev, ctypes.c_int]
    _lib.rtlsdr_set_tuner_gain_mode.restype = ctypes.c_int

    # rtlsdr_set_tuner_gain(dev, gain_tenths) -> int
    _lib.rtlsdr_set_tuner_gain.argtypes = [_p_rtlsdr_dev, ctypes.c_int]
    _lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int

    # rtlsdr_set_agc_mode(dev, on) -> int
    _lib.rtlsdr_set_agc_mode.argtypes = [_p_rtlsdr_dev, ctypes.c_int]
    _lib.rtlsdr_set_agc_mode.restype = ctypes.c_int

    # rtlsdr_set_freq_correction(dev, ppm) -> int
    _lib.rtlsdr_set_freq_correction.argtypes = [_p_rtlsdr_dev, ctypes.c_int]
    _lib.rtlsdr_set_freq_correction.restype = ctypes.c_int

    # rtlsdr_reset_buffer(dev) -> int
    _lib.rtlsdr_reset_buffer.argtypes = [_p_rtlsdr_dev]
    _lib.rtlsdr_reset_buffer.restype = ctypes.c_int

    # rtlsdr_read_async(dev, cb, ctx, buf_num, buf_len) -> int
    _lib.rtlsdr_read_async.argtypes = [
        _p_rtlsdr_dev, _read_async_cb_t, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32,
    ]
    _lib.rtlsdr_read_async.restype = ctypes.c_int

    # rtlsdr_cancel_async(dev) -> int
    _lib.rtlsdr_cancel_async.argtypes = [_p_rtlsdr_dev]
    _lib.rtlsdr_cancel_async.restype = ctypes.c_int

    # rtlsdr_get_tuner_gains(dev, gains_out) -> int (count)
    _lib.rtlsdr_get_tuner_gains.argtypes = [_p_rtlsdr_dev, ctypes.c_void_p]
    _lib.rtlsdr_get_tuner_gains.restype = ctypes.c_int

    # rtlsdr_get_device_count() -> uint32
    _lib.rtlsdr_get_device_count.argtypes = []
    _lib.rtlsdr_get_device_count.restype = ctypes.c_uint32

    # rtlsdr_get_device_name(index) -> const char*
    _lib.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    _lib.rtlsdr_get_device_name.restype = ctypes.c_char_p

    # rtlsdr_get_tuner_type(dev) -> int (RTLSDR_TUNER enum)
    _lib.rtlsdr_get_tuner_type.argtypes = [_p_rtlsdr_dev]
    _lib.rtlsdr_get_tuner_type.restype = ctypes.c_int

    # rtlsdr_set_bias_tee(dev, on) -> int  (only in librtlsdr >= 0.6)
    if hasattr(_lib, "rtlsdr_set_bias_tee"):
        _lib.rtlsdr_set_bias_tee.argtypes = [_p_rtlsdr_dev, ctypes.c_int]
        _lib.rtlsdr_set_bias_tee.restype = ctypes.c_int


_setup_signatures()


# RTLSDR_TUNER enum values from librtlsdr.h
TUNER_UNKNOWN = 0
TUNER_E4000   = 1
TUNER_FC0012  = 2
TUNER_FC0013  = 3
TUNER_FC2580  = 4
TUNER_R820T   = 5   # RTL-SDR Blog V3 (R820T2)
TUNER_R828D   = 6   # RTL-SDR Blog V4

_TUNER_NAMES = {
    TUNER_UNKNOWN: "unknown", TUNER_E4000: "E4000",
    TUNER_FC0012:  "FC0012",  TUNER_FC0013: "FC0013",
    TUNER_FC2580:  "FC2580",  TUNER_R820T:  "R820T/R820T2",
    TUNER_R828D:   "R828D",
}


def has_bias_tee_support() -> bool:
    """True if the loaded librtlsdr exports rtlsdr_set_bias_tee."""
    return hasattr(_lib, "rtlsdr_set_bias_tee")


def tuner_name(tuner_id: int) -> str:
    return _TUNER_NAMES.get(tuner_id, f"tuner-{tuner_id}")


def tuner_supports_bias_tee(tuner_id: int) -> bool:
    """
    Returns True if the tuner type is consistent with an RTL-SDR Blog V3/V4.
    Non-Blog dongles with the same tuner will also match — the bias tee call
    is harmless on those (it just does nothing).
    """
    return tuner_id in (TUNER_R820T, TUNER_R828D)


# ── Device release helpers ───────────────────────────────────────────────────

# Processes that typically hold an RTL-SDR device exclusively.
_RTLSDR_HOLDERS_UNIX = (
    "dump1090", "dump1090-fa", "dump1090-mutability",
    "rtl_tcp", "rtl_fm", "rtl_adsb", "rtl_433", "rtl_power", "rtl_sdr",
    "readsb",
)
_RTLSDR_HOLDERS_WIN = tuple(f"{n}.exe" for n in _RTLSDR_HOLDERS_UNIX)

# Kernel DVB modules that claim the RTL2832U on Linux and block librtlsdr.
_DVB_KMODS = ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830")


def try_release_rtlsdr() -> bool:
    """
    Best-effort: kill processes and unload kernel modules known to hold an
    RTL-SDR device, so the next rtlsdr_open() can succeed.
    Returns True if any action was taken.
    """
    import subprocess
    import sys

    acted = False
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, check=False,
            ).stdout
            wanted = {n.lower() for n in _RTLSDR_HOLDERS_WIN}
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower().lstrip('"') in wanted:
                    name = parts[0].lstrip('"')
                    try:
                        pid = int(parts[1])
                    except ValueError:
                        continue
                    log.warning("RTL-SDR: killing %s (PID %d) that holds the device", name, pid)
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, text=True, check=False,
                    )
                    acted = True
        else:
            # Kill known holder processes by name
            for name in _RTLSDR_HOLDERS_UNIX:
                rc = subprocess.run(
                    ["pkill", "-KILL", "-x", name],
                    capture_output=True, text=True, check=False,
                ).returncode
                if rc == 0:
                    log.warning("RTL-SDR: killed process '%s' that held the device", name)
                    acted = True

            # Unload kernel DVB modules that claim the RTL2832U (needs root)
            for mod in _DVB_KMODS:
                check = subprocess.run(
                    ["lsmod"], capture_output=True, text=True, check=False,
                ).stdout
                if mod not in check:
                    continue
                rc = subprocess.run(
                    ["rmmod", mod],
                    capture_output=True, text=True, check=False,
                ).returncode
                if rc == 0:
                    log.warning("RTL-SDR: unloaded kernel module '%s'", mod)
                    acted = True
                else:
                    log.warning(
                        "RTL-SDR: kernel module '%s' is loaded and blocks librtlsdr; "
                        "run 'sudo rmmod %s' (or blacklist it)", mod, mod
                    )
    except FileNotFoundError:
        # tasklist/pkill/lsmod/rmmod not present — skip silently
        pass
    except Exception as e:
        log.debug("try_release_rtlsdr: %s", e)
    return acted


# ── RtlSdr class ─────────────────────────────────────────────────────────────

class RtlSdr:
    """
    Drop-in replacement for pyrtlsdr.RtlSdr using ctypes.
    Only the subset of the API used by RTLSDRReceiver is implemented.
    """

    def __init__(self, device_index: int = 0):
        dev_p = ctypes.c_void_p()
        rc = _lib.rtlsdr_open(ctypes.byref(dev_p), ctypes.c_uint32(device_index))
        if rc != 0:
            count = _lib.rtlsdr_get_device_count()
            raise IOError(
                f"Failed to open RTL-SDR device {device_index} (rc={rc}). "
                f"Devices found: {count}"
            )
        self._dev = dev_p
        self._closed = False
        # Keep a reference to the ctypes callback so it isn't garbage-collected
        # while librtlsdr's async loop is still running.
        self._async_cb_ref = None
        self._user_callback = None

    def close(self):
        if not self._closed and self._dev:
            _lib.rtlsdr_close(self._dev)
            self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def sample_rate(self):
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, rate: int):
        rc = _lib.rtlsdr_set_sample_rate(self._dev, ctypes.c_uint32(int(rate)))
        if rc != 0:
            raise IOError(f"rtlsdr_set_sample_rate failed (rc={rc})")
        self._sample_rate = int(rate)

    @property
    def center_freq(self):
        return self._center_freq

    @center_freq.setter
    def center_freq(self, freq: int):
        rc = _lib.rtlsdr_set_center_freq(self._dev, ctypes.c_uint32(int(freq)))
        if rc != 0:
            raise IOError(f"rtlsdr_set_center_freq failed (rc={rc})")
        self._center_freq = int(freq)

    @property
    def gain(self):
        return self._gain

    @gain.setter
    def gain(self, value):
        """
        Set tuner gain.
        - 'auto': enable AGC
        - float/int: manual gain in dB (will be snapped to nearest supported value)
        """
        if isinstance(value, str) and value.lower() == "auto":
            rc1 = _lib.rtlsdr_set_tuner_gain_mode(self._dev, 0)   # 0 = auto
            rc2 = _lib.rtlsdr_set_agc_mode(self._dev, 1)           # enable digital AGC
            if rc1 != 0 or rc2 != 0:
                log.warning("RTL-SDR: auto-gain setup returned rc=%d/%d", rc1, rc2)
            self._gain = "auto"
        else:
            gain_db = float(value)
            rc1 = _lib.rtlsdr_set_tuner_gain_mode(self._dev, 1)   # 1 = manual
            rc2 = _lib.rtlsdr_set_agc_mode(self._dev, 0)
            gain_tenths = int(round(gain_db * 10))
            rc3 = _lib.rtlsdr_set_tuner_gain(self._dev, ctypes.c_int(gain_tenths))
            if rc1 != 0 or rc2 != 0 or rc3 != 0:
                log.warning("RTL-SDR: manual-gain setup returned rc=%d/%d/%d (requested %.1f dB)",
                            rc1, rc2, rc3, gain_db)
            self._gain = gain_db

    # ── Tuner type / bias tee ────────────────────────────────────────────────

    def get_tuner_type(self) -> int:
        """Return the RTLSDR_TUNER_* enum value for the opened device."""
        try:
            return int(_lib.rtlsdr_get_tuner_type(self._dev))
        except Exception:
            return TUNER_UNKNOWN

    def set_bias_tee(self, on: bool) -> bool:
        """
        Enable/disable the bias tee (powers an external LNA over the coax).
        Only effective on RTL-SDR Blog V3/V4 dongles; a no-op on others.
        Returns True on success.
        """
        if not has_bias_tee_support():
            return False
        rc = _lib.rtlsdr_set_bias_tee(self._dev, ctypes.c_int(1 if on else 0))
        return rc == 0

    # ── Frequency correction ─────────────────────────────────────────────────

    def set_freq_correction(self, ppm: int):
        rc = _lib.rtlsdr_set_freq_correction(self._dev, ctypes.c_int(int(ppm)))
        if rc != 0 and rc != -2:
            # rc=-2 means "same correction already set" — not a real error
            raise IOError(f"rtlsdr_set_freq_correction failed (rc={rc})")

    # ── Async read ───────────────────────────────────────────────────────────

    def read_samples_async(self, callback, num_samples: int = 131072):
        """
        Start the async read loop (blocks until cancel_read_async is called).

        `callback(samples, context)` receives a complex64 numpy array of IQ
        samples, matching the pyrtlsdr interface.
        """
        # Reset the internal buffer before starting
        _lib.rtlsdr_reset_buffer(self._dev)

        self._user_callback = callback
        # buf_len in bytes: each IQ sample is 2 bytes (I + Q as uint8)
        buf_len = num_samples * 2

        def _raw_cb(buf_ptr, length, _ctx):
            try:
                n = int(length)
                if n == 0:
                    return
                # Wrap the C buffer as a numpy uint8 array (pyrtlsdr idiom).
                # np.ctypeslib.as_array handles POINTER(c_ubyte) correctly;
                # addressof(buf_ptr.contents) does NOT reliably return the
                # underlying buffer address across ctypes versions.
                raw = np.ctypeslib.as_array(buf_ptr, shape=(n,)).copy()
                # Convert interleaved uint8 I/Q → complex64 (same as pyrtlsdr)
                iq = (raw.astype(np.float32) - 127.5) / 127.5
                samples = (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)
                self._user_callback(samples, None)
            except Exception:
                log.exception("RTL-SDR async callback error")

        # Wrap in a ctypes callback and prevent GC
        self._async_cb_ref = _read_async_cb_t(_raw_cb)

        # This call blocks until rtlsdr_cancel_async() is called
        rc = _lib.rtlsdr_read_async(
            self._dev, self._async_cb_ref, None,
            ctypes.c_uint32(0),          # buf_num=0 → default (15)
            ctypes.c_uint32(buf_len),
        )
        self._async_cb_ref = None
        self._user_callback = None
        if rc != 0:
            raise IOError(f"rtlsdr_read_async returned {rc}")

    def cancel_read_async(self):
        """Signal the async read loop to stop."""
        if not self._closed and self._dev:
            _lib.rtlsdr_cancel_async(self._dev)
