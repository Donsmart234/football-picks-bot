"""football.com "Any Team Over 1.5" builder (anyteam.py) and the football.com adapter (offline, nothing sent or booked).

    python tests/test_anyteam.py

Covers: the FootballComNG adapter (base url, share link, relay off, parsing of a 60045 event detail shaped like the
real 2026-11-13 response), request parsing (legs, odds range, window, scope words, competitions, teams), the filters
(odds range, Yes only, one leg per match, not started, national-team scope incl. youth / women / clubs left out,
automatic switch to top leagues, named leagues / competitions / teams as HARD filters never padded), ranking by the
de-margined Yes, the booked flow (one code, fewer than asked said plainly, Refresh = new matches only, History record
marked football.com), the reply format (first line Rules: ..., code, total odds, first kickoff WAT, combined chance,
legs), Telegram buttons and HTML validity, chat routing phrases, the /api/anyteam list mode, 60045 settlement
(final + early), History links per bookmaker, and that no emoji appears anywhere. A fake chat id is used, never the
owner's.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import anyteam  # noqa: E402
from bookmakers import (BookingError, Event, FootballComNG, SportyBetNG, footballcom_share_url,  # noqa: E402
                        share_url_for)

FAILS: list[str] = []
TEST_CHAT = 990000777          # fake chat id, never the owner's
OWNER_CHAT = 8590854295
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "footballcom_60045_event.json")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U000E0000-\U000E007F\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u20E3"
                   "\u200D]")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  PASS", msg)


# ---------------------------------------------------------------- fixtures
NOW = datetime.now(timezone.utc)
INTL = ("International", "sr:category:4")
YOUTH = ("International Youth", "sr:category:392")
ENG = ("England", "sr:category:1")


def raw_detail(eid, yes=1.25, no=3.6, active=True, with_market=True, status=0, o1=(1.3, 5.0, 9.0), ou=(1.7, 2.1)):
    """An event detail shaped like football.com's factsCenter/event response (2026-11-13)."""
    mk = []
    if with_market:
        mk.append({"id": "60045", "specifier": "total=1.5", "product": 3, "desc": "Any Team Over 1.5", "status": 0,
                   "group": "Goals", "name": "Any Team Over X.5", "sourceType": "BET_RADAR",
                   "outcomes": [{"id": "74", "odds": f"{yes:.2f}", "probability": "0.75", "isActive": int(active),
                                 "desc": "Yes"},
                                {"id": "76", "odds": f"{no:.2f}", "probability": "0.25", "isActive": int(active),
                                 "desc": "No"}]})
        mk.append({"id": "60045", "specifier": "total=2.5", "status": 0, "desc": "Any Team Over 2.5",
                   "outcomes": [{"id": "74", "odds": "2.40", "isActive": 1, "desc": "Yes"},
                                {"id": "76", "odds": "1.50", "isActive": 1, "desc": "No"}]})
    mk.append({"id": "1", "specifier": "", "status": 0, "desc": "1X2", "outcomes": [
        {"id": "1", "odds": f"{o1[0]}", "isActive": 1}, {"id": "2", "odds": f"{o1[1]}", "isActive": 1},
        {"id": "3", "odds": f"{o1[2]}", "isActive": 1}]})
    mk.append({"id": "18", "specifier": "total=2.5", "status": 0, "desc": "Over/Under", "outcomes": [
        {"id": "12", "odds": f"{ou[0]}", "isActive": 1}, {"id": "13", "odds": f"{ou[1]}", "isActive": 1}]})
    return {"eventId": eid, "status": status, "markets": mk}


def mk_event(i, cat=INTL, tour=("sr:tournament:1848", "Africa Cup of Nations Qualification"), home=None, away=None,
             start=None):
    return Event(event_id=f"sr:match:{1000 + i}", home=home or f"Home{i}", away=away or f"Away{i}",
                 start=start or NOW + timedelta(hours=2 + i * 0.5), league=tour[1], category=cat[0],
                 tournament_id=tour[0], markets={}, home_id=f"sr:competitor:{5000 + i}",
                 away_id=f"sr:competitor:{6000 + i}", category_id=cat[1])


