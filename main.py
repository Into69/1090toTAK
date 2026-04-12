#!/usr/bin/env python3
"""
1090toTAK — RTL-SDR ADS-B receiver with web display and TAK output.

Usage:
  python main.py [options]

Options:
  --receiver {sbs,avr,rtlsdr}   ADS-B source type (default: sbs)
  --host HOST                    dump1090 host (default: 127.0.0.1)
  --sbs-port PORT                SBS TCP port (default: 30003)
  --tak-host HOST                TAK server host
  --tak-port PORT                TAK server port
  --tak-protocol {udp,multicast,tcp}
  --tak-enable                   Enable TAK output on startup
  --web-port PORT                Web server port (default: 8080)
  --ttl SECONDS                  Aircraft TTL in seconds (default: 60)
  --debug                        Enable debug logging
"""

import argparse
import logging
import socket
import subprocess
import sys
import os
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))


def _port_in_use(host: str, port: int) -> bool:
    bind_host = "" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind_host, port))
        except OSError:
            return True
    return False


def _pids_on_port(port: int) -> list:
    """Return list of PIDs listening on the given TCP port (Windows/Linux/macOS)."""
    pids = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, check=False,
            ).stdout
            needle = f":{port}"
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(needle) \
                        and parts[3] == "LISTENING":
                    try:
                        pids.add(int(parts[4]))
                    except ValueError:
                        pass
        else:
            out = subprocess.run(
                ["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
                capture_output=True, text=True, check=False,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
    except FileNotFoundError:
        pass
    return sorted(pids)


def _kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            rc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, text=True, check=False,
            ).returncode
            return rc == 0
        else:
            os.kill(pid, 9)
            return True
    except Exception:
        return False


def free_port(host: str, port: int, log: logging.Logger) -> None:
    """If `port` is in use, locate the listening process and kill it."""
    if not _port_in_use(host, port):
        return
    my_pid = os.getpid()
    pids = [p for p in _pids_on_port(port) if p != my_pid]
    if not pids:
        log.warning("Port %d is in use but no owning PID found — startup may fail", port)
        return
    for pid in pids:
        log.warning("Port %d is held by PID %d — killing it", port, pid)
        if not _kill_pid(pid):
            log.error("Failed to kill PID %d — you may need to kill it manually", pid)
    # Give the OS a moment to release the socket
    for _ in range(20):
        if not _port_in_use(host, port):
            return
        time.sleep(0.1)
    log.warning("Port %d still in use after kill attempt", port)


def parse_args():
    p = argparse.ArgumentParser(
        description="1090toTAK — ADS-B to map + TAK",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--receiver", choices=["sbs", "avr", "rtlsdr"],
                   help="ADS-B source type")
    p.add_argument("--host", help="dump1090 / receiver host")
    p.add_argument("--sbs-port", type=int, help="SBS TCP port")
    p.add_argument("--avr-port", type=int, help="AVR TCP port")
    p.add_argument("--tak-host", help="TAK server host")
    p.add_argument("--tak-port", type=int, help="TAK server port")
    p.add_argument("--tak-protocol", choices=["udp", "multicast", "tcp"],
                   help="TAK protocol")
    p.add_argument("--tak-enable", action="store_true",
                   help="Enable TAK output on startup")
    p.add_argument("--web-port", type=int, help="Web server port")
    p.add_argument("--ttl", type=int, help="Aircraft TTL seconds")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p.parse_args()


def apply_cli(config, args):
    if args.receiver:
        config.receiver.type = args.receiver
    if args.host:
        config.receiver.host = args.host
    if args.sbs_port:
        config.receiver.sbs_port = args.sbs_port
    if args.avr_port:
        config.receiver.avr_port = args.avr_port
    if args.tak_host:
        config.tak.host = args.tak_host
    if args.tak_port:
        config.tak.port = args.tak_port
    if args.tak_protocol:
        config.tak.protocol = args.tak_protocol
    if args.tak_enable:
        config.tak.enabled = True
    if args.web_port:
        config.web.port = args.web_port
    if args.ttl:
        config.aircraft_ttl = args.ttl


def main():
    args = parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")

    # ── Load config ──────────────────────────────────────────
    from config import load_config
    config = load_config()
    apply_cli(config, args)

    # ── Aircraft registry ─────────────────────────────────────
    from aircraft.registry import AircraftRegistry
    registry = AircraftRegistry(ttl=config.aircraft_ttl)
    registry.start_expiry_thread()

    # ── Position history store ────────────────────────────────
    from aircraft.store import AircraftStore
    store = AircraftStore(history_ttl=config.history_ttl)
    store.start_purge_thread()
    registry.on_update(store.record)

    # ── ADS-B receiver ────────────────────────────────────────
    if config.receivers:
        from receivers.manager import MultiReceiverManager
        receiver = MultiReceiverManager(config, registry)
    else:
        from receivers.manager import ReceiverManager
        receiver = ReceiverManager(config, registry)
    receiver.start()

    # ── TAK sender ────────────────────────────────────────────
    from tak.tak_sender import TAKSender
    tak_sender = TAKSender(config, registry)
    tak_sender.start()

    if config.tak.enabled:
        log.info("TAK: %s → %s:%d (every %.0fs)",
                 config.tak.protocol, config.tak.host,
                 config.tak.port, config.tak.interval)
    else:
        log.info("TAK: disabled (configure via web UI or --tak-enable)")

    # ── Output servers (SBS / AVR rebroadcast) ────────────────
    from servers.output_servers import ServerManager
    server_manager = ServerManager(config, registry, receiver)
    server_manager.apply()

    # ── Location / gpsd ──────────────────────────────────────
    from location.gpsd_client import GpsdClient
    gpsd = GpsdClient(config)
    gpsd.start()

    # ── Web server ────────────────────────────────────────────
    from web.server import create_app
    app = create_app(config, registry, tak_sender, receiver, server_manager, store, gpsd)

    log.info("Web UI: http://localhost:%d", config.web.port)
    log.info("Press Ctrl+C to stop")

    free_port(config.web.host, config.web.port, log)

    import uvicorn
    try:
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="warning")
    except KeyboardInterrupt:
        log.info("Shutting down...")
        receiver.stop()
        tak_sender.stop()
        server_manager.stop()


if __name__ == "__main__":
    main()
