"""
Minimal gpsd client — connects to gpsd over TCP, watches for TPV reports,
and updates config.location.lat / config.location.lon in place.

Protocol: gpsd JSON over TCP (port 2947).
Sends ?WATCH={"enable":true,"json":true} then reads newline-delimited JSON.
Only TPV reports with mode >= 2 (2-D fix) are used.
"""

import json
import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

_WATCH_CMD = b'?WATCH={"enable":true,"json":true};\n'
_RECONNECT_DELAY = 10  # seconds between reconnect attempts


class GpsdClient(threading.Thread):
    def __init__(self, config):
        super().__init__(daemon=True, name="gpsd-client")
        self.config = config
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            if self.config.location.mode != "gpsd":
                time.sleep(2)
                continue
            try:
                self._connect()
            except Exception as e:
                log.warning("gpsd: connection failed: %s — retrying in %ds", e, _RECONNECT_DELAY)
            if not self._stop_event.is_set():
                time.sleep(_RECONNECT_DELAY)

    def _connect(self):
        host = self.config.location.gpsd_host
        port = self.config.location.gpsd_port
        log.info("gpsd: connecting to %s:%d", host, port)
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(30)
            sock.sendall(_WATCH_CMD)
            log.info("gpsd: connected")
            buf = b""
            while not self._stop_event.is_set():
                if self.config.location.mode != "gpsd":
                    break
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    log.warning("gpsd: connection closed by server")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle(line.decode("utf-8", errors="ignore").strip())

    def _handle(self, line: str):
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if msg.get("class") != "TPV":
            return
        mode = msg.get("mode", 0)
        if mode < 2:
            return
        lat = msg.get("lat")
        lon = msg.get("lon")
        if lat is not None and lon is not None:
            self.config.location.lat = float(lat)
            self.config.location.lon = float(lon)
            log.debug("gpsd: fix %.6f, %.6f (mode %d)", lat, lon, mode)