class FakeFC:
    """Fake football.com: list, event details, booking, load code."""
    BRAND = "football.com"
    parse_markets = staticmethod(SportyBetNG.parse_markets)

    def __init__(self, events, details, refuse_first=False):
        self.events, self.details, self.booked, self.refuse_first = events, details, [], refuse_first
        self.reads = 0

    async def list_events(self, hours=24, market_ids=None):
        return self.events

    async def _event_raw(self, eid):
        self.reads += 1
        return copy.deepcopy(self.details.get(eid, {"eventId": eid, "status": 0, "markets": []}))

    async def book(self, sels):
        if self.refuse_first and not self.booked:
            self.booked.append(None)
            raise BookingError("football.com refused the booking: odds changed (HTTP 200)")
        for s in sels:
            assert s.market_id == "60045" and s.specifier == "total=1.5" and s.outcome_id == "74"
        self.booked.append([s.event_id for s in sels])
        return f"FCT{len([b for b in self.booked if b]):03d}"

    async def load_code(self, code):
        return {"legs": len(self.booked[-1]), "unavailable": 0}

    def code_url(self, code):
        return footballcom_share_url(code)


class FakeTracker:
    def __init__(self, history=None):
        self.history, self.records = history, []

    async def record(self, legs, code, source, **kw):
        self.records.append({"legs": legs, "code": code, "source": source, **kw})
        return {"id": f"sl_test_{len(self.records)}"}


def make_svc(events, details, history=None, **kw):
    fc = FakeFC(events, details, **kw)
    svc = SimpleNamespace(footballcom=fc, bookmaker=None, anyteam=anyteam.AnyTeamFinder(fc), tracker=FakeTracker(history),
                          ai=None)
    return svc, fc


def national_world(n=14, odds=None):
    """n national-team matches in range, plus out-of-range, closed, no-market, youth, women, started, club ones."""
    evs, det = [], {}
    odds = odds or [1.16 + 0.013 * i for i in range(n)]
    for i in range(n):
        e = mk_event(i)
        evs.append(e)
        det[e.event_id] = raw_detail(e.event_id, yes=odds[i], no=1 / max(0.05, 1.06 - 1 / odds[i]))
    extra = [
        (mk_event(40), {"yes": 1.50, "no": 2.5}),                       # above range
        (mk_event(41), {"yes": 1.10, "no": 7.0}),                       # below range
        (mk_event(42), {"yes": 1.20, "no": 4.0, "active": False}),      # closed
        (mk_event(43), {"with_market": False}),                         # no 60045
        (mk_event(44, cat=YOUTH, tour=("sr:tournament:26", "U21 Euro Qualification"), home="Spain U21",
                  away="Japan U21"), {"yes": 1.17, "no": 5.0}),
        (mk_event(45, tour=("sr:tournament:851", "Int. Friendly Games, Women"), home="Nigeria", away="Ghana"),
         {"yes": 1.17, "no": 5.0}),
        (mk_event(46, start=NOW - timedelta(minutes=10)), {"yes": 1.17, "no": 5.0}),   # already started
        (mk_event(47, cat=ENG, tour=("sr:tournament:17", "Premier League"), home="Arsenal", away="Wolves"),
         {"yes": 1.17, "no": 5.0}),
    ]
    for e, kw in extra:
        evs.append(e)
        det[e.event_id] = raw_detail(e.event_id, **kw)
    return evs, det


