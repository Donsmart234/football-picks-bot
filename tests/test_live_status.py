"""Live match status: a finished match never shows as LIVE (Live list, Ended section, match view, analyzer legs,
settlement / early settlement). Offline: real SportyBet / Sportradar payloads captured 2026-11-03
(tests/fixtures/live_states.json) plus synthetic states built from them. Nothing is fetched, booked or sent.

    python tests/test_live_status.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""

import httpx  # noqa: E402

import live  # noqa: E402
from analyzer import parse_share as share_legs  # noqa: E402
from live import GONE_STOP_GRACE_S as GRACE  # noqa: E402
from matchstate import FROZEN_S, is_sr_id, sb_state, sr_state  # noqa: E402
from settle import MatchResult, ResultsClient, early_settle, settle_leg  # noqa: E402

FX = json.loads((Path(__file__).parent / "fixtures" / "live_states.json").read_text())
FAILS = 0


def check(cond, msg: str) -> None:
    global FAILS
    print(("  PASS " if cond else "  FAIL ") + msg)
    FAILS += 0 if cond else 1


IMABARI = "sr:match:72151320"            # J2, Sportradar-covered, in play
ASC = "sr:match:111111114633165"         # Bet Genius (no Sportradar feed), in play
BOMBAY = "sr:match:111111114645123"      # Bet Genius, status 9 (betting closed) while the clock runs
SCHALKE = "sr:match:72513288"            # ended 0:2, status 4 "Ended"


def feed_event(eid: str) -> tuple[dict, dict]:
    for t in FX["list"]:
        for e in t["events"]:
            if e["eventId"] == eid:
                return copy.deepcopy(e), {k: v for k, v in t.items() if k != "events"}
    raise KeyError(eid)


def tour(events: list[dict], t: dict) -> list[dict]:
    return [{**t, "events": events}]


class FakeLive(live.Live):
    """Live with the network replaced: event pages and Sportradar docs come from dicts the test controls."""

    def __init__(self):
        super().__init__(bookmaker=None)
        self.pages: dict[str, dict | None] = {}
        self.sr: dict[str, dict | None] = {}
        self.feed: list | Exception = []
        self.calls = 0

    async def event(self, eid, ttl=live.EVENT_TTL):
        self.calls += 1
        return copy.deepcopy(self.pages.get(eid))

    async def _sr(self, path):
        return copy.deepcopy(self.sr.get(path))

    async def _fetch_list(self):
        if isinstance(self.feed, Exception):
            raise self.feed
        return copy.deepcopy(self.feed)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def mapping() -> None:
    print("state mapping (SportyBet status + matchStatus, Sportradar status ids)")
    cases = [((1, "H1"), "live"), ((1, "HT"), "live"), ((2, "H2"), "live"), ((9, "H2"), "live"),
             ((1, "Penalties"), "live"), ((1, "Awaiting extra time"), "live"),
             ((1, "Ended"), "ended"), ((4, "Ended"), "ended"), ((3, "AET"), "ended"), ((4, "AP"), "ended"),
             ((4, "Ended after penalties"), "ended"), ((1, "Abandoned"), "void"), ((5, "Cancelled"), "void"),
             ((0, "Postponed"), "postponed"), ((0, "Start delayed"), "postponed"), ((1, "Interrupted"), "interrupted"),
             ((0, "Not start"), "notstarted"), ((9, ""), "unknown"), ((4, ""), "ended")]
    for (st, ms), want in cases:
        got = sb_state(st, ms)[0]
        check(got == want, f"SportyBet status {st} {ms!r} -> {want} (got {got})")
    check(sb_state(3, "AET")[1] == "AET" and sb_state(4, "AP")[1] == "PEN" and sb_state(4, "Ended")[1] == "FT",
          "badges FT / AET / PEN")
    srcases = [(100, "Ended", "ended", "FT"), (110, "AET", "ended", "AET"), (120, "AP", "ended", "PEN"),
               (90, "Abandoned", "void", "ABD"), (70, "Cancelled", "void", "CANC"), (91, "Walkover", "void", "ABD"),
               (92, "Retired", "void", "ABD"), (60, "Postponed", "postponed", "PPD"), (61, "Start delayed", "postponed", "PPD"),
               (80, "Interrupted", "interrupted", "INT"), (81, "Suspended", "interrupted", "INT"),
               (6, "1st half", "live", "LIVE"), (31, "Halftime", "live", "HT"), (7, "2nd half", "live", "LIVE"),
               (41, "1st extra", "live", "LIVE"), (50, "Penalties", "live", "LIVE"), (0, "Not started", "notstarted", ""),
               (999, "Ended", "ended", "FT")]
    for sid, name, want, badge in srcases:
        got = sr_state({"_id": sid, "name": name})
        check(got[:2] == (want, badge), f"Sportradar {sid} {name} -> {want} {badge} (got {got[:2]})")
    check(sr_state({"_id": 7, "name": "2nd half"}, {"ended": "1793463830"})[0] == "ended",
          "Sportradar timeinfo.ended set -> ended even if the status id still says 2nd half")
    check(sr_state({"_id": 7, "name": "2nd half"})[2] == "2h" and sr_state({"_id": 41, "name": "1st extra"})[2] == "other",
          "live phases: 2h allows early calls, extra time = other")
    check(is_sr_id(IMABARI) and not is_sr_id(ASC), "Betradar id vs SportyBet's own (Bet Genius) id")
    ev = FX["events"][SCHALKE]
    check(sb_state(ev["status"], ev["matchStatus"]) == ("ended", "FT"), "real ended event page (status 4 Ended) -> FT")
    ev = FX["events"][BOMBAY]
    check(sb_state(ev["status"], ev["matchStatus"])[0] == "live", "real status-9 event page with the clock running -> live")


def minutes() -> None:
    print("minute / badge text")
    check(live._minute("90:00", "Ended", 4) == "FT", "ended shows FT, not 91'")
    check(live._minute("120:00", "AET", 4) == "AET" and live._minute("120:00", "AP", 4) == "PEN", "AET / PEN")
    check(live._minute("63:00", "Abandoned", 1) == "ABD" and live._minute("", "Postponed", 0) == "PPD", "ABD / PPD")
    check(live._minute("63:12", "H2", 1) == "64'" and live._minute("", "HT", 1) == "HT", "in play unchanged")
    e, t = feed_event(IMABARI)
    e.update(status=4, matchStatus="Ended", playedSeconds="90:00", setScore="0:3")
    c = live.compact_event(e, t)
    check(c["minute"] == "FT" and c["state"] == "ended" and c["badge"] == "FT" and c["score"] == "0–3",
          "compact_event: ended row -> FT 0–3")


def feed_states() -> None:
    print("Live list: finished states in the feed itself")
    lv = FakeLive()
    now = time.time()
    rows_t = []
    for eid, (st, ms) in {IMABARI: (1, "Ended"), ASC: (1, "AET"), BOMBAY: (1, "AP")}.items():
        e, t = feed_event(eid)
        e.update(status=st, matchStatus=ms)
        rows_t.append(e)
    e, t = feed_event(IMABARI)
    others = []
    for i, ms in enumerate(("Abandoned", "Postponed", "Cancelled", "H2")):
        x = copy.deepcopy(e)
        x.update(eventId=f"sr:match:9000000{i}", matchStatus=ms, status=1)
        others.append(x)
    rows, _ = run(lv.reconcile(tour(rows_t + others, t), now))
    ids = {r["id"] for r in rows}
    check(ids == {"sr:match:90000003"}, f"only the H2 match stays live (got {sorted(ids)})")
    badges = {r["id"]: r["badge"] for r in lv.ended()}
    check(badges.get(IMABARI) == "FT" and badges.get(ASC) == "AET" and badges.get(BOMBAY) == "PEN",
          f"Ended section badges FT / AET / PEN ({badges})")
    check(badges.get("sr:match:90000000") == "ABD" and badges.get("sr:match:90000001") == "PPD"
          and badges.get("sr:match:90000002") == "CANC", "abandoned / postponed / cancelled -> ABD / PPD / CANC")
    check(all(r["odds"] == [None, None, None] for r in lv.ended()), "ended rows carry no live odds")
    check(all("/live/" not in r["url"] for r in lv.ended()), "ended rows link the match page, not the live page")


def real_feed() -> None:
    print("Live list: the real feed snapshot (8 in play at 08:13 WAT)")
    lv = FakeLive()
    rows, raw = run(lv.reconcile(FX["list"], 1793689993))
    check(len(rows) == 8 and not lv.ended(), f"all 8 in-play rows kept, nothing ended ({len(rows)})")
    check(all(r["state"] == "live" for r in rows), "rows carry state=live")


def disappear() -> None:
    print("Live list: a match drops out of the feed")
    t0 = 1793690000.0
    e1, t = feed_event(IMABARI)
    e2, _ = feed_event(ASC)
    # 1) ended on its event page -> Ended with the final score
    lv = FakeLive()
    run(lv.reconcile(tour([e1, e2], t), t0))
    page = copy.deepcopy(FX["events"][IMABARI])
    page.update(status=4, matchStatus="Ended", playedSeconds="90:00", setScore="0:3")
    lv.pages[IMABARI] = page
    lv.sr[f"match_info/{IMABARI.rsplit(':', 1)[-1]}"] = {"match": {"status": {"_id": 100, "name": "Ended"},
                                                                     "result": {"home": 0, "away": 3},
                                                                     "timeinfo": {"ended": "1"}}}
    rows, _ = run(lv.reconcile(tour([e2], t), t0 + 20))
    end = {r["id"]: r for r in lv.ended()}
    check(IMABARI not in {r["id"] for r in rows} and end.get(IMABARI, {}).get("badge") == "FT"
          and end[IMABARI]["score"] == "0–3", "gone + event page Ended -> Ended section, FT 0–3")
    # 2) still in play on the event page (real: ASC left the feed at 90:37, event page status 1 H2 92:02)
    lv = FakeLive()
    run(lv.reconcile(tour([e1, e2], t), t0))
    lv.pages[ASC] = copy.deepcopy(FX["events"][ASC])          # 93:56, status 1, H2
    rows, _ = run(lv.reconcile(tour([e1], t), t0 + 20))
    check(ASC in {r["id"] for r in rows} and ASC not in {r["id"] for r in lv.ended()},
          "gone but the event page still shows it in play and moving -> stays live")
    # ...then the clock stops moving for FROZEN_S -> ended
    rows, _ = run(lv.reconcile(tour([e1], t), t0 + 40))
    rows, _ = run(lv.reconcile(tour([e1], t), t0 + 40 + FROZEN_S + 1))
    check(ASC not in {r["id"] for r in rows} and ASC in {r["id"] for r in lv.ended()},
          "event page frozen for 5 min after leaving the feed -> Ended")
    # 3) event page unreachable, no Sportradar feed -> never kept live
    lv = FakeLive()
    run(lv.reconcile(tour([e1, e2], t), t0))
    lv.pages[ASC] = None
    rows, _ = run(lv.reconcile(tour([e1], t), t0 + 20))
    end = {r["id"]: r for r in lv.ended()}
    check(ASC not in {r["id"] for r in rows} and end.get(ASC, {}).get("badge") == "Ended",
          "gone + no answer anywhere -> 'Ended' (last score), not live")
    # 4) status 9 (betting closed) with the clock running: still in play
    lv = FakeLive()
    b, _ = feed_event(BOMBAY)
    run(lv.reconcile(tour([e1, b], t), t0))
    lv.pages[BOMBAY] = copy.deepcopy(FX["events"][BOMBAY])
    rows, _ = run(lv.reconcile(tour([e1], t), t0 + 20))
    check(BOMBAY in {r["id"] for r in rows}, "status 9 + clock moving -> stays live (it is still being played)")
    # 5) Sportradar says ended while SportyBet's page still says 1 -> ended
    lv = FakeLive()
    run(lv.reconcile(tour([e1, e2], t), t0))
    lv.pages[IMABARI] = copy.deepcopy(FX["events"][IMABARI])
    lv.sr[f"match_info/{IMABARI.rsplit(':', 1)[-1]}"] = FX["sr_match_info"][SCHALKE.rsplit(":", 1)[-1]]
    rows, _ = run(lv.reconcile(tour([e2], t), t0 + 20))
    check(IMABARI not in {r["id"] for r in rows} and {r["id"]: r for r in lv.ended()}[IMABARI]["score"] == "0–2",
          "Sportradar Ended beats SportyBet's lagging status 1 (score from Sportradar)")


def ticking_clock() -> None:
    print("Live list: Bet Genius clocks keep ticking after the final whistle")
    # real 2026-11-03: ASC & Center FC left the live list at 90:37 (07:26 UTC, estimateStopTime 07:25:42), the event
    # page still said status 1, H2, 97:56 at 07:33 and counting
    e, t = feed_event(ASC)
    e1, _ = feed_event(IMABARI)
    page = copy.deepcopy(FX["events"][ASC])
    stop = page["estimateStopTime"] / 1000
    lv = FakeLive()
    run(lv.reconcile(tour([e, e1], t), stop - 60))
    page["playedSeconds"] = "92:02"
    lv.pages[ASC] = copy.deepcopy(page)
    rows, _ = run(lv.reconcile(tour([e1], t), stop + 120))
    check(ASC in {r["id"] for r in rows}, "off the list at 92', just past the estimated stop -> still stoppage time")
    page["playedSeconds"] = "97:56"
    lv.pages[ASC] = copy.deepcopy(page)
    rows, _ = run(lv.reconcile(tour([e1], t), stop + 7 * 60 + 20))
    rows, _ = run(lv.reconcile(tour([e1], t), stop + GRACE + 5))
    check(ASC not in {r["id"] for r in rows} and ASC in {r["id"] for r in lv.ended()},
          "off the list, 10 min past the estimated stop, clock still ticking -> Ended")
    # clock past 105' in the 2nd half -> ended even while the feed still lists it
    lv = FakeLive()
    x = copy.deepcopy(e)
    x["playedSeconds"] = "106:30"
    page["playedSeconds"] = "106:40"
    lv.pages[ASC] = copy.deepcopy(page)
    rows, _ = run(lv.reconcile(tour([x], t), stop - 600))
    check(ASC not in {r["id"] for r in rows}, "2nd-half clock at 106' -> not live (no half lasts that long)")
    # match view: ticking clock, off the live list, past the stop -> FT
    lv = FakeLive()
    lv.feed = tour([e1], t)
    run(lv.list(force=True))
    page["playedSeconds"] = "99:10"
    page["estimateStopTime"] = int((time.time() - 15 * 60) * 1000)
    lv.pages[ASC] = copy.deepcopy(page)
    d = run(lv.match(ASC))
    check(d["head"]["live"] is False and d["head"]["badge"] == "FT", "match view: off the list 15 min past stop -> FT")


def caps() -> None:
    print("Live list: kickoff + 150 min cap")
    e, t = feed_event(ASC)                          # Bet Genius
    start = e["estimateStartTime"] / 1000
    t0 = start + 160 * 60
    # frozen past the cap, event page the same -> ended
    lv = FakeLive()
    lv.pages[ASC] = {**copy.deepcopy(FX["events"][ASC]), **{k: e[k] for k in ("matchStatus", "playedSeconds", "setScore")}}
    rows, _ = run(lv.reconcile(tour([e], t), t0))
    check(ASC in {r["id"] for r in rows}, "first look past the cap (no Sportradar feed): benefit of the doubt")
    rows, _ = run(lv.reconcile(tour([e], t), t0 + FROZEN_S + 5))
    check(ASC not in {r["id"] for r in rows} and ASC in {r["id"] for r in lv.ended()},
          "past the cap, no clock/score change for 5 min -> Ended")
    # the same frozen row keeps coming from the feed: it stays ended (no flicker back to LIVE)
    rows, _ = run(lv.reconcile(tour([e], t), t0 + FROZEN_S + 25))
    check(ASC not in {r["id"] for r in rows}, "same frozen feed row again -> stays out of Live")
    # ...until it really moves again
    e2 = copy.deepcopy(e)
    e2["playedSeconds"] = "95:10"
    rows, _ = run(lv.reconcile(tour([e2], t), t0 + FROZEN_S + 45))
    check(ASC in {r["id"] for r in rows} and ASC not in {r["id"] for r in lv.ended()},
          "clock moves again -> back in Live, out of Ended")
    # moving past the cap -> stays live
    lv = FakeLive()
    for i in range(20):
        x = copy.deepcopy(e)
        x["playedSeconds"] = f"{80 + i // 3}:{(i * 20) % 60:02d}"
        rows, _ = run(lv.reconcile(tour([x], t), t0 + i * 20))
    check(ASC in {r["id"] for r in rows}, "past the cap but the clock keeps moving -> live")
    # past kickoff + 5 h only Sportradar's word keeps it live
    lv = FakeLive()
    lv.pages[ASC] = copy.deepcopy(FX["events"][ASC])
    rows, _ = run(lv.reconcile(tour([e], t), start + 301 * 60))
    check(ASC not in {r["id"] for r in rows}, "kickoff + 5 h, no Sportradar -> not live")
    # Sportradar-covered: first look past the cap asks Sportradar
    e3, t3 = feed_event(IMABARI)
    s3 = e3["estimateStartTime"] / 1000
    num = IMABARI.rsplit(":", 1)[-1]
    lv = FakeLive()
    lv.pages[IMABARI] = copy.deepcopy(FX["events"][IMABARI])
    lv.sr[f"match_info/{num}"] = {"match": {"status": {"_id": 100, "name": "Ended"}, "result": {"home": 0, "away": 3}}}
    rows, _ = run(lv.reconcile(tour([e3], t3), s3 + 155 * 60))
    check(IMABARI not in {r["id"] for r in rows} and {r["id"]: r for r in lv.ended()}[IMABARI]["badge"] == "FT",
          "Sportradar-covered, first look past the cap, Sportradar Ended -> FT immediately")
    lv = FakeLive()
    lv.pages[IMABARI] = copy.deepcopy(FX["events"][IMABARI])
    lv.sr[f"match_info/{num}"] = FX["sr_match_info"][num]      # real: 2nd half, running True
    rows, _ = run(lv.reconcile(tour([e3], t3), s3 + 155 * 60))
    check(IMABARI in {r["id"] for r in rows}, "Sportradar clock running past the cap (late kickoff) -> live")


def stale_list() -> None:
    print("Live list: failed refresh")
    lv = FakeLive()
    e, t = feed_event(IMABARI)
    lv.feed = tour([e], t)
    rows = run(lv.list(force=True))
    check(len(rows) == 1, "first list")
    lv.feed = httpx.ConnectError("down")
    rows = run(lv.list(force=True))
    check(len(rows) == 1, "failed refresh within 60 s -> last list")
    lv._list = (time.time() - 61, lv._list[1], lv._list[2])
    try:
        run(lv.list(force=True))
        check(False, "failed refresh after 60 s must not serve the stale live list")
    except httpx.HTTPError:
        check(True, "failed refresh after 60 s -> error, not a stale live list")


def match_view() -> None:
    print("Match view head")
    lv = FakeLive()
    num = IMABARI.rsplit(":", 1)[-1]
    lv.pages[IMABARI] = copy.deepcopy(FX["events"][IMABARI])
    lv.sr[f"match_timeline/{num}"] = {"match": {"status": {"_id": 100, "name": "Ended"}, "result": {"home": 0, "away": 3},
                                                "timeinfo": {"ended": "1", "running": False}}, "events": []}
    d = run(lv.match(IMABARI))
    h = d["head"]
    check(h["live"] is False and h["ended"] and h["badge"] == "FT" and h["minute"] == "FT" and h["score"] == "0–3"
          and d["markets"] == [], "event page says 1 but Sportradar Ended -> FT 0–3, no live odds")
    # real 07:46 UTC 2026-11-03: match_info Ended (ended 1793691970) while match_timeline still said 2nd half, running,
    # and SportyBet's page said status 1, H2, 90:00
    lv = FakeLive()
    page = copy.deepcopy(FX["events"][IMABARI])
    page.update(playedSeconds="90:00", setScore="0:4")
    lv.pages[IMABARI] = page
    lv.sr[f"match_timeline/{num}"] = {"match": {"status": {"_id": 7, "name": "2nd half"}, "result": {"home": 0, "away": 4},
                                                "timeinfo": {"injurytime": "4", "ended": None, "played": "5698",
                                                             "running": True}}, "events": []}
    lv.sr[f"match_info/{num}"] = {"match": {"status": {"_id": 100, "name": "Ended"}, "result": {"home": 0, "away": 4},
                                            "timeinfo": {"ended": "1793691970", "played": "5723", "running": False}}}
    d = run(lv.match(IMABARI))
    check(d["head"]["live"] is False and d["head"]["badge"] == "FT" and d["head"]["score"] == "0–4",
          "match_info Ended beats a lagging match_timeline 'running' -> FT 0–4")
    lv = FakeLive()
    lv.pages[SCHALKE] = copy.deepcopy(FX["events"][SCHALKE])
    d = run(lv.match(SCHALKE))
    check(d["head"]["live"] is False and d["head"]["minute"] == "FT", "ended event page -> FT (was 91')")
    lv = FakeLive()
    lv.pages[IMABARI] = copy.deepcopy(FX["events"][IMABARI])
    lv.sr[f"match_timeline/{num}"] = {"match": FX["sr_match_info"][num]["match"], "events": []}
    d = run(lv.match(IMABARI))
    check(d["head"]["live"] is True and d["head"]["badge"] == "LIVE", "in play on both -> LIVE")
    ev = copy.deepcopy(FX["events"][ASC])
    ev["estimateStartTime"] = int((time.time() - 6 * 3600) * 1000)
    lv = FakeLive()
    lv.pages[ASC] = ev
    d = run(lv.match(ASC))
    check(d["head"]["live"] is False, "event page still 'live' 6 h after kickoff, no Sportradar -> not live")


def analyzer_legs() -> None:
    print("Analyzer legs (share payload)")
    data = json.loads((Path(__file__).parent / "fixtures" / "share_N6RXGP.json").read_text())
    data = data.get("data", data)
    o = data["outcomes"][0]
    variants = {"ended-1": dict(status=1, matchStatus="Ended"), "aet": dict(status=3, matchStatus="AET"),
                "s9": dict(status=9, matchStatus="H2"), "s2": dict(status=2, matchStatus="H2"),
                "abd": dict(status=1, matchStatus="Abandoned"), "ns": dict(status=0, matchStatus="Not start"),
                "tick": dict(status=1, matchStatus="H2", playedSeconds="107:10")}
    out = {}
    for k, v in variants.items():
        x = copy.deepcopy(o)
        x.update(v)
        out[k] = share_legs({"outcomes": [x]})[0]["state"]
    check(out == {"ended-1": "ended", "aet": "ended", "s9": "live", "s2": "live", "abd": "cancelled", "ns": "upcoming",
                  "tick": "ended"},
          f"share leg states {out}")


class FakeRC(ResultsClient):
    def __init__(self, docs, page=None):
        self._client = None
        self.docs = docs

        class BM:
            async def _event_raw(_s, eid):
                return copy.deepcopy(page)
        self.bm = BM()

    async def _doc(self, path):
        return copy.deepcopy(self.docs.get(path.split("/")[0]))


def settlement() -> None:
    print("Settlement uses the same states")
    leg_o25 = {"event_id": IMABARI, "market_id": "18", "outcome_id": "12", "specifier": "total=2.5", "market": "", "pick": ""}
    leg_1 = {"event_id": IMABARI, "market_id": "1", "outcome_id": "3", "specifier": "", "market": "", "pick": ""}

    def mi(sid, name, result=None, ft=None, p1=None, timeinfo=None):
        return {"match_info": {"match": {"status": {"_id": sid, "name": name}, "result": result,
                                         "periods": {k: v for k, v in (("ft", ft), ("p1", p1)) if v},
                                         "timeinfo": timeinfo or {}}}}
    r = run(FakeRC(mi(110, "AET", {"home": 2, "away": 1}, {"home": 1, "away": 1}, {"home": 0, "away": 1})).result(IMABARI))
    check(r.state == "ended" and r.ft == (1, 1), "AET -> ended, regular-time score 1-1 (extra time excluded)")
    r = run(FakeRC(mi(120, "AP", {"home": 5, "away": 4})).result(IMABARI))
    check(r.state == "unknown", "AP with no regular-time period -> waits (no wrong FT from the shoot-out total)")
    r = run(FakeRC(mi(92, "Retired")).result(IMABARI))
    check(r.state == "void" and settle_leg(leg_1, r)[0] == "void", "retired -> void (was 'live')")
    r = run(FakeRC(mi(81, "Suspended")).result(IMABARI))
    check(r.state == "postponed" and settle_leg(leg_1, r)[0] == "pending", "suspended -> waits, not live")
    r = run(FakeRC(mi(7, "2nd half", {"home": 0, "away": 3}, timeinfo={"ended": "1793463830"})).result(IMABARI))
    check(r.state == "ended" and r.ft == (0, 3) and settle_leg(leg_1, r)[0] == "won",
          "status still '2nd half' but timeinfo ended -> ended and settled")
    r = run(FakeRC(mi(7, "2nd half", {"home": 2, "away": 1}, p1={"home": 1, "away": 0},
                      timeinfo={"played": "3900", "running": True})).result(IMABARI))
    check(r.state == "live" and r.phase == "2h" and early_settle(leg_o25, r) == ("won", "Won early (66')"),
          "live 2nd half 2-1: Over 2.5 locked early as before")
    r = run(FakeRC(mi(41, "1st extra", {"home": 2, "away": 1})).result(IMABARI))
    check(r.state == "live" and early_settle(leg_o25, r) is None, "extra time: no early calls")
    # Bet Genius match: no Sportradar doc -> SportyBet event page
    page = copy.deepcopy(FX["events"][ASC])
    page.update(status=4, matchStatus="Ended", setScore="0:1", gameScore=["0:0", "0:1"])
    r = run(FakeRC({}, page).result(ASC))
    check(r.state == "ended" and r.ft == (0, 1) and r.ht == (0, 0) and settle_leg(dict(leg_1, event_id=ASC), r)[0] == "won",
          "Bet Genius ended -> settled from SportyBet's halves (0-0, 0-1)")
    page.update(status=1, matchStatus="H2", setScore="2:1")
    r = run(FakeRC({}, page).result(ASC))
    check(r.state == "live" and r.phase == "other" and early_settle(dict(leg_o25, event_id=ASC), r) is None,
          "Bet Genius in play -> live but never called early from that feed")
    page.update(status=1, matchStatus="H2", playedSeconds="108:00")
    check(run(FakeRC({}, page).result(ASC)).state == "unknown", "Bet Genius clock at 108' -> not live (waits for the result)")
    page.update(status=4, matchStatus="AET", gameScore=["0:0", "1:1", "1:0"])
    r = run(FakeRC({}, page).result(ASC))
    check(r.state == "unknown", "Bet Genius AET -> waits (no regular-time split)")
    page.update(status=1, matchStatus="Abandoned")
    check(run(FakeRC({}, page).result(ASC)).state == "void", "Bet Genius abandoned -> void")
    r = run(FakeRC({}, None).result(IMABARI))
    check(r.state == "unknown", "Sportradar id with no doc -> unknown (no SportyBet guess)")
    check(settle_leg(leg_1, MatchResult(state="live", status_name="2nd half"))[0] == "live", "live stays live")


def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
    mapping()
    minutes()
    feed_states()
    real_feed()
    disappear()
    ticking_clock()
    caps()
    stale_list()
    match_view()
    analyzer_legs()
    settlement()
    print("FAILURES:", FAILS)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
