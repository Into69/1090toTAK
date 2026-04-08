"""
TCP output servers — re-broadcast decoded aircraft data to connected clients.

SBSOutputServer : streams BaseStation/SBS MSG lines  (default port 30003)
AVROutputServer : streams raw AVR hex frames *HEX;\n (default port 30002)

SBS format is always available (generated from the aircraft registry).
AVR format requires the active receiver to be an AVR/subprocess source;
frames are forwarded via AVRReceiver.frame_sink.
"""

import socket
import threading
import time
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# Seconds to wait on a blocked send before treating the client as dead.
_SEND_TIMEOUT = 5.0

# Seconds of no successful send before the watchdog forcibly disconnects a client.
# SBS broadcasts every second, so 30s means ~30 consecutive failed sends went
# undetected (shouldn't happen with _SEND_TIMEOUT, but guards AVR idle periods).
_IDLE_TIMEOUT = 30.0

# Watchdog check interval in seconds.
_WATCHDOG_INTERVAL = 10.0


class _ClientEntry:
    """Holds a connected client socket and associated metadata."""
    __slots__ = ("sock", "addr", "connected_at", "last_ok")

    def __init__(self, sock: socket.socket, addr: tuple):
        self.sock = sock
        self.addr = addr          # (host, port)
        self.connected_at = time.time()
        self.last_ok = time.time()

    def info(self) -> dict:
        now = time.time()
        return {
            "addr": f"{self.addr[0]}:{self.addr[1]}",
            "connected_for": int(now - self.connected_at),
            "idle": int(now - self.last_ok),
        }


# ---------------------------------------------------------------------------
# Base TCP output server
# ---------------------------------------------------------------------------

class _TCPOutputServer:
    """Accept TCP connections and broadcast text lines to all clients."""

    def __init__(self, name: str, port: int):
        self._name = name
        self._port = port
        self._clients: list[_ClientEntry] = []
        self._lock = threading.Lock()
        self._server_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

    @property
    def port(self) -> int:
        return self._port

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def client_list(self) -> list:
        with self._lock:
            return [e.info() for e in self._clients]

    def start(self) -> None:
        self._stop_event.clear()
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self._port))
        self._server_sock.listen(10)
        self._server_sock.settimeout(1.0)
        threading.Thread(
            target=self._accept_loop, daemon=True, name=f"{self._name}-accept"
        ).start()
        threading.Thread(
            target=self._watchdog_loop, daemon=True, name=f"{self._name}-watchdog"
        ).start()
        log.info("%s: listening on port %d", self._name, self._port)

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        with self._lock:
            for entry in list(self._clients):
                try:
                    entry.sock.close()
                except Exception:
                    pass
            self._clients.clear()
        log.info("%s: stopped", self._name)

    def broadcast(self, line: str) -> None:
        """Send one line to every connected client; drop dead connections."""
        data = (line + "\r\n").encode("ascii", errors="ignore")
        dead = []
        with self._lock:
            for entry in self._clients:
                try:
                    entry.sock.sendall(data)
                    entry.last_ok = time.time()
                except Exception:
                    dead.append(entry)
            for entry in dead:
                self._clients.remove(entry)
                try:
                    entry.sock.close()
                except Exception:
                    pass
        if dead:
            for entry in dead:
                log.debug("%s: client %s disconnected (send failed)",
                          self._name, entry.addr[0])

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
                client_sock.settimeout(_SEND_TIMEOUT)
                entry = _ClientEntry(client_sock, addr)
                with self._lock:
                    self._clients.append(entry)
                log.debug("%s: client connected from %s:%d", self._name, addr[0], addr[1])
            except socket.timeout:
                continue
            except Exception:
                break

    def _watchdog_loop(self) -> None:
        """Periodically disconnect clients that have been idle too long."""
        while not self._stop_event.is_set():
            time.sleep(_WATCHDOG_INTERVAL)
            now = time.time()
            timed_out = []
            with self._lock:
                for entry in self._clients:
                    if now - entry.last_ok > _IDLE_TIMEOUT:
                        timed_out.append(entry)
                for entry in timed_out:
                    self._clients.remove(entry)
            for entry in timed_out:
                try:
                    entry.sock.close()
                except Exception:
                    pass
                log.debug(
                    "%s: client %s:%d timed out (idle %.0fs), disconnected",
                    self._name, entry.addr[0], entry.addr[1],
                    now - entry.last_ok,
                )


# ---------------------------------------------------------------------------
# SBS / BaseStation output server
# ---------------------------------------------------------------------------

def _sbs_dt() -> str:
    now = datetime.now()
    d = now.strftime("%Y/%m/%d")
    t = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    return f"{d},{t},{d},{t}"


