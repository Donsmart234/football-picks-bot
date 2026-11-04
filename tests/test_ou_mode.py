"""Overs only / Unders only mode with Asian goal lines (offline: no network, no Gemini, no booking, no messages).

    python tests/test_ou_mode.py

Covers: the goals-direction rule (constraints.py), Asian line mapping from SportyBet's plain Over/Under markets
(whole-number specifiers total=3 -> "Asian Under 3", quarter total=2.25) and push-aware pricing (est = full win only,
push = stake back), 1st/2nd-half and time-window exclusion, the Safe bar (>= 80%) in these modes, no padding with
other markets, natural-language parsing, settlement of pushes, the Telegram buttons / "Rules:" line and the Mini App
API field.
"""
import asyncio
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import markets  # noqa: E402
from bookmakers import Event  # noqa: E402
from constraints import TZ, Constraints, market_tags, parse_direction, parse_text  # noqa: E402
from scanner import Leg, Pool, Scanner, apply_stats  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)


NOW = datetime.now(timezone.utc)
KO = NOW + timedelta(hours=3)


def ev(i=1, tid=17, home="Arsenal", away="Everton", cat="England", cid=1, lg="Premier League"):
    return Event(event_id=f"sr:match:{i}", home=home, away=away, start=KO, league=lg, category=cat,
                 tournament_id=f"sr:tournament:{tid}", category_id=f"sr:category:{cid}", markets={}, market_count=900)


def o(oid, desc, odds, prob=None):
    return {"id": oid, "desc": desc, "odds": odds, "active": True, "prob": prob}


def m(mid, spec, desc, outs, group="Main"):
    return {"id": mid, "specifier": spec, "desc": desc, "group": group, "source": "", "outcomes": outs}


# a realistic SportyBet event detail (prices shaped like Rennes - Olympiacos / Cardiff - West Ham on 2026-11-04)
MARKETS = [
    m("1", "", "1X2", [o("1", "Home", 1.85), o("2", "Draw", 3.60), o("3", "Away", 4.20)]),
    m("18", "total=2.5", "Over/Under", [o("12", "Over 2.5", 1.80, 0.53), o("13", "Under 2.5", 2.05, 0.47)]),
    m("18", "total=1.5", "Over/Under", [o("12", "Over 1.5", 1.25), o("13", "Under 1.5", 4.00)]),
    m("18", "total=4.5", "Over/Under", [o("12", "Over 4.5", 5.50), o("13", "Under 4.5", 1.15)]),
    m("18", "total=3", "Over/Under", [o("12", "Over 3", 2.35, 0.4036), o("13", "Under 3", 1.61, 0.5964)]),
    m("18", "total=5", "Over/Under", [o("12", "Over 5", 9.60), o("13", "Under 5", 1.06)]),
    m("18", "total=1", "Over/Under", [o("12", "Over 1", 1.06), o("13", "Under 1", 9.30)]),
    m("18", "total=2.25", "Over/Under", [o("12", "Over 2.25", 1.62), o("13", "Under 2.25", 2.30)]),
    m("19", "total=1.5", "Arsenal Over/Under", [o("12", "Over 1.5", 2.10), o("13", "Under 1.5", 1.69)], "Goals"),
    m("20", "total=2.5", "Everton Over/Under", [o("12", "Over 2.5", 5.50), o("13", "Under 2.5", 1.13)], "Goals"),
    m("68", "total=1.5", "1st Half - Over/Under", [o("12", "Over 1.5", 2.75), o("13", "Under 1.5", 1.42)], "Half"),
    m("68", "total=1", "1st Half - Over/Under", [o("12", "Over 1", 1.65), o("13", "Under 1", 2.20)], "Half"),
    m("60180", "minsnr=10|total=1.5", "Over/Under - Early Goals",
      [o("12", "Over 1.5", 1.20), o("13", "Under 1.5", 4.50)]),
    m("900313", "minute=60|total=2.5", "Total Goals Over/Under from 1 to 60 minute",
      [o("12", "Over 2.5", 3.20), o("13", "Under 2.5", 1.30)], "Minutes"),
    m("166", "total=7.5", "Corners - Over/Under", [o("12", "Over 7.5", 1.32), o("13", "Under 7.5", 3.30)], "Corners"),
    m("37", "total=4.5", "1X2 & Over/Under 4.5", [o("1", "Home & Under 4.5", 2.10), o("2", "Draw & Under 4.5", 4.0)],
      "Combo"),
]


