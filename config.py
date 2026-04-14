import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

RECEIVER_SBS           = "sbs"            # dump1090 TCP port 30003 (BaseStation format)
RECEIVER_AVR           = "avr"            # dump1090 TCP port 30002 (AVR raw frames)
RECEIVER_AVR_SUBPROCESS = "avr_subprocess" # spawn rtl_adsb, read AVR frames from stdout
RECEIVER_RTLSDR        = "rtlsdr"         # Direct RTL-SDR via built-in ctypes / librtlsdr
RECEIVER_JSON          = "json"           # dump1090 / tar1090 HTTP JSON API
RECEIVER_HACKRF        = "hackrf"         # Direct HackRF One via hackrf library
RECEIVER_USRP          = "usrp"           # Direct USRP B205mini/B206mini via uhd

TAK_UDP       = "udp"
TAK_MULTICAST = "multicast"
TAK_TCP       = "tcp"

MAP_OSM          = "osm"
MAP_SATELLITE    = "satellite"
MAP_DARK         = "dark"
MAP_TOPO         = "topo"
MAP_GOOGLE_HYBRID  = "google_hybrid"
MAP_GOOGLE_ROADS   = "google_roads"
MAP_GOOGLE_TERRAIN = "google_terrain"
MAP_ESRI_STREET    = "esri_street"
MAP_ESRI_TOPO      = "esri_topo"
MAP_ESRI_NATGEO    = "esri_natgeo"


@dataclass
class ReceiverConfig:
    type: str = RECEIVER_SBS
    host: str = "127.0.0.1"
    sbs_port: int = 30003
    avr_port: int = 30002
    json_port: int = 8080
    rtlsdr_gain: float = 49.6
    rtlsdr_agc: bool = False
    rtlsdr_ppm: int = 0
    rtlsdr_device_index: int = 0
    rtlsdr_bias_tee: bool = False   # only meaningful on RTL-SDR Blog V3/V4
    hackrf_device_index: int = 0
    hackrf_lna_gain: int = 16     # 0-40 dB, steps of 8
    hackrf_vga_gain: int = 20     # 0-62 dB, steps of 2
    hackrf_amp: bool = False      # built-in amplifier (~14 dB)
    hackrf_ppm: int = 0
    usrp_serial: str = ""         # empty = first found B2xx device
    usrp_gain: float = 40.0       # 0-76 dB, continuous
    usrp_antenna: str = "RX2"     # "RX2" or "TX/RX"
    usrp_ppm: int = 0


@dataclass
class TAKConfig:
    enabled: bool = False
    protocol: str = TAK_UDP
    host: str = "239.2.3.1"
    port: int = 6969
    interval: float = 5.0


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    map_type: str = MAP_OSM
    auto_view: bool = False
    zoom_adjust: int = 0
    icon_type: str = "arrow"  # "arrow" | "plane" | "heli" | "dot" | "milsymbol"
    range_rings: bool = False
    range_rings_nm: str = "25,50,100,150,200"
    range_rings_opacity: float = 0.6
    range_rings_color: str = "#4e8fd6"
    range_rings_units: str = "nm"
    weather_overlay: bool = False
    weather_layer: str = "radar"           # radar | satellite | owm_precipitation | owm_clouds | owm_temp
    weather_opacity: float = 0.5
    weather_owm_key: str = ""              # OpenWeatherMap API key (required for owm_* layers)


@dataclass
class ServersConfig:
    sbs_enabled: bool = False
    sbs_port: int = 30003
    avr_enabled: bool = False
    avr_port: int = 30002


@dataclass
class UpdateConfig:
    source: str = "custom"   # "custom" | "github"
    host: str = ""
    port: int = 8080


LOCATION_NONE   = "none"
LOCATION_MANUAL = "manual"
LOCATION_GPSD   = "gpsd"


@dataclass
class LocationConfig:
    mode: str = LOCATION_NONE   # "none" | "manual" | "gpsd"
    lat: float = 0.0
    lon: float = 0.0
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947


@dataclass
class AlertConfig:
    enabled: bool = True
    auto_select: bool = False
    emergency_squawks: bool = True
    rules: list = field(default_factory=list)  # [{name, type, value, enabled}]


@dataclass
class AppConfig:
    receiver: ReceiverConfig = field(default_factory=ReceiverConfig)
    tak: TAKConfig = field(default_factory=TAKConfig)
    web: WebConfig = field(default_factory=WebConfig)
    servers: ServersConfig = field(default_factory=ServersConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    receivers: list = field(default_factory=list)  # [{id, label, enabled, type, host, ...}]
    aircraft_ttl: int = 60
    history_ttl: int = 86400


def _merge(defaults: dict, overrides: dict) -> dict:
    result = dict(defaults)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _filter_fields(cls, data: dict) -> dict:
    """Filter dict to only include keys that are valid fields of the dataclass."""
    valid = set(cls.__dataclass_fields__)
    return {k: v for k, v in data.items() if k in valid}


def load_config() -> AppConfig:
    defaults = asdict(AppConfig())
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                user = json.load(f)
            merged = _merge(defaults, user)
        except Exception:
            merged = defaults
    else:
        merged = defaults

    cfg = AppConfig(
        receiver=ReceiverConfig(**_filter_fields(ReceiverConfig, merged["receiver"])),
        tak=TAKConfig(**_filter_fields(TAKConfig, merged["tak"])),
        web=WebConfig(**_filter_fields(WebConfig, merged["web"])),
        servers=ServersConfig(**_filter_fields(ServersConfig, merged["servers"])),
        update=UpdateConfig(**_filter_fields(UpdateConfig, merged.get("update", {}))),
        location=LocationConfig(**_filter_fields(LocationConfig, merged.get("location", {}))),
        alerts=AlertConfig(**_filter_fields(AlertConfig, merged.get("alerts", {}))),
        receivers=merged.get("receivers", []),
        aircraft_ttl=merged["aircraft_ttl"],
        history_ttl=merged["history_ttl"],
    )
    return cfg


def save_config(cfg: AppConfig) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(cfg), f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def config_to_dict(cfg: AppConfig) -> dict:
    return asdict(cfg)


def update_config_from_dict(cfg: AppConfig, data: dict) -> None:
    if "receiver" in data:
        r = data["receiver"]
        for k, v in r.items():
            if hasattr(cfg.receiver, k):
                setattr(cfg.receiver, k, v)
    if "tak" in data:
        t = data["tak"]
        for k, v in t.items():
            if hasattr(cfg.tak, k):
                setattr(cfg.tak, k, v)
    if "web" in data:
        w = data["web"]
        for k, v in w.items():
            if hasattr(cfg.web, k):
                setattr(cfg.web, k, v)
    if "servers" in data:
        s = data["servers"]
        for k, v in s.items():
            if hasattr(cfg.servers, k):
                setattr(cfg.servers, k, v)
    if "update" in data:
        u = data["update"]
        for k, v in u.items():
            if hasattr(cfg.update, k):
                setattr(cfg.update, k, v)
    if "location" in data:
        loc = data["location"]
        for k, v in loc.items():
            if hasattr(cfg.location, k):
                setattr(cfg.location, k, v)
    if "alerts" in data:
        a = data["alerts"]
        for k, v in a.items():
            if hasattr(cfg.alerts, k):
                setattr(cfg.alerts, k, v)
    if "aircraft_ttl" in data:
        cfg.aircraft_ttl = int(data["aircraft_ttl"])
    if "history_ttl" in data:
        cfg.history_ttl = int(data["history_ttl"])
    if "receivers" in data:
        cfg.receivers = data["receivers"]