class SBSOutputServer(_TCPOutputServer):
    """
    Streams BaseStation MSG lines generated from the aircraft registry.
    Broadcasts all aircraft state once per second.
    On new client connect, the current state is sent immediately.
    """

    def __init__(self, port: int, registry):
        super().__init__("SBS Output", port)
        self._registry = registry

    def start(self) -> None:
        super().start()
        threading.Thread(
            target=self._broadcast_loop, daemon=True, name="SBS-Output-bcast"
        ).start()

    def _accept_loop(self) -> None:
        """Override to send current state immediately on new connection."""
        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
                client_sock.settimeout(_SEND_TIMEOUT)
                entry = _ClientEntry(client_sock, addr)
                with self._lock:
                    self._clients.append(entry)
                log.debug("%s: client connected from %s:%d", self._name, addr[0], addr[1])
                threading.Thread(
                    target=self._send_snapshot, args=(entry,),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _send_snapshot(self, entry: _ClientEntry) -> None:
        for ac in self._registry.get_all():
            for line in self._aircraft_lines(ac):
                try:
                    entry.sock.sendall((line + "\r\n").encode("ascii", errors="ignore"))
                    entry.last_ok = time.time()
                except Exception:
                    return

    def _broadcast_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(1.0)
            if self.client_count == 0:
                continue
            try:
                for ac in self._registry.get_all():
                    for line in self._aircraft_lines(ac):
                        self.broadcast(line)
                # Keep idle watchdog from firing during quiet periods (no aircraft).
                # The broadcast loop itself is proof the connection path is healthy.
                now = time.time()
                with self._lock:
                    for entry in self._clients:
                        if now - entry.last_ok > _IDLE_TIMEOUT / 2:
                            entry.last_ok = now
            except Exception as e:
                log.debug("%s broadcast error: %s", self._name, e)

    def _aircraft_lines(self, ac) -> list:
        lines = []
        icao = ac.icao
        dt = _sbs_dt()
        gnd = "-1" if ac.on_ground else "0"

        if ac.callsign:
            lines.append(
                f"MSG,1,1,1,{icao},1,{dt},{ac.callsign},,,,,,,,,,,,{gnd}"
            )

        if ac.altitude is not None or ac.lat is not None:
            alt = ac.altitude if ac.altitude is not None else ""
            lat = f"{ac.lat:.6f}" if ac.lat is not None else ""
            lon = f"{ac.lon:.6f}" if ac.lon is not None else ""
            lines.append(
                f"MSG,3,1,1,{icao},1,{dt},,{alt},,,{lat},{lon},,,,,,{gnd}"
            )

        if any(v is not None for v in (ac.ground_speed, ac.track, ac.vertical_rate)):
            spd = ac.ground_speed if ac.ground_speed is not None else ""
            trk = f"{ac.track:.1f}" if ac.track is not None else ""
            vr  = ac.vertical_rate if ac.vertical_rate is not None else ""
            lines.append(
                f"MSG,4,1,1,{icao},1,{dt},,,{spd},{trk},,,{vr},,,,,{gnd}"
            )

        if ac.squawk:
            alt = ac.altitude if ac.altitude is not None else ""
            lines.append(
                f"MSG,6,1,1,{icao},1,{dt},,{alt},,,,,,{ac.squawk},,,,{gnd}"
            )

        return lines


# ---------------------------------------------------------------------------
# AVR Raw output server
# ---------------------------------------------------------------------------

class AVROutputServer(_TCPOutputServer):
    """
    Streams raw AVR-format frames: *<HEX>;\r\n
    Frames are pushed in real-time via push_frame() from the active receiver.
    """

    def __init__(self, port: int):
        super().__init__("AVR Output", port)

    def start(self) -> None:
        super().start()
        threading.Thread(
            target=self._keepalive_loop, daemon=True, name="AVR-Output-keepalive"
        ).start()

    def _keepalive_loop(self) -> None:
        """Touch last_ok periodically so idle watchdog doesn't fire during quiet skies."""
        while not self._stop_event.is_set():
            time.sleep(_WATCHDOG_INTERVAL)
            if self.client_count == 0:
                continue
            now = time.time()
            with self._lock:
                for entry in self._clients:
                    if now - entry.last_ok > _IDLE_TIMEOUT / 2:
                        entry.last_ok = now

    def push_frame(self, hex_msg: str) -> None:
        """Forward a validated ADS-B hex frame to all connected clients."""
        if self.client_count > 0:
            self.broadcast(f"*{hex_msg};")


# ---------------------------------------------------------------------------
# Server manager — lifecycle tied to config
# ---------------------------------------------------------------------------

class ServerManager:
    """Start/stop/restart output servers when configuration changes."""

    def __init__(self, config, registry, receiver=None):
        self._config = config
        self._registry = registry
        self._receiver = receiver
        self._sbs: Optional[SBSOutputServer] = None
        self._avr: Optional[AVROutputServer] = None

    def apply(self) -> None:
        """Reconcile running servers with current config."""
        cfg = self._config.servers

        # --- SBS server ---
        if cfg.sbs_enabled:
            if self._sbs is None or self._sbs.port != cfg.sbs_port:
                if self._sbs:
                    self._sbs.stop()
                self._sbs = SBSOutputServer(cfg.sbs_port, self._registry)
                self._sbs.start()
        else:
            if self._sbs:
                self._sbs.stop()
                self._sbs = None

        # --- AVR server ---
        if cfg.avr_enabled:
            if self._avr is None or self._avr.port != cfg.avr_port:
                if self._avr:
                    self._avr.stop()
                self._avr = AVROutputServer(cfg.avr_port)
                self._avr.start()
            # Attach frame sink — resolve through ReceiverManager if needed
            active = getattr(self._receiver, "receiver", self._receiver)
            if active and hasattr(active, "frame_sink"):
                active.frame_sink = self._avr.push_frame
        else:
            if self._avr:
                self._avr.stop()
                self._avr = None
            active = getattr(self._receiver, "receiver", self._receiver)
            if active and hasattr(active, "frame_sink"):
                active.frame_sink = None

    def stop(self) -> None:
        if self._sbs:
            self._sbs.stop()
        if self._avr:
            self._avr.stop()

    def status(self) -> dict:
        return {
            "sbs": {
                "enabled": self._sbs is not None,
                "port": self._config.servers.sbs_port,
                "clients": self._sbs.client_count if self._sbs else 0,
                "client_list": self._sbs.client_list if self._sbs else [],
            },
            "avr": {
                "enabled": self._avr is not None,
                "port": self._config.servers.avr_port,
                "clients": self._avr.client_count if self._avr else 0,
                "client_list": self._avr.client_list if self._avr else [],
            },
        }
