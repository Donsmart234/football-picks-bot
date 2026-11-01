"""Model v2 (2026-11-01): what turns market prices + team stats into the probability every leg shows.

    est = calibrate_g( blend_g( p_market, p_stats ) )

* p_market  de-margined SportyBet prices (plus the Poisson fit to the 1X2 + O/U 2.5 book, and Betradar's own
            outcome probability when the feed has it): the scanner's existing consensus.
* p_stats   Poisson goals model from the free Sportradar feed (last 10 games per team) with
            - time decay (half-life in days, tuned), shrinkage towards the league average,
            - league-specific home / away goal averages (from 2 seasons of results per league),
            - a Dixon-Coles low-score correction (rho, tuned).
* blend_g   logit-space weight of the stats model per market group, tuned by backtest.
* calibrate_g  per market group isotonic or Platt map (chosen by cross-validated log loss on the training
            seasons), so "80%" means about 80% on past matches; it may move a probability by at most 3 pts
            (max_shift: past data is thin above 85%). Groups without backtest data are left as-is.

All parameters live in model_params.json, written by backtest/backtest.py. Pure Python (no numpy on Render).
Every number is an estimate, never a guarantee.
"""
from __future__ import annotations

import json
import math
import time
from bisect import bisect_left
from pathlib import Path

PARAMS_PATH = Path(__file__).resolve().parent / "model_params.json"
DEFAULTS = {
    "version": "v2-default",
    "half_life_days": 120.0,
    "shrink": 4.0,
    "rho": -0.06,
    "default_league": {"home": 1.52, "away": 1.22},
    "leagues": {},
    "weights": {},          # group -> stats weight (logit blend)
    "default_weight": 0.15,
    "calibration": {},      # group -> {"kind": "isotonic", "x": [...], "y": [...]} | {"kind": "platt", "a":, "b":}
    "max_shift": 0.03,      # calibration may move a probability by at most 3 pts (sparse data near 80-95%)
}


