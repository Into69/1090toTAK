"""
Direct HackRF receiver using the hackrf Python library.
Samples IQ data at 2 Msps on 1090 MHz, detects Mode S preambles,
extracts frames, then decodes with the same pipeline as RTLSDRReceiver.

Install the library with:  pip install hackrf
"""

import logging
import threading
import time
import numpy as np

from capabilities import HAS_HACKRF
if HAS_HACKRF:
    import hackrf as _hackrf_lib

from .rtlsdr_receiver import RTLSDRReceiver, SAMPLE_RATE, CENTER_FREQ
from .base import BaseReceiver

log = logging.getLogger(__name__)


class HackRFReceiver(RTLSDRReceiver):
    """
    Receives raw IQ from HackRF One, detects Mode S preambles,
    and decodes ADS-B via the same pipeline as RTLSDRReceiver.

    Gain model differs from RTL-SDR:
      - LNA gain: 0–40 dB in 8 dB steps
      - VGA gain: 0–62 dB in 2 dB steps
      - Amp:      0 or ~14 dB (built-in RF amplifier)
    """

    _device_label = "HackRF"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hrf = None          # active HackRF device
        self._rx_done = threading.Event()  # fired when RX loop should exit

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        BaseReceiver.stop(self)
        self._rx_done.set()
        self._cancel_hackrf()

    def reconnect(self) -> None:
        BaseReceiver.reconnect(self)
        with self._gain_lock:
            self._pending_gain = None
        self._rx_done.set()
        self._cancel_hackrf()

    def _cancel_hackrf(self) -> None:
        hrf = self._hrf
        if hrf is not None:
            try:
                hrf.stop_rx()
            except Exception:
                pass

    def run(self) -> None:
        if not HAS_HACKRF:
            log.error("hackrf library not installed. Install with: pip install hackrf")
            return
        self._reconnect_loop(self._connect, "HackRF")

    def _connect(self) -> None:
        cfg = self.config.receiver
        self._rx_done.clear()

        hrf = _hackrf_lib.HackRF(device_index=cfg.hackrf_device_index)
        hrf.sample_rate = SAMPLE_RATE
        hrf.center_freq = CENTER_FREQ
        hrf.lna_gain = cfg.hackrf_lna_gain
        hrf.vga_gain = cfg.hackrf_vga_gain
        hrf.amplifier_on = cfg.hackrf_amp
        if cfg.hackrf_ppm != 0:
            # HackRF doesn't have a native PPM correction API in all bindings;
            # adjust center frequency to compensate instead.
            correction_hz = int(CENTER_FREQ * cfg.hackrf_ppm / 1_000_000)
            hrf.center_freq = CENTER_FREQ - correction_hz

        log.info(
            "HackRF: opened device %d at %.1f MHz, LNA=%ddB, VGA=%ddB, amp=%s, ppm=%d",
            cfg.hackrf_device_index, CENTER_FREQ / 1e6,
            cfg.hackrf_lna_gain, cfg.hackrf_vga_gain,
            "on" if cfg.hackrf_amp else "off", cfg.hackrf_ppm,
        )

        self._hrf = hrf
        self.connected = True

        try:
            hrf.start_rx(self._hackrf_callback)
            # start_rx is non-blocking; block here until stop()/reconnect()
            self._rx_done.wait()
            hrf.stop_rx()

            if self.stopped() or self._reconnect_event.is_set():
                return
            raise RuntimeError("HackRF RX loop exited unexpectedly")

        except Exception as e:
            if self.stopped() or self._reconnect_event.is_set():
                return
            log.error("HackRF error: %s", e)
            raise
        finally:
            self._hrf = None
            try:
                hrf.close()
            except Exception:
                pass
            self.connected = False

    # ── RX callback ───────────────────────────────────────────────────────────

    def _hackrf_callback(self, hackrf_transfer) -> int:
        """Called from the hackrf library's background thread with each IQ buffer."""
        if self.stopped() or self._reconnect_event.is_set():
            self._rx_done.set()
            return -1   # non-zero signals the library to stop

        # Apply any queued gain changes
        with self._gain_lock:
            pending = self._pending_gain
            self._pending_gain = None
        if pending is not None:
            lna, vga, amp = pending
            try:
                self._hrf.lna_gain = lna
                self._hrf.vga_gain = vga
                self._hrf.amplifier_on = amp
                log.info("HackRF gain preview applied: LNA=%ddB VGA=%ddB amp=%s",
                         lna, vga, "on" if amp else "off")
            except Exception as e:
                log.warning("HackRF gain preview apply failed: %s", e)

        # Convert interleaved int8 I/Q bytes → complex64
        # valid_length (if present) tells us how many bytes are usable in this transfer.
        try:
            buf = hackrf_transfer.buffer
            n   = getattr(hackrf_transfer, "valid_length", len(buf))
            raw = np.frombuffer(buf, dtype=np.int8, count=n).astype(np.float32)
            i_samp = raw[0::2] / 128.0
            q_samp = raw[1::2] / 128.0
            samples = (i_samp + 1j * q_samp).astype(np.complex64)
        except Exception:
            return 0

        self._process_iq(samples)
        return 0

    # ── Gain preview ──────────────────────────────────────────────────────────

    def apply_gain_preview(self, lna: int, vga: int, amp: bool) -> bool:
        """Queue a gain change to be applied on the next callback."""
        if self._hrf is None:
            return False
        with self._gain_lock:
            self._pending_gain = (lna, vga, amp)
        log.info("HackRF gain preview queued: LNA=%ddB VGA=%ddB amp=%s",
                 lna, vga, "on" if amp else "off")
        return True

    def revert_gain_preview(self) -> bool:
        """Restore gain to the saved config values."""
        cfg = self.config.receiver
        return self.apply_gain_preview(cfg.hackrf_lna_gain, cfg.hackrf_vga_gain, cfg.hackrf_amp)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        s = BaseReceiver.status(self)
        s["sample_rate"] = SAMPLE_RATE
        s["frequency"] = CENTER_FREQ
        return s
