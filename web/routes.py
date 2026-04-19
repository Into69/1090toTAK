import logging
import math
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse, FileResponse, Response
from starlette.templating import Jinja2Templates

from config import AppConfig, update_config_from_dict, save_config, config_to_dict
from aircraft.registry import AircraftRegistry
from capabilities import HAS_RTLSDR, probe_gpsd

log = logging.getLogger(__name__)

# Initialise psutil for system-wide CPU/memory stats. cpu_percent(interval=None)
# needs one priming call before it returns meaningful values on Linux.
try:
    import psutil as _psutil
    _psutil.Process().cpu_percent(interval=None)  # prime the per-process counter
    _HAS_PSUTIL = True
except Exception as _e:
    _psutil = None
    _HAS_PSUTIL = False
    log.warning("psutil not available (%s) — falling back to /proc for system stats", _e)

# /proc-based fallback for Linux when psutil is missing
_prev_cpu_idle = 0
_prev_cpu_total = 0

def _proc_system_stats() -> dict | None:
    """Read CPU and memory from /proc (Linux only)."""
    global _prev_cpu_idle, _prev_cpu_total
    import os
    if os.name != "posix":
        return None
    try:
        # CPU — diff between two reads of /proc/stat
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # user nice system idle iowait irq softirq steal
        vals = [int(v) for v in parts[1:9]]
        idle = vals[3] + vals[4]   # idle + iowait
        total = sum(vals)
        d_idle = idle - _prev_cpu_idle
        d_total = total - _prev_cpu_total
        cpu_pct = 0.0 if d_total == 0 else (1.0 - d_idle / d_total) * 100.0
        _prev_cpu_idle = idle
        _prev_cpu_total = total

        # App RSS from /proc/self/status
        rss_kb = 0
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break

        return {
            "cpu_pct": round(cpu_pct, 1),
            "mem_used_mb": rss_kb >> 10,
        }
    except Exception:
        return None

_web_dir = Path(__file__).parent


