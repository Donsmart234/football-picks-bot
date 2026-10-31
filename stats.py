"""API-Football v3 client with aggressive SQLite caching and a hard daily request budget."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

import config
from matching import team_score
from storage import Storage

log = logging.getLogger(__name__)


@dataclass
class TeamStats:
    team_id: int
    name: str
    played_home: int = 0
    played_away: int = 0
    gf_home: float | None = None    # goals scored per home game
    ga_home: float | None = None    # goals conceded per home game
    gf_away: float | None = None
    ga_away: float | None = None
    form: str = ""                  # e.g. "WDLWW" (oldest -> newest, as API-Football returns)


@dataclass
class MatchStats:
    fixture_id: int
    league_id: int
    season: int
    home: TeamStats | None
    away: TeamStats | None
    h2h: list[tuple[int, int]] = field(default_factory=list)  # (goals of our home team, goals of our away team)
    requests_used: int = 0


class QuotaExceeded(RuntimeError):
    pass


class ApiFootball:
    BASE = "https://v3.football.api-sports.io"

    def __init__(self, storage: Storage, api_key: str = config.API_FOOTBALL_KEY,
                 daily_limit: int = config.API_FOOTBALL_DAILY_LIMIT):
        self.storage = storage
        self.enabled = bool(api_key)
        self.daily_limit = daily_limit
        self._client = httpx.AsyncClient(
            base_url=self.BASE, timeout=httpx.Timeout(20.0),
            headers={"x-apisports-key": api_key} if api_key else {})
        self._spent_this_call = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.storage.api_used_today())

    async def _get(self, endpoint: str, params: dict, ttl: int) -> list | dict | None:
        key = "apif:" + endpoint + ":" + json.dumps(params, sort_keys=True)
        cached = self.storage.cache_get(key)
        if cached is not None:
            return cached.get("response")
        if not self.enabled:
            return None
        if self.remaining_today() <= 0:
            raise QuotaExceeded("API-Football daily budget used up")
        self.storage.api_increment()
        self._spent_this_call += 1
        try:
            r = await self._client.get(endpoint, params=params)
        except httpx.HTTPError as exc:
            log.warning("API-Football %s failed: %s", endpoint, exc.__class__.__name__)
            return None
        # Sync our counter with the server's view of today's usage.
        try:
            lim = int(r.headers.get("x-ratelimit-requests-limit", ""))
            rem = int(r.headers.get("x-ratelimit-requests-remaining", ""))
            self.storage.api_set_used(lim - rem)
        except ValueError:
            pass
        if r.status_code != 200:
            log.warning("API-Football %s -> HTTP %s", endpoint, r.status_code)
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        errors = body.get("errors")
        if errors:
            # e.g. plan/season restrictions or bad key. Cache the miss so we don't burn quota retrying.
            log.warning("API-Football %s errors: %s", endpoint, errors)
            text = json.dumps(errors).lower()
            if "request" in text and ("limit" in text or "reached" in text):
                self.storage.api_set_used(self.daily_limit)
                raise QuotaExceeded("API-Football reported its limit reached")
            self.storage.cache_set(key, {"response": None}, 6 * 3600)
            return None
        resp = body.get("response")
        self.storage.cache_set(key, {"response": resp}, ttl)
        return resp

    # ---- endpoints -------------------------------------------------------
    async def fixtures_on(self, date_utc: str) -> list[dict]:
        """All fixtures on a UTC date: one request covers every match that day."""
        return await self._get("/fixtures", {"date": date_utc, "timezone": "UTC"}, ttl=6 * 3600) or []

    async def team_statistics(self, league: int, season: int, team: int) -> dict | None:
        return await self._get("/teams/statistics", {"league": league, "season": season, "team": team},
                               ttl=24 * 3600)

    async def head_to_head(self, home_id: int, away_id: int) -> list[dict]:
        a, b = sorted((home_id, away_id))
        return await self._get("/fixtures/headtohead", {"h2h": f"{a}-{b}", "last": 10}, ttl=24 * 3600) or []

    # ---- high level ------------------------------------------------------
    async def find_fixture(self, home: str, away: str, kickoff: datetime) -> dict | None:
        fixtures = await self.fixtures_on(kickoff.strftime("%Y-%m-%d"))
        best, best_score = None, 0.0
        for f in fixtures:
            try:
                fh, fa = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
                ts = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if abs((ts - kickoff).total_seconds()) > 3 * 3600:
                continue
            s = (team_score(home, fh) + team_score(away, fa)) / 2
            if s > best_score:
                best, best_score = f, s
        return best if best_score >= 72 else None

    @staticmethod
    def _f(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _team(self, raw: dict | None, team_id: int, name: str) -> TeamStats | None:
        if not raw:
            return None
        g = raw.get("goals") or {}
        played = (raw.get("fixtures") or {}).get("played") or {}
        return TeamStats(
            team_id=team_id, name=name,
            played_home=int(played.get("home") or 0), played_away=int(played.get("away") or 0),
            gf_home=self._f(((g.get("for") or {}).get("average") or {}).get("home")),
            ga_home=self._f(((g.get("against") or {}).get("average") or {}).get("home")),
            gf_away=self._f(((g.get("for") or {}).get("average") or {}).get("away")),
            ga_away=self._f(((g.get("against") or {}).get("average") or {}).get("away")),
            form=(raw.get("form") or "")[-10:],
        )

    async def match_stats(self, home: str, away: str, kickoff: datetime) -> MatchStats | None:
        """Up to ~4 requests the first time a match is analysed (1 shared per day), 0 after (cached)."""
        if not self.enabled:
            return None
        self._spent_this_call = 0
        try:
            fx = await self.find_fixture(home, away, kickoff)
            if not fx:
                return None
            league_id, season = fx["league"]["id"], fx["league"]["season"]
            hid, aid = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"]
            hraw = await self.team_statistics(league_id, season, hid)
            araw = await self.team_statistics(league_id, season, aid)
            h2h_raw = await self.head_to_head(hid, aid)
        except QuotaExceeded as exc:
            log.info("%s; using odds-only model", exc)
            return None
        h2h: list[tuple[int, int]] = []
        for f in h2h_raw:
            try:
                gh, ga = f["goals"]["home"], f["goals"]["away"]
                if gh is None or ga is None:
                    continue
                if f["teams"]["home"]["id"] == hid:
                    h2h.append((int(gh), int(ga)))
                else:
                    h2h.append((int(ga), int(gh)))
            except (KeyError, TypeError):
                continue
        return MatchStats(
            fixture_id=fx["fixture"]["id"], league_id=league_id, season=season,
            home=self._team(hraw, hid, fx["teams"]["home"]["name"]),
            away=self._team(araw, aid, fx["teams"]["away"]["name"]),
            h2h=h2h, requests_used=self._spent_this_call)
