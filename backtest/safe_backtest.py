"""Backtest of the Safe leg filter and of 2.00 ladder slips, old (est >= 80%, market only) vs new (safety.py).

Data: the same files as backtest.py (15 European leagues, 24/25 + 25/26 + 26/27 to late Oct 2026; Sportradar results,
football-data.co.uk pre-match 1X2 + O/U 2.5 market averages). Team stats as of kick-off = each team's last 10 games
inside the dataset. Lines without a historical price (O/U 0.5/1.5, team totals, DC, DNB, BTTS) are priced from the
Poisson fit to the 1X2 + O/U 2.5 book at a 6% margin, exactly like backtest.py; Asian handicaps aren't in the data.
National teams and cups aren't in the data either, so the league rule here is only "top-leagues allowlist"
(2. Bundesliga, Serie B, Ligue 2 etc. are out).

    python backtest/safe_backtest.py [DATA_DIR]         -> backtest/SAFE_REPORT.md + backtest/safe_metrics.json
Runs in chunks (cache in DATA_DIR/safe_items.pkl) so it fits short command windows: re-run until it says done.
"""
from __future__ import annotations

import json
import math
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt  # noqa: E402
import leagues  # noqa: E402
import model  # noqa: E402
import safety  # noqa: E402
from markets import FAMILIES, side_of  # noqa: E402
from scanner import search_combo  # noqa: E402
from srstats import H2H, MatchForm, TeamForm  # noqa: E402

TZ = ZoneInfo("Africa/Lagos")
OUT_MD = Path(__file__).resolve().parent / "SAFE_REPORT.md"
OUT_JSON = Path(__file__).resolve().parent / "safe_metrics.json"
CACHE = bt.DATA / "safe_items.pkl"
CHUNK = 20000


def build_rows():
    matches, _ = bt.load()
    hist = bt.build_history(matches)
    rows = []
    for m in matches:
        if not m["odds"]:
            continue
        hg, ag = bt.team_games(hist, m["huid"], m["uts"]), bt.team_games(hist, m["auid"], m["uts"])
        if len(hg) < 4 or len(ag) < 4:
            continue
        rows.append((m, hg, ag))
    return rows


def legs_for(m, hg, ag):
    lam_fit, priced = bt.market_parts(m)
    lam_new = model.stats_lambdas_v2(hg, ag, m["utid"], m["uts"])
    it = bt.item_probs(m, lam_fit, priced, None, lam_new, model.rho())
    form = MatchForm(TeamForm(str(m["huid"]), m["home"], hg), TeamForm(str(m["auid"]), m["away"], ag), H2H())
    out = []
    for name, fid, spec, oid, _ in bt.ITEMS:
        r = it.get(name)
        if r is None:
            continue
        fam = FAMILIES[fid]
        est = model.calibrate(model.blend(r["base"], r["ps_new"], r["group"]), r["group"],
                              market_only=r["ps_new"] is None)
        tags = ("under",) if oid == "13" else ("over",) if oid == "12" else ()
        if fam.kind == "team_total":
            tags = tags + ("team_totals",)
        if fam.period != "ft":
            tags = tags + ("halves",)
        side = side_of(fam, oid)
        verdict = "odds only" if r["ps_new"] is None else (
            "confirmed" if r["ps_new"] - r["base"] >= -0.03 else "demoted" if r["ps_new"] - r["base"] >= -0.10
            else "dropped")
        leg = {"event_id": m["id"] if "id" in m else f"{m['huid']}-{m['auid']}-{m['uts']}", "market_id": fid,
               "specifier": spec, "outcome_id": oid, "side": side, "tags": tags, "est": est, "odds": r["odds"],
               "p_stats": r["ps_new"], "p_fit": r["base"], "verdict": verdict, "y": r["y"], "name": name,
               "utid": m["utid"], "home": m["home"], "away": m["away"], "uts": m["uts"], "season": m["season_idx"],
               "priced": name in ("1X2 home", "1X2 draw", "1X2 away", "Over 2.5", "Under 2.5")}
        leg["safe_note"] = safety.leg_problem(leg, form)
        out.append(leg)
    return out


def old_ok(l) -> bool:      # production until 2026-11-14 (scanner.ladder_leg_ok / rollover, est >= 80%)
    return (l["verdict"] == "confirmed" and l["est"] >= 0.80 and 1.12 <= l["odds"] <= 2.15
            and "under" not in l["tags"] and "halves" not in l["tags"] and leagues.is_top_ids(l["utid"]))


