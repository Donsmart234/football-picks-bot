"""Regression: /draw5 Weekend answered "Something went wrong on my side" (prod, 2026-11-06 05:34 WAT).

    python tests/test_draw5_telegram.py

Cause: draw5.page_html cut a long page at 4000 raw characters, through an open <i> tag, so Telegram refused the edit
("Can't parse entities: can't find end tag corresponding to start tag "i""). The fixture is a 20-pick slice of the
real weekend result from that morning (every 5-minute market suspended, long reasons).

Offline: a fake Telegram message that rejects what Telegram rejects (unbalanced / unknown HTML tags, more than 4096
characters after entity parsing, callback data over 64 bytes). Runs the real callback path (on_callback -> D5|run|...)
for Weekend / Midweek / Full week, the plain "/draw5 weekend" command, pagination, an empty result, and the fallback
when Telegram refuses the HTML anyway. Nothing is sent anywhere.
"""
import asyncio
import copy
import html
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import draw5  # noqa: E402
import handlers  # noqa: E402

FAILS: list[str] = []
TEST_CHAT = 990000601          # fake chat id, never the owner's
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "draw5_weekend_20261106.json")
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D]")


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    else:
        print("  PASS", msg)


class BadRequest(Exception):
    pass


ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler",
           "blockquote", "span"}


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.err = [], None

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.err = self.err or f"unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.err = self.err or f"unexpected end tag \"{tag}\""
        else:
            self.stack.pop()


def tg_validate(text, parse_mode=None, reply_markup=None):
    """Raise like Telegram does for an invalid message."""
    if parse_mode and str(parse_mode).upper().endswith("HTML"):
        p = _Tags()
        p.feed(text)
        p.close()
        if p.err:
            raise BadRequest(f"Can't parse entities: {p.err}")
        if p.stack:
            raise BadRequest(f"Can't parse entities: can't find end tag corresponding to start tag \"{p.stack[-1]}\"")
        visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    else:
        visible = text
    if not visible.strip():
        raise BadRequest("Message text is empty")
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise BadRequest("Message is too long")
    if reply_markup is not None:
        for row in reply_markup.inline_keyboard:
            for b in row:
                if b.callback_data and len(b.callback_data.encode()) > 64:
                    raise BadRequest("Button_data_invalid")


class FakeMsg:
    def __init__(self, log, chat_id=TEST_CHAT, reject_html=False):
        self.log, self.chat_id, self.reject_html = log, chat_id, reject_html
        self.text = None

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        tg_validate(text, parse_mode, reply_markup)
        m = FakeMsg(self.log, self.chat_id, self.reject_html)
        m.text = text
        self.log.append(("reply", text, parse_mode, reply_markup))
        return m

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        if self.reject_html and parse_mode:
            raise BadRequest("Can't parse entities: forced by test")
        tg_validate(text, parse_mode, reply_markup)
        self.text = text
        self.log.append(("edit", text, parse_mode, reply_markup))
        return self

    async def reply_document(self, doc, filename=None, caption=None, **kw):
        self.log.append(("doc", filename, caption, None))


class FakeQuery:
    def __init__(self, data, msg):
        self.data, self.message, self.answers = data, msg, []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        return await self.message.edit_text(text, **kw)


class FakeFinder:
    def __init__(self, results):
        self.results, self.calls = results, []

    async def find(self, period, text="", **kw):
        self.calls.append((period, text))
        return copy.deepcopy(self.results[period])


def make_ctx(finder, args=None):
    app = SimpleNamespace(bot_data={"services": SimpleNamespace(draw5=finder, ai=None)})
    return SimpleNamespace(application=app, args=args or [])


def run_callback(ctx, data, msg):
    q = FakeQuery(data, msg)
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=TEST_CHAT),
                          effective_chat=SimpleNamespace(id=TEST_CHAT), effective_message=msg)
    asyncio.run(handlers.on_callback(upd, ctx))
    return q


def results():
    week = json.load(open(FIX, encoding="utf-8"))
    weekend = copy.deepcopy(week)
    mid = copy.deepcopy(week)
    mid.update(period="midweek", picks=mid["picks"][:16], count=16, top_count=9, wider_count=7, suspended_count=16,
               note="Only 16 matches on SportyBet offer the 5-minute market in this window (all leagues checked), "
                    "short of 50.")
    for i, p in enumerate(mid["picks"]):
        p["wider"] = i >= 9
    week["period"] = "week"
    return {"weekend": weekend, "midweek": mid, "week": week}


