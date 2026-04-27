"""
Military aircraft database loader.

Uses the Mictronics aircraft database (mirrored by wiedehopf/tar1090-db) to
identify military aircraft by exact ICAO match. The database is a gzipped CSV
with one row per known aircraft and a 'dbFlags' bitfield where bit 0 (& 1)
indicates military.

Range-based detection (the fallback used when this DB is disabled) is best-
effort and produces false positives in countries that mix military and
civilian aircraft within a single national allocation. The DB resolves that
by giving an authoritative per-tail flag.
"""

import csv
import gzip
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Set

log = logging.getLogger(__name__)


def _parse_json(fileobj) -> Set[str]:
    """Parse the Mictronics aircrafts.json format: {ICAO: [reg, type, flags]}.

    `flags` is a 2-char hex string where bit 0 (& 1) indicates military. The
    tar1090-db CSV uses the same bit but encodes the number in decimal — this
    parser normalizes both ("FF" hex and "255" decimal both become 255).
    """
    data = json.load(fileobj)
    military: Set[str] = set()
    if not isinstance(data, dict):
        return military
    for icao, row in data.items():
        if not isinstance(icao, str) or len(icao) != 6:
            continue
        flags = None
        if isinstance(row, list) and len(row) >= 3:
            flags = row[2]
        elif isinstance(row, dict):
            flags = row.get("flags") or row.get("dbFlags")
        if isinstance(flags, str):
            try:
                flags = int(flags, 16)
            except ValueError:
                continue
        if isinstance(flags, int) and flags & 1:
            military.add(icao.upper())
    return military


def _parse_csv(fileobj) -> Set[str]:
    """Parse the tar1090-db aircraft CSV. Tolerates either ',' or ';' delimiters
    and whatever column ordering as long as one column looks like a 6-hex ICAO
    and another column parses as an integer flags bitfield (bit 0 = military)."""
    sample = fileobj.read(4096)
    fileobj.seek(0)
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(fileobj, delimiter=delim)
    military: Set[str] = set()
    icao_col = None
    flag_col = None
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
        if icao_col >= len(row) or flag_col >= len(row):
            continue
        icao = row[icao_col].strip().upper()
        if len(icao) != 6:
            continue
        flag_str = row[flag_col].strip()
        if not flag_str.isdigit():
            continue
        if int(flag_str) & 1:
            military.add(icao)
    return military


class MilitaryDB:
    def __init__(self, path: str):
        self._path = path
        self._military: Set[str] = set()
        self._loaded_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def set_path(self, path: str) -> None:
        with self._lock:
            self._path = path

    def load(self) -> int:
        """Load military ICAOs from the on-disk file. Returns the count."""
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
                military = _parse_json(f) if is_json else _parse_csv(f)
        except Exception as e:
            log.warning("MilitaryDB: load error: %s", e)
            return 0
        with self._lock:
            self._military = military
            self._loaded_at = time.time()
        log.info("MilitaryDB: loaded %d military aircraft from %s", len(military), path)
        return len(military)

    def is_military(self, icao: str) -> bool:
        if not icao:
            return False
        with self._lock:
            return icao.upper() in self._military

    def count(self) -> int:
        with self._lock:
            return len(self._military)

    def icaos(self) -> list:
        with self._lock:
            return sorted(self._military)

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
