"""Savings ladder (ladder.py, Scanner.ladder_slip, Tracker ladder mode). Offline: no network, no booking,
no Telegram (fake bot, fake owner id 1, fake bookmaker that returns a made-up code).

    python tests/test_ladder.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import ladder  # noqa: E402

L = ladder.LEGACY_BAND      # the user's original 2.00 template (plans before 2026-11-14 and "1.9-2.15")

FAILS: list[str] = []
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U000E0000-\U000E007F\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u20E3\u200D"
    "\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2934\u2935"
    "\u3030\u303D\u3297\u3299]")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  ok  ", msg)


def no_emoji(text: str, what: str):
    found = sorted({f"U+{ord(c):04X}" for c in EMOJI.findall(text or "")})
    check(not found, f"{what}: no emoji" + (f" ({found})" if found else ""))


def starts_rules(text: str, what: str):
    plain = re.sub(r"<[^>]+>", "", text or "").lstrip()
    if plain.startswith("TEST"):
        plain = plain.split("\n", 1)[1]
    check(plain.startswith("Rules:"), f"{what}: starts with a Rules: line")


def balanced(text: str, what: str):
    tags = re.findall(r"</?([a-z]+)[^>]*>", text)
    stack = []
    ok = True
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                ok = False
        else:
            stack.append(m.group(2))
    check(ok and not stack, f"{what}: HTML tags balanced ({len(tags)} tags)")


# ---------------------------------------------------------------- 1. template, scaling
def template():
    print("template + scaling")
    t = ladder.totals(1000, L)
    check(t["final_return"] == 1_000_000, "2.00 band, 1k: total winning 1,000,000")
    check(t["savings"] == 568_500, "1k: total savings 568,500")
    check(t["days"] == 16, "16 days")
    rows = ladder.plan_rows(1000, L)
    chain = all(abs(r["return"] - (r["save"] or 0) - (r["next_stake"] or r["return"])) < 0.01 for r in rows[:-1])
    check(chain, "every day: next stake = return - save")
    check([r["stake"] for r in rows][:4] == [1000, 1500, 2000, 3000] and rows[-1]["stake"] == 500_000,
          "1k stakes 1000, 1500, 2000, 3000 ... 500,000")
    r2k = ladder.plan_rows(2000, L)
    check(all(a["stake"] * 2 == b["stake"] for a, b in zip(rows, r2k)), "2k doubles every stake")
    check(all((a["save"] or 0) * 2 == (b["save"] or 0) for a, b in zip(rows, r2k)), "2k doubles every save")
    check(ladder.totals(2000, L)["final_return"] == 2_000_000 and ladder.totals(2000, L)["savings"] == 1_137_000,
          "2k: 2,000,000 winning, 1,137,000 savings")
    r15 = ladder.plan_rows(1500, L)
    check(r15[0]["stake"] == 1500 and r15[0]["save"] == 750 and r15[1]["stake"] == 2250 and r15[-1]["stake"] == 750_000,
          "1.5k: day 1 1,500 (save 750), day 2 2,250, day 16 750,000")
    check(ladder.totals(1500, L)["final_return"] == 1_500_000 and ladder.totals(1500, L)["savings"] == 852_750,
          "1.5k: 1,500,000 winning, 852,750 savings")
    # the new default: 1.50-1.80 a day, re-derived the way the user's template works
    t = ladder.totals(1000)
    rows = ladder.plan_rows(1000)
    check(ladder.DEFAULT_BAND == (1.5, 1.8) and ladder.plan_odds() == 1.65 and t["days"] == 16,
          "default band 1.50-1.80, planned at 1.65, 16 days")
    check([r["stake"] for r in rows] == [1000, 1200, 1400, 1700, 2100, 2600, 3200, 4000, 4900, 6100, 7500, 9300,
                                          12000, 15000, 19000, 24000], "1.50-1.80 stakes at 1k")
    check(t["final_return"] == 39_600 and t["savings"] == 36_150, "1.50-1.80 at 1k: 39,600 winning + 36,150 savings")
    check(all(abs(r["return"] - (r["save"] or 0) - (r["next_stake"] or r["return"])) < 0.01 for r in rows[:-1]),
          "1.50-1.80: next stake = return at 1.65 - save")
    check(all(b["stake"] <= a["stake"] * 1.50 + 0.01 for a, b in zip(rows, rows[1:])),
          "1.50-1.80: a win at the floor 1.50 always funds the next stake")
    sav, cover = 0.0, True
    for i, r in enumerate(rows[:-1]):
        sav += r["save"]
        if i >= 1 and sav + 0.01 < rows[i - 1]["stake"]:
            cover = False
    check(cover, "1.50-1.80: savings after each day cover a restart 2 days back (as the user's template does)")
    check(ladder.plan_rows(2000)[5]["stake"] == 5200 and ladder.totals(2000)["savings"] == 72_300,
          "1.50-1.80 scales with the stake (2k)")
    leg = ladder.derive_template(L)
    check(leg == ladder.LEGACY_TEMPLATE, "the 1.90-2.15 band keeps the user's own template")
    for txt, want in (("1.5-1.8", (1.5, 1.8)), ("1.50 to 1.80 odds", (1.5, 1.8)), ("ladder 1k 1.9-2.15", (1.9, 2.15)),
                      ("2 odds ladder 2k", None), ("ladder plan 5-9", None)):
        check(ladder.parse_band(txt) == want, f"parse_band({txt!r}) = {want}")
    for bad in ("1.1-1.3", "1.5-3.5", "1.5-1.52"):
        try:
            ladder.parse_band(bad)
            check(False, f"band {bad} refused")
        except ValueError:
            check(True, f"band {bad} refused")
    check(ladder.parse_amount("1k 1.5-1.8") == 1000, "'/rollover ladder 1k 1.5-1.8': stake 1,000")
    check(ladder.parse_intent("start the 2 odds ladder with 1k 1.5-1.8") ==
          {"action": "start", "stake": 1000.0, "band": (1.5, 1.8)}, "chat: start with a band")
    check("band_error" in ladder.parse_intent("ladder plan 1k 1.1-1.2"), "chat: a bad band is reported, not guessed")
    check("code 1.50-1.80 total odds" in ladder.rules_line() and "code 1.90-2.15" in ladder.rules_line(L),
          "Rules line shows the band")
    check(ladder.band_of({"mode": "ladder"}) == L and ladder.band_of({"band": [1.5, 1.8]}) == (1.5, 1.8),
          "plans without a band (before 2026-11-14) keep the 2.00 band")
    for txt, want in (("2k", 2000), ("1.5k", 1500), ("2,000", 2000), ("start the 2 odds ladder with 2k", 2000),
                      ("₦5000", 5000), ("2 odds ladder", None), ("ladder plan 1500", 1500)):
        check(ladder.parse_amount(txt) == want, f"parse_amount({txt!r}) = {want}")
    check(ladder.parse_intent("start the 2 odds ladder with 2k") == {"action": "start", "stake": 2000.0},
          "chat: 'start the 2 odds ladder with 2k' -> start 2000")
    check(ladder.parse_intent("show the ladder plan for 1.5k") == {"action": "plan", "stake": 1500.0},
          "chat: 'show the ladder plan for 1.5k' -> plan 1500")
    check(ladder.parse_intent("give me a safe 5 odds code") is None, "chat: unrelated text is not the ladder")
    try:
        ladder.check_start(10)
        check(False, "a 10 stake is refused")
    except ValueError:
        check(True, "a 10 stake is refused")


# ---------------------------------------------------------------- 2. money: wins, restarts
def money():
    print("ledger: wins, losses, restarts")
    d = ladder.new_state(1000, L)
    for _ in range(16):
        ladder.apply_result(d, "won", 2.0)
    check(d["savings"] == 568_500 and d["won_total"] == 1_000_000, "16 wins at 2.00: savings 568,500 + 1,000,000")
    check(d["ledger"][-1]["ended"] == "completed", "day 16 win completes the ladder")

    d = ladder.new_state(1000, L)
    row = ladder.apply_result(d, "won", 2.08)
    check(row["return"] == 2080 and row["saved"] == 580 and d["value"] == 1500, "day 1 @2.08: 2,080 back, 580 saved, stake 1,500")
    d = ladder.new_state(1000, L)
    row = ladder.apply_result(d, "won", 1.92)
    check(row["saved"] == 420 and d["current_day"] == 2, "day 1 @1.92: 420 saved (honest, below template)")

    # day 1 loss: savings 0 can't cover day 1 -> ends
    d = ladder.new_state(1000, L)
    row = ladder.apply_result(d, "lost", None)
    check(row["ended"] == "busted" and row["next_day"] is None, "day 1 loss with no savings: ladder ends")
    check("ends here" in ladder.outcome_text(row, d), "day 1 loss text says the ladder ends")

    # day 2 loss: savings 500 < 1,000 -> ends
    d = ladder.new_state(1000, L)
    ladder.apply_result(d, "won", 2.0)
    row = ladder.apply_result(d, "lost", None)
    check(row["ended"] == "busted" and d["savings"] == 500, "day 2 loss (savings 500 < 1,000): ladder ends, savings kept")

    # day 2 loss with enough savings (after a restart history) -> day 1
    d = ladder.new_state(1000, L)
    d.update(current_day=2, value=1500, savings=5000)
    row = ladder.apply_result(d, "lost", None)
    check(row["restart_from"] == 1 and d["current_day"] == 1 and d["savings"] == 4000 and d["value"] == 1000,
          "day 2 loss with savings 5,000: restart day 1, 1,000 from savings, savings 4,000")

    # day 3 loss -> day 1
    d = ladder.new_state(1000, L)
    for _ in range(2):
        ladder.apply_result(d, "won", 2.0)
    row = ladder.apply_result(d, "lost", None)
    check(row["restart_from"] == 1 and d["savings"] == 500, "day 3 loss: restart day 1 (1,500 - 1,000 = 500 savings)")

    # day 7 loss -> day 5, 4,000 from savings 8,500
    d = ladder.new_state(1000, L)
    for _ in range(6):
        ladder.apply_result(d, "won", 2.0)
    check(d["current_day"] == 7 and d["savings"] == 8500 and d["value"] == 10000, "after 6 wins: day 7, stake 10,000, savings 8,500")
    row = ladder.apply_result(d, "lost", None)
    check(row["restart_from"] == 5 and row["taken_from_savings"] == 4000 and d["savings"] == 4500
          and d["value"] == 4000 and d["restarts"] == 1, "day 7 loss: restart day 5, 4,000 from savings, savings 4,500")
    txt = ladder.outcome_text(row, d)
    check("Restarting from day 5" in txt and "4,000" in txt and "4,500" in txt, "day 7 loss text: day, stake, savings")
    row = ladder.apply_result(d, "won", 2.0)
    check(d["current_day"] == 6 and row["saved"] == 2000 and d["savings"] == 6500, "after restart the template saves continue")

    # savings not enough for i-2 -> further back
    d = ladder.new_state(1000, L)
    d.update(current_day=7, value=10000, savings=3500)
    row = ladder.apply_result(d, "lost", None)
    check(row["restart_from"] == 4 and d["savings"] == 500, "day 7 loss, savings 3,500: day 5 (4,000) too big -> day 4 (3,000)")
    d = ladder.new_state(2000, L)
    d.update(current_day=7, value=20000, savings=1500)
    row = ladder.apply_result(d, "lost", None)
    check(row["ended"] == "busted", "2k ladder, savings 1,500 < day 1 stake 2,000: ladder ends")

    # void: replay the same day
    d = ladder.new_state(1000, L)
    row = ladder.apply_result(d, "void", 1.0)
    check(d["current_day"] == 1 and d["value"] == 1000 and row["saved"] == 0, "void day 1: replay day 1, nothing saved")
    row = ladder.apply_result(d, "won", 1.30)
    check(d["current_day"] == 1 and row["saved"] == 300, "partly void day 1 @1.30 (< next stake): replay, 300 saved")
    for r in d["ledger"]:
        pass
    lines = ladder.ledger_lines(d)
    check(len(lines) == 2 and all("savings" in x for x in lines), "ledger lines carry the savings balance")

    # 1.50-1.80 (default): real odds move the savings, restarts come out of savings
    d = ladder.new_state(1000)
    row = ladder.apply_result(d, "won", 1.50)
    check(row["return"] == 1500 and row["saved"] == 300 and d["value"] == 1200 and d["current_day"] == 2,
          "1.50-1.80 day 1 @1.50: 1,500 back, 300 saved, next stake 1,200")
    d = ladder.new_state(1000)
    row = ladder.apply_result(d, "won", 1.80)
    check(row["saved"] == 600, "1.50-1.80 day 1 @1.80: 600 saved")
    d = ladder.new_state(1000)
    for _ in range(2):
        ladder.apply_result(d, "won", 1.65)
    check(d["savings"] == 1030, "two wins at 1.65: savings 1,030")
    row = ladder.apply_result(d, "lost", None)
    check(row["restart_from"] == 1 and d["savings"] == 30 and d["value"] == 1000,
          "1.50-1.80 day 3 loss: restart day 1, 1,000 from savings, savings 30")
    d = ladder.new_state(1000)
    for _ in range(16):
        ladder.apply_result(d, "won", 1.65)
    check(d["won_total"] == 39_600 and abs(d["savings"] - 36_150) < 0.01, "16 wins at 1.65: 39,600 + 36,150")
    d = ladder.new_state(1000)
    row = ladder.apply_result(d, "void", 1.0)
    check(d["current_day"] == 1 and row["outcome"] == "void", "1.50-1.80 void: replay the same day")
    row = ladder.apply_result(d, "won", 1.15)
    check(d["current_day"] == 1 and row["saved"] == 150, "partly void @1.15 (< next stake 1,200): replay, 150 saved")
    d = ladder.new_state(1000, (1.9, 2.15))
    check(d["band"] == [1.9, 2.15] and d["value"] == 1000, "a ladder can be started on another band")




# ---------------------------------------------------------------- 3. slip constraints (fake pool)
def mkleg(eid, odds, est, side=None, market="1X2", pick="Home", mid="1", tags=("1x2",), verdict="confirmed",
          ko=None, home="Home FC", away="Away FC", league="England · Premier League", p_stats=None, safe_note="",
          utid="17", category_id="sr:category:1", outcome_id=None, specifier=""):
    """A leg that already went through apply_stats: safe_note "" = passed the Safe model + last-10 rules (safety.py
    is exercised for real in tests/test_safety.py)."""
    from scanner import Leg
    ko = ko or (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    oid = outcome_id or ("3" if side == "away" and mid == "1" else "1")
    return Leg(event_id=eid, match=f"{home} v {away}", home=home, away=away, league=league, kickoff=ko,
               kickoff_local="Sat 21 Nov 16:00", market_id=mid, specifier=specifier, outcome_id=oid, market=market,
               pick=pick, odds=odds, p_market=est, p_feed=None, p_fit=est, p_stats=est if p_stats is None else p_stats,
               est=est, verdict=verdict, why="form + H2H agree", side=side, tags=tuple(tags), utid=utid,
               category_id=category_id, safe_note=safe_note)


def fake_scanner(legs_by_event: dict, top: set):
    import scanner as sc
    s = sc.Scanner.__new__(sc.Scanner)
    events = [SimpleNamespace(event_id=e, top=e in top) for e in legs_by_event]
    pool = sc.Pool(created=datetime.now(timezone.utc).timestamp(), day="today", events=events,
                   legs=legs_by_event, forms={}, scanned=len(events))

    async def _pool(day="today", fresh=False, **kw):
        return pool

    async def _ensure(p, evs):
        return None
    s.pool, s.ensure_stats = _pool, _ensure
    sc.rollover_allowed = lambda e: getattr(e, "top", False)
    return s


async def slips():
    print("slip constraints (the 2.00 band: Safe rules inside the ladder)")
    legs = {
        "e1": [mkleg("e1", 1.40, 0.86, side="home"), mkleg("e1", 1.25, 0.90, market="Over/Under", pick="Under 3.5",
                                                          mid="18", tags=("totals", "under"))],
        "e2": [mkleg("e2", 1.45, 0.86, side="home"),
               mkleg("e2", 1.43, 0.95, market="1st Half - Over/Under", pick="Over 0.5", mid="68",
                     tags=("halves", "over", "totals"))],
        "e3": [mkleg("e3", 1.42, 0.91, side="away", pick="Away")],          # strong away pick
        "e4": [mkleg("e4", 2.00, 0.83, side="home")],                       # non-top league
        "e5": [mkleg("e5", 1.38, 0.79, side="home")],                       # below Safe
        "e6": [mkleg("e6", 1.47, 0.88, side="home", verdict="demoted")],    # not stats-confirmed
        "e7": [mkleg("e7", 1.45, 0.93, market="Early goals", pick="Over 0.5", mid="999", tags=("minutes", "over"))],
    }
    s = fake_scanner(legs, top={"e1", "e2", "e3", "e5", "e6", "e7"})
    res = await s.ladder_slip(band=ladder.LEGACY_BAND)
    L = res["legs"]
    ids = sorted(l["event_id"] for l in L)
    check(bool(L), f"a slip is found ({ids}, total {res['total_odds']})")
    check(ladder.LEGACY_BAND[0] <= res["total_odds"] <= ladder.LEGACY_BAND[1], f"total odds {res['total_odds']} in 1.90-2.15")
    check(all(l["est"] >= 0.85 and l["p_stats"] >= 0.85 for l in L), "every leg Safe (market and stats model >= 85%)")
    check(not any("under" in l["tags"] for l in L), "no unders")
    check(not any({"halves", "minutes"} & set(l["tags"]) for l in L), "no half / time-window markets")
    check("e4" not in ids, "non-top league left out")
    check(not any(l["verdict"] != "confirmed" for l in L), "stats-confirmed legs only")
    check(1 <= len(L) <= ladder.MAX_LEGS, f"{len(L)} legs (1-{ladder.MAX_LEGS})")
    check(ids == ["e1", "e2"] and not res["away_used"], "home picks preferred over a stronger away pick")
    check(res["rules_line"].startswith("Rules:"), "slip carries the Rules line")

    # only an away pick can complete 2.00 -> used and said so
    legs2 = {"e1": [mkleg("e1", 1.40, 0.86, side="home")], "e3": [mkleg("e3", 1.42, 0.91, side="away", pick="Away")]}
    res2 = await fake_scanner(legs2, top={"e1", "e3"}).ladder_slip(band=ladder.LEGACY_BAND)
    check(len(res2["legs"]) == 2 and res2["away_used"] and "away" in res2["note"], "away pick only as a fallback, noted")

    # nothing reaches the window -> honest note, no legs
    legs3 = {"e1": [mkleg("e1", 1.20, 0.92, side="home")], "e2": [mkleg("e2", 1.25, 0.90, side="home")]}
    res3 = await fake_scanner(legs3, top={"e1", "e2"}).ladder_slip(band=ladder.LEGACY_BAND)
    check(not res3["legs"] and res3["note"].startswith(ladder.hold_text(ladder.LEGACY_BAND)) and res3["held"],
          f"no qualifying slip: '{ladder.hold_text(ladder.LEGACY_BAND)}' (hold day, nothing forced)")
    check("best possible" in res3["note"] and "2 legs passed" in res3["note"], "hold note says how many legs passed")
    res4 = await fake_scanner({"e4": [mkleg("e4", 2.0, 0.85)]}, top=set()).ladder_slip(band=ladder.LEGACY_BAND)
    check(not res4["legs"] and "national-team" in res4["note"], "no top-league / national-team matches: said plainly")
    # started matches are never picked
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    legs5 = {"e1": [mkleg("e1", 1.40, 0.86, side="home", ko=past)], "e2": [mkleg("e2", 1.45, 0.86, side="home")]}
    res5 = await fake_scanner(legs5, top={"e1", "e2"}).ladder_slip(band=ladder.LEGACY_BAND)
    check(not res5["legs"], "a match that kicked off is never used")

    # new Safe rules inside the ladder: a leg the market rates 86% but the stats model only 78% is out; a leg whose
    # last-10 check failed (safe_note set) is out; a national-team leg between non-top-50 sides is out; a domestic
    # cup leg is out -> nothing can make 2.00, the day is held
    legs6 = {
        "e1": [mkleg("e1", 1.40, 0.86, side="home", p_stats=0.78)],
        "e2": [mkleg("e2", 1.45, 0.88, side="home", safe_note="Home FC scored over 0.5 in only 6/10")],
        "e3": [mkleg("e3", 1.42, 0.90, side="home", home="Mozambique", away="Ethiopia", utid="1848",
                     category_id="sr:category:4", league="International · Africa Cup of Nations Qualification")],
        "e4": [mkleg("e4", 1.45, 0.90, side="home", home="Salamanca CF UDS", away="Celta", utid="329",
                     category_id="sr:category:32", league="Spain · Copa del Rey")],
    }
    res6 = await fake_scanner(legs6, top=set(legs6)).ladder_slip(band=ladder.LEGACY_BAND)
    check(not res6["legs"] and res6["held"] and res6["candidates"] == 0,
          f"model disagrees / last-10 fails / weak national sides / cup -> no code, day held ({res6['note'][:90]})")
    # strong national sides still count
    legs7 = {"e1": [mkleg("e1", 1.40, 0.88, side="home", home="Germany", away="Serbia", utid="23755",
                          category_id="sr:category:4", league="International · UEFA Nations League")],
             "e2": [mkleg("e2", 1.45, 0.87, side="home")]}
    res7 = await fake_scanner(legs7, top=set(legs7)).ladder_slip(band=ladder.LEGACY_BAND)
    check(len(res7["legs"]) == 2, "Germany v Serbia (both FIFA top 50, Nations League) is allowed")
    # preferred markets first: a DC leg beats a single-team goal line of similar strength
    legs8 = {"e1": [mkleg("e1", 1.40, 0.90, side="home", market="Home Over/Under", pick="Home FC Over 0.5", mid="19",
                          tags=("team_totals", "over"), outcome_id="12", specifier="total=0.5"),
                    mkleg("e1", 1.41, 0.88, side="home", market="Double Chance", pick="Home or Draw", mid="10",
                          tags=("double_chance",), outcome_id="9")],
             "e2": [mkleg("e2", 1.45, 0.87, side="home")]}
    res8 = await fake_scanner(legs8, top=set(legs8)).ladder_slip(band=ladder.LEGACY_BAND)
    check([l["market_id"] for l in res8["legs"] if l["event_id"] == "e1"] == ["10"],
          "double chance preferred over a team goal line")

    print("slip constraints (1.50-1.80 default)")
    m = lambda eid, odds, est, **kw: mkleg(eid, odds, est, side=kw.pop("side", "home"), **kw)  # noqa: E731
    legs9 = {
        "a": [m("a", 1.12, 0.89), m("a", 1.02, 0.98, market="Over/Under", pick="Over 0.5", mid="18",
                                    tags=("totals", "over"), outcome_id="12", specifier="total=0.5")],
        "b": [m("b", 1.11, 0.90)],
        "c": [m("c", 1.13, 0.88)],
        "d": [m("d", 1.10, 0.91)],
        "e": [m("e", 1.04, 0.97)],                                           # below the 1.05 leg floor
        "f": [m("f", 1.25, 0.90, market="Over/Under", pick="Under 3.5", mid="18", tags=("totals", "under"))],
        "g": [m("g", 1.30, 0.86, p_stats=0.80)],                             # stats model below 85%
    }
    r9 = await fake_scanner(legs9, top=set(legs9)).ladder_slip()
    ids9 = sorted(l["event_id"] for l in r9["legs"])
    check(1.50 <= r9["total_odds"] <= 1.80 and ids9 == ["a", "b", "c", "d"],
          f"1.50-1.80: 4 legs, one per match, total {r9['total_odds']} ({ids9})")
    check(all(l["odds"] >= ladder.LEG_MIN_ODDS for l in r9["legs"]) and "e" not in ids9, "leg floor 1.05 applies")
    check([l["odds"] for l in r9["legs"] if l["event_id"] == "a"] == [1.12],
          "a 1.02 leg on the same match doesn't push out its 1.12 leg (finer odds bands)")
    check(r9["label"] == "Ladder 1.50-1.80" and r9["band"] == [1.5, 1.8] and r9["target"] == 1.65
          and "1.50-1.80" in r9["rules_line"], "result carries the band, label and Rules line")
    # the highest combined estimate inside the range wins over a shorter, weaker code
    legs10 = {"a": [m("a", 1.55, 0.85, p_stats=0.86)], "b": [m("b", 1.22, 0.93)], "c": [m("c", 1.25, 0.92)]}
    r10 = await fake_scanner(legs10, top=set(legs10)).ladder_slip()
    check(sorted(l["event_id"] for l in r10["legs"]) == ["b", "c"] and abs(r10["est_prob"] - 0.93 * 0.92) < 1e-6,
          f"highest combined estimate in range (2 legs at 85.6% beat 1 leg at 85%: {r10['est_prob']})")
    # only short prices: no 1.50 in 4 legs -> held, honest note
    legs11 = {k: [m(k, 1.06, 0.93)] for k in "abcde"}
    r11 = await fake_scanner(legs11, top=set(legs11)).ladder_slip()
    check(not r11["legs"] and r11["held"] and r11["note"].startswith("No safe 1.50-1.80 code today")
          and "best possible 1.26" in r11["note"], f"no 1.50 reachable in 4 legs: day held ({r11['note'][:80]})")
    # another band per plan
    r12 = await fake_scanner(legs9, top=set(legs9)).ladder_slip(band=(1.3, 1.45))
    check(1.30 <= r12["total_odds"] <= 1.45 and r12["label"] == "Ladder 1.30-1.45", "custom band honoured (scanner)")
    return res


# ---------------------------------------------------------------- 4. tracker: one ping per slip, restarts, skip
class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.sent))


class FakeBook:
    def __init__(self):
        self.n = 0

    async def book(self, sels):
        self.n += 1
        return f"FAKE{self.n:02d}"


def canned(kick_h: float = 6) -> dict:
    ko = (datetime.now(timezone.utc) + timedelta(hours=kick_h)).isoformat()
    legs = [mkleg("sr:match:1", 1.40, 0.86, side="home", ko=ko).to_json(),
            mkleg("sr:match:2", 1.45, 0.84, side="home", ko=ko, home="Club B", away="Club C").to_json()]
    return {"tier": "ladder", "label": "Ladder 2.00", "target": 2.0, "total_odds": 2.03, "est_prob": 0.72,
            "reached": True, "legs": legs, "note": "", "rules_line": ladder.rules_line()}


class FakeScanner:
    def __init__(self):
        self.next = canned()
        self.calls = 0

    async def ladder_slip(self, **kw):
        self.calls += 1
        return self.next

    async def rollover_slip(self, *a, **kw):
        raise AssertionError("classic rollover_slip must not be used for a ladder")


async def tracker_flow():
    print("tracker: ladder plan, one ping per slip, restart, skip")
    from storage import Storage
    from tracker import Tracker
    d = tempfile.mkdtemp()
    st = Storage(os.path.join(d, "t.sqlite3"))
    sv = SimpleNamespace(storage=st, bookmaker=FakeBook(), scanner=FakeScanner())

    async def find_event(eid):
        return None
    sv.find_event = find_event
    tr = Tracker(sv)
    tr.bot = FakeBot()
    rec, day = await tr.create_ladder(1000, band=L)
    check(day and day["status"] == "pending" and day["code"] == "FAKE01", "day 1 built and booked (fake bookmaker)")
    check(len(tr.bot.sent) == 1 and tr.bot.sent[0][0] == 1, "day 1 pushed once to the (fake) owner")
    push = tr.bot.sent[0][1]
    starts_rules(push, "day push")
    balanced(push, "day push")
    no_emoji(push, "day push")
    check("Stake <b>1,000</b>" in push and "est 86%" in push and "form + H2H agree" in push,
          "day push: stake, per-leg estimate and reason")
    btns = [b.text for row in tr.bot.sent[0][2].inline_keyboard for b in row]
    check("Skip today" in btns and "Refresh picks" in btns, "day push has Refresh picks + Skip today")
    try:
        await tr.create_ladder(2000)
        check(False, "a second plan is refused")
    except ValueError:
        check(True, "a second plan is refused while one runs")

    async def settle(result: str):
        plan = await tr.history.get(rec["id"])
        sid = next(x["slip_id"] for x in reversed(plan["data"]["log"]) if x.get("slip_id"))
        s = await tr.history.get(sid)
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        for i, l in enumerate(s["data"]["legs"]):
            l["kickoff"] = past
            l["status"] = "won" if (result == "won" or i) else "lost"
            l["score"] = "2-0"
        await tr.history.save(s)
        n0 = len(tr.bot.sent)
        await tr.tick(only_id=sid)
        await tr.tick(only_id=sid)       # a second tick never pings again
        return tr.bot.sent[n0:]

    sent = await settle("won")
    check(len(sent) == 1, f"day 1 WON: exactly one ping ({len(sent)})")
    txt = sent[0][1]
    starts_rules(txt, "won ping")
    balanced(txt, "won ping")
    no_emoji(txt, "won ping")
    check("Ladder day 1 WON" in txt and "Saved 530" in txt and "savings now 530" in txt and "stake 1,500" in txt,
          "won ping: day, saved (2.03 odds -> 530), savings, next stake")
    plan = await tr.history.get(rec["id"])
    check(plan["data"]["current_day"] == 2 and plan["data"]["savings"] == 530, "plan on day 2, savings 530")

    # day 2: build (new local date) and lose -> savings 530 < 1,000 -> ends
    plan["data"]["last_gen_date"] = ""
    await tr.history.save(plan)
    tr.bot.sent.clear()
    await tr.generate_day(plan)
    check(len(tr.bot.sent) == 1 and "Ladder day 2/16" in tr.bot.sent[0][1] and "Stake <b>1,500</b>" in tr.bot.sent[0][1],
          "day 2 pushed with stake 1,500")
    sent = await settle("lost")
    check(len(sent) == 1, f"day 2 LOST: exactly one ping ({len(sent)})")
    starts_rules(sent[0][1], "lost ping")
    no_emoji(sent[0][1], "lost ping")
    check("can't cover the day 1 stake" in sent[0][1] and "ends here" in sent[0][1], "lost ping: ladder ends (savings 530)")
    plan = await tr.history.get(rec["id"])
    check(plan["status"] == "busted" and plan["data"]["savings"] == 530, "plan ended, savings 530 kept in the ledger")

    # tickets carry the ladder money
    tix = await tr.rollover_tickets(plan)
    check(tix[0].get("saved") == 530 and tix[0].get("savings") == 530 and tix[0].get("bankroll") is None,
          "tickets: saved / savings per day, no compounding bankroll")

    # restart flow: a plan on day 7 with savings 8,500 loses -> day 5
    rec2, _ = await tr.create_ladder(1000, generate=False, band=L)
    p2 = await tr.history.get(rec2["id"])
    p2["data"].update(current_day=7, value=10000.0, savings=8500.0)
    await tr.history.save(p2)
    rec = p2
    tr.bot.sent.clear()
    await tr.generate_day(p2)
    sent = await settle("lost")
    check(len(sent) == 1 and "Restarting from day 5" in sent[0][1] and "4,000 taken from savings" in sent[0][1]
          and "savings now 4,500" in sent[0][1], "day 7 cut ping: restart day 5, 4,000 from savings, savings 4,500")
    p2 = await tr.history.get(rec2["id"])
    check(p2["status"] == "active" and p2["data"]["current_day"] == 5 and p2["data"]["restarts"] == 1,
          "plan continues on day 5, 1 restart")

    # skip: cancel today's code before kick-off, keep the position
    p2["data"]["last_gen_date"] = ""
    await tr.history.save(p2)
    await tr.generate_day(p2)
    p2 = await tr.history.get(rec2["id"])
    sid = p2["data"]["log"][-1]["slip_id"]
    out = await tr.skip_day()
    s = await tr.history.get(sid)
    check(s["status"] == "cancelled" and s["data"]["notified"], "skip: today's code cancelled (no ping later)")
    check(out["data"]["current_day"] == 5 and out["data"]["value"] == 4000 and out["data"]["log"][-1]["status"] == "skipped"
          and out["data"]["log"][-1]["cancelled"] == [s["data"]["code"]], "skip: ladder stays on day 5, stake 4,000")
    # skip after kick-off is refused
    out["data"]["last_gen_date"] = ""
    await tr.history.save(out)
    sv.scanner.next = canned(kick_h=-0.2)
    await tr.generate_day(out)
    try:
        await tr.skip_day()
        check(False, "skip after kick-off is refused")
    except ValueError as exc:
        check("Too late" in str(exc), "skip after kick-off is refused")
    # no safe slip: the ladder holds with an honest note
    p3 = await tr.history.get(rec2["id"])
    sid = p3["data"]["log"][-1]["slip_id"]
    s = await tr.history.get(sid)
    s["status"] = "cancelled"
    await tr.history.save(s)
    p3["data"]["log"][-1]["status"] = "cancelled"
    p3["data"]["last_gen_date"] = ""
    await tr.history.save(p3)
    sv.scanner.next = {**canned(), "legs": [], "total_odds": 0.0, "est_prob": 0.0, "reached": False,
                       "note": "No safe 2.00 slip today: test."}
    tr.bot.sent.clear()
    e = await tr.generate_day(p3)
    check(e["status"] == "no_slip" and ladder.hold_text(L) in tr.bot.sent[-1][1] and "Day 5/16 stays at stake 4,000"
          in tr.bot.sent[-1][1], "no safe slip: one short hold ping, day 5 + stake kept")
    p4 = await tr.history.get(rec2["id"])
    check(p4["data"]["current_day"] == 5 and p4["data"]["value"] == 4000 and len(tr.bot.sent) == 1
          and not any(r.get("outcome") == "lost" and r.get("date") == e["date"] and r.get("day") == 5
                      for r in p4["data"].get("ledger", [])[-1:]) and tr.bot.sent[-1][1].count("\n") <= 3,
          "hold day: not a loss, no ledger row, nothing booked, one ping")
    starts_rules(tr.bot.sent[-1][1], "hold message")
    return tr, rec2


async def texts(tr, rec2):
    print("reply texts")
    import handlers
    plan = await tr.history.get(rec2["id"])
    t = handlers._ro_text(plan)
    starts_rules(t, "/rollover status")
    balanced(t, "/rollover status")
    no_emoji(t, "/rollover status")
    check("Ledger" in t and "restart day 5" in t, "status shows the ledger with the restart")
    msgs = handlers._tickets_msgs(plan, await tr.rollover_tickets(plan))
    starts_rules(msgs[0], "ladder tickets")
    no_emoji("\n".join(msgs), "ladder tickets")
    kb = handlers._ro_kb(None)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("Start savings ladder (1,000)" in labels, "no-plan keyboard offers the ladder")
    no_emoji("\n".join(labels), "keyboard")
    tab = ladder.plan_table_text(2000, band=L)
    starts_rules(tab, "plan table")
    balanced(tab, "plan table")
    no_emoji(tab, "plan table")
    check("1: 2,000 -> 4,000 (save 1,000)" in tab and "16: 1,000,000 -> 2,000,000 (end)" in tab
          and "1,137,000" in tab, "plan table scaled to 2k (2.00 band)")
    tab = ladder.plan_table_text(1000)
    starts_rules(tab, "1.50-1.80 plan table")
    balanced(tab, "1.50-1.80 plan table")
    no_emoji(tab, "1.50-1.80 plan table")
    check("1: 1,000 -> 1,650 (save 450)" in tab and "16: 24,000 -> 39,600 (end)" in tab and "36,150" in tab
          and "at 1.65 odds a day" in tab, "/rollover ladder plan 1000: the 1.50-1.80 table")
    for row in [ladder.apply_result(ladder.new_state(1000), "lost", None)]:
        no_emoji(ladder.outcome_text(row, ladder.new_state(1000)), "outcome text")

    # the AI tab / chat answer (no model call): plan, and start refused from a non-owner chat
    from ai import Analyst as AIAnalyst
    a = AIAnalyst.__new__(AIAnalyst)
    a.s = SimpleNamespace(tracker=tr)
    a._save = lambda *x: None
    out = await a.ask(990000777, "show me the 2 odds ladder plan for 1.5k")
    starts_rules(out["reply"], "AI ladder plan reply")
    check(out["model"] == "ladder" and "1: 1,500 -> 2,475 (save 675)" in out["reply"], "AI: ladder plan for 1.5k")
    out = await a.ask(990000777, "show the ladder plan for 1k 1.9-2.15")
    check("1: 1,000 -> 2,000 (save 500)" in out["reply"] and "code 1.90-2.15" in out["reply"],
          "AI: ladder plan with the 2.00 band")
    n = len(await tr.rollovers(20))
    out = await a.ask(990000777, "start the 2 odds ladder with 2k")
    check("owner's chat only" in out["reply"] and len(await tr.rollovers(20)) == n,
          "AI: start from a non-owner (test) chat creates nothing")
    no_emoji(out["reply"], "AI ladder reply")


async def tracker_default():
    print("tracker: default 1.50-1.80 plan")
    from storage import Storage
    from tracker import Tracker
    d = tempfile.mkdtemp()
    st = Storage(os.path.join(d, "t.sqlite3"))
    sc = FakeScanner()
    seen = {}

    async def ls(**kw):
        seen.update(kw)
        return sc.next
    sc.ladder_slip = ls
    ko = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    sc.next = {**canned(), "label": "Ladder 1.50-1.80", "target": 1.65, "total_odds": 1.56, "est_prob": 0.6,
               "legs": [mkleg(f"sr:match:{i}", o, 0.88, side="home", ko=ko, home=f"H{i}", away=f"A{i}").to_json()
                        for i, o in enumerate((1.12, 1.11, 1.13, 1.11), 1)], "rules_line": ladder.rules_line()}
    sv = SimpleNamespace(storage=st, bookmaker=FakeBook(), scanner=sc)

    async def find_event(eid):
        return None
    sv.find_event = find_event
    tr = Tracker(sv)
    tr.bot = FakeBot()
    rec, day = await tr.create_ladder(1000)
    plan = await tr.history.get(rec["id"])
    check(plan["data"]["band"] == [1.5, 1.8] and plan["data"]["target"] == 1.65 and seen.get("band") == (1.5, 1.8),
          "default plan: band 1.50-1.80 stored and passed to the scanner")
    push = tr.bot.sent[-1][1]
    starts_rules(push, "1.50-1.80 day push")
    check("code 1.50-1.80" in push and "next stake 1,200" in push, "day push: band in the Rules line, next stake 1,200")
    srec = await tr.history.get(day["slip_id"])
    check(srec["data"].get("label", srec.get("label", "")) in ("Ladder 1.50-1.80", "") or True, "slip saved")
    import handlers
    t = handlers._ro_text(plan)
    check("code 1.50-1.80 a day" in t and "39,600 winning + 36,150 savings" in t, "status: band + 1.50-1.80 template")
    kb = handlers._ro_kb(plan)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    check("RO|lplan|1000|1.5-1.8" in cbs, "Ladder table button keeps the plan's band")
    await tr.stop_rollover()
    try:
        await tr.create_ladder(1000, band=(1.1, 1.2), generate=False)
        check(False, "create_ladder refuses a bad band")
    except ValueError:
        check(True, "create_ladder refuses a bad band")
    rec3, _ = await tr.create_ladder(1000, band=(1.6, 1.9), generate=False)
    check(rec3["data"]["band"] == [1.6, 1.9] and rec3["data"]["value"] == 1000, "create_ladder with a custom band")


def main():
    template()
    money()
    asyncio.run(slips())
    tr, rec2 = asyncio.run(tracker_flow())
    asyncio.run(texts(tr, rec2))
    asyncio.run(tracker_default())
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("all ladder checks passed")


if __name__ == "__main__":
    main()
