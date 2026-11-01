"""Offline tests for the Live view (live.py) and the official-stream filter (youtube.py).

    python tests/test_live.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live  # noqa: E402
import youtube  # noqa: E402

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  PASS " if cond else "  FAIL ") + msg)
    FAILS += 0 if cond else 1


def main() -> None:
    fx = json.loads((Path(__file__).parent / "fixtures" / "live_sample.json").read_text())
    v = live.feed_view(fx["timeline"], fx["details"])
    check(v["status"] and v["status"]["name"], f"status: {v['status']['name']}")
    check(any(e["type"] == "goal" and e.get("score") for e in v["events"]), "goals carry the score")
    check(any(e["type"] == "card" and e.get("card") in ("yellow", "red") for e in v["events"]), "cards typed")
    check(len(v["momentum"]) > 10, f"momentum series ({len(v['momentum'])} minutes)")
    check(v["ball"] and 0 <= v["ball"]["x"] <= 100, "ball position from the feed")
    check(any(s["label"] == "Possession %" for s in v["stats"]), "match stats parsed")
    mk = live.live_markets(fx["event"])
    check(mk and mk[0]["id"] == "1", "1X2 first among live markets")
    check(all(abs(sum(o["prob"] for o in m["outcomes"]) - (2 if m["id"] == "10" else 1)) < 1e-6
              for m in mk if all(o["prob"] for o in m["outcomes"])), "fair probabilities sum to 1 (DC: 2)")
    h = live.compact_event(fx["event"], {"name": "J1 League", "id": "sr:tournament:196"})
    check(h["url"].startswith("https://www.sportybet.com/ng/sport/football/live/Japan/J1_League/") and h["url"].endswith(h["id"]),
          "official SportyBet event link")
    check(live._minute("44:10", "H1") == "45'" and live._minute("46:00", "H1") == "45+2'" and live._minute("", "HT") == "HT",
          "minute formatting")
    check(live.stream_flag({"liveStreamInfo": {"available": True}}) and not live.stream_flag({"eventId": "x", "markets": []}),
          "stream flag only when SportyBet marks one")
    # official-stream filter: spam / unofficial channels are rejected, a verified club channel naming the opponent passes
    base = {"id": "abc", "live": True, "verified": True}
    check(youtube.official_match({**base, "title": "Arsenal v Chelsea | LIVE", "channel": "Arsenal"},
                                 "Arsenal", "Chelsea", "England · Premier League") is not None, "verified club channel accepted")
    check(youtube.official_match({**base, "title": "LIVE | Arsenal vs Chelsea", "channel": "Fry Irish"},
                                 "Arsenal", "Chelsea", "England · Premier League") is None, "verified but unrelated channel rejected")
    check(youtube.official_match({**base, "verified": False, "title": "Arsenal v Chelsea", "channel": "Arsenal"},
                                 "Arsenal", "Chelsea", "England · Premier League") is None, "unverified channel rejected")
    check(youtube.official_match({**base, "title": "Arsenal vs Chelsea LIVE SCORE", "channel": "Premier League"},
                                 "Arsenal", "Chelsea", "England · Premier League") is None, "score tickers rejected")
    check(youtube.official_match({**base, "live": False, "title": "Arsenal v Chelsea", "channel": "Arsenal"},
                                 "Arsenal", "Chelsea", "England · Premier League") is None, "not live -> rejected")
    print("FAILURES:", FAILS)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
