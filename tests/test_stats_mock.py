"""Offline test of the API-Football path (mocked HTTP): fixture match, stats parsing, quota, caching."""
import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bookmakers import Event, Outcome  # noqa: E402
from predictor import predict  # noqa: E402
from stats import ApiFootball  # noqa: E402
from storage import Storage  # noqa: E402

KICK = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
calls = []


def handler(req: httpx.Request) -> httpx.Response:
    calls.append(req.url.path)
    h = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": str(100 - len(calls))}
    if req.url.path == "/fixtures":
        return httpx.Response(200, headers=h, json={"errors": [], "response": [{
            "fixture": {"id": 1, "date": "2026-10-31T15:00:00+00:00"},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 49, "name": "Chelsea"}}}]})
    if req.url.path == "/teams/statistics":
        team = int(req.url.params["team"])
        gf = {"home": "2.2", "away": "1.6"} if team == 42 else {"home": "1.5", "away": "1.1"}
        ga = {"home": "0.8", "away": "1.0"} if team == 42 else {"home": "1.2", "away": "1.6"}
        return httpx.Response(200, headers=h, json={"errors": [], "response": {
            "form": "WWDWW" if team == 42 else "LDLWL", "fixtures": {"played": {"home": 5, "away": 5}},
            "goals": {"for": {"average": gf}, "against": {"average": ga}}}})
    if req.url.path == "/fixtures/headtohead":
        return httpx.Response(200, headers=h, json={"errors": [], "response": [
            {"teams": {"home": {"id": 42}, "away": {"id": 49}}, "goals": {"home": 2, "away": 1}},
            {"teams": {"home": {"id": 49}, "away": {"id": 42}}, "goals": {"home": 0, "away": 3}},
            {"teams": {"home": {"id": 42}, "away": {"id": 49}}, "goals": {"home": 1, "away": 1}}]})
    return httpx.Response(404)


async def main():
    st = Storage(Path(tempfile.mkdtemp()) / "t.sqlite3")
    api = ApiFootball(st, api_key="dummy-not-a-real-key", daily_limit=5)
    api._client = httpx.AsyncClient(base_url=api.BASE, transport=httpx.MockTransport(handler))
    ms = await api.match_stats("Arsenal FC", "Chelsea", KICK)
    assert ms and ms.home.gf_home == 2.2 and ms.away.form == "LDLWL" and len(ms.h2h) == 3, ms
    assert ms.h2h[1] == (3, 0), ms.h2h
    n = len(calls)
    await api.match_stats("Arsenal", "Chelsea", KICK)
    assert len(calls) == n == 4, calls  # second time fully cached
    assert st.api_used_today() == 4
    st.cache_purge()
    api2 = ApiFootball(st, api_key="dummy", daily_limit=4)  # budget exhausted
    api2._client = api._client
    st._exec("DELETE FROM cache")
    assert await api2.match_stats("Arsenal", "Chelsea", KICK) is None  # quota -> None -> odds-only
    assert len(calls) == 4
    ev = Event("sr:match:1", "Arsenal", "Chelsea", KICK, "Premier League", "England", "sr:tournament:17")
    def m(pairs): return {k: Outcome(k, k, v, True) for k, v in pairs}
    ev.markets = {("1", ""): m([("1", 1.9), ("2", 3.6), ("3", 4.2)]),
                  ("18", "total=2.5"): m([("12", 1.85), ("13", 1.95)]),
                  ("10", ""): m([("9", 1.25), ("10", 1.3), ("11", 1.95)]),
                  ("29", ""): m([("74", 1.8), ("76", 1.95)])}
    p = predict(ev, ms)
    assert p.source == "stats+odds", p.source
    print(p.reasoning)
    print("best:", p.best.label, round(p.best.prob, 3), p.best.odds)
    print("stats mock test OK")
    await api._client.aclose()

asyncio.run(main())
