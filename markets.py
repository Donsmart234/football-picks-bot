"""Market catalogue for the scanner: which SportyBet (Betradar) markets we read, how we de-margin them, and how a
Poisson score model prices each outcome. Half-goal lines can't push. Asian goal lines (SportyBet lists them inside the
plain Over/Under markets 18 / 19 / 20 as whole-number specifiers, e.g. total=3 "Under 3"; quarter lines total=2.25 if
ever offered) are priced push-aware: est = probability of a full win (a push / half result is not a win), with the
push (stake back) and half-win / half-loss chances carried alongside (`outcome_parts`)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# family: how the outcome is evaluated on a score grid. period: ft / h1 / h2. side: home/away for team totals.
# overlap=True means outcomes overlap (Double Chance: fair probabilities sum to 2).
@dataclass(frozen=True)
class Family:
    kind: str          # 1x2 | dc | dnb | total | btts | team_total | ah | eh
    period: str        # ft | h1 | h2
    name: str
    side: str = ""     # for team_total: home | away
    overlap: bool = False


FAMILIES: dict[str, Family] = {
    "1": Family("1x2", "ft", "1X2"),
    "10": Family("dc", "ft", "Double Chance", overlap=True),
    "11": Family("dnb", "ft", "Draw No Bet"),
    "18": Family("total", "ft", "Over/Under"),
    "29": Family("btts", "ft", "GG/NG"),
    "19": Family("team_total", "ft", "Home Over/Under", side="home"),
    "20": Family("team_total", "ft", "Away Over/Under", side="away"),
    "16": Family("ah", "ft", "Asian Handicap"),
    "14": Family("eh", "ft", "Handicap"),
    "60": Family("1x2", "h1", "1st Half 1X2"),
    "63": Family("dc", "h1", "1st Half Double Chance", overlap=True),
    "64": Family("dnb", "h1", "1st Half Draw No Bet"),
    "68": Family("total", "h1", "1st Half Over/Under"),
    "75": Family("btts", "h1", "1st Half GG/NG"),
    "69": Family("team_total", "h1", "1st Half Home O/U", side="home"),
    "70": Family("team_total", "h1", "1st Half Away O/U", side="away"),
    "83": Family("1x2", "h2", "2nd Half 1X2"),
    "85": Family("dc", "h2", "2nd Half Double Chance", overlap=True),
    "86": Family("dnb", "h2", "2nd Half Draw No Bet"),
    "90": Family("total", "h2", "2nd Half Over/Under"),
    "95": Family("btts", "h2", "2nd Half GG/NG"),
}
PERIOD_SHARE = {"ft": 1.0, "h1": 0.45, "h2": 0.55}   # typical share of goals per half
MAX_G = 10

# Outcomes that talk about one team (for the stats "why" and red flags). value: "home" | "away" | "draw" | None
_SIDE = {("1x2", "1"): "home", ("1x2", "3"): "away", ("1x2", "2"): "draw",
         ("dc", "9"): "home", ("dc", "11"): "away", ("dc", "10"): None,
         ("dnb", "4"): "home", ("dnb", "5"): "away",
         ("ah", "1714"): "home", ("ah", "1715"): "away", ("eh", "1711"): "home", ("eh", "1713"): "away",
         ("eh", "1712"): "draw"}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9:=|.+\-_ ]{0,80}$")


def valid_token(s: str) -> bool:
    return bool(_SAFE_TOKEN.match(s or ""))


def spec_value(spec: str, key: str) -> str | None:
    for part in (spec or "").split("|"):
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return None


def usable_line(fam: Family, spec: str) -> bool:
    """Only lines that can't push: x.5 totals, x.5 Asian handicaps, integer European handicaps."""
    if fam.kind in ("total", "team_total"):
        v = spec_value(spec, "total")
        return v is not None and _is_half(v)
    if fam.kind == "ah":
        v = spec_value(spec, "hcp")
        return v is not None and _is_half(v)
    if fam.kind == "eh":
        v = spec_value(spec, "hcp")
        return bool(v and re.fullmatch(r"\d+:\d+", v))
    return not spec


