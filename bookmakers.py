"""Bookmaker adapters. SportyBetNG uses SportyBet Nigeria's public web-app endpoints (unofficial)."""
from __future__ import annotations

import abc
import re
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

import config

log = logging.getLogger(__name__)

# Market / outcome ids, verified against a live pcUpcomingEvents response on 2026-10-31.
MARKET_1X2 = "1"          # outcomes 1=Home, 2=Draw, 3=Away
MARKET_OU = "18"          # specifier total=<line>; outcomes 12=Over, 13=Under
MARKET_DC = "10"          # outcomes 9=Home or Draw (1X), 10=Home or Away (12), 11=Draw or Away (X2)
MARKET_BTTS = "29"        # "GG/NG"; outcomes 74=Yes, 76=No
OU_SPECIFIER = "total=2.5"

# (market_id, outcome_id) -> (short label, specifier)
SELECTIONS: dict[tuple[str, str], tuple[str, str]] = {
    (MARKET_1X2, "1"): ("1 (Home)", ""),
    (MARKET_1X2, "2"): ("X (Draw)", ""),
    (MARKET_1X2, "3"): ("2 (Away)", ""),
    (MARKET_OU, "12"): ("Over 2.5", OU_SPECIFIER),
    (MARKET_OU, "13"): ("Under 2.5", OU_SPECIFIER),
    (MARKET_DC, "9"): ("1X", ""),
    (MARKET_DC, "10"): ("12", ""),
    (MARKET_DC, "11"): ("X2", ""),
    (MARKET_BTTS, "74"): ("GG (BTTS Yes)", ""),
    (MARKET_BTTS, "76"): ("NG (BTTS No)", ""),
}


@dataclass
class Outcome:
    id: str
    desc: str
    odds: float | None
    active: bool


@dataclass
class Event:
    event_id: str
    home: str
    away: str
    start: datetime            # timezone-aware UTC
    league: str
    category: str
    tournament_id: str
    markets: dict[tuple[str, str], dict[str, Outcome]] = field(default_factory=dict)
    home_id: str = ""          # sr:competitor:<uid>
    away_id: str = ""
    market_count: int = 0
    category_id: str = ""      # sr:category:<id> (Sportradar real category)

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def league_full(self) -> str:
        return f"{self.category} · {self.league}" if self.category else self.league

    def odds(self, market_id: str, outcome_id: str, specifier: str = "") -> float | None:
        o = self.markets.get((market_id, specifier), {}).get(outcome_id)
        return o.odds if o and o.active else None


@dataclass
class Selection:
    event_id: str
    market_id: str
    specifier: str
    outcome_id: str


class BookingError(RuntimeError):
    pass


# SportyBet's own share link for a booking code (the format SportyBet posts itself). www.sportybet.com is an
# app-claimed domain: its apple-app-site-association lists "/" + ?shareCode=*&c=xx as "Open share code" for the iOS
# app (7423W9V4WF.com.sportybet.SportyBet) and its assetlinks.json verifies com.sportybet.android(.gp) on Android, so
# the link opens the installed app straight on the code and falls back to the website (which loads the code) otherwise.
SPORTYBET_SHARE = "https://www.sportybet.com/?shareCode={code}&c={country}"


def sportybet_share_url(code: str, country: str = "ng") -> str:
    c = re.sub(r"[^A-Za-z0-9]", "", str(code or ""))[:16]
    if not c:
        return "https://www.sportybet.com/ng/m/code-hub/load-code"
    return SPORTYBET_SHARE.format(code=c.upper(), country=country)


def code_link(bookmaker, code: str | None) -> str:
    """Best link for `code` on `bookmaker` (works with any adapter-like object); load-code page without a code."""
    if bookmaker is None:
        return sportybet_share_url(code or "")
    fn = getattr(bookmaker, "code_url", None)
    if code and callable(fn):
        try:
            return fn(code) or getattr(bookmaker, "load_code_url", "")
        except Exception:
            pass
    return getattr(bookmaker, "load_code_url", "") or "https://www.sportybet.com/ng/m/code-hub/load-code"


