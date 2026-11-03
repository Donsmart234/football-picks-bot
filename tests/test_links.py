"""Booking-code links open the SportyBet app (offline, no network, no booking, no Telegram sends).

    python tests/test_links.py

Every place that shows a booking code links to SportyBet's own share URL https://www.sportybet.com/?shareCode=CODE&c=ng.
www.sportybet.com is app-claimed: its apple-app-site-association lists path "/" with ?shareCode=*&c=xx as
"Open share code" (iOS app 7423W9V4WF.com.sportybet.SportyBet) and its assetlinks.json verifies the Android apps
com.sportybet.android / com.sportybet.android.gp, so the OS opens the installed app on the code, the website otherwise.
Telegram side: url buttons (never web_app buttons) and, in the Mini App, Telegram.WebApp.openLink(url,
{try_instant_view: false}); the code itself stays in <code> (tap to copy).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")

FAILS: list[str] = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  ok  ", msg)


def main() -> None:
    import bookmakers as B
    from fmt import code_html, open_link, record_text
    from tracker import day_markup
    import handlers as H

    print("share URL")
    check(B.sportybet_share_url("N6RXGP") == "https://www.sportybet.com/?shareCode=N6RXGP&c=ng", "format = SportyBet's own")
    check(B.sportybet_share_url(" n6rxgp ") == "https://www.sportybet.com/?shareCode=N6RXGP&c=ng", "trimmed, upper-cased")
    check(B.sportybet_share_url("AB<script>12") == "https://www.sportybet.com/?shareCode=ABSCRIPT12&c=ng",
          "only letters/digits reach the URL")
    check(B.sportybet_share_url("") == B.SportyBetNG.load_code_url, "no code -> load-code page")

    print("adapter / code_link")
    sb = B.SportyBetNG.__new__(B.SportyBetNG)
    check(sb.code_url("GK69V5") == "https://www.sportybet.com/?shareCode=GK69V5&c=ng", "SportyBetNG.code_url")
    check(B.code_link(sb, "GK69V5").endswith("shareCode=GK69V5&c=ng"), "code_link(SportyBetNG)")
    check(B.code_link(sb, None) == B.SportyBetNG.load_code_url, "code_link without code -> code hub")
    check(B.code_link(None, "GK69V5").endswith("shareCode=GK69V5&c=ng"), "code_link(None) -> SportyBet share URL")

    class Fake:                         # test doubles without code_url keep their load_code_url
        load_code_url = "https://example/load"
    check(B.code_link(Fake(), "X1Y2Z3") == "https://example/load", "adapter without code_url -> load_code_url")

    print("Telegram buttons")
    kb = H.open_code_kb(sb, "GK69V5")
    btn = kb.inline_keyboard[0][0]
    check(btn.url == "https://www.sportybet.com/?shareCode=GK69V5&c=ng" and btn.web_app is None,
          "open_code_kb: url button (opened outside the webview) to the share URL")
    check("GK69V5" in btn.text and "SportyBet" in btn.text, f"button label names the code: {btn.text!r}")
    check(H.open_code_kb(sb, None).inline_keyboard[0][0].url == B.SportyBetNG.load_code_url, "no code -> code hub button")
    dm = day_markup("s1", True, "GK69V5")
    check(dm.inline_keyboard[0][0].url == "https://www.sportybet.com/?shareCode=GK69V5&c=ng",
          "rollover day: first button opens the code")
    check(all(b.url is None for b in day_markup("s1", False).inline_keyboard[0]), "rollover day without code: no link")
    ak = H._analysis_kb({"id": "a1", "code": "N6RXGP", "load_url": B.code_link(sb, "N6RXGP"), "suggestions": {}})
    check(ak.inline_keyboard[-1][1].url.endswith("shareCode=N6RXGP&c=ng"), "analyzer: Open button opens the analyzed code")

    print("Telegram HTML")
    h = code_html("GK69V5")
    check(h.startswith("<code>GK69V5</code>"), "code stays monospace (tap to copy)")
    check('href="https://www.sportybet.com/?shareCode=GK69V5&amp;c=ng"' in h, "href is HTML-escaped (&amp;)")
    check(open_link(None) == "" and code_html("") == "", "no code -> no link")
    rec = {"data": {"code": "GK69V5", "total_odds": 2.1, "legs": []}, "status": "pending"}
    check("shareCode=GK69V5" in record_text(rec, "t"), "record_text links the code")
    line = H._hist_line({"data": {"code": "GK69V5", "legs": []}, "status": "pending", "created_at": "2026-11-03T08:00:00Z"})
    check("shareCode=GK69V5" in line and "<code>GK69V5</code>" in line, "/history: running slip has code + link")
    done = H._hist_line({"data": {"code": "AAA111", "legs": []}, "status": "lost", "created_at": "2026-11-03T08:00:00Z"})
    check("shareCode" not in done and "<code>AAA111</code>" in done, "/history: settled slip shows the code only")

    print("Mini App")
    js = (ROOT / "webapp_static" / "app.js").read_text()
    check("https://www.sportybet.com/?shareCode=${encodeURIComponent(k)}&c=ng" in js, "codeUrl builds the share URL")
    check("try_instant_view: false" in js, "openLink passes try_instant_view:false")
    check('data-act="opencode"' in js and 'a === "opencode"' in js, "opencode buttons + handler")
    check(js.count('data-act="openhub"') == 0, "no code sheet still points at the generic code hub")

    print("\nFAILURES:", len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