# ---------------------------------------------------------------- adapter
def test_adapter():
    print("FootballComNG adapter")
    fc = FootballComNG(relay_url="")
    check(fc.BASE == "https://www.football.com/api/ng" and fc.name == "football.com NG" and fc.KEY == "footballcom",
          "base url / name / key")
    check(fc._url("/orders/share", {})[0] == "https://www.football.com/api/ng/orders/share", "direct url (no relay)")
    check(fc.HEADERS["Referer"] == "https://www.football.com/ng/" and fc.HEADERS["Origin"] == "https://www.football.com"
          and "Chrome" in fc.HEADERS["User-Agent"], "browser headers for football.com")
    check(fc.code_url("x4bd73ef") == "https://www.football.com/ng/?shareCode=X4BD73EF", "share link")
    check(share_url_for("footballcom", "ABC123") == "https://www.football.com/ng/?shareCode=ABC123"
          and share_url_for("", "ABC123").startswith("https://www.sportybet.com/?shareCode=ABC123"),
          "share_url_for picks the site by History bookmaker key")
    fc2 = FootballComNG(relay_url="https://relay.example/fc")
    url, q = fc2._url("/orders/share/X1", {})
    check(url == "https://relay.example/fc" and q["p"] == "/api/ng/orders/share/X1", "optional relay keeps p=/api/ng/...")
    sb = SportyBetNG(relay_url="")
    check(sb.BASE.startswith("https://www.sportybet.com") and sb.BRAND == "SportyBet", "SportyBet adapter unchanged")
    asyncio.run(fc.aclose())
    asyncio.run(fc2.aclose())
    asyncio.run(sb.aclose())
    data = json.load(open(FIX, encoding="utf-8"))
    mk = SportyBetNG.parse_markets(data)
    m = [x for x in mk if x["id"] == "60045" and x["specifier"] == "total=1.5"]
    check(len(m) == 1 and {o["id"] for o in m[0]["outcomes"]} == {"74", "76"}, "fixture: 60045 total=1.5 parsed")
    e = Event(event_id=data["eventId"], home="Nigeria", away="Tanzania", start=NOW + timedelta(hours=3),
              league="Africa Cup of Nations Qualification", category="International",
              tournament_id="sr:tournament:1848", category_id="sr:category:4")
    leg, why = anyteam.leg_from_detail(e, mk, 1.15, 1.35)
    check(leg is not None and leg["odds"] == 1.31 and leg["outcome_id"] == "74" and leg["specifier"] == "total=1.5"
          and leg["market_id"] == "60045", f"fixture: Yes @1.31 picked ({why})")
    check(0.70 < leg["est"] < 0.76 and leg["est"] < 1 / 1.31, f"de-margined Yes below the implied price ({leg['est']})")
    check(leg["p_model"] is not None and 0.4 < leg["p_model"] < 0.95, f"Poisson check present ({leg['p_model']})")
    _, why = anyteam.leg_from_detail(e, mk, 1.15, 1.30)
    check(why == "range", "1.31 is out of 1.15-1.30")


# ---------------------------------------------------------------- parsing
def test_parse():
    print("request parsing")
    r = anyteam.parse_request("")
    check((r.n, r.lo, r.hi, r.hours, r.scope) == (10, 1.15, 1.35, 72, "auto"), "defaults 10 legs, 1.15-1.35, 72 h, auto")
    r = anyteam.parse_request("any team over 1.5 8 legs odds 1.2-1.3")
    check((r.n, r.lo, r.hi) == (8, 1.2, 1.3), f"'8 legs odds 1.2-1.3' ({r.n}, {r.lo}, {r.hi})")
    r = anyteam.parse_request("12 1.18 - 1.4 nigeria egypt")
    check((r.n, r.lo, r.hi) == (12, 1.18, 1.4) and "nigeria" in r.team_text, "/anyteam 12 1.18 - 1.4 nigeria egypt")
    r = anyteam.parse_request("any team over 1.5 u21 games today")
    check(r.youth and r.day == "today" and r.n == 10, "u21 + today; the 21 in u21 is not a leg count")
    r = anyteam.parse_request("any team over 1.5 afcon qualifiers 48h")
    check([c[0] for c in r.competitions] == ["AFCON qualifiers"] and r.hours == 48 and r.named, "AFCON qualifiers, 48h")
    r = anyteam.parse_request("anyteam premier league and serie a")
    check(r.leagues_c.include_leagues == ["premier league", "serie a"] and r.named, "named leagues")
    check(anyteam.parse_request("any team over 1.5 top leagues").scope == "top", "top leagues word")
    check(anyteam.parse_request("any team over 1.5 national teams").scope == "national", "national word")
    check(anyteam.parse_request("any team over 1.5 all leagues").scope == "all", "all leagues word")
    check(anyteam.parse_request("refresh any team over 1.5").refresh, "refresh word")
    check(anyteam.parse_request("", n=99).n == anyteam.MAX_LEGS, "legs capped")
    check(anyteam.parse_request("1.35-1.15").lo == 1.15, "reversed odds range")
    for t in ("any team over 1.5", "Any Team Over 1.5 code 10 legs", "anyteam", "give me any team o1.5",
              "any-team over 1.5 national teams"):
        check(anyteam.wants_anyteam(t), f"routes: {t!r}")
    for t in ("over 1.5 goals", "5 min draw weekend", "any team to win", "safe 5 odds"):
        check(not anyteam.wants_anyteam(t), f"not routed: {t!r}")