class BookmakerAdapter(abc.ABC):
    """Interface every bookmaker integration implements."""

    name: str = "bookmaker"
    load_code_url: str = ""

    def code_url(self, code: str) -> str:
        """Link that opens this booking code (default: the generic load-code page)."""
        return self.load_code_url

    @abc.abstractmethod
    async def list_events(self, hours: int = 24) -> list[Event]:
        """Upcoming football events with odds for the main markets."""

    @abc.abstractmethod
    async def book(self, selections: list[Selection]) -> str:
        """Create a booking (share) code for the selections and return it. No money involved."""

    async def get_event(self, event_id: str) -> Event | None:
        for hours in (24, 72):
            for e in await self.list_events(hours):
                if e.event_id == event_id:
                    return e
        return None

    async def aclose(self) -> None:  # pragma: no cover - optional
        pass


class SportyBetNG(BookmakerAdapter):
    name = "SportyBet NG"
    load_code_url = "https://www.sportybet.com/ng/m/code-hub/load-code"
    BASE = "https://www.sportybet.com/api/ng"

    def code_url(self, code: str) -> str:
        return sportybet_share_url(code)
    HEADERS = {
        # A plain/library User-Agent gets a CloudFront 403; a browser UA is required.
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sportybet.com/ng/",
        "Origin": "https://www.sportybet.com",
    }
    PAGE_SIZE = 100
    MAX_PAGES = 60

    def __init__(self, cache_seconds: int = config.SPORTYBET_CACHE_SECONDS, relay_url: str | None = None):
        # Relay mode (default): every call goes through a small proxy (Supabase Edge Function) whose egress
        # IP isn't challenged by SportyBet's AWS WAF, unlike Render's. Direct mode when the relay is unset.
        self.relay = (config.SPORTYBET_RELAY_URL if relay_url is None else relay_url).strip().rstrip("/")
        self._client = httpx.AsyncClient(headers=self.HEADERS, timeout=httpx.Timeout(15.0), http2=False)
        self._cache: dict[int, tuple[float, list[Event]]] = {}
        self._cache_seconds = cache_seconds
        self._locks: dict[int, asyncio.Lock] = {}
        self._mk_cache: dict[str, tuple[float, list[dict]]] = {}
        self._mk_sem = asyncio.Semaphore(6)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str, params: dict | None = None) -> tuple[str, dict]:
        """path is relative to /api/ng (e.g. '/orders/share'). Returns (url, params) for relay or direct mode."""
        params = dict(params or {})
        if self.relay:
            return self.relay, {"p": "/api/ng" + path, **params}
        return self.BASE + path, params

    @staticmethod
    def _check(r: httpx.Response) -> None:
        upstream = r.headers.get("x-upstream-status")
        waf = r.headers.get("x-upstream-waf") or r.headers.get("x-amzn-waf-action")
        if waf or r.status_code == 202 or (upstream and upstream == "202"):
            raise httpx.HTTPStatusError(f"SportyBet WAF challenge ({waf or 202})", request=r.request, response=r)
        r.raise_for_status()

    async def _get_page(self, hours: int, page: int) -> dict:
        params = {
            "sportId": "sr:sport:1",
            "marketId": f"{MARKET_1X2},{MARKET_OU},{MARKET_DC},{MARKET_BTTS}",
            "pageSize": self.PAGE_SIZE,
            "pageNum": page,
            "option": 1,
            "timeline": hours,
        }
        for attempt in range(2):  # at most one retry
            try:
                url, q = self._url("/factsCenter/pcUpcomingEvents", params)
                r = await self._client.get(url, params=q)
                self._check(r)
                body = r.json()
                return body.get("data") or {}
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 1:
                    raise
                log.warning("SportyBet page %s failed (%s), retrying once", page, exc)
                await asyncio.sleep(1.5)
        return {}

    @staticmethod
    def _parse_event(raw: dict, tournament: dict) -> Event:
        sport = raw.get("sport") or {}
        cat = (sport.get("category") or {})
        markets: dict[tuple[str, str], dict[str, Outcome]] = {}
        for m in raw.get("markets") or []:
            key = (str(m.get("id")), m.get("specifier") or "")
            outs = {}
            for o in m.get("outcomes") or []:
                try:
                    odds = float(o.get("odds"))
                except (TypeError, ValueError):
                    odds = None
                outs[str(o.get("id"))] = Outcome(
                    id=str(o.get("id")), desc=o.get("desc", ""), odds=odds,
                    active=bool(o.get("isActive", 1)) and m.get("status", 0) == 0)
            markets[key] = outs
        return Event(
            event_id=raw["eventId"],
            home=raw.get("homeTeamName", "?"),
            away=raw.get("awayTeamName", "?"),
            start=datetime.fromtimestamp(raw.get("estimateStartTime", 0) / 1000, tz=timezone.utc),
            league=tournament.get("name") or (cat.get("tournament") or {}).get("name", ""),
            category=cat.get("name", ""),
            tournament_id=tournament.get("id", ""),
            markets=markets,
            home_id=str(raw.get("homeTeamId") or ""),
            away_id=str(raw.get("awayTeamId") or ""),
            market_count=int(raw.get("totalMarketSize") or 0),
            category_id=str(cat.get("id") or ""),
        )

    def _extract(self, data: dict, seen: set[str], out: list[Event]) -> int:
        n = 0
        for t in data.get("tournaments") or []:
            for raw in t.get("events") or []:
                eid = raw.get("eventId")
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                try:
                    out.append(self._parse_event(raw, t))
                    n += 1
                except Exception as exc:  # malformed event: skip it, keep the rest
                    log.debug("skip event %s: %s", eid, exc)
        return n

    async def list_events(self, hours: int = 24) -> list[Event]:
        cached = self._cache.get(hours)
        if cached and time.time() - cached[0] < self._cache_seconds:
            return cached[1]
        lock = self._locks.setdefault(hours, asyncio.Lock())
        async with lock:
            cached = self._cache.get(hours)
            if cached and time.time() - cached[0] < self._cache_seconds:
                return cached[1]
            seen: set[str] = set()
            events: list[Event] = []
            first = await self._get_page(hours, 1)
            self._extract(first, seen, events)
            total = int(first.get("totalNum") or 0)
            pages = min(self.MAX_PAGES, math.ceil(total / self.PAGE_SIZE) + 1) if total else 1
            sem = asyncio.Semaphore(4)

            async def fetch(p: int) -> dict:
                async with sem:
                    try:
                        return await self._get_page(hours, p)
                    except Exception as exc:
                        log.warning("SportyBet page %s skipped: %s", p, exc)
                        return {}

            results = await asyncio.gather(*(fetch(p) for p in range(2, pages + 1)))
            for data in results:
                self._extract(data, seen, events)
            events.sort(key=lambda e: (e.start, e.league_full, e.home))
            log.info("SportyBet: %d events (totalNum=%d, %d pages, %dh)", len(events), total, pages, hours)
            self._cache[hours] = (time.time(), events)
            return events

    @staticmethod
    def parse_markets(data: dict, families: dict | None = None) -> list[dict]:
        """Open markets of a factsCenter/event payload: [{id, specifier, desc, group, source, outcomes:[...]}]."""
        out = []
        for m in data.get("markets") or []:
            mid = str(m.get("id"))
            if families is not None and mid not in families:
                continue
            if m.get("status", 0) != 0:
                continue
            outs = []
            for o in m.get("outcomes") or []:
                try:
                    odds = float(o.get("odds"))
                except (TypeError, ValueError):
                    odds = None
                try:
                    prob = float(o.get("probability")) if o.get("probability") is not None else None
                except (TypeError, ValueError):
                    prob = None
                outs.append({"id": str(o.get("id")), "desc": o.get("desc", ""), "odds": odds,
                             "active": bool(o.get("isActive", 1)), "prob": prob})
            out.append({"id": mid, "specifier": m.get("specifier") or "", "desc": m.get("desc") or m.get("name", ""),
                        "group": m.get("group", ""), "source": m.get("sourceType", ""), "outcomes": outs})
        return out

    async def _event_raw(self, event_id: str) -> dict | None:
        async with self._mk_sem:
            try:
                url, q = self._url("/factsCenter/event", {"eventId": event_id, "productId": 3})
                r = await self._client.get(url, params=q)
                self._check(r)
                return (r.json() or {}).get("data") or {}
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("event markets %s failed: %s", event_id, exc.__class__.__name__)
                return None

    async def event_markets(self, event_id: str, families: dict | None = None,
                            ttl: int = 600, cache: bool = True) -> list[dict] | None:
        """Full market list of one event (GET /factsCenter/event?eventId=..&productId=3), filtered to Betradar
        markets in `families`. Each item: {id, specifier, desc, outcomes:[{id, desc, odds, active, prob}]}.
        Cached `ttl` seconds. Returns None when SportyBet didn't answer or the event isn't open for betting."""
        hit = self._mk_cache.get(event_id)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        data = await self._event_raw(event_id)
        if data is None:
            return None
        if not data or data.get("status", 0) != 0:
            return []
        out = self.parse_markets(data, families)
        if cache:   # big matches carry 1,000+ markets: keep only a few raw books in memory
            self._mk_cache[event_id] = (time.time(), out)
            if len(self._mk_cache) > 12:
                for k, _ in sorted(self._mk_cache.items(), key=lambda kv: kv[1][0])[:6]:
                    self._mk_cache.pop(k, None)
        return out

    async def event_detail(self, event_id: str) -> tuple[Event | None, list[dict] | None, dict]:
        """(Event built from the event header, open pre-match markets or None, raw header without markets).
        Works for any listed event, including ones beyond the 72 h browse window."""
        data = await self._event_raw(event_id)
        if data is None:
            return None, None, {}
        if not data.get("eventId"):
            return None, [], {}
        hdr = {k: v for k, v in data.items() if k != "markets"}
        cat = ((data.get("sport") or {}).get("category") or {})
        tour = cat.get("tournament") or {}
        try:
            ev = Event(event_id=data["eventId"], home=data.get("homeTeamName", "?"), away=data.get("awayTeamName", "?"),
                       start=datetime.fromtimestamp((data.get("estimateStartTime") or 0) / 1000, tz=timezone.utc),
                       league=tour.get("name", ""), category=cat.get("name", ""), tournament_id=tour.get("id", ""),
                       home_id=str(data.get("homeTeamId") or ""), away_id=str(data.get("awayTeamId") or ""),
                       market_count=len(data.get("markets") or []), category_id=str(cat.get("id") or ""))
        except (TypeError, ValueError, OSError):
            return None, [], hdr
        markets = self.parse_markets(data) if data.get("status", 0) == 0 else []
        return ev, markets, hdr

    async def share(self, code: str) -> dict:
        """Load a booking code (GET /orders/share/<code>). Returns {ok: True, data} or {ok: False, error, kind}
        where kind is invalid | unavailable (SportyBet didn't answer)."""
        try:
            url, q = self._url(f"/orders/share/{code}")
            r = await self._client.get(url, params=q)
            self._check(r)
            payload = r.json() or {}
        except httpx.HTTPStatusError as exc:
            return {"ok": False, "kind": "unavailable",
                    "error": f"SportyBet didn't answer (HTTP {exc.response.status_code}). Try again in a minute."}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "kind": "unavailable",
                    "error": f"SportyBet didn't answer ({exc.__class__.__name__}). Try again in a minute."}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data or str(payload.get("bizCode")) != "10000":
            msg = (payload.get("message") or payload.get("innerMsg") or "") if isinstance(payload, dict) else ""
            return {"ok": False, "kind": "invalid",
                    "error": (msg or "The code is invalid") .rstrip(".") + " (wrong, expired or never booked)."}
        return {"ok": True, "data": data}

    async def load_code(self, code: str) -> dict | None:
        """Read a booking code back (GET /orders/share/<code>): {legs, unavailable}. None on failure."""
        res = await self.share(code)
        if not res["ok"]:
            return None
        data = res["data"]
        return {"legs": len(data.get("outcomes") or []), "unavailable": len(data.get("unavailableOutcomes") or [])}

    async def book(self, selections: list[Selection]) -> str:
        if not selections:
            raise BookingError("No selections to book.")
        body = {"selections": [
            {"eventId": s.event_id, "marketId": s.market_id, "specifier": s.specifier or "",
             "outcomeId": s.outcome_id} for s in selections]}
        try:
            url, q = self._url("/orders/share")
            r = await self._client.post(url, params=q, json=body, headers={"Content-Type": "application/json"})
            if r.headers.get("x-upstream-waf") or r.status_code == 202:
                raise BookingError("SportyBet's firewall blocked the request. Try again in a minute.")
            payload = r.json()
        except httpx.TimeoutException as exc:
            raise BookingError("SportyBet took too long to answer. Try again in a minute.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BookingError(f"SportyBet did not answer the booking request ({exc.__class__.__name__}).") from exc
        code = (payload.get("data") or {}).get("shareCode") if isinstance(payload, dict) else None
        if not code:
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)[:200]
            raise BookingError(f"SportyBet refused the booking: {msg} (HTTP {r.status_code})")
        return code
