"""5-minute draw finder (offline: no network, no Gemini, no booking, no Telegram sends).

    python tests/test_draw5.py

Covers: SportyBet market mapping (900069 "1X2 from 1 to 5 minute", minute=5, outcome 2 = Draw), suspended markets,
probability sanity (Poisson level chance, de-margining, early-goal tendency, est range), period windows in WAT, the
minimum-50 rule (widening to other leagues only to reach 50, tagged; named leagues are hard filters; fewer than 50 ->
all of them and a plain note), goal-minute parsing from the Sportradar comment, settlement of the minute market,
chat / command parsing, Telegram texts and buttons (Rules: line, pagination, no emoji), the Mini App API (preview
slips registered, nothing booked or saved) and the Mini App section.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import draw5  # noqa: E402
from bookmakers import Event, Outcome  # noqa: E402
from srstats import H2H, Game, MatchForm, TeamForm, goal_minutes  # noqa: E402

FAILS: list[str] = []
WAT = timezone(timedelta(hours=1))
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D]")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  PASS", msg)


def ev(i, start, top=True, d5=(9.0, 1.10, 11.0), active=True, x12=(2.2, 3.3, 3.3), ou=(1.95, 1.85), probs=None,
       league=None):
    tid, cat, lg, cname = ("sr:tournament:17", "sr:category:1", "Premier League", "England") if top else \
        ("sr:tournament:999001", "sr:category:9999", "Premier League", "Wakanda")
    if league:
        tid, cat, lg, cname = league
    mk = {("1", ""): {k: Outcome(k, d, o, True) for k, d, o in zip(("1", "2", "3"), ("Home", "Draw", "Away"), x12)},
          ("18", "total=2.5"): {"12": Outcome("12", "Over 2.5", ou[0], True),
                                "13": Outcome("13", "Under 2.5", ou[1], True)}}
    if d5:
        pr = probs or (None, None, None)
        mk[("900069", "minute=5")] = {k: Outcome(k, d, o, active, p) for k, d, o, p in
                                      zip(("1", "2", "3"), ("Home", "Draw", "Away"), d5, pr)}
    return Event(event_id=f"sr:match:{1000 + i}", home=f"Home{i}", away=f"Away{i}", start=start, league=lg,
                 category=cname, tournament_id=tid, markets=mk, home_id=f"sr:competitor:{500 + i}",
                 away_id=f"sr:competitor:{800 + i}", category_id=cat)


class FakeBM:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.booked = 0

    async def list_events(self, hours=24, market_ids=None):
        self.calls.append((hours, market_ids))
        return self.events

    async def book(self, sels):
        self.booked += 1
        raise AssertionError("no booking in tests")


class FakeStats:
    def __init__(self, slow=True):
        self.slow = slow
        self.n = 0

    async def match_form(self, hid, aid, home="", away=""):
        self.n += 1
        now = int(datetime.now(timezone.utc).timestamp())
        def games(early):
            out = []
            for k in range(10):
                mins_for = (3,) if early and k < 4 else (60,)
                out.append(Game("H" if k % 2 else "A", 1, 1, 0, 0, "x", now - k * 7 * 86400, mins_for, (75,)))
            return out
        return MatchForm(TeamForm(hid, home, games(not self.slow)), TeamForm(aid, away, games(not self.slow)),
                         H2H(games=[(1, 0, now)], first_goal=[50]))


def test_mapping():
    print("market mapping")
    now = datetime(2026, 11, 5, 21, 29, tzinfo=timezone.utc)
    e = ev(1, now + timedelta(days=1))
    p = draw5.make_pick(e)
    j = p.to_json(1)
    check(j["market_id"] == "900069" and j["specifier"] == "minute=5" and j["outcome_id"] == "2",
          "pick books market 900069 / minute=5 / outcome 2 (Draw)")
    check(j["pick"] == "Draw (1-5 min)" and j["market"] == "1X2 from 1 to 5 minute", "pick + market labels")
    check(draw5.LIST_MARKETS == "1,18,900069", "list call asks for 1X2, O/U and the minute 1X2 market")
    check(abs(j["odds"] - 1.10) < 1e-9 and j["open"], "odds read from the Draw outcome, market open")
    check(draw5.make_pick(ev(2, now, d5=None)) is None, "event without the market -> no pick")
    sus = draw5.make_pick(ev(3, now, active=False))
    check(sus is not None and sus.open is False, "suspended market kept, flagged open=False")
    fp = draw5.make_pick(ev(4, now, probs=(0.06, 0.88, 0.06)))
    check(abs(fp.p_market - 0.88) < 1e-9, "feed probabilities used as the margin-free market price when listed")


def test_probability():
    print("probability sanity")
    check(abs(draw5.p_level(0, 0) - 1.0) < 1e-12, "no goals expected -> level for sure")
    p = draw5.p_level(1.4 * draw5.SHARE5, 1.1 * draw5.SHARE5)
    check(0.88 < p < 0.95, f"2.5 exp. goals -> level after 5 min ~{p:.3f} (0.88-0.95)")
    check(draw5.p_level(0.05, 0.05) > draw5.p_level(0.1, 0.1), "more early goals -> lower level chance")
    pw = draw5.devig_power([9.0, 1.10, 11.0])
    pr = draw5.devig([9.0, 1.10, 11.0])
    check(abs(sum(pw) - 1) < 1e-9 and pw[1] > pr[1], "power de-margining sums to 1, leaves more on the favourite")
    check(abs(draw5.early_mult(0, 0) - 1.0) < 1e-12, "no early-goal data -> neutral tendency")
    check(draw5.early_mult(0, 3.0) < 1 < draw5.early_mult(6, 3.0), "slow starters < 1 < fast starters")
    now = datetime(2026, 11, 5, 21, 29, tzinfo=timezone.utc)
    low = draw5.make_pick(ev(1, now, x12=(2.6, 2.9, 3.1), ou=(2.6, 1.5)))
    high = draw5.make_pick(ev(2, now, x12=(1.5, 4.5, 6.0), ou=(1.4, 2.9)))
    check(low.est > high.est, f"low-scoring game ranks above high-scoring ({low.est:.3f} > {high.est:.3f})")
    check(all(0.80 < x.est < 0.97 for x in (low, high)), "est in a sane band (80-97%)")
    a = draw5.make_pick(ev(3, now))
    b = draw5.make_pick(ev(4, now))
    draw5.apply_form(a, asyncio.run(FakeStats(slow=True).match_form("sr:competitor:1", "sr:competitor:2", "A", "B")))
    draw5.apply_form(b, asyncio.run(FakeStats(slow=False).match_form("sr:competitor:1", "sr:competitor:2", "A", "B")))
    check(a.stats and b.stats and a.est > b.est, f"slow starters ({a.est:.3f}) above fast starters ({b.est:.3f})")
    check("slow starters" in a.why and "1-15'" in a.why and "H2H" in a.why, f"reason text: {a.why}")
    check(a.p_model is not None and min(a.p_model, a.p_market) <= a.est <= max(a.p_model, a.p_market),
          "est lies between the market price and the model")


def test_periods():
    print("period windows (WAT)")
    thu = datetime(2026, 11, 5, 22, 29, tzinfo=WAT)          # Thursday night
    w = draw5.period_window("weekend", thu)
    check(w["start"].astimezone(WAT) == datetime(2026, 11, 6, 18, 0, tzinfo=WAT), "weekend starts Fri 18:00 WAT")
    check(w["end"].astimezone(WAT).strftime("%a %d %H:%M") == "Sun 08 23:59", "weekend ends Sun 23:59 WAT")
    check("Fri 06 Nov 18:00 - Sun 08 Nov 23:59 WAT" in w["label"], f"absolute dates in the label: {w['label']}")
    m = draw5.period_window("midweek", thu)                    # < 24 h of this midweek left -> next week's
    check(m["start"].astimezone(WAT) == datetime(2026, 11, 9, 0, 0, tzinfo=WAT) and
          m["end"].astimezone(WAT).strftime("%a %d %H:%M") == "Fri 13 17:59", "Thu night: midweek = next Mon-Fri 17:59")
    tue = datetime(2026, 11, 10, 10, 0, tzinfo=WAT)
    m2 = draw5.period_window("midweek", tue)
    check(m2["start"] == tue.astimezone(timezone.utc) and m2["end"].astimezone(WAT).day == 13,
          "Tuesday: midweek runs from now to Fri 17:59")
    sat = datetime(2026, 11, 7, 15, 0, tzinfo=WAT)
    w2 = draw5.period_window("weekend", sat)
    check(w2["start"] == sat.astimezone(timezone.utc) and w2["end"].astimezone(WAT).day == 8,
          "inside the weekend: from now to Sun 23:59")
    m3 = draw5.period_window("midweek", sat)
    check(m3["start"].astimezone(WAT) == datetime(2026, 11, 9, 0, 0, tzinfo=WAT), "weekend: midweek = next Monday")
    wk = draw5.period_window("week", thu)
    check(wk["end"] - wk["start"] == timedelta(days=7), "full week = next 7 days")
    check(draw5.parse_period("5 min draw weekend") == "weekend" and draw5.parse_period("mid week") == "midweek"
          and draw5.parse_period("full week") == "week" and draw5.parse_period("/draw5 week") == "week"
          and draw5.parse_period("5 min draw") is None, "period words parsed (weekend / mid week / full week)")
    check(all(draw5.wants_draw5(t) for t in ("5 min draw", "Give me 5 mins draw fixtures for weekend",
                                              "5-minute draws midweek", "draw in the first 5 minutes", "draw5"))
          and not any(draw5.wants_draw5(t) for t in ("safe 5 odds", "draw no bet", "over 2.5 in 5 games")),
          "chat alias detected, other requests left alone")


def _picks(n_top, n_wider, now):
    out = []
    for i in range(n_top):
        out.append(draw5.make_pick(ev(i, now, d5=(9.0, 1.08 + i * 0.001, 11.0))))
    for i in range(n_wider):
        out.append(draw5.make_pick(ev(100 + i, now, top=False, d5=(9.0, 1.05 + i * 0.001, 11.0))))
    return out


def test_min50():
    print("minimum 50 / widening")
    now = datetime(2026, 11, 5, 21, 29, tzinfo=timezone.utc)
    sel, c = draw5.select(_picks(30, 40, now), 50)
    check(len(sel) == 50 and c["top"] == 30 and c["wider"] == 20, "30 top + 20 wider = 50")
    check(sum(1 for p in sel if not p.top) == 20 and all(p.top for p in sel if p.top_league), "wider picks tagged")
    check([p.est for p in sel] == sorted([p.est for p in sel], reverse=True), "ranked by est")
    sel, c = draw5.select(_picks(70, 40, now), 50)
    check(len(sel) == 70 and c["wider"] == 0, "70 top-league picks -> no widening, all 70 shown")
    sel, c = draw5.select(_picks(30, 40, now), 50, widen=False)
    check(len(sel) == 30, "named leagues: no widening")
    sel, c = draw5.select(_picks(10, 5, now), 50)
    check(len(sel) == 15 and "Only 15" in draw5.short_note("top", c, 15), "fewer than 50 anywhere -> all + note")

    # end to end with a fake bookmaker + stats
    thu = datetime(2026, 11, 5, 21, 29, tzinfo=timezone.utc)
    sat = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    evs = [ev(i, sat + timedelta(minutes=i)) for i in range(35)] + \
          [ev(100 + i, sat + timedelta(minutes=i), top=False) for i in range(30)] + \
          [ev(200, thu + timedelta(hours=3)), ev(201, sat, d5=None)]    # outside the window / no market
    bm, st = FakeBM(evs), FakeStats()
    f = draw5.Draw5Finder(bm, st)
    res = asyncio.run(f.find("weekend", "", now=thu))
    check(res["count"] == 50 and res["top_count"] == 35 and res["wider_count"] == 15,
          f"finder: 35 top + 15 wider = 50 (got {res['count']}, {res['top_count']}, {res['wider_count']})")
    check(res["rules"].startswith("5-min Draw") and "15 wider-league" in res["rules"] and "Weekend" in res["rules"],
          "rules line names market, window and widening")
    check(bm.calls and bm.calls[0][1] == "1,18,900069", "one list call with the market ids (no detail calls)")
    check(st.n >= 50 and res["with_stats"] >= 50, "team stats read for every listed pick")
    check(all(p["kickoff"] >= "2026-11-06T17:00" for p in res["picks"]), "only kickoffs inside the window")
    check(bm.booked == 0, "nothing booked")
    res2 = asyncio.run(draw5.Draw5Finder(FakeBM(evs), FakeStats()).find("weekend", "premier league", now=thu))
    check(res2["scope"] == "filter" and res2["count"] == 35 and "didn't add others" in res2["note"],
          "named league = hard filter: 35, no widening, plain note")
    sl = draw5.slip_from(res, 10)
    tot = 1.0
    for l in sl["legs"]:
        tot *= l["odds"]
    check(len(sl["legs"]) == 10 and abs(sl["total_odds"] - round(tot, 2)) < 1e-9 and sl["legs"][0]["rank"] == 1,
          "slip preview = top 10, total odds = product")
    allsus = dict(res, picks=[dict(p, open=False) for p in res["picks"]])
    check(draw5.slip_from(allsus, 10)["legs"] == [], "no slip from suspended picks")


def test_minutes_and_settle():
    print("goal minutes + settlement")
    check(goal_minutes("1:0 (7.) L.Yamal, 2:0 (52.) Raphinha, 3:0 (64.) Raphinha", 3, 0) == ((7, 52, 64), ()),
          "home goals read from the running score")
    check(goal_minutes("0:1 (3.) X, 1:1 (45.+2) Y", 1, 1) == ((45,), (3,)), "stoppage-time minute, away first")
    check(goal_minutes("", 2, 1) is None and goal_minutes("", 0, 0) == ((), ()), "no detail -> unknown; 0-0 known")
    import settle
    leg = {"market_id": "900069", "specifier": "minute=5", "outcome_id": "2", "market": "1X2 from 1 to 5 minute"}
    r = settle.MatchResult(state="ended", ft=(0, 0))
    check(settle.settle_leg(leg, r)[0] == "won", "0-0 full time -> Draw (1-5 min) won")
    r = settle.MatchResult(state="ended", ft=(2, 1))
    check(settle.settle_leg(leg, r)[0] == "check", "goals scored -> check (timing needed)")
    r = settle.MatchResult(state="live", live=(0, 0), minute=8, phase="1h")
    check((settle.early_settle(leg, r) or ("",))[0] == "won", "0-0 at minute 8 -> won early")
    r = settle.MatchResult(state="live", live=(0, 0), minute=4, phase="1h")
    check(settle.early_settle(leg, r) is None, "minute 4 -> still open")
    r = settle.MatchResult(state="live", live=(1, 0), minute=20, phase="1h")
    check(settle.early_settle(leg, r) is None, "1-0 at minute 20 -> unknown timing, wait")


def test_texts_and_buttons():
    print("Telegram texts / buttons, Mini App")
    thu = datetime(2026, 11, 5, 21, 29, tzinfo=timezone.utc)
    sat = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    evs = [ev(i, sat + timedelta(minutes=i)) for i in range(55)]
    res = asyncio.run(draw5.Draw5Finder(FakeBM(evs), FakeStats()).find("weekend", "", now=thu))
    body, pages = draw5.page_html(res, 0, 10)
    check(body.startswith("<b>Rules:</b>") and pages == 6 and len(body) <= 4096, "page 1/6 opens with Rules:, fits")
    last, _ = draw5.page_html(res, 9, 10)
    check("page 6/6" in last, "page index clamped")
    import handlers
    kb = handlers.d5_kb("abcd1234", res, 0, pages)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    check({"Next", "Weekend", "[Weekend]"} & set(labels) and "[Weekend]" in labels and "Midweek" in labels
          and "Full week" in labels, f"buttons: {labels}")
    check("Slip: top 10" in labels and "Full list (file)" in labels, "slip preview + full-list buttons")
    check(all(len(d.encode()) <= 64 for d in datas), "callback data within Telegram's 64 bytes")
    sl = draw5.slip_from(res, 5)
    txt = body + draw5.file_text(res) + draw5.slip_html(sl) + draw5.chat_summary(res) + " ".join(labels) + \
        handlers.HELP
    check(not EMOJI.search(txt), "no emoji in any draw5 text or button")
    check(draw5.slip_html(sl).startswith("<b>Rules:</b>") and "Preview only" in draw5.slip_html(sl),
          "slip preview says nothing is booked")
    check("/draw5" in handlers.HELP, "help lists /draw5")

    # Mini App API: preview slips registered for booking, nothing booked, nothing saved
    import webapp
    reg = {}
    fake_ai = SimpleNamespace(remember_slip=lambda s: reg.setdefault(f"sid{len(reg)}", s) and f"sid{len(reg) - 1}")
    tr = SimpleNamespace(record=None)
    bm = FakeBM(evs)
    svc = SimpleNamespace(draw5=draw5.Draw5Finder(bm, FakeStats()), ai=fake_ai, tracker=tr)

    async def _json(req):
        return await req.json()

    me = SimpleNamespace(s=svc, _json=_json, _gz=lambda r: r)

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

        def __getitem__(self, k):
            return {"id": 1, "admin": True}

    later = datetime.now(timezone.utc) + timedelta(days=1)
    svc.draw5 = draw5.Draw5Finder(FakeBM([ev(i, later + timedelta(minutes=i)) for i in range(55)]), FakeStats())
    resp = asyncio.run(webapp.MiniApp.draw5(me, Req({"period": "week"})))
    out = json.loads(resp.text)
    check(resp.status == 200 and out["count"] == 55 and set(out["slips"]) == {"5", "10", "20"},
          "/api/draw5 week -> 55 picks + top 5/10/20 preview slips")
    check(bm.booked == 0 and tr.record is None, "API books nothing and saves nothing to History")
    bad = asyncio.run(webapp.MiniApp.draw5(me, Req({"period": "month"})))
    check(bad.status == 400, "unknown period -> 400")
    js = open(os.path.join(os.path.dirname(__file__), "..", "webapp_static", "app.js"), encoding="utf-8").read()
    check("/api/draw5" in js and "5-min Draw" in js and "Full week" in js and "Midweek" in js and "Rules:" in js,
          "Mini App: 5-min Draw section with the period switch and a Rules line")


if __name__ == "__main__":
    test_mapping()
    test_probability()
    test_periods()
    test_min50()
    test_minutes_and_settle()
    test_texts_and_buttons()
    print(f"\nFAILURES: {len(FAILS)}")
    if FAILS:
        sys.exit(1)
    print("ALL PASSED")