def test_markets():
    print("Asian line maths (markets.py)")
    f = markets.FAMILIES["18"]
    check([markets.line_kind(f, s) for s in ("total=3", "total=2.5", "total=2.25", "total=2.75", "total=0.5")]
          == ["whole", "half", "quarter", "quarter", "half"], "line kinds")
    check(markets.asian_line(f, "total=3") and not markets.asian_line(f, "total=2.5"), "asian_line")
    lh, la = 1.5, 1.2
    p = markets.outcome_parts(f, "total=3", "13", lh, la)
    check(abs(sum(p.values()) - 1) < 1e-9, f"parts sum to 1 ({sum(p.values())})")
    g = markets.Grid(lh, la)
    d = g.goals_dist()
    check(abs(p["push"] - d[3]) < 1e-9, "Under 3: push = P(exactly 3 goals)")
    check(abs(p["win"] - sum(d[:3])) < 1e-9, "Under 3: full win = P(0-2 goals), push not counted")
    check(abs(markets.model_prob(f, "total=3", "13", lh, la) - p["win"]) < 1e-12, "model_prob = full-win prob")
    check(abs(markets.model_prob(f, "total=3", "13", lh, la) - markets.model_prob(f, "total=2.5", "13", lh, la))
          < 1e-9, "Under 3 wins exactly when Under 2.5 wins (the extra case is a push, not a win)")
    po = markets.outcome_parts(f, "total=3", "12", lh, la)
    check(abs(po["win"] - sum(d[4:])) < 1e-9 and abs(po["push"] - d[3]) < 1e-9, "Over 3: win >= 4, push = 3")
    q = markets.outcome_parts(f, "total=2.25", "12", lh, la)
    check(abs(q["half_loss"] - d[2]) < 1e-9 and abs(q["win"] - sum(d[3:])) < 1e-9 and q["push"] == 0,
          "Over 2.25: 3+ full win, exactly 2 = half loss (half the stake back)")
    q = markets.outcome_parts(f, "total=2.75", "13", lh, la)
    check(abs(q["half_loss"] - d[3]) < 1e-9 and abs(q["win"] - sum(d[:3])) < 1e-9, "Under 2.75: 3 = half loss")
    q = markets.outcome_parts(f, "total=2.75", "12", lh, la)
    check(abs(q["half_win"] - d[3]) < 1e-9, "Over 2.75: exactly 3 = half win")
    t = markets.FAMILIES["19"]
    pt = markets.outcome_parts(t, "total=1", "12", lh, la)
    dh = g.goals_dist("home")
    check(abs(pt["push"] - dh[1]) < 1e-9 and abs(pt["win"] - sum(dh[2:])) < 1e-9, "team total Over 1: home goals")
    check(markets.pretty_pick(f, "Under 3", "A", "B", "total=3") == "Asian Under 3", "label Asian Under 3")
    check(markets.pretty_pick(f, "Under 2.5", "A", "B", "total=2.5") == "Under 2.5", "x.5 label unchanged")
    check(markets.pretty_pick(t, "Over 1", "A", "B", "total=1") == "A Asian Over 1", "team Asian label")
    check(markets.market_label(f, "total=3", "Over/Under") == "Asian Over/Under", "market label")


def scan_legs():
    sc = Scanner(None, None)
    e = ev()
    legs = sc.event_legs(e, MARKETS)
    for l in legs:
        apply_stats(l, None)          # no stats feed: odds-only path (est = calibrated base - 2 pts)
    return {(l.market_id, l.specifier, l.outcome_id): l for l in legs}


