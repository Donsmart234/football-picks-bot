"""Savings ladder (rollover mode): the user's "2odds Rollover template" re-derived for a smaller daily code.

Default since 2026-11-14 (user's choice): a code of 1.50-1.80 total odds a day (plan odds 1.65). The user can set
another band per plan ("/rollover ladder 1k 1.5-1.8"); plans started before 2026-11-14 keep the original 2.00 band.

User's original template (base 1,000, about 2.00 a day; next stake = return minus the day's save), kept for the
1.90-2.15 band (LEGACY_BAND):

    day  stake    return    save          day  stake    return    save
     1   1,000     2,000      500           9   25,000    50,000   10,000
     2   1,500     3,000    1,000          10   40,000    80,000   20,000
     3   2,000     4,000    1,000          11   60,000   120,000   20,000
     4   3,000     6,000    2,000          12  100,000   200,000   50,000
     5   4,000     8,000    2,000          13  150,000   300,000   50,000
     6   6,000    12,000    2,000          14  250,000   500,000  100,000
     7  10,000    20,000    5,000          15  400,000   800,000  300,000
     8  15,000    30,000    5,000          16  500,000 1,000,000  (end)

How it works, and how other bands are derived the same way (derive_template):
- plan odds P = the band's middle (1.65 for 1.50-1.80); the user's template rolls about 3/4 of each return and saves
  about 1/4, so each next stake is the stake x 0.75 x P rounded to a clean figure (2 significant figures), never more
  than the band floor x the stake (a win at the lowest odds still funds the next day);
- every day's savings must cover a restart two days back (savings after day i >= stake of day i-1), as the user's
  template does; where the rounded step would break that, the next stake is cut until it holds;
- 16 days; day 16's whole return is the winning.
At a 1,000 start for 1.50-1.80: 1,000, 1,200, 1,400, 1,700, 2,100, 2,600, 3,200, 4,000, 4,900, 6,100, 7,500, 9,300,
12,000, 15,000, 19,000, 24,000 -> 39,600 winning + 36,150 savings at 1.65 a day. Everything scales with start / 1000.

User's decisions (2026-11-13, kept):
- a loss on day i (i >= 3) restarts at day i-2, that day's stake taken from savings; a loss on day 1 or 2 restarts
  at day 1. If savings can't cover that stake, go back further to the latest day savings can cover; if even day 1
  can't be covered, the plan ends.
- each day's code: total odds inside the band from Safe legs (safety.py: market est AND stats model >= 85% each +
  last-10 record check), 1-4 legs, one leg per match, top leagues (no cups, national teams FIFA top ~50), no unders,
  no half / time-window markets, home or neutral picks first; highest combined estimate inside the band.
- no safe code on a day = the ladder holds (no day advanced, nothing lost, nothing staked; one short ping).

Money is tracked with the real odds of each code: a win returns stake x odds; the next day's stake is the template
stake and everything above it goes to savings. A void / partly void code that returns less than the next stake
replays the same day with the same stake; anything above that stake is saved. A restart stake comes out of savings.

Everything here is pure (no I/O) so the money rules are unit-tested; tracker.py stores the state in the plan record.
"""
from __future__ import annotations

import re

BASE = 1000.0
MIN_P = float(__import__("os").environ.get("SAFE_MIN_P") or 0.85)   # market estimate AND stats model, each (safety.py); 80% (market only) until 2026-11-14
MAX_LEGS = 4
LEG_MIN_ODDS = float(__import__("os").environ.get("LADDER_LEG_MIN_ODDS") or 1.05)   # 1.12 until 2026-11-14: 85% legs price ~1.05-1.12
MIN_STAKE = 100.0
MAX_STAKE = 100_000_000.0
DAYS = 16
DEFAULT_BAND = (1.50, 1.80)       # user's choice 2026-11-14
LEGACY_BAND = (1.90, 2.15)        # the original 2.00 ladder (plans started before 2026-11-14 keep it)
BAND_MIN, BAND_MAX = 1.40, 3.00   # what /rollover ladder ... a-b accepts
ROLL_SHARE = 0.75                 # share of a return the user's template rolls on (it saves about a quarter)
# the user's own template (stake, save) at a 1,000 start, about 2.00 a day; day 16's save is None (the end)
LEGACY_TEMPLATE: tuple[tuple[float, float | None], ...] = (
    (1000, 500), (1500, 1000), (2000, 1000), (3000, 2000), (4000, 2000), (6000, 2000), (10000, 5000),
    (15000, 5000), (25000, 10000), (40000, 20000), (60000, 20000), (100000, 50000), (150000, 50000),
    (250000, 100000), (400000, 300000), (500000, None))
