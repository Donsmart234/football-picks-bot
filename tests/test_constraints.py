"""Constraint layer tests (offline, no network, no Gemini, no booking).

    python tests/test_constraints.py

Covers: market-type tagging (every kind of Under), natural-language / command parsing, per-leg and slip
verification, the scanner enforcing rules at the candidate stage (synthetic pool), rollover excluding unders,
and the AI tool path (a fake Gemini that "forgets" the no-unders rule: the server still enforces it, and booking
refuses a slip that breaks the user's words).
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

from constraints import Constraints, is_under, market_tags, parse_text, verify  # noqa: E402
from scanner import Leg, Pool, Scanner  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("FAIL", msg)


# ------------------------------------------------------------------ tagging
UNDERS = [
    ("18", "Over/Under", "Under 2.5", "Main"), ("18", "Over/Under", "Under 2.25", "Main"),          # match + Asian
    ("19", "Arsenal Over/Under", "Arsenal Under 1.5", "Goals"),                                       # team
    ("69", "1st half - Arsenal Over/Under", "Arsenal Under 0.5", "Goals"),
    ("68", "1st Half - Over/Under", "Under 1.5", "Half"), ("90", "2nd Half - Over/Under", "Under 1.5", "Half"),
    ("166", "Corners - Over/Under", "Under 9.5", "Corners"), ("900300", "Home Team Total Corners", "Under 4.5", "Corners"),
    ("139", "Bookings - Over/Under", "Under 4.5", "Bookings"), ("138", "Booking Points-Over/Under", "Under 45.5", "Bookings"),
    ("900304", "Home Team Total Bookings", "Under 2.5", "Bookings"),
    ("37", "1X2 & Over/Under 2.5", "Home & Under 2.5", "Combo"), ("36", "Over/Under & GG/NG", "Under 2.5 & No", "Combo"),
    ("547", "Double Chance & Over/Under 3.5", "Home/Draw & Under 3.5", "Combo"),
    ("59", "Both Halves Under 1.5", "Yes", "Goals"), ("58", "Both Halves Over 1.5", "No", "Goals"),
    ("855", "Home Team or Under 2.5", "Yes", "Combo"), ("900313", "Total Goals Over/Under from 1 to 10 minute", "Under 0.5", "Minutes"),
    ("450001", "Goal Bounds", "0-2", "Main"), ("25", "Goal Range", "0-1", "Main"), ("169", "Corner Range", "0-8", "Corners"),
    ("142", "Exact Bookings", "0-3", "Bookings"), ("21", "Exact Goals", "0", "Main"),
    ("900393", "Shots on Target Over/Under", "Under 6.5", "Match"), ("60180", "Over/Under - Early Goals", "Under 1.5", "Main"),
]
NOT_UNDERS = [
    ("1", "1X2", "Home", "Main"), ("10", "Double Chance", "Home or Draw", "Main"), ("18", "Over/Under", "Over 1.5", "Main"),
    ("29", "GG/NG", "No", "Main"), ("31", "Home Team Clean Sheet", "Yes", "Goals"), ("16", "Asian Handicap -0.5", "Home (-0.5)", "Main"),
    ("58", "Both Halves Over 1.5", "Yes", "Goals"), ("855", "Home Team or Under 2.5", "No", "Combo"),
    ("548", "Multigoals", "1-3", "Goals"), ("166", "Corners - Over/Under", "Over 7.5", "Corners"),
    ("800063", "Match Cards", "3+", "Match"), ("45", "Correct Score", "1:0", "Main"),
]


def test_tags():
    for a in UNDERS:
        check("under" in market_tags(*a, home="Arsenal", away="Chelsea"), f"should be under: {a}")
    for a in NOT_UNDERS:
        check("under" not in market_tags(*a, home="Arsenal", away="Chelsea"), f"should not be under: {a}")
    # team called "Under..." must not trip the word match
    check("under" not in market_tags("1", "1X2", "Underhill FC", "Main", home="Underhill FC", away="X"), "team name")
    t = market_tags("1", "1X2", "Home", "Main")
    check(t == ("1x2",), f"1x2 tags {t}")
    check("double_chance" in market_tags("63", "1st Half - Double Chance", "Home or Draw", "Half"), "dc half")
    check({"corners", "handicap"} <= set(market_tags("165", "Corner Handicap", "Home (-1.5)", "Corners")), "corner hcp")
    check("player_props" in market_tags("775", "Player goals (incl. overtime)", "Saka (Arsenal) 1+", "Players",
                                        "variant=pre:playerprops:1"), "props")
    check("cards" in market_tags("146", "Sending Off", "No", "Bookings"), "cards")
    check("btts" in market_tags("29", "GG/NG", "Yes", "Main"), "btts")
    check("team_totals" in market_tags("19", "Arsenal Over/Under", "Arsenal Over 0.5", "Goals", home="Arsenal"), "tt")
    check("totals" in market_tags("18", "Over/Under", "Over 2.5", "Main"), "totals")
    check("specials" in market_tags("900394", "Shots Over/Under", "Over 27.5", "Match"), "specials")


# ------------------------------------------------------------------ parsing
def test_parse():
    c = parse_text("give me sure 5 odds without under selections")
    check(c.tier == "safe" and c.target_odds == 5 and c.exclude_markets == ["under"], f"sure 5 no unders: {c.to_dict()}")
    c = parse_text("safe 10 odds, no unders, no corners or cards, max 8 legs, premier league only")
    check(c.max_legs == 8 and set(c.exclude_markets) == {"under", "corners", "cards"}
          and c.include_leagues == ["premier league"], f"combo parse {c.to_dict()}")
    c = parse_text("risky 20 odds tomorrow after 6pm only overs and btts")
    check(c.tier == "risk" and c.day == "tomorrow" and c.kickoff_after == "18:00"
          and c.include_markets == ["over", "btts"], f"risky parse {c.to_dict()}")
    c = parse_text("mid safe 10 odds legs above 75% stats confirmed only")
    check(c.tier == "mid" and c.min_leg_prob == 0.75 and c.stats_confirmed_only, f"mid parse {c.to_dict()}")
    c = parse_text("safe 3 odds with each leg at least 1.2, between 4-6 legs, exclude la liga")
    check(c.min_leg_odds == 1.2 and (c.min_legs, c.max_legs) == (4, 6) and c.exclude_leagues == ["la liga"],
          f"legs/odds parse {c.to_dict()}")
    c = parse_text("safe 5 nounders -corners only:1x2,dc legs:4-8 minodds:1.15 maxodds:1.4 after:18:00 before:22:30")
    check(set(c.exclude_markets) == {"under", "corners"} and c.include_markets == ["1x2", "double_chance"]
          and (c.min_legs, c.max_legs) == (4, 8) and c.min_leg_odds == 1.15 and c.max_leg_odds == 1.4
          and c.kickoff_after == "18:00" and c.kickoff_before == "22:30", f"command parse {c.to_dict()}")
    c = parse_text("no more than 5 legs please, 4 odds")
    check(c.max_legs == 5 and c.target_odds == 4 and not c.exclude_markets, f"no more than {c.to_dict()}")
    check(parse_text("who wins arsenal vs chelsea?").empty(), "plain question has no rules")
    # AI args -> constraints, then the user's own words are folded in
    ai = Constraints.from_dict({"tier": "sure", "target_odds": "5", "exclude_markets": ["Corners", "nonsense"]})
    check(ai.tier == "safe" and ai.target_odds == 5 and ai.exclude_markets == ["corners"]
          and ai.unknown == ["nonsense"], f"from_dict {ai.to_dict()}")
    ai.merge_hard(parse_text("sure 5 odds without unders"))
    check(set(ai.exclude_markets) == {"corners", "under"}, f"merge {ai.to_dict()}")
    c = Constraints.from_dict({"include_markets": ["under", "over"], "exclude_markets": ["unders"]})
    c.merge_hard(Constraints())
    check(c.include_markets == ["over"], "exclusion beats inclusion")


# ------------------------------------------------------------------ synthetic pool
KO = (datetime.now(timezone.utc) + timedelta(hours=20)).replace(minute=0, second=0, microsecond=0)


def mk_leg(i, mid, market, pick, odds, est, league="England · Premier League", cat="England", group="Main",
           verdict="confirmed", hours=0):
    home, away = f"Home{i}", f"Away{i}"
    l = Leg(event_id=f"sr:match:{i}", match=f"{home} vs {away}", home=home, away=away, league=league,
            kickoff=(KO + timedelta(hours=hours)).isoformat(), kickoff_local="Sun 01 Nov 16:00", market_id=mid,
            specifier="", outcome_id="1", market=market, pick=pick, odds=odds, p_market=est, p_feed=None,
            p_fit=est, p_stats=est, base=est, est=est, verdict=verdict, group=group, category=cat)
    l.tags = market_tags(mid, market, pick, group, "", home, away)
    return l


def synthetic_pool() -> Pool:
    pool = Pool(created=time.time(), day="tomorrow", events=[])
    for i in range(1, 25):
        legs = [
            # the strongest leg of every match is an Under (what the scanner would naturally pick first)
            mk_leg(i, "18", "Over/Under", "Under 4.5", 1.22, 0.90),
            mk_leg(i, "166", "Corners - Over/Under", "Under 12.5", 1.20, 0.89, group="Corners"),
            mk_leg(i, "10", "Double Chance", "Home or Draw", 1.18 + (i % 4) * 0.03, 0.84 - (i % 4) * 0.01),
            mk_leg(i, "18", "Over/Under", "Over 1.5", 1.20 + (i % 3) * 0.02, 0.83 - (i % 3) * 0.01),
            mk_leg(i, "1", "1X2", "Home", 1.30, 0.81, hours=i % 6),
            mk_leg(i, "139", "Bookings - Over/Under", "Over 1.5", 1.15, 0.86, group="Bookings"),
            mk_leg(i, "18", "Over/Under", "Over 2.5", 1.34, 0.70),        # below the Safe bar
        ]
        if i > 12:
            for l in legs:
                l.league, l.category = "Spain · LaLiga", "Spain"
        pool.legs[f"sr:match:{i}"] = legs
    pool.scanned = 24
    return pool


def test_scanner():
    sc = Scanner(None, None)
    pool = synthetic_pool()
    base = sc.build_slip(pool, "safe", 5)
    check(any(is_under(l) for l in base["legs"]), "baseline (no rules) does use unders in this pool")
    c = parse_text("give me sure 5 odds without under selections")
    res = sc.build_slip(pool, "safe", 5, c)
    legs = res["legs"]
    check(legs and not any(is_under(l) for l in legs), "no Under leg in a no-unders slip")
    check(all(l["est"] >= 0.80 for l in legs), "every leg >= 80% (Safe)")
    check(5 * 0.97 <= res["total_odds"] <= 5 * 1.25, f"total {res['total_odds']} lands on ~5")
    check(res["constraint_check"]["ok"] and res["constraint_check"]["target_reached"], f"check {res['constraint_check']}")
    check(len({l["event_id"] for l in legs}) == len(legs), "one leg per match")
    # include only double chance; no corners/cards; league + kickoff rules; leg counts and odds
    res = sc.build_slip(pool, "safe", 3, Constraints.from_dict(
        {"include_markets": ["double_chance"], "include_leagues": ["laliga"], "max_legs": 8}))
    check(res["legs"] and all(l["market"] == "Double Chance" and "LaLiga" in l["league"] for l in res["legs"])
          and len(res["legs"]) <= 8, "include DC + LaLiga + max legs")
    res = sc.build_slip(pool, "safe", 5, Constraints.from_dict({"exclude_markets": ["under", "corners", "cards"],
                                                                "min_leg_odds": 1.2}))
    check(res["legs"] and all(l["odds"] >= 1.2 and not {"under", "corners", "cards"} & set(l["tags"])
                              for l in res["legs"]), "exclude many + min leg odds")
    res = sc.build_slip(pool, "safe", 5, Constraints.from_dict({"min_legs": 9, "max_legs": 10, "exclude_markets": ["under"]}))
    check(9 <= len(res["legs"]) <= 10 and res["constraint_check"]["ok"], f"min/max legs {len(res['legs'])}")
    # rules that can't be met: honest short slip, never a violation
    res = sc.build_slip(pool, "safe", 50, Constraints.from_dict({"exclude_markets": ["under"], "max_legs": 4}))
    check(not res["reached"] and res["note"] and res["constraint_check"]["ok"] and len(res["legs"]) <= 4,
          "impossible target -> short + honest note")
    res = sc.build_slip(pool, "safe", 5, Constraints.from_dict({"include_markets": ["player_props"]}))
    check(not res["legs"] and "No legs qualify" in res["note"], "nothing qualifies -> empty with note")
    # kickoff window
    after = (KO + timedelta(hours=3)).astimezone(__import__("constraints").TZ).strftime("%H:%M")
    res = sc.build_slip(pool, "safe", 2, Constraints.from_dict({"include_markets": ["1x2"], "kickoff_after": after}))
    check(all(datetime.fromisoformat(l["kickoff"]) >= KO + timedelta(hours=3) for l in res["legs"]), "kickoff_after")
    # verify() flags a bad slip
    bad = {"legs": [mk_leg(1, "18", "Over/Under", "Under 2.5", 1.3, 0.85).to_json()], "target": 1.3}
    v = verify(bad, parse_text("no unders"))
    check(not v["ok"] and "excluded market type" in v["violations"][0], f"verify flags unders {v}")


def test_rollover():
    sc = Scanner(None, None)
    pool = synthetic_pool()

    async def fake_pool(day="today", *a, **k):
        return pool

    async def no_stats(p, evs):
        return None
    sc.pool = fake_pool
    sc.ensure_stats = no_stats
    from bookmakers import Event
    pool.events = [Event(event_id=f"sr:match:{i}", home=f"Home{i}", away=f"Away{i}",
                         league="Premier League" if i <= 12 else "LaLiga", category="England" if i <= 12 else "Spain",
                         tournament_id="t", start=KO, markets={}, market_count=100)
                   for i in range(1, 25)]
    for tgt in (2, 3, 5):
        r = asyncio.run(sc.rollover_slip(tgt))
        check(r["legs"] and not any(is_under(l) for l in r["legs"]) and r["constraint_check"]["ok"],
              f"rollover {tgt}: no unders")
        check(tgt * 0.97 <= r["total_odds"] <= tgt * 1.2, f"rollover {tgt} total {r['total_odds']}")
    r = asyncio.run(sc.rollover_slip(2, allow_unders=True))
    check(any(is_under(l) for l in r["legs"]), "rollover allow_unders=True may use unders")


# ------------------------------------------------------------------ AI tool path (fake Gemini)
def test_ai():
    from ai import Analyst
    from storage import Storage

    pool = synthetic_pool()
    sc = Scanner(None, None)
    seen = {}

    async def fake_scan(tier="safe", target=None, day="today", leagues=None, constraints=None):
        seen["c"] = constraints
        out = sc.build_slip(pool, tier, target, constraints)
        out["generated"] = "now"
        return out
    sc.scan = fake_scan
    booked = []

    class BM:
        load_code_url = "x"

        async def book(self, sels):
            booked.append(sels)
            return "TEST01"

        async def load_code(self, code):
            return {"legs": 0, "unavailable": 0}

    class S:
        storage = Storage(os.path.join(os.environ["DATA_DIR"], "t.sqlite"))
        scanner = sc
        bookmaker = BM()
        tracker = None

    an = Analyst(S(), api_key="fake")
    calls = iter([
        # the model "forgets" the no-unders rule in its arguments
        {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "scan_markets", "args": {"tier": "safe", "target_odds": 5}}}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Here is your safe slip (estimates)."}]}}]},
    ])

    async def fake_generate(contents):
        return next(calls)
    an._generate = fake_generate
    res = asyncio.run(an.ask(1, "give me sure 5 odds without under selections"))
    c = seen.get("c")
    check(c is not None and "under" in c.exclude_markets and c.tier == "safe" and c.target_odds == 5,
          f"server folded the user's rule in: {c and c.to_dict()}")
    sl = res["slip"]
    check(sl and sl["legs"] and not any(is_under(l) for l in sl["legs"]), "AI slip has no unders")
    check("Rules check" in res["reply"], "reply carries the server rules check line")
    check(not booked, "nothing booked in the AI test")
    # booking refuses a slip that breaks the user's words (an older slip with unders)
    old = sc.build_slip(pool, "safe", 5)
    sid = an.remember_slip(old)
    turn = {"slip": None, "code": None, "tools": [], "hard": parse_text("book it without unders")}
    r = asyncio.run(an.run_tool("build_booking_code", {"slip_id": sid}, turn))
    check("error" in r and "Refused" in r["error"] and not booked, f"refuse unders slip: {r}")
    # short slip is not booked without explicit acceptance
    short = sc.build_slip(pool, "safe", 50, Constraints.from_dict({"max_legs": 3}))
    sid = an.remember_slip(short)
    r = asyncio.run(an.run_tool("build_booking_code", {"slip_id": sid}, {"hard": Constraints()}))
    check("error" in r and "short" in r["error"] and not booked, f"short slip not booked: {r}")


if __name__ == "__main__":
    for f in (test_tags, test_parse, test_scanner, test_rollover, test_ai):
        f()
        print("ok" if not FAILS else "..", f.__name__)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'}")
    sys.exit(1 if FAILS else 0)
