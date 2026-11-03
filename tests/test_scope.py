"""Named-scope rules are HARD filters (offline, no network, no Gemini, no booking).

    python tests/test_scope.py

Bug 2026-11-03: "Champions League selections" came back with picks from other leagues. Causes: the deterministic
parser only read a league after "only / from / in" or before "only / games / matches", so "champions league
selections", "ucl picks", "only Europa" set no rule; and league text was matched as a substring, so a model call with
"Champions League" also pulled in AFC Champions League Elite / Gulf Club Champions League. Covers: aliases, id-based
matching, the AI tool path (model forgets / widens the league), list_matches, no-UCL-today message with the nearest
day, team filter, mixed rules, slip-edit scope.
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from bookmakers import Event  # noqa: E402
from constraints import TZ, Constraints, competition, detect_teams, is_under, market_tags, parse_text  # noqa: E402
from scanner import Leg, Pool, Scanner  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)


NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(TZ).date()
# kick-offs late today (local) so "today" windows have them; tomorrow evening for the next-day case
KO_TODAY = max(NOW + timedelta(hours=1), datetime.combine(TODAY, datetime.min.time(), TZ).replace(hour=21)
               .astimezone(timezone.utc))
if KO_TODAY.astimezone(TZ).date() != TODAY:
    KO_TODAY = NOW + timedelta(minutes=20)
KO_TMR = datetime.combine(TODAY + timedelta(days=1), datetime.min.time(), TZ).replace(hour=21).astimezone(timezone.utc)

COMPS = {  # tid: (category, category id, league name)
    7: ("International Clubs", 393, "UEFA Champions League"),
    679: ("International Clubs", 393, "UEFA Europa League"),
    463: ("International Clubs", 393, "AFC Champions League Elite"),
    53692: ("International Clubs", 393, "Gulf Club Champions League"),
    32303: ("Simulated Reality League", 2000, "UEFA Champions League SRL"),
    17: ("England", 1, "Premier League"),
    8: ("Spain", 32, "LaLiga"),
}


def ev(i, tid, home, away, ko):
    cat, cid, lg = COMPS[tid]
    return Event(event_id=f"sr:match:{i}", home=home, away=away, start=ko, league=lg, category=cat,
                 tournament_id=f"sr:tournament:{tid}", category_id=f"sr:category:{cid}", markets={}, market_count=200)


def events(ucl_today=True):
    ucl = [("Barcelona", "Aston Villa"), ("Atletico Madrid", "Bayern Munich"), ("Man Utd", "Roma"),
           ("Villarreal", "PSG"), ("Feyenoord", "Inter")]
    out, i = [], 1
    for h, a in ucl:
        out.append(ev(i, 7, h, a, KO_TODAY if ucl_today else KO_TMR)); i += 1
    out.append(ev(i, 7, "AEK Athens", "Real Madrid", KO_TMR)); i += 1
    for h, a in [("Al Hilal", "Al Sadd"), ("Al Ahli", "Persepolis"), ("Ulsan", "Buriram")]:
        out.append(ev(i, 463, h, a, KO_TODAY)); i += 1
    out.append(ev(i, 53692, "Al Wasl", "Kuwait SC", KO_TODAY)); i += 1
    out.append(ev(i, 32303, "Barcelona SRL", "Inter SRL", KO_TODAY)); i += 1
    for h, a in [("Arsenal", "Everton"), ("Chelsea", "Brentford"), ("Liverpool", "Fulham"), ("Newcastle", "Wolves"),
                 ("Brighton", "Leeds"), ("Man City", "Burnley")]:
        out.append(ev(i, 17, h, a, KO_TODAY)); i += 1
    for h, a in [("Rennes", "Olympiacos"), ("Crystal Palace", "Hoffenheim")]:
        out.append(ev(i, 679, h, a, KO_TODAY)); i += 1
    for h, a in [("Sevilla", "Getafe"), ("Valencia", "Osasuna")]:
        out.append(ev(i, 8, h, a, KO_TODAY)); i += 1
    return out


def legs_for(e: Event) -> list[Leg]:
    def mk(mid, market, pick, odds, est, group="Main"):
        l = Leg(event_id=e.event_id, match=e.name, home=e.home, away=e.away, league=e.league_full,
                kickoff=e.start.isoformat(), kickoff_local=e.start.astimezone(TZ).strftime("%a %d %b %H:%M"),
                market_id=mid, specifier="", outcome_id="1", market=market, pick=pick, odds=odds, p_market=est,
                p_feed=None, p_fit=est, p_stats=est, base=est, est=est, verdict="confirmed", group=group,
                category=e.category, utid=e.tournament_id, category_id=e.category_id)
        l.tags = market_tags(mid, market, pick, group, "", e.home, e.away)
        return l
    # the AFC / Gulf matches carry the *strongest* legs, so any leak would be picked first
    boost = 0.06 if "AFC" in e.league or "Gulf" in e.league else 0.0
    return [mk("18", "Over/Under", "Under 4.5", 1.22, 0.88 + boost),
            mk("10", "Double Chance", "Home or Draw", 1.25, 0.84 + boost),
            mk("18", "Over/Under", "Over 1.5", 1.28, 0.83 + boost),
            mk("1", "1X2", "Home", 1.55, 0.66)]


class FakeBM:
    load_code_url = "x"

    def __init__(self, evs):
        self.evs = evs
        self.booked = []

    async def list_events(self, hours=24):
        lim = NOW + timedelta(hours=hours)
        return [e for e in self.evs if e.start <= lim]

    async def book(self, sels):
        self.booked.append(sels)
        return "TEST01"

    async def load_code(self, code):
        return {}


def scanner(evs):
    bm = FakeBM(evs)
    sc = Scanner(bm, None)

    async def fake_pool(day="today", leagues=None, cap=80, rules=None, fresh=False):
        rules = rules if rules is not None and rules.match_key() else None
        wins = await sc.window_events(day)
        sel = sc.select(wins, leagues, cap, rules)
        p = Pool(created=time.time(), day=day, events=sel)
        for e in sel:
            p.legs[e.event_id] = legs_for(e)
        p.scanned = len(sel)
        return p
    sc.pool = fake_pool
    return sc, bm


def ucl_only(legs):
    return bool(legs) and all(l["league"].endswith("· UEFA Champions League") for l in legs)


# ------------------------------------------------------------------ parsing / aliases
def test_aliases():
    for txt, key in [("champions league selections", "ucl"), ("UCL", "ucl"), ("ucl picks", "ucl"),
                     ("give me Champions League games", "ucl"), ("only Europa", "uel"), ("europa league picks", "uel"),
                     ("UEL tips", "uel"), ("conference league slip", "uecl"), ("UECL picks", "uecl"),
                     ("Premier League only", "epl"), ("EPL safe 3 odds", "epl"), ("la liga picks", "laliga"),
                     ("serie a games", "serie a"), ("bundesliga selections", "bundesliga"),
                     ("ligue 1 picks", "ligue 1"), ("2 odds from the champions-league", "ucl")]:
        c = parse_text(txt)
        check(len(c.include_leagues) == 1 and competition(c.include_leagues[0]) == key,
              f"alias {txt!r} -> {c.include_leagues}")
    check(competition("AFC Champions League") == "afc champions league", "AFC CL is its own competition")
    c = parse_text("afc champions league picks")
    check([competition(x) for x in c.include_leagues] == ["afc champions league"], f"afc parse {c.include_leagues}")
    c = parse_text("ucl and europa picks tomorrow")
    check({competition(x) for x in c.include_leagues} == {"ucl", "uel"} and c.day == "tomorrow", "two leagues")
    c = parse_text("safe picks, no premier league")
    check(not c.include_leagues and [competition(x) for x in c.exclude_leagues] == ["epl"], "negated league")
    c = parse_text("not interested in epl, give me ucl")
    check([competition(x) for x in c.include_leagues] == ["ucl"] and
          [competition(x) for x in c.exclude_leagues] == ["epl"], f"mixed include/exclude {c.to_dict()}")
    c = parse_text("champions league selections today, 4 legs, odds 1.2 to 1.5 each, no unders, after 20:00")
    check(c.min_legs == c.max_legs == 4 and (c.min_leg_odds, c.max_leg_odds) == (1.2, 1.5) and
          "under" in c.exclude_markets and c.kickoff_after == "20:00" and c.day == "today" and
          competition(c.include_leagues[0]) == "ucl", f"mixed parse {c.to_dict()}")
    check("UEFA Champions League" in parse_text("ucl picks").describe(), "describe shows the competition label")
    names = [x for e in events() for x in (e.home, e.away)]
    inc, exc = detect_teams("give me picks from Barcelona and Bayern games", names)
    check(set(inc) == {"Barcelona", "Bayern Munich"} and not exc, f"teams {inc} {exc}")
    inc, exc = detect_teams("safe slip, avoid man utd", names)
    check(exc == ["Man Utd"] and not inc, f"team exclusion {inc} {exc}")
    inc, exc = detect_teams("who wins barcelona vs aston villa?", names)
    check(not inc and not exc, "a plain question is not a scope rule")
    from constraints import _team_match
    check(not _team_match("Inter", "Inter Club de Escaldes") and _team_match("Bayern", "Bayern Munich") and
          not _team_match("Real Madrid", "Real Sociedad") and _team_match("Man Utd", "Manchester United"),
          "team matching: same club only")
    # user's named league replaces a model's wider list
    m = Constraints.from_dict({"include_leagues": ["Champions League", "AFC Champions League"]})
    m.merge_hard(parse_text("champions league selections"))
    check([competition(x) for x in m.include_leagues] == ["ucl"], f"user league wins {m.include_leagues}")


# ------------------------------------------------------------------ id matching
def test_matching():
    c = Constraints.from_dict({"include_leagues": ["Champions League"]})
    ok = {e.league for e in events() if c.event_ok(e)}
    check(ok == {"UEFA Champions League"}, f"'Champions League' by id -> {ok}")
    for q in ("UCL", "uefa champions league", "UEFA Champions League", "champions league"):
        check(Constraints.from_dict({"include_leagues": [q]}).event_ok(ev(1, 7, "A", "B", KO_TODAY)) and
              not Constraints.from_dict({"include_leagues": [q]}).event_ok(ev(2, 463, "A", "B", KO_TODAY)) and
              not Constraints.from_dict({"include_leagues": [q]}).event_ok(ev(3, 32303, "A", "B", KO_TODAY)),
              f"{q}: UCL yes, AFC/SRL no")
    leg = {"league": "International Clubs · UEFA Champions League", "est": 0.9, "odds": 1.2, "tags": ("1x2",)}
    check(c.leg_ok(leg), "leg dict without an id: exact name match")
    leg2 = {"league": "International Clubs · AFC Champions League Elite", "est": 0.9, "odds": 1.2, "tags": ("1x2",)}
    check(not c.leg_ok(leg2), "leg dict AFC without id: rejected")
    check(Constraints.from_dict({"include_leagues": ["Premier League"]}).event_ok(ev(9, 17, "A", "B", KO_TODAY)),
          "EPL by id")


# ------------------------------------------------------------------ scanner
def test_scanner():
    sc, _ = scanner(events())
    res = asyncio.run(sc.scan("safe", 3, "today", None, parse_text("champions league selections")))
    check(ucl_only(res["legs"]), f"/scan UCL -> only UCL legs: {[l['league'] for l in res['legs']]}")
    check(res["constraint_check"]["ok"], "check ok")
    # legacy league text (old AI arg / Mini App) is id-matched too
    res = asyncio.run(sc.scan("safe", 3, "today", ["Champions League"], None))
    check(ucl_only(res["legs"]), f"legacy leagues=['Champions League'] -> only UCL: {[l['league'] for l in res['legs']]}")
    res = asyncio.run(sc.scan("safe", 2, "today", None, parse_text("only europa")))
    check(res["legs"] and all("Europa League" in l["league"] for l in res["legs"]), "only Europa")
    res = asyncio.run(sc.scan("safe", 2, "today", None, parse_text("premier league only")))
    check(res["legs"] and all(l["league"] == "England · Premier League" for l in res["legs"]), "EPL only")
    # few matches: never padded with other leagues; says so
    res = asyncio.run(sc.scan("safe", 50, "today", None, parse_text("ucl picks")))
    check(ucl_only(res["legs"]) and not res["reached"] and "won't" not in "" and
          "UEFA Champions League" in res["note"] and "other leagues" in res["note"],
          f"UCL 50 odds -> short, UCL only, honest note: {res['note']}")
    # team filter
    c = parse_text("safe picks")
    c.include_teams = ["Barcelona", "Bayern Munich"]
    res = asyncio.run(sc.scan("safe", 1.3, "today", None, c))
    check(res["legs"] and all({"Barcelona", "Bayern Munich"} & {l["home"], l["away"]} for l in res["legs"]),
          f"team filter {[l['match'] for l in res['legs']]}")
    # mixed rules
    c = parse_text("ucl picks, 3 legs, no unders, odds 1.2 to 1.3 each")
    res = asyncio.run(sc.scan("safe", 2, "today", None, c))
    check(ucl_only(res["legs"]) and len(res["legs"]) <= 3 and not any(is_under(l) for l in res["legs"]) and
          all(1.2 <= l["odds"] <= 1.3 for l in res["legs"]) and res["constraint_check"]["ok"],
          f"mixed rules {[(l['match'], l['pick'], l['odds']) for l in res['legs']]}")


def test_no_ucl_today():
    sc, _ = scanner(events(ucl_today=False))
    res = asyncio.run(sc.scan("safe", 5, "today", None, parse_text("champions league selections")))
    check(not res["legs"], f"no UCL today -> no legs (got {[l['league'] for l in res['legs']]})")
    sc_ = res.get("scope") or {}
    check(sc_.get("matches_in_window") == 0 and sc_.get("next_days") and sc_["next_days"][0]["label"] == "tomorrow",
          f"scope report {sc_}")
    check("No matches today in your scope" in res["note"] and "tomorrow" in res["note"] and
          "other leagues" in res["note"], f"clear note: {res['note']}")


# ------------------------------------------------------------------ AI path
def analyst(sc, bm, calls):
    from ai import Analyst
    from storage import Storage

    class S:
        storage = Storage(os.path.join(os.environ["DATA_DIR"], "t.sqlite"))
        scanner = sc
        bookmaker = bm
        tracker = None

        async def events(self, k):
            return await bm.list_events(24)
    an = Analyst(S(), api_key="fake")
    it = iter(calls)

    async def fake_generate(contents, **kw):
        return next(it)
    an._generate = fake_generate
    return an


def call(name, args):
    return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}}]}


def text(t):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]}


def test_ai():
    sc, bm = scanner(events())
    # 1) the model forgets the league entirely
    an = analyst(sc, bm, [call("scan_markets", {"tier": "safe", "target_odds": 3}), text("Here you go (estimates).")])
    res = asyncio.run(an.ask(1, "Champions League selections"))
    check(ucl_only(res["slip"]["legs"]), f"AI forgot league -> still UCL only {[l['league'] for l in res['slip']['legs']]}")
    check("UEFA Champions League" in res["reply"] and "Rules check" in res["reply"], "reply states the rules")
    # 2) the model passes the legacy substring arg
    an = analyst(sc, bm, [call("scan_markets", {"tier": "safe", "target_odds": 3, "leagues": ["Champions League"]}),
                          text("ok (estimates)")])
    res = asyncio.run(an.ask(2, "ucl picks"))
    check(ucl_only(res["slip"]["legs"]), "AI legacy leagues arg -> UCL only")
    # 3) the model widens the scope with AFC CL / other leagues
    an = analyst(sc, bm, [call("scan_markets", {"tier": "safe", "target_odds": 3,
                                                "include_leagues": ["Champions League", "AFC Champions League",
                                                                    "Premier League"]}), text("ok (estimates)")])
    res = asyncio.run(an.ask(3, "give me UCL selections"))
    check(ucl_only(res["slip"]["legs"]), "AI widened -> user's league wins")
    # 4) list_matches(league="Champions League") never returns AFC / Gulf / SRL; user scope applies to it too
    turn = {"hard": parse_text("ucl games"), "tools": []}
    r = asyncio.run(an.run_tool("list_matches", {"day": "today", "league": "Champions League"}, turn))
    check(r["count"] > 0 and all(m["league"].endswith("· UEFA Champions League") for m in r["matches"]),
          f"list_matches CL {[m['league'] for m in r['matches']]}")
    r = asyncio.run(an.run_tool("list_matches", {"day": "today"}, turn))
    check(all(m["league"].endswith("· UEFA Champions League") for m in r["matches"]), "list_matches obeys user scope")
    # 5) team detection on the AI path
    an = analyst(sc, bm, [call("scan_markets", {"tier": "safe", "target_odds": 1.3}), text("ok (estimates)")])
    res = asyncio.run(an.ask(4, "safe picks from Barcelona and Bayern games"))
    check(res["slip"]["legs"] and all({"Barcelona", "Bayern Munich"} & {l["home"], l["away"]}
                                      for l in res["slip"]["legs"]), "AI team filter")
    # 6) no UCL today on the AI path: plain message with the nearest day, no substitute slip
    sc2, bm2 = scanner(events(ucl_today=False))
    an = analyst(sc2, bm2, [call("scan_markets", {"tier": "safe", "target_odds": 5}),
                            text("No UCL games today (estimates).")])
    res = asyncio.run(an.ask(5, "champions league selections today"))
    check(not res["slip"]["legs"] and "tomorrow" in res["reply"] and "other leagues" in res["reply"],
          f"AI no-UCL reply: {res['reply'][-300:]}")
    check(res["reply"].count("No matches today in your scope") <= 1, "scope note not repeated")
    # fewer legs than asked: honest info line, not a "failed" warning
    an = analyst(sc, bm, [call("scan_markets", {"tier": "safe", "target_odds": 1.3}), text("ok (estimates)")])
    res = asyncio.run(an.ask(6, "ucl picks, 9 legs"))
    sl = res["slip"]
    check(ucl_only(sl["legs"]) and len(sl["legs"]) < 9 and "nothing padded" in res["reply"] and
          "failed" not in res["reply"], f"short legs footer: {res['reply'][-250:]}")
    check(not bm.booked and not bm2.booked, "nothing booked")


# ------------------------------------------------------------------ slip edits keep the scope stated in the edit
def test_edit_scope():
    from editflow import SlipEditor
    cands = [l.to_json() for e in events() for l in legs_for(e)]
    ctx = {"user_scope": parse_text("replace leg 2 with a champions league game").to_dict()}
    out = SlipEditor._scoped(SlipEditor.__new__(SlipEditor), ctx, cands)
    check(out and all(l["league"].endswith("· UEFA Champions League") for l in out), "edit candidates scoped to UCL")
    check(SlipEditor._scoped(SlipEditor.__new__(SlipEditor), {}, cands) == cands, "no scope -> unchanged")


if __name__ == "__main__":
    for f in (test_aliases, test_matching, test_scanner, test_no_ucl_today, test_ai, test_edit_scope):
        f()
        print("ok" if not FAILS else "..", f.__name__)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'}")
    sys.exit(1 if FAILS else 0)