MODE = "ladder"
NAME = "savings ladder"
EXCLUDE_MARKETS = ("under", "halves", "minutes")
ODDS_LO, ODDS_HI = DEFAULT_BAND


def norm_band(band=None) -> tuple[float, float]:
    """(lo, hi) of a band given as None (default), a pair, or a plan's data dict (no 'band' = a pre-2026-11-14 plan,
    the 2.00 band)."""
    if isinstance(band, dict):
        band = band.get("band") or LEGACY_BAND
    if not band:
        return DEFAULT_BAND
    lo, hi = float(band[0]), float(band[1])
    return (round(lo, 2), round(hi, 2))


def band_of(d: dict) -> tuple[float, float]:
    return norm_band(d if isinstance(d, dict) else None)


def check_band(lo, hi=None) -> tuple[float, float]:
    try:
        lo, hi = float(lo), float(hi if hi is not None else lo)
    except (TypeError, ValueError):
        raise ValueError("The daily target must be an odds range, e.g. 1.5-1.8.")
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        raise ValueError("Give the daily target as a range, e.g. 1.5-1.8.")
    if not (BAND_MIN <= lo and hi <= BAND_MAX) or hi - lo < 0.05:
        raise ValueError(f"The daily target must be a range between {BAND_MIN:.2f} and {BAND_MAX:.2f} odds, at "
                         "least 0.05 wide, e.g. 1.5-1.8.")
    return (round(lo, 2), round(hi, 2))


def plan_odds(band=None) -> float:
    """The odds the template is planned at: 2.00 for the user's own band, else the band's middle (1.65)."""
    b = norm_band(band)
    return 2.0 if b == LEGACY_BAND else round((b[0] + b[1]) / 2, 2)


TARGET = plan_odds(DEFAULT_BAND)


def band_text(band=None) -> str:
    lo, hi = norm_band(band)
    return f"{lo:.2f}-{hi:.2f}"


def label(band=None) -> str:
    return f"Ladder {band_text(band)}"


def hold_text(band=None) -> str:
    return f"No safe {band_text(band)} code today \u2014 ladder day held, nothing staked."


HOLD_TEXT = hold_text(DEFAULT_BAND)


def _nice(x: float, down: bool = False) -> float:
    e = 10 ** (__import__("math").floor(__import__("math").log10(x)) - 1)
    return float((__import__("math").floor(x / e + 1e-9) if down else round(x / e)) * e)


_TEMPLATES: dict = {}


def derive_template(band=None) -> tuple[tuple[float, float | None], ...]:
    """(stake, save) per day at a 1,000 start for a band, derived the way the user's template works (module doc)."""
    b = norm_band(band)
    if b == LEGACY_BAND:
        return LEGACY_TEMPLATE
    if b in _TEMPLATES:
        return _TEMPLATES[b]
    p = plan_odds(b)
    g = min(ROLL_SHARE * p, b[0])
    stakes, saves, sav = [BASE], [], 0.0
    for _ in range(1, DAYS):
        cur = stakes[-1]
        need = stakes[-2] if len(stakes) >= 2 else 0.0      # savings after this day cover a restart 2 days back
        nxt = _nice(cur * g)
        cap = sav + p * cur - need
        if nxt > cap:
            nxt = _nice(cap, down=True)
        nxt = min(nxt, _nice(b[0] * cur, down=True))
        save = round(p * cur - nxt, 2)
        sav += save
        saves.append(save)
        stakes.append(nxt)
    t = tuple((st, sv) for st, sv in zip(stakes, saves + [None]))
    _TEMPLATES[b] = t
    return t


