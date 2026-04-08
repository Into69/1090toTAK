"""
Minimal self-contained ADS-B (DF17) decoder.
Covers the fields used by AVRReceiver: callsign, barometric altitude,
CPR position, and airborne velocity.

No external dependencies — replaces pyModeS for the AVR path.

References:
  "The 1090 Megahertz Riddle" by Junzi Sun (open-access textbook)
  ICAO Doc 9684 (Mode S transponder specification)
"""

import math
from typing import Optional

# ---------------------------------------------------------------------------
# CRC-24 validation
# ---------------------------------------------------------------------------

_CRC_GENERATOR = 0xFFF409  # Mode S generator polynomial (bits 0-23)


def _crc_remainder(msg: str) -> int:
    """Compute CRC-24 remainder over the full message (including CRC field).
    Returns 0 for a valid message; non-zero remainder is the error syndrome."""
    crc = 0
    for i in range(0, len(msg), 2):
        byte = int(msg[i:i + 2], 16)
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= _CRC_GENERATOR
    return crc & 0xFFFFFF


def _build_syndrome_table(total_bits: int) -> dict:
    """Pre-compute {syndrome: bit_position} for single-bit error correction."""
    payload_bits = total_bits - 24
    hex_len = total_bits // 4
    table = {}
    for bit_pos in range(payload_bits):
        val = 1 << (total_bits - 1 - bit_pos)
        hex_str = format(val, f"0{hex_len}X")
        syndrome = _crc_remainder(hex_str)
        if syndrome != 0:
            table[syndrome] = bit_pos
    return table


# Pre-computed syndrome tables for 56-bit and 112-bit Mode S messages
_SYNDROME_56 = _build_syndrome_table(56)
_SYNDROME_112 = _build_syndrome_table(112)


def crc_ok(msg: str) -> bool:
    """
    Return True if the 24-bit CRC embedded in the last 3 bytes is valid.
    Works for both short (7-byte / 56-bit) and long (14-byte / 112-bit) messages.
    """
    if len(msg) < 8:
        return False
    return _crc_remainder(msg) == 0


def fix_single_bit(msg: str) -> Optional[str]:
    """
    Attempt single-bit error correction using syndrome lookup (O(1)).
    Returns the corrected hex string if a single payload-bit error is found,
    else None.
    """
    total_bits = len(msg) * 4
    table = {56: _SYNDROME_56, 112: _SYNDROME_112}.get(total_bits)
    if table is None:
        return None
    syndrome = _crc_remainder(msg)
    if syndrome == 0:
        return msg  # already valid
    bit_pos = table.get(syndrome)
    if bit_pos is None:
        return None
    val = int(msg, 16)
    val ^= 1 << (total_bits - 1 - bit_pos)
    return format(val, f"0{len(msg)}X")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _bits(msg: str) -> str:
    """Full 112-bit message as a binary string."""
    return bin(int(msg, 16))[2:].zfill(len(msg) * 4)


def _me(msg: str) -> str:
    """56-bit ME (message) field as a binary string (bytes 5-11)."""
    return _bits(msg)[32:88]


# ---------------------------------------------------------------------------
# Frame identification
# ---------------------------------------------------------------------------

def df(msg: str) -> int:
    """Downlink Format — top 5 bits of first byte."""
    return int(msg[:2], 16) >> 3


def icao(msg: str) -> str:
    """ICAO 24-bit address for DF17 (bytes 2-4)."""
    return msg[2:8].upper()


def typecode(msg: str) -> int:
    """ADS-B type code — top 5 bits of ME field."""
    return int(msg[8:10], 16) >> 3


# ---------------------------------------------------------------------------
# TC 1-4  Identification (callsign)
# ---------------------------------------------------------------------------