# ---------------------------------------------------------------- filters / scope
def test_filters():
    print("filters and scope")
    evs, det = national_world()
    svc, fc = make_svc(evs, det)
    out = asyncio.run(anyteam.build(svc, TEST_CHAT, "", book=False))
    legs = out["legs"]
    check(out["status"] == "listed" and out["scope"] == "national" and len(legs) == 10, "national scope, 10 legs listed")
    check(all(1.15 <= l["odds"] <= 1.35 for l in legs), "every leg inside 1.15-1.35")
    check(len({l["event_id"] for l in legs}) == len(legs), "one leg per match")
    check(all(l["outcome_id"] == "74" and l["pick"] == "Yes" for l in legs), "Yes only")
    bad = {"sr:match:1040", "sr:match:1041", "sr:match:1042", "sr:match:1043", "sr:match:1044", "sr:match:1045",
           "sr:match:1046", "sr:match:1047"}
    pool_ids = {l["event_id"] for l in out["pool"]}
    check(not (pool_ids & bad), "out-of-range, closed, no-market, youth, women, started and club matches left out")
    ests = [l["est"] for l in out["pool"]]
    check(ests == sorted(ests, reverse=True), "ranked by estimated chance")
    check(legs[0]["odds"] == min(l["odds"] for l in out["pool"]), "shortest Yes ranks first")
    check(not out["rules"] or out["rules"].startswith("Any Team Over 1.5 (Yes), 10 legs, odds 1.15-1.35, national teams"),
          f"rules text ({out['rules']})")

    print("youth only when asked")
    out = asyncio.run(anyteam.build(svc, TEST_CHAT, "u21 national teams", book=False))
    check("sr:match:1044" in {l["event_id"] for l in out["pool"]}, "u21 asked -> youth national teams included")

    print("auto: no national teams -> top leagues")
    clubs = [mk_event(60 + i, cat=ENG, tour=("sr:tournament:17", "Premier League"), home=f"Club{i}", away=f"Club{i}b")
             for i in range(6)]
    lower = [mk_event(70 + i, cat=ENG, tour=("sr:tournament:173", "National League"), home=f"Low{i}", away=f"Low{i}b")
             for i in range(6)]
    d2 = {e.event_id: raw_detail(e.event_id, yes=1.2, no=4.5) for e in clubs + lower}
    svc2, _ = make_svc(clubs + lower, d2)
    out = asyncio.run(anyteam.build(svc2, TEST_CHAT, "", book=False))
    check(out["scope"] == "top" and len(out["legs"]) == 6 and all(l["event_id"] in {e.event_id for e in clubs}
                                                                     for l in out["legs"]),
          "top leagues only (6), lower league not used to pad")
    check("Only 6" in out["short_note"] and "not 10" in out["short_note"], "fewer than asked said plainly")
    check(out["rules"].endswith("top leagues"), "rules say top leagues")
    out = asyncio.run(anyteam.build(svc2, TEST_CHAT, "all leagues", book=False))
    check(out["scope"] == "all" and len(out["legs"]) == 10, "all leagues lifts the scope")

    print("hard filters: teams / competitions / leagues, never padded")
    evs3, det3 = national_world()
    evs3[0].home, evs3[0].away = "Nigeria", "Tanzania"
    evs3[1].home, evs3[1].away = "Egypt", "Malawi"
    evs3[2].tournament_id, evs3[2].league = "sr:tournament:23755", "UEFA Nations League"
    svc3, _ = make_svc(evs3, det3)
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "nigeria egypt", book=False))
    check(out["scope"] == "filter" and {l["home"] for l in out["legs"]} == {"Nigeria", "Egypt"},
          f"teams named -> only those 2 ({[l['home'] for l in out['legs']]})")
    check("only Nigeria, Egypt" in out["rules"] or "only Egypt, Nigeria" in out["rules"], f"rules ({out['rules']})")
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "nations league", book=False))
    check([l["event_id"] for l in out["legs"]] == [evs3[2].event_id], "Nations League named -> only that match")
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "premier league", book=False))
    check(out["scope"] == "filter" and [l["home"] for l in out["legs"]] == ["Arsenal"],
          "Premier League named -> only the EPL match, no national teams")
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "national teams no nigeria", book=False))
    check(out["scope"] == "national" and "Nigeria" not in {l["home"] for l in out["pool"]}, "'no nigeria' excludes")
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "1.40-1.60", book=False))
    check(all(1.4 <= l["odds"] <= 1.6 for l in out["pool"]) and out["pool"], "custom odds range applied")
    out = asyncio.run(anyteam.build(svc3, TEST_CHAT, "2.5-3.0", book=False))
    check(out["status"] == "none" and "no national teams match" in out["message"].lower().replace("no national teams",
                                                                                                 "no national teams"),
          f"nothing in range -> plain message ({out['message'][:80]})")


