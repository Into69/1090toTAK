"""
SBS / BaseStation TCP receiver.
Connects to dump1090 (or compatible) port 30003 and parses comma-separated MSG lines.

Message type → fields populated:
  1  – callsign
  2  – altitude, ground_speed, track, lat, lon, on_ground
  3  – altitude, lat, lon, on_ground
  4  – ground_speed, track, vertical_rate
  5  – altitude, on_ground
  6  – altitude, squawk, on_ground
  7  – altitude
  8  – on_ground
"""

import socket
import logging
from .base import BaseReceiver

log = logging.getLogger(__name__)


def _apply_position(fields: dict, lat, lon) -> None:
    """Only write lat/lon when both are present and not the Null Island bogus value."""
    if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
        fields["lat"] = lat
        fields["lon"] = lon


def _parse_sbs_line(line: str) -> dict | None:
    if not line:
        return None

    parts = line.split(",")

    # Need at least: type(0), msgtype(1), ..., icao(4) — 5 fields minimum.
    # Full spec is 22 but some implementations omit trailing empty fields.
    if len(parts) < 5 or parts[0] != "MSG":
        return None

    # Safe index accessor — returns "" for any out-of-range field.
    def _f(idx: int) -> str:
        return parts[idx].strip() if idx < len(parts) else ""

    def _float(idx):
        v = _f(idx)
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def _int(idx):
        v = _f(idx)
        if not v:
            return None
        try:
            return int(float(v))
        except ValueError:
            return None

    def _str(idx):
        v = _f(idx)
        return v if v else None

    def _bool(idx):
        v = _f(idx)
        if not v:
            return None
        # readsb/dump1090 uses "-1" for true, "0" or "" for false
        return v in ("1", "-1")

    try:
        msg_type = int(_f(1))
    except ValueError:
        log.debug("SBS: bad msg_type in line: %s", line[:80])
        return None

    icao = _f(4).upper()
    if not icao:
        return None

    fields: dict = {"icao": icao}

    if msg_type == 1:
        fields["callsign"] = _str(10)

    elif msg_type == 2:
        fields["altitude"] = _int(11)
        fields["ground_speed"] = _int(12)
        fields["track"] = _float(13)
        _apply_position(fields, _float(14), _float(15))
        fields["on_ground"] = _bool(21)

    elif msg_type == 3:
        fields["altitude"] = _int(11)
        _apply_position(fields, _float(14), _float(15))
        fields["on_ground"] = _bool(21)

    elif msg_type == 4:
        fields["ground_speed"] = _int(12)
        fields["track"] = _float(13)
        fields["vertical_rate"] = _int(16)

    elif msg_type == 5:
        fields["altitude"] = _int(11)
        fields["on_ground"] = _bool(21)

    elif msg_type == 6:
        fields["altitude"] = _int(11)
        fields["squawk"] = _str(17)
        fields["on_ground"] = _bool(21)

    elif msg_type == 7:
        fields["altitude"] = _int(11)

    elif msg_type == 8:
        fields["on_ground"] = _bool(21)

    else:
        log.debug("SBS: unknown msg_type %d", msg_type)
        return None

    # Remove None values so Aircraft.update() skips them.
    # Keep False (on_ground=False) and 0 (altitude/speed at zero).
    return {k: v for k, v in fields.items() if v is not None}


class SBSReceiver(BaseReceiver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rejected_count = 0

    def run(self) -> None:
        self._reconnect_loop(self._connect, "SBS")

    def _connect(self) -> None:
        host = self.config.receiver.host
        port = self.config.receiver.sbs_port
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(30)
            self.connected = True
            log.info("SBS: connected to %s:%d", host, port)
            buf = ""
            logged_sample = False
            while not self.stopped() and not self._reconnect_event.is_set():
                try:
                    chunk = sock.recv(4096).decode("ascii", errors="ignore")
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    # Log the very first raw line at INFO so format problems are visible
                    if not logged_sample:
                        log.info("SBS: first raw line: %s", line[:120])
                        logged_sample = True

                    fields = _parse_sbs_line(line)
                    if fields and len(fields) > 1:  # more than just icao
                        icao = fields.pop("icao")
                        self.registry.update(icao, **fields)
                        self.message_count += 1
                    elif fields:
                        # ICAO seen but no decodable fields in this message type
                        icao = fields.pop("icao")
                        self.registry.update(icao)
                        self.message_count += 1
                    else:
                        self.rejected_count += 1
                        if self.rejected_count <= 5:
                            log.debug("SBS: rejected line: %s", line[:80])
                        elif self.rejected_count == 6:
                            log.debug("SBS: suppressing further rejected-line logs")
        self.connected = False

    def status(self) -> dict:
        s = super().status()
        s["rejected"] = self.rejected_count
        return s
