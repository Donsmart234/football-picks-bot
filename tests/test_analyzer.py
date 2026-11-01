"""Offline tests for the bet analyzer and the top-leagues allowlist (no network, no booking, no Telegram).

    python tests/test_analyzer.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_KEY"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import analyzer as A  # noqa: E402
import leagues  # noqa: E402
from bookmakers import Event  # noqa: E402
from constraints import Constraints, parse_text  # noqa: E402
from scanner import Leg, Scanner  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def share(code):
    return json.loads((FIX / f"share_{code}.json").read_text())["data"]


# ------------------------------------------------------------------ codes
def test_codes():
    print("codes")
    check(A.bare_code("N6RXGP") == "N6RXGP", "bare code with digit")
    check(A.bare_code(" ngka1g ") == "NGKA1G", "lower-case code with digit, trimmed")
    check(A.bare_code("ABCDEF") == "ABCDEF", "all-caps 6 letters")
    check(A.bare_code("thanks") is None and A.bare_code("Arsena") is None, "ordinary words go to the AI")
    check(A.bare_code("N6RXGP please") is None and A.bare_code("N6RXG") is None, "only a bare 6-char message")
    check(A.extract_code("http://www.sportybet.com/ng/?shareCode=N6RXGP") == "N6RXGP", "share link")
    check(A.extract_code("N6RXGP no unders") == "N6RXGP", "code + rules")


# ------------------------------------------------------------------ share parsing
def test_parse():
    print("parse_share")
    legs = A.parse_share(share("N6RXGP"))
    check(len(legs) == 2 and all(l["state"] == "ended" for l in legs), "N6RXGP: 2 ended legs")
    check([l["result"] for l in legs] == ["won", "lost"], f"isWinning -> won/lost {[l['result'] for l in legs]}")
    check(legs[0]["tournament_id"] == "sr:tournament:35" and legs[0]["category_id"] == "sr:category:30",
          "Sportradar tournament + category ids kept")
    legs = A.parse_share(share("NGKA1G"))
    check(len(legs) == 4 and all(l["state"] == "upcoming" for l in legs), "NGKA1G: 4 upcoming legs")
    check(legs[0]["pick"] == "Como 1907" and legs[0]["market"] == "Draw No Bet", f"pretty pick {legs[0]['pick']}")
    check(legs[2]["pick"] == "Academico de Viseu FC Over 0.5", "team total pick names the team")
    legs = A.parse_share(share("P45SST"))
    check(sum(1 for l in legs if l["state"] == "unavailable") == 1, "P45SST: unavailable selection kept")
    prop = [l for l in legs if "playerprops" in l["specifier"]]
    check(prop and prop[0]["result"] == "void", "player prop with refundFactor 1 = void")


# ------------------------------------------------------------------ rating / grading
def test_rating():
    print("rate_leg / grade")
    check(A.rate_leg(0.86, -0.02, "confirmed") == "Strong", "86% fair price -> Strong")
    check(A.rate_leg(0.70, -0.03, "confirmed") == "OK", "70% -> OK")
    check(A.rate_leg(0.84, -0.10, "confirmed") == "OK", "overpriced (value -10%) drops one step")
    check(A.rate_leg(0.84, -0.02, "demoted") == "OK", "demoted drops one step")
    check(A.rate_leg(0.55, 0.0, "") == "Weak", "55% -> Weak")
    check(A.rate_leg(0.30, 0.05, "") == "Avoid", "30% -> Avoid")
    check(A.rate_leg(0.90, 0.0, "dropped") == "Weak", "stats-dropped leg is at best Weak")
    check(A.rate_leg(None, None, rated=False) == A.NOT_RATED, "unrated")

    def leg(i, est, odds, rating, **kw):
        return {"idx": i, "est": est, "odds": odds, "rating": rating, "rated": True, "state": "upcoming",
                "result": None, "match": f"M{i}", "pick": f"P{i}", "event_id": f"sr:match:{i}", "market_id": "1",
                "outcome_id": "1", "specifier": "", **kw}
    strong = [leg(i, 0.86, 1.13, "Strong") for i in range(4)]
    g = A.grade_slip(strong)
    check(g["grade"] == "A", f"4 Strong legs ~55% -> A ({g})")
    long = [leg(i, 0.33, 2.9, "Avoid") for i in range(5)]
    check(A.grade_slip(long)["grade"] == "F", "5 Avoid legs -> F")
    mixed = strong[:3] + [leg(3, 0.33, 2.9, "Avoid")]
    check(A.grade_slip(mixed)["grade"] in ("C", "D", "F"), f"an Avoid leg caps at C ({A.grade_slip(mixed)})")
    settled = [dict(leg(0, None, 1.5, "Not rated"), result="won", rated=False, state="ended"),
               dict(leg(1, None, 1.5, "Not rated"), result="lost", rated=False, state="ended")]
    check(A.combined(settled)["chance"] == 0.0 and A.grade_slip(settled)["grade"] is None,
          "a lost leg -> 0% and no grade")
    thin = [leg(0, 0.86, 1.13, "Strong"), dict(leg(1, None, 2.0, "Not rated"), rated=False, est=None)]
    c = A.combined(thin)
    check(c["approx_legs"] == 1 and abs(c["chance"] - 0.86 / (2.0 * 1.06)) < 1e-9,
          "not-rated leg counted at de-margined price and flagged")

    # suggestions
    alt = {**leg(1, 0.80, 1.21, "Strong"), "pick": "Over 1.5", "market": "Over/Under", "specifier": "total=1.5",
           "market_id": "18", "outcome_id": "12"}
    legs = [leg(0, 0.86, 1.13, "Strong"),
            {**leg(1, 0.59, 1.63, "Weak"), "alternatives": [alt]},
            leg(2, 0.33, 2.98, "Avoid"),
            {**leg(3, 0.0, 0, "Not rated"), "state": "unavailable", "rated": False, "est": None, "odds": None}]
    sg = A.suggestions(legs)
    check(sg["drops"] and sg["drops"][0]["idx"] == 2, "weakest leg suggested as the first drop")
    check(abs(sg["drops"][0]["chance"] - round(0.86 * 0.59, 4)) < 1e-4, "drop recomputes the combined chance")
    check(sg["swaps"] and sg["swaps"][0]["to"].startswith("Over 1.5"), "safer swap offered")
    imp = sg["improved"]
    check(imp and len(imp["legs"]) == 2 and imp["legs"][1]["pick"] == "Over 1.5", "improved: swap Weak, drop Avoid")
    check(abs(imp["chance"] - 0.86 * 0.80) < 1e-4 and imp["chance"] > imp["base_chance"], "improved chance higher")
    check(any("unavailable" in c for c in imp["changes"]), "unavailable selection dropped")
    check(imp["bookable"], "improved slip bookable without rules")
    sg2 = A.suggestions(legs, parse_text("no overs"))
    check(not sg2["improved"]["bookable"], "improved slip that breaks the user's rules isn't bookable")


def test_related():
    print("safer alternatives")

    def L(mid, spec, oid, pick, odds, est, side=None, verdict="confirmed", level="model"):
        return Leg(event_id="sr:match:1", match="A vs B", home="A", away="B", league="England · Premier League",
                   kickoff="", kickoff_local="", market_id=mid, specifier=spec, outcome_id=oid, market="m", pick=pick,
                   odds=odds, p_market=est, p_feed=None, p_fit=None, est=est, verdict=verdict, side=side, level=level)
    o25 = L("18", "total=2.5", "12", "Over 2.5", 1.85, 0.55)
    o15 = L("18", "total=1.5", "12", "Over 1.5", 1.25, 0.78)
    o35 = L("18", "total=3.5", "12", "Over 3.5", 3.2, 0.28)
    u35 = L("18", "total=3.5", "13", "Under 3.5", 1.3, 0.72)
    away = L("1", "", "3", "B", 3.0, 0.32, side="away")
    x2 = L("10", "", "11", "Draw or B", 1.55, 0.62, side="away")
    dnb_h = L("11", "", "4", "A", 1.4, 0.68, side="home")
    check(A.related(o25, o15) and not A.related(o25, o35), "Over 2.5 -> Over 1.5, not Over 3.5")
    check(not A.related(o25, u35), "an over never becomes an under")
    check(A.related(away, x2) and not A.related(away, dnb_h), "away win -> X2, never the other side")
    sw = A.best_swaps(o25, [o15, o35, u35])
    check(sw == [o15], "best swap is Over 1.5")
    check(A.best_swaps(o25, [o15], parse_text("no overs")) == [], "swaps obey the user's rules")


def test_grounding():
    print("narrative grounding")
    facts = {"combined_chance": "46%", "legs": [{"est": "88%"}]}
    check(A.grounded("About 46% overall; the DNB leg is 88%.", facts), "numbers from facts pass")
    check(not A.grounded("A 70% chance.", facts), "invented percentage rejected")


# ------------------------------------------------------------------ top leagues
def test_leagues():
    print("top-leagues allowlist")
    check(leagues.is_top_ids("sr:tournament:17", "sr:category:1"), "EPL by id")
    check(leagues.is_top_ids("sr:tournament:34480", "sr:category:393"), "UECL id 34480")
    check(leagues.is_top_ids("sr:tournament:16", "sr:category:4"), "World Cup")
    check(not leagues.is_top_ids("sr:tournament:20808", "sr:category:31"), "Serie A Women excluded")
    check(not leagues.is_top_ids("sr:tournament:851", "sr:category:4"), "international friendlies excluded")
    check(not leagues.is_top_ids("sr:tournament:17", "sr:category:99"), "category id must match")
    check(not leagues.is_top_ids("sr:tournament:19", "sr:category:1", "Arsenal U21", "Fulham"), "youth side guard")
    check(not leagues.is_top_ids("sr:tournament:331", "sr:category:22"), "Scottish Challenge Cup not top")
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    evs = [Event("sr:match:1", "Arsenal", "Chelsea", ko, "Premier League", "England", "sr:tournament:17",
                 category_id="sr:category:1"),
           Event("sr:match:2", "Perth", "Adelaide", ko, "A-League", "Australia", "sr:tournament:136",
                 category_id="sr:category:34"),
           Event("sr:match:3", "Roma W", "Lazio W", ko, "Serie A, Women", "Italy", "sr:tournament:20808",
                 category_id="sr:category:31")]
    sc = Scanner(None, None)
    check([e.event_id for e in sc.select(evs, None, 80)] == ["sr:match:1"], "default scan = top leagues only")
    c = parse_text("give me safe 5 odds from all leagues")
    check(c.all_leagues and c.widens_leagues(), "'all leagues' parsed")
    check(len(sc.select(evs, None, 80, Constraints(all_leagues=True))) == 3, "all_leagues widens the scan")
    check([e.event_id for e in sc.select(evs, None, 80, Constraints(include_countries=["Australia"]))]
          == ["sr:match:2"], "a named country widens to that country")
    check([e.event_id for e in sc.select(evs, None, 80, parse_text("only top leagues no unders"))] == ["sr:match:1"],
          "'top leagues' keeps the default")
    check(Constraints(include_leagues=["top leagues"]).leg_ok(
        {"league": "England · Premier League", "tournament_id": "sr:tournament:17", "category_id": "sr:category:1",
         "est": 0.9, "odds": 1.2, "tags": ("1x2",)}), "top-leagues rule matches by id")


# ------------------------------------------------------------------ history: analyzed codes, no double ping
def test_history():
    print("history")
    from storage import Storage
    from history import HistoryStore, slip_record

    st = Storage(os.path.join(os.environ["DATA_DIR"], "h.sqlite"))
    hs = HistoryStore(st, url="", key="")

    class TR:
        history = hs

        async def record(self, *a, **k):
            raise AssertionError("must not book/record in this test")

    class BM:
        load_code_url = "x"

        async def share(self, code):
            return {"ok": True, "data": share("N6RXGP")} if code == "N6RXGP" else {
                "ok": False, "kind": "invalid", "error": "The code is invalid (wrong, expired or never booked)."}

        async def event_detail(self, eid):
            return None, [], {}      # header only for finished legs; never re-priced

    class S:
        bookmaker = BM()
        tracker = TR()
        ai = None

    an = A.Analyzer(S())
    r = asyncio.run(an.analyze("N6RXGP"))
    check(r["ok"] and r["summary"]["status"] == "lost" and r["summary"]["grade"] is None, "finished slip: lost")
    check(r["recommendation"]["kind"] == "settled" and r["recommendation"]["text"].startswith("❌"),
          f"settled verdict: {r['recommendation']['text']}")
    rows = asyncio.run(hs.list("slip", None, 50))
    check(rows == [] and "history" not in r, "analyzed code NOT saved to history / not tracked")
    booked = slip_record([{"event_id": "sr:match:9", "market_id": "1", "outcome_id": "1", "odds": 1.5}], "ABC123",
                         "app", "safe")
    asyncio.run(hs.save(booked))
    found = asyncio.run(hs.find_code("abc123"))
    check(found and found["id"] == booked["id"], "find_code (read-only lookup of booked odds) is case-insensitive")
    bad = asyncio.run(an.analyze("ZZZZZ9"))
    check(not bad["ok"] and bad["kind"] == "invalid" and "invalid" in bad["error"], "invalid code reported")
    from fmt import analysis_messages
    msgs = analysis_messages(r)
    check(msgs and all(len(m) <= 4096 for m in msgs) and "Lost" in msgs[0] and "Already lost" in msgs[-1],
          "Telegram text renders and ends with the verdict")


def test_verdict():
    print("verdict line")

    def leg(i, est, odds, rating):
        return {"idx": i, "est": est, "odds": odds, "rating": rating, "rated": True, "state": "upcoming",
                "result": None, "match": f"Home{i} vs Away{i}", "pick": f"P{i}", "event_id": f"sr:match:{i}",
                "market_id": "1", "outcome_id": "1", "specifier": "", "alternatives": []}

    def res(legs):
        sg = A.suggestions(legs)
        g = A.grade_slip(legs)
        counts = {r: sum(1 for l in legs if l["rating"] == r) for r in (*A.RATINGS, A.NOT_RATED)}
        s = {"grade": g["grade"], "chance": A.combined(legs)["chance"], "status": "not started", "ratings": counts,
             "total_odds": A.total_odds(legs)}
        return {"legs": legs, "summary": s, "suggestions": sg}
    good = [leg(i, 0.86, 1.13, "Strong") for i in range(4)]
    check(A.recommendation(res(good))["kind"] == "go", "4 Strong legs -> ✅ go")
    fixable = good[:3] + [leg(3, 0.33, 2.9, "Avoid")]
    r = A.recommendation(res(fixable))
    check(r["kind"] == "fix" and "dropping Home3 v Away3" in r["text"], f"one Avoid leg -> ⚠️ after dropping it: {r['text']}")
    junk = [leg(i, 0.33, 2.9, "Avoid") for i in range(5)]
    check(A.recommendation(res(junk))["kind"] == "skip", "5 long shots -> ❌ skip")


if __name__ == "__main__":
    for t in (test_codes, test_parse, test_rating, test_related, test_grounding, test_leagues, test_history, test_verdict):
        t()
    print(f"\nFAILURES: {len(FAILS)}")
    sys.exit(1 if FAILS else 0)
