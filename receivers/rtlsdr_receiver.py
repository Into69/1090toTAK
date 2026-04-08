"""
Direct RTL-SDR receiver using pyrtlsdr.
Samples IQ data, detects Mode S preambles, extracts frames,
then decodes with pyModeS (same pipeline as AVRReceiver).

This is the most resource-intensive source; prefer SBS/AVR if dump1090
is already running.
"""

import logging
import threading
import numpy as np

from capabilities import HAS_PYMODES, HAS_RTLSDR
if HAS_RTLSDR:
    from rtlsdr import RtlSdr
if HAS_PYMODES:
    import pyModeS as pms
    from pyModeS import adsb

from .base import BaseReceiver
from .avr_receiver import AVRReceiver

log = logging.getLogger(__name__)

SAMPLE_RATE = 2_000_000
CENTER_FREQ = 1_090_000_000
SAMPLES_PER_BIT = int(SAMPLE_RATE / 1_000_000)  # 2 samples per µs
PREAMBLE_SAMPLES = 8 * SAMPLES_PER_BIT           # 8 µs preamble (Mode S standard)

SPECTRUM_FFT_SIZE = 4096   # samples per FFT frame
SPECTRUM_BINS     = 256    # output bins sent to UI


class RTLSDRReceiver(BaseReceiver):
    """
    Receives raw IQ from RTL-SDR, detects Mode S preambles,
    and decodes ADS-B via pyModeS. Inherits AVR frame parsing logic.
    """

    _device_label = "RTL-SDR"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reuse AVRReceiver's CPR buffer and frame parser
        self._avr = AVRReceiver(*args, **kwargs)
        # Expose frame_sink so ServerManager can wire AVR output server
        self.frame_sink = None
        # Latest spectrum: list of SPECTRUM_BINS dBFS floats (or empty)
        self.spectrum: list = []
        # Internal numpy array for EMA (avoids list→numpy conversion each callback)
        self._spectrum_arr = np.zeros(SPECTRUM_BINS, dtype=np.float32)
        self._spectrum_valid = False
        # Pre-computed FFT window (avoids re-allocating each callback)
        self._fft_window = np.hanning(SPECTRUM_FFT_SIZE).astype(np.float32)
        # Pre-computed bit offsets for demodulation (112 bits × SAMPLES_PER_BIT)
        self._bit_offsets = np.arange(112, dtype=np.intp) * SAMPLES_PER_BIT
        # Active SDR device — set during _connect so stop/reconnect can cancel it
        self._sdr = None
        # Signal quality — updated every callback, read by events loop
        # status: "ok" | "overload" | "weak"
        self.signal_quality: dict = {"status": "ok", "clip_pct": 0.0, "noise_floor_dbfs": 0.0}
        # Pending gain preview — set by apply_gain_preview(), applied in the
        # callback so the gain change is always made from the receiver thread.
        self._pending_gain = None   # (agc: bool, gain: float) | None
        self._gain_lock    = threading.Lock()

    def stop(self) -> None:
        super().stop()
        self._cancel_sdr()

    def reconnect(self) -> None:
        super().reconnect()
        with self._gain_lock:
            self._pending_gain = None
        self._cancel_sdr()

    def _cancel_sdr(self) -> None:
        sdr = self._sdr
        if sdr is not None:
            try:
                sdr.cancel_read_async()
            except Exception:
                pass

    def run(self) -> None:
        if not HAS_RTLSDR:
            log.error("pyrtlsdr not installed. Install with: pip install pyrtlsdr")
            return
        self._reconnect_loop(self._connect, "RTL-SDR")

    def _connect(self) -> None:
        cfg = self.config.receiver
        sdr = RtlSdr(device_index=cfg.rtlsdr_device_index)
        sdr.sample_rate = SAMPLE_RATE
        sdr.center_freq = CENTER_FREQ
        sdr.gain = 'auto' if cfg.rtlsdr_agc else cfg.rtlsdr_gain
        if cfg.rtlsdr_ppm != 0:
            sdr.set_freq_correction(cfg.rtlsdr_ppm)

        gain_str = "auto (AGC)" if cfg.rtlsdr_agc else f"{cfg.rtlsdr_gain:.1f} dB"
        log.info("RTL-SDR: opened device %d at %.1f MHz, gain=%s, ppm=%d",
                 cfg.rtlsdr_device_index, CENTER_FREQ / 1e6, gain_str, cfg.rtlsdr_ppm)
        self._sdr = sdr
        self.connected = True
        try:
            sdr.read_samples_async(self._samples_callback, num_samples=131072)
            # read_samples_async returns after cancel_read_async() is called —
            # check whether it was a deliberate stop/reconnect or an unexpected exit.
            if self.stopped() or self._reconnect_event.is_set():
                return  # clean exit — no error count
            # Unexpected silent exit (USB dropout, buffer overflow, etc.) — let
            # _reconnect_loop handle backoff by re-raising.
            raise RuntimeError("read_samples_async exited unexpectedly")
        except Exception as e:
            if self.stopped() or self._reconnect_event.is_set():
                return  # deliberate — not a real error
            log.error("RTL-SDR error: %s", e)
            raise
        finally:
            self._sdr = None
            try:
                sdr.close()
            except Exception:
                pass
            self.connected = False

    def _samples_callback(self, samples, _context) -> None:
        if self.stopped() or self._reconnect_event.is_set():
            return

        # Apply any queued gain-preview change from the receiver thread so
        # we never call pyrtlsdr/librtlsdr from an unrelated HTTP thread.
        with self._gain_lock:
            pending = self._pending_gain
            self._pending_gain = None
        if pending is not None:
            agc, gain = pending
            try:
                self._sdr.gain = "auto" if agc else gain
                log.info("RTL-SDR gain preview applied: %s",
                         "AGC" if agc else f"{gain:.1f} dB")
            except Exception as e:
                log.warning("RTL-SDR gain preview apply failed: %s", e)

        self._process_iq(samples)

    def _process_iq(self, samples) -> None:
        """Process a buffer of complex IQ samples (shared with HackRFReceiver)."""
        # Spectrum: FFT of the first SPECTRUM_FFT_SIZE samples in this chunk
        if len(samples) >= SPECTRUM_FFT_SIZE:
            chunk = samples[:SPECTRUM_FFT_SIZE]
            fft_out = np.fft.fftshift(np.fft.fft(chunk * self._fft_window))
            power = (np.abs(fft_out) / SPECTRUM_FFT_SIZE) ** 2
            db = (10.0 * np.log10(power + 1e-12)).astype(np.float32)
            # Downsample to SPECTRUM_BINS via max-pooling
            factor = SPECTRUM_FFT_SIZE // SPECTRUM_BINS
            db_bins = db[:factor * SPECTRUM_BINS].reshape(SPECTRUM_BINS, factor).max(axis=1)
            # Exponential moving average for smooth display
            if self._spectrum_valid:
                self._spectrum_arr = 0.4 * db_bins + 0.6 * self._spectrum_arr
            else:
                self._spectrum_arr = db_bins.copy()
                self._spectrum_valid = True
            self.spectrum = self._spectrum_arr.tolist()

        # Compute magnitude once — reused for preamble detection and signal quality
        mag = np.abs(samples).astype(np.float32)

        frames = self._detect_frames(mag)

        if frames:
            log.debug("%s: %d valid frame(s) decoded", self._device_label, len(frames))

        for hex_str in frames:
            self._avr._parse_avr(f"*{hex_str};")
            self.message_count += 1
            if self.frame_sink:
                try:
                    self.frame_sink(hex_str)
                except Exception:
                    pass

        # ── Signal quality monitoring ─────────────────────────────────────────
        # Check real/imaginary component clipping (cheaper than re-computing abs)
        clip_count = int(np.sum(
            (np.abs(samples.real) > 0.95) | (np.abs(samples.imag) > 0.95)
        ))
        clip_pct = clip_count / len(samples) * 100.0
        noise_floor_dbfs = float(20.0 * np.log10(np.median(mag) + 1e-12))

        if clip_pct > 1.0:
            sq_status = "overload"
        elif noise_floor_dbfs < -35.0:
            sq_status = "weak"
        else:
            sq_status = "ok"

        self.signal_quality = {
            "status": sq_status,
            "clip_pct": round(clip_pct, 1),
            "noise_floor_dbfs": round(noise_floor_dbfs, 1),
        }

    def _detect_frames(self, mag: np.ndarray) -> list:
        """
        Scan magnitude array for Mode S preambles and return a list of hex
        strings for every frame that passes the 24-bit CRC check.

        Preamble detection uses vectorized numpy operations to find candidate
        positions via relative amplitude comparisons (the dump1090 approach),
        then demodulates and CRC-checks only at those candidates.

        Both timing offsets 0 and ±1 are tried for each candidate preamble,
        and single-bit error correction is attempted before discarding a frame.
        """
        from . import adsb_decoder as _dec
        frames = []
        n = len(mag)
        min_frame = PREAMBLE_SAMPLES + 112 * SAMPLES_PER_BIT
        max_i = n - min_frame
        if max_i <= 0:
            return frames

        # Vectorized preamble shape detection — compare all positions at once.
        # Mode S preamble at 2 Msps: HIGH at samples 0,2,7,9; LOW at 1,3-6,8.
        m0 = mag[0:max_i]
        m1 = mag[1:max_i + 1]
        m2 = mag[2:max_i + 2]
        m3 = mag[3:max_i + 3]
        m4 = mag[4:max_i + 4]
        m5 = mag[5:max_i + 5]
        m6 = mag[6:max_i + 6]
        m7 = mag[7:max_i + 7]
        m8 = mag[8:max_i + 8]
        m9 = mag[9:max_i + 9]

        mask = (
            (m0 > m1) & (m1 < m2) & (m2 > m3) &
            (m3 < m0) & (m4 < m0) & (m5 < m0) & (m6 < m0) &
            (m7 > m8) & (m8 < m9) & (m9 > m6)
        )

        # SNR quality gate: HIGH mean ≥ 1.1× LOW mean
        high = (m0 + m2 + m7 + m9) * 0.25
        low = (m1 + m3 + m4 + m5 + m6 + m8) / 6.0
        mask &= (high >= low * 1.1)

        # Mid-point thresholds for demodulation
        mid_arr = (high + low) * 0.5

        candidates = np.flatnonzero(mask)

        last_accepted = -min_frame
        for pos in candidates:
            pos = int(pos)
            if pos < last_accepted + min_frame:
                continue
            mid = float(mid_arr[pos])
            frame_start = pos + PREAMBLE_SAMPLES
            accepted = False
            for offset in (-1, 0, 1):
                dpos = frame_start + offset
                if dpos < 0 or dpos + 112 * SAMPLES_PER_BIT > n:
                    continue
                hex_str = self._demodulate_hex(mag, dpos, mid)
                if not hex_str:
                    continue
                if _dec.crc_ok(hex_str):
                    frames.append(hex_str)
                    accepted = True
                    break
                # Single-bit error correction
                corrected = _dec.fix_single_bit(hex_str)
                if corrected:
                    frames.append(corrected)
                    accepted = True
                    break
            if accepted:
                last_accepted = pos
        return frames

    def _demodulate_hex(self, mag: np.ndarray, start: int, mid: float) -> str:
        """
        PPM demodulate 112 bits starting at `start` using vectorized numpy.
        Uses a per-frame mid-point threshold derived from preamble SNR for
        confident decisions; falls back to direct sample comparison for
        ambiguous bits near the threshold.

        Mode S bit '1': first-half sample high, second-half low.
        Mode S bit '0': first-half sample low, second-half high.
        """
        end = start + 112 * SAMPLES_PER_BIT
        if end > len(mag):
            return ""
        base = self._bit_offsets + start
        s0 = mag[base]
        s1 = mag[base + 1]
        # Vectorized bit decisions
        bits = np.where(
            (s0 >= mid) & (s1 < mid), np.uint8(1),
            np.where(
                (s0 < mid) & (s1 >= mid), np.uint8(0),
                np.where(s0 > s1, np.uint8(1), np.uint8(0))
            )
        )
        # Pack to hex: pad 112 bits to 128, use np.packbits, take first 14 bytes
        padded = np.zeros(128, dtype=np.uint8)
        padded[:112] = bits
        packed = np.packbits(padded)[:14]
        return ''.join(format(b, '02X') for b in packed)

    def apply_gain_preview(self, agc: bool, gain: float) -> bool:
        """
        Queue a gain change to be applied on the next callback invocation.
        The change is made from the receiver thread (inside read_samples_async)
        so we never call pyrtlsdr/librtlsdr from a foreign HTTP thread.
        Returns True if the device is open and the request was queued.
        """
        if self._sdr is None:
            return False
        with self._gain_lock:
            self._pending_gain = (agc, gain)
        log.info("RTL-SDR gain preview queued: %s",
                 "AGC" if agc else f"{gain:.1f} dB")
        return True

    def revert_gain_preview(self) -> bool:
        """Restore the gain values stored in config (the last-saved settings)."""
        cfg = self.config.receiver
        return self.apply_gain_preview(cfg.rtlsdr_agc, cfg.rtlsdr_gain)

    def status(self) -> dict:
        s = super().status()
        s["sample_rate"] = SAMPLE_RATE
        s["frequency"] = CENTER_FREQ
        return s
