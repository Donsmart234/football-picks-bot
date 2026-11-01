"""Poisson goals model blended with de-vigged SportyBet prices. Output is an estimate, never a guarantee."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import model
from bookmakers import (MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, OU_SPECIFIER, SELECTIONS, Event)
from markets import Grid
from stats import MatchStats, TeamStats

MAX_GOALS = 10
LEAGUE_HOME_AVG = 1.50   # typical goals per game by home sides across leagues
LEAGUE_AWAY_AVG = 1.20
STATS_WEIGHT = 0.35      # weight of the stats model (last-10 games, small samples) vs the de-vigged market

Key = tuple[str, str]  # (market_id, outcome_id)


def _pmf(lam: float) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, MAX_GOALS + 1):
        p.append(p[-1] * lam / k)
    return p


def poisson_markets(lh: float, la: float) -> dict[Key, float]:
    ph, pa = _pmf(lh), _pmf(la)
    home = draw = away = over = btts = 0.0
    total = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            p = x * y
            total += p
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            if i + j >= 3:
                over += p
            if i > 0 and j > 0:
                btts += p
    home, draw, away, over, btts = (v / total for v in (home, draw, away, over, btts))
    return _complete({(MARKET_1X2, "1"): home, (MARKET_1X2, "2"): draw, (MARKET_1X2, "3"): away,
                      (MARKET_OU, "12"): over, (MARKET_BTTS, "74"): btts})


def _complete(p: dict[Key, float]) -> dict[Key, float]:
    """Fill complementary outcomes and derive Double Chance from 1X2 so everything is consistent."""
    h, d, a = p[(MARKET_1X2, "1")], p[(MARKET_1X2, "2")], p[(MARKET_1X2, "3")]
    s = h + d + a
    h, d, a = h / s, d / s, a / s
    out = dict(p)
    out.update({(MARKET_1X2, "1"): h, (MARKET_1X2, "2"): d, (MARKET_1X2, "3"): a,
                (MARKET_DC, "9"): h + d, (MARKET_DC, "10"): h + a, (MARKET_DC, "11"): d + a,
                (MARKET_OU, "13"): 1 - p[(MARKET_OU, "12")], (MARKET_BTTS, "76"): 1 - p[(MARKET_BTTS, "74")]})
    return out


def devig(odds: list[float | None]) -> list[float] | None:
    """Proportional de-vig: implied probabilities rescaled to sum to 1."""
    if not odds or any(o is None or o <= 1.0 for o in odds):
        return None
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def market_probs(event: Event) -> dict[Key, float]:
    out: dict[Key, float] = {}
    x = devig([event.odds(MARKET_1X2, o) for o in ("1", "2", "3")])
    if x:
        out.update({(MARKET_1X2, "1"): x[0], (MARKET_1X2, "2"): x[1], (MARKET_1X2, "3"): x[2]})
    ou = devig([event.odds(MARKET_OU, o, OU_SPECIFIER) for o in ("12", "13")])
    if ou:
        out.update({(MARKET_OU, "12"): ou[0], (MARKET_OU, "13"): ou[1]})
    bt = devig([event.odds(MARKET_BTTS, o) for o in ("74", "76")])
    if bt:
        out.update({(MARKET_BTTS, "74"): bt[0], (MARKET_BTTS, "76"): bt[1]})
    dc_raw = [event.odds(MARKET_DC, o) for o in ("9", "10", "11")]
    if all(dc_raw):  # DC outcomes overlap, sum of fair probs is 2
        inv = [1 / o for o in dc_raw]
        s = sum(inv) / 2
        out.update({(MARKET_DC, k): v / s for k, v in zip(("9", "10", "11"), inv)})
    return out


def fit_lambdas(target: dict[Key, float]) -> tuple[float, float] | None:
    """Find Poisson (home, away) expected goals that best reproduce the market's 1X2 (+ O/U 2.5)."""
    if (MARKET_1X2, "1") not in target:
        return None
    keys = [(MARKET_1X2, "1"), (MARKET_1X2, "3"), (MARKET_1X2, "2")]
    if (MARKET_OU, "12") in target:
        keys.append((MARKET_OU, "12"))

    def err(lh: float, la: float) -> float:
        p = poisson_markets(lh, la)
        return sum((p[k] - target[k]) ** 2 for k in keys)

    best = min(((err(h / 10, a / 10), h / 10, a / 10) for h in range(2, 46) for a in range(2, 46)))
    _, bh, ba = best
    fine = min(((err(bh + dh / 100, ba + da / 100), bh + dh / 100, ba + da / 100)
                for dh in range(-10, 11, 2) for da in range(-10, 11, 2) if bh + dh / 100 > 0.05 and ba + da / 100 > 0.05))
    return fine[1], fine[2]