def test_page_html_valid():
    print("page_html on the real 2026-11-06 weekend result")
    res = results()["weekend"]
    old_style = res["rules"] + "".join(p["why"] for p in res["picks"][:10])
    check(len(old_style) > 2000, "fixture carries the long reasons that broke the page")
    for pg in range(3):
        body, pages = draw5.page_html(res, pg, 10)
        try:
            tg_validate(body, "HTML")
            ok = True
        except BadRequest as exc:
            ok = str(exc)
        check(ok is True, f"page {pg + 1}: valid Telegram HTML within 4096 ({ok})")
    body, _ = draw5.page_html(res, 0, 10)
    check("suspended on all of them right now" in body and "no slip yet" in body,
          "all-suspended result says so plainly")
    huge = copy.deepcopy(res)
    for p in huge["picks"]:
        p["why"] = ("very long reason <with> & markup " * 40)
        p["match"] = p["match"] + " & Co <U23>"
    for pg in range(2):
        body, _ = draw5.page_html(huge, pg, 10)
        try:
            tg_validate(body, "HTML")
            ok = True
        except BadRequest as exc:
            ok = str(exc)
        check(ok is True, f"oversized reasons + markup characters still valid on page {pg + 1} ({ok})")
    check("..." in draw5.page_html(huge, 0, 10)[0], "oversized reasons shortened, not cut through a tag")
    empty = dict(res, picks=[], count=0, top_count=0, suspended_count=0, open_count=0, offered_in_window=0)
    body, pages = draw5.page_html(empty, 0, 10)
    tg_validate(body, "HTML")
    check(pages == 1 and "No matches" in body, "empty window -> clear reason")
    sl = {"rules_text": "x", "legs": [{"kickoff_local": "Sat 07 Nov 16:00", "match": "A <b> & C " * 30, "odds": 1.1,
                                       "est": 0.9, "wider": False}] * 20, "total_odds": 6.7, "est_prob": 0.12}
    tg_validate(draw5.slip_html(sl), "HTML")
    check(True, "slip preview with long names stays valid")


def test_callback_path():
    print("Telegram callback path (fake chat, offline)")
    res = results()
    for period, label in (("weekend", "Weekend"), ("midweek", "Midweek"), ("week", "Full week")):
        log = []
        finder = FakeFinder(res)
        ctx = make_ctx(finder)
        msg = FakeMsg(log)
        run_callback(ctx, f"D5|run|{period}|", msg)
        edits = [x for x in log if x[0] == "edit"]
        texts = " ".join(x[1] for x in log)
        check(finder.calls == [(period, "")], f"{label}: finder called once")
        check(edits and edits[-1][2] == "HTML" and "5-MIN DRAW FINDER" in edits[-1][1],
              f"{label}: list shown as HTML page 1")
        check("Something went wrong" not in texts and not EMOJI.search(texts), f"{label}: no generic error, no emoji")
        kb = edits[-1][3]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        check(f"[{label}]" in labels and "Full list (file)" in labels, f"{label}: buttons {labels}")
        check(not any(lb.startswith("Slip: top") for lb in labels), f"{label}: no slip buttons while all suspended")
        tok = next(k for k in ctx.application.bot_data["draw5"] if not k.startswith("_"))
        if res[period]["count"] > 10 and len(res[period]["picks"]) > 10:
            q = run_callback(ctx, f"D5|{tok}|p|1", msg)
            check("page 2/" in msg.text and q.answers == [None], f"{label}: Next page works")
        run_callback(ctx, f"D5|{tok}|f", msg)
        check(any(x[0] == "doc" for x in log), f"{label}: Full list file sent")

    print("/draw5 weekend command")
    log = []
    finder = FakeFinder(res)
    ctx = make_ctx(finder, ["weekend"])
    msg = FakeMsg(log)
    upd = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=TEST_CHAT))
    asyncio.run(handlers.draw5_cmd(upd, ctx))
    check(finder.calls == [("weekend", "weekend")] and log[-1][0] == "edit" and "page 1/" in log[-1][1],
          "/draw5 weekend -> page 1")
    log.clear()
    asyncio.run(handlers.draw5_cmd(SimpleNamespace(effective_message=msg), make_ctx(finder, [])))
    check(log and "Pick the window" in log[0][1], "/draw5 alone -> window picker")

    print("Telegram refuses the HTML anyway -> plain text, never the generic error")
    log = []
    ctx = make_ctx(FakeFinder(res))
    msg = FakeMsg(log, reject_html=True)
    run_callback(ctx, "D5|run|weekend|", msg)
    edits = [x for x in log if x[0] == "edit"]
    check(edits and edits[-1][2] is None and "5-MIN DRAW FINDER" in edits[-1][1] and "<b>" not in edits[-1][1],
          "plain-text fallback shown with the same list")

    print("finder error -> clear message")

    class Boom:
        async def find(self, *a, **k):
            raise RuntimeError("relay timeout")
    log = []
    run_callback(make_ctx(Boom()), "D5|run|weekend|", FakeMsg(log))
    check(log and "failed" in log[-1][1] and "Something went wrong" not in log[-1][1], "search failure explained")


if __name__ == "__main__":
    test_page_html_valid()
    test_callback_path()
    print(f"\nFAILURES: {len(FAILS)}")
    if FAILS:
        sys.exit(1)
    print("ALL PASSED")
