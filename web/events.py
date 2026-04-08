import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from aircraft.registry import AircraftRegistry
from config import AppConfig

log = logging.getLogger(__name__)

# Client tracking — accessed only from the asyncio event loop, no locks needed.
_clients: dict[str, dict] = {}   # id -> {ws, addr, connected_at}
_spectrum_subs: set[str] = set()


def web_client_status() -> dict:
    """Return current web client count and list for /api/stats."""
    now = time.time()
    client_list = [
        {
            "addr": info["addr"],
            "connected_for": int(now - info["connected_at"]),
        }
        for info in _clients.values()
    ]
    return {"clients": len(client_list), "client_list": client_list}


async def _broadcast(msg: dict):
    """Send a JSON message to every connected WebSocket client."""
    dead: list[str] = []
    for cid, info in _clients.items():
        ws: WebSocket = info["ws"]
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json(msg)
        except Exception:
            dead.append(cid)
    for cid in dead:
        _clients.pop(cid, None)
        _spectrum_subs.discard(cid)


async def _broadcast_loop(registry: AircraftRegistry):
    prev_icaos: set = set()
    while True:
        await asyncio.sleep(1.0)
        try:
            aircraft = registry.get_all_dicts()
            curr_icaos = {ac["icao"] for ac in aircraft}
            await _broadcast({
                "type": "aircraft_update",
                "data": {"aircraft": aircraft, "full": True},
            })
            for icao in prev_icaos - curr_icaos:
                try:
                    await _broadcast({"type": "aircraft_remove", "data": {"icao": icao}})
                except Exception:
                    pass
            prev_icaos = curr_icaos
        except Exception as e:
            log.warning("Broadcast error: %s", e)


async def _spectrum_loop(receiver):
    while True:
        try:
            await asyncio.sleep(0.5)
            if not _spectrum_subs:
                continue
            spec = None
            if receiver is not None:
                r = getattr(receiver, "_receiver", receiver)
                spec = getattr(r, "spectrum", None)
            if spec:
                msg = {
                    "type": "spectrum_update",
                    "data": {
                        "bins": [round(v, 1) for v in spec],
                        "center_freq": 1_090_000_000,
                        "sample_rate": 2_000_000,
                    },
                }
                # Send only to spectrum subscribers
                dead: list[str] = []
                for cid in list(_spectrum_subs):
                    info = _clients.get(cid)
                    if not info:
                        dead.append(cid)
                        continue
                    ws: WebSocket = info["ws"]
                    try:
                        if ws.client_state == WebSocketState.CONNECTED:
                            await ws.send_json(msg)
                    except Exception:
                        dead.append(cid)
                for cid in dead:
                    _clients.pop(cid, None)
                    _spectrum_subs.discard(cid)
        except Exception as e:
            log.warning("Spectrum loop error: %s", e)


async def _signal_quality_loop(receiver):
    while True:
        await asyncio.sleep(2.0)
        try:
            sq = None
            if receiver is not None:
                r = getattr(receiver, "_receiver", receiver)
                sq = getattr(r, "signal_quality", None)
            await _broadcast({
                "type": "signal_quality_update",
                "data": sq if sq else {"status": "none"},
            })
        except Exception as e:
            log.warning("Signal quality loop error: %s", e)


def create_lifespan(config: AppConfig, registry: AircraftRegistry, receiver=None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [
            asyncio.create_task(_broadcast_loop(registry)),
            asyncio.create_task(_spectrum_loop(receiver)),
            asyncio.create_task(_signal_quality_loop(receiver)),
        ]
        yield
        for t in tasks:
            t.cancel()
    return lifespan


def setup_websocket(app: FastAPI, config: AppConfig, registry: AircraftRegistry, receiver=None):

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        client_id = str(id(ws))
        addr = ws.client.host if ws.client else "?"
        _clients[client_id] = {"ws": ws, "addr": addr, "connected_at": time.time()}
        log.debug("Web client connected: %s (%s)", client_id, addr)

        # Send full snapshot immediately
        try:
            await ws.send_json({
                "type": "aircraft_update",
                "data": {"aircraft": registry.get_all_dicts(), "full": True},
            })
        except Exception:
            _clients.pop(client_id, None)
            return

        try:
            while True:
                raw = await ws.receive_json()
                msg_type = raw.get("type")
                if msg_type == "spectrum_subscribe":
                    if raw.get("data"):
                        _spectrum_subs.add(client_id)
                    else:
                        _spectrum_subs.discard(client_id)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            _clients.pop(client_id, None)
            _spectrum_subs.discard(client_id)
            log.debug("Web client disconnected: %s", client_id)
