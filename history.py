"""Durable history of booked slips and rollover plans.

Production: one Postgres table in Supabase, reached through PostgREST with the service key (Render's free disk is
wiped on every restart/redeploy, so SQLite alone would lose the history):

    public.fra_records(id text pk, kind text, status text, created_at timestamptz, updated_at timestamptz, data jsonb)
    kind = 'slip' | 'rollover'   (RLS on, no policies: only the service key can read/write)

Local / tests: the same records in the bot's SQLite file when SUPABASE_URL or SUPABASE_SERVICE_KEY is unset.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)
TABLE = "fra_records"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%y%m%d')}_{uuid.uuid4().hex[:6]}"


class HistoryStore:
    def __init__(self, storage=None, url: str | None = None, key: str | None = None):
        self.url = (url if url is not None else os.environ.get("SUPABASE_URL", "")).strip().rstrip("/")
        self.key = (key if key is not None else os.environ.get("SUPABASE_SERVICE_KEY", "")).strip()
        self.storage = storage
        self._client: httpx.AsyncClient | None = None
        self.last_error = ""
        if not self.remote and storage is not None:
            storage._exec(f"CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, kind TEXT, status TEXT, "
                          "created_at TEXT, updated_at TEXT, data TEXT)")

    @property
    def remote(self) -> bool:
        return bool(self.url and self.key)

    @property
    def backend(self) -> str:
        return "supabase" if self.remote else ("sqlite" if self.storage is not None else "memory-off")

    def _headers(self) -> dict:
        h = {"apikey": self.key, "Content-Type": "application/json"}
        if not self.key.startswith("sb_"):      # legacy JWT keys also go in Authorization; sb_secret keys must not
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=f"{self.url}/rest/v1", headers=self._headers(),
                                             timeout=httpx.Timeout(15.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- core ---------------------------------------------------------------
    async def save(self, rec: dict) -> bool:
        """Upsert {id, kind, status, created_at?, data}. Returns False (and logs) when the store is unreachable."""
        rec = dict(rec)
        rec.setdefault("created_at", now_iso())
        rec["updated_at"] = now_iso()
        if self.remote:
            for attempt in range(3):
                try:
                    r = await self._http().post(f"/{TABLE}", json=rec, params={"on_conflict": "id"},
                                                headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
                    if r.status_code < 300:
                        return True
                    self.last_error = f"HTTP {r.status_code} {r.text[:160]}"
                except httpx.HTTPError as exc:
                    self.last_error = exc.__class__.__name__
                await asyncio.sleep(1.5 * (attempt + 1))
            log.warning("history save failed: %s", self.last_error)
            return False
        if self.storage is None:
            return False
        self.storage._exec(f"INSERT OR REPLACE INTO {TABLE} VALUES (?,?,?,?,?,?)",
                           (rec["id"], rec["kind"], rec["status"], rec["created_at"], rec["updated_at"],
                            json.dumps(rec.get("data") or {})))
        return True

    async def list(self, kind: str, statuses: list[str] | None = None, limit: int = 50,
                   since: str | None = None) -> list[dict]:
        if self.remote:
            params = {"kind": f"eq.{kind}", "order": "created_at.desc", "limit": str(limit), "select": "*"}
            if statuses:
                params["status"] = f"in.({','.join(statuses)})"
            if since:
                params["created_at"] = f"gte.{since}"
            try:
                r = await self._http().get(f"/{TABLE}", params=params)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.last_error = f"{exc.__class__.__name__}"
                log.warning("history list failed: %s", exc.__class__.__name__)
                raise
        if self.storage is None:
            return []
        sql = f"SELECT id, kind, status, created_at, updated_at, data FROM {TABLE} WHERE kind=?"
        args: list = [kind]
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            args += statuses
        if since:
            sql += " AND created_at >= ?"
            args.append(since)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = self.storage._exec(sql, tuple(args))
        return [{"id": r[0], "kind": r[1], "status": r[2], "created_at": r[3], "updated_at": r[4],
                 "data": json.loads(r[5] or "{}")} for r in rows]

    async def get(self, rid: str) -> dict | None:
        if self.remote:
            r = await self._http().get(f"/{TABLE}", params={"id": f"eq.{rid}", "select": "*"})
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
        if self.storage is None:
            return None
        rows = self.storage._exec(f"SELECT id, kind, status, created_at, updated_at, data FROM {TABLE} WHERE id=?",
                                  (rid,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "kind": r[1], "status": r[2], "created_at": r[3], "updated_at": r[4],
                "data": json.loads(r[5] or "{}")}

    async def find_code(self, code: str) -> dict | None:
        """Newest slip record whose booking code is `code` (None when the code isn't in the history)."""
        code = (code or "").strip().upper()
        if not code:
            return None
        if self.remote:
            r = await self._http().get(f"/{TABLE}", params={"kind": "eq.slip", "data->>code": f"eq.{code}",
                                                            "order": "created_at.desc", "limit": "1", "select": "*"})
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
        if self.storage is None:
            return None
        rows = self.storage._exec(f"SELECT id, kind, status, created_at, updated_at, data FROM {TABLE} WHERE kind='slip' "
                                  "AND data LIKE ? ORDER BY created_at DESC", (f'%"code": "{code}"%',))
        for r in rows:
            d = json.loads(r[5] or "{}")
            if str(d.get("code") or "").upper() == code:
                return {"id": r[0], "kind": r[1], "status": r[2], "created_at": r[3], "updated_at": r[4], "data": d}
        return None

    async def delete(self, rid: str) -> None:
        if self.remote:
            await self._http().delete(f"/{TABLE}", params={"id": f"eq.{rid}"})
        elif self.storage is not None:
            self.storage._exec(f"DELETE FROM {TABLE} WHERE id=?", (rid,))


# ------------------------------------------------------------------ slip records
def _uid(competitor_id: str | None) -> str:
    """Sportradar uid from 'sr:competitor:38' (only real Betradar ids; Genius ids are 13 digits)."""
    s = str(competitor_id or "").rsplit(":", 1)[-1]
    return s if s.isdigit() and len(s) <= 9 else ""


def leg_record(l: dict) -> dict:
    """Normalise a leg (scanner Leg.to_json, Mini App slip item or chat slip row) for the history."""
    return {
        "event_id": l.get("event_id", ""), "match": l.get("match") or l.get("match_name") or "",
        "home": l.get("home", ""), "away": l.get("away", ""),
        "home_uid": l.get("home_uid") or _uid(l.get("home_id")), "away_uid": l.get("away_uid") or _uid(l.get("away_id")),
        "league": l.get("league", ""), "category": l.get("category", ""),
        "kickoff": l.get("kickoff") or l.get("start") or "", "kickoff_local": l.get("kickoff_local") or l.get("when") or "",
        "market": l.get("market", ""), "market_id": str(l.get("market_id", "")), "specifier": l.get("specifier") or "",
        "outcome_id": str(l.get("outcome_id", "")), "pick": l.get("pick") or l.get("label") or "",
        "odds": float(l["odds"]) if l.get("odds") else None,
        "est": round(float(l["est"]), 4) if l.get("est") is not None else None,
        "verdict": l.get("verdict", ""), "status": "pending", "score": "", "note": "",
    }


def slip_record(legs: list[dict], code: str | None, source: str, tier: str = "custom", label: str = "",
                total_odds: float | None = None, est_prob: float | None = None, test: bool = False,
                rollover_id: str = "", rollover_day: int = 0, target: float | None = None) -> dict:
    lr = [leg_record(l) for l in legs]
    tot = total_odds
    if tot is None:
        tot = 1.0
        for l in lr:
            tot *= l["odds"] or 1.0
    if est_prob is None and lr and all(l["est"] is not None for l in lr):
        est_prob = 1.0
        for l in lr:
            est_prob *= l["est"]
    sid = new_id("rd" if rollover_id else "sl")
    return {"id": sid, "kind": "slip", "status": "pending", "created_at": now_iso(), "data": {
        "code": code or "", "source": source, "tier": tier, "label": label or tier.title(),
        "total_odds": round(float(tot), 2), "est_prob": None if est_prob is None else round(est_prob, 4),
        "target": target, "legs": lr, "notified": False, "test": test, "rollover_id": rollover_id,
        "rollover_day": rollover_day, "created_ts": time.time()}}