def line_kind(fam: Family | None, spec: str) -> str | None:
    """Goal-total line type: 'half' (x.5, no push), 'whole' (Asian: push on exactly x), 'quarter' (Asian x.25 / x.75:
    stake split over the two neighbouring lines), None for other markets / unreadable lines."""
    if fam is None or fam.kind not in ("total", "team_total"):
        return None
    try:
        x = abs(float(spec_value(spec, "total") or "nan"))
    except ValueError:
        return None
    if x != x:
        return None
    f = round((x % 1) * 4) / 4
    return {0.5: "half", 0.0: "whole", 1.0: "whole", 0.25: "quarter", 0.75: "quarter"}.get(f)


def asian_line(fam: Family | None, spec: str) -> bool:
    return line_kind(fam, spec) in ("whole", "quarter")


def is_asian_spec(market_id: str, spec: str) -> bool:
    return asian_line(FAMILIES.get(str(market_id or "")), spec or "")


def _ou_result(t: int, line: float, over: bool) -> int:
    """+1 win, 0 push, -1 loss for one plain line."""
    if t == line:
        return 0
    return 1 if (t > line) == over else -1


def ou_parts_from_dist(dist: list[float], line: float, over: bool) -> dict:
    """Settlement chances of an Over/Under bet on `line` given P(total goals = t) for t = 0..len-1.
    win / half_win / push / half_loss / loss (half_* only on quarter lines)."""
    kind = round((abs(line) % 1) * 4) / 4
    comps = [line - 0.25, line + 0.25] if kind in (0.25, 0.75) else [line]
    out = {"win": 0.0, "half_win": 0.0, "push": 0.0, "half_loss": 0.0, "loss": 0.0}
    for t, p in enumerate(dist):
        r = sum(_ou_result(t, c, over) for c in comps)
        if len(comps) == 1:
            k = {1: "win", 0: "push", -1: "loss"}[r]
        else:
            k = {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[r]
        out[k] += p
    return out


def _is_half(v: str) -> bool:
    try:
        x = float(v)
    except ValueError:
        return False
    return abs(abs(x) % 1 - 0.5) < 1e-9


def side_of(fam: Family, outcome_id: str) -> str | None:
    if fam.kind == "team_total":
        return fam.side
    return _SIDE.get((fam.kind, outcome_id))


# ------------------------------------------------------------------ Poisson grid
def _pmf(lam: float) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, MAX_G + 1):
        p.append(p[-1] * lam / k)
    return p


def dc_tau(i: int, j: int, lh: float, la: float, rho: float) -> float:
    """Dixon-Coles low-score correction (rho < 0 lifts 0-0 / 1-1 and trims 1-0 / 0-1)."""
    if i == 0 and j == 0:
        return max(0.0, 1 - lh * la * rho)
    if i == 0 and j == 1:
        return max(0.0, 1 + lh * rho)
    if i == 1 and j == 0:
        return max(0.0, 1 + la * rho)
    if i == 1 and j == 1:
        return max(0.0, 1 - rho)
    return 1.0


class Grid:
    """Joint score distribution for one period (Poisson, optional Dixon-Coles correction)."""

    def __init__(self, lh: float, la: float, rho: float = 0.0):
        lh, la = max(lh, 0.02), max(la, 0.02)
        ph, pa = _pmf(lh), _pmf(la)
        self.cells = [(i, j, x * y * (dc_tau(i, j, lh, la, rho) if rho and i < 2 and j < 2 else 1.0))
                      for i, x in enumerate(ph) for j, y in enumerate(pa)]
        s = sum(c[2] for c in self.cells)
        self.cells = [(i, j, p / s) for i, j, p in self.cells]

    def prob(self, cond) -> float:
        return sum(p for i, j, p in self.cells if cond(i, j))

    def goals_dist(self, which: str = "total") -> list[float]:
        """P(goals = t): which = total | home | away."""
        d = [0.0] * (2 * MAX_G + 1)
        for i, j, p in self.cells:
            d[i + j if which == "total" else i if which == "home" else j] += p
        return d


def _grid(fam: Family, lh: float, la: float, grid_cache: dict | None, rho: float) -> Grid:
    share = PERIOD_SHARE[fam.period]
    key = (fam.period, round(lh, 3), round(la, 3), round(rho, 4))
    g = grid_cache.get(key) if grid_cache is not None else None
    if g is None:
        g = Grid(lh * share, la * share, rho)
        if grid_cache is not None:
            grid_cache[key] = g
    return g


