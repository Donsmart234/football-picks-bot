"""Market scanner: reads every bookable market of a bounded set of matches, estimates each outcome's probability,
checks the shortlisted matches against team stats (form, home/away record, goals, head-to-head), and builds
risk-tiered accumulators to a target total odds (one pick per match).

Probability per outcome (all estimates):
  market   = de-margined SportyBet price (proportional, per market line)
  feed     = Betradar's own outcome probability shipped with the price (when present)
  fit      = Poisson model fitted to the match's 1X2 + O/U 2.5 prices
  base     = 0.55*market + 0.15*feed + 0.30*fit
  stats    = model v2 (model.py): time-decayed Dixon-Coles Poisson from both teams' last 10 games with league home/away
             goal averages, when the stats feed answers
  estimate = calibrate_g( logit-blend_g(base, stats) )  (weights + calibration maps tuned by backtest/backtest.py on
             ~9,800 matches; goals markets only). Odds-only goals legs: calibrate_g(base) - 2 pts. Corners/cards/props/
             specials have no backtest and keep their market estimate.
Stats then confirm (stats ≥ market - 3 pts), demote (-3..-10 pts) or drop (< -10 pts, or a form red flag) each leg.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from bookmakers import Event, SportyBetNG
from constraints import TOTAL_HI, TOTAL_LO, Constraints, leg_tags, market_tags, verify
from markets import FAMILIES, Family, devig_group, model_prob, pretty_pick, side_of, spec_value, usable_line
import leagues
import model
from srstats import MatchForm, SportradarStats

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)

TIERS: dict[str, dict] = {
    "safe": {"label": "Safe", "min_p": 0.80, "odds": (1.05, 1.35), "target": 5.0, "max_legs": 25},
    "mid": {"label": "Mid-safe", "min_p": 0.70, "odds": (1.30, 1.70), "target": 10.0, "max_legs": 14},
    "risk": {"label": "Risk", "min_p": 0.55, "odds": (1.60, 2.30), "target": 20.0, "max_legs": 10},
    "high": {"label": "High risk", "min_p": 0.30, "odds": (2.00, 8.00), "target": 50.0, "max_legs": 10},
}
TIER_ALIASES = {"safe": "safe", "low": "safe", "mid": "mid", "mid-safe": "mid", "midsafe": "mid", "medium": "mid",
                "risk": "risk", "risky": "risk", "high": "high", "high-risk": "high", "highrisk": "high",
                "high risk": "high", "value": "high", "audit": "audit", "grand": "audit", "grand audit": "audit",
                "grand-audit": "audit", "all": "audit"}
DAYS = {"today", "tomorrow", "24h", "48h"}
MAX_EVENTS = 80
ROLLOVER_MIN_P = 0.80
ROLLOVER_MIN_ODDS = 1.12   # 1.03-1.10 legs add bookmaker margin and leg count for almost no odds
STATS_EVENTS = 40
POOL_TTL = 600
FRESH_TTL = 60          # a refresh re-reads prices unless the pool is younger than this

# Major / reliable competitions first (category, tournament name). Everything else ordered by market depth.
PRIORITY = [
    ("England", "Premier League"), ("Spain", "LaLiga"), ("Italy", "Serie A"), ("Germany", "Bundesliga"),
    ("France", "Ligue 1"), ("International Clubs", "UEFA Champions League"),
    ("International Clubs", "UEFA Europa League"), ("International Clubs", "UEFA Conference League"),
    ("Netherlands", "Eredivisie"), ("Portugal", "Liga Portugal"), ("England", "Championship"),
    ("Belgium", "Pro League"), ("Turkiye", "Super Lig"), ("Scotland", "Premiership"), ("Germany", "2. Bundesliga"),
    ("Spain", "LaLiga 2"), ("Italy", "Serie B"), ("France", "Ligue 2"), ("Austria", "Bundesliga"),
    ("Switzerland", "Super League"), ("Denmark", "Superliga"), ("Norway", "Eliteserien"),
    ("Sweden", "Allsvenskan"), ("Greece", "Super League"), ("Brazil", "Brasileiro Serie A"),
    ("Argentina", "Liga Profesional"), ("USA", "MLS"), ("Saudi Arabia", "Saudi Pro League"),
    ("England", "League One"), ("England", "League Two"), ("Nigeria", "NPFL"), ("Poland", "Ekstraklasa"),
    ("Czechia", "1. Liga"), ("Japan", "J.League"), ("Mexico", "Liga MX"),
]
_EXCLUDE = ("simulated", "srl", "esoccer", "virtual", "ebattle", "e-football")


def _prio(e: Event) -> tuple:
    cat, lg = (e.category or "").lower(), (e.league or "").lower()
    for i, (c, l) in enumerate(PRIORITY):
        if cat == c.lower() and (lg == l.lower() or lg.startswith(l.lower())):
            return (0, i, -e.market_count)
    penalty = 2 if ("friendl" in lg or "women" in lg or "u19" in lg or "u21" in lg or "u23" in lg) else 1
    return (penalty, 0, -e.market_count)


def sel_key(l) -> str:
    """Identity of one selection (Leg or leg dict): event|market|specifier|outcome."""
    g = (lambda k: getattr(l, k, None)) if not isinstance(l, dict) else l.get
    return f"{g('event_id')}|{g('market_id')}|{g('specifier') or ''}|{g('outcome_id')}"


def not_started(l, soon: datetime) -> bool:
    """Kick-off still ahead of `soon` (legs from a pool up to 10 min old can belong to a match that just began)."""
    try:
        return datetime.fromisoformat(str(l.kickoff).replace("Z", "+00:00")) > soon
    except (ValueError, TypeError):
        return True


def uid_of(competitor_id: str) -> str:
    s = str(competitor_id or "").rsplit(":", 1)[-1]
    return s if s.isdigit() and len(s) <= 9 else ""


def rollover_allowed(e: Event) -> bool:
    """Rollover (and every default scan) uses the top-leagues allowlist, matched by Sportradar ids (leagues.py)."""
    return leagues.is_top(e)


def top_league(e) -> bool:
    return leagues.is_top(e)


def _excluded(e: Event) -> bool:
    s = f"{e.category} {e.league} {e.home} {e.away}".lower()
    return any(x in s for x in _EXCLUDE)


@dataclass(slots=True)
class Leg:
    event_id: str
    match: str
    home: str
    away: str
    league: str
    kickoff: str            # ISO UTC
    kickoff_local: str      # "Sat 31 Oct 16:00"
    market_id: str
    specifier: str
    outcome_id: str
    market: str
    pick: str
    odds: float
    p_market: float
    p_feed: float | None
    p_fit: float | None
    p_stats: float | None = None
    base: float = 0.0               # estimate before the stats check
    est: float = 0.0
    verdict: str = "odds only"      # confirmed | demoted | dropped | odds only | market only
    why: str = ""
    side: str | None = None
    level: str = "model"            # model (Poisson+stats) | market (de-margined full book) | thin (single-price)
    group: str = ""
    home_uid: str = ""              # Sportradar competitor uid (team crest: img.sportradar.com/ls/crest/medium/<uid>.png)
    away_uid: str = ""
    category: str = ""              # country / "International Clubs"
    tags: tuple = ()                # market types (constraints.market_tags): under, over, 1x2, corners, ...
    utid: str = ""                  # Sportradar unique tournament id (league goal averages for the stats model)
    p_raw: float | None = None      # blended probability before calibration
    category_id: str = ""           # sr:category:<id>

    @property
    def ev(self) -> float:
        return self.est * self.odds

    @property
    def conf(self) -> float:
        gap = max(0.0, self.p_market - self.p_stats) if self.p_stats is not None else 0.02
        return self.est - 0.3 * gap

    def selection(self) -> dict:
        return {"event_id": self.event_id, "market_id": self.market_id, "specifier": self.specifier,
                "outcome_id": self.outcome_id}

    def to_json(self) -> dict:
        d = asdict(self)
        d.update({"est": round(self.est, 4), "p_market": round(self.p_market, 4),
                  "p_feed": None if self.p_feed is None else round(self.p_feed, 4),
                  "p_fit": None if self.p_fit is None else round(self.p_fit, 4),
                  "p_stats": None if self.p_stats is None else round(self.p_stats, 4),
                  "p_raw": None if self.p_raw is None else round(self.p_raw, 4),
                  "ev": round(self.ev, 3)})
        return d


@dataclass
class Pool:
    created: float
    day: str
    events: list[Event]
    legs: dict[str, list[Leg]] = field(default_factory=dict)          # event_id -> candidate legs
    forms: dict[str, MatchForm | None] = field(default_factory=dict)  # event_id -> stats (shortlisted only)
    scanned: int = 0
    failed: int = 0


# ------------------------------------------------------------------ fast Poisson fit
def _pmf(lam: float, n: int = 11) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, n):
        p.append(p[-1] * lam / k)
    return p


def _fit_err(lh: float, la: float, t: dict) -> float:
    ph, pa = _pmf(lh), _pmf(la)
    h = d = over = s = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            p = x * y
            s += p
            if i > j:
                h += p
            elif i == j:
                d += p
            if i + j >= 3:
                over += p
    h, d, over = h / s, d / s, over / s
    a = 1 - h - d
    e = (h - t["h"]) ** 2 + (d - t["d"]) ** 2 + (a - t["a"]) ** 2
    if "o" in t:
        e += (over - t["o"]) ** 2
    return e


def fit_lambdas(t: dict) -> tuple[float, float] | None:
    if not all(k in t for k in ("h", "d", "a")):
        return None
    best = min((_fit_err(h / 5, a / 5, t), h / 5, a / 5) for h in range(1, 21) for a in range(1, 21))
    _, bh, ba = best
    fine = min((_fit_err(bh + dh / 20, ba + da / 20, t), bh + dh / 20, ba + da / 20)
               for dh in range(-4, 5) for da in range(-4, 5) if bh + dh / 20 > 0.05 and ba + da / 20 > 0.05)
    return fine[1], fine[2]


# ------------------------------------------------------------------ scanner
class Scanner:
    def __init__(self, bookmaker: SportyBetNG, stats: SportradarStats, api_football=None):
        self.bm = bookmaker
        self.stats = stats
        self.apif = api_football
        self._pools: dict[str, Pool] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- window -----------------------------------------------------------
    async def window_events(self, day: str) -> list[Event]:
        now = datetime.now(timezone.utc)
        local_today = now.astimezone(TZ).date()
        if day == "today":
            evs = await self.bm.list_events(24)
            return [e for e in evs if e.start > now + timedelta(minutes=5) and e.start.astimezone(TZ).date() == local_today]
        if day == "tomorrow":
            evs = await self.bm.list_events(48)
            tmr = local_today + timedelta(days=1)
            return [e for e in evs if e.start.astimezone(TZ).date() == tmr]
        hours = 48 if day == "48h" else 24
        evs = await self.bm.list_events(hours)
        return [e for e in evs if now + timedelta(minutes=5) < e.start <= now + timedelta(hours=hours)]

    def select(self, events: list[Event], leagues: list[str] | None, cap: int,
               rules: Constraints | None = None) -> list[Event]:
        """Matches to scan. Default: top-leagues allowlist only (leagues.py, by Sportradar ids). Widened only when
        the user explicitly names leagues / countries / teams or asks for all leagues."""
        evs = [e for e in events if not _excluded(e)]
        if not (leagues or (rules is not None and rules.widens_leagues())):
            evs = [e for e in evs if top_league(e)]
        if leagues:
            ls = [x.lower() for x in leagues if x]
            evs = [e for e in evs if any(x in e.league_full.lower() for x in ls)]
        if rules is not None:
            evs = [e for e in evs if rules.event_ok(e)]
        return sorted(evs, key=_prio)[:cap]

    # ---- pool -------------------------------------------------------------
    async def pool(self, day: str = "today", leagues: list[str] | None = None, cap: int = MAX_EVENTS,
                   rules: Constraints | None = None, fresh: bool = False) -> Pool:
        """Scanned matches for a window. `rules` = match-level constraints (leagues/countries/teams/kick-off):
        when set, the 80 matches are chosen among those that satisfy them (separate cache entry)."""
        day = day if day in DAYS else "today"
        mkey = rules.match_key() if rules is not None else ""
        rules = rules if mkey else None
        key = f"{day}|{','.join(sorted(x.lower() for x in leagues or []))}|{cap}|{mkey}"
        ttl = FRESH_TTL if fresh else POOL_TTL      # refresh = re-read prices unless the copy is under a minute old
        p = self._pools.get(key)
        if p and time.time() - p.created < ttl:
            return p
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            p = self._pools.get(key)
            if p and time.time() - p.created < ttl:
                return p
            t0 = time.time()
            events = self.select(await self.window_events(day), leagues, cap, rules)
            pool = Pool(created=time.time(), day=day, events=events)
            results = await asyncio.gather(*(self.bm.event_markets(e.event_id, None, cache=False) for e in events))
            for e, mk in zip(events, results):
                if mk is None:
                    pool.failed += 1
                    continue
                pool.scanned += 1
                legs = self.event_legs(e, mk)
                if legs:
                    pool.legs[e.event_id] = legs
            await self._attach_stats(pool)
            log.info("scan pool %s: %d events, %d with legs, %d failed, %d with stats, %.1fs", key, len(events),
                     len(pool.legs), pool.failed, sum(1 for f in pool.forms.values() if f and f.ok), time.time() - t0)
            self._pools[key] = pool
            for k in [k for k, v in self._pools.items() if time.time() - v.created > POOL_TTL * 3]:
                self._pools.pop(k, None)
            return pool

    def event_legs(self, e: Event, markets: list[dict], keep: int = 160,
                   odds_range: tuple[float, float] = (1.04, 15.0)) -> list[Leg]:
        """Score every outcome of every open market of one match. Returns the strongest `keep` legs plus the best
        value legs (memory bound on the free instance); everything is scored before pruning."""
        t = {}
        for m in markets:
            outs = {o["id"]: o for o in m["outcomes"]}
            if m["id"] == "1" and not m["specifier"] and all(k in outs and outs[k]["odds"] for k in ("1", "2", "3")):
                h, d, a = devig_group([outs[k]["odds"] for k in ("1", "2", "3")], False)
                t.update(h=h, d=d, a=a)
            if m["id"] == "18" and m["specifier"] == "total=2.5" and all(
                    k in outs and outs[k]["odds"] for k in ("12", "13")):
                t["o"] = devig_group([outs["12"]["odds"], outs["13"]["odds"]], False)[0]
        lam = fit_lambdas(t)
        grid: dict = {}
        local = e.start.astimezone(TZ)
        legs: list[Leg] = []
        for m in markets:
            outs_all = m["outcomes"]
            outs = [o for o in outs_all if o["active"] and o["odds"] and o["odds"] > 1.0]
            if not outs:
                continue
            fam = FAMILIES.get(m["id"])
            modeled = bool(fam and usable_line(fam, m["specifier"]) and lam)
            n, over = len(outs), sum(1 / o["odds"] for o in outs)
            props = "playerprops" in m["specifier"] or any("playerprops" in o["id"] for o in outs) or \
                m.get("source") == "SPORTING_RISK"
            if fam and fam.overlap and n == len(outs_all) == 3:
                fair, mode = devig_group([o["odds"] for o in outs], True), "devig"
            elif (n >= 2 and n == len(outs_all) and 1.0 < over <= 1.05 + 0.035 * n and not props):
                fair, mode = devig_group([o["odds"] for o in outs], False), "devig"
            else:   # overlapping outcomes, partial lists, single-outcome props: price each outcome on its own
                fair, mode = [min(0.985, 1 / (o["odds"] * 1.06)) for o in outs], "single"
            mk_name = m["desc"] or (fam.name if fam else f"Market {m['id']}")
            for o, pm in zip(outs, fair):
                if o["odds"] < odds_range[0] or o["odds"] > odds_range[1]:
                    continue
                pf = o["prob"] if o["prob"] and 0 < o["prob"] < 1 else None
                pfit = model_prob(fam, m["specifier"], o["id"], *lam, grid_cache=grid) if modeled else None
                if mode == "devig":
                    parts = [(pm, 0.55)] + ([(pf, 0.15)] if pf is not None else []) + (
                        [(pfit, 0.30)] if pfit is not None else [])
                else:
                    parts = [(pm, 0.45)] + ([(pf, 0.55)] if pf is not None else [])
                base = sum(p * w for p, w in parts) / sum(w for _, w in parts)
                level = "model" if pfit is not None else ("market" if mode == "devig" and not props else "thin")
                base -= {"model": 0.0, "market": 0.015, "thin": 0.03}[level]
                pick = pretty_pick(fam, o["desc"], e.home, e.away) if fam else o["desc"]
                legs.append(Leg(
                    event_id=e.event_id, match=e.name, home=e.home, away=e.away, league=e.league_full,
                    kickoff=e.start.isoformat(), kickoff_local=local.strftime("%a %d %b %H:%M"),
                    market_id=m["id"], specifier=m["specifier"], outcome_id=o["id"], market=mk_name,
                    pick=pick, odds=o["odds"], p_market=pm, p_feed=pf, p_fit=pfit, base=base, est=base - 0.02,
                    side=side_of(fam, o["id"]) if fam else None, level=level, group=m.get("group", ""),
                    home_uid=uid_of(e.home_id), away_uid=uid_of(e.away_id), category=e.category,
                    utid=model.utid_of(e.tournament_id), category_id=getattr(e, "category_id", ""),
                    tags=market_tags(m["id"], mk_name, pick, m.get("group", ""), m["specifier"], e.home, e.away)))
        if len(legs) > keep:
            by_conf = sorted(legs, key=lambda l: -l.base)
            strong = by_conf[:keep]
            ids = {id(l) for l in strong}
            value = sorted((l for l in legs if id(l) not in ids and l.base >= 0.3),
                           key=lambda l: -(l.base * l.odds))[:40]
            mid_band = [l for l in by_conf[keep:] if 1.3 <= l.odds <= 2.4 and l.base >= 0.5][:60]
            # keep the strongest few legs of every market type too, so a constrained scan ("no unders", "only
            # 1X2", "no corners") still has the best legs of what's allowed after the top-`keep` cut
            per_type: dict[str, int] = {}
            diverse = []
            for l in by_conf[keep:]:
                if l.base < 0.5 or l.odds < 1.05:
                    continue
                ts = [t for t in l.tags if t not in ("halves",)] or ["other"]
                if any(per_type.get(t, 0) < 8 for t in ts) and not ("under" in l.tags and per_type.get("under", 0) >= 8):
                    diverse.append(l)
                    for t in ts:
                        per_type[t] = per_type.get(t, 0) + 1
            seen = set()
            out = []
            for l in strong + value + mid_band + diverse:
                if id(l) not in seen:
                    seen.add(id(l))
                    out.append(l)
            legs = out
        return legs

    # ---- stats ------------------------------------------------------------
    def _shortlist(self, pool: Pool, n: int) -> list[Event]:
        """Matches whose best candidate legs look strongest for any tier (odds-only pass)."""
        def score(e: Event) -> float:
            legs = pool.legs.get(e.event_id) or []
            best = 0.0
            for t in TIERS.values():
                lo, hi = t["odds"]
                fits = [l.est for l in legs if lo <= l.odds <= hi and l.est >= t["min_p"] - 0.05]
                if fits:
                    best = max(best, max(fits) - t["min_p"] + 1)
            return best
        evs = [e for e in pool.events if e.event_id in pool.legs]
        evs.sort(key=lambda e: (-score(e), _prio(e)))
        return evs[:n]

    async def _attach_stats(self, pool: Pool, n: int = STATS_EVENTS) -> None:
        short = [e for e in self._shortlist(pool, n) if e.event_id not in pool.forms]
        forms = await asyncio.gather(*(self._form(e) for e in short), return_exceptions=True)
        for e, f in zip(short, forms):
            pool.forms[e.event_id] = f if isinstance(f, MatchForm) else None
        for e in pool.events:
            f = pool.forms.get(e.event_id)
            for leg in pool.legs.get(e.event_id) or []:
                apply_stats(leg, f)

    async def _form(self, e: Event) -> MatchForm | None:
        if not (e.home_id and e.away_id):
            return None
        return await self.stats.match_form(e.home_id, e.away_id, e.home, e.away)

    async def match_form(self, e: Event) -> MatchForm | None:
        return await self._form(e)

    # ---- slips ------------------------------------------------------------
    def tier_rules(self, tier: str, c: Constraints | None) -> tuple[float, float, float, int]:
        """(min_p, odds_lo, odds_hi, max_legs) for a tier after the user's constraints. The probability bar can
        only go up; leg-odds bounds narrow the band (a stated max leg odds may widen the upper end because the
        probability bar still holds)."""
        t = TIERS[tier]
        min_p = max(t["min_p"], (c.min_leg_prob or 0) if c else 0)
        lo, hi = t["odds"]
        max_legs = t["max_legs"]
        if c:
            if c.min_leg_odds:
                lo = max(lo, c.min_leg_odds)
            if c.max_leg_odds:
                hi = c.max_leg_odds
            if c.max_legs:
                max_legs = min(max_legs, c.max_legs) if tier != "safe" else c.max_legs
        return min_p, lo, hi, max_legs

    def allowed(self, legs: list[Leg], tier: str, c: Constraints | None, exclude: set | None = None) -> list[Leg]:
        min_p, lo, hi, _ = self.tier_rules(tier, c)
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        ok = [l for l in legs if lo <= l.odds <= hi and l.est >= min_p and l.verdict != "dropped"
              and not_started(l, soon) and not (exclude and sel_key(l) in exclude)]
        if tier == "high":
            ok = [l for l in ok if l.verdict != "demoted" and l.ev >= 0.97]
        if c:
            ok = [l for l in ok if c.leg_ok(l, min_p)]
        return ok

    def candidates(self, pool: Pool, tier: str, c: Constraints | None = None, exclude: set | None = None) -> list[Leg]:
        best: list[Leg] = []
        for legs in pool.legs.values():
            ok = self.allowed(legs, tier, c, exclude)
            if ok:
                best.append(max(ok, key=lambda l: self._rank(l, tier)))
        best.sort(key=lambda l: -self._rank(l, tier))
        return best

    @staticmethod
    def _rank(l: Leg, tier: str) -> float:
        bonus = 0.02 if l.verdict == "confirmed" else 0.0
        if tier in ("safe", "mid", "risk"):
            # odds gained per unit of hit-probability given up: maximises the slip's combined probability
            c = min(max(l.conf, 0.05), 0.995)
            return math.log(l.odds) / -math.log(c) * (1.05 if l.verdict == "confirmed" else 1.0)
        return l.ev + bonus

    def _greedy(self, cands: list[Leg], tier: str, target: float, max_legs: int) -> list[Leg]:
        legs: list[Leg] = []
        total = 1.0
        remaining = list(cands)
        while remaining and total < target * 0.98 and len(legs) < max_legs:
            need = target / total
            finishers = [l for l in remaining if l.odds >= need * 0.98]
            if finishers and (len(remaining) == len(finishers) or need <= max(l.odds for l in remaining)):
                # close the slip with the strongest leg that reaches the target without overshooting far
                fit = [l for l in finishers if l.odds <= need * 1.35] or finishers
                leg = max(fit, key=lambda l: self._rank(l, tier) - 0.05 * abs(math.log(l.odds / need)))
            else:
                leg = remaining[0]
            legs.append(leg)
            total *= leg.odds
            remaining.remove(leg)
        return legs

    def build_slip(self, pool: Pool, tier: str, target: float | None = None,
                   c: Constraints | None = None, exclude: set | None = None) -> dict:
        t = TIERS[tier]
        target = float(target or (c.target_odds if c else None) or t["target"])
        target = max(1.2, min(target, 100000.0))
        min_p, lo, hi, max_legs = self.tier_rules(tier, c)
        min_legs = (c.min_legs or 1) if c else 1
        cands = self.candidates(pool, tier, c, exclude)
        legs = self._greedy(cands, tier, target, max_legs)
        g_total = math.prod(l.odds for l in legs) if legs else 1.0
        g_ok = legs and target * TOTAL_LO <= g_total <= target * TOTAL_HI and len(legs) >= min_legs
        if tier in ("safe", "mid", "risk"):
            # exhaustive-ish search: the combination with the highest combined estimate that lands on the target
            pool_legs = []
            for ls in pool.legs.values():
                ok = self.allowed(ls, tier, c, exclude)
                bands: dict[int, Leg] = {}
                for l in ok:
                    b = 0 if l.odds < 1.12 else 1 if l.odds < 1.2 else 2 if l.odds < 1.35 else 3 if l.odds < 1.55 \
                        else 4 if l.odds < 1.8 else 5 if l.odds < 2.2 else 6
                    if b not in bands or l.conf > bands[b].conf:
                        bands[b] = l
                pool_legs += bands.values()
            found = search_combo(pool_legs, target * TOTAL_LO, target * 1.20, min(max_legs, 16), min_legs)
            if found and (not g_ok or math.prod(l.est for l in found) > math.prod(l.est for l in legs) + 1e-9):
                legs = found
        legs = [l for l in legs if l.est >= min_p and (c is None or c.leg_ok(l, min_p))]   # hard guard
        total = math.prod(l.odds for l in legs) if legs else 1.0
        legs.sort(key=lambda l: l.kickoff)
        prob = math.prod(l.est for l in legs) if legs else 0.0
        reached = total >= target * 0.98 if not c else total >= target * TOTAL_LO
        rules_txt = c.describe() if c else ""
        if not legs:
            note = (f"No legs qualify for {t['label']} right now (needs est. ≥{min_p:.0%} at odds "
                    f"{lo:.2f}–{hi:.2f} after the stats check"
                    + (f"; your rules: {rules_txt}" if rules_txt else "") + ").")
        elif not reached:
            note = (f"Only {len(legs)} qualifying legs; total odds reach {total:.2f}, short of the {target:g} target"
                    + (f" with your rules ({rules_txt})" if rules_txt else "") + ". I won't pad the slip with weaker "
                    "legs or break your rules.")
        elif len(legs) < min_legs:
            note = f"Reached the target with {len(legs)} legs, fewer than the {min_legs} you asked for."
        else:
            note = ""
        risk = 1 - prob if legs else 1.0
        out = {"tier": tier, "label": t["label"], "target": target, "total_odds": round(total, 2),
               "est_prob": round(prob, 4), "reached": reached, "risk_meter": round(risk, 3),
               "legs": [l.to_json() for l in legs], "candidates": len(cands), "note": note,
               "rules": {"min_p": min_p, "odds": (lo, hi)}, "day": pool.day,
               "scanned": pool.scanned, "with_stats": sum(1 for f in pool.forms.values() if f and f.ok)}
        if c is not None:
            out["constraints"] = c.to_dict()
            out["constraints_text"] = rules_txt
            out["constraint_check"] = verify(out, c, min_p)
        return out

    def audit(self, pool: Pool, c: Constraints | None = None) -> dict:
        best_per_match = []
        value = []
        dropped = 0
        for legs in pool.legs.values():
            dropped += sum(1 for l in legs if l.verdict == "dropped")
            if c is not None:
                legs = [l for l in legs if c.leg_ok(l)]
            ok = [l for l in legs if l.verdict != "dropped" and l.odds >= 1.20]
            if ok:
                best_per_match.append(max(ok, key=lambda l: l.conf))
            value += [l for l in legs if l.verdict in ("confirmed",) and l.est >= 0.35 and l.ev >= 1.0]
        best_per_match.sort(key=lambda l: -l.conf)
        value.sort(key=lambda l: -l.ev)
        seen, top_value = set(), []
        for l in value:
            if l.event_id not in seen:
                seen.add(l.event_id)
                top_value.append(l)
        return {"tier": "audit", "day": pool.day, "scanned": pool.scanned, "failed": pool.failed,
                "with_stats": sum(1 for f in pool.forms.values() if f and f.ok), "dropped_by_stats": dropped,
                "best_per_match": [l.to_json() for l in best_per_match[:30]],
                "top_value": [l.to_json() for l in top_value[:12]],
                "slips": {k: self.build_slip(pool, k, None, c) for k in TIERS}}

    async def scan(self, tier: str = "safe", target: float | None = None, day: str = "today",
                   leagues: list[str] | None = None, constraints: Constraints | None = None,
                   exclude: set | None = None, fresh: bool = False) -> dict:
        """constraints: structured user rules (see constraints.py), enforced at the candidate stage and
        re-verified on the finished slip (`constraint_check`)."""
        c = constraints if constraints is not None and not constraints.empty() else None
        if leagues:
            # legacy league text filters become real league rules (matched by Sportradar id, never by substring:
            # "champions league" must not pull in AFC / Gulf / SRL competitions). A league the user named wins.
            c = c or Constraints()
            if not [x for x in c.include_leagues if x.lower().strip() != "top leagues"]:
                c.include_leagues = [str(x) for x in leagues if x]
            leagues = None
        tier = TIER_ALIASES.get((tier or (c.tier if c else None) or "safe").lower().strip(), "safe")
        if c and c.day:
            day = c.day
        if c and target is None:
            target = c.target_odds
        pool = await self.pool(day, leagues, rules=c, fresh=fresh)
        out = self.audit(pool, c) if tier == "audit" else self.build_slip(pool, tier, target, c, exclude)
        if c is not None and tier != "audit" and c.match_key() and (
                not out.get("legs") or not out.get("reached") or (c.min_legs and len(out["legs"]) < c.min_legs)):
            try:
                out["scope"] = await self.scope_report(c, day, pool, out)
                if out["scope"].get("note"):
                    out["note"] = (out["scope"]["note"] + " " + (out.get("note") or "")).strip()
            except Exception as exc:          # the report is a courtesy: never fail the scan for it
                log.info("scope report failed: %s", exc)
        out["generated"] = datetime.fromtimestamp(pool.created, timezone.utc).isoformat()
        out["disclaimer"] = ("Estimates from prices + team stats, not guarantees. 'Safe' means lower-risk, never "
                             "certain. Combined probability assumes independent legs. 18+.")
        return out

    async def scope_report(self, c: Constraints, day: str, pool: Pool, out: dict) -> dict:
        """When a named scope (league / team / time window) can't fill the slip: how many matches it has in this
        window, and the nearest days that do have matches in scope (next 7 days). Never substitutes other leagues."""
        rules = c.match_rules()
        n_in = len(pool.events)
        upcoming = await self.bm.list_events(168)
        now = datetime.now(timezone.utc) + timedelta(minutes=5)
        window_ids = {e.event_id for e in pool.events}
        days: dict = {}
        for e in upcoming:
            if e.start <= now or e.event_id in window_ids or _excluded(e):
                continue
            if not rules.event_ok(e) or (not c.widens_leagues() and not top_league(e)):
                continue
            d = e.start.astimezone(TZ).date()
            days.setdefault(d, []).append(e)
        today = datetime.now(TZ).date()
        win_label = {"today": "today", "tomorrow": "tomorrow", "24h": "in the next 24 h", "48h": "in the next 48 h"}.get(
            day, day)
        nxt = []
        for d in sorted(days)[:3]:
            evs = days[d]
            lab = "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else d.strftime("%a %d %b")
            nxt.append({"date": d.isoformat(), "label": lab, "matches": len(evs),
                        "first": min(evs, key=lambda e: e.start).start.astimezone(TZ).strftime("%H:%M"),
                        "day_arg": "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else None})
        scope = c.describe() or "your rules"
        mr = c.match_rules().describe() or scope
        if n_in == 0:
            note = (f"No matches {win_label} in your scope ({mr}), so there's no slip. I won't fill it with other "
                    "leagues or teams.")
        elif not out.get("legs"):
            note = (f"{n_in} match{'es' if n_in != 1 else ''} {win_label} in your scope ({mr}), but none has a leg "
                    "that passes the other rules. I won't fill it with other leagues or teams.")
        else:
            note = (f"Only {n_in} match{'es' if n_in != 1 else ''} {win_label} in your scope ({mr}): {len(out['legs'])} "
                    f"qualifying leg{'s' if len(out['legs']) != 1 else ''}. I kept to them rather than add other "
                    "leagues; fewer legs or a lower target is the option.")
        if nxt:
            note += " Nearest with matches in scope: " + ", ".join(
                f"{x['label']} ({x['matches']} match{'es' if x['matches'] != 1 else ''}, first {x['first']})"
                for x in nxt[:2]) + "."
        elif n_in == 0:
            note += " None listed on SportyBet in the next 7 days either."
        return {"matches_in_window": n_in, "window": day, "next_days": nxt, "note": note}

    # ---- rollover ---------------------------------------------------------
    async def ensure_stats(self, pool: Pool, events: list[Event]) -> None:
        todo = [e for e in events if e.event_id in pool.legs and e.event_id not in pool.forms]
        if not todo:
            return
        forms = await asyncio.gather(*(self._form(e) for e in todo), return_exceptions=True)
        for e, f in zip(todo, forms):
            pool.forms[e.event_id] = f if isinstance(f, MatchForm) else None
            for leg in pool.legs.get(e.event_id) or []:
                apply_stats(leg, pool.forms[e.event_id])

    async def rollover_slip(self, target: float, min_p: float = ROLLOVER_MIN_P, day: str = "today",
                            allow_unders: bool = False, constraints: Constraints | None = None,
                            exclude: set | None = None, fresh: bool = False) -> dict:
        """Fewest, strongest stats-confirmed legs from allowlisted leagues whose total odds land on the target
        (between 0.97x and 1.20x of it). Every leg est >= min_p. Maximises the combined estimate.
        Under selections of every kind are excluded at the candidate stage unless allow_unders=True."""
        c = Constraints(**{**(constraints.to_dict() if constraints else {})}) if constraints else Constraints()
        if not allow_unders and "under" not in c.exclude_markets:
            c.exclude_markets.append("under")
        pool = await self.pool(day, fresh=fresh)
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        allowed = [e for e in pool.events if rollover_allowed(e)]
        await self.ensure_stats(pool, allowed)
        ids = {e.event_id for e in allowed}
        lo_t, hi_t = target * 0.97, target * 1.20
        per_match: list[Leg] = []
        for eid in ids:
            legs = [l for l in pool.legs.get(eid) or [] if l.verdict == "confirmed" and l.est >= min_p
                    and ROLLOVER_MIN_ODDS <= l.odds <= hi_t and c.leg_ok(l, min_p)
                    and not_started(l, soon) and not (exclude and sel_key(l) in exclude)]
            # keep the strongest leg in each odds band so the search can mix short and longer prices
            bands: dict[int, Leg] = {}
            for l in legs:
                b = 0 if l.odds < 1.2 else 1 if l.odds < 1.35 else 2 if l.odds < 1.55 else 3 if l.odds < 1.8 else 4
                if b not in bands or l.est > bands[b].est:
                    bands[b] = l
            per_match += bands.values()
        found = search_combo(per_match, lo_t, hi_t, 12)
        best = {"p": math.prod(l.est for l in found) if found else 0.0, "legs": found or []}
        cands = per_match[:80]
        legs = sorted(best["legs"], key=lambda l: l.kickoff)
        total = math.prod(l.odds for l in legs) if legs else 0.0
        note = "" if legs else (
            f"No combination of stats-confirmed legs (each est. ≥{min_p:.0%}) from the top leagues lands on "
            f"{target:g} odds today ({len(cands)} strong legs in {len(allowed)} top-league matches). "
            "Suggest skipping today rather than forcing weaker picks.")
        return {"tier": "rollover", "label": "Rollover", "target": target, "total_odds": round(total, 2),
                "est_prob": round(best["p"], 4) if legs else 0.0, "reached": bool(legs),
                "legs": [l.to_json() for l in legs], "candidates": len(cands), "note": note,
                "rules": {"min_p": min_p, "leagues": "allowlist", "no_unders": not allow_unders},
                "constraints": c.to_dict(), "constraint_check": verify({"legs": [l.to_json() for l in legs],
                                                                        "target": target}, c, min_p),
                "day": pool.day, "scanned": pool.scanned,
                "with_stats": sum(1 for f in pool.forms.values() if f and f.ok), "top_league_matches": len(allowed),
                "generated": datetime.fromtimestamp(pool.created, timezone.utc).isoformat(),
                "disclaimer": "Estimates from prices + team stats, not guarantees. 18+."}

    async def edit_candidates(self, mode: str, day: str = "today", min_p: float = ROLLOVER_MIN_P,
                              allow_unders: bool = False, tier: str | None = None, c: Constraints | None = None,
                              fresh: bool = False) -> list[dict]:
        """Every leg that may replace / join a slip of a section (slip edits, slipedit.py), as leg dicts.
        rollover: same per-leg rules as rollover_slip (top leagues, stats-confirmed, est >= min_p, odds >= 1.12, no
        unders unless allowed, not kicking off within 5 min). scan: the tier + the user's constraints (allowed()).
        custom: top leagues, not dropped by the stats check, est >= min_p."""
        soon = datetime.now(timezone.utc) + timedelta(minutes=5)
        out: list[Leg] = []
        if mode == "rollover":
            rc = Constraints(**{**(c.to_dict() if c else {})}) if c else Constraints()
            if not allow_unders and "under" not in rc.exclude_markets:
                rc.exclude_markets.append("under")
            pool = await self.pool(day, fresh=fresh)
            allowed = [e for e in pool.events if rollover_allowed(e)]
            await self.ensure_stats(pool, allowed)
            for e in allowed:
                out += [l for l in pool.legs.get(e.event_id) or [] if l.verdict == "confirmed" and l.est >= min_p
                        and l.odds >= ROLLOVER_MIN_ODDS and rc.leg_ok(l, min_p) and not_started(l, soon)]
        elif mode == "scan":
            t = TIER_ALIASES.get((tier or "safe").lower(), "safe")
            cc = c if c is not None and not c.empty() else None
            pool = await self.pool(day, rules=cc, fresh=fresh)
            for legs in pool.legs.values():
                out += self.allowed(legs, t, cc)
        else:
            pool = await self.pool(day, fresh=fresh)
            for legs in pool.legs.values():
                out += [l for l in legs if l.verdict != "dropped" and l.est >= min_p and 1.04 <= l.odds <= 10
                        and not_started(l, soon) and (allow_unders or "under" not in l.tags)]
        return [l.to_json() for l in out]

    def event_report(self, pool: Pool | None, legs: list[Leg], top: int = 12) -> list[dict]:
        ok = sorted([l for l in legs if l.odds >= 1.15], key=lambda l: -l.conf)
        return [l.to_json() for l in ok[:top]]


# ------------------------------------------------------------------ combination search
def search_combo(legs: list[Leg], lo_t: float, hi_t: float, max_legs: int, min_legs: int = 1,
                 shortlist: int = 80, node_cap: int = 250000) -> list[Leg]:
    """Combination of legs (one per match) with the highest combined estimate whose total odds land in
    [lo_t, hi_t]; fewest legs on ties. Candidates are shortlisted by odds gained per unit of probability given
    up, then searched strongest-first so the probability bound prunes early. [] when nothing fits."""
    pool = [l for l in legs if 1.0 < l.odds <= hi_t and 0 < l.est < 1]
    pool.sort(key=lambda l: -(math.log(l.odds) / -math.log(min(l.est, 0.995))))
    cands = sorted(pool[:shortlist], key=lambda l: -l.est)
    best: dict = {"p": 0.0, "legs": []}
    nodes = [0]

    def dfs(start: int, chosen: list[Leg], used: set, odds: float, p: float) -> None:
        nodes[0] += 1
        if nodes[0] > node_cap or p <= best["p"]:
            return
        if odds >= lo_t:
            if odds <= hi_t and len(chosen) >= min_legs and (
                    p > best["p"] + 1e-9 or (abs(p - best["p"]) < 1e-9 and len(chosen) < len(best["legs"]))):
                best.update(p=p, legs=list(chosen))
            if len(chosen) >= min_legs:
                return
        if len(chosen) >= max_legs:
            return
        for k in range(start, len(cands)):
            l = cands[k]
            if l.event_id in used or odds * l.odds > hi_t:
                continue
            chosen.append(l)
            used.add(l.event_id)
            dfs(k + 1, chosen, used, odds * l.odds, p * l.est)
            chosen.pop()
            used.discard(l.event_id)

    dfs(0, [], set(), 1.0, 1.0)
    return best["legs"]


# ------------------------------------------------------------------ stats check
_WIN_KINDS = {"1x2", "dnb", "ah", "eh", "dc"}


def apply_stats(leg: Leg, form: MatchForm | None) -> None:
    fam: Family | None = FAMILIES.get(leg.market_id)
    base = leg.base
    if leg.p_fit is None or fam is None:   # corners, bookings, combos, props, specials: no goals model
        leg.p_stats, leg.est = None, base
        leg.verdict = "market only"
        src = "full-book de-margined odds" if leg.level == "market" else "single price"
        leg.why = (f"Market-only estimate ({src}{' + feed probability' if leg.p_feed is not None else ''}); "
                   f"the stats model can't price {leg.group.lower() or 'this'} markets. Lower confidence.")
        if form and form.ok:
            leg.why += f" Form: {form.home.name} {_rec(form.home)}, {form.away.name} {_rec(form.away)}."
        return
    if not form or not form.ok:
        raw, cal = model.estimate(base, None, fam.kind, fam.period)
        leg.p_stats, leg.p_raw, leg.est, leg.verdict = None, raw, cal - 0.02, "odds only"
        leg.why = "Odds-only: no team stats available for this match."
        return
    lam = model.stats_lambdas_v2(form.home.games, form.away.games, leg.utid)
    if not lam:
        raw, cal = model.estimate(base, None, fam.kind, fam.period)
        leg.p_stats, leg.p_raw, leg.est, leg.verdict = None, raw, cal - 0.02, "odds only"
        leg.why = "Odds-only: too few recent games in the stats feed."
        return
    ps = model_prob(fam, leg.specifier, leg.outcome_id, *lam, rho=model.rho())
    leg.p_stats = ps
    # Model v2: logit blend of market and stats (weight tuned per market group by backtest) then the group's
    # calibration map, so the estimate is what such legs actually hit on past matches.
    leg.p_raw, leg.est = model.estimate(base, ps, fam.kind, fam.period)
    delta = ps - leg.p_market
    flag = _red_flag(leg, fam, form)
    if flag:
        leg.verdict = "dropped"
    elif delta >= -0.03:
        leg.verdict = "confirmed"
    elif delta >= -0.10:
        leg.verdict = "demoted"
    else:
        leg.verdict = "dropped"
    leg.why = _why(leg, fam, form) + (f" ⚠ {flag}" if flag else "")


def _red_flag(leg: Leg, fam: Family, f: MatchForm) -> str:
    if fam.kind not in _WIN_KINDS or leg.side not in ("home", "away"):
        return ""
    team = f.home if leg.side == "home" else f.away
    opp = f.away if leg.side == "home" else f.home
    w, d, l = team.record(n=5)
    ow, od, ol = opp.record(n=5)
    backing_win = fam.kind in ("1x2", "dnb") or (fam.kind == "ah" and float(spec_value(leg.specifier, "hcp") or 0)
                                                 * (1 if leg.side == "home" else -1) < 0)
    if backing_win and l >= 3:
        return f"{team.name} lost {l} of last 5"
    if backing_win and ow >= 4 and w <= 1:
        return f"{opp.name} won {ow} of last 5"
    if fam.kind == "dc" and l >= 4:
        return f"{team.name} lost {l} of last 5"
    return ""


def _rec(t) -> str:
    w, d, l = t.record(n=5)
    return f"W{w} D{d} L{l}"


def _why(leg: Leg, fam: Family, f: MatchForm) -> str:
    h, a, hh = f.home, f.away, f.h2h.summary()
    h2h = (f"H2H {hh['home_w']}-{hh['draws']}-{hh['away_w']}, avg {hh['avg_goals']:.1f} goals"
           if hh.get("n") else "no recent H2H")
    if fam.kind in ("total", "team_total") or fam.kind == "btts":
        def g(t, v):
            x, y = t.avg("gf", v), t.avg("ga", v)
            return f"{(x or 0) + (y or 0):.1f}"
        o_h, o_a = h.rate(lambda x: x.gf + x.ga >= 3), a.rate(lambda x: x.gf + x.ga >= 3)
        if fam.kind == "btts":
            b_h, b_a = h.rate(lambda x: x.gf > 0 and x.ga > 0), a.rate(lambda x: x.gf > 0 and x.ga > 0)
            s = f"BTTS in {b_h[0]}/{b_h[1]} ({h.name}) & {b_a[0]}/{b_a[1]} ({a.name})"
            if hh.get("n"):
                s += f"; H2H BTTS {hh['btts']}/{hh['n']}"
            return s
        if fam.kind == "team_total":
            t, o = (h, a) if fam.side == "home" else (a, h)
            v, ov = ("H", "A") if fam.side == "home" else ("A", "H")
            return (f"{t.name} scores {t.avg('gf', v) or 0:.1f}/g {'home' if v == 'H' else 'away'} "
                    f"({len(t._sub(v))} g); {o.name} concedes {o.avg('ga', ov) or 0:.1f}/g ({len(o._sub(ov))} g); {h2h}")
        nh, na = len(h._sub("H")), len(a._sub("A"))
        return (f"Avg match goals {g(h, 'H')} ({h.name}, {nh} home) / {g(a, 'A')} ({a.name}, {na} away); "
                f"O2.5 {o_h[0]}/{o_h[1]} & {o_a[0]}/{o_a[1]}; {h2h}")
    hw, hd, hl = h.record("H")
    aw, ad, al = a.record("A")
    return (f"{h.name} {_rec(h)} last 5, home W{hw} D{hd} L{hl}; {a.name} {_rec(a)} last 5, "
            f"away W{aw} D{ad} L{al}; {h2h}")
