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
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))


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
    # Suppress noisy loggers
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)

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
    app, _ = create_app(config, registry, tak_sender, receiver, server_manager, store, gpsd)

    log.info("Web UI: http://localhost:%d", config.web.port)
    log.info("Press Ctrl+C to stop")

    from waitress import serve
    try:
        serve(app, host=config.web.host, port=config.web.port, threads=32)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        receiver.stop()
        tak_sender.stop()
        server_manager.stop()


if __name__ == "__main__":
    main()
