# 1090toTAK

ADS-B receiver gateway with a live web map and TAK (Team Awareness Kit) output. Receives Mode S/ADS-B data from a variety of sources and displays aircraft on a Leaflet map in real time, with optional forwarding to TAK servers via Cursor on Target (CoT).

## Features

- **Live web map** — Leaflet-based UI with aircraft markers, trails, altitude colour coding, click-to-select, and multiple map tile providers
- **History & heatmap** — 24-hour playback and heatmap visualisation (SQLite-backed)
- **TAK integration** — CoT XML output over UDP unicast, multicast, or TCP
- **Multiple receiver sources** — SBS (port 30003), AVR (port 30002), Beast (port 30005), RTL-SDR direct, and external JSON feeds
- **Weather overlay** — RainViewer precipitation radar, NASA GIBS GOES-East IR satellite, and OpenWeatherMap layers, with automatic tile-retry on transient failures
- **Spectrum display** — Real-time FFT spectrum canvas for RTL-SDR
- **Sensor location** — Manual coordinates or live GPSD tracking
- **App updates** — Pull updates from a peer instance or directly from GitHub
- **Hot-reload config** — All settings adjustable at runtime through the web UI

## Requirements

- Python 3.10+
- An ADS-B data source (dump1090, **readsb (recommended)**, RTL-SDR dongle, or network feed)

### Recommended decoder: readsb

For the best decode sensitivity and message rate, run [**readsb**](https://github.com/wiedehopf/readsb) on the host attached to your SDR and point 1090toTAK at it via the JSON receiver type. readsb's C-based demodulator recovers significantly more frames in marginal conditions than the built-in Python decoder (`type: rtlsdr`), and provides richer fields (emitter category, MLAT positions when fed by a network) through its `aircraft.json` API.

```bash
# Install readsb (Debian/Ubuntu/Pi):
sudo bash -c "$(wget -nv -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"

# Then in 1090toTAK settings, set:
#   Receiver type: JSON API
#   Host: 127.0.0.1   (or readsb host IP)
#   JSON port: 8080   (readsb's tar1090 web port)
```

The built-in `rtlsdr` receiver is convenient (no extra services), but a C decoder will catch ~20–40% more frames in the same RF environment.

## Installation

### One-line install (Linux / macOS / WSL)

Downloads the installer, makes it executable, and runs it. The installer clones the repo, creates a Python venv, installs requirements, and offers to install [readsb](https://github.com/wiedehopf/readsb).

```bash
curl -fsSLO https://raw.githubusercontent.com/Into69/1090toTAK/main/install.1090toTAK.sh && chmod +x install.1090toTAK.sh && ./install.1090toTAK.sh
```

### Manual install

```bash
git clone https://github.com/Into69/1090toTAK.git
cd 1090toTAK
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Core dependencies

| Package | Purpose |
|---------|---------|
| fastapi | Web framework |
| uvicorn | ASGI server |
| jinja2 | HTML templating |
| pyModeS | ADS-B decoding |
| psutil | CPU/RAM monitoring |

### Optional dependencies

| Package | Purpose |
|---------|---------|
| pyrtlsdr | Direct RTL-SDR dongle support |

The app detects available hardware libraries at startup and enables receiver types accordingly.

## Usage

```bash
./1090toTAK.sh        # auto-activates the venv created by the installer
# or, with the venv already active:
python 1090toTAK.py
```

The web UI is available at **http://localhost:8080**.

### Command-line options

```
--receiver {sbs,avr,rtlsdr}    ADS-B source type (default: sbs)
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

Additional source types (`beast`, `json`) are configured through the web UI / `config.json`.

All options can also be set through the web UI settings panel and are persisted to `config.json`.

## Receiver Sources

| Source | Type key | Notes |
|--------|----------|-------|
| SBS TCP | `sbs` | dump1090 BaseStation format on port 30003 |
| AVR raw | `avr` | dump1090 raw format on port 30002 |
| Beast binary | `beast` | dump1090 / readsb binary frames on port 30005 |
| RTL-SDR | `rtlsdr` | Direct via pyrtlsdr with Mode S preamble detection |
| JSON API | `json` | External [readsb](https://github.com/wiedehopf/readsb) (recommended) or dump1090/tar1090 JSON feed |

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
├── 1090toTAK.py         # Entry point
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
│   ├── beast_receiver.py   # Beast binary (port 30005)
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
    ├── server.py        # FastAPI app factory
    ├── routes.py        # REST API endpoints
    ├── events.py        # WebSocket broadcast loop
    ├── updater.py       # Peer and GitHub update logic
    ├── tile_proxy.py    # Map tile caching proxy
    └── templates/
        └── index.html   # Full Leaflet web UI
```

## License

See repository for license details.
