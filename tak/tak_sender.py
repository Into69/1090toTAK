"""
TAK sender — periodically sends CoT XML for all positioned aircraft.

Supports:
  udp       — unicast UDP (also works for LAN broadcast)
  multicast — UDP multicast (default: 239.2.3.1:6969, standard SA multicast)
  tcp       — TCP connection (reconnects on failure)
"""

import socket
import threading
import time
import logging
from typing import Optional

from aircraft.registry import AircraftRegistry
from config import AppConfig, TAK_UDP, TAK_MULTICAST, TAK_TCP
from .cot_builder import CotBuilder

log = logging.getLogger(__name__)


class TAKSender:
    def __init__(self, config: AppConfig, registry: AircraftRegistry):
        self.config = config
        self.registry = registry
        self.builder = CotBuilder()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.sent_count = 0
        self.error_count = 0
        self._tcp_sock: Optional[socket.socket] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tak-sender"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_tcp()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self.config.tak.interval)
            if self._stop_event.is_set():
                break
            if not self.config.tak.enabled:
                continue
            self._send_all()

    def _send_all(self) -> None:
        import time as _time
        aircraft_list = self.registry.get_all()
        now = _time.time()
        positioned = [
            a for a in aircraft_list
            if a.has_position() and (now - a.last_position) <= self.config.aircraft_ttl
        ]
        if not positioned:
            return

        log.debug("TAK: sending %d aircraft", len(positioned))
        for ac in positioned:
            try:
                cot = self.builder.build(ac, self.config.aircraft_ttl)
                self._dispatch(cot)
                self.sent_count += 1
            except Exception as e:
                self.error_count += 1
                log.warning("TAK send error for %s: %s", ac.icao, e)

    def _dispatch(self, data: bytes) -> None:
        proto = self.config.tak.protocol
        host = self.config.tak.host
        port = self.config.tak.port

        if proto == TAK_MULTICAST:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                               socket.IPPROTO_UDP) as s:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
                s.sendto(data, (host, port))

        elif proto == TAK_UDP:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(data, (host, port))

        elif proto == TAK_TCP:
            self._tcp_send(data, host, port)

    def _tcp_send(self, data: bytes, host: str, port: int) -> None:
        if self._tcp_sock is None:
            self._tcp_connect(host, port)
        try:
            self._tcp_sock.sendall(data)
        except Exception:
            self._close_tcp()
            self._tcp_connect(host, port)
            self._tcp_sock.sendall(data)

    def _tcp_connect(self, host: str, port: int) -> None:
        s = socket.create_connection((host, port), timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._tcp_sock = s
        log.info("TAK TCP: connected to %s:%d", host, port)

    def _close_tcp(self) -> None:
        if self._tcp_sock:
            try:
                self._tcp_sock.close()
            except Exception:
                pass
            self._tcp_sock = None

    def send_single(self, icao: str) -> tuple[bool, str]:
        """Send a single aircraft immediately (used by API endpoint).
        Returns (success, reason) so the caller can surface a useful message.
        Works regardless of config.tak.enabled — the button is a manual override.
        """
        ac = self.registry.get(icao)
        if not ac:
            return False, f"Aircraft {icao} not found"
        if not ac.has_position():
            return False, "No position data received for this aircraft yet"
        cfg = self.config.tak
        try:
            cot = self.builder.build(ac, self.config.aircraft_ttl)
            self._dispatch(cot)
            self.sent_count += 1
            return True, f"Sent via {cfg.protocol.upper()} to {cfg.host}:{cfg.port}"
        except Exception as e:
            log.warning("TAK single send error: %s", e)
            return False, str(e)

    def status(self) -> dict:
        return {
            "enabled": self.config.tak.enabled,
            "protocol": self.config.tak.protocol,
            "host": self.config.tak.host,
            "port": self.config.tak.port,
            "interval": self.config.tak.interval,
            "sent": self.sent_count,
            "errors": self.error_count,
        }
