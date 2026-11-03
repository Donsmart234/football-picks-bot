"""No emoji anywhere in the product (offline, no network, no Telegram sends).

    python tests/test_no_emoji.py

The bot's Telegram messages and buttons, notification pings, analyzer verdicts, the Mini App (webapp_static) and the
slip image use plain text labels (WON / LOST / LIVE / VOID / REPLACED ...) and, in the Mini App, inline SVG icons.
AI replies are told not to use emoji (system prompt) and are stripped before they are sent (fmt.strip_emoji).
Drawn shapes on the slip image (ball, badges, bars, the Nigeria flag drawn as two green bands) are fine; emoji glyphs
(including regional-indicator flags) are not.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("DATA_DIR", "/tmp/fra_noemoji_test")

FAILS: list[str] = []

# Independent of fmt.EMOJI_CLASS on purpose: pictographs, dingbats (incl. check/cross marks), misc technical
# (hourglass, play/pause), arrows that have emoji presentation, regional indicators (flags), tag sequences,
# keycap, variation selector-16 and ZWJ.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U000E0000-\U000E007F\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u20E3\u200D"
    "\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2934\u2935"
    "\u3030\u303D\u3297\u3299]")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  ok  ", msg)


def emoji_in(text: str) -> list[str]:
    return sorted({f"U+{ord(c):04X}" for c in EMOJI.findall(text or "")})


def no_emoji(text: str, what: str):
    found = emoji_in(text)
    check(not found, f"{what}: no emoji" + (f" (found {', '.join(found)})" if found else ""))


def kb_text(kb) -> str:
    if kb is None:
        return ""
    return "\n".join(b.text for row in kb.inline_keyboard for b in row)


# ---------------------------------------------------------------- 1. static files
def static_files():
    files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "webapp_static").glob("*.js")) + \
        sorted((ROOT / "webapp_static").glob("*.css")) + sorted((ROOT / "webapp_static").glob("*.html"))
    check(len(files) > 20, f"scanning {len(files)} source / static files")
    for p in files:
        bad = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            f = emoji_in(line)
            if f:
                bad.append(f"{i}: {', '.join(f)}")
        check(not bad, f"{p.relative_to(ROOT)}" + (f" has emoji on lines {bad[:6]}" if bad else ""))
    js = (ROOT / "webapp_static" / "app.js").read_text(encoding="utf-8")
    check("const IC = {" in js and "<svg class=\"i" in js, "Mini App: inline SVG icon set present")
    check('"WON"' in js and '"LOST"' in js and '"REPLACED"' in js and '"CANCELLED"' in js,
          "Mini App: status pills use text labels")


# ---------------------------------------------------------------- 2. strip_emoji
def strip_cases():
    from fmt import strip_emoji
    cases = {
        "\u2705 Done": "Done",
        "Great pick \U0001F525\U0001F525 here": "Great pick here",
        "- \u26bd Arsenal v Everton": "- Arsenal v Everton",
        "I'd go for this \U0001F44D.": "I'd go for this.",
        "Nigeria \U0001F1F3\U0001F1EC wins": "Nigeria wins",
        "**\u2705 Rules**: ok": "**Rules**: ok",
        "\u26a0\ufe0f Note: risky": "Note: risky",
        "plain · text → ≥80% × 2 – fine": "plain · text → ≥80% × 2 – fine",
        "line one \U0001F389\n    indented \u274c two": "line one\n    indented two",
    }
    for src, want in cases.items():
        got = strip_emoji(src)
        check(got == want and not emoji_in(got), f"strip_emoji({src!r}) -> {got!r}")


# ---------------------------------------------------------------- 3. rendered bot text
LEG = {"event_id": "sr:match:1", "home": "Arsenal", "away": "Everton", "market": "1X2", "pick": "Arsenal",
       "odds": 1.4, "est": 0.82, "kickoff": "2026-11-03T15:00:00Z", "league": "England · Premier League",
       "category": "England", "verdict": "confirmed", "why": "form WWDWW · caution: key striker out"}


def rendered_text():
    import fmt
    import handlers as H
    from tracker import day_markup, img_markup

    sl = {"legs": [LEG, {**LEG, "verdict": "demoted"}], "total_odds": 1.96, "est_prob": 0.6, "note": "short of target"}
    no_emoji(fmt.slip_text(sl, "<b>SAFE SLIP</b>"), "slip_text")
    statuses = ["won", "lost", "void", "live", "pending", "replaced", "check", "cancelled"]
    rec = {"id": "sl_1", "status": "pending", "created_at": "2026-11-03T09:00:00Z",
           "data": {"code": "AB12CD", "total_odds": 2.0, "label": "Safe",
                    "legs": [{**LEG, "status": s, "score": "1:0"} for s in statuses]}}
    txt = fmt.record_text(rec, "<b>Latest running slip</b>")
    no_emoji(txt, "record_text (every leg status)")
    check(all(w in txt for w in ("WON", "LOST", "VOID", "LIVE", "REPLACED", "CANCELLED")),
          "record_text: statuses spelled out (WON / LOST / VOID / LIVE / REPLACED / CANCELLED)")
    for st in ("won", "lost", "pending", "replaced", "void", "check"):
        no_emoji(H._hist_line({**rec, "status": st}), f"/history line ({st})")
    check("<b>WON</b>" in H._hist_line({**rec, "status": "won"}), "/history line says WON")
    plan = {"id": "ro_1", "status": "active", "data": {
        "target": 2, "days": 5, "current_day": 3, "stake": 1000, "value": 2000, "allow_unders": False,
        "log": [{"day": 1, "date": "2026-11-01", "status": "won", "code": "AAA111", "total_odds": 2.0, "slip_id": "a"},
                {"day": 2, "date": "2026-11-02", "status": "replaced", "code": "BBB222", "slip_id": "b"},
                {"day": 2, "date": "2026-11-02", "status": "pending", "code": "CCC333", "slip_id": "c"},
                {"day": 3, "date": "2026-11-03", "status": "no_slip", "note": "nothing qualifies"},
                {"day": 4, "date": "2026-11-04", "status": "skipped", "note": "skipped"}]}}
    no_emoji(H._ro_text(plan), "/rollover text")
    no_emoji(H._ro_text(None), "/rollover text (no plan)")
    no_emoji(kb_text(H._ro_kb(plan)), "/rollover buttons")
    tickets = [{"day": i + 1, "date": "2026-11-0%d" % (i + 1), "status": s, "code": "XY34ZQ", "total_odds": 2.0,
                "n_legs": 1, "slip_id": f"s{i}", "legs": [{**LEG, "status": s}]}
               for i, s in enumerate(["won", "lost", "replaced", "live", "pending", "no_slip", "skipped"])]
    msgs = H._tickets_msgs(plan, tickets)
    for m in msgs:
        no_emoji(m, "rollover tickets")
    check("REPLACED" in "".join(msgs) and "NO SLIP" in "".join(msgs), "tickets: REPLACED / NO SLIP spelled out")
    no_emoji(H.HELP + H.RG_NOTE, "/help and responsible-gambling note")
    no_emoji(kb_text(H.open_code_kb(SimpleNamespace(name="sportybet"), "AB12CD")), "open-code buttons")
    no_emoji(kb_text(day_markup("sl_1", True, "AB12CD")) + kb_text(day_markup("sl_1", False, "")), "rollover day buttons")
    no_emoji(kb_text(img_markup("sl_1")), "result-ping image button")
    no_emoji(H.img_btn().text, "slip image button")
    no_emoji(fmt.code_html("AB12CD"), "code link")


def notifications():
    """Result pings from the tracker (won early / won / lost / check / void / correction) are plain text."""
    import tracker as T
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=len(sent))

    tr = T.Tracker.__new__(T.Tracker)
    tr.bot = Bot()

    async def send(text, test=False, markup=None, **kw):
        sent.append(text)
        return True
    tr.send = send
    base = {"label": "Safe", "code": "AB12CD", "total_odds": 2.0}
    legs_won = [{**LEG, "status": "won"}]
    asyncio.run(tr._notify({"id": "1", "status": "won", "data": {**base, "legs": legs_won}}))
    asyncio.run(tr._notify({"id": "2", "status": "won", "data": {**base, "legs": [{**LEG, "status": "won",
                                                                                     "early": {"min": 63}}]}}))
    asyncio.run(tr._notify({"id": "3", "status": "lost", "data": {**base, "legs": [{**LEG, "status": "lost",
                                                                                      "score": "0:1"}]}}))
    asyncio.run(tr._notify({"id": "4", "status": "check", "data": {**base, "legs": [{**LEG, "status": "check"}]}}))
    asyncio.run(tr._notify({"id": "5", "status": "void", "data": {**base, "legs": [{**LEG, "status": "void"}]}}))
    check(len(sent) == 5, f"5 result pings rendered ({len(sent)})")
    for t in sent:
        no_emoji(t, "result ping " + t.split("\n")[0][:40])
    check("WON" in sent[0] and "LOST" in sent[2] and "VOID" in sent[4], "pings say WON / LOST / VOID in words")


def verdicts():
    import analyzer as A
    src = (ROOT / "analyzer.py").read_text(encoding="utf-8")
    check("I'd go for this" in src and "I'd skip this" in src, "verdict lines keep their wording")
    check(A.CAUTION and not emoji_in(A.CAUTION), "leg red-flag marker is plain text")


# ---------------------------------------------------------------- 4. AI
def ai_replies():
    import ai as AI
    check("NO EMOJI" in AI.SYSTEM and "Never use emoji" in AI.SYSTEM, "system prompt tells Gemini not to use emoji")
    a = AI.Analyst.__new__(AI.Analyst)

    async def gen(contents, system=None, tools=True, **kw):
        return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
            {"text": "\u2705 Rules: safe \U0001F525\n- \u26bd Arsenal v Everton \u2192 Arsenal @1.40 \u2705\n"
                     "\u26a0\ufe0f Estimates, not guarantees. I'd go for this \U0001F44D."}]}}]}
    a._generate = gen
    a.last_turn = {}
    out = asyncio.run(a._loop([{"role": "user", "parts": [{"text": "hi"}]}], {"tools": []}, tools=None))
    no_emoji(out, "AI reply after the loop")
    check(out.startswith("Rules: safe") and "I'd go for this." in out and "→ Arsenal" in out,
          f"AI reply keeps its words: {out!r}")


# ---------------------------------------------------------------- 5. slip image text
def slip_image():
    import slipimg as S
    spec = S.make_spec([{**LEG, "pick": "Arsenal \u2705", "status": "won"},
                        {**LEG, "home": "Chelsea", "away": "Spurs", "pick": "Over 1.5 \U0001F525", "status": "live",
                         "league": "\U0001F3F4 England · Premier League"}],
                       code="AB12CD", label="\U0001F525 Safe slip", note="\u26a0\ufe0f check")
    texts = [spec["label"], spec["note"]] + [str(l.get(k) or "") for l in spec["legs"] for k in
                                             ("pick", "market", "league", "home", "away", "match")]
    no_emoji("\n".join(texts), "slip image spec text (emoji in input stripped)")
    no_emoji(S.SlipImages.caption(spec), "slip image caption")
    png = S.render(spec, {})
    check(png[:4] == b"\x89PNG" and len(png) > 10000, "slip image still renders")


def main():
    print("static files"); static_files()
    print("strip_emoji"); strip_cases()
    print("rendered bot text"); rendered_text()
    print("notifications"); notifications()
    print("verdicts"); verdicts()
    print("AI"); ai_replies()
    print("slip image"); slip_image()
    print(f"\nFAILURES: {len(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