_CHARSET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def callsign(msg: str) -> Optional[str]:
    """Decode 8-character callsign from a TC 1-4 message.
    Returns None if the result contains invalid characters."""
    bits = _me(msg)
    result = ""
    for i in range(8, 56, 6):
        idx = int(bits[i:i + 6], 2)
        result += _CHARSET[idx] if idx < len(_CHARSET) else "#"
    cs = result.strip("_# ").strip()
    if not cs or "#" in cs:
        return None
    # Reject callsigns with characters outside A-Z 0-9 and space
    if not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 " for c in cs):
        return None
    return cs


def category(msg: str) -> Optional[str]:
    """Decode emitter category from a TC 1-4 identification message.

    Returns a two-character string e.g. "A3" (TC=4, category=3 → Large aircraft)
    or None if no category information is available.

    TC → letter:  4 → A (most common for airborne)
                  3 → B  2 → C  1 → D
    Category values for TC=4 (set A):
      1=Light  2=Small  3=Large  4=High-vortex  5=Heavy  6=High-perf  7=Rotorcraft
    """
    bits = _me(msg)
    tc = int(bits[:5], 2)
    ca = int(bits[5:8], 2)
    if ca == 0:
        return None
    letter = {4: "A", 3: "B", 2: "C", 1: "D"}.get(tc)
    return f"{letter}{ca}" if letter else None


# ---------------------------------------------------------------------------
# TC 9-18  Airborne position (barometric altitude + CPR)
# ---------------------------------------------------------------------------

def altitude(msg: str) -> Optional[int]:
    """
    Decode barometric altitude in feet from a TC 9-18 message.
    Handles Q=1 (25-ft resolution) encoding; returns None for rare Q=0 Gillham.
    """
    bits = _me(msg)
    alt_bits = bits[8:20]   # 12-bit altitude code inside ME
    q = alt_bits[7]         # Q bit at position 7 (0-indexed)
    if q == "1":
        n = int(alt_bits[:7] + alt_bits[8:], 2)   # strip Q bit
        return n * 25 - 1000
    # Q=0: Gillham/Gray coding — uncommon in modern aircraft, skip for now
    return None


def oe_flag(msg: str) -> int:
    """CPR odd/even flag: 0 = even, 1 = odd."""
    return int(_me(msg)[21])


def _cpr_lat(msg: str) -> float:
    return int(_me(msg)[22:39], 2) / 131072.0   # divide by 2^17


def _cpr_lon(msg: str) -> float:
    return int(_me(msg)[39:56], 2) / 131072.0


_NZ = 15  # number of latitude zones (constant per spec)


def _nl(lat: float) -> int:
    """Number of longitude zones for a given latitude."""
    if abs(lat) >= 87.0:
        return 1
    if abs(lat) >= 86.53536:
        return 2
    try:
        return max(1, math.floor(
            2 * math.pi / math.acos(
                1 - (1 - math.cos(math.pi / (2 * _NZ))) /
                (math.cos(math.radians(lat)) ** 2)
            )
        ))
    except (ValueError, ZeroDivisionError):
        return 1


def cpr_position(
    even_msg: str, odd_msg: str,
    t_even: float, t_odd: float,
) -> tuple[Optional[float], Optional[float]]:
    """
    Global CPR position decode from an even + odd frame pair.
    Returns (lat, lon) in decimal degrees, or (None, None) on failure.
    Frames must be within 10 seconds of each other (caller's responsibility).
    """
    lat_e = _cpr_lat(even_msg)
    lon_e = _cpr_lon(even_msg)
    lat_o = _cpr_lat(odd_msg)
    lon_o = _cpr_lon(odd_msg)

    dlat_e = 360.0 / (4 * _NZ)
    dlat_o = 360.0 / (4 * _NZ - 1)

    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)

    lat_even = dlat_e * (j % 60 + lat_e)
    lat_odd  = dlat_o * (j % 59 + lat_o)

    # Normalise to -90..+90
    if lat_even >= 270:
        lat_even -= 360
    if lat_odd >= 270:
        lat_odd -= 360

    if _nl(lat_even) != _nl(lat_odd):
        return None, None   # straddling a zone boundary — discard

    # Pick lat/lon from the most recent frame
    if t_even >= t_odd:
        lat = lat_even
        is_odd = 0
        lon_cpr = lon_e
    else:
        lat = lat_odd
        is_odd = 1
        lon_cpr = lon_o

    nl_lat = _nl(lat)
    ni = max(1, nl_lat - is_odd)
    m = math.floor(lon_e * (nl_lat - 1) - lon_o * nl_lat + 0.5)
    lon = (360.0 / ni) * (m % ni + lon_cpr)

    if lon >= 180:
        lon -= 360

    return round(lat, 6), round(lon, 6)