# ---------------------------------------------------------------- booking flow + reply
def tg_check(text, kb=None):
    from test_draw5_telegram import BadRequest, tg_validate
    try:
        tg_validate(text, "HTML", kb)
        return True
    except BadRequest as exc:
        return str(exc)


def test_booking_flow():
    print("booked flow (fake football.com, sqlite history)")
    from history import HistoryStore
    from storage import Storage
    path = os.path.join(tempfile.mkdtemp(), "at.sqlite3")
    hist = HistoryStore(storage=Storage(path), url="", key="")
    evs, det = national_world(n=14)
    svc, fc = make_svc(evs, det, history=hist, refuse_first=True)
    out = asyncio.run(anyteam.build(svc, TEST_CHAT, ""))
    check(out["status"] == "booked" and out["code"] == "FCT001" and out["n"] == 10, "one code of 10 (after one retry)")
    check(out["url"] == "https://www.football.com/ng/?shareCode=FCT001", "football.com share link")
    check(out["loaded"] == 10 and out["unavailable"] == 0, "load_code check recorded")
    rec = svc.tracker.records[-1]
    check(rec["source"] == "anyteam" and rec["bookmaker"] == "footballcom" and rec["code"] == "FCT001"
          and "football.com" in rec["label"], "saved to History, marked football.com")
    check(abs(out["total_odds"] - round(anyteam._product(out["legs"], "odds"), 2)) < 1e-9, "total odds = product")
    body = anyteam.code_html(out)
    lines = anyteam.code_plain(out).split("\n")
    check(lines[0] == "Rules: Any Team Over 1.5 (Yes), 10 legs, odds 1.15-1.35, national teams", f"first line ({lines[0]})")
    check("football.com code: FCT001" in lines, "code line says football.com")
    check(any(l.startswith("Total odds ") for l in lines) and any(l.startswith("First kickoff: ") and l.endswith(" WAT")
                                                                   for l in lines)
          and any(l.startswith("Combined estimated chance: ") for l in lines), "total odds, first kickoff WAT, chance")
    legl = [l for l in lines if re.match(r"^\d+\. ", l)]
    check(len(legl) == 10 and all(re.match(r"^\d+\. .+ v .+ @\d\.\d\d, \w{3} \d\d \w{3} \d\d:\d\d WAT$", l) for l in legl),
          f"legs: Home v Away @odds, kickoff WAT ({legl[0] if legl else ''})")
    check(not EMOJI.search(body), "no emoji in the reply")
    check(tg_check(body) is True, "valid Telegram HTML")

    print("refresh: new matches only, persisted")
    first = set(fc.booked[-1])
    out2 = asyncio.run(anyteam.build(svc, TEST_CHAT, "", refresh=True))
    check(out2["status"] == "booked" and not (set(fc.booked[-1]) & first) and out2["n"] == 4,
          f"refresh books the 4 unused matches ({out2['n']})")
    check("Only 4" in out2["short_note"] and "earlier codes" in out2["short_note"], "and says the count")
    out3 = asyncio.run(anyteam.build(svc, TEST_CHAT, "", refresh=True))
    check(out3["status"] == "exhausted" and "No new matches left" in out3["message"], "then: none left, said so")
    svc_b, fc_b = make_svc(evs, det, history=hist)        # new process, same store
    out4 = asyncio.run(anyteam.build(svc_b, TEST_CHAT, "", refresh=True))
    check(out4["status"] == "exhausted", "used ids survive a restart")
    out5 = asyncio.run(anyteam.build(svc_b, TEST_CHAT + 1, "", refresh=True))
    check(out5["status"] == "booked" and out5["n"] == 10, "another owner starts fresh")

    print("list mode books nothing")
    svc6, fc6 = make_svc(evs, det)
    out6 = asyncio.run(anyteam.build(svc6, TEST_CHAT, "", book=False))
    check(out6["status"] == "listed" and not fc6.booked and not svc6.tracker.records and len(out6["pool"]) == 14,
          "list: 10 picks + pool of 14, no booking, no History")
    check("Picks only (nothing booked)" in anyteam.code_plain(out6), "list reply says nothing was booked")

    print("errors are plain")

    class Down(FakeFC):
        async def list_events(self, hours=24, market_ids=None):
            raise RuntimeError("403")
    svc7 = SimpleNamespace(footballcom=Down([], {}), bookmaker=None, tracker=None, ai=None)
    svc7.anyteam = anyteam.AnyTeamFinder(svc7.footballcom)
    out7 = asyncio.run(anyteam.build(svc7, TEST_CHAT, ""))
    check(out7["status"] == "error" and "football.com didn't answer" in out7["message"], "site down -> plain message")

    class Blocked(FakeFC):
        async def _event_raw(self, eid):
            return None
    svc8, _ = make_svc(evs, det)
    svc8.footballcom = Blocked(evs, det)
    svc8.anyteam = anyteam.AnyTeamFinder(svc8.footballcom)
    out8 = asyncio.run(anyteam.build(svc8, TEST_CHAT, ""))
    check(out8["status"] == "error" and "blocking" in out8["message"], "every detail refused -> says so, not 'no match'")