def test_scanner_mapping():
    print("Scanner: Asian lines mapped and priced push-aware")
    L = scan_legs()
    u3 = L.get(("18", "total=3", "13"))
    check(u3 is not None, "Under 3 (whole line) becomes a leg")
    if u3 is None:
        return
    check(u3.pick == "Asian Under 3" and u3.market == "Asian Over/Under" and u3.line_type == "asian",
          f"labelled Asian Under 3 ({u3.pick} / {u3.market} / {u3.line_type})")
    check("asian" in u3.tags and "under" in u3.tags and "totals" in u3.tags, f"tags {u3.tags}")
    check(0.05 < u3.p_push < 0.4, f"push chance carried ({u3.p_push:.3f})")
    cond = (1 / 1.61) / (1 / 2.35 + 1 / 1.61)       # de-margined win-vs-loss price (pushes taken out)
    check(u3.p_market < cond - 0.05, f"market prob converted to a full-win prob ({u3.p_market:.3f} < {cond:.3f})")
    check(u3.est < cond and abs(u3.p_noloss - (u3.est + u3.p_push)) < 1e-9, "est counts full wins only")
    check(u3.p_fit is not None and u3.level == "model", "Asian line priced by the goals model")
    check("push" in u3.why and "stake back" in u3.why, f"why explains the push ({u3.why[-90:]})")
    u25 = L.get(("18", "total=2.5", "13"))
    check(u25 is not None and abs(u3.p_fit - u25.p_fit) < 1e-9, "model: Under 3 full win == Under 2.5 win")
    q = L.get(("18", "total=2.25", "12"))
    check(q is not None and q.line_type == "asian_quarter" and q.pick == "Asian Over 2.25" and q.p_half_loss > 0,
          "quarter line mapped with a half-loss chance")
    o1 = L.get(("18", "total=1", "12"))
    check(o1 is not None and o1.pick == "Asian Over 1" and o1.est < 0.95, "Asian Over 1 mapped")
    h1 = L.get(("68", "total=1", "12"))
    check(h1 is not None and "halves" in h1.tags, "1st-half whole line tagged halves")
    eg = L.get(("60180", "minsnr=10|total=1.5", "12"))
    check(eg is not None and "minutes" in eg.tags, f"Early Goals tagged minutes ({eg.tags if eg else None})")
    mn = L.get(("900313", "minute=60|total=2.5", "13"))
    check(mn is not None and "minutes" in mn.tags, "1-60 minute market tagged minutes")
    j = u3.to_json()
    check(all(k in j for k in ("p_push", "p_noloss", "line_type")), "leg json carries push fields")