def old_safe_scan(l) -> bool:   # Scanner Safe tier until 2026-11-14 (band 1.05-1.35, est >= 80%, not dropped)
    return 1.05 <= l["odds"] <= 1.35 and l["est"] >= 0.80 and l["verdict"] != "dropped" and \
        leagues.is_top_ids(l["utid"])


def new_ok(l, min_p=None, ladder=True) -> bool:
    """safety.py rules on a backtest leg (min_p = what-if bar; default the production 85%)."""
    min_p = safety.SAFE_MIN_P if min_p is None else min_p
    lo, hi = (1.12, 2.15) if ladder else (1.05, 1.35)
    return (lo <= l["odds"] <= hi and l["p_stats"] is not None and l["p_stats"] >= min_p and l["est"] >= min_p
            and l["verdict"] == "confirmed" and l["stats_rule"] == "" and "halves" not in l["tags"]
            and not safety.under_like(l) and leagues.is_top_ids(l["utid"]))


def slips(legs, ok, max_legs=4, prefer=True):
    """One 2.00 slip (1.90-2.15) per WAT day, same search as Scanner.ladder_slip. Returns [(day, won, odds, n)]."""
    by_day = defaultdict(list)
    for l in legs:
        if ok(l):
            by_day[datetime.fromtimestamp(l["uts"], TZ).date()].append(l)
    out = []
    for d, ls in sorted(by_day.items()):
        objs = [SimpleNamespace(**l, p_noloss=l["est"]) for l in ls]

        def banded(xs):
            best = {}
            for x in xs:
                b = 0 if x.odds < 1.2 else 1 if x.odds < 1.35 else 2 if x.odds < 1.55 else 3 if x.odds < 1.8 else 4
                k = (x.event_id, b)
                if k not in best or x.est > best[k].est:
                    best[k] = x
            return list(best.values())
        home = [x for x in objs if x.side != "away"]
        found = []
        if prefer:
            found = search_combo(banded([x for x in home if safety.preferred_market(x)]), 1.90, 2.15, max_legs)
        found = found or search_combo(banded(home), 1.90, 2.15, max_legs) or \
            search_combo(banded(objs), 1.90, 2.15, max_legs)
        if found:
            out.append((d.isoformat(), all(x.y == 1 for x in found), math.prod(x.odds for x in found), len(found),
                        [x.name for x in found]))
    return out


def summarize_legs(legs, ok):
    sel = [l for l in legs if ok(l)]
    n = len(sel)
    hit = sum(l["y"] for l in sel)
    return {"legs": n, "hit_rate": round(hit / n, 4) if n else None,
            "mean_est": round(sum(l["est"] for l in sel) / n, 4) if n else None,
            "mean_odds": round(sum(l["odds"] for l in sel) / n, 3) if n else None,
            "by_market": dict(sorted(((k, v) for k, v in _by(sel).items()), key=lambda kv: -kv[1]["n"])[:8])}


def _by(sel):
    g = defaultdict(lambda: [0, 0])
    for l in sel:
        g[l["name"]][0] += 1
        g[l["name"]][1] += l["y"]
    return {k: {"n": v[0], "hit": round(v[1] / v[0], 3)} for k, v in g.items()}


def summarize_slips(ss, days):
    n = len(ss)
    w = sum(1 for s in ss if s[1])
    return {"days": days, "slips": n, "won": w, "hit_rate": round(w / n, 4) if n else None,
            "days_held": days - n, "mean_odds": round(sum(s[2] for s in ss) / n, 3) if n else None,
            "mean_legs": round(sum(s[3] for s in ss) / n, 2) if n else None}


