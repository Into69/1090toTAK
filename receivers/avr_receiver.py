"""
AVR / raw hex receiver.
Supports two modes:
  - TCP: connects to dump1090 port 30002 (raw AVR frames)
  - Subprocess: spawns rtl_adsb and reads its stdout

AVR frame format:  *<hex_bytes>;

Decoding priority:
  1. pyModeS (if installed) — more complete, handles edge cases
  2. Built-in adsb_decoder   — no external deps, covers common cases
"""

import math
import socket
import subprocess
import time
import logging
from typing import Optional

from capabilities import HAS_PYMODES
if HAS_PYMODES:
    import pyModeS as pms
    from pyModeS import adsb as pms_adsb

# Always available — our own minimal decoder
from . import adsb_decoder as dec

from .base import BaseReceiver

log = logging.getLogger(__name__)


class AVRReceiver(BaseReceiver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # CPR buffer: icao -> {"even": (msg, t), "odd": (msg, t)}
        self._cpr_buf: dict = {}
        # Optional callback(hex_str) — AVROutputServer hooks in here
        self.frame_sink = None

    def run(self) -> None:
        decoder = "pyModeS" if HAS_PYMODES else "built-in"
        log.info("AVR: using %s decoder", decoder)
        if self.config.receiver.type == "avr_subprocess":
            self._reconnect_loop(self._run_subprocess, "rtl_adsb")
        else:
            self._reconnect_loop(self._connect_tcp, "AVR")

    # ------------------------------------------------------------------ TCP

    def _connect_tcp(self) -> None:
        host = self.config.receiver.host
        port = self.config.receiver.avr_port
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(30)
            self.connected = True
            log.info("AVR: connected to %s:%d", host, port)
            buf = b""
            while not self.stopped() and not self._reconnect_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b";" in buf:
                    end = buf.index(b";")
                    frame = buf[:end + 1]
                    buf = buf[end + 1:]
                    self._parse_avr(frame.decode("ascii", errors="ignore").strip())
        self.connected = False

    # ----------------------------------------------------------- subprocess

    def _run_subprocess(self) -> None:
        cfg = self.config.receiver
        cmd = ["rtl_adsb", "-d", str(cfg.rtlsdr_device_index)]
        if not cfg.rtlsdr_agc:
            cmd += ["-g", str(cfg.rtlsdr_gain)]
        if cfg.rtlsdr_ppm != 0:
            cmd += ["-p", str(cfg.rtlsdr_ppm)]
        log.info("rtl_adsb: launching %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "rtl_adsb not found on PATH — install rtl-sdr: "
                "sudo apt install rtl-sdr  (Linux) or https://osmocom.org/projects/rtl-sdr"
            )

        self.connected = True
        logged_sample = False
        try:
            for raw in proc.stdout:
                if self.stopped() or self._reconnect_event.is_set():
                    break
                line = raw.decode("ascii", errors="ignore").strip()
                if line and not logged_sample:
                    log.info("rtl_adsb: first raw line: %s", line[:80])
                    logged_sample = True
                self._parse_avr(line)

            # Process exited — surface any stderr so the user can diagnose
            stderr_out = proc.stderr.read().decode("ascii", errors="ignore").strip()
            if stderr_out:
                log.warning("rtl_adsb stderr: %s", stderr_out[:400])
            ret = proc.wait()
            if ret not in (0, -15):   # -15 = SIGTERM (our own terminate)
                raise RuntimeError(f"rtl_adsb exited with code {ret}")
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
            self.connected = False

    # ---------------------------------------------------------- frame parse

    def _parse_avr(self, raw: str) -> None:
        msg = raw.strip("*;").strip().upper()
        if len(msg) < 14:
            return

        # Reject frames with bad CRC — catches rtl_adsb bit errors
        if not dec.crc_ok(msg):
            return

        # Forward raw frame to AVR output server if one is attached
        if self.frame_sink:
            try:
                self.frame_sink(msg)
            except Exception:
                pass

        try:
            df = pms.df(msg) if HAS_PYMODES else dec.df(msg)
        except Exception:
            return

        if df != 17:
            return

        try:
            icao = (pms.icao(msg) if HAS_PYMODES else dec.icao(msg)).upper()
            tc   = (pms_adsb.typecode(msg) if HAS_PYMODES else dec.typecode(msg))

            if 1 <= tc <= 4:
                fields = {}
                cs = pms_adsb.callsign(msg) if HAS_PYMODES else dec.callsign(msg)
                if cs:
                    fields["callsign"] = cs.strip("_").strip()
                cat = dec.category(msg)   # always use built-in; pyModeS lacks this
                if cat:
                    fields["category"] = cat
                if fields:
                    log.debug("AVR ident frame icao=%s tc=%d callsign=%s category=%s",
                              icao, tc, fields.get("callsign"), fields.get("category"))
                # Always touch last_seen even if no new fields decoded
                self.registry.update(icao, **fields)
                self.message_count += 1

            elif 5 <= tc <= 8:
                # Surface position — aircraft is on the ground
                self.registry.update(icao, on_ground=True)
                self.message_count += 1

            elif 9 <= tc <= 18 or 20 <= tc <= 22:
                # Touch last_seen regardless of whether CPR can be resolved yet
                self.registry.update(icao, on_ground=False)
                log.debug("AVR pos frame icao=%s tc=%d", icao, tc)
                self._handle_position(icao, msg, tc)
                self.message_count += 1

            elif tc == 19:
                result = pms_adsb.velocity(msg) if HAS_PYMODES else dec.velocity(msg)
                if result:
                    spd, hdg, vr, _ = result
                    self.registry.update(
                        icao,
                        ground_speed=int(spd) if spd is not None else None,
                        track=hdg,
                        vertical_rate=int(vr) if vr is not None else None,
                    )
                else:
                    self.registry.update(icao)
                self.message_count += 1

            else:
                # TC 0, 23-31 — no useful data but update last_seen
                self.registry.update(icao)

        except Exception as e:
            log.warning("AVR decode error for %s: %s", raw[:30], e)

    def _handle_position(self, icao: str, msg: str, tc: int) -> None:
        t = time.time()
        try:
            oe = dec.oe_flag(msg)
        except Exception:
            return

        # Always buffer the CPR frame
        buf = self._cpr_buf.setdefault(icao, {"even": None, "odd": None})
        buf["even" if oe == 0 else "odd"] = (msg, t)

        alt = dec.altitude(msg)
        if HAS_PYMODES:
            try:
                alt = pms_adsb.altitude(msg)
            except Exception:
                pass

        lat = lon = None

        # ----------------------------------------------------------
        # 1. Global CPR (primary) — needs even+odd pair, self-correcting
        # ----------------------------------------------------------
        even, odd = buf["even"], buf["odd"]
        if even and odd:
            try:
                if HAS_PYMODES:
                    pos = pms_adsb.position(even[0], odd[0], even[1], odd[1])
                    if pos is not None:
                        lat, lon = pos
                else:
                    lat, lon = dec.cpr_position(even[0], odd[0], even[1], odd[1])
            except Exception as e:
                log.warning("CPR global decode failed for %s: %s", icao, e)

            # Cross-check global result with local CPR if reference available
            if lat is not None and lon is not None:
                ref_lat, ref_lon = self._get_cpr_reference(icao, t)
                if ref_lat is not None:
                    loc_lat, loc_lon = dec.cpr_position_local(msg, ref_lat, ref_lon)
                    if loc_lat is not None:
                        dist = self._haversine_nm(lat, lon, loc_lat, loc_lon)
                        if dist > 25:
                            # Global and local disagree — likely zone error, use local
                            log.debug("CPR cross-check icao=%s: global/local disagree by %.0f nm, using local",
                                      icao, dist)
                            lat, lon = loc_lat, loc_lon

        # ----------------------------------------------------------
        # 2. Local CPR (fallback) — single frame + reference point
        #    Used when only one frame type available (no even+odd pair yet)
        # ----------------------------------------------------------
        if lat is None:
            ref_lat, ref_lon = self._get_cpr_reference(icao, t)
            if ref_lat is not None:
                lat, lon = dec.cpr_position_local(msg, ref_lat, ref_lon)
                if lat is not None:
                    log.debug("CPR local decode icao=%s lat=%.4f lon=%.4f", icao, lat, lon)

        if lat is None or lon is None:
            return

        # Plausibility checks — reject bad coordinates
        if not self._position_plausible(lat, lon, t, icao):
            return

        log.debug("Position decoded icao=%s lat=%.4f lon=%.4f alt=%s", icao, lat, lon, alt)
        self.registry.update(icao, lat=lat, lon=lon, altitude=alt)

    def _get_cpr_reference(self, icao: str, t: float):
        """Return (ref_lat, ref_lon) for local CPR, or (None, None)."""
        # Prefer aircraft's own last known position
        ac = self.registry.get(icao)
        if ac and ac.lat is not None and ac.lon is not None and ac.last_position:
            if t - ac.last_position < 120:
                return ac.lat, ac.lon
        # Fall back to configured receiver location
        cfg_loc = self.config.location
        if cfg_loc.lat != 0 or cfg_loc.lon != 0:
            return cfg_loc.lat, cfg_loc.lon
        return None, None

    def _position_plausible(self, lat: float, lon: float, t: float, icao: str) -> bool:
        """Reject positions outside valid bounds, beyond receiver range, or
        that imply impossible speed from the aircraft's last known position."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            log.debug("Position rejected icao=%s: out-of-range lat=%.4f lon=%.4f", icao, lat, lon)
            return False

        # Check against receiver location — max 450 nm (ADS-B line-of-sight limit)
        cfg_loc = self.config.location
        if cfg_loc.lat != 0 or cfg_loc.lon != 0:
            dist = self._haversine_nm(cfg_loc.lat, cfg_loc.lon, lat, lon)
            if dist > 450:
                log.debug("Position rejected icao=%s: %.0f nm from receiver (max 450)", icao, dist)
                return False

        # Speed gate — reject jumps implying > 4000 knots
        ac = self.registry.get(icao)
        if ac and ac.lat is not None and ac.lon is not None and ac.last_position:
            dt = t - ac.last_position
            if dt > 0.5:
                dist = self._haversine_nm(ac.lat, ac.lon, lat, lon)
                speed_kts = dist / (dt / 3600)
                if speed_kts > 4000:
                    log.debug("Position rejected icao=%s: %.0f nm in %.1fs (%.0f kts)",
                              icao, dist, dt, speed_kts)
                    return False

        return True

    @staticmethod
    def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in nautical miles."""
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return 2 * 3440.065 * math.asin(math.sqrt(a))  # 3440.065 nm = earth radius