def test_direction_filter():
    print("Direction filter (constraints.py)")
    L = scan_legs()
    for l in L.values():
        l.est = 0.9                     # isolate the market-type rule from the probability bar
    under, over = Constraints().set_direction("under"), Constraints().set_direction("over")
    ok_u = {k for k, l in L.items() if under.leg_ok(l)}
    ok_o = {k for k, l in L.items() if over.leg_ok(l)}
    want_u = {("18", "total=2.5", "13"), ("18", "total=1.5", "13"), ("18", "total=4.5", "13"), ("18", "total=3", "13"),
              ("18", "total=5", "13"), ("18", "total=1", "13"), ("18", "total=2.25", "13"), ("19", "total=1.5", "13"),
              ("20", "total=2.5", "13")}
    present_u = {k for k in want_u if k in L}
    check(ok_u == present_u, f"Unders only = every full-match/team Under incl. Asian; got extra "
                             f"{sorted(ok_u - present_u)} missing {sorted(present_u - ok_u)}")
    check(all(k[2] == "12" for k in ok_o) and ("18", "total=3", "12") in ok_o and ("19", "total=1.5", "12") in ok_o,
          "Overs only = Over lines incl. Asian and team totals")
    for bad in (("68", "total=1.5", "13"), ("68", "total=1", "13"), ("60180", "minsnr=10|total=1.5", "13"),
                ("900313", "minute=60|total=2.5", "13"), ("166", "total=7.5", "13"), ("37", "total=4.5", "1")):
        if bad in L:
            check(not under.leg_ok(L[bad]), f"{bad} excluded from Unders only ({under.leg_problem(L[bad])})")
    inc = Constraints(include_markets=["halves"]).set_direction("under")
    check(inc.leg_ok(L[("68", "total=1.5", "13")]), "halves allowed when the user asks for them")
    asian_only = Constraints(include_markets=["asian"]).set_direction("under")
    got = {k for k, l in L.items() if asian_only.leg_ok(l)}
    check(got == {("18", "total=3", "13"), ("18", "total=5", "13"), ("18", "total=1", "13"),
                  ("18", "total=2.25", "13")}, f"Asian only: {sorted(got)}")
    # an Unders mode lifts a stale 'no unders'; the opposite direction is never let through
    c = Constraints(exclude_markets=["under", "corners"]).set_direction("under")
    check(c.exclude_markets == ["corners"], "unders mode drops a 'no unders' exclusion")
    check(Constraints().leg_ok({"pick": "Under 2.5", "market": "Over/Under", "market_id": "18", "est": 0.5,
                                "odds": 1.9}), "no mode: unders and any estimate pass the plain rules as before")


def test_safe_bar():
    print("Safe bar in O/U modes")
    c = Constraints().set_direction("under")
    leg = {"market_id": "18", "market": "Over/Under", "pick": "Under 4.5", "specifier": "total=4.5", "est": 0.79,
           "odds": 1.2, "league": "England · Premier League"}
    check("below 80%" in c.leg_problem(leg), f"79% rejected without any tier ({c.leg_problem(leg)})")
    leg["est"] = 0.80
    check(c.leg_ok(leg), "80% accepted")
    leg["est"] = 0.85
    check("below 90%" in c.leg_problem(leg, 0.9), "a stricter user bar still applies")
    # Asian line: the bar is on the FULL-win est, not est + push
    a = {"market_id": "18", "market": "Asian Over/Under", "pick": "Asian Under 3", "specifier": "total=3",
         "est": 0.74, "p_push": 0.2, "odds": 1.3}
    check(not c.leg_ok(a), "Asian line with 74% full win + 20% push is not Safe")


# ------------------------------------------------------------------ scanner end-to-end (fake pool)
def _mk(e, mid, spec, market, pick, odds, est, group="Main", push=0.0, line=""):
    l = Leg(event_id=e.event_id, match=e.name, home=e.home, away=e.away, league=e.league_full,
            kickoff=e.start.isoformat(), kickoff_local=e.start.astimezone(TZ).strftime("%a %d %b %H:%M"),
            market_id=mid, specifier=spec, outcome_id="13" if "Under" in pick else "12", market=market, pick=pick,
            odds=odds, p_market=est, p_feed=None, p_fit=est, p_stats=est, base=est, est=est, verdict="confirmed",
            group=group, category=e.category, utid="17", category_id=e.category_id, line_type=line, p_push=push)
    l.tags = market_tags(mid, market, pick, group, spec, e.home, e.away)
    return l


