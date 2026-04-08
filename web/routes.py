import logging
import math
from flask import Blueprint, jsonify, request, render_template, current_app, send_from_directory, make_response, Response

from config import AppConfig, update_config_from_dict, save_config, config_to_dict
from aircraft.registry import AircraftRegistry
from capabilities import HAS_RTLSDR, HAS_HACKRF, HAS_UHD, probe_gpsd

log = logging.getLogger(__name__)


def register_routes(app, config: AppConfig, registry: AircraftRegistry, tak_sender=None, receiver=None, server_manager=None, store=None, gpsd_client=None):

    @app.route("/")
    def index():
        from version import __version__
        return render_template(
            "index.html",
            config=config,
            has_rtlsdr=HAS_RTLSDR,
            has_hackrf=HAS_HACKRF,
            has_usrp=HAS_UHD,
            has_gpsd=probe_gpsd(config.location.gpsd_host, config.location.gpsd_port),
            version=__version__,
        )

    @app.route("/api/gpsd/probe")
    def gpsd_probe():
        host = request.args.get("host", config.location.gpsd_host)
        port = int(request.args.get("port", config.location.gpsd_port))
        return jsonify({"available": probe_gpsd(host, port)})

    @app.route("/tile-sw.js")
    def tile_sw():
        import os
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        resp = make_response(send_from_directory(static_dir, "tile-sw.js"))
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/api/aircraft")
    def get_aircraft():
        return jsonify(registry.get_all_dicts())

    @app.route("/data/aircraft.json")
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
        return jsonify({"now": _time.time(), "messages": total_msgs, "aircraft": aircraft_list})

    @app.route("/data/receiver.json")
    def dump1090_receiver_json():
        """dump1090-compatible receiver.json endpoint."""
        loc = config.location
        entry = {"version": "1090toTAK", "refresh": 1000, "history": 0}
        if loc.lat and loc.lon:
            entry["lat"] = loc.lat
            entry["lon"] = loc.lon
        return jsonify(entry)

    @app.route("/api/stats")
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
        try:
            import os as _os, psutil as _psutil
            _proc = _psutil.Process(_os.getpid())
            stats["system"] = {
                "cpu_pct":    _proc.cpu_percent(interval=None),
                "mem_used_mb": _proc.memory_info().rss >> 20,
            }
        except Exception:
            pass
        return jsonify(stats)

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(config_to_dict(config))

    @app.route("/api/config", methods=["POST"])
    def post_config():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid JSON"}), 400
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
            return jsonify({"ok": True, "config": config_to_dict(config)})
        except Exception as e:
            log.error("Config update error: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/rtlsdr/devices")
    def get_rtlsdr_devices():
        if not HAS_RTLSDR:
            return jsonify([])
        try:
            from rtlsdr import librtlsdr
            count = librtlsdr.rtlsdr_get_device_count()
            devices = []
            for i in range(count):
                try:
                    name = librtlsdr.rtlsdr_get_device_name(i)
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", errors="replace")
                    name = name.strip() or f"RTL-SDR Device {i}"
                except Exception:
                    name = f"RTL-SDR Device {i}"
                devices.append({"index": i, "name": name})
            return jsonify(devices)
        except Exception as e:
            log.warning("RTL-SDR device enumeration failed: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/rtlsdr/gain/preview", methods=["POST"])
    def rtlsdr_gain_preview():
        data = request.get_json(silent=True) or {}
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
            return jsonify({"ok": False, "error": "RTL-SDR not active"})
        ok = r.apply_gain_preview(agc, gain)
        if not ok:
            return jsonify({"ok": False, "error": "SDR device not open"})
        return jsonify({"ok": ok})

    @app.route("/api/rtlsdr/gain/revert", methods=["POST"])
    def rtlsdr_gain_revert():
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "revert_gain_preview"):
            return jsonify({"ok": False, "error": "RTL-SDR not active"}), 400
        ok = r.revert_gain_preview()
        return jsonify({"ok": ok})

    @app.route("/api/hackrf/gain/preview", methods=["POST"])
    def hackrf_gain_preview():
        data = request.get_json(silent=True) or {}
        try:
            lna = int(data.get("lna_gain", 16))
            vga = int(data.get("vga_gain", 20))
            amp = bool(data.get("amp", False))
            # Clamp to valid ranges and step sizes
            lna = max(0, min(40, round(lna / 8) * 8))
            vga = max(0, min(62, round(vga / 2) * 2))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid gain values"}), 400
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "apply_gain_preview") or r.__class__.__name__ != "HackRFReceiver":
            return jsonify({"ok": False, "error": "HackRF not active"})
        ok = r.apply_gain_preview(lna, vga, amp)
        if not ok:
            return jsonify({"ok": False, "error": "HackRF device not open"})
        return jsonify({"ok": True})

    @app.route("/api/hackrf/gain/revert", methods=["POST"])
    def hackrf_gain_revert():
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "revert_gain_preview") or r.__class__.__name__ != "HackRFReceiver":
            return jsonify({"ok": False, "error": "HackRF not active"}), 400
        ok = r.revert_gain_preview()
        return jsonify({"ok": ok})

    @app.route("/api/usrp/gain/preview", methods=["POST"])
    def usrp_gain_preview():
        data = request.get_json(silent=True) or {}
        try:
            gain = float(data.get("gain", 40.0))
            gain = max(0.0, min(76.0, gain))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid gain value"}), 400
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "apply_gain_preview") or r.__class__.__name__ != "USRPReceiver":
            return jsonify({"ok": False, "error": "USRP not active"})
        ok = r.apply_gain_preview(gain)
        if not ok:
            return jsonify({"ok": False, "error": "USRP device not open"})
        return jsonify({"ok": True})

    @app.route("/api/usrp/gain/revert", methods=["POST"])
    def usrp_gain_revert():
        r = getattr(receiver, "_receiver", receiver) if receiver else None
        if r is None or not hasattr(r, "revert_gain_preview") or r.__class__.__name__ != "USRPReceiver":
            return jsonify({"ok": False, "error": "USRP not active"}), 400
        ok = r.revert_gain_preview()
        return jsonify({"ok": ok})

    @app.route("/api/history/<icao>")
    def get_history(icao):
        if store is None:
            return jsonify([])
        return jsonify(store.get_track(icao.upper()))

    @app.route("/api/history/range")
    def history_range():
        import time as _t
        if store is None:
            return jsonify({"aircraft": {}, "start": 0, "end": 0, "count": 0})
        now = _t.time()
        try:
            end   = float(request.args.get("end",   now))
            start = float(request.args.get("start", end - 86400))
            step  = int(request.args.get("step", 30))
        except ValueError:
            return jsonify({"error": "bad params"}), 400
        if end - start > 86400:
            start = end - 86400
        data  = store.get_range(start, end, step)
        count = sum(len(v) for v in data.values())
        return jsonify({"aircraft": data, "start": start, "end": end, "count": count})

    @app.route("/api/heatmap")
    def get_heatmap():
        import time as _t
        if store is None:
            return jsonify([])
        now = _t.time()
        try:
            end   = float(request.args.get("end",   now))
            start = float(request.args.get("start", end - 86400))
        except ValueError:
            return jsonify({"error": "bad params"}), 400
        if end - start > 86400:
            start = end - 86400
        return jsonify(store.get_heatmap_cells(start, end))

    @app.route("/api/store/stats")
    def store_stats():
        if store is None:
            return jsonify({"row_count": 0, "size_bytes": 0, "db_path": ""})
        return jsonify(store.stats())

    @app.route("/api/store/reset", methods=["POST"])
    def store_reset():
        if store is None:
            return jsonify({"cleared": 0})
        count = store.clear()
        return jsonify({"cleared": count})

    @app.route("/api/location")
    def get_location():
        from config import LOCATION_NONE, RECEIVER_JSON
        loc = config.location
        if loc.mode == LOCATION_NONE:
            # Fall back to receiver's own location if using JSON API source
            if config.receiver.type == RECEIVER_JSON and receiver is not None:
                rlat = getattr(receiver, "receiver_lat", None)
                rlon = getattr(receiver, "receiver_lon", None)
                if rlat is not None and rlon is not None:
                    return jsonify({"mode": "receiver", "lat": rlat, "lon": rlon})
            return jsonify({"mode": "none", "lat": None, "lon": None})
        if loc.lat == 0.0 and loc.lon == 0.0:
            return jsonify({"mode": loc.mode, "lat": None, "lon": None})
        return jsonify({"mode": loc.mode, "lat": loc.lat, "lon": loc.lon})

    @app.route("/tiles/<source>/<int:z>/<int:x>/<int:y>")
    def proxy_tile(source, z, x, y):
        from .tile_proxy import fetch_tile
        try:
            data, ct = fetch_tile(source, z, x, y)
        except ValueError as e:
            return str(e), 404
        except Exception as e:
            log.warning("Tile fetch failed %s/%d/%d/%d: %s", source, z, x, y, e)
            return "Tile unavailable", 502
        resp = Response(data, content_type=ct)
        resp.headers["Cache-Control"] = "public, max-age=604800"  # 7 days browser cache
        return resp

    @app.route("/api/tiles/stats")
    def tile_cache_stats():
        from .tile_proxy import cache_stats
        return jsonify(cache_stats())

    @app.route("/api/tiles/clear", methods=["POST"])
    def tile_cache_clear():
        from .tile_proxy import clear_cache
        stats = clear_cache()
        log.info("Tile cache cleared: %d tiles, %d bytes", stats["tiles"], stats["bytes"])
        return jsonify({"ok": True, **stats})

    @app.route("/api/tak/send/<icao>", methods=["POST"])
    def tak_send_single(icao):
        if not tak_sender:
            return jsonify({"ok": False, "error": "TAK sender not running"}), 503
        ok, reason = tak_sender.send_single(icao.upper())
        return jsonify({"ok": ok, "error": reason if not ok else None, "message": reason})

    # ------------------------------------------------------------------
    # Peer update routes — serve this app's files to sibling instances
    # and pull updates from the configured JSON API server.
    # ------------------------------------------------------------------

    @app.route("/api/update/manifest")
    def update_manifest():
        import hashlib as _hl
        from .updater import app_files
        files = []
        for rel, abs_path in app_files():
            with open(abs_path, "rb") as f:
                h = _hl.sha256(f.read()).hexdigest()
            files.append({"path": rel, "hash": h})
        return jsonify({"app": "1090toTAK", "files": files})

    @app.route("/api/update/file")
    def update_file_serve():
        import os as _os
        from flask import abort, send_file
        from .updater import safe_abs_path
        path = request.args.get("path", "")
        if not path:
            abort(400)
        try:
            abs_path = safe_abs_path(path)
        except ValueError:
            abort(403)
        if not _os.path.isfile(abs_path):
            abort(404)
        return send_file(abs_path, mimetype="text/plain")

    @app.route("/api/update/check")
    def update_check():
        from .updater import check_for_updates, get_state
        host = config.update.host
        if not host:
            return jsonify({"available": False, "reason": "no update server configured"})
        changed = check_for_updates(host, config.update.port)
        if changed is None:
            s = get_state()
            return jsonify({"available": False, "error": s.get("error"), **s})
        return jsonify({"available": bool(changed), "files": changed, "total": len(changed)})

    @app.route("/api/update/pull", methods=["POST"])
    def update_pull():
        import os as _os
        import urllib.request as _urlreq
        import urllib.parse as _urlparse
        from .updater import safe_abs_path, _fmt_error
        host = config.update.host
        if not host:
            return jsonify({"ok": False, "error": "no update server configured"})
        port = config.update.port
        data = request.get_json(force=True) or {}
        files_to_pull = data.get("files", [])
        results = []
        for path in files_to_pull:
            try:
                abs_path = safe_abs_path(path)
            except ValueError as e:
                results.append({"path": path, "ok": False, "error": str(e)})
                continue
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
        return jsonify({"ok": all(r["ok"] for r in results), "results": results})
        return jsonify({"ok": all(r["ok"] for r in results), "results": results})

    @app.route("/api/restart", methods=["POST"])
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
        return jsonify({"ok": True})
