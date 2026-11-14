"""2-odds savings ladder: the user's "2odds Rollover template" as a rollover mode.

Template (base stake 1,000 NGN, about 2.00 odds a day; each day's next stake = return minus the day's save):

    day  stake    return    save
     1   1,000     2,000      500
     2   1,500     3,000    1,000
     3   2,000     4,000    1,000
     4   3,000     6,000    2,000
     5   4,000     8,000    2,000
     6   6,000    12,000    2,000
     7  10,000    20,000    5,000
     8  15,000    30,000    5,000
     9  25,000    50,000   10,000
    10  40,000    80,000   20,000
    11  60,000   120,000   20,000
    12 100,000   200,000   50,000
    13 150,000   300,000   50,000
    14 250,000   500,000  100,000
    15 400,000   800,000  300,000
    16 500,000 1,000,000  (end)

Total winning 1,000,000, total savings 568,500, 16 days. The ladder scales with the starting stake (every stake and
save x start / 1000). User's decisions (2026-11-13):
- a loss on day i (i >= 3) restarts at day i-2, that day's stake taken from savings; a loss on day 1 or 2 restarts
  at day 1. If savings can't cover that stake, go back further to the latest day savings can cover; if even day 1
  can't be covered, the plan ends.
- each day's code is about 2.00 total odds (1.90-2.15) from Safe legs (since 2026-11-14: market est AND stats model
  >= 85% each + last-10 record check, safety.py; was est >= 80%), as few legs as possible,
  top leagues, no unders, no half / time-window markets, home or neutral picks preferred, every leg stats-confirmed.
- no safe slip on a day = the ladder holds (no day advanced, nothing lost, nothing staked; one short ping).

Money is tracked honestly with the real odds of each code: a win returns stake x odds; the next day's stake is the
template stake and everything above it goes to savings (so odds above 2.00 save a little more, odds below 2.00 a
little less than the template). A void / partly void code that returns less than the next stake replays the same
day with the same stake; anything above that stake is saved. A restart stake is deducted from savings.

Everything here is pure (no I/O) so the money rules are unit-tested; tracker.py stores the state in the plan record.
"""
from __future__ import annotations

import re

BASE = 1000.0
TARGET = 2.0
ODDS_LO = 1.90
ODDS_HI = 2.15
MIN_P = float(__import__("os").environ.get("SAFE_MIN_P") or 0.85)   # market estimate AND stats model, each (safety.py); 80% (market only) until 2026-11-14
MAX_LEGS = 4
MIN_STAKE = 100.0
MAX_STAKE = 100_000_000.0
# (stake, save) at a 1,000 start; day 16's save is None (the end: the whole return is the winning)
TEMPLATE: tuple[tuple[float, float | None], ...] = (
    (1000, 500), (1500, 1000), (2000, 1000), (3000, 2000), (4000, 2000), (6000, 2000), (10000, 5000),
    (15000, 5000), (25000, 10000), (40000, 20000), (60000, 20000), (100000, 50000), (150000, 50000),
    (250000, 100000), (400000, 300000), (500000, None))
DAYS = len(TEMPLATE)
MODE = "ladder"
NAME = "2-odds savings ladder"
EXCLUDE_MARKETS = ("under", "halves", "minutes")
HOLD_TEXT = "No safe 2.00 today \u2014 ladder day held, nothing staked."


def rules_line() -> str:
    """The 'Rules:' line every ladder reply starts with (plain text, no HTML)."""
    return (f"Rules: {NAME} · code {TARGET:.2f} odds ({ODDS_LO:.2f}-{ODDS_HI:.2f}) · Safe, every leg market AND stats "
            f"model >={MIN_P:.0%} + last-10 check · 1-{MAX_LEGS} legs · top leagues (no cups, national teams FIFA top "
            "~50) · no unders · full match only · home or neutral picks preferred · no safe code = day held")


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


def stake_for(day: int, start: float | None) -> float:
    if not 1 <= day <= DAYS:
        raise ValueError(f"Ladder days run 1-{DAYS}.")
    return r2(TEMPLATE[day - 1][0] * scale(start))


def save_for(day: int, start: float | None) -> float | None:
    s = TEMPLATE[day - 1][1]
    return None if s is None else r2(s * scale(start))


def plan_rows(start: float | None = BASE) -> list[dict]:
    """The full template scaled to the start: day, stake, return (x2.00), save, next stake."""
    rows = []
    for d in range(1, DAYS + 1):
        st = stake_for(d, start)
        rows.append({"day": d, "stake": st, "return": r2(st * TARGET), "save": save_for(d, start),
                     "next_stake": stake_for(d + 1, start) if d < DAYS else None})
    return rows


def totals(start: float | None = BASE) -> dict:
    rows = plan_rows(start)
    return {"start": float(start or BASE), "final_return": rows[-1]["return"],
            "savings": r2(sum(r["save"] or 0 for r in rows)), "days": DAYS}


def restart_day(lost_day: int, savings: float, start: float | None) -> int | None:
    """Day to restart from after a loss on lost_day, or None when savings can't cover even day 1."""
    r = 1 if lost_day <= 2 else lost_day - 2
    while r >= 1 and stake_for(r, start) > savings + 0.005:
        r -= 1
    return r if r >= 1 else None


