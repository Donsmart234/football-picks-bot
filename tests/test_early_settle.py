"""Early settlement ("entered before the final whistle"), rollover tickets and refresh picks. Offline: SQLite history,
fake results feed, fake scanner / bookmaker / bot. Nothing is booked and nothing is sent.

    python tests/test_early_settle.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["OWNER_TELEGRAM_ID"] = "1"

from settle import MatchResult, early_settle, settle_leg  # noqa: E402
from storage import Storage  # noqa: E402

NOW = datetime.now(timezone.utc)


def L(mid, oid, spec="", pick="", market="", odds=1.2, eid="sr:match:1", ko=None, home="Home FC", away="Away FC"):
    return {"event_id": eid, "home": home, "away": away, "home_uid": "1", "away_uid": "2",
            "kickoff": (ko or NOW - timedelta(minutes=40)).isoformat(), "league": "England · Premier League",
            "category": "England", "market_id": mid, "outcome_id": oid, "specifier": spec, "pick": pick,
            "market": market, "odds": odds, "est": 0.85}


def live(i, j, phase="2h", minute=63, ht=None):
    sid = {"1h": 6, "ht": 31, "2h": 7, "other": 41}[phase]
    return MatchResult(state="live", live=(i, j), ht=ht, phase=phase, minute=minute, status_id=sid,
                       status_name={"1h": "1st half", "ht": "Halftime", "2h": "2nd half", "other": "1st extra"}[phase])


def unit():
    W, X = "won", "lost"
    cases = [
        # (leg, live result, expected status or None = not certain yet)
        ("O1.5 reached", L("18", "12", "total=1.5"), live(2, 0), W),
        ("O2.5 not yet", L("18", "12", "total=2.5"), live(2, 0), None),
        ("O2.5 reached 1-2", L("18", "12", "total=2.5"), live(1, 2), W),
        ("U2.5 exceeded", L("18", "13", "total=2.5"), live(2, 1), X),
        ("U2.5 still alive", L("18", "13", "total=2.5"), live(1, 1), None),
        ("O0.5 at HT", L("18", "12", "total=0.5"), live(1, 0, "ht", 45, (1, 0)), W),
        ("GG yes both scored", L("29", "74"), live(1, 1), W),
        ("GG yes one scored", L("29", "74"), live(3, 0), None),
        ("GG no both scored", L("29", "76"), live(1, 1), X),
        ("GG no alive", L("29", "76"), live(2, 0), None),
        ("home O1.5 reached", L("19", "12", "total=1.5"), live(2, 0), W),
        ("home O1.5 away goals don't count", L("19", "12", "total=1.5"), live(1, 3), None),
        ("away O0.5 reached", L("20", "12", "total=0.5"), live(0, 1), W),
        ("away U1.5 exceeded", L("20", "13", "total=1.5"), live(0, 2), X),
        ("1X2 never early", L("1", "1"), live(4, 0, minute=88), None),
        ("DC never early", L("10", "9"), live(3, 0, minute=89), None),
        ("DNB never early", L("11", "4"), live(3, 0), None),
        ("AH never early", L("16", "1714", "hcp=1.5"), live(0, 0), None),
        ("EH never early", L("14", "1711", "hcp=0:1"), live(3, 0), None),
        ("odd/even never early", L("26", "70", market="Odd/Even"), live(1, 0), None),
        ("corners never early", L("166", "12", "total=8.5", market="Corners"), live(1, 0), None),
        ("1H O0.5 in 1st half", L("68", "12", "total=0.5"), live(1, 0, "1h", 23), W),
        ("1H O1.5 not yet", L("68", "12", "total=1.5"), live(1, 0, "1h", 23), None),
        ("1H 1X2 settled at HT", L("60", "1"), live(1, 0, "ht", 45, (1, 0)), W),
        ("1H 1X2 lost (2nd half)", L("60", "3"), live(1, 1, "2h", 70, (1, 0)), X),
        ("1H DC at HT", L("63", "9"), live(0, 0, "ht", 45, (0, 0)), W),
        ("1H 1X2 not during 1st half", L("60", "1"), live(2, 0, "1h", 30), None),
        ("1H GG no settled at HT", L("75", "76"), live(1, 0, "ht", 45, (1, 0)), W),
        ("1H DNB draw at HT waits", L("64", "4"), live(0, 0, "ht", 45, (0, 0)), None),
        ("2H O0.5 from 2nd-half goals", L("90", "12", "total=0.5"), live(2, 0, "2h", 60, (1, 0)), W),
        ("2H O0.5 1st-half goal only", L("90", "12", "total=0.5"), live(1, 0, "2h", 60, (1, 0)), None),
        ("2H O0.5 not in 1st half", L("90", "12", "total=0.5"), live(1, 0, "1h", 30), None),
        ("correct score exceeded", L("45", "x", pick="1:0", market="Correct Score"), live(2, 0), X),
        ("correct score alive", L("45", "x", pick="2:1", market="Correct Score"), live(1, 1), None),
        ("exact goals exceeded", L("21", "x", pick="2", market="Exact Goals"), live(2, 1), X),
        ("multigoals 3+ reached", L("21", "x", pick="3+", market="Multigoals"), live(2, 1), W),
        ("multigoals 1-3 exceeded", L("21", "x", pick="1-3", market="Multigoals"), live(3, 1), X),
        ("extra time: no early calls", L("18", "12", "total=2.5"), live(2, 2, "other", 100), None),
        ("ended: not early", L("18", "12", "total=1.5"), MatchResult(state="ended", ft=(2, 0), ht=(1, 0)), None),
        ("not started", L("18", "12", "total=1.5"), MatchResult(state="notstarted"), None),
    ]
    bad = []
    for name, leg, r, exp in cases:
        got = early_settle(leg, r)
        if (got[0] if got else None) != exp:
            bad.append((name, got, exp))
        # an early call must never disagree with the final result if the score didn't change afterwards
        if got and r.state == "live" and r.phase in ("1h", "2h") and leg["market_id"] not in ("60", "63", "64", "75"):
            ft = r.live
            final = settle_leg(leg, MatchResult(state="ended", ft=ft, ht=r.ht or ft))[0]
            if final != got[0]:
                bad.append((name + " (vs final)", got, final))
    assert not bad, bad
    note = early_settle(L("18", "12", "total=1.5"), live(2, 0, minute=63))[1]
    assert note == "Won early (63')", note
    assert early_settle(L("18", "13", "total=0.5"), live(1, 0, "1h", 12))[1] == "Lost early (12')"
    print(f"unit: {len(cases)} early-settlement cases OK")


# ------------------------------------------------------------------ tracker flow (fake feed)
class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


class FakeResults:
    def __init__(self):
        self.by_event = {}

    async def result(self, event_id, need_stats=False):
        return self.by_event.get(event_id, MatchResult(state="notstarted"))

    async def aclose(self):
        pass


class FakeBookmaker:
    def __init__(self):
        self.booked = []

    async def book(self, sels):
        self.booked.append(sels)
        return f"NEW{len(self.booked):03d}"


class FakeScanner:
    """Returns slips built from a fixed list of future legs, honouring `exclude`."""
    def __init__(self, legs):
        self.legs = legs
        self.calls = []

    async def rollover_slip(self, target, allow_unders=False, exclude=None, fresh=False, **kw):
        from scanner import sel_key
        self.calls.append({"exclude": exclude, "fresh": fresh})
        legs = [l for l in self.legs if not (exclude and sel_key(l) in exclude)][:2]
        tot = 1.0
        for l in legs:
            tot *= l["odds"]
        return {"legs": legs, "total_odds": round(tot, 2) if legs else 0.0, "est_prob": 0.7 if legs else 0.0,
                "note": "" if legs else "nothing qualifies"}


class FakeServices:
    def __init__(self, st, scanner=None):
        self.storage = st
        self.bookmaker = FakeBookmaker()
        self.scanner = scanner

    async def find_event(self, eid):
        return None


def make_tracker(scanner=None):
    import tracker as T
    d = tempfile.mkdtemp()
    st = Storage(os.path.join(d, "t.sqlite3"))
    tr = T.Tracker(FakeServices(st, scanner))
    tr.results = FakeResults()
    tr.bot = FakeBot()
    return tr


async def flow_early():
    import tracker as T
    T.EARLY_CONFIRM_S = 0
    tr = make_tracker()
    a, b = "sr:match:101", "sr:match:102"
    rec = await tr.record([L("18", "12", "total=1.5", "Over 1.5", "Over/Under", 1.25, a),
                           L("29", "74", "", "Yes", "GG/NG", 1.6, b)], "TESTE1", "test", "test", "Test", test=True)
    feed = tr.results.by_event
    feed[a] = live(1, 0, minute=30)
    feed[b] = live(0, 0, minute=30)
    await tr.tick()
    r = await tr.history.get(rec["id"])
    assert r["status"] == "pending" and not tr.bot.sent
    assert [l["status"] for l in r["data"]["legs"]] == ["live", "live"]
    feed[a] = live(2, 0, minute=51)          # Over 1.5 now certain: first look only
    await tr.tick()
    r = await tr.history.get(rec["id"])
    assert r["data"]["legs"][0]["status"] == "live" and r["data"]["legs"][0].get("lock_seen"), r["data"]["legs"][0]
    feed[a] = live(2, 0, minute=53)
    await tr.tick()                          # second look confirms
    r = await tr.history.get(rec["id"])
    assert r["data"]["legs"][0]["status"] == "won" and r["data"]["legs"][0]["note"] == "Won early (53')", r["data"]["legs"][0]
    assert r["status"] == "pending" and not tr.bot.sent
    feed[b] = live(2, 1, minute=63)
    await tr.tick()
    await tr.tick()
    r = await tr.history.get(rec["id"])
    assert r["status"] == "won", r
    assert len(tr.bot.sent) == 1 and "Entered" in tr.bot.sent[0][1], tr.bot.sent
    assert r["data"]["early_open"] and r["data"]["settled_early"]
    await tr.tick()
    assert len(tr.bot.sent) == 1, "no duplicate ping"
    feed[a] = MatchResult(state="ended", ft=(3, 0), ht=(1, 0))
    feed[b] = MatchResult(state="ended", ft=(2, 2), ht=(0, 0))
    await tr.tick()
    r = await tr.history.get(rec["id"])
    assert all(l["early"]["verified"] for l in r["data"]["legs"]) and not r["data"]["early_open"]
    assert r["data"]["legs"][0]["score"].startswith("3-0") and r["data"]["legs"][0]["note"] == "Won early (53')"
    await tr.tick()
    assert len(tr.bot.sent) == 1, tr.bot.sent
    print("flow: live -> first look -> confirmed early win -> one '✅ Entered' ping -> verified at FT, no repeats OK")

    # lost early: Under 2.5 exceeded -> one ❌ ping straight away
    c = "sr:match:103"
    rec2 = await tr.record([L("18", "13", "total=2.5", "Under 2.5", "Over/Under", 1.5, c)], "TESTE2", "test", "test",
                           "Test", test=True)
    feed[c] = live(2, 1, minute=70)
    await tr.tick()
    await tr.tick()
    r2 = await tr.history.get(rec2["id"])
    assert r2["status"] == "lost" and r2["data"]["legs"][0]["note"] == "Lost early (70')", r2
    assert len(tr.bot.sent) == 2 and "lost" in tr.bot.sent[1][1]
    print("flow: Under exceeded -> lost early, one ❌ ping OK")

    # VAR-style correction: locked Over 2.5 at 3 goals, final shows 2 -> one correction ping
    e = "sr:match:104"
    rec3 = await tr.record([L("18", "12", "total=2.5", "Over 2.5", "Over/Under", 1.9, e)], "TESTE3", "test", "test",
                           "Test", test=True)
    feed[e] = live(2, 1, minute=80)
    await tr.tick()
    await tr.tick()
    assert (await tr.history.get(rec3["id"]))["status"] == "won"
    feed[e] = MatchResult(state="ended", ft=(2, 0), ht=(1, 0))
    await tr.tick()
    await tr.tick()
    r3 = await tr.history.get(rec3["id"])
    assert r3["status"] == "lost" and r3["data"]["corrected"] == "won -> lost", r3["data"]
    corr = [m for m in tr.bot.sent if "Correction" in m[1]]
    assert len(corr) == 1, tr.bot.sent
    print("flow: early call contradicted at full time -> corrected once OK")

    # a goal that disappears before the second look is never locked
    f = "sr:match:105"
    rec4 = await tr.record([L("18", "12", "total=0.5", "Over 0.5", "Over/Under", 1.1, f)], "TESTE4", "test", "test",
                           "Test", test=True)
    feed[f] = live(1, 0, minute=20)
    await tr.tick()
    feed[f] = live(0, 0, minute=22)         # goal ruled out
    await tr.tick()
    r4 = await tr.history.get(rec4["id"])
    assert r4["data"]["legs"][0]["status"] == "live" and not r4["data"]["legs"][0].get("lock_seen"), r4["data"]["legs"][0]
    print("flow: score reverted before confirmation -> not locked OK")

    # history = booked slips only
    assert await tr.record([L("1", "1")], "", "app") is None
    assert await tr.record([L("1", "1")], "", "ai") is None
    print("record guard: unbooked scanner/app/AI slips are not saved OK")
    await tr.aclose()


async def flow_rollover():
    import tracker as T
    T.EARLY_CONFIRM_S = 0
    fut = NOW + timedelta(hours=6)
    pool = [L("18", "12", "total=1.5", "Over 1.5", "Over/Under", 1.3, f"sr:match:2{i}", ko=fut + timedelta(minutes=i))
            for i in range(6)]
    sc = FakeScanner(pool)
    tr = make_tracker(sc)
    plan, _ = await tr.create_rollover(2.0, 3, 1000.0, autobook=False, generate=False, push=False)
    plan = await tr.history.get(plan["id"])
    day = await tr.generate_day(plan, force=True, push=False)
    assert day["status"] == "pending" and day["stake"] == 1000.0 and day["expected_return"] == 1690.0, day
    plan = await tr.history.get(plan["id"])
    tix = await tr.rollover_tickets(plan)
    assert len(tix) == 1 and tix[0]["status"] == "pending" and tix[0]["can_refresh"] and len(tix[0]["legs"]) == 2
    old_id = tix[0]["slip_id"]

    # same picks with fresh prices -> say so; different picks -> replace (unbooked: no confirm needed)
    out = await tr.refresh_day(old_id)
    assert out["status"] == "same" and sc.calls[-1]["fresh"] and sc.calls[-1]["exclude"] is None, out
    out = await tr.refresh_day(old_id, exclude_prev=True)
    assert out["status"] == "replaced" and sc.calls[-1]["exclude"], out
    new_id = out["record"]["id"]
    old = await tr.history.get(old_id)
    assert old["status"] == "replaced" and old["data"]["replaced_by"] == new_id
    plan = await tr.history.get(plan["id"])
    tix = await tr.rollover_tickets(plan)
    assert [t["status"] for t in tix] == ["replaced", "pending"], tix
    assert tix[1]["refreshed"] and tix[1]["can_refresh"] and not tix[0]["can_refresh"]
    assert {l["event_id"] for l in tix[1]["legs"]}.isdisjoint({l["event_id"] for l in tix[0]["legs"]})
    assert not tr.s.bookmaker.booked, "unbooked refresh must not book"
    try:
        await tr.refresh_day(old_id)
        raise AssertionError("refresh of a replaced slip must be refused")
    except ValueError:
        pass
    print("rollover: refresh same/different, old ticket kept as replaced (not a loss), no booking OK")

    # booked slip -> preview first, a new code only on confirm
    newrec = await tr.history.get(new_id)
    newrec["data"]["code"] = "OLD123"
    await tr.history.save(newrec)
    sc.legs = pool[::-1]
    out = await tr.refresh_day(new_id, exclude_prev=True)
    assert out["status"] == "preview" and out["old_code"] == "OLD123" and not tr.s.bookmaker.booked, out
    assert (await tr.history.get(new_id))["status"] == "pending", "preview must not touch the booked slip"
    done = await tr.refresh_confirm(out["token"])
    assert done["code"] == "NEW001" and len(tr.s.bookmaker.booked) == 1
    assert (await tr.history.get(new_id))["status"] == "replaced"
    try:
        await tr.refresh_confirm(out["token"])
        raise AssertionError("a token is single-use")
    except ValueError:
        pass
    print("rollover: booked slip -> preview, new code only on explicit confirm, token single-use OK")

    # no different slip left -> honest message, nothing changes
    cur = done["record"]["id"]
    sc.legs = [l for l in pool if l["event_id"] in {x["event_id"] for x in done["record"]["data"]["legs"]}]
    out = await tr.refresh_day(cur, exclude_prev=True)
    assert out["status"] == "none" and "No different valid slip" in out["note"], out

    # kicked off -> refused
    rec = await tr.history.get(cur)
    rec["data"]["legs"][0]["kickoff"] = (NOW - timedelta(minutes=1)).isoformat()
    await tr.history.save(rec)
    try:
        await tr.refresh_day(cur)
        raise AssertionError("refresh after kick-off must be refused")
    except ValueError as exc:
        assert "kicked off" in str(exc)
    print("rollover: no-different-slip message, refresh refused after first kick-off OK")

    # settle the day early -> bankroll compounds, ticket shows won early, past day stays viewable after advancing
    feed = tr.results.by_event
    for l in rec["data"]["legs"]:
        feed[l["event_id"]] = live(2, 0, minute=58)
    for l in rec["data"]["legs"]:
        l["kickoff"] = (NOW - timedelta(minutes=60)).isoformat()
    await tr.history.save(rec)
    await tr.tick()
    await tr.tick()
    plan = await tr.history.get(plan["id"])
    assert plan["data"]["current_day"] == 2 and plan["data"]["value"] == 1690.0, plan["data"]
    tix = await tr.rollover_tickets(plan)
    last = tix[-1]
    assert last["status"] == "won" and last["won_early"] == 58 and last["bankroll"] == 1690.0, last
    assert [t["status"] for t in tix] == ["replaced", "replaced", "won"]
    entered = [m for m in tr.bot.sent if "Entered" in m[1]]
    assert len(entered) == 1, tr.bot.sent
    day2 = await tr.generate_day(plan, force=True, push=False)
    plan = await tr.history.get(plan["id"])
    tix = await tr.rollover_tickets(plan)
    assert [t["status"] for t in tix] == ["replaced", "replaced", "won", "pending"] and tix[-1]["stake"] == 1690.0
    assert day2["stake"] == 1690.0
    print("rollover: won early -> plan advances, bankroll 1000 -> 1690, day-1 tickets still listed OK")
    await tr.aclose()


if __name__ == "__main__":
    unit()
    asyncio.run(flow_early())
    asyncio.run(flow_rollover())
    print("ALL OK")
