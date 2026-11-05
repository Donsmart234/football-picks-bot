"""Leg settlement from real match results.

Source: the free Sportradar feed SportyBet's own widgets use (no key):
  match_info/<id>            status (Not started / live / Ended / AET / AP / Postponed / Cancelled …), score per period
                             (periods.p1 = half time, periods.ft = regular time; extra time and penalties excluded,
                             exactly like bookmaker settlement)
  match_detailsextended/<id> corners and cards (totals + "1st/2nd half" splits), only fetched for those markets
Fallback for the score: SportyBet's results list (factsCenter/eventResultList, setScore + gameScore per half).

Auto-settled: every goals market the scanner models (1X2, Double Chance, DNB, Over/Under, team totals, GG/NG, Asian
x.5 and European handicaps, for full time / 1st half / 2nd half), odd/even goals, correct score, exact goals,
multigoal ranges, corners (O/U, 1X2, handicap, team totals, odd/even, full time + 1st half) and bookings
(O/U, 1X2, team totals; yellow = 1, red = 2). Anything else (player props, specials, minute markets, combos)
becomes 'check' (look on SportyBet) once the match has ended.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from markets import FAMILIES, spec_value
from matchstate import clock_overrun, is_sr_id, sb_state, sr_state

log = logging.getLogger(__name__)
SR = "https://stats.fn.sportradar.com/sportybet/en/Africa:Lagos/gismo"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36"),
           "Referer": "https://www.sportybet.com/", "Origin": "https://www.sportybet.com"}
# Status mapping (ended / AET / AP / abandoned / cancelled / postponed / interrupted / live phase) lives in
# matchstate.sr_state, shared with the Live view. Live phases 1h / ht / 2h allow early calls; anything else in play
# (extra time, penalties) is "other" and waits for the final whistle.


@dataclass
class MatchResult:
    state: str = "unknown"             # notstarted | live | ended | void | postponed | unknown
    status_name: str = ""
    ft: tuple[int, int] | None = None  # regular time
    ht: tuple[int, int] | None = None
    live: tuple[int, int] | None = None
    corners: dict = field(default_factory=dict)   # {"ft": (h,a), "h1": (h,a)}
    status_id: int = -1
    minute: int | None = None          # live: minute of play (from timeinfo.played seconds)
    phase: str = ""                    # live: 1h | ht | 2h | other (extra time, penalties, interruptions)
    cards: dict = field(default_factory=dict)     # {"y": (h,a), "r": (h,a), "yr": (h,a), "y_h1": .., "r_h1": ..}

    @property
    def score(self) -> str:
        s = self.ft or self.live
        return f"{s[0]}-{s[1]}" if s else ""


def _pair(d) -> tuple[int, int] | None:
    try:
        if d and d.get("home") is not None and d.get("away") is not None:
            return int(d["home"]), int(d["away"])
    except (TypeError, ValueError):
        pass
    return None


def _split(v: str) -> tuple[int, int] | None:
    try:
        a, b = str(v).split("/")
        return int(a), int(b)
    except (ValueError, TypeError):
        return None


def _minute(played, phase: str) -> int | None:
    try:
        sec = int(played)
    except (TypeError, ValueError):
        return 45 if phase == "ht" else None
    if phase == "ht":
        return 45
    return max(1, sec // 60 + 1)


class ResultsClient:
    def __init__(self, bookmaker=None):
        self._client = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(12.0))
        self.bm = bookmaker

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _doc(self, path: str) -> dict | None:
        try:
            r = await self._client.get(f"{SR}/{path}")
            r.raise_for_status()
            return (r.json().get("doc") or [{}])[0].get("data") or None
        except (httpx.HTTPError, ValueError, IndexError, AttributeError) as exc:
            log.info("results %s failed: %s", path, exc.__class__.__name__)
            return None

    async def _sb_fallback(self, event_id: str, res: MatchResult) -> None:
        """SportyBet's own (Bet Genius) matches have no Sportradar feed: read the state from SportyBet's event page.
        Ended -> regular-time score from gameScore (two halves) when the match ended in regular time; AET/AP or no
        halves -> 'unknown' (left for the user to check after 72 h). Never 'live' with a usable phase, so no early
        calls are made from this feed (phase 'other')."""
        try:
            data = await self.bm._event_raw(event_id)
        except Exception:
            data = None
        if not data or not data.get("eventId"):
            return
        state, badge = sb_state(data.get("status"), data.get("matchStatus"))
        res.status_name = str(data.get("matchStatus") or "")
        halves = [_split(str(x).replace(":", "/")) for x in (data.get("gameScore") or [])]
        if state == "ended" and badge == "FT" and len(halves) == 2 and all(halves):
            res.state = "ended"
            res.ht = halves[0]
            res.ft = (halves[0][0] + halves[1][0], halves[0][1] + halves[1][1])
        elif state == "ended":
            res.state = "unknown"
        elif state == "void":
            res.state = "void"
        elif state in ("postponed", "interrupted"):
            res.state = "postponed"
        elif state == "notstarted":
            res.state = "notstarted"
        elif state == "live" and clock_overrun(data.get("matchStatus"), data.get("playedSeconds")):
            res.state = "unknown"           # clock ticking past any real half: over, result not posted yet
        elif state == "live":
            res.state, res.phase = "live", "other"
            sc = _split(str(data.get("setScore") or "").replace(":", "/"))
            res.live = sc

    async def result(self, event_id: str, need_stats: bool = False) -> MatchResult:
        mid = event_id.rsplit(":", 1)[-1]
        res = MatchResult()
        data = await self._doc(f"match_info/{mid}")
        m = (data or {}).get("match") or {}
        st = m.get("status") or {}
        if st:
            sid, name = int(st.get("_id", -1)), str(st.get("name", ""))
            res.status_name = name
            periods = m.get("periods") or {}
            state, _badge, phase = sr_state(st, m.get("timeinfo"))
            if state == "ended":
                res.state = "ended"
                res.ft = _pair(periods.get("ft")) or (_pair(m.get("result")) if sid not in (110, 120) and
                                                      "aet" not in name.lower() and "ap" != name.lower() else None)
                res.ht = _pair(periods.get("p1"))
            elif state == "void":
                res.state = "void"
            elif state in ("postponed", "interrupted"):
                res.state = "postponed"     # waits (pending); a resumed match comes back as live
            elif state == "notstarted":
                res.state = "notstarted"
            elif state == "live":
                res.state = "live"
                res.status_id = sid
                res.live = _pair(m.get("result"))
                res.ht = _pair(periods.get("p1"))
                res.phase = phase
                res.minute = _minute((m.get("timeinfo") or {}).get("played"), res.phase)
        elif self.bm is not None and not is_sr_id(event_id):
            await self._sb_fallback(event_id, res)
        if res.state == "ended" and res.ft is None:
            res.state = "unknown"       # ended but no score yet: try again next tick
        if res.state == "ended" and need_stats:
            det = await self._doc(f"match_detailsextended/{mid}")
            vals = (det or {}).get("values") or {}

            def tot(k):
                return _pair((vals.get(k) or {}).get("value"))

            def half(k):
                v = (vals.get(k) or {}).get("value") or {}
                h, a = _split(v.get("home")), _split(v.get("away"))
                return (h[0], a[0]) if h and a else None
            if tot("124"):
                res.corners = {"ft": tot("124"), "h1": half("1634")}
            if tot("40") or tot("50"):
                res.cards = {"y": tot("40") or (0, 0), "r": tot("50") or (0, 0), "yr": tot("45") or (0, 0),
                             "y_h1": half("1627"), "r_h1": half("1628")}
        return res


# ------------------------------------------------------------------ evaluation
def _cmp(i: int, j: int, o: str, ids=("1", "2", "3")) -> bool | None:
    if o == ids[0]:
        return i > j
    if o == ids[1]:
        return i == j
    if o == ids[2]:
        return i < j
    return None


def _over_under(total: float, line: float, o: str, over_ids=("12",), under_ids=("13",)) -> str:
    if abs(line * 4) % 2 == 1:      # Asian quarter line (x.25 / x.75): split stake, half results -> manual look
        return "check"
    if total == line:               # whole (Asian) line landed exactly: push, stake back
        return "void"
        return "void"
    if o in over_ids:
        return "won" if total > line else "lost"
    if o in under_ids:
        return "won" if total < line else "lost"
    return "check"


def _wl(b: bool | None) -> str:
    return "check" if b is None else ("won" if b else "lost")


def _hcp(i: float, j: float, hcp: float, o: str, home_ids=("1714",), away_ids=("1715",)) -> str:
    d = i + hcp - j
    if abs(hcp * 4) % 2 == 1:      # quarter lines: split stakes, leave to the user
        return "check"
    if d == 0:
        return "void"
    if o in home_ids:
        return "won" if d > 0 else "lost"
    if o in away_ids:
        return "won" if d < 0 else "lost"
    return "check"


def _goals_family(leg: dict, i: int, j: int) -> str:
    fam = FAMILIES[leg["market_id"]]
    k, o, spec = fam.kind, leg["outcome_id"], leg.get("specifier") or ""
    if k == "1x2":
        return _wl(_cmp(i, j, o))
    if k == "dc":
        return _wl({"9": i >= j, "10": i != j, "11": i <= j}.get(o))
    if k == "dnb":
        if i == j:
            return "void"
        return _wl({"4": i > j, "5": i < j}.get(o))
    if k in ("total", "team_total"):
        line = float(spec_value(spec, "total") or "nan")
        t = i + j if k == "total" else (i if fam.side == "home" else j)
        return _over_under(t, line, o)
    if k == "btts":
        return _wl({"74": i > 0 and j > 0, "76": not (i > 0 and j > 0)}.get(o))
    if k == "ah":
        return _hcp(i, j, float(spec_value(spec, "hcp") or "nan"), o)
    if k == "eh":
        a, b = (int(x) for x in (spec_value(spec, "hcp") or "0:0").split(":"))
        return _wl(_cmp(i + a, j + b, o, ("1711", "1712", "1713")))
    return "check"


_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)|(\+))?\s*$")


def _in_range(n: int, desc: str) -> bool | None:
    m = _RANGE.match(desc or "")
    if not m:
        return None
    lo = int(m.group(1))
    if m.group(3):
        return n >= lo
    hi = int(m.group(2)) if m.group(2) else lo
    return lo <= n <= hi


CORNER = {"166": ("ft", "total"), "177": ("h1", "total"), "162": ("ft", "1x2"), "173": ("h1", "1x2"),
          "165": ("ft", "hcp"), "176": ("h1", "hcp"), "900300": ("ft", "home"), "900301": ("ft", "away"),
          "900302": ("h1", "home"), "900303": ("h1", "away"), "172": ("ft", "oddeven"), "183": ("h1", "oddeven")}
BOOKING = {"139": ("ft", "total"), "152": ("h1", "total"), "136": ("ft", "1x2"), "149": ("h1", "1x2"),
           "900304": ("ft", "home"), "900305": ("ft", "away"), "900306": ("h1", "home"), "900307": ("h1", "away"),
           "900312": ("ft", "hcp")}


def needs_stats(leg: dict) -> bool:
    return leg.get("market_id") in CORNER or leg.get("market_id") in BOOKING


def _count_market(kind: str, h: int, a: int, leg: dict) -> str:
    o, spec = leg["outcome_id"], leg.get("specifier") or ""
    if kind == "total":
        return _over_under(h + a, float(spec_value(spec, "total") or "nan"), o)
    if kind in ("home", "away"):
        return _over_under(h if kind == "home" else a, float(spec_value(spec, "total") or "nan"), o,
                           ("30", "12"), ("31", "13"))
    if kind == "1x2":
        return _wl(_cmp(h, a, o))
    if kind == "hcp":
        return _hcp(h, a, float(spec_value(spec, "hcp") or "nan"), o, ("1714", "1"), ("1715", "3"))
    if kind == "oddeven":
        return _wl({"70": (h + a) % 2 == 1, "72": (h + a) % 2 == 0}.get(o))
    return "check"


def settle_leg(leg: dict, r: MatchResult) -> tuple[str, str]:
    """Returns (status, note). status: won | lost | void | check | pending | live."""
    if r.state == "void":
        return "void", f"match {r.status_name.lower() or 'cancelled'}"
    if r.state in ("notstarted", "unknown", "postponed"):
        return ("pending", r.status_name.lower()) if r.state != "postponed" else ("pending", "postponed")
    if r.state == "live":
        return "live", r.status_name
    ft, ht = r.ft, r.ht
    mid = leg.get("market_id", "")
    try:
        if mid == MINUTE_1X2:
            return _minute_1x2_final(leg, ft)
        if mid in FAMILIES:
            fam = FAMILIES[mid]
            if fam.period == "ft":
                return _goals_family(leg, *ft), ""
            if ht is None:
                return "check", "no half-time score in the feed"
            if fam.period == "h1":
                return _goals_family(leg, *ht), f"HT {ht[0]}-{ht[1]}"
            return _goals_family(leg, ft[0] - ht[0], ft[1] - ht[1]), f"2H {ft[0] - ht[0]}-{ft[1] - ht[1]}"
        market = (leg.get("market") or "").lower()
        pick = leg.get("pick") or ""
        half = "1st half" in market or "2nd half" in market
        if mid == "26" or (market.startswith("odd/even") and "corner" not in market and not half):
            return _wl({"70": sum(ft) % 2 == 1, "72": sum(ft) % 2 == 0}.get(leg["outcome_id"])), ""
        if market == "correct score" and re.fullmatch(r"\d+:\d+", pick.strip()):
            a, b = (int(x) for x in pick.split(":"))
            return _wl((a, b) == tuple(ft)), ""
        if market in ("exact goals", "multigoals", "goal range", "total goals range") and not half:
            return _wl(_in_range(sum(ft), pick)), ""
        if mid in CORNER:
            per, kind = CORNER[mid]
            c = r.corners.get(per)
            if not c:
                return "check", "no corner data in the feed"
            return _count_market(kind, c[0], c[1], leg), f"corners {c[0]}-{c[1]}"
        if mid in BOOKING:
            per, kind = BOOKING[mid]
            cd = r.cards
            if not cd:
                return "check", "no card data in the feed"
            if per == "ft":
                y, rd, yr = cd["y"], cd["r"], cd.get("yr") or (0, 0)
            else:
                y, rd, yr = cd.get("y_h1"), cd.get("r_h1"), (0, 0)
                if y is None or rd is None:
                    return "check", "no 1st-half card data"
            if sum(yr):
                return "check", "second-yellow red card: check SportyBet's settlement"
            h, a = y[0] + 2 * rd[0], y[1] + 2 * rd[1]
            return _count_market(kind, h, a, leg), f"bookings {h}-{a}"
    except (TypeError, ValueError) as exc:
        log.info("settle %s failed: %s", mid, exc)
    return "check", "market not auto-settled: check SportyBet"


MINUTE_1X2 = "900069"      # SportyBet "1X2 from 1 to N minute" (specifier minute=N), e.g. the 5-minute draw


def _minute_1x2_final(leg: dict, ft) -> tuple[str, str]:
    """Final result alone settles it only when no goal was scored at all (0-0: level at every minute)."""
    if ft is not None and tuple(ft) == (0, 0):
        return _wl({"1": False, "2": True, "3": False}.get(leg.get("outcome_id", ""))), "0-0: level throughout"
    return "check", "minute market: needs the goal times, check SportyBet"


def slip_status(legs: list[dict]) -> str:
    """pending | won | lost | check (all decided legs won/void but some need a manual look)."""
    st = [l.get("status") for l in legs]
    if "lost" in st:
        return "lost"
    if any(s in ("pending", "live", None, "") for s in st):
        return "pending"
    if "check" in st:
        return "check"
    return "won" if any(s == "won" for s in st) else "void"


def effective_odds(legs: list[dict]) -> float:
    t = 1.0
    for l in legs:
        if l.get("status") != "void":
            t *= l.get("odds") or 1.0
    return round(t, 2)


# ------------------------------------------------------------------ early settlement (live score)
def _lock_goals(kind: str, side: str | None, spec: str, o: str, i: int, j: int) -> str | None:
    """Outcome already certain from a running score that can only go up? 'won' / 'lost' / None (not yet)."""
    if kind in ("total", "team_total"):
        line = float(spec_value(spec, "total") or "nan")
        if line != line:
            return None
        t = i + j if kind == "total" else (i if side == "home" else j)
        if abs(line * 4) % 2 == 1:        # quarter line: fully decided only past its upper half-line
            line += 0.25
        if t > line:                      # line already passed: over has landed, under is gone
            return "won" if o == "12" else "lost" if o == "13" else None
        return None
    if kind == "btts":
        if i > 0 and j > 0:
            return "won" if o == "74" else "lost" if o == "76" else None
        return None
    return None                           # 1X2, DC, DNB, handicaps: a later goal can still flip them


def early_settle(leg: dict, r: MatchResult) -> tuple[str, str] | None:
    """Mathematically certain result of a leg while its match is still in play, or None.

    Only goal counts that can't go back down are used (regular time only: no calls in extra time / penalties /
    interruptions). Won early: Over x.5 / team Over x.5 passed, GG Yes after both scored, a range "n+" reached.
    Lost early: Under x.5 / team Under passed, GG No after both scored, exact goals / multigoals / correct score
    already exceeded. 1st-half markets are settled in full once half time is reached. 1X2, Double Chance, DNB,
    handicaps, odd/even, corners and cards always wait for the final whistle."""
    if r.state != "live" or r.live is None or r.phase not in ("1h", "ht", "2h"):
        return None
    i, j = r.live
    mid = leg.get("market_id", "")
    tag = f"({r.minute}')" if r.minute else ("(HT)" if r.phase == "ht" else "")
    out: str | None = None
    try:
        if mid == MINUTE_1X2:
            # "1X2 from 1 to N minute": 0-0 with the clock already past minute N+1 means the window ended level
            n = int(spec_value(leg.get("specifier") or "", "minute") or "0")
            past = r.phase in ("ht", "2h") or (r.minute is not None and r.minute >= n + 2)
            if n and past and (i, j) == (0, 0):
                out = {"1": "lost", "2": "won", "3": "lost"}.get(leg.get("outcome_id", ""))
        elif mid in FAMILIES:
            fam = FAMILIES[mid]
            o, spec = leg.get("outcome_id", ""), leg.get("specifier") or ""
            if fam.period == "ft":
                out = _lock_goals(fam.kind, fam.side, spec, o, i, j)
            elif fam.period == "h1":
                if r.phase in ("ht", "2h") and r.ht is not None:
                    full = _goals_family(leg, *r.ht)        # first half is over: its score is final
                    out = full if full in ("won", "lost") else None
                    tag = f"(HT {r.ht[0]}-{r.ht[1]})"
                elif r.phase == "1h":
                    out = _lock_goals(fam.kind, fam.side, spec, o, i, j)
            elif fam.period == "h2" and r.phase == "2h" and r.ht is not None:
                out = _lock_goals(fam.kind, fam.side, spec, o, i - r.ht[0], j - r.ht[1])
        else:
            market = (leg.get("market") or "").lower()
            pick = (leg.get("pick") or "").strip()
            half = "1st half" in market or "2nd half" in market
            if market == "correct score" and re.fullmatch(r"\d+:\d+", pick):
                a, b = (int(x) for x in pick.split(":"))
                out = "lost" if (i > a or j > b) else None
            elif market in ("exact goals", "multigoals", "goal range", "total goals range") and not half:
                m = _RANGE.match(pick)
                if m:
                    lo = int(m.group(1))
                    hi = None if m.group(3) else (int(m.group(2)) if m.group(2) else lo)
                    if hi is None:
                        out = "won" if i + j >= lo else None
                    else:
                        out = "lost" if i + j > hi else None
    except (TypeError, ValueError):
        return None
    if out is None:
        return None
    return out, f"{'Won' if out == 'won' else 'Lost'} early {tag}".strip()