def create_router(config: AppConfig, registry: AircraftRegistry, templates: Jinja2Templates, tak_sender=None, receiver=None, server_manager=None, store=None, gpsd_client=None):

    router = APIRouter()

    @router.get("/")
    def index(request: Request):
        from version import __version__
        return templates.TemplateResponse(request, "index.html", {
            "config": config,
            "has_rtlsdr": HAS_RTLSDR,
            "has_gpsd": probe_gpsd(config.location.gpsd_host, config.location.gpsd_port),
            "version": __version__,
        })

    @router.get("/api/gpsd/probe")
    def gpsd_probe(host: str = Query(None), port: int = Query(None)):
        if host is None:
            host = config.location.gpsd_host
        if port is None:
            port = config.location.gpsd_port
        return {"available": probe_gpsd(host, port)}

    @router.get("/tile-sw.js")
    def tile_sw():
        path = str(_web_dir / "static" / "tile-sw.js")
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @router.get("/api/aircraft")
    def get_aircraft():
        return registry.get_all_dicts()

    @router.get("/api/receivers")
    def get_receivers():
        if hasattr(receiver, '_receivers'):
            return [{"id": k, **v.status()} for k, v in receiver._receivers.items()]
        return [{"id": "default", **receiver.status()}]

    @router.get("/data/aircraft.json")
    def dump1090_aircraft_json():
        """dump1090-compatible aircraft.json endpoint."""
        import time as _time
        _squawk_emergency = {"7500": "unlawful", "7600": "nordo", "7700": "general"}
        aircraft_list = []
        for ac in registry.get_all():
            entry = {"hex": ac.icao.lower(), "seen": round(ac.age(), 1)}
            if ac.callsign:
                entry["flight"] = ac.callsign
            if ac.on_ground:
                entry["alt_baro"] = "ground"
            elif ac.altitude is not None:
                entry["alt_baro"] = ac.altitude
            if ac.lat is not None and ac.lon is not None:
                entry["lat"] = ac.lat
                entry["lon"] = ac.lon
            if ac.ground_speed is not None:
                entry["gs"] = ac.ground_speed
            if ac.track is not None:
                entry["track"] = ac.track
            if ac.vertical_rate is not None:
                entry["baro_rate"] = ac.vertical_rate
            if ac.squawk:
                entry["squawk"] = ac.squawk
                entry["emergency"] = _squawk_emergency.get(ac.squawk, "none")
            else:
                entry["emergency"] = "none"
            if ac.category:
                entry["category"] = ac.category
            aircraft_list.append(entry)
        total_msgs = receiver.status().get("messages", 0) if receiver else 0
        return {"now": _time.time(), "messages": total_msgs, "aircraft": aircraft_list}

    @router.get("/data/receiver.json")
    def dump1090_receiver_json():
        """dump1090-compatible receiver.json endpoint."""
        loc = config.location
        entry = {"version": "1090toTAK", "refresh": 1000, "history": 0}
        if loc.lat and loc.lon:
            entry["lat"] = loc.lat
            entry["lon"] = loc.lon
        return entry

    @router.get("/data/spectrum.json")
    def spectrum_json():
        """Serve current spectrum data for remote JSON consumers."""
        rx = receiver
        if hasattr(rx, "_receivers"):
            for r in rx._receivers.values():
                if getattr(r, "spectrum", None):
                    rx = r
                    break
        spec = getattr(rx, "spectrum", None) if rx else None
        if not spec:
            return {"bins": [], "center_freq": 0, "sample_rate": 0}
        st = rx.status() if hasattr(rx, "status") else {}
        return {
            "bins": [round(v, 1) for v in spec],
            "center_freq": st.get("frequency", 1_090_000_000),
            "sample_rate": st.get("sample_rate", 2_000_000),
        }

    @router.get("/api/stats")
    def get_stats():
        from config import config_to_dict
        from .events import web_client_status
        from .updater import get_state as _get_update_state
        stats = {
            "total": registry.count(),
            "with_position": registry.count_with_position(),
            "capabilities": {"rtlsdr": HAS_RTLSDR},
        }
        if receiver:
            stats["receiver"] = receiver.status()
        if tak_sender:
            stats["tak"] = tak_sender.status()
        if server_manager:
            stats["servers"] = server_manager.status()
        stats["web"] = {
            "port": config.web.port,
            **web_client_status(),
        }
        stats["update"] = _get_update_state()
        # Location summary for status bar
        from config import LOCATION_NONE, RECEIVER_JSON
        loc = config.location
        if loc.mode != LOCATION_NONE and not (loc.lat == 0.0 and loc.lon == 0.0):
            stats["location"] = {"mode": loc.mode, "lat": loc.lat, "lon": loc.lon}
        elif loc.mode == LOCATION_NONE and config.receiver.type == RECEIVER_JSON and receiver is not None:
            rlat = getattr(receiver, "receiver_lat", None)
            rlon = getattr(receiver, "receiver_lon", None)
            if rlat is not None and rlon is not None:
                stats["location"] = {"mode": "receiver", "lat": rlat, "lon": rlon}
            else:
                stats["location"] = {"mode": "none"}
        else:
            stats["location"] = {"mode": loc.mode if loc.mode != LOCATION_NONE else "none"}
        if _HAS_PSUTIL:
            proc = _psutil.Process()
            mem_info = proc.memory_info()
            stats["system"] = {
                "cpu_pct":      proc.cpu_percent(interval=None),
                "mem_used_mb":  mem_info.rss >> 20,
            }
        else:
            proc_stats = _proc_system_stats()
            if proc_stats:
                stats["system"] = proc_stats
        return stats

    @router.get("/api/config")
    def get_config():
        return config_to_dict(config)

    @router.post("/api/config")
    async def post_config(request: Request):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not data:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        try:
            update_config_from_dict(config, data)
            save_config(config)
            # If receiver settings changed, restart if type changed else reconnect
            if receiver and "receiver" in data:
                from receivers.manager import ReceiverManager
                if isinstance(receiver, ReceiverManager):
                    if receiver.active_type != config.receiver.type:
                        log.info("Receiver type changed %s → %s — restarting",
                                 receiver.active_type, config.receiver.type)
                        receiver.restart()
                        # Re-attach AVR frame sink to new receiver instance
                        if server_manager:
                            server_manager.apply()
                    else:
                        receiver.reconnect()
                        log.info("Receiver config changed — reconnecting")
                else:
                    receiver.reconnect()
                    log.info("Receiver config changed — reconnecting")
            if receiver and "receivers" in data:
                receiver.restart()
                log.info("Multi-receiver config changed — restarting all")
                if server_manager:
                    server_manager.apply()
            if server_manager and "servers" in data:
                server_manager.apply()
                log.info("Server config changed — applied")
            if "aircraft_ttl" in data:
                registry.set_ttl(config.aircraft_ttl)
            if store and "history_ttl" in data:
                store.set_ttl(config.history_ttl)
            if gpsd_client and "location" in data:
                # Clear stale fix when switching away from gpsd
                from config import LOCATION_GPSD
                if config.location.mode != LOCATION_GPSD:
                    config.location.lat = 0.0
                    config.location.lon = 0.0
            log.info("Config updated and saved")
            return {"ok": True, "config": config_to_dict(config)}
        except Exception as e:
            log.error("Config update error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.get("/api/rtlsdr/devices")
    def get_rtlsdr_devices():
        if not HAS_RTLSDR:
            return []
        try:
            try:
                from receivers.rtlsdr_ctypes import _lib as _rtl_lib
            except (ImportError, OSError):
                from rtlsdr import librtlsdr as _rtl_lib
            count = _rtl_lib.rtlsdr_get_device_count()
            devices = []
            for i in range(count):
                try:
                    name = _rtl_lib.rtlsdr_get_device_name(i)
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", errors="replace")
                    name = name.strip() or f"RTL-SDR Device {i}"
                except Exception:
                    name = f"RTL-SDR Device {i}"
                devices.append({"index": i, "name": name})
            return devices
        except Exception as e:
            log.warning("RTL-SDR device enumeration failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.post("/api/rtlsdr/gain/preview")
    async def rtlsdr_gain_preview(request: Request):
        data = await request.json() if await request.body() else {}
        agc  = bool(data.get("agc", False))
        try:
            gain = float(data.get("gain", 49.6))
            if not math.isfinite(gain):
                gain = 49.6
        except (TypeError, ValueError):
            gain = 49.6
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        log.debug("gain preview: receiver=%r, r=%r, has_method=%r",
                  type(receiver).__name__, type(r).__name__ if r else None,
                  hasattr(r, "apply_gain_preview") if r else False)
        if r is None or not hasattr(r, "apply_gain_preview"):
            return {"ok": False, "error": "RTL-SDR not active"}
        ok = r.apply_gain_preview(agc, gain)
        if not ok:
            return {"ok": False, "error": "SDR device not open"}
        return {"ok": ok}

    @router.post("/api/rtlsdr/gain/revert")
    def rtlsdr_gain_revert():
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "revert_gain_preview"):
            return JSONResponse({"ok": False, "error": "RTL-SDR not active"}, status_code=400)
        ok = r.revert_gain_preview()
        return {"ok": ok}

    @router.get("/api/history/range")
    def history_range(end: float = Query(None), start: float = Query(None), step: int = Query(30)):
        import time as _t
        if store is None:
            return {"aircraft": {}, "start": 0, "end": 0, "count": 0}
        now = _t.time()
        if end is None:
            end = now
        if start is None:
            start = end - 86400
        if end - start > 86400:
            start = end - 86400
        data  = store.get_range(start, end, step)
        count = sum(len(v) for v in data.values())
        return {"aircraft": data, "start": start, "end": end, "count": count}

    @router.get("/api/history/{icao}")
    def get_history(icao: str):
        if store is None:
            return []
        return store.get_track(icao.upper())

    @router.get("/api/heatmap")
    def get_heatmap(end: float = Query(None), start: float = Query(None)):
        import time as _t
        if store is None:
            return []
        now = _t.time()
        if end is None:
            end = now
        if start is None:
            start = end - 86400
        if end - start > 86400:
            start = end - 86400
        return store.get_heatmap_cells(start, end)

    @router.get("/api/store/stats")
    def store_stats():
        if store is None:
            return {"row_count": 0, "size_bytes": 0, "db_path": ""}
        return store.stats()

    @router.post("/api/store/reset")
    def store_reset():
        if store is None:
            return {"cleared": 0}
        count = store.clear()
        return {"cleared": count}

    @router.get("/api/store/dashboard")
    def store_dashboard():
        if store is None:
            return {"error": "store not available"}
        return {
            "unique_all_time": store.unique_aircraft_count(),
            "unique_today": store.unique_aircraft_today(),
            "peak_concurrent": registry.peak_count,
            "current_count": registry.count(),
            "top_aircraft": store.top_aircraft(10),
            "hourly_histogram": store.hourly_histogram(),
            "altitude_distribution": store.altitude_distribution(),
            "category_breakdown": store.category_breakdown(),
        }

    @router.get("/api/location")
    def get_location():
        from config import LOCATION_NONE, RECEIVER_JSON
        loc = config.location
        if loc.mode == LOCATION_NONE:
            # Fall back to receiver's own location if using JSON API source
            if config.receiver.type == RECEIVER_JSON and receiver is not None:
                rlat = getattr(receiver, "receiver_lat", None)
                rlon = getattr(receiver, "receiver_lon", None)
                if rlat is not None and rlon is not None:
                    return {"mode": "receiver", "lat": rlat, "lon": rlon}
            return {"mode": "none", "lat": None, "lon": None}
        if loc.lat == 0.0 and loc.lon == 0.0:
            return {"mode": loc.mode, "lat": None, "lon": None}
        return {"mode": loc.mode, "lat": loc.lat, "lon": loc.lon}

    @router.get("/tiles/weather/{source}/{frame}/{z}/{x}/{y}")
    def proxy_weather_tile(source: str, frame: str, z: int, x: int, y: int):
        from .tile_proxy import fetch_weather_tile
        try:
            data, ct = fetch_weather_tile(source, frame, z, x, y, config.web.weather_owm_key)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            log.warning("Weather tile fetch failed %s/%s/%d/%d/%d: %s", source, frame, z, x, y, e)
            raise HTTPException(status_code=502, detail="Tile unavailable")
        return Response(content=data, media_type=ct, headers={"Cache-Control": "public, max-age=600"})

    @router.get("/tiles/{source}/{z}/{x}/{y}")
    def proxy_tile(source: str, z: int, x: int, y: int):
        from .tile_proxy import fetch_tile
        try:
            data, ct = fetch_tile(source, z, x, y)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            log.warning("Tile fetch failed %s/%d/%d/%d: %s", source, z, x, y, e)
            raise HTTPException(status_code=502, detail="Tile unavailable")
        return Response(content=data, media_type=ct, headers={"Cache-Control": "public, max-age=604800"})

    @router.get("/api/tiles/stats")
    def tile_cache_stats():
        from .tile_proxy import cache_stats
        return cache_stats()

    @router.post("/api/tiles/clear")
    def tile_cache_clear():
        from .tile_proxy import clear_cache
        stats = clear_cache()
        log.info("Tile cache cleared: %d tiles, %d bytes", stats["tiles"], stats["bytes"])
        return {"ok": True, **stats}

    @router.post("/api/tak/send/{icao}")
    def tak_send_single(icao: str):
        if not tak_sender:
            return JSONResponse({"ok": False, "error": "TAK sender not running"}, status_code=503)
        ok, reason = tak_sender.send_single(icao.upper())
        return {"ok": ok, "error": reason if not ok else None, "message": reason}

    # ------------------------------------------------------------------
    # Peer update routes — serve this app's files to sibling instances
    # and pull updates from the configured JSON API server.
    # ------------------------------------------------------------------

    @router.get("/api/update/manifest")
    def update_manifest():
        import hashlib as _hl
        from .updater import app_files
        files = []
        for rel, abs_path in app_files():
            with open(abs_path, "rb") as f:
                h = _hl.sha256(f.read()).hexdigest()
            files.append({"path": rel, "hash": h})
        return {"app": "1090toTAK", "files": files}

    @router.get("/api/update/file")
    def update_file_serve(path: str = Query("")):
        import os as _os
        from .updater import safe_abs_path
        if not path:
            raise HTTPException(status_code=400)
        try:
            abs_path = safe_abs_path(path)
        except ValueError:
            raise HTTPException(status_code=403)
        if not _os.path.isfile(abs_path):
            raise HTTPException(status_code=404)
        return FileResponse(abs_path, media_type="text/plain")

    @router.get("/api/update/check")
    def update_check():
        from .updater import check_for_updates, check_for_updates_github, get_state
        if config.update.source == "github":
            changed = check_for_updates_github()
        else:
            host = config.update.host
            if not host:
                return {"available": False, "reason": "no update server configured"}
            changed = check_for_updates(host, config.update.port)
        if changed is None:
            s = get_state()
            return {"available": False, "error": s.get("error"), **s}
        return {"available": bool(changed), "files": changed, "total": len(changed)}

    @router.post("/api/update/pull")
    async def update_pull(request: Request):
        import os as _os
        import urllib.request as _urlreq
        import urllib.parse as _urlparse
        from .updater import safe_abs_path, _fmt_error, GITHUB_REPO, GITHUB_BRANCH
        host = config.update.host
        port = config.update.port
        is_github = config.update.source == "github"
        if not is_github and not host:
            return {"ok": False, "error": "no update server configured"}
        try:
            data = await request.json()
        except Exception:
            data = {}
        files_to_pull = data.get("files", [])
        results = []
        for path in files_to_pull:
            try:
                abs_path = safe_abs_path(path)
            except ValueError as e:
                results.append({"path": path, "ok": False, "error": str(e)})
                continue
            if is_github:
                url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{_urlparse.quote(path, safe='/')}"
            else:
                url = f"http://{host}:{port}/api/update/file?{_urlparse.urlencode({'path': path})}"
            try:
                with _urlreq.urlopen(url, timeout=10) as resp:
                    content = resp.read()
                tmp = abs_path + ".tmp"
                _os.makedirs(_os.path.dirname(abs_path), exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(content)
                _os.replace(tmp, abs_path)
                results.append({"path": path, "ok": True})
            except Exception as e:
                results.append({"path": path, "ok": False, "error": _fmt_error(e, url)})
        return {"ok": all(r["ok"] for r in results), "results": results}

    @router.post("/api/restart")
    def restart_app():
        import os as _os
        import sys as _sys
        import subprocess as _sp
        import threading as _th

        def _do_restart():
            import time as _t
            _t.sleep(0.5)
            _sp.Popen([_sys.executable] + _sys.argv)
            _os._exit(0)

        _th.Thread(target=_do_restart, daemon=True).start()
        return {"ok": True}

    return router