def outcome_parts(fam: Family, spec: str, outcome_id: str, lh: float, la: float,
                  grid_cache: dict | None = None, rho: float = 0.0) -> dict | None:
    """Settlement chances {win, half_win, push, half_loss, loss} of a goal-total outcome (any line type)."""
    if fam.kind not in ("total", "team_total") or outcome_id not in ("12", "13"):
        return None
    try:
        line = float(spec_value(spec, "total") or "nan")
    except ValueError:
        return None
    if line != line:
        return None
    g = _grid(fam, lh, la, grid_cache, rho)
    which = "total" if fam.kind == "total" else fam.side
    return ou_parts_from_dist(g.goals_dist(which), line, outcome_id == "12")


def model_prob(fam: Family, spec: str, outcome_id: str, lh: float, la: float,
               grid_cache: dict | None = None, rho: float = 0.0) -> float | None:
    """Poisson probability of one outcome given full-time expected goals (lh, la); rho = Dixon-Coles.
    Asian goal lines (whole / quarter): the probability of a FULL win (a push or a half result is not a win)."""
    g = _grid(fam, lh, la, grid_cache, rho)
    k, o = fam.kind, outcome_id
    if k == "1x2":
        cond = {"1": lambda i, j: i > j, "2": lambda i, j: i == j, "3": lambda i, j: i < j}.get(o)
        return g.prob(cond) if cond else None
    if k == "dc":
        cond = {"9": lambda i, j: i >= j, "10": lambda i, j: i != j, "11": lambda i, j: i <= j}.get(o)
        return g.prob(cond) if cond else None
    if k == "dnb":
        h, a = g.prob(lambda i, j: i > j), g.prob(lambda i, j: i < j)
        if h + a <= 0:
            return None
        return {"4": h / (h + a), "5": a / (h + a)}.get(o)
    if k in ("total", "team_total"):
        line = float(spec_value(spec, "total") or "nan")
        if asian_line(fam, spec):
            parts = outcome_parts(fam, spec, o, lh, la, grid_cache, rho)
            return parts["win"] if parts else None
        if k == "total":
            over = g.prob(lambda i, j: i + j > line)
        elif fam.side == "home":
            over = g.prob(lambda i, j: i > line)
        else:
            over = g.prob(lambda i, j: j > line)
        return {"12": over, "13": 1 - over}.get(o)
    if k == "btts":
        y = g.prob(lambda i, j: i > 0 and j > 0)
        return {"74": y, "76": 1 - y}.get(o)
    if k == "ah":
        h = float(spec_value(spec, "hcp") or "nan")
        hw = g.prob(lambda i, j: i + h > j)
        return {"1714": hw, "1715": 1 - hw}.get(o)
    if k == "eh":
        a, b = (int(x) for x in (spec_value(spec, "hcp") or "0:0").split(":"))
        cond = {"1711": lambda i, j: i + a > j + b, "1712": lambda i, j: i + a == j + b,
                "1713": lambda i, j: i + a < j + b}.get(o)
        return g.prob(cond) if cond else None
    return None


def devig_group(odds: list[float], overlap: bool) -> list[float]:
    inv = [1 / o for o in odds]
    s = sum(inv) / (2 if overlap else 1)
    return [x / s for x in inv]


def pretty_pick(fam: Family, outcome_desc: str, home: str, away: str, spec: str = "") -> str:
    d = outcome_desc or ""
    if fam.kind in ("1x2", "dnb"):
        return {"Home": home, "Away": away, "Draw": "Draw"}.get(d, d)
    if fam.kind == "dc":
        return {"Home or Draw": f"{home} or Draw", "Draw or Away": f"Draw or {away}",
                "Home or Away": f"{home} or {away}"}.get(d, d)
    if fam.kind in ("ah", "eh"):
        return d.replace("Home", home).replace("Away", away)
    if fam.kind == "btts":
        return {"Yes": "Both teams score", "No": "Not both teams score"}.get(d, d)
    asian = "Asian " if fam.kind in ("total", "team_total") and asian_line(fam, spec) else ""
    if fam.kind == "team_total":
        return f"{home if fam.side == 'home' else away} {asian}{d}"
    return asian + d


def market_label(fam: Family | None, spec: str, desc: str) -> str:
    """Market name shown on a leg: Asian goal lines are labelled as such ('Asian Over/Under')."""
    if fam is not None and asian_line(fam, spec):
        if fam.kind == "total":
            return ("1st Half - " if fam.period == "h1" else "2nd Half - " if fam.period == "h2" else "") + \
                "Asian Over/Under"
        return (desc or fam.name).replace("Over/Under", "Asian Over/Under").replace("O/U", "Asian O/U")
    return desc
