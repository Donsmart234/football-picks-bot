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

from matchstate import ABS_CAP_MIN, FROZEN_S, LIVE_CAP_MIN, clock_overrun, is_sr_id, sb_state, sr_state
from srstats import BASE as SR_BASE, HEADERS as SR_HEADERS

log = logging.getLogger(__name__)
LIST_TTL = 20
EVENT_TTL = 8
FEED_TTL = 10
STALE_OK_S = 60            # a failed list refresh may reuse the last list only this long
ENDED_KEEP_S = 45 * 60     # finished matches stay in the Ended section this long
RECHECK_MAX = 8            # event / Sportradar rechecks per list refresh (relay calls)
GONE_STOP_GRACE_S = 10 * 60   # off the live list and this long past SportyBet's estimateStopTime -> ended
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


def _minute(played: str | None, status: str | None, sb_status=1) -> str:
    state, badge = sb_state(sb_status, status)
    if state != "live":
        return badge                # FT / AET / PEN / ABD / CANC / PPD / INT (never "91'")
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
    state, badge = sb_state(e.get("status"), e.get("matchStatus"))
    return {"id": e.get("eventId"), "home": home, "away": away,
            "home_uid": str(e.get("homeTeamId") or "").rsplit(":", 1)[-1],
            "away_uid": str(e.get("awayTeamId") or "").rsplit(":", 1)[-1],
            "league": f"{category} · {league}" if category else league, "category": category,
            "tid": tournament.get("id", ""),
            "score": (e.get("setScore") or "").replace(":", "–"), "ht": (e.get("gameScore") or [None])[0],
            "minute": _minute(e.get("playedSeconds"), e.get("matchStatus"), e.get("status")),
            "status": e.get("matchStatus") or "", "state": state, "badge": badge,
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


def finished_row(row: dict, state: str, badge: str, score: str | None = None, at: float | None = None) -> dict:
    """A list row turned into an Ended-section row: FT / AET / PEN / ABD / CANC / PPD badge, final score, no odds."""
    out = dict(row)
    out.update(state=state, badge=badge or "FT", minute=badge or "FT", odds=[None, None, None],
               ended_at=int(at or time.time()))
    if score is not None:
        out["score"] = score
    out["url"] = sportybet_url(row.get("id", ""), row.get("category", ""), (row.get("league", "").split(" · ")[-1]),
                               row.get("home", ""), row.get("away", ""), False)
    return out


class Live:
    """Live list = SportyBet's in-play feed, checked so a finished match never stays "live":
    - a row whose matchStatus says Ended / AET / AP / abandoned / postponed / cancelled leaves the Live list;
    - a match that drops out of the feed is re-checked on its event page (and Sportradar) and moves to Ended
      with its final score and status, unless the event page still shows it running;
    - past kickoff + LIVE_CAP_MIN a match stays live only while it moves (clock / score / status changed in the
      last FROZEN_S) or Sportradar says it's running; past kickoff + ABS_CAP_MIN only on Sportradar's word;
    - a failed refresh reuses the last list for STALE_OK_S at most."""

    def __init__(self, bookmaker):
        self.bm = bookmaker
        self._client = httpx.AsyncClient(headers=SR_HEADERS, timeout=httpx.Timeout(10.0))
        self._list: tuple[float, list[dict], dict] | None = None
        self._lock = asyncio.Lock()
        self._ev: dict[str, tuple[float, dict | None]] = {}
        self._feed: dict[str, tuple[float, dict | None]] = {}
        self._track: dict[str, dict] = {}     # eid -> {sig, changed, seen, row}
        self._ended: dict[str, dict] = {}     # eid -> Ended-section row
        self._dead: dict[str, tuple | None] = {}   # eid -> the frozen feed signature it was called ended on
        self.ids_now: set[str] = set()             # ids in SportyBet's live list at the last refresh

    async def aclose(self) -> None:
        await self._client.aclose()

    def ended(self) -> list[dict]:
        now = time.time()
        for k in [k for k, v in self._ended.items() if now - v.get("ended_at", now) > ENDED_KEEP_S]:
            self._ended.pop(k, None)
        for k in [k for k in self._dead if k not in self._ended]:
            self._dead.pop(k, None)
        return sorted(self._ended.values(), key=lambda r: -r.get("ended_at", 0))[:20]

    async def _fetch_list(self) -> list:
        url, q = self.bm._url("/factsCenter/liveOrPrematchEvents", {"sportId": "sr:sport:1", "marketId": "1"})
        r = await self.bm._client.get(url, params=q)
        self.bm._check(r)
        data = (r.json() or {}).get("data") or []
        return data if isinstance(data, list) else []

    async def list(self, force: bool = False) -> list[dict]:
        if self._list and not force and time.time() - self._list[0] < LIST_TTL:
            return self._list[1]
        async with self._lock:
            if self._list and not force and time.time() - self._list[0] < LIST_TTL:
                return self._list[1]
            try:
                data = await self._fetch_list()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("live list failed: %s", exc.__class__.__name__)
                if self._list and time.time() - self._list[0] < STALE_OK_S:
                    return self._list[1]
                raise
            out, raw = await self.reconcile(data, time.time())
            self._list = (time.time(), out, raw)
            return out

    async def reconcile(self, data: list, now: float) -> tuple[list[dict], dict]:
        """Feed tournaments -> checked live rows (+ Ended section updates). Separate from the fetch for tests."""
        rows, raw = [], {}
        for t in data:
            for e in t.get("events") or []:
                if not e.get("eventId"):
                    continue
                lg = f"{(((e.get('sport') or {}).get('category') or {}).get('name') or '')} {t.get('name', '')}".lower()
                if any(x in lg for x in ("srl", "simulated", "esoccer", "ebattle", "virtual")):
                    continue
                st, badge = sb_state(e.get("status"), e.get("matchStatus"))
                c = compact_event(e, t)
                if st in ("ended", "void", "postponed"):
                    if e.get("status") in (1, 2) or e["eventId"] in self._track or e["eventId"] in self._ended:
                        self._finish(c, st, badge, now)
                    self._track.pop(e["eventId"], None)
                    continue
                if e.get("status") not in (1, 2) and st != "interrupted":
                    continue
                sig = (e.get("matchStatus"), e.get("playedSeconds"), e.get("setScore"))
                if c["id"] in self._dead:
                    if self._dead[c["id"]] == sig:
                        continue                 # still the same frozen "live" row we already called ended
                    self._dead.pop(c["id"], None)
                    self._ended.pop(c["id"], None)
                tr = self._track.get(c["id"])
                if not tr or tr["sig"] != sig:
                    tr = {"sig": sig, "changed": now, "first": (tr or {}).get("first", now), "row": c}
                tr.update(seen=now, row=c)
                self._track[c["id"]] = tr
                rows.append(c)
                raw[c["id"]] = e
        # matches that dropped out of the feed: re-check before calling them finished
        cur = {c["id"] for c in rows}
        self.ids_now = cur
        gone = [k for k in self._track if k not in cur]
        # matches past the cap that aren't moving: re-check too
        suspect = []
        for c in rows:
            tr = self._track[c["id"]]
            age_min = (now - c["start"]) / 60 if c["start"] else 0
            frozen = now - tr["changed"] >= FROZEN_S
            first = tr["first"] == now and is_sr_id(c["id"])   # first look already past the cap: ask Sportradar
            if clock_overrun(c["status"], c["played"]) or c["start"] and (
                    age_min >= ABS_CAP_MIN or (age_min >= LIVE_CAP_MIN and (frozen or first))):
                suspect.append(c["id"])
        checks = (gone + suspect)[:RECHECK_MAX]
        verdicts = await asyncio.gather(*(self._recheck(k, now) for k in checks))
        drop = set()
        for k, (state, badge, score, still) in zip(checks, verdicts):
            tr = self._track.get(k) or {}
            row = tr.get("row") or {}
            if state == "live" and k in gone and still:
                # the event page still shows it in play and moving: a feed blip, keep it live
                if still["sig"] != tr.get("sig"):
                    tr.update(sig=still["sig"], changed=now)
                rows.append(still["row"])
                raw[k] = still["raw"]
                tr.update(seen=now, row=still["row"])
                continue
            if state == "live" and k not in gone:
                continue                       # Sportradar / event page says it's still running and moving
            self._finish(row, state if state in ("ended", "void", "postponed") else "ended", badge, now, score,
                         tr.get("sig"))
            self._track.pop(k, None)
            drop.add(k)
        for k in gone[RECHECK_MAX:]:
            tr = self._track.get(k) or {}
            if now - tr.get("seen", now) > 120 and tr.get("row"):   # couldn't re-check it: never keep it live
                self._finish(tr["row"], "ended", "Ended", now, sig=tr.get("sig"))
                self._track.pop(k, None)
        rows = [c for c in rows if c["id"] not in drop]
        for k in [k for k in self._track if now - self._track[k].get("seen", now) > 6 * 3600]:
            self._track.pop(k, None)
        return rows, raw

    def _finish(self, row: dict, state: str, badge: str, now: float, score: str | None = None, sig=None) -> None:
        """Move a match to the Ended section. `sig` = the frozen (status, clock, score) it was judged on: the feed
        showing exactly that again doesn't bring it back; any change does."""
        if not row or not row.get("id"):
            return
        prev = self._ended.get(row["id"])
        self._ended[row["id"]] = finished_row(row, state, badge, score, prev["ended_at"] if prev else now)
        self._dead[row["id"]] = sig
        self.ended()

    async def _recheck(self, eid: str, now: float) -> tuple[str, str, str | None, dict | None]:
        """(state, badge, final score or None, live row from the event page or None) for a match that left the
        feed or stopped moving. A finished status from Sportradar or SportyBet's event page wins; it stays live
        only when Sportradar's clock is running or the event page shows it in play and still moving; anything
        else is treated as ended."""
        tr = self._track.get(eid) or {}
        row = tr.get("row") or {}
        ev = await self.event(eid, ttl=0)
        sr = None
        if is_sr_id(eid):
            info = await self._sr(f"match_info/{eid.rsplit(':', 1)[-1]}")
            sr = (info or {}).get("match") or None
        score = str(ev["setScore"]).replace(":", "–") if ev and ev.get("setScore") else None
        s_state, s_badge, _ = sr_state((sr or {}).get("status"), (sr or {}).get("timeinfo"))
        e_state, e_badge = sb_state(ev.get("status"), ev.get("matchStatus")) if ev else ("unknown", "")
        res = (sr or {}).get("result") or {}
        if res.get("home") is not None and res.get("away") is not None:
            score = f"{res['home']}–{res['away']}"
        if s_state in ("ended", "void", "postponed"):
            return s_state, s_badge, score, None
        if e_state in ("ended", "void", "postponed"):
            return e_state, e_badge, score, None
        still = self._still(ev, row)
        if s_state == "live" and (sr.get("timeinfo") or {}).get("running"):
            return "live", "LIVE", score, still
        if e_state in ("live", "interrupted") and still and not clock_overrun(ev.get("matchStatus"), ev.get("playedSeconds")):
            age_min = (now - row.get("start", now)) / 60 if row.get("start") else 0
            stop = int(ev.get("estimateStopTime") or 0) / 1000
            off_list = eid not in self.ids_now
            if off_list and stop and now > stop + GONE_STOP_GRACE_S:
                return "ended", "FT" if score else "Ended", score, None
            seen_moving = tr.get("changed", 0) > tr.get("first", 0) and now - tr["changed"] < FROZEN_S
            first_look = tr.get("changed") == tr.get("first") and now - tr.get("first", 0) < FROZEN_S
            # first look with no Sportradar answer: benefit of the doubt until it's had FROZEN_S to move
            moving = still["sig"] != tr.get("sig") or seen_moving or (first_look and s_state == "unknown")
            if age_min < ABS_CAP_MIN and moving:
                return "live", "LIVE", score, still
        return "ended", "FT" if score else "Ended", score, None

    @staticmethod
    def _still(ev: dict | None, row: dict) -> dict | None:
        if not ev or not ev.get("eventId"):
            return None
        sp = (ev.get("sport") or {}).get("category") or {}
        t = sp.get("tournament") or {}
        c = compact_event(ev, {"name": t.get("name", ""), "id": t.get("id", "")})
        if row.get("league"):
            c["league"] = row["league"]
        return {"sig": (ev.get("matchStatus"), ev.get("playedSeconds"), ev.get("setScore")), "row": c, "raw": ev}

    def ids(self) -> set[str]:
        return {x["id"] for x in (self._list[1] if self._list else [])}

    async def event(self, eid: str, ttl: int = EVENT_TTL) -> dict | None:
        hit = self._ev.get(eid)
        if hit and time.time() - hit[0] < max(ttl, 0) and ttl:
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

    def head_state(self, ev: dict | None, tl: dict | None, now: float | None = None,
                   info: dict | None = None) -> tuple[str, str]:
        """(state, badge) for one match's Live view: a finished status from any source wins (Sportradar match_info,
        match_timeline, SportyBet's event page; match_timeline lags match_info by minutes at full time, seen
        2026-11-03), then the same caps as the list (a quiet "live" match long past kickoff isn't live)."""
        now = now or time.time()
        m = (tl or {}).get("match") or {}
        mi = (info or {}).get("match") or {}
        s_state, s_badge, _ = sr_state(m.get("status"), m.get("timeinfo")) if m.get("status") else ("unknown", "", "")
        i_state, i_badge, _ = sr_state(mi.get("status"), mi.get("timeinfo")) if mi.get("status") else ("unknown", "", "")
        e_state, e_badge = sb_state(ev.get("status"), ev.get("matchStatus")) if ev else ("unknown", "")
        for st, b in ((i_state, i_badge), (s_state, s_badge), (e_state, e_badge)):
            if st in ("ended", "void", "postponed"):
                return st, b
        if (ev or {}).get("eventId") in self._ended:
            r = self._ended[ev["eventId"]]
            if not (mi.get("timeinfo") or {}).get("running"):
                return r["state"], r["badge"]
        if "live" in (s_state, e_state) or "interrupted" in (s_state, e_state):
            st, b = (s_state, s_badge) if s_state in ("live", "interrupted") else (e_state, e_badge)
            start = int((ev or {}).get("estimateStartTime") or 0) / 1000
            age_min = (now - start) / 60 if start else 0
            eid = (ev or {}).get("eventId") or ""
            tr = self._track.get(eid)
            running = bool((m.get("timeinfo") or {}).get("running"))
            if s_state != "live" and ev and clock_overrun(ev.get("matchStatus"), ev.get("playedSeconds")):
                return "ended", "FT"
            stop = int((ev or {}).get("estimateStopTime") or 0) / 1000
            if s_state != "live" and stop and now > stop + GONE_STOP_GRACE_S and self._list \
                    and eid not in self.ids_now and now - self._list[0] < 120:
                return "ended", "FT"
            if start and age_min >= ABS_CAP_MIN and not running:
                return "ended", "FT"
            if start and age_min >= LIVE_CAP_MIN and not running and (
                    eid in self._ended or (tr and now - tr["changed"] >= FROZEN_S)):
                return "ended", "FT"
            return st, b
        if e_state == "notstarted" or s_state == "notstarted":
            return "notstarted", ""
        return "unknown", ""

    async def match(self, eid: str) -> dict | None:
        """Everything the Live view needs for one match."""
        num = eid.rsplit(":", 1)[-1]
        if not num.isdigit():
            return None
        ev, tl, det, info = await asyncio.gather(
            self.event(eid), self._sr(f"match_timeline/{num}"), self._sr(f"match_detailsextended/{num}"),
            self._sr(f"match_info/{num}") if is_sr_id(eid) else asyncio.sleep(0, None))
        if not ev and not tl:
            return None
        head = None
        if ev and ev.get("status") in (1, 2) and eid not in self._ended:
            sig = (ev.get("matchStatus"), ev.get("playedSeconds"), ev.get("setScore"))
            tr = self._track.get(eid)
            if tr and tr["sig"] != sig:      # the match view keeps the "still moving" clock fresh too
                tr.update(sig=sig, changed=time.time())
        state, badge = self.head_state(ev, tl, info=info)
        if ev:
            sp = (ev.get("sport") or {}).get("category") or {}
            head = compact_event(ev, {"name": (sp.get("tournament") or {}).get("name", ""),
                                      "id": (sp.get("tournament") or {}).get("id", "")})
        elif eid in self._ended:
            head = dict(self._ended[eid])
        if head is not None:
            head["state"], head["badge"] = state, badge
            head["live"] = state in ("live", "interrupted")
            head["ended"] = state in ("ended", "void", "postponed")
            if not head["live"]:
                head["minute"] = badge or ("" if state == "notstarted" else "FT")
                res = ((info or {}).get("match") or {}).get("result") or ((tl or {}).get("match") or {}).get("result") or {}
                if head["ended"] and res.get("home") is not None and res.get("away") is not None:
                    head["score"] = f"{res['home']}–{res['away']}"
        live = bool(head and head.get("live"))
        return {"event_id": eid, "head": head, "markets": live_markets(ev) if ev and live and ev.get("status") == 1 else [],
                "feed": feed_view(tl, det), "updated": int(time.time())}
