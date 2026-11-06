"""5-minute draw finder: SportyBet's "1X2 from 1 to 5 minute" market, pick = Draw (score level when the first five
minutes are over: 0-0, or the rare 1-1). Ranked list of at least 50 selections per request.

SportyBet market (checked 2026-11-05 on event detail and the upcoming-events list through the relay):
    market id 900069 "1X2 from 1 to 5 minute", specifier "minute=5" (the same market also carries minute=15..85),
    outcomes 1 = Home, 2 = Draw, 3 = Away. The upcoming list returns it when asked (marketId=1,18,900069), so no
    per-event detail calls are needed; events that don't list it don't offer it (checked against event detail).

Estimate per match (pre-match, an estimate, never a guarantee):
    p_market  SportyBet's three prices for the market, margin removed.
    p_model   goals in minutes 1-5 ~ Poisson: expected match goals (fitted to SportyBet's 1X2 + Over/Under 2.5 book,
              blended 70/30 with the Sportradar form model when both teams have >= 4 recent games) x SHARE5 (share of
              all goals scored in minutes 1-5) x the two teams' early-goal tendency (goals for + against in minutes
              1-15 of their last 10 games vs. expected, shrunk to 1.0). P(draw) = P(0-0) + P(1-1) + P(2-2).
    est       logit blend 50/50 of p_market and p_model.
SHARE5 = 0.0355 and SHARE15 = 0.1207 were measured on 2,676 goals with minute detail from 1,116 recent matches of
teams listed on SportyBet (Sportradar comments, 2026-11-05); a uniform rate would be 5/90 = 0.056, so early minutes
are clearly quieter than average and SportyBet's price for this market sits below what that history implies.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import leagues
import model

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)

MARKET_ID = "900069"
SPEC = "minute=5"
DRAW = "2"
PICK = "Draw (1-5 min)"
MARKET_NAME = "1X2 from 1 to 5 minute"
LIST_MARKETS = "1,18,900069"

SHARE5 = 0.0355          # share of a match's goals scored in minutes 1-5 (measured, see module doc)
SHARE15 = 0.1207         # share in minutes 1-15 (team early-goal tendency is read on this wider window)
EARLY_PRIOR = 1.5        # shrinkage (in expected early goals) of a team's early-goal tendency towards 1.0
W_MODEL = 0.5            # logit weight of p_model against SportyBet's de-margined price
W_STATS_LAMBDA = 0.3     # weight of the form model in expected match goals (rest: SportyBet's 1X2 + O/U book)
MIN_SELECTIONS = 50
MAX_LIST = 150           # longest list returned (bounds the stats look-ups)
STATS_BUDGET_S = 45.0    # stop waiting for team stats after this; the rest are priced market + goals line only
RESULT_TTL = 600
MIN_LEAD = timedelta(minutes=5)
PERIODS = ("weekend", "midweek", "week")
PERIOD_LABEL = {"weekend": "Weekend", "midweek": "Midweek", "week": "Full week"}
_EXCLUDE = ("simulated", "srl", "esoccer", "virtual", "ebattle", "e-football")

_WANTS = re.compile(r"\b(5|five)\s*-?\s*(min|mins|minute|minutes)\b.{0,20}\bdraws?\b|\bdraws?\b.{0,25}\b(first\s+)?"
                    r"(5|five)\s*-?\s*(min|mins|minute|minutes)\b|\bdraw5\b", re.I)


def wants_draw5(text: str) -> bool:
    """'5 min draw', '5 minutes draw fixtures for the weekend', 'draw in first 5 minutes', 'draw5'."""
    return bool(_WANTS.search(text or ""))


def parse_period(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"\b(full\s*-?\s*week|whole\s+week|all\s+week|7\s*days?|next\s+week|week\s*long|week)\b", t) and \
            not re.search(r"\b(mid\s*-?\s*week|weekends?)\b", t):
        return "week"
    if re.search(r"\bmid\s*-?\s*week\b|\bweekdays?\b", t):
        return "midweek"
    if re.search(r"\bweekends?\b|\bsat(urday)?\b|\bsun(day)?\b", t):
        return "weekend"
    return None


# ------------------------------------------------------------------ periods (WAT)
def period_window(period: str, now: datetime | None = None) -> dict:
    """The upcoming window for a period, in Africa/Lagos time:
        weekend  Fri 18:00 - Sun 23:59 (Friday-evening kickoffs count as the weekend)
        midweek  Mon 00:00 - Fri 17:59 (the current one while at least 24 h of it are left, else next week's)
        week     the next 7 days from now
    Inside a window it runs from now. Returns {period, start, end (UTC, aware), label}."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    loc = now.astimezone(TZ)
    monday = (loc - timedelta(days=loc.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekend":
        ws, we = monday + timedelta(days=4, hours=18), monday + timedelta(days=7) - timedelta(seconds=1)
        start, end = max(ws, loc), we
    elif period == "midweek":
        ms, me = monday, monday + timedelta(days=4, hours=18) - timedelta(seconds=1)
        if loc > me - timedelta(hours=24):
            ms, me = ms + timedelta(days=7), me + timedelta(days=7)
        start, end = max(ms, loc), me
    else:
        period = "week"
        start, end = loc, loc + timedelta(days=7)
    return {"period": period, "start": start.astimezone(timezone.utc), "end": end.astimezone(timezone.utc),
            "label": f"{PERIOD_LABEL[period]} · {fmt_local(start)} - {fmt_local(end)} WAT"}


def fmt_local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


# ------------------------------------------------------------------ maths
def devig(odds: list[float]) -> list[float] | None:
    if not odds or any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def devig_power(odds: list[float]) -> list[float] | None:
    """Power de-margining: fair p_i = (1/o_i)^k with k such that they sum to 1. Puts more of the margin on the
    long shots than the proportional method (favourite-longshot bias), which matters for a 1.08 favourite."""
    if not odds or any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1 / o for o in odds]
    if sum(inv) <= 1.0:
        s = sum(inv)
        return [x / s for x in inv]
    lo, hi = 1.0, 10.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(x ** k for x in inv) > 1:
            lo = k
        else:
            hi = k
    out = [x ** hi for x in inv]
    s = sum(out)
    return [x / s for x in out]


def market_probs(odds: list, feed: list | None = None) -> list[float] | None:
    """The market's fair probabilities: the feed's own outcome probabilities when all three are listed (they carry no
    margin), else the power-de-margined prices."""
    if feed and all(isinstance(x, (int, float)) and 0 < x < 1 for x in feed):
        s = sum(feed)
        if 0.9 < s < 1.1:
            return [x / s for x in feed]
    return devig_power(odds)


def p_level(lh5: float, la5: float) -> float:
    """P(home goals == away goals) when both counts are Poisson(lh5), Poisson(la5): 0-0 + 1-1 + 2-2."""
    lh5, la5 = max(lh5, 0.0), max(la5, 0.0)
    x = lh5 * la5
    return math.exp(-(lh5 + la5)) * (1 + x + x * x / 4)


def early_mult(obs: float, exp: float, prior: float = EARLY_PRIOR) -> float:
    """Early-goal tendency multiplier (goals seen in minutes 1-15 vs expected), shrunk towards 1.0."""
    return (obs + prior) / (exp + prior)


def team_early(tf) -> dict | None:
    """Goals for + against in minutes 1-5 and 1-15 over a team's recent games with minute detail."""
    if tf is None:
        return None
    games = [g for g in tf.games if getattr(g, "minutes_known", False)]
    if not games:
        return None
    goals = sum(g.gf + g.ga for g in games)
    m15 = sum(1 for g in games for x in (g.min_for or ()) + (g.min_ag or ()) if x <= 15)
    m5 = sum(1 for g in games for x in (g.min_for or ()) + (g.min_ag or ()) if x <= 5)
    scored15 = sum(1 for g in games for x in (g.min_for or ()) if x <= 15)
    games15 = sum(1 for g in games if any(x <= 15 for x in (g.min_for or ()) + (g.min_ag or ())))
    return {"n": len(games), "goals": goals, "m15": m15, "m5": m5, "scored15": scored15, "games15": games15,
            "exp15": goals * SHARE15, "mult": early_mult(m15, goals * SHARE15)}


def market_lambdas(e) -> tuple[float, float] | None:
    """Expected goals (home, away) fitted to SportyBet's 1X2 and Over/Under 2.5 prices of the event."""
    from scanner import fit_lambdas
    o1 = [e.odds("1", k) for k in ("1", "2", "3")]
    p = devig(o1)
    if not p:
        return None
    t = {"h": p[0], "d": p[1], "a": p[2]}
    ou = devig([e.odds("18", "12", "total=2.5"), e.odds("18", "13", "total=2.5")])
    if ou:
        t["o"] = ou[0]
    return fit_lambdas(t)


@dataclass
class Pick:
    event_id: str
    home: str
    away: str
    league: str
    category: str
    tournament_id: str
    category_id: str
    start: datetime
    odds: float
    odds3: tuple
    p_market: float
    home_uid: str = ""
    away_uid: str = ""
    top: bool = True                              # counts as in-scope (top league, or the user's / all leagues)
    top_league: bool = True                       # on the top-leagues allowlist (leagues.py)
    open: bool = True                             # market open for bets now (False = listed but suspended)
    lam: tuple[float, float] | None = None       # expected match goals used
    lam_src: str = ""
    p_model: float | None = None
    est: float = 0.0
    mult: float = 1.0
    stats: bool = False
    why: str = ""
    early: dict = field(default_factory=dict)

    @property
    def value(self) -> float:
        return self.est * self.odds - 1

    def to_json(self, rank: int = 0) -> dict:
        return {"rank": rank, "event_id": self.event_id, "match": f"{self.home} vs {self.away}", "home": self.home,
                "away": self.away, "league": f"{self.category} · {self.league}" if self.category else self.league,
                "category": self.category, "tournament_id": self.tournament_id, "category_id": self.category_id,
                "kickoff": self.start.isoformat(), "kickoff_local": fmt_local(self.start),
                "odds": self.odds, "est": round(self.est, 4), "p_market": round(self.p_market, 4),
                "p_model": round(self.p_model, 4) if self.p_model is not None else None,
                "value": round(self.value, 4), "why": self.why, "wider": not self.top,
                "top_league": self.top_league, "open": self.open, "stats": self.stats,
                "exp_goals": round(sum(self.lam), 2) if self.lam else None, "early_mult": round(self.mult, 3),
                "market_id": MARKET_ID, "specifier": SPEC, "outcome_id": DRAW, "pick": PICK, "market": MARKET_NAME,
                "home_uid": self.home_uid, "away_uid": self.away_uid, "verdict": "confirmed" if self.stats else
                "market only"}


def _uid(cid: str) -> str:
    s = str(cid or "").rsplit(":", 1)[-1]
    return s if s.isdigit() else ""


def _excluded(e) -> bool:
    s = f"{e.category} {e.league} {e.home} {e.away}".lower()
    return any(x in s for x in _EXCLUDE)


def make_pick(e) -> Pick | None:
    """A pick for an event offering the 5-minute 1X2 (with prices), market-only estimate filled in."""
    outs = e.markets.get((MARKET_ID, SPEC)) or {}
    # SportyBet lists this (BetGenius-fed) market as suspended at times (status 2): the price is shown but no bets
    # are taken until it reopens. Such picks are kept, priced from the shown odds and flagged open=False.
    o3 = [outs.get(k).odds if outs.get(k) else None for k in ("1", "2", "3")]
    p = market_probs(o3, [getattr(outs.get(k), "prob", None) for k in ("1", "2", "3")])
    if not p or o3[1] is None:
        return None
    is_open = all(outs.get(k) is not None and outs.get(k).active for k in ("1", "2", "3"))
    pk = Pick(event_id=e.event_id, home=e.home, away=e.away, league=e.league, category=e.category,
              tournament_id=e.tournament_id, category_id=getattr(e, "category_id", ""), start=e.start,
              odds=float(o3[1]), odds3=tuple(o3), p_market=p[1], home_uid=_uid(e.home_id), away_uid=_uid(e.away_id),
              top=leagues.is_top(e), top_league=leagues.is_top(e), open=is_open)
    lam = market_lambdas(e)
    if lam:
        pk.lam, pk.lam_src = lam, "market"
    price(pk)
    return pk


def price(pk: Pick) -> None:
    """est from the market price and the early-goals model (whatever inputs the pick carries)."""
    if pk.lam:
        lh, la = pk.lam
        pk.p_model = p_level(lh * SHARE5 * pk.mult, la * SHARE5 * pk.mult)
        pk.est = model.sigmoid((1 - W_MODEL) * model.logit(pk.p_market) + W_MODEL * model.logit(pk.p_model))
    else:
        pk.p_model, pk.est = None, pk.p_market


def apply_form(pk: Pick, form, utid: str = "") -> None:
    """Team stats: expected goals from recent form (blended into the market's), early-goal tendency, H2H."""
    if form is None:
        pk.why = why_text(pk, None, None, None)
        return
    hg = list(getattr(form.home, "games", None) or [])
    ag = list(getattr(form.away, "games", None) or [])
    sl = model.stats_lambdas_v2(hg, ag, utid or model.utid_of(pk.tournament_id))
    if sl:
        if pk.lam:
            w = W_STATS_LAMBDA
            pk.lam = ((1 - w) * pk.lam[0] + w * sl[0], (1 - w) * pk.lam[1] + w * sl[1])
            pk.lam_src = "market+form"
        else:
            pk.lam, pk.lam_src = sl, "form"
        pk.stats = True
    eh, ea = team_early(form.home), team_early(form.away)
    if eh and ea:
        pk.mult = max(0.75, min(1.3, math.sqrt(eh["mult"] * ea["mult"])))
        pk.stats = True
    elif eh or ea:
        pk.mult = max(0.85, min(1.15, math.sqrt((eh or ea)["mult"])))
    pk.early = {"home": eh, "away": ea}
    price(pk)
    pk.why = why_text(pk, eh, ea, getattr(form, "h2h", None))


def _starter(d: dict) -> str:
    return "slow" if d["mult"] <= 0.9 else "fast" if d["mult"] >= 1.12 else "normal"


def _starter_word(k: str) -> str:
    return {"slow": "slow starter", "fast": "starts fast", "normal": "normal start"}[k]


def why_text(pk: Pick, eh: dict | None, ea: dict | None, h2h) -> str:
    parts = []
    if pk.lam:
        g = sum(pk.lam)
        tag = "low-scoring" if g < 2.3 else "high-scoring" if g > 3.0 else "average goals"
        parts.append(f"{tag}: {g:.1f} exp. goals")
    if eh and ea:
        kh, ka = _starter(eh), _starter(ea)
        if kh == ka:
            lead = {"slow": "both slow starters", "fast": "both start fast (early-goal risk)",
                    "normal": "normal starts"}[kh]
        else:
            lead = f"{pk.home} {_starter_word(kh)}, {pk.away} {_starter_word(ka)}"
        parts.append(f"{lead}: goals in 1-15' {pk.home} {eh['m15']} in {eh['n']}, {pk.away} {ea['m15']} in {ea['n']} "
                     f"games")
    elif eh or ea:
        side, d = (pk.home, eh) if eh else (pk.away, ea)
        parts.append(f"goals in 1-15': {side} {d['m15']} in {d['n']} games")
    fg = [x for x in (getattr(h2h, "first_goal", None) or [])][:6]
    if fg:
        early = sum(1 for x in fg if x is not None and x <= 15)
        parts.append(f"H2H: goal before 15' in {early} of {len(fg)}")
    if not pk.stats:
        parts.append("price + goals line only (no team stats)")
    return " · ".join(parts)


# ------------------------------------------------------------------ selection
def select(picks: list[Pick], minimum: int = MIN_SELECTIONS, widen: bool = True,
           cap: int = MAX_LIST) -> tuple[list[Pick], dict]:
    """Top-league picks first (all of them, up to cap); when fewer than `minimum` and widening is allowed, the best
    wider-league picks by est fill the gap up to `minimum`. Returns (picks ranked by est, counts)."""
    top = sorted([p for p in picks if p.top], key=lambda p: (-p.est, -p.value))[:cap]
    wider_pool = sorted([p for p in picks if not p.top], key=lambda p: (-p.est, -p.value))
    wider = wider_pool[:max(0, minimum - len(top))] if widen else []
    out = sorted(top + wider, key=lambda p: (-p.est, -p.value, p.start))
    return out, {"top": len(top), "wider": len(wider), "available_top": len([p for p in picks if p.top]),
                 "available_wider": len(wider_pool), "available": len(picks)}


def rules_line(win: dict, scope: str, counts: dict, n: int, minimum: int = MIN_SELECTIONS) -> str:
    if scope == "filter":
        sc = "your leagues only (no widening)"
    elif scope == "all":
        sc = "all leagues"
    elif counts.get("wider"):
        sc = f"top leagues + {counts['wider']} wider-league picks (widened to reach {minimum})"
    else:
        sc = "top leagues"
    return (f"5-min Draw (SportyBet '{MARKET_NAME}' = Draw: level after minute 5) · {win['label']} · {sc} · "
            f"{n} selections · ranked by est.")


def short_note(scope: str, counts: dict, n: int, minimum: int = MIN_SELECTIONS) -> str:
    if n >= minimum:
        return ""
    if scope == "filter":
        return (f"Only {n} matches in your leagues offer the 5-minute market in this window (minimum asked: "
                f"{minimum}). I kept to your leagues and didn't add others.")
    if scope == "all" or counts.get("available_wider", 0) <= counts.get("wider", 0):
        return (f"Only {n} matches on SportyBet offer the 5-minute market in this window (all leagues checked), "
                f"short of {minimum}. Showing all of them.")
    return f"{n} selections (short of {minimum})."


# ------------------------------------------------------------------ finder
class Draw5Finder:
    def __init__(self, bookmaker, stats=None):
        self.bm = bookmaker
        self.stats = stats
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _events(self, win: dict, now: datetime) -> list:
        hours = min(200, max(24, math.ceil((win["end"] - now).total_seconds() / 3600) + 2))
        evs = await self.bm.list_events(hours, market_ids=LIST_MARKETS)
        return [e for e in evs if win["start"] <= e.start <= win["end"] and e.start > now + MIN_LEAD
                and not _excluded(e)]

    async def _attach_stats(self, picks: list[Pick], budget: float = STATS_BUDGET_S) -> int:
        if not self.stats or not picks:
            for p in picks:
                p.why = p.why or why_text(p, None, None, None)
            return 0

        async def one(p: Pick):
            if not (p.home_uid and p.away_uid):
                return None
            return await self.stats.match_form(f"sr:competitor:{p.home_uid}", f"sr:competitor:{p.away_uid}",
                                               p.home, p.away)

        tasks = {asyncio.ensure_future(one(p)): p for p in picks}
        done, pending = await asyncio.wait(tasks.keys(), timeout=budget)
        for t in pending:
            t.cancel()
        n = 0
        for t, p in tasks.items():
            form = None
            if t in done and not t.cancelled() and t.exception() is None:
                form = t.result()
            apply_form(p, form)
            n += 1 if p.stats else 0
        return n

    async def find(self, period: str = "weekend", text: str = "", minimum: int = MIN_SELECTIONS,
                   now: datetime | None = None, fresh: bool = False) -> dict:
        period = period if period in PERIODS else "weekend"
        key = f"{period}|{(text or '').strip().lower()}|{minimum}"
        hit = self._cache.get(key)
        if hit and not fresh and time.time() - hit[0] < RESULT_TTL:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and not fresh and time.time() - hit[0] < RESULT_TTL:
                return hit[1]
            res = await self._find(period, text, minimum, now)
            self._cache[key] = (time.time(), res)
            if len(self._cache) > 20:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:10]:
                    self._cache.pop(k, None)
            return res

    async def _find(self, period: str, text: str, minimum: int, now: datetime | None) -> dict:
        t0 = time.time()
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        win = period_window(period, now)
        from constraints import parse_text
        c = parse_text(text or "")
        named = bool(c.include_leagues or c.include_countries or c.include_teams)
        scope = "filter" if named else "all" if c.all_leagues else "top"
        events = await self._events(win, now)
        offered = 0
        picks: list[Pick] = []
        for e in events:
            if (MARKET_ID, SPEC) not in e.markets:
                continue
            offered += 1
            if named and not c.event_ok(e):
                continue
            pk = make_pick(e)
            if pk:
                if scope != "top":
                    pk.top = True          # the user's leagues / all leagues: no "wider" tag, no widening
                picks.append(pk)
        # stats for every candidate that can make the list (top leagues, plus the best wider ones if needed)
        pre, counts = select(picks, minimum, widen=(scope == "top"))
        extra = []
        if scope == "top" and counts["wider"]:
            chosen = {p.event_id for p in pre}
            extra = [p for p in sorted([p for p in picks if not p.top], key=lambda p: -p.est)
                     if p.event_id not in chosen][:15]
        with_stats = await self._attach_stats(pre + extra)
        final, counts = select(picks, minimum, widen=(scope == "top"))
        for p in final:
            if not p.why:
                p.why = why_text(p, None, None, None)
        n = len(final)
        rows = [p.to_json(i + 1) for i, p in enumerate(final)]
        return {"period": win["period"], "label": win["label"], "window_start": win["start"].isoformat(),
                "window_end": win["end"].isoformat(), "scope": scope, "filter_text": c.describe() if named else "",
                "rules": rules_line(win, scope, counts, n, minimum), "note": short_note(scope, counts, n, minimum),
                "count": n, "top_count": sum(1 for p in final if p.top_league),
                "wider_count": sum(1 for p in final if not p.top_league), "minimum": minimum,
                "open_count": sum(1 for p in final if p.open), "suspended_count": sum(1 for p in final if not p.open),
                "offered_in_window": offered, "matches_in_window": len(events), "with_stats": with_stats,
                "picks": rows, "market": {"id": MARKET_ID, "specifier": SPEC, "outcome": DRAW, "name": MARKET_NAME},
                "generated_at": datetime.now(timezone.utc).isoformat(), "took_s": round(time.time() - t0, 1),
                "method": (f"est = 50/50 blend of SportyBet's de-margined price and an early-goals model (expected goals "
                           f"x {SHARE5:.2%} share of goals in minutes 1-5 x both teams' early-goal tendency). "
                           "Estimates, not guarantees.")}


def slip_from(res: dict, n: int) -> dict:
    """Preview slip (top n picks). Nothing booked or saved until the user taps Book."""
    legs = [dict(p) for p in (res.get("picks") or []) if p.get("open", True)][:max(1, n)]
    tot, prob = 1.0, 1.0
    for l in legs:
        tot *= l["odds"]
        prob *= l["est"]
    for l in legs:
        l.setdefault("verdict", "confirmed" if l.get("stats") else "market only")
    if not legs:
        return {"tier": "custom", "label": "5-min Draw", "legs": [], "total_odds": 0, "est_prob": 0,
                "rules_text": res.get("rules", ""), "draw5": True,
                "note": "SportyBet has the 5-minute market suspended on every listed match right now; try again later."}
    return {"tier": "custom", "label": f"5-min Draw top {len(legs)}", "legs": legs, "total_odds": round(tot, 2),
            "est_prob": prob, "target": None,
            "rules_text": f"{res.get('rules', '')} · slip = top {len(legs)} open picks",
            "draw5": True}


# ------------------------------------------------------------------ text output
def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


TG_LIMIT = 4000          # Telegram allows 4096 characters after entity parsing; keep headroom


def tg_len(text: str) -> int:
    """Length Telegram counts for an HTML message: tags removed, entities decoded, UTF-16 code units."""
    visible = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return len(visible.encode("utf-16-le")) // 2


def _clip(s, n: int | None) -> str:
    s = str(s or "")
    if n is None or len(s) <= n:
        return s
    return s[:max(0, n - 3)].rstrip() + "..."


def pick_line_html(p: dict, why_max: int | None = None) -> str:
    tag = (" · <i>wider league</i>" if p.get("wider") else "") + \
        ("" if p.get("open", True) else " · <i>suspended now</i>")
    line = (f"<b>{p['rank']}.</b> {_e(p['kickoff_local'])} · <b>{_e(p['match'])}</b>\n"
            f"    {_e(p['league'])}{tag}\n"
            f"    Draw (1-5 min) @{p['odds']:.2f} · est <b>{p['est']:.1%}</b> · value {p['value']:+.1%}")
    if why_max != 0 and p.get("why"):
        line += f"\n    <i>{_e(_clip(p['why'], why_max))}</i>"
    return line


def page_html(res: dict, page: int, per: int = 10, limit: int = TG_LIMIT) -> tuple[str, int]:
    """One Telegram page. Always valid HTML within Telegram's length: when the full reasons would not fit, they are
    shortened (never cut through a tag); the full text is in the file."""
    picks = res.get("picks") or []
    pages = max(1, math.ceil(len(picks) / per))
    page = max(0, min(page, pages - 1))
    head = (f"<b>Rules:</b> {_e(res.get('rules'))}\n<b>5-MIN DRAW FINDER</b> · page {page + 1}/{pages}\n"
            f"<i>{res.get('count', len(picks))} selections ({res.get('top_count', 0)} top-league"
            + (f", {res['wider_count']} wider-league" if res.get("wider_count") else "")
            + f") · {res.get('offered_in_window', 0)} matches offer the market in this window · "
              f"{res.get('with_stats', 0)} stats-checked</i>")
    if res.get("note"):
        head += f"\nNote: {_e(res['note'])}"
    if res.get("suspended_count"):
        if res.get("open_count"):
            head += (f"\nNote: {res['suspended_count']} of these have the market suspended on SportyBet right now "
                     "(price shown, no bets taken until it reopens).")
        else:
            head += ("\nNote: SportyBet has this market suspended on all of them right now (prices shown, no bets "
                     "taken until it reopens, so no slip yet). Check again later.")
    chunk = picks[page * per:(page + 1) * per]
    foot = "\n\n<i>Est. = chance the score is level after minute 5. Estimates, not guarantees. 18+.</i>"
    empty = "No matches in this window offer the market right now."
    text = ""
    for why_max in (None, 160, 90, 0):
        body = "\n\n".join(pick_line_html(p, why_max) for p in chunk) or empty
        text = head + "\n\n" + body + foot
        if tg_len(text) <= limit:
            return text, pages
    while chunk and tg_len(text) > limit:       # last resort: fewer lines on this page, still balanced HTML
        chunk = chunk[:-1]
        body = "\n\n".join(pick_line_html(p, 0) for p in chunk) + "\n\n<i>More on this page: use Full list (file).</i>"
        text = head + "\n\n" + body + foot
    if tg_len(text) > limit:
        text = f"<b>5-MIN DRAW FINDER</b> · {res.get('count', len(picks))} selections. Use Full list (file)."
    return text, pages


def html_to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def file_text(res: dict) -> str:
    lines = [f"Rules: {res['rules']}", f"5-MIN DRAW FINDER · {res['count']} selections", ""]
    if res.get("note"):
        lines += [f"Note: {res['note']}", ""]
    for p in res.get("picks") or []:
        lines.append(f"{p['rank']:>3}. {p['kickoff_local']} WAT · {p['match']} · {p['league']}"
                     + (" (wider league)" if p.get("wider") else "")
                     + ("" if p.get("open", True) else " (market suspended now)"))
        lines.append(f"     Draw (1-5 min) @{p['odds']:.2f} · est {p['est']:.1%} · market {p['p_market']:.1%} · "
                     f"value {p['value']:+.1%}")
        lines.append(f"     {p['why']}")
    lines += ["", res.get("method", ""), "Estimates, not guarantees. 18+."]
    return "\n".join(lines)


def slip_html(sl: dict) -> str:
    rows = [f"<b>Rules:</b> {_e(sl['rules_text'])}", f"<b>5-MIN DRAW SLIP (preview)</b> · {len(sl['legs'])} legs"]
    for i, l in enumerate(sl["legs"], 1):
        rows.append(f"{i}. {_e(l['kickoff_local'])} · {_e(l['match'])}: Draw (1-5 min) @{l['odds']:.2f} "
                    f"({l['est']:.0%})" + (" · wider league" if l.get("wider") else ""))
    rows.append(f"\nTotal odds <b>{sl['total_odds']:.2f}</b> · est. hit chance {sl['est_prob']:.1%} "
                "(legs treated as independent)")
    rows.append("<i>Preview only: nothing is booked or saved until you tap Book. Estimates, not guarantees. 18+.</i>")
    out = "\n".join(rows)
    while tg_len(out) > TG_LIMIT and len(rows) > 4:     # drop whole leg lines, never cut through a tag
        rows.pop(-3)
        out = "\n".join(rows)
    return out


def chat_summary(res: dict, top: int = 15) -> str:
    """Plain/markdown-light reply for the AI chat (Mini App): the rules, the top picks, where the full list is."""
    out = [f"Rules: {res['rules']}"]
    if res.get("note"):
        out.append(f"Note: {res['note']}")
    for p in (res.get("picks") or [])[:top]:
        out.append(f"{p['rank']}. {p['kickoff_local']} · {p['match']} ({p['league']}"
                   + (", wider league" if p.get("wider") else "") + f"): Draw (1-5 min) @{p['odds']:.2f} "
                   f"(est {p['est']:.0%})")
    if res["count"] > top:
        out.append(f"... and {res['count'] - top} more: the full list is under Scanner > 5-min Draw, or /draw5 in chat.")
    out.append("Estimates, not guarantees. 18+.")
    return "\n".join(out)
