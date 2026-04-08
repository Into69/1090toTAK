"""
Cursor on Target (CoT) XML builder for ADS-B aircraft.

CoT schema reference: https://www.mitre.org/sites/default/files/pdf/09_4937.pdf
Aircraft type codes follow MIL-STD-2525 symbology.
"""

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional

from aircraft.models import Aircraft


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ft_to_m(feet: Optional[float]) -> float:
    if feet is None:
        return 9999.0
    return round(feet * 0.3048, 1)


def _kts_to_ms(knots: Optional[float]) -> float:
    if knots is None:
        return 0.0
    return round(knots * 0.514444, 2)


# CoT type codes — MIL-STD-2525 symbology
COT_FIXED_WING      = "a-f-A-M-F-Q"   # friendly fixed wing
COT_FIXED_WING_LG   = "a-f-A-M-F-Q-H" # heavy fixed wing (> 300k lbs)
COT_HIGH_PERF        = "a-f-A-M-F-Q-J" # high-performance / fighter
COT_ROTORCRAFT       = "a-f-A-M-H"     # rotorcraft
COT_UAV              = "a-f-A-M-F-U"   # UAV / drone
COT_LIGHTER_THAN_AIR = "a-f-A-M-F-L"   # balloon / airship
COT_GROUND_VEHICLE   = "a-f-G-E-V"     # ground vehicle
COT_AIR_UNKNOWN      = "a-u-A"         # unknown air

# Emergency squawk codes
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}

# ADS-B emitter category → CoT type
_CATEGORY_COT_MAP = {
    "A1": COT_FIXED_WING,        # Light (< 15,500 lbs)
    "A2": COT_FIXED_WING,        # Small (15,500–75,000 lbs)
    "A3": COT_FIXED_WING,        # Large (75,000–300,000 lbs)
    "A4": COT_FIXED_WING_LG,     # High vortex large
    "A5": COT_FIXED_WING_LG,     # Heavy (> 300,000 lbs)
    "A6": COT_HIGH_PERF,         # High performance (> 5g, > 400 kts)
    "A7": COT_ROTORCRAFT,        # Rotorcraft
    "B1": COT_FIXED_WING,        # Glider / sailplane
    "B2": COT_LIGHTER_THAN_AIR,  # Lighter-than-air
    "B4": COT_FIXED_WING,        # Ultralight / hang-glider / paraglider
    "B6": COT_UAV,               # UAV / drone
    "B7": COT_UAV,               # Space / trans-atmospheric
    "C1": COT_GROUND_VEHICLE,    # Emergency vehicle
    "C2": COT_GROUND_VEHICLE,    # Service vehicle
}


def _cot_type(aircraft: Aircraft) -> str:
    if aircraft.squawk in EMERGENCY_SQUAWKS:
        return COT_AIR_UNKNOWN
    cat = (aircraft.category or "").upper()
    return _CATEGORY_COT_MAP.get(cat, COT_FIXED_WING)


class CotBuilder:
    def build(self, aircraft: Aircraft, aircraft_ttl: int = 60) -> bytes:
        now = datetime.now(timezone.utc)
        stale = now + timedelta(seconds=aircraft_ttl)
        time_str = _iso_z(now)
        stale_str = _iso_z(stale)

        uid = f"ADSB-{aircraft.icao}"
        cot_type = _cot_type(aircraft)
        callsign = aircraft.callsign or aircraft.icao

        lat = aircraft.lat or 0.0
        lon = aircraft.lon or 0.0
        hae = _ft_to_m(aircraft.altitude)
        speed_ms = _kts_to_ms(aircraft.ground_speed)
        course = aircraft.track or 0.0

        # Build XML
        event = ET.Element("event", {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "time": time_str,
            "start": time_str,
            "stale": stale_str,
            "how": "m-g",
            "access": "Undefined",
        })

        ET.SubElement(event, "point", {
            "lat": str(round(lat, 6)),
            "lon": str(round(lon, 6)),
            "hae": str(hae),
            "ce": "9999999.0",
            "le": "9999999.0",
        })

        detail = ET.SubElement(event, "detail")

        ET.SubElement(detail, "contact", {"callsign": callsign})
        ET.SubElement(detail, "track", {
            "speed": str(speed_ms),
            "course": str(round(course, 1)),
        })
        ET.SubElement(detail, "uid", {"Droid": callsign})

        remarks_parts = [f"ICAO: {aircraft.icao}"]
        if aircraft.altitude is not None:
            remarks_parts.append(f"Alt: {aircraft.altitude}ft")
        if aircraft.ground_speed is not None:
            remarks_parts.append(f"Spd: {aircraft.ground_speed}kts")
        if aircraft.squawk:
            remarks_parts.append(f"Squawk: {aircraft.squawk}")
        if aircraft.vertical_rate is not None:
            sign = "+" if aircraft.vertical_rate >= 0 else ""
            remarks_parts.append(f"VS: {sign}{aircraft.vertical_rate}fpm")

        ET.SubElement(detail, "remarks").text = " | ".join(remarks_parts)

        # Optional: flight summary extension used by some TAK plugins
        ET.SubElement(detail, "__flightsummary", {
            "FlightNumber": callsign,
            "ICAO": aircraft.icao,
            "Altitude": str(aircraft.altitude or 0),
            "Speed": str(aircraft.ground_speed or 0),
            "Squawk": aircraft.squawk or "",
        })

        xml_str = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        xml_str += ET.tostring(event, encoding="unicode")
        return xml_str.encode("utf-8")
