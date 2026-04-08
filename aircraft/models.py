import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Aircraft:
    icao: str
    callsign: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude: Optional[int] = None        # feet baro
    ground_speed: Optional[int] = None   # knots
    track: Optional[float] = None        # degrees true, 0=N clockwise
    vertical_rate: Optional[int] = None  # ft/min
    squawk: Optional[str] = None
    on_ground: bool = False
    category: Optional[str] = None       # ADS-B emitter category
    last_seen: float = field(default_factory=time.time)
    last_position: float = field(default_factory=float)

    # Internal CPR state for AVR decoding (not serialized to clients)
    _cpr_even: Optional[tuple] = field(default=None, repr=False)
    _cpr_odd: Optional[tuple] = field(default=None, repr=False)

    def update(self, **kwargs) -> None:
        has_pos = ("lat" in kwargs or "lon" in kwargs)
        for k, v in kwargs.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)
        self.last_seen = time.time()
        if has_pos and self.lat is not None and self.lon is not None:
            self.last_position = self.last_seen

    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def age(self) -> float:
        return time.time() - self.last_seen

    def to_dict(self) -> dict:
        return {
            "icao": self.icao,
            "callsign": self.callsign,
            "lat": self.lat,
            "lon": self.lon,
            "altitude": self.altitude,
            "ground_speed": self.ground_speed,
            "track": self.track,
            "vertical_rate": self.vertical_rate,
            "squawk": self.squawk,
            "on_ground": self.on_ground,
            "category": self.category,
            "last_seen": self.last_seen,
            "age": round(self.age(), 1),
        }
