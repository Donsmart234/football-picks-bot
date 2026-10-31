"""Live sandbox test (no Telegram token needed).

    python tests/live_test.py            # fetch, verify ids, predict 3 matches, offline checks
    python tests/live_test.py --book     # ...and create ONE real SportyBet booking code (2 selections)

Booking codes are anonymous share codes: no account, no money.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bookmakers import (MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, OU_SPECIFIER, SELECTIONS,  # noqa: E402
                        Selection, SportyBetNG)
from handlers import format_prediction, prediction_keyboard  # noqa: E402
from matching import rank_events  # noqa: E402
from services import Services  # noqa: E402
from stats import ApiFootball  # noqa: E402
from storage import Storage  # noqa: E402

EXPECTED_DESC = {
    (MARKET_1X2, "1"): "Home", (MARKET_1X2, "2"): "Draw", (MARKET_1X2, "3"): "Away",
    (MARKET_OU, "12"): "Over 2.5", (MARKET_OU, "13"): "Under 2.5",
    (MARKET_DC, "9"): "Home or Draw", (MARKET_DC, "10"): "Home or Away", (MARKET_DC, "11"): "Draw or Away",
    (MARKET_BTTS, "74"): "Yes", (MARKET_BTTS, "76"): "No",
}


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        check.failures += 1


check.failures = 0


async def main(do_book: bool) -> int:
    tmp = Path(tempfile.mkdtemp()) / "test.sqlite3"
    storage = Storage(tmp)
    bm = SportyBetNG()
    stats = ApiFootball(storage)  # uses API_FOOTBALL_KEY from env if present
    svc = Services(storage, bm, stats)

    print("1) SportyBet feed")
    t0 = datetime.now()
    events = await bm.list_events(24)
    print(f"   {len(events)} events in next 24h, fetched in {(datetime.now() - t0).total_seconds():.1f}s")
    check(len(events) > 100, "feed returns >100 football events")
    t1 = datetime.now()
    await bm.list_events(24)
    check((datetime.now() - t1).total_seconds() < 0.05, "second call served from 5-min cache")
    today = await svc.events("t")
    up = await svc.events("u")
    print(f"   today (Lagos) remaining: {len(today)}, next 3 days: {len(up)}, leagues today: {len(svc.leagues(today))}")

    print("2) Market / outcome ids against live data")
    descs: dict = {}
    for e in events:
        for (mid, spec), outs in e.markets.items():
            for oid, o in outs.items():
                if (mid, oid) in EXPECTED_DESC and (mid != MARKET_OU or spec == OU_SPECIFIER):
                    descs.setdefault((mid, oid), o.desc)
    for k, want in EXPECTED_DESC.items():
        check(descs.get(k) == want, f"market {k[0]} outcome {k[1]} = {want!r} (live: {descs.get(k)!r})")

    print("3) Predictions on 3 matches")
    now = datetime.now(timezone.utc)
    candidates = [e for e in events if e.start > now + timedelta(minutes=90)
                  and all(e.odds(m, o, s) for (m, o), (_, s) in SELECTIONS.items())]
    top = {("England", "Premier League"), ("Spain", "LaLiga"), ("Italy", "Serie A"), ("Germany", "Bundesliga"),
           ("France", "Ligue 1"), ("England", "Championship")}
    big = [e for e in candidates if (e.category, e.league) in top]
    chosen = (big + [e for e in candidates if e not in big])[:3]
    preds = []
    for e in chosen:
        p = await svc.predict(e)
        preds.append(p)
        print("   ---")
        print("   " + format_prediction(p).replace("\n", "\n   "))
        tot = sum(p.probs[(MARKET_1X2, o)] for o in "123")
        check(abs(tot - 1) < 1e-6, "1X2 probabilities sum to 1")
        kb = prediction_keyboard(p)
        lens = [len(b.callback_data.encode()) for row in kb.inline_keyboard for b in row if b.callback_data]
        check(max(lens) <= 64, f"callback_data <= 64 bytes (max {max(lens)})")
    print(f"   model source: {preds[0].source if preds else 'n/a'}; API-Football used today: {storage.api_used_today()}")

    print("4) Offline checks")
    if chosen:
        e = chosen[0]
        r = rank_events(f"{e.home.lower()} vs {e.away}", events, 3)
        check(r[0][1].event_id == e.event_id, f"fuzzy match finds '{e.name}' (score {r[0][0]:.0f})")
    os.environ.pop("OWNER_TELEGRAM_ID", None)
    check(storage.get_owner() is None, "no owner before first /start")
    check(storage.claim_owner(111) is True, "first user claims owner")
    check(storage.claim_owner(222) is False, "second user is rejected")
    check(Storage(tmp).get_owner() == 111, "owner persisted to SQLite")
    storage.slip_add(111, "ev1", "1", "", "1", "1 (Home)", 2.0, "A vs B")
    replaced = storage.slip_add(111, "ev1", "18", OU_SPECIFIER, "12", "Over 2.5", 1.8, "A vs B")
    check(replaced and len(storage.slip_get(111)) == 1, "one pick per event (replace)")
    storage.slip_clear(111)

    from aiohttp.test_utils import TestClient, TestServer
    from aiohttp import web
    import main as entry
    app = web.Application()
    app.router.add_get("/health", entry.health)
    async with TestClient(TestServer(app)) as c:
        r1 = await c.get("/health")
        r2 = await c.head("/health")
        check(r1.status == 200 and r2.status == 200, "/health returns 200 for GET and HEAD")

    if do_book:
        print("5) Real booking code (2 selections)")
        picks = [p for p in preds if p and p.best][:2]
        if len(picks) < 2:
            check(False, "need 2 predicted matches to book")
        else:
            sels = [Selection(p.event.event_id, p.best.market_id, p.best.specifier, p.best.outcome_id) for p in picks]
            for p in picks:
                print(f"   {p.event.name} [{p.event.event_id}] -> {p.best.label} market={p.best.market_id} "
                      f"spec={p.best.specifier!r} outcome={p.best.outcome_id} @ {p.best.odds}")
            code = await bm.book(sels)
            print(f"   BOOKING CODE: {code}")
            check(bool(code), "share endpoint returned a code")

    await svc.aclose()
    print(f"\n{'ALL PASSED' if not check.failures else f'{check.failures} FAILURE(S)'}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--book" in sys.argv)))