def test_lister_fallback():
    print("list source: SportyBet list first, football.com list when it fails")

    class Lister:
        BRAND = "SportyBet"

        def __init__(self, evs, fail=False):
            self.evs, self.fail, self.calls = evs, fail, 0

        async def list_events(self, hours=24, market_ids=None):
            self.calls += 1
            if self.fail:
                raise RuntimeError("relay down")
            return self.evs
    evs, det = national_world()
    fc = FakeFC([], det)
    lst = Lister(evs)
    f = anyteam.AnyTeamFinder(fc, lister=lst)
    res = asyncio.run(f.find(anyteam.parse_request("")))
    check(lst.calls == 1 and res["list_source"] == "SportyBet" and res["qualifying"] == 14, "SportyBet list used")
    fc2 = FakeFC(evs, det)
    f2 = anyteam.AnyTeamFinder(fc2, lister=Lister([], fail=True))
    res2 = asyncio.run(f2.find(anyteam.parse_request("")))
    check(res2["list_source"] == "football.com" and res2["qualifying"] == 14, "fallback to football.com's list")


# ---------------------------------------------------------------- Telegram
def test_telegram():
    print("Telegram: /anyteam + Refresh callback (fake chat, nothing sent)")
    import handlers
    from test_draw5_telegram import TEST_CHAT as CB_CHAT, FakeMsg, make_ctx, run_callback   # callbacks come from CB_CHAT
    evs, det = national_world(n=14)
    svc, fc = make_svc(evs, det)
    ctx = make_ctx(svc, args=["10", "1.15-1.35"])
    log = []
    msg = FakeMsg(log)
    upd = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=CB_CHAT),
                          effective_chat=SimpleNamespace(id=CB_CHAT))
    asyncio.run(handlers.anyteam_cmd(upd, ctx))
    edits = [x for x in log if x[0] == "edit"]
    body = edits[-1][1] if edits else ""
    check(edits and edits[-1][2] == "HTML" and body.startswith("<b>Rules:</b> Any Team Over 1.5 (Yes), 10 legs"),
          "reply opens with the Rules line")
    check("football.com code: <code>FCT001</code>" in body, "football.com code shown")
    kb = edits[-1][3]
    btns = [b for row in kb.inline_keyboard for b in row]
    labels = [b.text for b in btns]
    check(labels == ["Open in football.com", "Refresh (new picks)", "Slip image"], f"buttons {labels}")
    check(btns[0].url == "https://www.football.com/ng/?shareCode=FCT001", "Open button = football.com share link")
    check(all(not b.callback_data or len(b.callback_data.encode()) <= 64 for b in btns), "callback data <= 64 bytes")
    check(btns[2].callback_data == "IMG|h|sl_test_1", "slip image from the History record")
    tok = next(iter(ctx.application.bot_data["anyteam"]))
    run_callback(ctx, f"AT|r|{tok}", msg)
    check(len(fc.booked) == 2 and not (set(fc.booked[0]) & set(fc.booked[1])), "Refresh books new matches only")
    texts = " ".join(str(x[1]) for x in log) + " ".join(b.text for x in log if x[3] for row in x[3].inline_keyboard
                                                        for b in row)
    check(not EMOJI.search(texts) and "Something went wrong" not in texts, "no emoji, no generic error")
    check(all(getattr(m, "chat_id", TEST_CHAT) != OWNER_CHAT for m in [msg]), "fake chat only")

    print("chat phrase routed without the AI")
    log2 = []
    msg2 = FakeMsg(log2)
    msg2.text = "any team over 1.5 5 legs"
    msg2.reply_to_message = None
    msg2.photo, msg2.caption = None, None
    upd2 = SimpleNamespace(effective_message=msg2, effective_user=SimpleNamespace(id=TEST_CHAT),
                           effective_chat=SimpleNamespace(id=TEST_CHAT))
    svc2, fc2 = make_svc(evs, det)
    asyncio.run(handlers.free_text(upd2, make_ctx(svc2)))
    check(len(fc2.booked) == 1 and len(fc2.booked[0]) == 5, "'any team over 1.5 5 legs' -> one code of 5")
    check("/anyteam" in handlers.HELP, "help lists /anyteam")


