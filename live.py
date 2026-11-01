"""In-play matches: SportyBet live list + live odds (productId=1) and the free Sportradar live feed (timeline,
match stats) that SportyBet's own match pages use. No video: we never embed or link unlicensed streams. When
SportyBet's own event data flags an official stream, the app offers a link to that match on sportybet.com/ng (the
owner's own account). The Sportradar Live Match Tracker widget is licensed per client and domain-locked
(widgets.sir.sportradar.com answers 403 outside SportyBet), so the app draws its own pitch / momentum view
from the free feed instead."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from srstats import BASE as SR_BASE, HEADERS as SR_HEADERS

log = logging.getLogger(__name__)
LIST_TTL = 20
EVENT_TTL = 8
FEED_TTL = 10
SHOW_MARKETS = {"1": "1X2", "8": "Next goal", "18": "Over/Under", "10": "Double chance", "29": "GG/NG",
                "11": "Draw no bet", "7": "Rest of the match"}
STREAM_KEY = re.compile(r"stream|video|livetv|tvchannel|broadcast", re.I)
TL_TYPES = {"goal", "card", "corner", "substitution", "shotontarget", "shotofftarget", "penalty_awarded",
            "penalty_missed", "periodstart", "periodscore", "injurytimeshown", "match_ended", "video_assistant_referee",
            "goalkeeper_saved", "offside"}
SITUATION_W = {"safe": 0.0, "attack": 1.0, "dangerous": 2.0}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_")


def sportybet_url(eid: str, category: str, league: str, home: str, away: str, live: bool = True) -> str:
    """Official SportyBet NG match page (opens in the user's own SportyBet session)."""
    seg = "live/" if live else ""
    return (f"https://www.sportybet.com/ng/sport/football/{seg}{_slug(category)}/{_slug(league)}/"
            f"{_slug(home)}_vs_{_slug(away)}/{eid}")


def stream_flag(raw: dict) -> bool:
    """True when SportyBet's event JSON carries a truthy stream/video/TV field (none seen on 2026-11-01)."""
    def walk(o, depth=0):
        if depth > 3:
            return False
        if isinstance(o, dict):
            for k, v in o.items():
                if STREAM_KEY.search(str(k)) and v not in (None, False, 0, "", "0", [], {}, "false"):
                    return True
                if isinstance(v, (dict, list)) and k != "markets" and walk(v, depth + 1):
                    return True
        elif isinstance(o, list):
            return any(walk(x, depth + 1) for x in o[:20])
        return False
    return walk(raw)


def _minute(played: str | None, status: str | None) -> str:
    st = (status or "").upper()
    if st in ("HT", "HALFTIME", "PAUSE"):
        return "HT"
    if not played or ":" not in str(played):
        return {"H1": "1H", "H2": "2H", "NS": "", "ET": "ET", "PEN": "PEN", "AET": "AET", "ENDED": "FT"}.get(st, st)
    mm = int(str(played).split(":")[0])
    if st == "H1" and mm >= 45:
        return f"45+{mm - 44}'"
    if st == "H2" and mm >= 90:
        return f"90+{mm - 89}'"
    return f"{mm + 1}'"


def _odds(o) -> float | None:
    try:
        v = float(o)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def compact_event(e: dict, tournament: dict) -> dict:
    cat = ((e.get("sport") or {}).get("category") or {})
    odds = {}
    for m in e.get("markets") or []:
        if str(m.get("id")) == "1" and not m.get("specifier"):
            for o in m.get("outcomes") or []:
                odds[str(o.get("id"))] = _odds(o.get("odds")) if o.get("isActive", 1) else None
    home, away = e.get("homeTeamName", "?"), e.get("awayTeamName", "?")
    league = tournament.get("name") or ""
    category = cat.get("name", "")
    return {"id": e.get("eventId"), "home": home, "away": away,
            "home_uid": str(e.get("homeTeamId") or "").rsplit(":", 1)[-1],
            "away_uid": str(e.get("awayTeamId") or "").rsplit(":", 1)[-1],
            "league": f"{category} · {league}" if category else league, "category": category,
            "tid": tournament.get("id", ""),
            "score": (e.get("setScore") or "").replace(":", "–"), "ht": (e.get("gameScore") or [None])[0],
            "minute": _minute(e.get("playedSeconds"), e.get("matchStatus")), "status": e.get("matchStatus") or "",
            "played": e.get("playedSeconds") or "", "start": int(e.get("estimateStartTime") or 0) // 1000,
            "odds": [odds.get("1"), odds.get("2"), odds.get("3")],
            "tracker": not e.get("matchTrackerNotAllowed", False), "stream": stream_flag(e),
            "url": sportybet_url(e.get("eventId", ""), category, league, home, away, True)}


def _devig(odds: list[float | None]) -> list[float | None]:
    if not odds or any(o is None for o in odds):
        return [None] * len(odds or [])
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def live_markets(raw: dict, max_ou: int = 4) -> list[dict]:
    """Main live markets with odds and de-margined probability (full books only)."""
    out, ou = [], []
    for m in raw.get("markets") or []:
        mid = str(m.get("id"))
        if mid not in SHOW_MARKETS or m.get("status", 0) != 0:
            continue
        outs = [{"id": str(o.get("id")), "desc": o.get("desc", ""),
                 "odds": _odds(o.get("odds")) if o.get("isActive", 1) else None} for o in m.get("outcomes") or []]
        if not any(o["odds"] for o in outs):
            continue
        overlap = mid == "10"
        probs = _devig([o["odds"] for o in outs])
        if overlap and all(probs):
            probs = [p * 2 for p in probs]
        for o, p in zip(outs, probs):
            o["prob"] = round(p, 4) if p else None
        item = {"id": mid, "spec": m.get("specifier") or "", "title": m.get("desc") or SHOW_MARKETS[mid],
                "outcomes": outs}
        if mid == "18":
            ou.append(item)
        else:
            out.append(item)
    # O/U: the lines closest to a coin flip
    ou.sort(key=lambda x: min(abs((o.get("prob") or 0) - 0.5) for o in x["outcomes"]))
    order = ["1", "8", "7", "10", "29", "11"]
    out.sort(key=lambda x: order.index(x["id"]) if x["id"] in order else 99)
    return out[:1] + sorted(ou[:max_ou], key=lambda x: x["spec"]) + out[1:]


def feed_view(timeline: dict | None, details: dict | None) -> dict:
    """Timeline (key events), momentum per minute, last ball position / situation, match stats."""
    out: dict = {"events": [], "momentum": [], "ball": None, "stats": [], "status": None}
    if timeline:
        m = timeline.get("match") or {}
        out["status"] = {"name": (m.get("status") or {}).get("name"), "p": m.get("p"),
                         "result": m.get("result"), "periods": m.get("periods"),
                         "played": ((m.get("timeinfo") or {}).get("played")),
                         "running": ((m.get("timeinfo") or {}).get("running"))}
        mom: dict[int, float] = {}
        last_ball = None
        for e in timeline.get("events") or []:
            t, team = e.get("type"), e.get("team")
            minute = e.get("time") if isinstance(e.get("time"), int) else None
            if t == "matchsituation" and minute is not None and team in ("home", "away"):
                w = SITUATION_W.get(e.get("situation"), 0.0)
                mom[minute] = mom.get(minute, 0.0) + (w if team == "home" else -w)
                last_ball = {"x": e.get("X"), "y": e.get("Y"), "team": team, "situation": e.get("situation"),
                             "t": e.get("uts")}
            elif t in ("shotontarget", "shotofftarget", "goal", "corner") and minute is not None and team in ("home", "away"):
                w = {"goal": 4.0, "shotontarget": 2.5, "shotofftarget": 1.5, "corner": 1.0}[t]
                mom[minute] = mom.get(minute, 0.0) + (w if team == "home" else -w)
            if t == "ballcoordinates" and e.get("coordinates"):
                c = (e.get("coordinates") or [{}])[0]
                if last_ball is not None:
                    last_ball.update({"x": c.get("X", last_ball["x"]), "y": c.get("Y", last_ball["y"])})
            if t in TL_TYPES and not e.get("disabled"):
                item = {"type": t, "min": minute, "team": team or "", "name": e.get("name", "")}
                if t == "card":
                    item["card"] = e.get("card") or ("red" if "red" in str(e.get("name", "")).lower() else "yellow")
                if t == "goal":
                    item["score"] = f"{(e.get('result') or {}).get('home', '')}–{(e.get('result') or {}).get('away', '')}"
                    item["note"] = "own goal" if e.get("owngoal") else "penalty" if e.get("penalty") else (
                        "header" if e.get("header") else "")
                if t == "periodscore":
                    ps = e.get("periodscore") or {}
                    item["name"] = f"{e.get('name', '')} ends {ps.get('home', '')}–{ps.get('away', '')}"
                pl = (e.get("player") or {}).get("name") or ((e.get("scorer") or {}).get("name"))
                if pl:
                    item["player"] = pl
                if t == "substitution":
                    item["player"] = f"{((e.get('playerin') or {}).get('name') or '')} ↔ {((e.get('playerout') or {}).get('name') or '')}".strip(" ↔")
                if t == "injurytimeshown":
                    item["name"] = f"+{e.get('minutes') or e.get('injurytime', '')} min added"
                out["events"].append(item)
        if mom:
            top = max(mom)
            out["momentum"] = [round(mom.get(i, 0.0), 1) for i in range(0, top + 1)]
        out["ball"] = last_ball
        out["events"] = [x for x in out["events"] if x["type"] not in ("goalkeeper_saved", "offside")][-40:][::-1]
    if details:
        vals = details.get("values") or {}
        for key, label in (("110", "Possession %"), ("1126", "Attacks"), ("1029", "Dangerous attacks"),
                           ("goalattempts", "Goal attempts"), ("125", "Shots on target"), ("126", "Shots off target"),
                           ("124", "Corners"), ("40", "Yellow cards"), ("50", "Red cards"), ("123", "Offsides"),
                           ("129", "Fouls"), ("127", "Saves")):
            v = (vals.get(key) or {}).get("value") or {}
            h, a = v.get("home"), v.get("away")
            if isinstance(h, (int, float)) and isinstance(a, (int, float)):
                out["stats"].append({"k": key, "label": label, "home": h, "away": a})
    return out


class Live:
    def __init__(self, bookmaker):
        self.bm = bookmaker
        self._client = httpx.AsyncClient(headers=SR_HEADERS, timeout=httpx.Timeout(10.0))
        self._list: tuple[float, list[dict], dict] | None = None
        self._lock = asyncio.Lock()
        self._ev: dict[str, tuple[float, dict | None]] = {}
        self._feed: dict[str, tuple[float, dict | None]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list(self, force: bool = False) -> list[dict]:
        if self._list and not force and time.time() - self._list[0] < LIST_TTL:
            return self._list[1]
        async with self._lock:
            if self._list and not force and time.time() - self._list[0] < LIST_TTL:
                return self._list[1]
            try:
                url, q = self.bm._url("/factsCenter/liveOrPrematchEvents", {"sportId": "sr:sport:1", "marketId": "1"})
                r = await self.bm._client.get(url, params=q)
                self.bm._check(r)
                data = (r.json() or {}).get("data") or []
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("live list failed: %s", exc.__class__.__name__)
                if self._list:
                    return self._list[1]
                raise
            out, raw = [], {}
            for t in data if isinstance(data, list) else []:
                for e in t.get("events") or []:
                    if e.get("status") != 1 or not e.get("eventId"):
                        continue
                    lg = f"{(((e.get('sport') or {}).get('category') or {}).get('name') or '')} {t.get('name', '')}".lower()
                    if any(x in lg for x in ("srl", "simulated", "esoccer", "ebattle", "virtual")):
                        continue
                    c = compact_event(e, t)
                    out.append(c)
                    raw[c["id"]] = e
            self._list = (time.time(), out, raw)
            return out

    def ids(self) -> set[str]:
        return {x["id"] for x in (self._list[1] if self._list else [])}

    async def event(self, eid: str) -> dict | None:
        hit = self._ev.get(eid)
        if hit and time.time() - hit[0] < EVENT_TTL:
            return hit[1]
        try:
            url, q = self.bm._url("/factsCenter/event", {"eventId": eid, "productId": 1})
            r = await self.bm._client.get(url, params=q)
            self.bm._check(r)
            data = (r.json() or {}).get("data") or None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("live event %s failed: %s", eid, exc.__class__.__name__)
            return hit[1] if hit else None
        self._ev[eid] = (time.time(), data)
        if len(self._ev) > 30:
            for k, _ in sorted(self._ev.items(), key=lambda kv: kv[1][0])[:10]:
                self._ev.pop(k, None)
        return data

    async def _sr(self, path: str) -> dict | None:
        hit = self._feed.get(path)
        if hit and time.time() - hit[0] < FEED_TTL:
            return hit[1]
        try:
            r = await self._client.get(f"{SR_BASE}/{path}")
            r.raise_for_status()
            doc = (r.json().get("doc") or [{}])[0]
            data = None if doc.get("event") == "exception" else (doc.get("data") or None)
        except (httpx.HTTPError, ValueError, IndexError, AttributeError) as exc:
            log.warning("live feed %s failed: %s", path.split("/")[0], exc.__class__.__name__)
            return hit[1] if hit else None
        self._feed[path] = (time.time(), data)
        if len(self._feed) > 60:
            for k, _ in sorted(self._feed.items(), key=lambda kv: kv[1][0])[:20]:
                self._feed.pop(k, None)
        return data

    async def match(self, eid: str) -> dict | None:
        """Everything the Live view needs for one match."""
        num = eid.rsplit(":", 1)[-1]
        if not num.isdigit():
            return None
        ev, tl, det = await asyncio.gather(self.event(eid), self._sr(f"match_timeline/{num}"),
                                           self._sr(f"match_detailsextended/{num}"))
        if not ev and not tl:
            return None
        head = None
        if ev:
            sp = (ev.get("sport") or {}).get("category") or {}
            head = compact_event(ev, {"name": (sp.get("tournament") or {}).get("name", ""),
                                      "id": (sp.get("tournament") or {}).get("id", "")})
            head["live"] = ev.get("status") == 1
            head["ended"] = ev.get("status") in (2, 3, 4) or (ev.get("matchStatus") or "").lower() in ("ended", "ft")
        return {"event_id": eid, "head": head, "markets": live_markets(ev) if ev and ev.get("status") == 1 else [],
                "feed": feed_view(tl, det), "updated": int(time.time())}
