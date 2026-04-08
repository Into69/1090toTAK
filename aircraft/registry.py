import math
import threading
import time
import logging
from typing import Dict, List, Optional, Callable

from .models import Aircraft

log = logging.getLogger(__name__)

# Maximum plausible speed in knots — anything above this is a corrupt position.
# SR-71 cruise was ~1900 kts; allow generous margin above that.
_MAX_SPEED_KTS = 4000


class AircraftRegistry:
    def __init__(self, ttl: int = 60):
        self._aircraft: Dict[str, Aircraft] = {}
        self._lock = threading.RLock()
        self._ttl = ttl
        self._peak_count = 0
        self._expiry_thread: Optional[threading.Thread] = None
        self._remove_callbacks: List[Callable] = []
        self._update_callbacks: List[Callable] = []

    # --- Public API ---

    def update(self, icao: str, **fields) -> Aircraft:
        icao = icao.upper()
        with self._lock:
            if icao not in self._aircraft:
                self._aircraft[icao] = Aircraft(icao=icao)
            ac = self._aircraft[icao]

            # ── Position sanity checks ──────────────────────────────────
            new_lat = fields.get("lat")
            new_lon = fields.get("lon")
            if new_lat is not None and new_lon is not None:
                # Reject out-of-range coordinates
                if not (-90 <= new_lat <= 90 and -180 <= new_lon <= 180):
                    log.debug("Registry: reject out-of-range position for %s: %.4f, %.4f",
                              icao, new_lat, new_lon)
                    fields.pop("lat", None)
                    fields.pop("lon", None)
                # Speed gate: reject implausible jumps from known position
                elif ac.lat is not None and ac.lon is not None and ac.last_position:
                    dt = time.time() - ac.last_position
                    if dt > 0.5:
                        dlat = new_lat - ac.lat
                        dlon = new_lon - ac.lon
                        cos_lat = math.cos(math.radians(new_lat))
                        dist_nm = math.sqrt((dlat * 60) ** 2 + (dlon * 60 * cos_lat) ** 2)
                        speed_kts = dist_nm / (dt / 3600)
                        if speed_kts > _MAX_SPEED_KTS:
                            log.debug("Registry: reject implausible jump for %s: "
                                      "%.0f nm in %.1fs (%.0f kts)",
                                      icao, dist_nm, dt, speed_kts)
                            fields.pop("lat", None)
                            fields.pop("lon", None)

            ac.update(**fields)
            self._peak_count = max(self._peak_count, len(self._aircraft))
        for cb in self._update_callbacks:
            try:
                cb(ac)
            except Exception:
                pass
        return ac

    def get(self, icao: str) -> Optional[Aircraft]:
        with self._lock:
            return self._aircraft.get(icao.upper())

    def get_all(self) -> List[Aircraft]:
        with self._lock:
            return list(self._aircraft.values())

    def get_all_dicts(self) -> List[dict]:
        with self._lock:
            return [ac.to_dict() for ac in self._aircraft.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._aircraft)

    def count_with_position(self) -> int:
        with self._lock:
            return sum(1 for ac in self._aircraft.values() if ac.has_position())

    @property
    def peak_count(self) -> int:
        return self._peak_count

    def on_remove(self, callback: Callable[[str], None]) -> None:
        self._remove_callbacks.append(callback)

    def on_update(self, callback: Callable) -> None:
        self._update_callbacks.append(callback)

    def set_ttl(self, ttl: int) -> None:
        self._ttl = ttl

    # --- Expiry ---

    def start_expiry_thread(self) -> None:
        self._expiry_thread = threading.Thread(
            target=self._expiry_loop, daemon=True, name="aircraft-expiry"
        )
        self._expiry_thread.start()

    def _expiry_loop(self) -> None:
        while True:
            time.sleep(10)
            self._purge_stale()

    def _purge_stale(self) -> List[str]:
        removed = []
        with self._lock:
            stale = [
                icao for icao, ac in self._aircraft.items()
                if ac.age() > self._ttl
            ]
            for icao in stale:
                del self._aircraft[icao]
                removed.append(icao)
        if removed:
            log.debug("Purged %d stale aircraft: %s", len(removed), removed)
            for icao in removed:
                for cb in self._remove_callbacks:
                    try:
                        cb(icao)
                    except Exception:
                        pass
        return removed
