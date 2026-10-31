"""Tracker / settlement tests against REAL finished matches (free Sportradar feed), SQLite history, fake bot.

    python tests/test_tracker.py            # settles Arsenal 2-0 Everton (24 Oct 2026) legs, checks one ping
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["OWNER_TELEGRAM_ID"] = "1"

from storage import Storage  # noqa: E402
from settle import MatchResult, settle_leg, slip_status  # noqa: E402

ARS = {"event_id": "sr:match:72221332", "home": "Arsenal", "away": "Everton", "home_uid": "42", "away_uid": "48",
       "kickoff": "2026-10-24T14:00:00+00:00", "league": "England · Premier League", "category": "England"}


def L(mid, oid, spec="", pick="", market="", odds=1.2):
    return {**ARS, "market_id": mid, "outcome_id": oid, "specifier": spec, "pick": pick, "market": market,
            "odds": odds, "est": 0.85}


def unit():
    r = MatchResult(state="ended", ft=(2, 0), ht=(1, 0), corners={"ft": (12, 3), "h1": (10, 3)},
                    cards={"y": (1, 3), "r": (0, 0), "yr": (0, 0), "y_h1": (0, 1), "r_h1": (0, 0)})
    cases = [
        (L("1", "1"), "won"), (L("1", "2"), "lost"), (L("10", "9"), "won"), (L("10", "11"), "lost"),
        (L("11", "4"), "won"), (L("18", "12", "total=1.5"), "won"), (L("18", "13", "total=2.5"), "won"),
        (L("18", "12", "total=2"), "void"), (L("29", "76"), "won"), (L("29", "74"), "lost"),
        (L("19", "12", "total=1.5"), "won"), (L("20", "13", "total=0.5"), "won"),
        (L("16", "1714", "hcp=-1.5"), "won"), (L("14", "1712", "hcp=0:2"), "won"),
        (L("60", "1"), "won"), (L("68", "13", "total=1.5"), "won"), (L("83", "1"), "won"), (L("90", "12", "total=0.5"), "won"),
        (L("166", "12", "total=8.5"), "won"), (L("162", "1"), "won"), (L("900301", "31", "total=3.5"), "won"),
        (L("139", "12", "total=3.5"), "won"), (L("136", "3"), "won"),
        (L("26", "72", market="Odd/Even"), "won"), (L("45", "x", pick="2:0", market="Correct Score"), "won"),
        (L("999", "1", pick="Saka 1+", market="Player to score"), "check"),
    ]
    bad = [(c[0]["market_id"], c[0]["outcome_id"], c[0]["specifier"], settle_leg(c[0], r)[0], c[1])
           for c in cases if settle_leg(c[0], r)[0] != c[1]]
    assert not bad, bad
    assert slip_status([{"status": "won"}, {"status": "lost"}, {"status": "pending"}]) == "lost"
    assert slip_status([{"status": "won"}, {"status": "void"}]) == "won"
    assert slip_status([{"status": "won"}, {"status": "check"}]) == "check"
    print(f"unit: {len(cases)} settlement cases OK")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeServices:
    def __init__(self, st):
        self.storage = st
        self.bookmaker = None

    async def find_event(self, eid):
        return None


async def live():
    from tracker import Tracker
    d = tempfile.mkdtemp()
    st = Storage(os.path.join(d, "t.sqlite3"))
    tr = Tracker(FakeServices(st))
    tr.bot = FakeBot()
    won = await tr.record([L("1", "1", pick="Arsenal", market="1X2", odds=1.4),
                           L("166", "12", "total=8.5", pick="Over 8.5", market="Corners - Over/Under", odds=1.8)],
                          "TESTWIN", "test", "test", "Test", test=True)
    lost = await tr.record([L("29", "74", pick="Both teams score", market="GG/NG", odds=1.7)],
                           "TESTLOSS", "test", "test", "Test", test=True)
    await tr.tick()
    w, l_ = await tr.history.get(won["id"]), await tr.history.get(lost["id"])
    print("won slip:", w["status"], [(x["pick"], x["status"], x["score"], x["note"]) for x in w["data"]["legs"]])
    print("lost slip:", l_["status"], [(x["pick"], x["status"], x["score"]) for x in l_["data"]["legs"]])
    assert w["status"] == "won" and l_["status"] == "lost"
    n = len(tr.bot.sent)
    await tr.tick()          # second tick must not ping again
    assert len(tr.bot.sent) == n == 2, tr.bot.sent
    print("pings:", n, "(one per slip, none repeated)")
    print(tr.bot.sent[0][1][:600])
    await tr.aclose()


if __name__ == "__main__":
    unit()
    asyncio.run(live())
