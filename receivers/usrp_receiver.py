"""
Direct USRP B205mini / B206mini receiver using Ettus Research's UHD Python library.
Samples IQ data at 2 Msps on 1090 MHz, detects Mode S preambles,
extracts frames, then decodes with the same pipeline as RTLSDRReceiver.

The UHD Python bindings are typically installed via:
  - Ubuntu/Debian: sudo apt install python3-uhd
  - Conda:         conda install -c conda-forge uhd
  - PyPI:          pip install uhd  (wraps the system UHD library)
"""

import logging
import threading
import numpy as np

from capabilities import HAS_UHD
if HAS_UHD:
    import uhd
    # TuneRequest moved between UHD binding versions; handle both.
    try:
        _TuneRequest = uhd.types.TuneRequest
    except AttributeError:
        _TuneRequest = uhd.libpyuhd.types.tune_request

from .rtlsdr_receiver import RTLSDRReceiver, SAMPLE_RATE, CENTER_FREQ
from .base import BaseReceiver

log = logging.getLogger(__name__)

# Samples per recv() call — ~10 ms of data at 2 Msps
_SAMPS_PER_RECV = 20_000


class USRPReceiver(RTLSDRReceiver):
    """
    Receives raw IQ from a USRP B205mini, B206mini, or any B2xx device,
    detects Mode S preambles, and decodes ADS-B via the same pipeline as
    RTLSDRReceiver.

    Gain model: single unified RX gain 0–76 dB (continuous, no step constraint).
    """

    _device_label = "USRP"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usrp        = None
        self._rx_streamer = None
        self._stop_rx     = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        BaseReceiver.stop(self)
        self._stop_rx.set()

    def reconnect(self) -> None:
        BaseReceiver.reconnect(self)
        with self._gain_lock:
            self._pending_gain = None
        self._stop_rx.set()

    def run(self) -> None:
        if not HAS_UHD:
            log.error(
                "uhd library not installed — USRP receiver unavailable. "
                "Install with: sudo apt install python3-uhd  or  pip install uhd"
            )
            return
        self._reconnect_loop(self._connect, "USRP")

    def _connect(self) -> None:
        cfg = self.config.receiver
        self._stop_rx.clear()

        # Build device args — filter for B2xx; include serial if configured.
        args = "type=b200"
        if cfg.usrp_serial.strip():
            args += f",serial={cfg.usrp_serial.strip()}"

        usrp = uhd.usrp.MultiUSRP(args)

        # Configure RX chain for channel 0
        usrp.set_rx_rate(SAMPLE_RATE, 0)

        # Apply PPM correction by offsetting the tune frequency
        if cfg.usrp_ppm != 0:
            correction_hz = int(CENTER_FREQ * cfg.usrp_ppm / 1_000_000)
            tune_freq = CENTER_FREQ - correction_hz
        else:
            tune_freq = CENTER_FREQ
        usrp.set_rx_freq(_TuneRequest(tune_freq), 0)

        usrp.set_rx_gain(cfg.usrp_gain, 0)
        usrp.set_rx_antenna(cfg.usrp_antenna, 0)

        log.info(
            "USRP: opened (%s) at %.1f MHz, gain=%.1f dB, antenna=%s, ppm=%d",
            args, CENTER_FREQ / 1e6, cfg.usrp_gain, cfg.usrp_antenna, cfg.usrp_ppm,
        )

        # Set up complex float32 RX stream
        st_args = uhd.usrp.StreamArgs("fc32", "sc16")
        st_args.channels = [0]
        rx_streamer = usrp.get_rx_stream(st_args)

        stream_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        stream_cmd.stream_now = True
        rx_streamer.issue_stream_cmd(stream_cmd)

        self._usrp        = usrp
        self._rx_streamer = rx_streamer
        self.connected    = True

        recv_buf = np.zeros((1, _SAMPS_PER_RECV), dtype=np.complex64)
        metadata = uhd.types.RXMetadata()
        ok_code  = uhd.types.RXMetadataErrorCode.none
        timeout_code = uhd.types.RXMetadataErrorCode.timeout

        try:
            while not self._stop_rx.is_set():
                # Apply any queued gain change
                with self._gain_lock:
                    pending = self._pending_gain
                    self._pending_gain = None
                if pending is not None:
                    gain, = pending
                    try:
                        self._usrp.set_rx_gain(gain, 0)
                        log.info("USRP gain preview applied: %.1f dB", gain)
                    except Exception as e:
                        log.warning("USRP gain preview apply failed: %s", e)

                num_rx = rx_streamer.recv(recv_buf, metadata, timeout=0.1)

                if metadata.error_code not in (ok_code, timeout_code):
                    log.warning("USRP RX error: %s", metadata.strerror())

                if num_rx > 0:
                    self._process_iq(recv_buf[0, :num_rx].copy())

        except Exception as e:
            if self.stopped() or self._reconnect_event.is_set():
                return
            log.error("USRP error: %s", e)
            raise
        finally:
            try:
                stop_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
                rx_streamer.issue_stream_cmd(stop_cmd)
            except Exception:
                pass
            self._usrp        = None
            self._rx_streamer = None
            self.connected    = False

    # ── Gain preview ──────────────────────────────────────────────────────────

    def apply_gain_preview(self, gain: float) -> bool:
        """Queue a gain change to be applied on the next recv() iteration."""
        if self._usrp is None:
            return False
        gain = max(0.0, min(76.0, float(gain)))
        with self._gain_lock:
            self._pending_gain = (gain,)
        log.info("USRP gain preview queued: %.1f dB", gain)
        return True

    def revert_gain_preview(self) -> bool:
        """Restore gain to the saved config value."""
        return self.apply_gain_preview(self.config.receiver.usrp_gain)

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        s = BaseReceiver.status(self)
        s["sample_rate"] = SAMPLE_RATE
        s["frequency"]   = CENTER_FREQ
        return s