def rules_line(band=None) -> str:
    """The 'Rules:' line every ladder reply starts with (plain text, no HTML)."""
    return (f"Rules: {NAME} · code {band_text(band)} total odds a day · Safe, every leg market AND stats model "
            f">={MIN_P:.0%} + last-10 check · 1-{MAX_LEGS} legs, one per match · top leagues (no cups, national teams "
            "FIFA top ~50) · no unders · full match only · home or neutral picks first · no safe code = day held")


def r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def scale(start: float | None) -> float:
    return float(start or BASE) / BASE


def check_start(start) -> float:
    try:
        s = float(start if start not in (None, "") else BASE)
    except (TypeError, ValueError):
        raise ValueError("The starting stake must be a number, e.g. 1000 or 2k.")
    if not MIN_STAKE <= s <= MAX_STAKE:
        raise ValueError(f"The starting stake must be between {MIN_STAKE:,.0f} and {MAX_STAKE:,.0f}.")
    return s


def stake_for(day: int, start: float | None, band=None) -> float:
    if not 1 <= day <= DAYS:
        raise ValueError(f"Ladder days run 1-{DAYS}.")
    return r2(derive_template(band)[day - 1][0] * scale(start))


def save_for(day: int, start: float | None, band=None) -> float | None:
    s = derive_template(band)[day - 1][1]
    return None if s is None else r2(s * scale(start))


def plan_rows(start: float | None = BASE, band=None) -> list[dict]:
    """The full template scaled to the start: day, stake, return (at the plan odds), save, next stake."""
    p = plan_odds(band)
    rows = []
    for d in range(1, DAYS + 1):
        st = stake_for(d, start, band)
        rows.append({"day": d, "stake": st, "return": r2(st * p), "save": save_for(d, start, band),
                     "next_stake": stake_for(d + 1, start, band) if d < DAYS else None})
    return rows


def totals(start: float | None = BASE, band=None) -> dict:
    rows = plan_rows(start, band)
    lo, hi = norm_band(band)
    return {"start": float(start or BASE), "final_return": rows[-1]["return"],
            "savings": r2(sum(r["save"] or 0 for r in rows)), "days": DAYS, "band": [lo, hi],
            "plan_odds": plan_odds(band)}


def restart_day(lost_day: int, savings: float, start: float | None, band=None) -> int | None:
    """Day to restart from after a loss on lost_day, or None when savings can't cover even day 1."""
    r = 1 if lost_day <= 2 else lost_day - 2
    while r >= 1 and stake_for(r, start, band) > savings + 0.005:
        r -= 1
    return r if r >= 1 else None


def new_state(start: float, band=None) -> dict:
    """Ladder fields of a plan's data (tracker.create_ladder adds the rollover fields)."""
    b = norm_band(band)
    return {"mode": MODE, "start": float(start), "band": [b[0], b[1]], "current_day": 1,
            "value": stake_for(1, start, b), "savings": 0.0, "funded": float(start), "restarts": 0, "ledger": [],
            "won_total": 0.0}


def apply_result(d: dict, status: str, odds: float | None, date: str = "", slip_id: str = "", code: str = "") -> dict:
    """Apply a settled code to the ladder state d (mutated). status: won | lost | void.
    Returns the ledger row: day, stake, odds, return, saved, savings, outcome, next_day, restart_from,
    taken_from_savings, ended (busted | completed | '')."""
    start = d["start"]
    band = band_of(d)
    day = int(d["current_day"])
    stake = float(d.get("value") or stake_for(day, start, band))
    row = {"date": date, "day": day, "stake": r2(stake), "odds": None, "return": 0.0, "saved": 0.0,
           "savings_before": r2(d.get("savings") or 0), "outcome": status, "slip_id": slip_id, "code": code,
           "next_day": day, "restart_from": None, "taken_from_savings": 0.0, "ended": ""}
    sav = float(d.get("savings") or 0)
    if status in ("won", "void"):
        o = float(odds or (1.0 if status == "void" else plan_odds(band)))
        ret = r2(stake * o)
        row.update(odds=round(o, 2), **{"return": ret})
        if day >= DAYS and ret > stake + 0.005:
            # the end: the whole return is the winning
            d["won_total"] = ret
            d["current_day"] = DAYS + 1
            d["value"] = 0.0
            row.update(next_day=None, ended="completed", outcome="won")
        else:
            nxt = stake_for(day + 1, start, band) if day < DAYS else stake
            if ret + 0.005 >= nxt and status == "won" and day < DAYS:
                saved = r2(ret - nxt)
                d["current_day"], d["value"] = day + 1, nxt
                row["next_day"] = day + 1
            else:
                # void / partly void below the next stake: replay the same day with the same stake
                saved = r2(max(0.0, ret - stake))
                row["next_day"] = day
                row["outcome"] = "replay" if status == "won" else "void"
            sav = r2(sav + saved)
            row["saved"] = saved
    elif status == "lost":
        row.update(odds=None, **{"return": 0.0})
        r = restart_day(day, sav, start, band)
        if r is None:
            row.update(next_day=None, ended="busted")
            d["current_day"], d["value"] = day, 0.0
        else:
            take = stake_for(r, start, band)
            sav = r2(sav - take)
            d["current_day"], d["value"] = r, take
            d["restarts"] = int(d.get("restarts") or 0) + 1
            row.update(next_day=r, restart_from=r, taken_from_savings=take)
    else:
        raise ValueError(f"unknown result {status}")
    d["savings"] = sav
    row["savings"] = sav
    row["restarts"] = int(d.get("restarts") or 0)
    d.setdefault("ledger", []).append(row)
    return row