def _shrunk(rate: float | None, n: int, prior: float) -> float:
    if rate is None or n <= 0:
        return prior
    w = n / (n + 3)
    return w * rate + (1 - w) * prior


def _form_mult(form: str) -> float:
    last = [c for c in form.upper() if c in "WDL"][-5:]
    if len(last) < 3:
        return 1.0
    ppg = sum(3 if c == "W" else 1 if c == "D" else 0 for c in last) / len(last)
    return max(0.92, min(1.08, 1 + 0.1 * (ppg - 1.4) / 1.6))


def stats_lambdas(ms: MatchStats) -> tuple[float, float] | None:
    h, a = ms.home, ms.away
    if not h or not a or (h.played_home + a.played_away) < 2:
        return None
    h_att = _shrunk(h.gf_home, h.played_home, LEAGUE_HOME_AVG)
    h_def = _shrunk(h.ga_home, h.played_home, LEAGUE_AWAY_AVG)
    a_att = _shrunk(a.gf_away, a.played_away, LEAGUE_AWAY_AVG)
    a_def = _shrunk(a.ga_away, a.played_away, LEAGUE_HOME_AVG)
    lh = h_att * a_def / LEAGUE_HOME_AVG
    la = a_att * h_def / LEAGUE_AWAY_AVG
    lh *= _form_mult(h.form)
    la *= _form_mult(a.form)
    if len(ms.h2h) >= 3:  # light head-to-head nudge on the goal total
        n = min(len(ms.h2h), 6)
        g = sum(x + y for x, y in ms.h2h[:n]) / n
        t = lh + la
        f = max(0.9, min(1.1, 1 + 0.15 * (g - t) / max(t, 0.5) * n / 6))
        lh, la = lh * f, la * f
    return max(0.15, min(4.5, lh)), max(0.15, min(4.5, la))


@dataclass
class Pick:
    market_id: str
    outcome_id: str
    specifier: str
    label: str
    prob: float
    odds: float | None

    @property
    def ev(self) -> float | None:
        return self.prob * self.odds if self.odds else None


@dataclass
class Prediction:
    event: Event
    probs: dict[Key, float]
    source: str                      # "stats+odds" or "odds-only"
    xg: tuple[float, float] | None
    best: Pick | None
    reasoning: str
    picks: list[Pick] = field(default_factory=list)
    form: object = None              # srstats.MatchForm when the free stats feed answered


def _v2_probs(base: dict[Key, float], lam: tuple[float, float] | None) -> dict[Key, float]:
    """Model v2 for the card: stats grid (Dixon-Coles) blended with the market per group, then calibrated.
    lam None = market only (calibrated)."""
    ps = None
    if lam:
        g = Grid(*lam, model.rho())
        h, d = g.prob(lambda i, j: i > j), g.prob(lambda i, j: i == j)
        ps = {(MARKET_1X2, "1"): h, (MARKET_1X2, "2"): d, (MARKET_1X2, "3"): 1 - h - d,
              (MARKET_OU, "12"): g.prob(lambda i, j: i + j > 2.5), (MARKET_BTTS, "74"): g.prob(lambda i, j: i > 0 and j > 0)}
    out = {}
    for k, kind in (((MARKET_1X2, "1"), "1x2"), ((MARKET_1X2, "2"), "1x2"), ((MARKET_1X2, "3"), "1x2"),
                    ((MARKET_OU, "12"), "total"), ((MARKET_BTTS, "74"), "btts")):
        out[k] = model.estimate(base[k], ps[k] if ps else None, kind)[1]
    return out