def main():
    t0 = time.time()
    state = pickle.loads(CACHE.read_bytes()) if CACHE.exists() else None
    if state is None:
        rows = build_rows()
        state = {"rows": rows, "done": 0, "legs": []}
        CACHE.write_bytes(pickle.dumps(state))
        print(f"rows {len(rows)} built in {time.time() - t0:.0f}s; re-run to score")
        return
    rows = state["rows"]
    if state["done"] < len(rows):
        end = min(len(rows), state["done"] + CHUNK)
        for m, hg, ag in rows[state["done"]:end]:
            form = MatchForm(TeamForm("h", m["home"], hg), TeamForm("a", m["away"], ag), H2H())
            for l in legs_for(m, hg, ag):
                l["stats_rule"] = safety.stats_problem(l, form)
                state["legs"].append(l)
            if time.time() - t0 > 95:
                end = rows.index((m, hg, ag)) + 1
                break
        state["done"] = end
        CACHE.write_bytes(pickle.dumps(state))
        print(f"scored {state['done']}/{len(rows)} matches ({time.time() - t0:.0f}s); re-run until done")
        if state["done"] < len(rows):
            return
    legs = state["legs"]
    res = {}
    for split, cond in (("test_2627", lambda l: l["season"] == 0), ("all_seasons", lambda l: True)):
        L = [l for l in legs if cond(l)]
        days = len({datetime.fromtimestamp(l["uts"], TZ).date() for l in L if leagues.is_top_ids(l["utid"])})
        r = {"matches": len({l["event_id"] for l in L}), "match_days_top": days}
        r["legs_old_ladder"] = summarize_legs(L, old_ok)
        r["legs_new_ladder"] = summarize_legs(L, lambda l: new_ok(l))
        r["legs_old_scan_safe"] = summarize_legs(L, old_safe_scan)
        r["legs_new_scan_safe"] = summarize_legs(L, lambda l: new_ok(l, ladder=False))
        so = slips(L, old_ok, prefer=False)
        sn = slips(L, lambda l: new_ok(l))
        sn6 = slips(L, lambda l: new_ok(l), max_legs=6)
        s80 = slips(L, lambda l: new_ok(l, 0.80))
        s10 = slips(L, lambda l: new_ok(l, ladder=False), max_legs=10)
        r["slips_old"] = summarize_slips(so, days)
        r["slips_new_4legs"] = summarize_slips(sn, days)
        r["slips_new_6legs_whatif"] = summarize_slips(sn6, days)
        r["slips_new_rules_at_80_whatif"] = summarize_slips(s80, days)
        r["slips_new_10legs_from_1.05_whatif"] = summarize_slips(s10, days)
        r["examples_new"] = [s for s in sn[:5]]
        res[split] = r
    OUT_JSON.write_text(json.dumps(res, indent=1, default=str))
    write_md(res)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk.startswith("slips") or kk.startswith("legs")}
                      for k, v in res.items()}, indent=1, default=str)[:6000])
    print(f"done in {time.time() - t0:.0f}s")


def write_md(res):
    L = ["# Safe filter backtest (2026-11-14)", "",
         "Old = production until 2026-11-14: est >= 80% (calibrated de-margined market), 'confirmed' = stats model not "
         ">3 pts below it, top-leagues allowlist, no unders/halves. New = safety.py: market est AND stats model each "
         ">= 85%, market-specific last-10 record rule, missing stats out, no unders/BTTS No, league quality. Ladder = "
         "legs priced 1.12-2.15, one 2.00 slip (1.90-2.15, max 4 legs) per WAT match day; Scan Safe = legs 1.05-1.35.",
         "", "Caveat: only 1X2 and O/U 2.5 have real historical prices; other lines are priced from the Poisson fit at a "
         "6% margin. No Asian handicaps, national teams or cups in the data.", ""]
    for split, r in res.items():
        L += [f"## {split} ({r['matches']} matches, {r['match_days_top']} top-league match days)", "",
              "| Legs | n | hit rate | mean est | mean odds |", "|---|---|---|---|---|"]
        for k in ("legs_old_ladder", "legs_new_ladder", "legs_old_scan_safe", "legs_new_scan_safe"):
            x = r[k]
            L.append(f"| {k[5:]} | {x['legs']} | {x['hit_rate']} | {x['mean_est']} | {x['mean_odds']} |")
        L += ["", "| 2.00 slips | slips | won | hit rate | days held | mean odds | mean legs |",
              "|---|---|---|---|---|---|---|"]
        for k in ("slips_old", "slips_new_4legs", "slips_new_6legs_whatif", "slips_new_rules_at_80_whatif",
                  "slips_new_10legs_from_1.05_whatif"):
            x = r[k]
            L.append(f"| {k[6:]} | {x['slips']} | {x['won']} | {x['hit_rate']} | {x['days_held']} | {x['mean_odds']} | "
                     f"{x['mean_legs']} |")
        L.append("")
    L.append("_Estimates, not guarantees. Past hit rates don't promise future ones._")
    OUT_MD.write_text("\n".join(L))


if __name__ == "__main__":
    main()