def outcome_text(row: dict, d: dict) -> str:
    """Plain-text ladder line for a settlement ping (HTML-safe: no < > &)."""
    day = row["day"]
    if row["ended"] == "completed":
        return (f"Ladder COMPLETE: day {day} landed. Final return {row['return']:,.0f} + savings {row['savings']:,.0f}"
                f" = {row['return'] + row['savings']:,.0f} in total (you put in {d.get('funded', 0):,.0f}).")
    if row["outcome"] == "won":
        return (f"Ladder day {day} WON: stake {row['stake']:,.0f} x {row['odds']:.2f} = {row['return']:,.0f}. "
                f"Saved {row['saved']:,.0f}, savings now {row['savings']:,.0f}. Next: day {row['next_day']} with stake "
                f"{d['value']:,.0f} (next code tomorrow ~09:00).")
    if row["outcome"] in ("void", "replay"):
        return (f"Ladder day {day} returned {row['return']:,.0f} (void legs, odds {row['odds']:.2f}): below the next "
                f"stake, so day {day} replays with stake {d['value']:,.0f}. Saved {row['saved']:,.0f}, savings now "
                f"{row['savings']:,.0f}.")
    if row["ended"] == "busted":
        return (f"Ladder day {day} LOST (stake {row['stake']:,.0f}). Savings {row['savings']:,.0f} can't cover the "
                f"day 1 stake {stake_for(1, d['start'], band_of(d)):,.0f}, so the ladder ends here. You keep the "
                f"savings {row['savings']:,.0f}. Start a new ladder any time with /rollover ladder.")
    return (f"Ladder day {day} LOST (stake {row['stake']:,.0f}). Restarting from day {row['restart_from']}: stake "
            f"{row['taken_from_savings']:,.0f} taken from savings, savings now {row['savings']:,.0f}. "
            f"Restarts so far: {row['restarts']}. Next code tomorrow ~09:00.")


# ---------------------------------------------------------------- parsing (commands + chat)
_AMOUNT = re.compile(r"(?<![\w.])(?:ngn|n|₦)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|thousand|million)?(?![\w.])",
                     re.I)


_BAND = re.compile(r"(?<![\w.])(\d(?:\.\d{1,2})?)\s*(?:-|–|to)\s*(\d(?:\.\d{1,2})?)(?:\s*(?:total\s+)?odds?)?(?![\w.])", re.I)


def parse_band(text: str) -> tuple[float, float] | None:
    """'1.5-1.8' / '1.50 to 1.80 odds' -> (1.5, 1.8); None when no odds range is given. Raises ValueError for a
    range outside BAND_MIN-BAND_MAX."""
    for m in _BAND.finditer(text or ""):
        lo, hi = float(m.group(1)), float(m.group(2))
        if "." not in m.group(1) + m.group(2) and max(lo, hi) > BAND_MAX:
            continue
        return check_band(lo, hi)
    return None


def strip_band(text: str) -> str:
    return _BAND.sub(" ", text or "")


