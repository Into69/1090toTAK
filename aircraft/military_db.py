"""
Military / VVIP aircraft database loader.

Loads two ICAO sets from a flag-encoded aircraft database:
  • Military — heads-of-state-rare-but-not-unique military tails
  • VVIP / "interesting" — presidential, government, notable special airframes

Bit semantics differ between supported source formats:

  Mictronics aircrafts.json (default):
    flags is a hex string per row.
      bit 4 (0x10) → military
      bit 0 (0x01) → VVIP / interesting

  tar1090-db CSV (alternative source):
    flags is a decimal integer per row.
      bit 0 (0x01) → military
      bit 1 (0x02) → VVIP / interesting

Range-based detection (the fallback when no DB is loaded) is crude and
produces false positives wherever a country mixes mil/civ in one allocation.
The DB resolves that by giving an authoritative per-tail answer.
"""

import csv
import gzip
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Dict, Set, Optional

log = logging.getLogger(__name__)


def _parse_json(fileobj):
    """Parse the Mictronics aircrafts.json format: {ICAO: [reg, type, flags, desc?]}.

    `flags` is a hex string. In Mictronics' encoding bit 4 (0x10) flags
    military, bit 0 (0x01) flags VVIP / "interesting". Returns
    (records, military_set, vvip_set) where records is a dict keyed by
    upper-case ICAO with {reg, type, flags, desc?} per entry.
    """
    data = json.load(fileobj)
    records: Dict[str, dict] = {}
    military: Set[str] = set()
    vvip: Set[str] = set()
    if not isinstance(data, dict):
        return records, military, vvip
    for icao, row in data.items():
        if not isinstance(icao, str) or len(icao) != 6:
            continue
        reg = type_ = desc = None
        flags = None
        if isinstance(row, list):
            if len(row) >= 1: reg = row[0] or None
            if len(row) >= 2: type_ = row[1] or None
            if len(row) >= 3: flags = row[2]
            if len(row) >= 4: desc = row[3] or None
        elif isinstance(row, dict):
            reg = row.get("reg") or row.get("r") or None
            type_ = row.get("type") or row.get("t") or None
            desc = row.get("desc") or row.get("d") or None
            flags = row.get("flags") or row.get("dbFlags")
        if isinstance(flags, str):
            try:
                flags = int(flags, 16)
            except ValueError:
                flags = None
        if not isinstance(flags, int):
            flags = 0
        up = icao.upper()
        is_mil = bool(flags & 0x10)
        is_vvip = bool(flags & 0x01)
        rec = {"reg": reg, "type": type_, "flags": flags,
               "is_military": is_mil, "is_vvip": is_vvip}
        if desc:
            rec["desc"] = desc
        records[up] = rec
        if is_mil:  military.add(up)
        if is_vvip: vvip.add(up)
    return records, military, vvip


def _parse_csv(fileobj):
    """Parse the tar1090-db aircraft CSV. Bit 0 (& 1) = military, bit 1 (& 2)
    = VVIP / interesting. Tolerates either ',' or ';' delimiters and whatever
    column ordering. Returns (records, military_set, vvip_set). Registration /
    type columns are best-effort: the first non-numeric, non-ICAO column is
    treated as registration and the second as type."""
    sample = fileobj.read(4096)
    fileobj.seek(0)
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(fileobj, delimiter=delim)
    records: Dict[str, dict] = {}
    military: Set[str] = set()
    vvip: Set[str] = set()
    icao_col = None
    flag_col = None
    text_cols: list = []
    for row in reader:
        if not row:
            continue
        # Column auto-detection on the first usable row
        if icao_col is None:
            for i, cell in enumerate(row):
                v = cell.strip()
                if len(v) == 6 and all(c in "0123456789ABCDEFabcdef" for c in v):
                    icao_col = i
                    break
            if icao_col is None:
                continue  # header row — skip and try the next
            for i, cell in enumerate(row):
                if i == icao_col:
                    continue
                v = cell.strip()
                if v.isdigit() and 0 <= int(v) <= 0xFFFF:
                    flag_col = i
                    break
            if flag_col is None:
                # No numeric column on this row — skip and try next
                icao_col = None
                continue
            text_cols = [i for i in range(len(row))
                         if i != icao_col and i != flag_col][:2]
        if icao_col >= len(row) or flag_col >= len(row):
            continue
        icao = row[icao_col].strip().upper()
        if len(icao) != 6:
            continue
        flag_str = row[flag_col].strip()
        if not flag_str.isdigit():
            continue
        n = int(flag_str)
        reg  = row[text_cols[0]].strip() if text_cols and text_cols[0] < len(row) else None
        typ  = row[text_cols[1]].strip() if len(text_cols) > 1 and text_cols[1] < len(row) else None
        is_mil  = bool(n & 0x01)
        is_vvip = bool(n & 0x02)
        records[icao] = {"reg": reg or None, "type": typ or None, "flags": n,
                         "is_military": is_mil, "is_vvip": is_vvip}
        if is_mil:  military.add(icao)
        if is_vvip: vvip.add(icao)
    return records, military, vvip