def predict(event: Event, ms: MatchStats | None = None, form=None) -> Prediction | None:
    """form (srstats.MatchForm) -> model v2; ms only (API-Football fallback) -> the older stats blend."""
    mkt = market_probs(event)
    fitted = fit_lambdas(mkt)
    if fitted is None:
        return None
    fit_probs = poisson_markets(*fitted)
    base = {k: mkt.get(k, fit_probs[k]) for k in fit_probs}  # market where priced, Poisson-fit elsewhere

    lam2 = None
    if form is not None and getattr(form, "ok", False):
        lam2 = model.stats_lambdas_v2(form.home.games, form.away.games, model.utid_of(event.tournament_id))
    s_lam = stats_lambdas(ms) if ms and not lam2 else None
    if lam2:
        raw = _v2_probs(base, lam2)
        xg = fitted
        source = f"stats+odds (model {model.PARAMS.get('version', 'v2')}, calibrated)"
    elif s_lam:
        sm = poisson_markets(*s_lam)
        raw = {k: STATS_WEIGHT * sm[k] + (1 - STATS_WEIGHT) * base[k] for k in base}
        xg = (STATS_WEIGHT * s_lam[0] + (1 - STATS_WEIGHT) * fitted[0],
              STATS_WEIGHT * s_lam[1] + (1 - STATS_WEIGHT) * fitted[1])
        source = "stats+odds"
    else:
        raw = {k: 0.5 * fit_probs[k] + 0.5 * base[k] for k in base}
        xg = fitted
        source = "odds-only"
    probs = _complete(raw)

    picks = []
    for (mid, oid), (label, spec) in SELECTIONS.items():
        picks.append(Pick(mid, oid, spec, label, probs[(mid, oid)], event.odds(mid, oid, spec)))
    priced = [p for p in picks if p.odds]
    pool = [p for p in priced if p.prob >= 0.5 and p.odds >= 1.25]
    best = max(pool, key=lambda p: p.ev) if pool else (max(priced, key=lambda p: p.prob) if priced else None)

    reasoning = _reason(event, probs, xg, source, ms, best)
    if lam2:
        reasoning += f"\nStats model xG {lam2[0]:.1f}–{lam2[1]:.1f} (last 10 games, time-weighted)."
    return Prediction(event, probs, source, xg, best, reasoning, picks)


def _reason(event: Event, probs: dict[Key, float], xg, source: str, ms: MatchStats | None, best: Pick | None) -> str:
    h, d, a = (probs[(MARKET_1X2, o)] for o in ("1", "2", "3"))
    fav = event.home if h >= a else event.away
    line1 = f"Expected goals ≈ {xg[0]:.1f}–{xg[1]:.1f}; {fav} favoured ({max(h, a):.0%}), draw {d:.0%}."
    if source.startswith("stats+odds") and ms and ms.home and ms.away:
        hf, af = ms.home.form[-5:] or "n/a", ms.away.form[-5:] or "n/a"
        extra = f" Form {hf} vs {af}"
        if len(ms.h2h) >= 3:
            n = min(len(ms.h2h), 6)
            extra += f"; last {n} H2H avg {sum(x + y for x, y in ms.h2h[:n]) / n:.1f} goals"
        line2 = extra.strip() + "."
    else:
        line2 = "Odds-only estimate (no team stats available): built from SportyBet's de-vigged prices."
    if best and best.ev and best.ev >= 1.05 and source.startswith("stats+odds"):
        line2 += f" {best.label} looks like value vs the price."
    return f"{line1}\n{line2}"
