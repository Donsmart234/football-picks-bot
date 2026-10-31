"""Mini App tests.

    python tests/test_webapp.py            # offline: initData validation unit tests (fake bot token)
    python tests/test_webapp.py --live     # + local aiohttp server: /app, /api/events, /api/predict via relay
    python tests/test_webapp.py --book     # + ONE real (anonymous, free) booking code via /api/book

No Telegram token is needed: a fake token signs the initData.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp import MiniApp, sign_init_data, validate_init_data  # noqa: E402

FAKE_TOKEN = "123456:TEST-fake-token-not-real"
OWNER = 8590854295
FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  PASS " if cond else "  FAIL ") + msg)
    FAILS += 0 if cond else 1


def fields(uid: int = OWNER, age: int = 10) -> dict:
    return {"auth_date": str(int(time.time()) - age), "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps({"id": uid, "first_name": "Owner", "username": "owner", "language_code": "en"},
                               separators=(",", ":"))}


def unit() -> None:
    print("1) initData validation")
    good = sign_init_data(fields(), FAKE_TOKEN)
    check(validate_init_data(good, FAKE_TOKEN, OWNER) is not None, "valid owner initData accepted")
    check(validate_init_data(good, "999:other-token", OWNER) is None, "wrong bot token rejected")
    check(validate_init_data(sign_init_data(fields(uid=42), FAKE_TOKEN), FAKE_TOKEN, OWNER) is None,
          "other user rejected")
    check(validate_init_data(sign_init_data(fields(age=25 * 3600), FAKE_TOKEN), FAKE_TOKEN, OWNER) is None,
          "auth_date older than 24h rejected")
    check(validate_init_data(sign_init_data(fields(age=23 * 3600), FAKE_TOKEN), FAKE_TOKEN, OWNER) is not None,
          "auth_date 23h old accepted")
    tampered = good.replace("Owner", "Evil")
    check(validate_init_data(tampered, FAKE_TOKEN, OWNER) is None, "tampered payload rejected")
    no_hash = "&".join(p for p in good.split("&") if not p.startswith("hash="))
    check(validate_init_data(no_hash, FAKE_TOKEN, OWNER) is None, "missing hash rejected")
    check(validate_init_data("", FAKE_TOKEN, OWNER) is None, "empty rejected")
    check(validate_init_data(good, FAKE_TOKEN, None) is None, "no owner configured rejected")
    check(validate_init_data(good + "&auth_date=1", FAKE_TOKEN, OWNER) is None, "duplicate key rejected")
    # Known vector shape: Telegram signs every field except hash, sorted, joined by \n (incl. signature if present)
    with_sig = dict(fields(), signature="abc")
    check(validate_init_data(sign_init_data(with_sig, FAKE_TOKEN), FAKE_TOKEN, OWNER) is not None,
          "extra fields (signature) included in check string")


async def live(do_book: bool) -> None:
    import httpx
    from aiohttp import web

    from services import Services
    from storage import Storage

    print("2) local server")
    svc = Services(Storage(Path(tempfile.mkdtemp()) / "t.sqlite3"))
    print("   relay:", "on" if svc.bookmaker.relay else "OFF (direct)")
    app = web.Application()
    app.router.add_get("/health", lambda _: web.Response(text="ok"))
    MiniApp(svc, bot_token=FAKE_TOKEN, owner_id=OWNER).register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8099).start()
    hdr = {"X-Telegram-Init-Data": sign_init_data(fields(), FAKE_TOKEN)}
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8099", timeout=120, trust_env=False) as c:
            r = await c.get("/app")
            check(r.status_code == 200 and "telegram-web-app.js" in r.text, "/app serves index.html")
            check((await c.get("/app/app.js")).status_code == 200, "/app/app.js served")
            check((await c.get("/app/app.css")).status_code == 200, "/app/app.css served")
            check((await c.get("/app/../config.py")).status_code == 404, "no path traversal")
            check((await c.get("/api/events?range=today")).status_code == 403, "/api/events 403 without initData")
            bad = {"X-Telegram-Init-Data": sign_init_data(fields(uid=42), FAKE_TOKEN)}
            check((await c.get("/api/events?range=today", headers=bad)).status_code == 403, "non-owner 403")
            t = time.time()
            r = await c.get("/api/events?range=today", headers=hdr)
            j = r.json()
            check(r.status_code == 200 and j["count"] > 0, f"/api/events today: {j.get('count')} matches, "
                  f"{len(j.get('leagues', []))} leagues in {time.time() - t:.1f}s ({len(r.content) // 1024} KB)")
            t = time.time()
            r2 = await c.get("/api/events?range=today", headers=hdr)
            check(r2.content == r.content and time.time() - t < 0.5, "second call served from cache")
            r = await c.get("/api/events?range=upcoming", headers=hdr)
            ju = r.json()
            check(r.status_code == 200 and ju["count"] >= j["count"],
                  f"/api/events upcoming: {ju.get('count')} matches ({len(r.content) // 1024} KB)")
            m = next(m for l in ju["leagues"] for m in l["matches"] if m["odds"]["1"])
            print(f"   first league: {ju['leagues'][0]['name']}; sample: {m['day']} {m['time']} "
                  f"{m['home']} vs {m['away']} {m['odds']}")
            r = await c.post("/api/predict", json={"event_id": m["id"]}, headers=hdr)
            p = r.json()
            check(r.status_code == 200 and len(p.get("markets", [])) == 4 and p.get("disclaimer"),
                  f"/api/predict ok: best={p.get('best', {}) and p['best']['label']} source={p.get('source')}")
            check((await c.post("/api/predict", json={"event_id": "sr:match:0"}, headers=hdr)).status_code == 404,
                  "unknown event -> 404")
            check((await c.post("/api/book", json={"selections": []}, headers=hdr)).status_code == 400,
                  "empty slip -> 400")
            if do_book:
                picks, used = [], set()
                for l in ju["leagues"]:
                    for mm in l["matches"]:
                        if mm["odds"]["1"] and mm["id"] not in used and len(picks) < 2:
                            used.add(mm["id"])
                            picks.append({"event_id": mm["id"], "market_id": "1", "specifier": "",
                                          "outcome_id": "1"})
                r = await c.post("/api/book", json={"selections": picks}, headers=hdr)
                b = r.json()
                check(r.status_code == 200 and b.get("code"), f"/api/book -> code {b.get('code') or b}")
    finally:
        await runner.cleanup()
        await svc.aclose()


if __name__ == "__main__":
    unit()
    if "--live" in sys.argv or "--book" in sys.argv:
        asyncio.run(live("--book" in sys.argv))
    print("FAILURES:", FAILS)
    sys.exit(1 if FAILS else 0)