class MilitaryDB:
    def __init__(self, path: str):
        self._path = path
        self._records: Dict[str, dict] = {}
        self._military: Set[str] = set()
        self._vvip: Set[str] = set()
        self._loaded_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def set_path(self, path: str) -> None:
        with self._lock:
            self._path = path

    def load(self) -> int:
        """Load military + VVIP ICAOs from the on-disk file. Returns the
        military count (kept as the headline number for backward compat)."""
        path = self._path
        if not os.path.exists(path):
            log.info("MilitaryDB: file not present at %s", path)
            return 0
        try:
            opener = gzip.open if path.endswith(".gz") else open
            # Strip ".gz" before checking the inner format
            inner = path[:-3] if path.endswith(".gz") else path
            is_json = inner.endswith(".json")
            with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
                records, military, vvip = _parse_json(f) if is_json else _parse_csv(f)
        except Exception as e:
            log.warning("MilitaryDB: load error: %s", e)
            return 0
        with self._lock:
            self._records = records
            self._military = military
            self._vvip = vvip
            self._loaded_at = time.time()
        log.info("MilitaryDB: loaded %d records (%d military + %d VVIP) from %s",
                 len(records), len(military), len(vvip), path)
        return len(military)

    def lookup(self, icao: str) -> Optional[dict]:
        """Return per-tail metadata if this ICAO is in the DB, else None."""
        if not icao:
            return None
        with self._lock:
            return self._records.get(icao.upper())

    def is_military(self, icao: str) -> bool:
        if not icao:
            return False
        with self._lock:
            return icao.upper() in self._military

    def is_vvip(self, icao: str) -> bool:
        if not icao:
            return False
        with self._lock:
            return icao.upper() in self._vvip

    def count(self) -> int:
        with self._lock:
            return len(self._military)

    def vvip_count(self) -> int:
        with self._lock:
            return len(self._vvip)

    def icaos(self) -> list:
        with self._lock:
            return sorted(self._military)

    def vvip_icaos(self) -> list:
        with self._lock:
            return sorted(self._vvip)

    def status(self) -> dict:
        path = self._path
        exists = os.path.exists(path)
        try:
            size = os.path.getsize(path) if exists else 0
            mtime = os.path.getmtime(path) if exists else 0
        except OSError:
            size, mtime = 0, 0
        return {
            "path": path,
            "exists": exists,
            "size_bytes": size,
            "file_mtime": mtime,
            "loaded_count": self.count(),
            "vvip_count": self.vvip_count(),
            "loaded_at": self._loaded_at,
        }

    def download(self, url: str, timeout: int = 60) -> dict:
        """Fetch the database from `url`, write to disk, then load it."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "1090toTAK/military-db"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except Exception as e:
            log.warning("MilitaryDB: download failed: %s", e)
            return {"ok": False, "error": str(e)}
        if not data:
            return {"ok": False, "error": "empty response"}
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)) or ".", exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("MilitaryDB: write failed: %s", e)
            return {"ok": False, "error": str(e)}
        log.info("MilitaryDB: downloaded %d bytes to %s", len(data), self._path)
        count = self.load()
        return {"ok": True, "size_bytes": len(data), "loaded": count}