# ---------------------------------------------------------------- web API
def test_webapp():
    print("/api/anyteam list mode (no booking)")
    import webapp
    evs, det = national_world(n=14)
    svc, fc = make_svc(evs, det)

    class Req(dict):
        def __init__(self, body):
            super().__init__(tg_user={"id": TEST_CHAT, "admin": True})
            self.body = body

        async def json(self):
            return self.body
    wa = webapp.MiniApp.__new__(webapp.MiniApp)
    wa.s = svc
    resp = asyncio.run(wa.anyteam(Req({"list": True, "legs": 6, "min_odds": 1.15, "max_odds": 1.3})))
    raw = resp.body
    if resp.headers.get("Content-Encoding") == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    check(data["status"] == "listed" and data["n"] == 6 and not fc.booked, "list: 6 picks, nothing booked")
    check(all(1.15 <= l["odds"] <= 1.3 for l in data["legs"]) and data["reply"].startswith("Rules: Any Team Over 1.5"),
          "odds from the request, reply text included")


# ---------------------------------------------------------------- settlement / history links / emoji
def test_settle():
    print("settlement of 60045")
    from settle import MatchResult, early_settle, settle_leg
    leg = {"market_id": "60045", "specifier": "total=1.5", "outcome_id": "74"}
    no = dict(leg, outcome_id="76")
    cases = [((2, 0), "won"), ((0, 2), "won"), ((1, 1), "lost"), ((0, 0), "lost"), ((3, 3), "won"), ((1, 0), "lost")]
    for ft, want in cases:
        st, _ = settle_leg(leg, MatchResult(state="ended", ft=ft))
        st2, _ = settle_leg(no, MatchResult(state="ended", ft=ft))
        check(st == want and st2 == {"won": "lost", "lost": "won"}[want], f"FT {ft[0]}-{ft[1]}: Yes {want}")
    st, _ = settle_leg(dict(leg, specifier="total=2.5"), MatchResult(state="ended", ft=(2, 1)))
    check(st == "lost", "total=2.5 line respected (2-1 -> Yes lost)")
    e = early_settle(leg, MatchResult(state="live", live=(2, 0), phase="2h", minute=60))
    check(e and e[0] == "won", "live 2-0: Yes won early")
    check(early_settle(leg, MatchResult(state="live", live=(1, 1), phase="2h", minute=60)) is None, "live 1-1: wait")
    e = early_settle(no, MatchResult(state="live", live=(0, 2), phase="1h", minute=30))
    check(e and e[0] == "lost", "live 0-2: No lost early")
    check(early_settle(leg, MatchResult(state="live", live=(2, 0), phase="other")) is None, "no call in extra time")


