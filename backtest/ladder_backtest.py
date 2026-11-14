"""Backtest of the savings ladder's daily code band (2026-11-14): 1.50-1.80 (new default) vs 2.00 (1.90-2.15),
old rules (est >= 80%, market only) vs new rules (safety.py), and simulated 16-day ladders on the real day sequence.

Uses the leg cache safe_backtest.py builds (DATA_DIR/safe_items.pkl; run that first). Same data caveats: only 1X2 and
O/U 2.5 have real historical prices, other lines are priced from the Poisson fit at a 6% margin; no Asian handicaps,
national teams or cups in the data.

    python backtest/ladder_backtest.py [DATA_DIR]       -> backtest/LADDER_REPORT.md + backtest/ladder_metrics.json
"""
from __future__ import annotations

import json
import math
import pickle
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_backtest as sbt  # noqa: E402
import ladder  # noqa: E402
import leagues  # noqa: E402
import safety  # noqa: E402
from scanner import ladder_banded as banded, search_combo  # noqa: E402

OUT_MD = Path(__file__).resolve().parent / "LADDER_REPORT.md"
OUT_JSON = Path(__file__).resolve().parent / "ladder_metrics.json"
OLD, NEW = (1.90, 2.15), (1.50, 1.80)


def old_ok(l, floor, hi):
    return (l["verdict"] == "confirmed" and l["est"] >= 0.80 and floor <= l["odds"] <= hi
            and "under" not in l["tags"] and "halves" not in l["tags"] and leagues.is_top_ids(l["utid"]))


def new_ok(l, floor, hi):
    return (floor <= l["odds"] <= hi and l["p_stats"] is not None and l["p_stats"] >= ladder.MIN_P
            and l["est"] >= ladder.MIN_P and l["verdict"] == "confirmed" and l["stats_rule"] == ""
            and "halves" not in l["tags"] and not safety.under_like(l) and leagues.is_top_ids(l["utid"]))


def day_slips(legs, ok, band, max_legs=4, prefer=True):
    """One code per WAT day, same search order as Scanner.ladder_slip. [(day, won, odds, n_legs, est, names)]."""
    lo, hi = band
    by_day = defaultdict(list)
    for l in legs:
        if ok(l):
            by_day[datetime.fromtimestamp(l["uts"], sbt.TZ).date()].append(l)
    out = []
    for d, ls in sorted(by_day.items()):
        objs = [SimpleNamespace(**l, p_noloss=l["est"]) for l in ls]
        home = [x for x in objs if x.side != "away"]
        found = []
        if prefer:
            found = search_combo(banded([x for x in home if safety.preferred_market(x)]), lo, hi, max_legs)
        found = found or search_combo(banded(home), lo, hi, max_legs) or search_combo(banded(objs), lo, hi, max_legs)
        if found:
            out.append((d.isoformat(), all(x.y == 1 for x in found), round(math.prod(x.odds for x in found), 3),
                        len(found), round(math.prod(x.est for x in found), 4), [x.name for x in found]))
    return out


def run_ladders(slips, band, start=1000.0):
    """Consecutive 16-day ladders on the real chronological sequence of codes (held days skipped). A new ladder
    starts on the next code after one completes or busts. Returns per-ladder results."""
    res, d = [], None
    for day, won, odds, *_ in slips:
        if d is None:
            d = ladder.new_state(start, band)
            d["first"] = day
        row = ladder.apply_result(d, "won" if won else "lost", odds if won else None, date=day)
        if row["ended"]:
            res.append({"from": d["first"], "to": day, "ended": row["ended"], "codes": len(d["ledger"]),
                        "restarts": d["restarts"], "savings": d["savings"], "winning": d.get("won_total") or 0.0,
                        "net": round(d["savings"] + (d.get("won_total") or 0.0) - start, 2)})
            d = None
    if d is not None:
        res.append({"from": d["first"], "to": slips[-1][0], "ended": "running", "codes": len(d["ledger"]),
                    "restarts": d["restarts"], "savings": d["savings"], "winning": 0.0, "day": d["current_day"],
                    "net": None})
    return res