def parse_amount(text: str) -> float | None:
    """'2k' -> 2000, '1.5k' -> 1500, '2,000' -> 2000, '₦5000' -> 5000. The '2' of '2 odds' is ignored."""
    t = re.sub(r"\b(?:2|two)\s*-?\s*odds?\b", " ", strip_band(text), flags=re.I)
    t = re.sub(r"\b\d+\s*(?:days?|legs?)\b", " ", t, flags=re.I)
    for m in _AMOUNT.finditer(t):
        v = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        v *= 1000 if unit in ("k", "thousand") else 1_000_000 if unit in ("m", "million") else 1
        if v >= MIN_STAKE or unit:
            return v
    return None


_LADDER = re.compile(r"\b(?:(?:2|two)\s*-?\s*odds?\s+(?:savings\s+)?(?:ladder|rollover\s+template)|savings\s+ladder|"
                     r"ladder)\b", re.I)


def parse_intent(text: str) -> dict | None:
    """Chat intent for the ladder: {'action': start | plan | status, 'stake': float | None}, or None. When the text
    names an odds range ('1.5-1.8') the dict also carries 'band': (lo, hi) (or 'band_error' for a bad range)."""
    t = (text or "").strip()
    if not _LADDER.search(t):
        return None
    low = t.lower()
    stake = parse_amount(t)
    extra: dict = {}
    try:
        b = parse_band(t)
        if b:
            extra["band"] = b
    except ValueError as exc:
        extra["band_error"] = str(exc)
    if re.search(r"\b(plan|table|template|breakdown|schedule)\b", low) and not re.search(
            r"\b(start|begin|launch|run|create|kick\s*off|set\s*up)\b", low):
        return {"action": "plan", "stake": stake, **extra}
    if re.search(r"\b(start|begin|launch|run|create|kick\s*off|set\s*up|open|new)\b", low):
        return {"action": "start", "stake": stake, **extra}
    return {"action": "status", "stake": stake, **extra}


# ---------------------------------------------------------------- texts
def plan_table_text(start: float | None = BASE, html: bool = True, band=None) -> str:
    """The full 16-day table scaled to the stake, for the band. Starts with the Rules line."""
    tot = totals(start, band)
    p = plan_odds(band)
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = [rules_line(band), b(f"Savings ladder · start {tot['start']:,.0f} · code {band_text(band)} a day"),
             f"Day: stake -> return at {p:.2f} (save)"]
    for r in plan_rows(start, band):
        tail = "(end)" if r["save"] is None else f"(save {r['save']:,.0f})"
        lines.append(f"{r['day']}: {r['stake']:,.0f} -> {r['return']:,.0f} {tail}")
    fin, sav = f"{tot['final_return']:,.0f}", f"{tot['savings']:,.0f}"
    lines += [f"Total winning {b(fin)} · total savings {b(sav)} · {tot['days']} days at {p:.2f} odds a day",
              "After a win, everything above the next day's stake goes to savings. A cut on day 3 or later restarts "
              "2 days back with that stake taken from savings (day 1 or 2: back to day 1); if savings can't cover "
              "it, further back; if not even day 1, the ladder ends. No safe code on a day = the day is held.",
              f"Real odds are used for the money ({band_text(band)} a day), so savings move a little with them. "
              "Estimates, not guarantees. 18+."]
    return "\n".join(lines)


def ledger_lines(d: dict, last: int = 16) -> list[str]:
    out = []
    for r in (d.get("ledger") or [])[-last:]:
        if r["outcome"] == "lost":
            what = ("LOST, ladder ended" if r["ended"] == "busted" else
                    f"LOST, restart day {r['restart_from']} (-{r['taken_from_savings']:,.0f} from savings)")
            out.append(f"Day {r['day']} · {r.get('date') or ''} · stake {r['stake']:,.0f} · {what} · savings "
                       f"{r['savings']:,.0f}")
        else:
            out.append(f"Day {r['day']} · {r.get('date') or ''} · stake {r['stake']:,.0f} @ {r['odds']:.2f} -> "
                       f"{r['return']:,.0f} · {'WON' if r['outcome'] == 'won' else r['outcome'].upper()} · saved "
                       f"{r['saved']:,.0f} · savings {r['savings']:,.0f}")
    return out