def fake_scanner(n_matches=6, strong_unders=2):
    evs = [ev(i, home=f"Home{i}", away=f"Away{i}") for i in range(1, n_matches + 1)]
    sc = Scanner(None, None)

    async def fake_pool(day="today", leagues=None, cap=80, rules=None, fresh=False):
        p = Pool(created=time.time(), day=day, events=evs)
        for k, e in enumerate(evs):
            legs = [_mk(e, "10", "", "Double Chance", "Home or Draw", 1.20, 0.92),          # strongest, wrong market
                    _mk(e, "68", "total=1.5", "1st Half - Over/Under", "Under 1.5", 1.18, 0.90, "Half"),
                    _mk(e, "60180", "minsnr=10|total=1.5", "Over/Under - Early Goals", "Over 1.5", 1.15, 0.91),
                    _mk(e, "166", "total=11.5", "Corners - Over/Under", "Under 11.5", 1.15, 0.90, "Corners"),
                    _mk(e, "18", "total=1.5", "Over/Under", "Over 1.5", 1.22, 0.84)]
            if k < strong_unders:
                legs.append(_mk(e, "18", "total=5", "Asian Over/Under", "Asian Under 5", 1.08, 0.84, push=0.09,
                                line="asian"))
                legs.append(_mk(e, "18", "total=4.5", "Over/Under", "Under 4.5", 1.17, 0.84))
            else:
                legs.append(_mk(e, "18", "total=3.5", "Over/Under", "Under 3.5", 1.30, 0.70))  # not safe
            p.legs[e.event_id] = legs
        p.scanned = len(evs)
        return p
    sc.pool = fake_pool
    return sc


def test_scan_modes():
    print("Scanner: Unders only / Overs only slips")
    sc = fake_scanner()
    c = parse_text("safe unders slip 4 legs")
    r = asyncio.run(sc.scan("safe", None, "today", None, c))
    legs = r["legs"]
    check(len(legs) == 2, f"only the 2 safe unders, never padded to 4 ({len(legs)})")
    check(all("under" in l["tags"] and "totals" in l["tags"] for l in legs), "every leg is a goals Under")
    check(all(l["est"] >= 0.80 for l in legs), "every leg >= 80%")
    check(any(l["pick"] == "Asian Under 5" for l in legs), "Asian Under chosen when its no-loss chance is higher")
    check("didn't pad" in r["note"] and "you asked for 4" in r["note"], f"says so plainly: {r['note']}")
    check(r["constraint_check"]["ok"] is False and r["constraint_check"]["violations"] == ["2 legs < min 4"],
          f"only the leg-count shortfall is flagged ({r['constraint_check']['violations']})")
    check(r["constraints_text"].startswith("Unders only"), f"mode in rules text ({r['constraints_text']})")
    r = asyncio.run(sc.scan("risk", None, "today", None, Constraints().set_direction("under")))
    check(r["tier"] == "safe" and all(l["est"] >= 0.80 for l in r["legs"]), "a riskier tier is lifted to Safe")
    r = asyncio.run(sc.scan("safe", 2.0, "today", None, parse_text("overs only")))
    check(r["legs"] and all(l["pick"] == "Over 1.5" and l["market_id"] == "18" for l in r["legs"]),
          "Overs only: full-match Over lines, no Early Goals / half / corners / DC")
    r = asyncio.run(sc.scan("safe", 2.0, "today", None, parse_text("asian unders only")))
    check(r["legs"] and all(l["pick"].startswith("Asian Under") for l in r["legs"]), "Asian unders only")
    r = asyncio.run(sc.scan("safe", 2.0, "today", None, None))
    check(any(l["market_id"] == "10" for l in r["legs"]), "no mode: the normal scan is unchanged")


def test_parse():
    print("Natural language")
    cases = {"safe unders slip 4 legs": "under", "asian unders only": "under", "/scan overs": "over",
             "give me only overs": "over", "even asian under": "under", "unders only abeg": "under",
             "i want over games today": "over", "no unders": None, "sure 5 odds without under selections": None,
             "change leg 3 to over 1.5": None, "over/under games": None, "overs and unders": None,
             "risky 20 odds only overs and btts": None}
    for txt, want in cases.items():
        got = parse_text(txt).direction
        check(got == want, f"{txt!r} -> {got} (want {want})")
    c = parse_text("safe unders slip 4 legs")
    check(c.min_legs == 4 and c.max_legs == 4 and c.tier == "safe", "legs + tier still parsed")
    check("asian" in parse_text("asian unders only").include_markets, "'asian ... only' -> Asian lines only")
    check("asian" not in parse_text("safe unders incl asian").include_markets, "Asian not exclusive by default")
    check(parse_direction("only under 2.5 for leg 2") is None, "a single line is not a mode")
    # the model forgets the mode: the user's words win
    model = Constraints.from_dict({"tier": "safe", "include_markets": ["over"]})
    model.merge_hard(parse_text("safe unders slip"))
    check(model.direction == "under" and "over" not in model.include_markets, "merge_hard: user direction wins")
    check(Constraints.from_dict({"goals_direction": "over"}).direction == "over", "tool arg goals_direction")
    check(Constraints.from_dict({"goals_direction": "any"}).direction is None, "goals_direction any = no mode")
    rl = parse_text("unders only").rules_line()
    check(rl.startswith("Rules: Unders only") and "80%" in rl and "full match" in rl, f"rules line {rl}")