def bootstrap(slips, band, n=20000, start=1000.0, seed=7):
    """Ladders run on codes drawn at random (with replacement) from the backtest's own codes: completion rate and
    mean savings when the ladder ends, a smoother read than the few real sequences."""
    if not slips:
        return None
    rnd = random.Random(seed)
    done = bust = 0
    sav_end, net, codes = [], [], []
    for _ in range(n):
        d = ladder.new_state(start, band)
        for _k in range(400):
            _, won, odds, *_ = rnd.choice(slips)
            row = ladder.apply_result(d, "won" if won else "lost", odds if won else None)
            if row["ended"]:
                break
        if row["ended"] == "completed":
            done += 1
        else:
            bust += 1
        sav_end.append(d["savings"])
        net.append(d["savings"] + (d.get("won_total") or 0.0) - start)
        codes.append(len(d["ledger"]))
    return {"runs": n, "completed_pct": round(100 * done / n, 2), "busted_pct": round(100 * bust / n, 2),
            "mean_savings_at_end": round(sum(sav_end) / n, 0), "mean_net": round(sum(net) / n, 0),
            "median_net": sorted(net)[n // 2], "mean_codes": round(sum(codes) / n, 1)}


def summarize(slips, days):
    n = len(slips)
    w = sum(1 for s in slips if s[1])
    return {"days": days, "codes": n, "days_held": days - n, "won": w, "hit_rate": round(w / n, 4) if n else None,
            "mean_odds": round(sum(s[2] for s in slips) / n, 3) if n else None,
            "mean_est": round(sum(s[4] for s in slips) / n, 4) if n else None,
            "mean_legs": round(sum(s[3] for s in slips) / n, 2) if n else None}


CONFIGS = [   # name, rules, band, leg floor, max legs
    ("old rules, 2.00 (1.90-2.15), legs >=1.12 [production until 14 Nov]", old_ok, OLD, 1.12, 4),
    ("new rules, 2.00 (1.90-2.15), legs >=1.12 [14 Nov safe change]", new_ok, OLD, 1.12, 4),
    ("new rules, 2.00 (1.90-2.15), legs >=1.05", new_ok, OLD, 1.05, 4),
    ("old rules, 1.50-1.80, legs >=1.12", old_ok, NEW, 1.12, 4),
    ("new rules, 1.50-1.80, legs >=1.12", new_ok, NEW, 1.12, 4),
    ("new rules, 1.50-1.80, legs >=1.08", new_ok, NEW, 1.08, 4),
    ("new rules, 1.50-1.80, legs >=1.05 [new default]", new_ok, NEW, 1.05, 4),
    ("new rules, 1.50-1.80, legs >=1.03", new_ok, NEW, 1.03, 4),
    ("what-if: new rules, 1.50-1.80, legs >=1.05, up to 6 legs", new_ok, NEW, 1.05, 6),
]


def main():
    state = pickle.loads(sbt.CACHE.read_bytes())
    legs = state["legs"]
    out = {}
    for split, cond in (("test_2627", lambda l: l["season"] == 0), ("all_seasons", lambda l: True)):
        L = [l for l in legs if cond(l)]
        days = len({datetime.fromtimestamp(l["uts"], sbt.TZ).date() for l in L if leagues.is_top_ids(l["utid"])})
        r = {"matches": len({l["event_id"] for l in L}), "match_days_top": days, "configs": {}}
        for name, rule, band, floor, mx in CONFIGS:
            ss = day_slips(L, lambda l, rule=rule, floor=floor, band=band: rule(l, floor, band[1]), band, mx)
            c = summarize(ss, days)
            if split == "all_seasons":
                lad = run_ladders(ss, band)
                fin = [x for x in lad if x["ended"] != "running"]
                c["real_sequence"] = {
                    "ladders": len(lad), "completed": sum(1 for x in lad if x["ended"] == "completed"),
                    "busted": sum(1 for x in lad if x["ended"] == "busted"),
                    "running_at_end": sum(1 for x in lad if x["ended"] == "running"),
                    "mean_savings_at_end": round(sum(x["savings"] for x in fin) / len(fin), 0) if fin else None,
                    "mean_net": round(sum(x["net"] for x in fin) / len(fin), 0) if fin else None,
                    "ladders_detail": lad[:12]}
                c["bootstrap"] = bootstrap(ss, band)
                c["examples"] = ss[:6]
            r["configs"][name] = c
        out[split] = r
    OUT_JSON.write_text(json.dumps(out, indent=1, default=str))
    write_md(out)
    print(OUT_MD.read_text())


def write_md(out):
    t = ladder.totals(1000, NEW)
    L = ["# Savings ladder band backtest (2026-11-14)", "",
         "Leg banding: strongest leg per match per odds band, bands split at 1.05/1.08/1.11/1.15/1.20/1.35/1.55/1.80 "
         "(scanner.ladder_banded, also used by production since 14 Nov). Daily code: 1-4 legs (one per match), highest combined estimate inside the band, same search order as "
         "Scanner.ladder_slip (home/neutral preferred markets, then home/neutral, then any side). Old rules = est >= "
         "80% (market only, production until 14 Nov). New rules = safety.py (market est AND stats model each >= 85%, "
         "last-10 record rule, no unders). Ladders: start 1,000, the band's 16-day template "
         f"(1.50-1.80: {t['final_return']:,.0f} winning + {t['savings']:,.0f} savings if all 16 days win; 2.00: "
         "1,000,000 + 568,500), real odds, restart 2 days back from savings, ends when day 1 can't be covered.", "",
         "Caveat: only 1X2 and O/U 2.5 have real historical prices; other lines priced from the Poisson fit at a 6% "
         "margin. No Asian handicaps, national teams or cups in the data. Held days are skipped (no money moves).",
         ""]
    for split, r in out.items():
        L += [f"## {split} ({r['matches']} matches, {r['match_days_top']} top-league match days)", "",
              "| Config | codes | days held | won | hit rate | mean odds | mean est | mean legs |",
              "|---|---|---|---|---|---|---|---|"]
        for k, c in r["configs"].items():
            L.append(f"| {k} | {c['codes']} | {c['days_held']} | {c['won']} | {c['hit_rate']} | {c['mean_odds']} | "
                     f"{c['mean_est']} | {c['mean_legs']} |")
        if split == "all_seasons":
            L += ["", "Ladders (start 1,000). Real sequence = the codes in date order; bootstrap = 20,000 ladders on "
                  "codes drawn at random from the same codes. With fewer than ~30 codes the bootstrap only repeats a "
                  "handful of results and says little (the new-rules 1.50-1.80 rows rest on 6 codes).", "",
                  "| Config | real: ladders | completed | busted | running | mean savings at end | mean net | "
                  "bootstrap: completed % | mean savings at end | mean net | median net | codes per ladder |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for k, c in r["configs"].items():
                rs, bs = c["real_sequence"], c["bootstrap"] or {}
                L.append(f"| {k} | {rs['ladders']} | {rs['completed']} | {rs['busted']} | {rs['running_at_end']} | "
                         f"{rs['mean_savings_at_end']} | {rs['mean_net']} | {bs.get('completed_pct')} | "
                         f"{bs.get('mean_savings_at_end')} | {bs.get('mean_net')} | {bs.get('median_net')} | "
                         f"{bs.get('mean_codes')} |")
        L.append("")
    L.append("_Estimates, not guarantees. Past hit rates don't promise future ones._")
    OUT_MD.write_text("\n".join(L))


if __name__ == "__main__":
    main()