def test_history_links():
    print("History links per bookmaker")
    from fmt import code_html, record_text
    from history import slip_record
    rec = slip_record([{"event_id": "sr:match:1", "odds": 1.2, "market_id": "60045", "outcome_id": "74"}], "FCX123",
                      "anyteam", bookmaker="footballcom")
    check(rec["data"]["bookmaker"] == "footballcom", "slip_record keeps the bookmaker")
    check("football.com/ng/?shareCode=FCX123" in record_text(rec, "t"), "record text links to football.com")
    plain = slip_record([], "SBX123", "app")
    check("bookmaker" not in plain["data"] and "sportybet.com/?shareCode=SBX123" in code_html("SBX123"),
          "SportyBet records unchanged")
    js = open(os.path.join(os.path.dirname(__file__), "..", "webapp_static", "app.js"), encoding="utf-8").read()
    check("football.com/ng/?shareCode=" in js and "openChip(d.code, d.bookmaker)" in js and '"/api/anyteam"' in js,
          "Mini App: football.com links for football.com records + Any Team scanner")


def test_no_emoji_source():
    print("no emoji in the new source")
    root = os.path.join(os.path.dirname(__file__), "..")
    for f in ("anyteam.py", "bookmakers.py", "handlers.py", "webapp_static/app.js"):
        txt = open(os.path.join(root, f), encoding="utf-8").read()
        check(not EMOJI.search(txt), f"{f}: no emoji")


if __name__ == "__main__":
    test_adapter()
    test_parse()
    test_filters()
    test_booking_flow()
    test_lister_fallback()
    test_telegram()
    test_webapp()
    test_settle()
    test_history_links()
    test_no_emoji_source()
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
