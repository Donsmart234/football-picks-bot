"""Regression: /draw5 Weekend answered "Something went wrong on my side" (prod, 2026-11-06 05:34 WAT).

    python tests/test_draw5_telegram.py

Cause: draw5.page_html cut a long page at 4000 raw characters, through an open <i> tag, so Telegram refused the edit
("Can't parse entities: can't find end tag corresponding to start tag "i""). The fixture is a 20-pick slice of the
real weekend result from that morning (every 5-minute market suspended, long reasons).

2026-11-06 later: /draw5 now replies with one booked code (draw5book.py); test_code_flow covers it (code-only reply,
exactly 50 legs, no German league, Refresh excludes earlier events and survives a restart, all-suspended message).

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
sys.path.insert(0, os.path.dirname(__file__))
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


def make_ctx(svc, args=None):
    app = SimpleNamespace(bot_data={"services": svc})
    return SimpleNamespace(application=app, args=args or [])


def run_callback(ctx, data, msg):
    q = FakeQuery(data, msg)
    q.from_user = SimpleNamespace(id=TEST_CHAT)
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


# ---------------------------------------------------------------- booked-code flow (2026-11-06 rule)
from test_draw5 import FakeStats, ev  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
import draw5book  # noqa: E402
from history import HistoryStore  # noqa: E402
from storage import Storage  # noqa: E402

OWNER_CHAT = 8590854295
GERMAN = ("sr:tournament:35", "sr:category:30", "Bundesliga", "Germany")


class BookBM:
    """Fake SportyBet: lists events, books codes (refuses suspended outcomes like SportyBet does)."""
    def __init__(self, events):
        self.events, self.booked, self.last = events, [], None

    async def list_events(self, hours=24, market_ids=None):
        return self.events

    async def book(self, sels):
        from bookmakers import BookingError
        by = {e.event_id: e for e in self.events}
        for s_ in sels:
            o = by[s_.event_id].markets[(s_.market_id, s_.specifier)][s_.outcome_id]
            if not o.active:
                raise BookingError("SportyBet refused the booking: selection suspended (HTTP 200)")
        if len(sels) > 50:
            raise BookingError("SportyBet refused the booking: too many selections")
        self.booked.append([s_.event_id for s_ in sels])
        return f"D5T{len(self.booked):03d}"


class FakeTracker:
    def __init__(self, history):
        self.history, self.records = history, []

    async def record(self, legs, code, source, **kw):
        self.records.append({"legs": legs, "code": code, "source": source, **kw})
        return {"id": f"sl_test_{len(self.records)}"}


def make_svc(events, store_path=None):
    st = Storage(store_path or os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
    hist = HistoryStore(storage=st, url="", key="")
    bm = BookBM(events)
    svc = SimpleNamespace(draw5=draw5.Draw5Finder(bm, FakeStats()), ai=None, bookmaker=bm,
                          tracker=FakeTracker(hist))
    return svc, bm


def week_events(n_top=60, n_wider=20, n_german=10, active=True):
    start = datetime.now(timezone.utc) + timedelta(days=1)
    evs = [ev(i, start + timedelta(minutes=i), active=active) for i in range(n_top)]
    evs += [ev(500 + i, start + timedelta(minutes=i), top=False, active=active) for i in range(n_wider)]
    # German matches with the BEST prices, so they would rank first if not excluded
    evs += [ev(900 + i, start + timedelta(minutes=i), league=GERMAN, d5=(12.0, 1.04, 15.0), active=active)
            for i in range(n_german)]
    return evs


def chats_ok(log):
    return all(getattr(m, "chat_id", TEST_CHAT) != OWNER_CHAT for m in log)


def test_code_flow():
    print("Telegram: Full week -> one booked code (fake chat, offline)")
    svc, bm = make_svc(week_events())
    ctx = make_ctx(svc)
    log = []
    msg = FakeMsg(log)
    run_callback(ctx, "D5|run|week|", msg)
    edits = [x for x in log if x[0] == "edit"]
    body = edits[-1][1] if edits else ""
    check(len(bm.booked) == 1 and len(bm.booked[0]) == 50, f"one code of exactly 50 legs ({[len(b) for b in bm.booked]})")
    check(not any(e.startswith("sr:match:19") for e in bm.booked[0]), "no German league in the code")
    check(edits and edits[-1][2] == "HTML" and "<code>D5T001</code>" in body and body.startswith("<b>Rules:</b>"),
          "reply opens with Rules: and shows the code")
    check("50 selections" in body and "no German leagues" in body and "total odds" in body
          and "https://www.sportybet.com/?shareCode=D5T001&amp;c=ng" in body, "rules, total odds, open link")
    check("5-MIN DRAW FINDER" not in body and body.count("\n") <= 8 and "Prev" not in body,
          "code only: no per-pick pages")
    check(tg_len_ok(body), "fits Telegram")
    kb = edits[-1][3]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("Refresh (new 50)" in labels and "Slip image" in labels and "Full list (file)" in labels and
          "Open D5T001 in SportyBet" in labels, f"buttons {labels}")
    rec = svc.tracker.records[-1]
    check(rec["code"] == "D5T001" and rec["label"] == "5-min draw Full week" and rec["source"] == "draw5"
          and len(rec["legs"]) == 50, "saved to History as a booked slip '5-min draw Full week'")
    texts = " ".join(str(x[1]) for x in log)
    check("Something went wrong" not in texts and not EMOJI.search(texts), "no generic error, no emoji")

    tok = next(k for k in ctx.application.bot_data["draw5"] if not k.startswith("_"))
    run_callback(ctx, f"D5|{tok}|f", msg)
    check(any(x[0] == "doc" and "D5T001" in x[1] for x in log), "Full list file sent")

    print("Refresh -> 50 matches not in the earlier code")
    first = set(bm.booked[0])
    run_callback(ctx, f"D5|r|week|{tok}", msg)
    second = bm.booked[-1] if len(bm.booked) == 2 else []
    check(len(bm.booked) == 2 and not (set(second) & first), "refresh code shares no match with the first")
    check(len(second) == 30 and "Only 30" in log[-1][1], f"only 30 unused open picks left -> 30, said plainly ({len(second)})")
    check(not any(e.startswith("sr:match:19") for e in second), "refresh: still no German league")
    run_callback(ctx, f"D5|r|week|{tok}", msg)
    check(len(bm.booked) == 2 and "No new matches left" in log[-1][1], "third refresh: unused picks ran out, said so")

    print("used ids survive a restart (history store), reset on a new window")
    path = os.path.join(tempfile.mkdtemp(), "p.sqlite3")
    svc1, bm1 = make_svc(week_events(), path)
    asyncio.run(draw5book.book_draw5(svc1, TEST_CHAT, "week"))
    svc2, bm2 = make_svc(week_events(), path)          # new process, same store
    out = asyncio.run(draw5book.book_draw5(svc2, TEST_CHAT, "week", refresh=True))
    check(out["status"] == "booked" and not (set(bm2.booked[0]) & set(bm1.booked[0])), "refresh after restart still new")
    us = draw5book.UsedStore(svc2.tracker.history)
    win = draw5.period_window("week")
    key = draw5book.window_key("week", win)
    check(len(asyncio.run(us.load(TEST_CHAT, "week", key))) == 80, "80 used ids persisted")
    check(asyncio.run(us.load(TEST_CHAT, "week", "week:2099-01-05")) == {}, "new window -> fresh start")

    print("all suspended -> plain message + Try again, nothing booked")
    svc3, bm3 = make_svc(week_events(active=False))
    ctx3 = make_ctx(svc3)
    log3 = []
    run_callback(ctx3, "D5|run|week|", FakeMsg(log3))
    last = log3[-1]
    labels = [b.text for row in last[3].inline_keyboard for b in row]
    check(bm3.booked == [] and "closed right now" in last[1] and "Try again" in labels and "error" not in last[1].lower(),
          f"closed-market reply: {last[1][:120]}")
    check("Something went wrong" not in last[1], "no generic error")

    print("market watcher records open/closed transitions (no messages)")
    r = asyncio.run(draw5book.check_market(svc3))
    check(r["state"] == "closed" and r["open"] == 0 and r["offered"] == 80, f"closed state read ({r})")
    svc3.bookmaker.events = week_events()
    r = asyncio.run(draw5book.check_market(svc3))
    meta = asyncio.run(draw5book._meta(svc3.tracker.history))
    check(r["state"] == "open" and [t["state"] for t in meta["transitions"]] == ["closed", "open"]
          and meta.get("last_open"), "closed -> open transition and last_open recorded")
    svc3.bookmaker.events = week_events(active=False)
    svc3.draw5 = draw5.Draw5Finder(svc3.bookmaker, FakeStats())
    out = asyncio.run(draw5book.book_draw5(svc3, TEST_CHAT, "week"))
    check(out["status"] == "closed" and "Last seen open by me" in out["message"], "closed reply names when last seen open")

    print("/draw5 weekend command and chat '5 min draw'")
    svc4, bm4 = make_svc(week_events())
    log4 = []
    m4 = FakeMsg(log4)
    asyncio.run(handlers.draw5_cmd(SimpleNamespace(effective_message=m4, effective_user=SimpleNamespace(id=TEST_CHAT)),
                                   make_ctx(svc4, ["week"])))
    check(len(bm4.booked) == 1 and "<code>D5T001</code>" in log4[-1][1], "/draw5 week -> code")
    log4.clear()
    asyncio.run(handlers.draw5_cmd(SimpleNamespace(effective_message=m4), make_ctx(svc4, [])))
    check(log4 and "Pick the window" in log4[0][1], "/draw5 alone -> window picker")

    print("search failure -> clear message")

    class Boom:
        async def find(self, *a, **k):
            raise RuntimeError("relay timeout")
    svc5, _ = make_svc([])
    svc5.draw5 = Boom()
    log5 = []
    run_callback(make_ctx(svc5), "D5|run|weekend|", FakeMsg(log5))
    check(log5 and "Try again" in log5[-1][1] and "Something went wrong" not in log5[-1][1], "search failure explained")
    check(all(chats_ok(l) for l in (log, log3, log4, log5)), "nothing sent to the owner chat")


def tg_len_ok(body):
    try:
        tg_validate(body, "HTML")
        return True
    except BadRequest:
        return False


if __name__ == "__main__":
    test_page_html_valid()
    test_code_flow()
    print(f"\nFAILURES: {len(FAILS)}")
    if FAILS:
        sys.exit(1)
    print("ALL PASSED")