def cpr_position_local(
    msg: str, ref_lat: float, ref_lon: float,
) -> tuple[Optional[float], Optional[float]]:
    """
    Local CPR position decode using a single frame and a reference point.
    The reference can be the aircraft's last known position or the receiver
    location.  Unambiguous within ~180 nm of the reference.
    Returns (lat, lon) in decimal degrees, or (None, None) on failure.
    """
    oe = int(_me(msg)[21])
    cpr_lat = _cpr_lat(msg)
    cpr_lon = _cpr_lon(msg)

    dlat = 360.0 / (4 * _NZ - oe)

    j = math.floor(ref_lat / dlat) + math.floor(
        0.5 + (ref_lat % dlat) / dlat - cpr_lat
    )
    lat = dlat * (j + cpr_lat)

    if lat > 90 or lat < -90:
        return None, None

    nl = _nl(lat)
    ni = max(1, nl - oe)
    dlon = 360.0 / ni

    m = math.floor(ref_lon / dlon) + math.floor(
        0.5 + (ref_lon % dlon) / dlon - cpr_lon
    )
    lon = dlon * (m + cpr_lon)

    if lon >= 180:
        lon -= 360

    return round(lat, 6), round(lon, 6)


# ---------------------------------------------------------------------------
# TC 19  Airborne velocity
# ---------------------------------------------------------------------------

def velocity(msg: str) -> tuple[Optional[int], Optional[float], Optional[int], str]:
    """
    Decode airborne velocity from a TC 19 message.
    Returns (speed_kts, heading_deg, vertical_rate_fpm, speed_type).
    Any component may be None if unavailable.
    """
    bits = _me(msg)
    subtype = int(bits[5:8], 2)

    # Vertical rate (common to all subtypes)
    # VRC (source) = ME bit 35, VRS (sign) = ME bit 36, magnitude = ME bits 37:46
    vr_sign = int(bits[36])
    vr_raw  = int(bits[37:46], 2) - 1
    vr: Optional[int] = (vr_raw * 64) * (-1 if vr_sign else 1) if vr_raw >= 0 else None

    if subtype in (1, 2):
        # Ground speed components
        dew = int(bits[13])
        vew = int(bits[14:24], 2) - 1
        dns = int(bits[24])
        vns = int(bits[25:35], 2) - 1

        if vew < 0 or vns < 0:
            return None, None, vr, "GS"

        v_ew = vew * (-1 if dew else 1)
        v_ns = vns * (-1 if dns else 1)

        speed   = round(math.sqrt(v_ew ** 2 + v_ns ** 2))
        heading = (math.degrees(math.atan2(v_ew, v_ns)) + 360) % 360
        return speed, round(heading, 1), vr, "GS"

    elif subtype in (3, 4):
        # Airspeed + heading
        hdg_avail = int(bits[13])
        hdg = (
            int(bits[14:24], 2) / 1024.0 * 360.0 if hdg_avail else None
        )
        airspeed_type = int(bits[24])
        airspeed = int(bits[25:35], 2) - 1
        if subtype == 4:
            airspeed *= 4
        spd = airspeed if airspeed >= 0 else None
        return spd, hdg, vr, "TAS" if airspeed_type else "IAS"

    return None, None, vr, "unknown"