def test_settle():
    print("Settlement of Asian lines")
    from settle import _goals_family, early_settle, MatchResult
    leg = {"market_id": "18", "specifier": "total=3", "outcome_id": "13"}
    check(_goals_family(leg, 2, 1) == "void", "Under 3 at 2-1: push -> void (stake back)")
    check(_goals_family(leg, 1, 1) == "won", "Under 3 at 1-1: won")
    check(_goals_family(leg, 3, 1) == "lost", "Under 3 at 3-1: lost")
    q = {"market_id": "18", "specifier": "total=2.25", "outcome_id": "12"}
    check(_goals_family(q, 1, 1) == "check", "quarter line half result -> manual check, never a wrong call")
    r = MatchResult(state="live", live=(2, 1), phase="2h", minute=60)
    check(early_settle({"market_id": "18", "specifier": "total=2.75", "outcome_id": "13"}, r) is None,
          "Under 2.75 at 3 goals is not lost yet (could be a half loss)")


def test_surfaces():
    print("Telegram buttons, Rules line, Mini App field")
    import handlers
    res = {"tier": "safe", "label": "Safe", "target": 5.0, "day": "today", "legs": [], "total_odds": 1.0,
           "est_prob": 0.0, "note": "0 safe Under legs qualify", "rules": {"min_p": 0.8},
           "constraints": {"direction": "under"}, "constraints_text": "Unders only (incl. Asian lines) · Safe, every "
                                                                      "leg ≥80% · full match",
           "constraint_check": {"ok": True}}
    txt = handlers.format_slip(res)
    check(txt.startswith("<b>Rules:</b> Unders only"), f"slip starts with Rules: ({txt[:60]})")
    check("UNDERS ONLY" in txt, "mode in the slip title")
    kb = handlers._scan_kb("abc123", res)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("Overs only" in labels and "Any market" in labels and "Unders only" not in labels,
          f"mode buttons on an Unders slip ({labels})")
    check("SR|abc123|o" in datas and "SR|abc123|a" in datas, "callbacks re-run the same slip")
    plain = dict(res, constraints=None, constraints_text="")
    labels = [b.text for row in handlers._scan_kb("abc123", plain).inline_keyboard for b in row]
    check("Overs only" in labels and "Unders only" in labels, "plain safe slip offers both modes")
    check(handlers.format_slip(plain).startswith("<b>Rules:</b> Safe"), "a plain slip has a Rules line too")
    check("/scan unders" in handlers.HELP and "/scan overs" in handlers.HELP, "help lists the commands")
    js = open(os.path.join(os.path.dirname(__file__), "..", "webapp_static", "app.js"), encoding="utf-8").read()
    check("Overs only" in js and "Unders only" in js and "state.scanReq.direction" in js, "Mini App selector")
    src = open(os.path.join(os.path.dirname(__file__), "..", "webapp.py"), encoding="utf-8").read()
    check('c.set_direction(data.get("direction"))' in src, "/api/scan reads direction")


if __name__ == "__main__":
    for f in (test_markets, test_scanner_mapping, test_direction_filter, test_safe_bar, test_scan_modes, test_parse,
              test_settle, test_surfaces):
        f()
        print("ok" if not FAILS else "..", f.__name__)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'}")
    sys.exit(1 if FAILS else 0)
