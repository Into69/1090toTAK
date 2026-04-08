# 1090toTAK

ADS-B receiver gateway with a live web map and TAK (Team Awareness Kit) output. Receives Mode S/ADS-B data from a variety of sources and displays aircraft on a Leaflet map in real time, with optional forwarding to TAK servers via Cursor on Target (CoT).

## Features

- **Live web map** — Leaflet-based UI with aircraft markers, trails, altitude colour coding, click-to-select, and multiple map tile providers
- **History & heatmap** — 24-hour playback and heatmap visualisation (SQLite-backed)
- **TAK integration** — CoT XML output over UDP unicast, multicast, or TCP
- **Multiple receiver sources** — SBS (port 30003), AVR (port 30002), RTL-SDR direct, HackRF One, USRP B205mini, and external JSON feeds
- **Spectrum display** — Real-time FFT spectrum canvas for RTL-SDR and HackRF
- **Sensor location** — Manual coordinates or live GPSD tracking
- **App updates** — Pull updates from a peer instance or directly from GitHub
- **Hot-reload config** — All settings adjustable at runtime through the web UI

## Requirements

- Python 3.10+
- An ADS-B data source (dump1090, RTL-SDR dongle, HackRF, USRP, or network feed)

## Installation

```bash
git clone https://github.com/Into69/1090toTAK.git
cd 1090toTAK
pip install -r requirements.txt
```

### Core dependencies

| Package | Purpose |
|---------|---------|
| flask | Web framework |
| flask-socketio | Real-time aircraft updates |
| waitress | Production WSGI server |
| pyModeS | ADS-B decoding |
| psutil | CPU/RAM monitoring |

### Optional dependencies

| Package | Purpose |
|---------|---------|
| pyrtlsdr | Direct RTL-SDR dongle support |
| hackrf | HackRF One support |
| uhd | USRP B205mini/B206mini support |

The app detects available hardware libraries at startup and enables receiver types accordingly.

## Usage

```bash
python main.py
```

The web UI is available at **http://localhost:8080**.

### Command-line options

```
--receiver {sbs,avr,rtlsdr}   ADS-B source type (default: sbs)
--host HOST                    dump1090 / receiver host (default: 127.0.0.1)
--sbs-port PORT                SBS TCP port (default: 30003)
--avr-port PORT                AVR TCP port (default: 30002)
--tak-host HOST                TAK server host
--tak-port PORT                TAK server port
--tak-protocol {udp,multicast,tcp}
--tak-enable                   Enable TAK output on startup
--web-port PORT                Web server port (default: 8080)
--ttl SECONDS                  Aircraft TTL in seconds (default: 60)
--debug                        Enable debug logging
```

All options can also be set through the web UI settings panel and are persisted to `config.json`.

## Receiver Sources

| Source | Type key | Notes |
|--------|----------|-------|
| SBS TCP | `sbs` | dump1090 BaseStation format on port 30003 |
| AVR raw | `avr` | dump1090 raw format on port 30002 |
| RTL-SDR | `rtlsdr` | Direct via pyrtlsdr with Mode S preamble detection |
| HackRF One | `hackrf` | Via hackrf library, shares RTL-SDR IQ pipeline |
| USRP | `usrp` | Ettus B205mini/B206mini via UHD |
| JSON API | `json` | External dump1090/readsb JSON feed |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/aircraft` | Current aircraft list |
| GET | `/api/stats` | Receiver status, TAK, system stats |
| POST | `/api/config` | Update configuration (hot-reload) |
| GET | `/api/update/check` | Check for available updates |
| POST | `/api/update/pull` | Download updated files |
| GET | `/data/aircraft.json` | dump1090-compatible output |

## Project Structure

```
1090toTAK/
├── main.py              # Entry point
├── config.py            # Configuration dataclasses and persistence
├── capabilities.py      # Runtime hardware detection
├── version.py           # Version string
├── aircraft/
│   ├── models.py        # Aircraft dataclass
│   ├── registry.py      # Thread-safe aircraft store
│   └── store.py         # SQLite history storage
├── receivers/
│   ├── base.py          # Base receiver class
│   ├── sbs_receiver.py  # SBS TCP (port 30003)
│   ├── avr_receiver.py  # AVR raw (port 30002)
│   ├── rtlsdr_receiver.py  # Direct RTL-SDR
│   ├── hackrf_receiver.py  # HackRF One
│   ├── usrp_receiver.py    # USRP B205mini
│   ├── json_receiver.py    # External JSON feed
│   └── manager.py       # Receiver type switching
├── tak/
│   ├── cot_builder.py   # Cursor on Target XML
│   └── tak_sender.py    # UDP/multicast/TCP sender
├── servers/
│   └── output_servers.py  # SBS/AVR re-broadcast servers
├── location/
│   └── gpsd_client.py   # GPSD integration
└── web/
    ├── server.py        # Flask app factory
    ├── routes.py        # REST API endpoints
    ├── events.py        # Socket.IO broadcast loop
    ├── updater.py       # Peer and GitHub update logic
    ├── tile_proxy.py    # Map tile caching proxy
    └── templates/
        └── index.html   # Full Leaflet web UI
```

## License

See repository for license details.
