"""
Direct RTL-SDR receiver using built-in ctypes wrapper (or pyrtlsdr fallback).
Samples IQ data, detects Mode S preambles, extracts frames,
then decodes with pyModeS (same pipeline as AVRReceiver).

This is the most resource-intensive source; prefer SBS/AVR if dump1090
is already running.
"""

import logging
import math
import threading
import time
import numpy as np

from capabilities import HAS_PYMODES, HAS_RTLSDR
if HAS_RTLSDR:
    try:
        from .rtlsdr_ctypes import RtlSdr      # built-in ctypes wrapper (no pip dep)
    except (ImportError, OSError):
        from rtlsdr import RtlSdr              # fallback to pyrtlsdr package
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
        # Tuner detection (populated on connect)
        self.tuner_id: int = 0
        self.bias_tee_supported: bool = False
        # Signal quality — updated every callback, read by events loop
        # status: "ok" | "overload" | "weak"
        self.signal_quality: dict = {"status": "ok", "clip_pct": 0.0, "noise_floor_dbfs": 0.0}
        # Pending gain preview — set by apply_gain_preview(), applied in the
        # callback so the gain change is always made from the receiver thread.
        self._pending_gain = None   # (agc: bool, gain: float) | None
        self._gain_lock    = threading.Lock()
        # Heartbeat stats — logged every _hb_interval seconds
        self._hb_last     = time.monotonic()
        self._hb_buffers  = 0
        self._hb_frames   = 0
        self._hb_interval = 5.0

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
            log.error("librtlsdr not found. Install: apt install librtlsdr-dev / brew install librtlsdr / place rtlsdr.dll on PATH")
            return
        self._reconnect_loop(self._connect, "RTL-SDR")

    def _connect(self) -> None:
        cfg = self.config.receiver
        try:
            sdr = RtlSdr(device_index=cfg.rtlsdr_device_index)
        except (IOError, OSError) as e:
            log.warning("RTL-SDR open failed (%s) — attempting to release device", e)
            try:
                from .rtlsdr_ctypes import try_release_rtlsdr
                if try_release_rtlsdr():
                    import time
                    time.sleep(0.5)
            except Exception:
                pass
            sdr = RtlSdr(device_index=cfg.rtlsdr_device_index)
        sdr.sample_rate = SAMPLE_RATE
        sdr.center_freq = CENTER_FREQ
        sdr.gain = 'auto' if cfg.rtlsdr_agc else cfg.rtlsdr_gain
        if cfg.rtlsdr_ppm != 0:
            sdr.set_freq_correction(cfg.rtlsdr_ppm)

        # Tuner detection + bias tee (V3/V4 only; harmless elsewhere)
        bias_applied = False
        try:
            from .rtlsdr_ctypes import (
                tuner_name, tuner_supports_bias_tee, has_bias_tee_support,
            )
            self.tuner_id = sdr.get_tuner_type()
            self.bias_tee_supported = (
                has_bias_tee_support() and tuner_supports_bias_tee(self.tuner_id)
            )
            if cfg.rtlsdr_bias_tee and self.bias_tee_supported:
                bias_applied = sdr.set_bias_tee(True)
            tuner_label = tuner_name(self.tuner_id)
        except Exception:
            tuner_label = "unknown"
            self.tuner_id = 0
            self.bias_tee_supported = False

        gain_str = "auto (AGC)" if cfg.rtlsdr_agc else f"{cfg.rtlsdr_gain:.1f} dB"
        bias_str = ""
        if self.bias_tee_supported:
            if cfg.rtlsdr_bias_tee:
                bias_str = ", bias-tee=ON" if bias_applied else ", bias-tee=FAILED"
            else:
                bias_str = ", bias-tee=off"
        log.info("RTL-SDR: opened device %d (tuner=%s) at %.1f MHz, gain=%s, ppm=%d%s",
                 cfg.rtlsdr_device_index, tuner_label,
                 CENTER_FREQ / 1e6, gain_str, cfg.rtlsdr_ppm, bias_str)
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
        """Process a buffer of complex IQ samples."""
        # DC block — RTL-SDR zero-IF leaks a tuner DC component that biases
        # magnitude, drowns preamble detection in false positives, and
        # produces a fat spike in the centre of the spectrum display.
        # Applied before the spectrum FFT so the waterfall is also clean.
        samples = samples - np.mean(samples)

        # Spectrum: FFT of the first SPECTRUM_FFT_SIZE samples in this chunk
        if len(samples) >= SPECTRUM_FFT_SIZE:
            chunk = samples[:SPECTRUM_FFT_SIZE]
            fft_out = np.fft.fftshift(np.fft.fft(chunk * self._fft_window))
            power = (np.abs(fft_out) / SPECTRUM_FFT_SIZE) ** 2
            # Notch the residual DC bin (and its two neighbours) by replacing
            # them with the average of nearby bins. The per-buffer mean
            # subtraction above kills *average* DC, but tuner DC drift within
            # the buffer leaves a thin spike at the centre that we cover here.
            dc_bin = SPECTRUM_FFT_SIZE // 2
            ref = 0.5 * (power[dc_bin - 4] + power[dc_bin + 4])
            power[dc_bin - 1:dc_bin + 2] = ref
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
        # Classify by SNR (peak vs noise) — gain-independent and matches what
        # other ADS-B tools display. Absolute noise floor varies wildly with
        # gain, sample-rate, and DC removal, so it's a poor proxy on its own.
        clip_count = int(np.sum(
            (np.abs(samples.real) > 0.95) | (np.abs(samples.imag) > 0.95)
        ))
        clip_pct = clip_count / len(samples) * 100.0
        noise_mag = float(np.median(mag))
        peak_mag  = float(mag.max())
        noise_floor_dbfs = 20.0 * math.log10(noise_mag + 1e-12)
        peak_dbfs        = 20.0 * math.log10(peak_mag  + 1e-12)
        snr_db           = peak_dbfs - noise_floor_dbfs

        if clip_pct > 1.0:
            sq_status = "overload"
        elif snr_db < 10.0:
            sq_status = "weak"
        else:
            sq_status = "ok"

        self.signal_quality = {
            "status": sq_status,
            "clip_pct": round(clip_pct, 1),
            "noise_floor_dbfs": round(noise_floor_dbfs, 1),
            "peak_dbfs": round(peak_dbfs, 1),
            "snr_db": round(snr_db, 1),
        }

        # ── Heartbeat ─────────────────────────────────────────────────────────
        self._hb_buffers += 1
        self._hb_frames  += len(frames)
        now = time.monotonic()
        elapsed = now - self._hb_last
        if elapsed >= self._hb_interval:
            log.debug(
                "RTL-SDR: %d buf/s, %d frame/s, %s (noise=%.1f dBFS, clip=%.1f%%)",
                int(round(self._hb_buffers / elapsed)),
                int(round(self._hb_frames / elapsed)),
                sq_status, noise_floor_dbfs, clip_pct,
            )
            self._hb_buffers = 0
            self._hb_frames  = 0
            self._hb_last    = now

    def _detect_frames(self, mag: np.ndarray) -> list:
        """
        Scan magnitude array for Mode S preambles and return a list of hex
        strings for every frame that passes the 24-bit CRC check.

        Detection combines:
          * Correlation score against the ideal 16-sample preamble template
            (pulses at 0,2,7,9; quiet zones everywhere else). This is more
            sensitive to weak preambles than a pure relative-amplitude test.
          * Per-candidate adaptive noise floor from the gap samples, giving
            a variable SNR requirement (stays strict where noise is high,
            relaxes where the signal is clean).

        For each surviving candidate, demodulation is attempted at timing
        offsets -1/0/+1 samples; CRC is checked, then single- and two-bit
        error correction as fallbacks.
        """
        from . import adsb_decoder as _dec
        frames = []
        n = len(mag)
        min_frame = PREAMBLE_SAMPLES + 112 * SAMPLES_PER_BIT
        max_i = n - min_frame
        if max_i <= 0:
            return frames

        # Mode S preamble at 2 Msps: HIGH at samples 0,2,7,9; LOW at the rest.
        m0  = mag[0:max_i]
        m1  = mag[1:max_i + 1]
        m2  = mag[2:max_i + 2]
        m3  = mag[3:max_i + 3]
        m4  = mag[4:max_i + 4]
        m5  = mag[5:max_i + 5]
        m6  = mag[6:max_i + 6]
        m7  = mag[7:max_i + 7]
        m8  = mag[8:max_i + 8]
        m9  = mag[9:max_i + 9]
        m11 = mag[11:max_i + 11]
        m12 = mag[12:max_i + 12]
        m13 = mag[13:max_i + 13]
        m14 = mag[14:max_i + 14]

        # Average pulse level and adaptive noise (mean of quiet-zone samples).
        # Ten "low" samples: 1,3,4,5,6,8,11,12,13,14. We omit sample 10 which
        # is the transition point between preamble and first data bit.
        high  = (m0 + m2 + m7 + m9) * 0.25
        noise = (m1 + m3 + m4 + m5 + m6 + m8 + m11 + m12 + m13 + m14) * 0.1

        # Coarse shape filter — cheap relative checks that every real preamble
        # must satisfy. Eliminates ~99% of positions without any arithmetic on
        # the noise estimate, keeping the per-candidate work small.
        mask = (
            (m0 > m1) & (m0 > m3) &
            (m2 > m1) & (m2 > m3) &
            (m7 > m6) & (m7 > m8) &
            (m9 > m8) & (m9 > m6)
        )

        # Correlation-based SNR gate. The ideal preamble has pulses at 4 of
        # the 14 examined positions. Require the pulse level to clearly
        # exceed the adaptive noise estimate (≥ 2.5×, i.e. ~8 dB), and
        # require every quiet-zone sample to sit below the pulse midpoint.
        # The 2.5× ratio is weaker than dump1090's hard-coded /6 rule and
        # pulls in weaker frames, but the per-candidate noise baseline keeps
        # the false-positive rate bounded.
        mid = high * 0.5
        mask &= (high >= noise * 2.5)
        mask &= (m1 < mid) & (m3 < mid) & (m4 < mid) & (m5 < mid) & (m6 < mid) & (m8 < mid)
        mask &= (m11 < mid) & (m12 < mid) & (m13 < mid) & (m14 < mid)

        # Per-candidate mid threshold for demod: halfway between pulse and noise
        mid_arr = (high + noise) * 0.5

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
                corrected = _dec.fix_single_bit(hex_str)
                if corrected:
                    frames.append(corrected)
                    accepted = True
                    break
                corrected = _dec.fix_two_bit(hex_str)
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
        Dump1090-style direct comparison: s0 > s1 → '1', else '0'. This is
        more robust than a global mid-threshold for weak/strong signal mix.

        Mode S bit '1': first-half sample high, second-half low.
        Mode S bit '0': first-half sample low, second-half high.
        """
        end = start + 112 * SAMPLES_PER_BIT
        if end > len(mag):
            return ""
        base = self._bit_offsets + start
        s0 = mag[base]
        s1 = mag[base + 1]
        bits = (s0 > s1).astype(np.uint8)
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
        s["tuner_id"] = self.tuner_id
        s["bias_tee_supported"] = self.bias_tee_supported
        return s