def new_state(start: float) -> dict:
    """Ladder fields of a plan's data (tracker.create_ladder adds the rollover fields)."""
    return {"mode": MODE, "start": float(start), "current_day": 1, "value": stake_for(1, start), "savings": 0.0,
            "funded": float(start), "restarts": 0, "ledger": [], "won_total": 0.0}


def apply_result(d: dict, status: str, odds: float | None, date: str = "", slip_id: str = "", code: str = "") -> dict:
    """Apply a settled code to the ladder state d (mutated). status: won | lost | void.
    Returns the ledger row: day, stake, odds, return, saved, savings, outcome, next_day, restart_from,
    taken_from_savings, ended (busted | completed | '')."""
    start = d["start"]
    day = int(d["current_day"])
    stake = float(d.get("value") or stake_for(day, start))
    row = {"date": date, "day": day, "stake": r2(stake), "odds": None, "return": 0.0, "saved": 0.0,
           "savings_before": r2(d.get("savings") or 0), "outcome": status, "slip_id": slip_id, "code": code,
           "next_day": day, "restart_from": None, "taken_from_savings": 0.0, "ended": ""}
    sav = float(d.get("savings") or 0)
    if status in ("won", "void"):
        o = float(odds or (1.0 if status == "void" else TARGET))
        ret = r2(stake * o)
        row.update(odds=round(o, 2), **{"return": ret})
        if day >= DAYS and ret > stake + 0.005:
            # the end: the whole return is the winning
            d["won_total"] = ret
            d["current_day"] = DAYS + 1
            d["value"] = 0.0
            row.update(next_day=None, ended="completed", outcome="won")
        else:
            nxt = stake_for(day + 1, start) if day < DAYS else stake
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
        r = restart_day(day, sav, start)
        if r is None:
            row.update(next_day=None, ended="busted")
            d["current_day"], d["value"] = day, 0.0
        else:
            take = stake_for(r, start)
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
                f"{d['value']:,.0f} (next slip tomorrow ~09:00).")
    if row["outcome"] in ("void", "replay"):
        return (f"Ladder day {day} returned {row['return']:,.0f} (void legs, odds {row['odds']:.2f}): below the next "
                f"stake, so day {day} replays with stake {d['value']:,.0f}. Saved {row['saved']:,.0f}, savings now "
                f"{row['savings']:,.0f}.")
    if row["ended"] == "busted":
        return (f"Ladder day {day} LOST (stake {row['stake']:,.0f}). Savings {row['savings']:,.0f} can't cover the "
                f"day 1 stake {stake_for(1, d['start']):,.0f}, so the ladder ends here. You keep the savings "
                f"{row['savings']:,.0f}. Start a new ladder any time with /rollover ladder.")
    return (f"Ladder day {day} LOST (stake {row['stake']:,.0f}). Restarting from day {row['restart_from']}: stake "
            f"{row['taken_from_savings']:,.0f} taken from savings, savings now {row['savings']:,.0f}. "
            f"Restarts so far: {row['restarts']}. Next slip tomorrow ~09:00.")


# ---------------------------------------------------------------- parsing (commands + chat)
_AMOUNT = re.compile(r"(?<![\w.])(?:ngn|n|₦)?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|thousand|million)?(?![\w.])",
                     re.I)


def parse_amount(text: str) -> float | None:
    """'2k' -> 2000, '1.5k' -> 1500, '2,000' -> 2000, '₦5000' -> 5000. The '2' of '2 odds' is ignored."""
    t = re.sub(r"\b(?:2|two)\s*-?\s*odds?\b", " ", text or "", flags=re.I)
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
    """Chat intent for the ladder: {'action': start | plan | status, 'stake': float | None}, or None."""
    t = (text or "").strip()
    if not _LADDER.search(t):
        return None
    low = t.lower()
    stake = parse_amount(t)
    if re.search(r"\b(plan|table|template|breakdown|schedule)\b", low) and not re.search(
            r"\b(start|begin|launch|run|create|kick\s*off|set\s*up)\b", low):
        return {"action": "plan", "stake": stake}
    if re.search(r"\b(start|begin|launch|run|create|kick\s*off|set\s*up|open|new)\b", low):
        return {"action": "start", "stake": stake}
    return {"action": "status", "stake": stake}


# ---------------------------------------------------------------- texts
def plan_table_text(start: float | None = BASE, html: bool = True) -> str:
    """The full 16-day table scaled to the stake. Starts with the Rules line."""
    tot = totals(start)
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = [rules_line(), b(f"2-odds savings ladder · start {tot['start']:,.0f}"),
             "Day: stake -> return (save)"]
    for r in plan_rows(start):
        tail = "(end)" if r["save"] is None else f"(save {r['save']:,.0f})"
        lines.append(f"{r['day']}: {r['stake']:,.0f} -> {r['return']:,.0f} {tail}")
    fin, sav = f"{tot['final_return']:,.0f}", f"{tot['savings']:,.0f}"
    lines += [f"Total winning {b(fin)} · total savings {b(sav)} · {tot['days']} days at 2.00 odds a day",
              "A cut on day 3 or later restarts 2 days back with that stake taken from savings (day 1 or 2: back to "
              "day 1); if savings can't cover it, further back; if not even day 1, the ladder ends.",
              "Real odds are used for the money (1.90-2.15 a day), so savings move a little with them. Estimates, "
              "not guarantees. 18+."]
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
