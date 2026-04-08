"""
SQLite-backed aircraft position history store.

Records position snapshots at most every _MIN_WRITE_INTERVAL seconds per
aircraft, so the database doesn't balloon with every decoded message.
Old records are purged on a background thread when they exceed history_ttl.
"""

import sqlite3
import threading
import time
import logging
from typing import List

log = logging.getLogger(__name__)

_MIN_WRITE_INTERVAL = 5.0   # seconds between writes per aircraft
_PURGE_INTERVAL     = 300.0 # how often the background thread purges (5 min)


class AircraftStore:
    def __init__(self, db_path: str = "aircraft_history.db", history_ttl: int = 3600):
        self._db_path = db_path
        self._history_ttl = history_ttl
        self._lock = threading.Lock()
        self._last_write: dict[str, float] = {}
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                icao         TEXT    NOT NULL,
                callsign     TEXT,
                lat          REAL    NOT NULL,
                lon          REAL    NOT NULL,
                altitude     INTEGER,
                ground_speed INTEGER,
                track        REAL,
                on_ground    INTEGER,
                ts           REAL    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_icao_ts ON positions (icao, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts       ON positions (ts)")
        conn.commit()
        log.info("AircraftStore: opened %s (history_ttl=%ds)", self._db_path, self._history_ttl)
        return conn

    def set_ttl(self, history_ttl: int) -> None:
        self._history_ttl = history_ttl

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(self, ac) -> None:
        """Record a position snapshot. Silently skipped if no position or too recent."""
        if ac.lat is None or ac.lon is None:
            return
        icao = ac.icao
        now = time.time()
        with self._lock:
            if now - self._last_write.get(icao, 0.0) < _MIN_WRITE_INTERVAL:
                return
            self._last_write[icao] = now
            try:
                self._conn.execute(
                    """INSERT INTO positions
                       (icao, callsign, lat, lon, altitude, ground_speed, track, on_ground, ts)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        icao,
                        ac.callsign,
                        ac.lat,
                        ac.lon,
                        ac.altitude,
                        ac.ground_speed,
                        ac.track,
                        1 if ac.on_ground else 0,
                        now,
                    ),
                )
                self._conn.commit()
            except Exception as e:
                log.warning("AircraftStore: write error: %s", e)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_track(self, icao: str) -> List[dict]:
        """Return position history for one aircraft within history_ttl."""
        cutoff = time.time() - self._history_ttl
        with self._lock:
            rows = self._conn.execute(
                """SELECT lat, lon, altitude, track, on_ground, ts
                   FROM positions WHERE icao = ? AND ts >= ?
                   ORDER BY ts ASC""",
                (icao.upper(), cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_range(self, start: float, end: float, step: int = 30) -> dict:
        """Return position history grouped by ICAO for a time window.
        Decimated to at most one point per `step` seconds per aircraft."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT icao, callsign, lat, lon, altitude, ground_speed, track, on_ground, ts
                   FROM positions WHERE ts >= ? AND ts < ?
                   ORDER BY icao, ts ASC""",
                (start, end),
            ).fetchall()
        result: dict = {}
        last_ts: dict = {}
        for row in rows:
            d = dict(row)
            icao = d.pop("icao")
            if icao not in result:
                result[icao] = []
                last_ts[icao] = 0.0
            if d["ts"] - last_ts[icao] >= step:
                result[icao].append(d)
                last_ts[icao] = d["ts"]
        return result

    def get_heatmap_cells(self, start: float, end: float, cell_deg: float = 0.02) -> list:
        """Return [[lat, lon, intensity], ...] bucketed into a lat/lon grid.
        Intensity is normalised 0–1 relative to the densest cell."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT lat, lon FROM positions WHERE ts >= ? AND ts < ?",
                (start, end),
            ).fetchall()
        inv = 1.0 / cell_deg
        cells: dict = {}
        for row in rows:
            key = (round(row["lat"] * inv) / inv, round(row["lon"] * inv) / inv)
            cells[key] = cells.get(key, 0) + 1
        if not cells:
            return []
        max_count = max(cells.values())
        return [[lat, lon, count / max_count] for (lat, lon), count in cells.items()]

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def purge(self) -> int:
        """Delete records older than history_ttl. Returns count removed."""
        cutoff = time.time() - self._history_ttl
        with self._lock:
            cur = self._conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
            self._conn.commit()
            count = cur.rowcount
        if count:
            log.debug("AircraftStore: purged %d records older than %ds", count, self._history_ttl)
        return count

    def clear(self) -> int:
        """Delete ALL records and vacuum. Returns count removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM positions")
            self._conn.commit()
            count = cur.rowcount
            self._conn.execute("VACUUM")
            self._last_write.clear()
        log.info("AircraftStore: cleared all %d records", count)
        return count

    def stats(self) -> dict:
        """Return database file size and row count."""
        import os
        with self._lock:
            row_count = self._conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        try:
            size_bytes = os.path.getsize(self._db_path)
        except OSError:
            size_bytes = 0
        return {"row_count": row_count, "size_bytes": size_bytes, "db_path": self._db_path}

    def start_purge_thread(self) -> None:
        t = threading.Thread(target=self._purge_loop, daemon=True, name="store-purge")
        t.start()

    def _purge_loop(self) -> None:
        while True:
            time.sleep(_PURGE_INTERVAL)
            try:
                self.purge()
            except Exception as e:
                log.warning("AircraftStore: purge error: %s", e)
