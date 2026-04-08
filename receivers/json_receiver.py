"""
dump1090 / tar1090 HTTP JSON API receiver.

Polls http://host:port/data/aircraft.json every second and updates the
aircraft registry.  This is the richest data source available from dump1090
and provides fields that the SBS and AVR formats lack, most notably the
ADS-B emitter category (e.g. "A3" = large aircraft).

The aircraft.json endpoint is served by dump1090-fa and tar1090 on the
web UI port (default 8080).
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import BaseReceiver

log = logging.getLogger(__name__)

# Map dump1090 emergency strings to squawk codes
_EMERG_SQUAWK_MAP = {
    "general":    "7700",
    "lifeguard":  "7700",
    "minfuel":    "7700",
    "nordo":      "7600",
    "unlawful":   "7500",
    "downed":     "7700",
}


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class JSONReceiver(BaseReceiver):
    """Poll dump1090's aircraft.json HTTP endpoint."""

    POLL_INTERVAL = 1.0          # seconds between aircraft.json polls
    LOCATION_REFRESH = 30.0      # seconds between receiver.json re-fetches

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.receiver_lat: Optional[float] = None
        self.receiver_lon: Optional[float] = None
        self.rejected_count: int = 0   # aircraft entries with no useful fields
        self.poll_count: int = 0       # successful polls
        self._last_location_fetch: float = 0.0
        self._logged_connected: bool = False
        self.last_error: str = ""

    def run(self) -> None:
        host = self.config.receiver.host
        port = self.config.receiver.json_port
        log.info("JSON: starting — polling http://%s:%d/data/aircraft.json", host, port)
        self._fetch_receiver_location()
        while not self.stopped():
            # Pick up host/port changes requested via reconnect()
            if self._reconnect_event.is_set():
                self._reconnect_event.clear()
                host = self.config.receiver.host
                port = self.config.receiver.json_port
                self._logged_connected = False
                log.info("JSON: reconnecting — polling http://%s:%d/data/aircraft.json", host, port)
            try:
                self._poll_once()
                if not self._logged_connected:
                    log.info("JSON: connected — polling http://%s:%d/data/aircraft.json", host, port)
                    self._logged_connected = True
                self.last_error = ""
            except Exception as e:
                first_error = self.connected or not self.last_error
                self.connected = False
                self.error_count += 1
                self._logged_connected = False
                self.last_error = str(e)
                if first_error:
                    log.warning("JSON: poll error at http://%s:%d/data/aircraft.json: %s",
                                self.config.receiver.host, self.config.receiver.json_port, e)
            # Periodically refresh receiver location
            if time.time() - self._last_location_fetch > self.LOCATION_REFRESH:
                self._fetch_receiver_location()
            time.sleep(self.POLL_INTERVAL)

    def _fetch_receiver_location(self) -> None:
        self._last_location_fetch = time.time()
        host = self.config.receiver.host
        port = self.config.receiver.json_port
        url = f"http://{host}:{port}/data/receiver.json"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            lat = _float_or_none(data.get("lat"))
            lon = _float_or_none(data.get("lon"))
            if lat is not None and lon is not None:
                self.receiver_lat = lat
                self.receiver_lon = lon
                log.debug("JSON receiver location: %.6f, %.6f", lat, lon)
            else:
                log.debug("receiver.json has no lat/lon")
        except Exception as e:
            log.debug("Could not fetch receiver.json: %s", e)

    def _poll_once(self) -> None:
        host = self.config.receiver.host
        port = self.config.receiver.json_port
        url = f"http://{host}:{port}/data/aircraft.json"

        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())

        self.connected = True
        self.poll_count += 1

        for ac in data.get("aircraft", []):
            icao = (ac.get("hex") or "").strip().upper()
            if not icao:
                continue

            fields: dict = {}

            # Callsign / flight number
            flight = (ac.get("flight") or "").strip()
            if flight:
                fields["callsign"] = flight

            # Altitude — dump1090 sends "ground" for surface targets
            alt_baro = ac.get("alt_baro")
            if alt_baro == "ground":
                fields["on_ground"] = True
            elif alt_baro is not None:
                v = _int_or_none(alt_baro)
                if v is not None:
                    fields["altitude"] = v

            # Position
            lat = _float_or_none(ac.get("lat"))
            lon = _float_or_none(ac.get("lon"))
            if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
                fields["lat"] = lat
                fields["lon"] = lon

            # Ground speed (knots) and track
            gs = _int_or_none(ac.get("gs"))
            if gs is not None:
                fields["ground_speed"] = gs
            track = _float_or_none(ac.get("track"))
            if track is not None:
                fields["track"] = track

            # Vertical rate (ft/min)
            vr = _int_or_none(ac.get("baro_rate"))
            if vr is not None:
                fields["vertical_rate"] = vr

            # Squawk
            squawk = ac.get("squawk")
            if squawk:
                fields["squawk"] = str(squawk)

            # Emitter category — e.g. "A3" (large aircraft), "A7" (rotorcraft)
            category = ac.get("category")
            if category:
                fields["category"] = str(category)

            # Emergency
            emergency = ac.get("emergency", "none")
            if emergency and emergency != "none":
                eq = _EMERG_SQUAWK_MAP.get(emergency)
                if eq and not fields.get("squawk"):
                    fields["squawk"] = eq

            if fields:
                self.registry.update(icao, **fields)
                self.message_count += 1
            else:
                # ICAO seen but nothing useful decoded — touch last_seen only
                self.registry.update(icao)
                self.rejected_count += 1

    def status(self) -> dict:
        s = super().status()
        s["url"]        = f"http://{self.config.receiver.host}:{self.config.receiver.json_port}/data/aircraft.json"
        s["polls"]      = self.poll_count
        s["rejected"]   = self.rejected_count
        s["last_error"] = self.last_error
        return s
