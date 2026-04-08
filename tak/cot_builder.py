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


# CoT type codes for aircraft
# a-f-A = assumed friendly air
# a-u-A = unknown air
# a-h-A = hostile air (not used here)
COT_TYPE_AIR_FRIENDLY    = "a-f-A-M-F-Q"  # fixed wing commercial
COT_TYPE_AIR_UNKNOWN     = "a-u-A"
COT_TYPE_ROTORCRAFT      = "a-f-A-M-H"
COT_TYPE_UAV             = "a-f-A-M-F-U"

# Emergency squawk codes
EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}


def _cot_type(aircraft: Aircraft) -> str:
    if aircraft.squawk in EMERGENCY_SQUAWKS:
        return "a-u-A"
    cat = aircraft.category or ""
    # ADS-B emitter category codes: "A7" = rotorcraft, "B6" = UAV/drone
    if cat == "A7":
        return COT_TYPE_ROTORCRAFT
    if cat in ("B6", "B7"):
        return COT_TYPE_UAV
    return COT_TYPE_AIR_FRIENDLY


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
