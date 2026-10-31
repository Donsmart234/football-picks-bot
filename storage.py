"""Small SQLite store: owner lock, JSON cache with expiry, API quota counter, bet slips."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, count INTEGER);
CREATE TABLE IF NOT EXISTS slip (
    user_id INTEGER, event_id TEXT, market_id TEXT, specifier TEXT, outcome_id TEXT,
    label TEXT, odds REAL, match_name TEXT, added_at REAL,
    PRIMARY KEY (user_id, event_id)
);
"""


class Storage:
    def __init__(self, path: Path | str = config.DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # ---- meta / owner -------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        rows = self._exec("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    def get_owner(self) -> int | None:
        if config.OWNER_TELEGRAM_ID:
            return config.OWNER_TELEGRAM_ID
        v = self.get_meta("owner_id")
        return int(v) if v else None

    def claim_owner(self, user_id: int) -> bool:
        """Atomically make user_id the owner if nobody is. Returns True if user_id is (now) the owner."""
        if config.OWNER_TELEGRAM_ID:
            return user_id == config.OWNER_TELEGRAM_ID
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='owner_id'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta(key, value) VALUES('owner_id', ?)", (str(user_id),))
                self._conn.commit()
                return True
            return int(row[0]) == user_id

    # ---- cache --------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        rows = self._exec("SELECT value, expires FROM cache WHERE key=?", (key,))
        if not rows:
            return None
        value, expires = rows[0]
        if expires and expires < time.time():
            self._exec("DELETE FROM cache WHERE key=?", (key,))
            return None
        return json.loads(value)

    def cache_set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO cache(key, value, expires) VALUES(?, ?, ?)",
            (key, json.dumps(value), time.time() + ttl_seconds),
        )

    def cache_purge(self) -> None:
        self._exec("DELETE FROM cache WHERE expires < ?", (time.time(),))

    # ---- API quota (UTC day, matches API-Football's reset) -------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def api_used_today(self) -> int:
        rows = self._exec("SELECT count FROM api_usage WHERE day=?", (self._today(),))
        return rows[0][0] if rows else 0

    def api_increment(self) -> int:
        day = self._today()
        self._exec(
            "INSERT INTO api_usage(day, count) VALUES(?, 1) ON CONFLICT(day) DO UPDATE SET count=count+1",
            (day,),
        )
        return self.api_used_today()

    def api_set_used(self, used: int) -> None:
        """Sync with the server-reported usage when it is higher than our own count."""
        day = self._today()
        self._exec(
            "INSERT INTO api_usage(day, count) VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET count=MAX(count, excluded.count)",
            (day, used),
        )

    # ---- slip ---------------------------------------------------------
    def slip_add(self, user_id: int, event_id: str, market_id: str, specifier: str, outcome_id: str,
                 label: str, odds: float, match_name: str) -> bool:
        """Add or replace the pick for this event. Returns True if it replaced an existing pick."""
        existed = bool(self._exec("SELECT 1 FROM slip WHERE user_id=? AND event_id=?", (user_id, event_id)))
        self._exec(
            "INSERT OR REPLACE INTO slip VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, event_id, market_id, specifier, outcome_id, label, odds, match_name, time.time()),
        )
        return existed

    def slip_get(self, user_id: int) -> list[dict]:
        rows = self._exec(
            "SELECT event_id, market_id, specifier, outcome_id, label, odds, match_name FROM slip "
            "WHERE user_id=? ORDER BY added_at", (user_id,))
        keys = ["event_id", "market_id", "specifier", "outcome_id", "label", "odds", "match_name"]
        return [dict(zip(keys, r)) for r in rows]

    def slip_remove(self, user_id: int, event_id: str) -> None:
        self._exec("DELETE FROM slip WHERE user_id=? AND event_id=?", (user_id, event_id))

    def slip_clear(self, user_id: int) -> None:
        self._exec("DELETE FROM slip WHERE user_id=?", (user_id,))
