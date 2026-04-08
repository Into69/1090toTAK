import logging
import time
import threading
from flask import request
from flask_socketio import SocketIO, emit

from aircraft.registry import AircraftRegistry
from config import AppConfig

log = logging.getLogger(__name__)

# Thread-safe client registry: sid → {addr, connected_at}
_clients: dict = {}
_clients_lock = threading.Lock()
# SIDs that have the settings modal open and want spectrum data
_spectrum_subs: set = set()
_spectrum_lock = threading.Lock()


def web_client_status() -> dict:
    """Return current web client count and list for /api/stats."""
    now = time.time()
    with _clients_lock:
        client_list = [
            {
                "addr": info["addr"],
                "connected_for": int(now - info["connected_at"]),
            }
            for info in _clients.values()
        ]
    return {"clients": len(client_list), "client_list": client_list}


def register_events(socketio: SocketIO, config: AppConfig, registry: AircraftRegistry, receiver=None):

    @socketio.on("connect")
    def on_connect():
        addr = request.remote_addr or "?"
        sid = request.sid
        with _clients_lock:
            _clients[sid] = {"addr": addr, "connected_at": time.time(), "last_seen": time.time()}
        log.debug("Web client connected: %s (%s)", sid, addr)
        # Send full snapshot immediately
        emit("aircraft_update", {
            "aircraft": registry.get_all_dicts(),
            "full": True,
        })

    @socketio.on("disconnect")
    def on_disconnect():
        sid = request.sid
        with _clients_lock:
            _clients.pop(sid, None)
        with _spectrum_lock:
            _spectrum_subs.discard(sid)
        log.debug("Web client disconnected: %s", sid)

    @socketio.on("spectrum_subscribe")
    def on_spectrum_subscribe(active):
        sid = request.sid
        with _spectrum_lock:
            if active:
                _spectrum_subs.add(sid)
            else:
                _spectrum_subs.discard(sid)

    @socketio.on("request_config")
    def on_request_config():
        from config import config_to_dict
        emit("config_update", config_to_dict(config))

    def _broadcast_loop():
        import time
        _prev_icaos: set = set()
        while True:
            time.sleep(1.0)
            try:
                aircraft = registry.get_all_dicts()
                curr_icaos = {ac["icao"] for ac in aircraft}
                socketio.emit("aircraft_update", {
                    "aircraft": aircraft,
                    "full": True,
                })
                # Emit explicit remove events for aircraft that just disappeared
                for icao in _prev_icaos - curr_icaos:
                    try:
                        socketio.emit("aircraft_remove", {"icao": icao})
                    except Exception:
                        pass
                _prev_icaos = curr_icaos
            except Exception as e:
                log.warning("Broadcast error: %s", e)

    socketio.start_background_task(_broadcast_loop)

    def _spectrum_loop():
        import time
        while True:
            try:
                time.sleep(0.5)
                with _spectrum_lock:
                    has_subs = bool(_spectrum_subs)
                if not has_subs:
                    continue
                spec = None
                if receiver is not None:
                    r = getattr(receiver, "_receiver", receiver)
                    spec = getattr(r, "spectrum", None)
                if spec:
                    socketio.emit("spectrum_update", {
                        "bins": [round(v, 1) for v in spec],
                        "center_freq": 1_090_000_000,
                        "sample_rate": 2_000_000,
                    })
            except Exception as e:
                log.warning("Spectrum loop error: %s", e)

    socketio.start_background_task(_spectrum_loop)

    def _signal_quality_loop():
        import time
        while True:
            time.sleep(2.0)
            try:
                sq = None
                if receiver is not None:
                    r = getattr(receiver, "_receiver", receiver)
                    sq = getattr(r, "signal_quality", None)
                socketio.emit("signal_quality_update",
                              sq if sq else {"status": "none"})
            except Exception as e:
                log.warning("Signal quality loop error: %s", e)

    socketio.start_background_task(_signal_quality_loop)