def _load() -> dict:
    p = dict(DEFAULTS)
    try:
        p.update(json.loads(PARAMS_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return p


PARAMS = _load()


def reload() -> dict:
    global PARAMS
    PARAMS = _load()
    return PARAMS


# ------------------------------------------------------------------ helpers
def clamp(p: float, lo: float = 1e-4, hi: float = 1 - 1e-4) -> float:
    return lo if p < lo else hi if p > hi else p


def logit(p: float) -> float:
    p = clamp(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    z = math.exp(x)
    return z / (1 + z)


def group_of(kind: str, period: str = "ft") -> str:
    """Market group used for blend weights and calibration."""
    if period == "ft" and kind in ("1x2", "dc", "dnb", "total", "btts", "team_total"):
        return kind
    if period == "h1" and kind == "total":
        return "h1_total"
    return "other"


def league_avgs(utid: str | int | None) -> tuple[float, float]:
    lg = PARAMS.get("leagues", {}).get(str(utid or "")) if utid else None
    d = lg or PARAMS["default_league"]
    return float(d["home"]), float(d["away"])


def utid_of(tournament_id: str) -> str:
    """'sr:tournament:17' -> '17' (Sportradar unique tournament id = SportyBet tournament id)."""
    t = (tournament_id or "").rsplit(":", 1)[-1]
    return t if t.isdigit() else ""


# ------------------------------------------------------------------ stats model
def team_strength(games, venue_avgs: tuple[float, float], now_uts: float, half_life: float, shrink: float):
    """games: iterable of objects with venue ('H'/'A'), gf, ga, uts (newest first, any count).
    Returns (attack, defence) multipliers vs the league average (1.0 = average), time-decayed and shrunk."""
    h_avg, a_avg = venue_avgs
    mean = (h_avg + a_avg) / 2
    sw_gf = sw_ef = sw_ga = sw_ea = 0.0
    for g in games:
        age = max(0.0, (now_uts - (g.uts or now_uts)) / 86400.0)
        w = 0.5 ** (age / half_life) if half_life > 0 else 1.0
        ef, ea = (h_avg, a_avg) if g.venue == "H" else (a_avg, h_avg)
        sw_gf += w * g.gf
        sw_ef += w * ef
        sw_ga += w * g.ga
        sw_ea += w * ea
    att = (sw_gf + shrink * mean) / (sw_ef + shrink * mean)
    dfn = (sw_ga + shrink * mean) / (sw_ea + shrink * mean)
    return att, dfn


def stats_lambdas_v2(home_games, away_games, utid: str | int | None = None, now_uts: float | None = None,
                     params: dict | None = None, min_games: int = 4) -> tuple[float, float] | None:
    """Expected goals (home, away) from both teams' recent games. None when either side has < min_games."""
    p = params or PARAMS
    hg, ag = list(home_games or []), list(away_games or [])
    if len(hg) < min_games or len(ag) < min_games:
        return None
    now = now_uts if now_uts is not None else time.time()
    avgs = league_avgs(utid) if params is None else _avgs_from(p, utid)
    h_att, h_def = team_strength(hg, avgs, now, p["half_life_days"], p["shrink"])
    a_att, a_def = team_strength(ag, avgs, now, p["half_life_days"], p["shrink"])
    lh = avgs[0] * h_att * a_def
    la = avgs[1] * a_att * h_def
    return max(0.15, min(4.5, lh)), max(0.15, min(4.5, la))


def _avgs_from(p: dict, utid) -> tuple[float, float]:
    lg = p.get("leagues", {}).get(str(utid or "")) if utid else None
    d = lg or p["default_league"]
    return float(d["home"]), float(d["away"])


def rho() -> float:
    return float(PARAMS.get("rho", 0.0))


# ------------------------------------------------------------------ blend + calibration
def blend(p_market: float, p_stats: float | None, group: str, params: dict | None = None) -> float:
    p = params or PARAMS
    if p_stats is None:
        return p_market
    w = float(p.get("weights", {}).get(group, p.get("default_weight", 0.15)))
    return sigmoid((1 - w) * logit(p_market) + w * logit(p_stats))


def calibrate(p: float, group: str, params: dict | None = None, market_only: bool = False) -> float:
    """Map a raw probability to its calibrated value for the market group (identity when the group has no map)."""
    prm = params or PARAMS
    cal = prm.get("calibration", {})
    c = (cal.get("mkt:" + group) if market_only else None) or cal.get(group)
    if not c:
        return p
    ms = float(prm.get("max_shift", DEFAULTS["max_shift"]))
    if c.get("kind") == "platt":
        y = clamp(sigmoid(c["a"] * logit(p) + c["b"]), 0.001, 0.999)
    else:
        xs, ys = c["x"], c["y"]
        if p <= xs[0]:
            y = ys[0]
        elif p >= xs[-1]:
            y = ys[-1]
        else:
            i = bisect_left(xs, p)
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            y = y0 + (y1 - y0) * (p - x0) / (x1 - x0) if x1 > x0 else y1
    return max(p - ms, min(p + ms, y))


def estimate(p_market: float, p_stats: float | None, kind: str, period: str = "ft",
             params: dict | None = None) -> tuple[float, float]:
    """(raw blended probability, calibrated estimate) for one outcome."""
    g = group_of(kind, period)
    raw = blend(p_market, p_stats, g, params)
    return raw, calibrate(raw, g, params, market_only=p_stats is None)


def info() -> dict:
    return {"version": PARAMS.get("version"), "half_life_days": PARAMS.get("half_life_days"),
            "shrink": PARAMS.get("shrink"), "rho": PARAMS.get("rho"), "weights": PARAMS.get("weights"),
            "calibrated_groups": sorted(PARAMS.get("calibration", {}).keys()),
            "leagues": len(PARAMS.get("leagues", {}))}
