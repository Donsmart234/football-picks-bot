"""Branded slip images (offline: no network, no booking, no History writes, no Telegram sends).

    python tests/test_slipimg.py

Covers: rendering 1 / 6 / 20 / 25 legs (image grows, 20-leg cap with "+N more"), long names wrap / ellipsize, preview
("Preview - not booked"), statuses, every spec source (history record, share payload fixture, editflow context, scan
slip, analysis), the "send slip image" phrase detector, the 🖼 buttons on every slip keyboard, the Telegram callback
and reply flows (photo sent, nothing saved or booked), and the Mini App endpoints (/api/slipimg, /slipimg/<token>).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ.setdefault("FRA_WARMER", "0")
os.environ["CREST_CACHE_DIR"] = tempfile.mkdtemp()

from PIL import Image  # noqa: E402

import slipimg as SI  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def leg(i, home="Arsenal", away="Chelsea", **kw):
    d = {"event_id": f"sr:match:{1000 + i}", "market_id": "18", "specifier": "total=1.5", "outcome_id": "12",
         "home": home, "away": away, "home_uid": "", "away_uid": "", "market": "Over/Under", "pick": "Over 1.5",
         "odds": 1.2, "est": 0.84, "league": "England · Premier League", "kickoff": "2026-11-04T19:00:00+00:00"}
    d.update(kw)
    return d


def img(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


class NoNet(SI.CrestCache):
    async def get(self, uid):          # offline: every crest missing -> initials badge
        return None


def render_tests():
    print("render")
    hs = []
    for n in (1, 6, 20):
        sp = SI.make_spec([leg(i) for i in range(n)], "LPDNPJ")
        png = SI.render(sp)
        im = img(png)
        check(im.format == "PNG" and im.width == SI.W, f"{n} legs: PNG {im.size}")
        hs.append(im.height)
    check(hs[0] < hs[1] < hs[2], f"height grows with legs {hs}")
    sp = SI.make_spec([leg(i) for i in range(25)], "LPDNPJ")
    check(len(sp["legs"]) == 20 and sp["more"] == 5, "25 legs -> 20 drawn + '5 more'")
    check(abs(sp["total_odds"] - 1.2 ** 20) < 0.05, "total odds from the drawn legs when none given")
    h20 = img(SI.render(sp)).height
    check(h20 > hs[2], "'+ N more' line adds height")
    long = leg(0, home="Borussia Mönchengladbach Under-23 Reserves Team Extra", away="Wolverhampton Wanderers Football Club",
               pick="Celtic (-0.5) & Over 1.5 goals in the match combined special pick text that goes on",
               market="Handicap & Over/Under combo special market with a really long name " * 2,
               league="International Clubs · UEFA Europa Conference League Qualification Playoffs Round")
    png = SI.render(SI.make_spec([long], "ABC123"))
    check(img(png).width == SI.W, "long names render without error")
    f = SI.font("b", 38)
    lines = SI.wrap(f, long["pick"], 600, 2)
    check(len(lines) == 2 and all(SI.tw(f, x) <= 600 for x in lines) and lines[-1].endswith("…"),
          f"pick wraps to 2 lines, ellipsized: {lines}")
    check(SI.tw(f, SI.ellipsize(f, "x" * 400, 300)) <= 300, "ellipsize fits the width")
    check(len(SI.wrap(f, "Supercalifragilisticexpialidocious" * 3, 200, 2)) == 2, "over-long word hard-splits")
    pv = SI.make_spec([leg(0)], "")
    check(pv["preview"] and SI.render(pv), "no code -> preview spec renders")
    check(SI.make_spec([], "X")["legs"] == [] and SI.render(SI.make_spec([], "")), "0 legs renders an empty slip")


def spec_tests():
    print("specs")
    rec = {"id": "rd_1", "kind": "slip", "status": "pending", "created_at": "2026-11-03T07:18:00+00:00",
           "data": {"code": "N6RXGP", "label": "Rollover 2x", "rollover_id": "ro_1", "rollover_day": 2, "target": 2.0,
                    "total_odds": 1.49, "est_prob": 0.7,
                    "legs": [leg(0, status="won", score="2-1", early={"min": 38}),
                             leg(1, status="live", score="0-0")]}}
    sp = SI.spec_from_record(rec)
    check(sp["code"] == "N6RXGP" and not sp["preview"] and sp["label"].startswith("Rollover day 2"), "record: code + label")
    check(sp["status"] == "live" and sp["legs"][0]["status"] == "won" and sp["legs"][0]["early"] == 38,
          f"record: slip live, leg won early ({sp['status']})")
    check(sp["when"].isoformat().startswith("2026-11-03T07:18"), "record: time = created_at")
    rec2 = json.loads(json.dumps(rec))
    rec2["status"] = "lost"
    check(SI.spec_from_record(rec2)["status"] == "lost", "record status lost -> slip lost")
    data = json.loads((ROOT / "tests/fixtures/share_N6RXGP.json").read_text())["data"]
    sp = SI.spec_from_share("N6RXGP", data)
    check(sp["code"] == "N6RXGP" and len(sp["legs"]) == len(data["outcomes"]) + len(data.get("unavailableOutcomes") or []),
          f"share payload: {len(sp['legs'])} legs")
    check(all(l["status"] in ("won", "lost", "void") for l in sp["legs"]) and sp["status"] in ("won", "lost"),
          f"share payload: settled legs -> {sp['status']}")
    check(SI.render(sp)[:4] == b"\x89PNG", "share payload renders")
    ctx = {"section": "scan", "sid": "s1", "legs": [leg(0), leg(1)], "slip": {"label": "Safe", "target": 2,
                                                                         "total_odds": 1.44, "est_prob": 0.7}}
    sp = SI.spec_from_ctx(ctx)
    check(sp["preview"] and sp["total_odds"] == 1.44 and "Safe" in sp["label"], f"editflow scan ctx -> preview ({sp['label']})")
    sp = SI.spec_from_ctx({"section": "rollover_preview", "code": "OLD123", "legs": [leg(0)]})
    check(sp["preview"] and sp["code"] == "", "rollover preview: never shows the old code as booked")
    sp = SI.spec_from_slip({"label": "Mid-safe", "target": 5, "legs": [leg(0)], "total_odds": 1.2, "est_prob": 0.84})
    check(sp["label"] == "Mid-safe slip · target 5" and sp["est_prob"] == 0.84, sp["label"])
    an = {"code": "N6RXGP", "summary": {"grade": "B"}, "legs": [dict(leg(0), state="upcoming", rated=True),
                                                              dict(leg(1), state="live", minute="63:10", score="1:0"),
                                                              dict(leg(2), state="upcoming", rated=False)]}
    sp = SI.spec_from_analysis(an)
    check(sp["legs"][1]["status"] == "live" and sp["legs"][1]["minute"] == "63'" and sp["legs"][1]["score"] == "1-0",
          "analysis: live leg minute + score")
    check(sp["legs"][2]["est"] is None and sp["est_prob"] is None and sp["unrated"] == 1, "analysis: unrated leg -> no combined")
    check(SI._league({"league": "International Clubs · UEFA Champions League"}) == "UEFA Champions League", "league tag drops 'International Clubs'")
    check(SI._league({"league": "Premier League", "category": "England"}) == "England · Premier League", "league tag adds the country")
    check(SI._initials("FC Bayern München") == "BM" and SI._initials("Inter") == "IN", "initials fallback")
    check(SI.CrestCache._decode(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
                                b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;") is None,
          "Sportradar's 1x1 'missing crest' gif -> None (initials)")


def phrase_tests():
    print("phrases")
    yes = ["send slip image", "Send me the slip image", "generate image of this slip", "make a picture of this ticket",
           "image of N6RXGP", "share this as an image", "can I get a pic of the code", "slip pic", "png of this slip pls",
           "give me this slip as photo", "screenshot this slip"]
    no = ["what's in this image?", "analyze this screenshot", "who wins Arsenal vs Chelsea", "safe 5 odds",
          "change the first selection", "read the photo"]
    for t in yes:
        check(SI.wants_image(t), f"image request: {t!r}")
    for t in no:
        check(not SI.wants_image(t), f"not an image request: {t!r}")


def keyboard_tests():
    print("buttons")
    import handlers as H
    from tracker import day_markup, img_markup
    datas = lambda kb: [b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data]
    check("IMG|c|GK69V5" in datas(H.open_code_kb(None, "GK69V5")), "booked code message: IMG|c|CODE")
    check(datas(H.open_code_kb(None, None)) == [], "no code -> no image button")
    check("IMG|s|abc" in datas(H._scan_kb("abc", {"legs": [leg(0)]})), "scan result: IMG|s|sid")
    check("IMG|h|rd_1" in datas(day_markup("rd_1", True, "X1Y2Z3")), "rollover day: IMG|h|slip_id")
    check(datas(img_markup("sl_9")) == ["IMG|h|sl_9"], "result ping: IMG|h|record")
    check("IMG|a|a1" in datas(H._analysis_kb({"id": "a1", "code": "N6RXGP", "suggestions": {}})), "analyzer: IMG|a|aid")
    _, kb = H._edit_html({"section": "custom", "legs": [leg(0)], "total_odds": 1.2, "est_prob": 0.8, "sid": "e1"})
    check("IMG|s|e1" in datas(kb), "edited slip: IMG|s|sid")
    check(len(H.img_btn("h", "x" * 80).callback_data) <= 64, "callback data within Telegram's 64 bytes")
    check(H._code_in("image of n6rxgp please") == "N6RXGP" and H._code_in("send TICKET image") is None
          and H._code_in("pic of https://www.sportybet.com/?shareCode=AB12CD&c=ng") == "AB12CD", "code in text")


# ------------------------------------------------------------------ Telegram flows (fakes)
class FakeHistory:
    def __init__(self, recs):
        self.recs = {r["id"]: r for r in recs}
        self.saves = 0
        self.remote = False

    async def get(self, rid):
        return self.recs.get(rid)

    async def find_code(self, code):
        return next((r for r in self.recs.values() if r["data"].get("code") == code), None)

    async def save(self, rec):
        self.saves += 1
        return True


class FakeChat:
    id = 1

    def __init__(self):
        self.photos, self.docs = [], []

    async def send_action(self, *_a, **_k):
        pass

    async def send_photo(self, photo, **kw):
        self.photos.append((photo, kw))
        return SimpleNamespace(message_id=7000 + len(self.photos))

    async def send_document(self, document, **kw):
        self.docs.append((document, kw))


class Msg:
    def __init__(self, chat, text="", message_id=1, reply_to=None, reply_markup=None, from_bot=False):
        self.chat, self.text, self.message_id, self.reply_to_message = chat, text, message_id, reply_to
        self.reply_markup, self.chat_id, self.caption = reply_markup, 1, None
        self.from_user = SimpleNamespace(is_bot=from_bot, id=1)
        self.photo, self.document = None, None
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=8000, chat_id=1)


class Q:
    def __init__(self, data, message):
        self.data, self.message = data, message
        self.from_user = SimpleNamespace(id=1)
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def world():
    from storage import Storage
    from editflow import SlipEditor
    st = Storage(os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
    rec = {"id": "rd_261103_aaaaaa", "kind": "slip", "status": "pending", "created_at": "2026-11-03T07:18:00+00:00",
           "data": {"code": "N6RXGP", "label": "Rollover 2x", "rollover_id": "ro_1", "rollover_day": 2,
                    "total_odds": 1.44, "legs": [leg(0), leg(1, "Inter", "Como")]}}
    hist = FakeHistory([rec])

    class BM:
        booked = 0

        async def book(self, *_a):
            BM.booked += 1
            raise AssertionError("no booking from images")

        async def share(self, code):
            data = json.loads((ROOT / "tests/fixtures/share_N6RXGP.json").read_text())["data"]
            return {"ok": True, "data": data} if code == "P45SST" else {"ok": False, "kind": "invalid"}
    s = SimpleNamespace(storage=st, bookmaker=BM(), tracker=SimpleNamespace(history=hist, preview=lambda t: None,
                                                                           enrich=None),
                        ai=SimpleNamespace(slips={"s1": {"ts": time.time(), "slip": {"label": "Safe", "target": 2,
                                                                                    "legs": [leg(3)], "total_odds": 1.2,
                                                                                    "est_prob": 0.84}}}),
                        analyzer=SimpleNamespace(get=lambda aid: None))
    s.slipimg = SI.SlipImages(s, crests=NoNet())
    s.editor = SlipEditor(s)
    return s, hist, rec


async def telegram_tests():
    print("telegram")
    import handlers as H
    s, hist, rec = world()
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"services": s}))
    chat = FakeChat()
    slipmsg = Msg(chat, "🔁 Rollover day 2/5", message_id=50, from_bot=True)
    q = Q(f"IMG|h|{rec['id']}", slipmsg)
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    check(len(chat.photos) == 1 and chat.photos[0][0][:4] == b"\x89PNG", "🖼 tap: one PNG photo sent")
    kw = chat.photos[0][1]
    check(kw["reply_to_message_id"] == 50 and "N6RXGP" in kw["caption"] and kw["filename"].endswith(".png"),
          f"photo replies to the slip, caption names the code: {kw['caption']!r}")
    check(hist.saves == 0 and s.bookmaker.booked == 0, "nothing saved to History, nothing booked")
    # scan slip
    q = Q("IMG|s|s1", slipmsg)
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    check(len(chat.photos) == 2 and "preview" in chat.photos[1][1]["caption"], "scan slip -> preview image")
    # expired scan slip, no stored ref -> polite message, no photo
    q = Q("IMG|s|gone", Msg(chat, message_id=51, from_bot=True))
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    check(len(chat.photos) == 2 and q.message.replies and "can't find" in q.message.replies[0], "expired slip: honest note")
    # expired scan slip but the message has a stored ref -> drawn from the ref
    s.editor.remember(1, 52, {"section": "scan", "sid": "gone", "legs": [leg(5, "Lazio", "Roma")]})
    q = Q("IMG|s|gone", Msg(chat, message_id=52, from_bot=True))
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    check(len(chat.photos) == 3, "expired in-memory slip -> drawn from the message's stored slip ref")
    # tall slip -> full-size file button, then the file
    big = {"id": "sl_big", "kind": "slip", "status": "pending", "created_at": "2026-11-03T07:00:00+00:00",
           "data": {"code": "BIG123", "legs": [leg(i) for i in range(15)]}}
    hist.recs[big["id"]] = big
    q = Q("IMG|h|sl_big", slipmsg)
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    kb = chat.photos[-1][1]["reply_markup"]
    fdata = kb.inline_keyboard[0][0].callback_data if kb else ""
    check(fdata.startswith("IMG|f|"), "15 legs (taller than Telegram's 2560 px): full-size file button")
    q = Q(fdata, Msg(chat, message_id=60, from_bot=True))
    await H._slip_image_cb(q, ctx, q.data.split("|"))
    check(len(chat.docs) == 1 and chat.docs[0][0][:4] == b"\x89PNG", "full-size file sent as a document")
    # free text: reply "send slip image" to a slip message that carries the button
    from telegram import InlineKeyboardMarkup as Kb
    bot_msg = Msg(chat, "🔁 Rollover day 2/5", message_id=70, from_bot=True,
                  reply_markup=Kb([[H.img_btn("h", rec["id"])]]))
    user = Msg(chat, "send slip image", message_id=71, reply_to=bot_msg)
    upd = SimpleNamespace(effective_message=user, effective_chat=chat, effective_user=SimpleNamespace(id=1))
    n = len(chat.photos)
    await H.free_text(upd, ctx)
    check(len(chat.photos) == n + 1 and chat.photos[-1][1]["reply_to_message_id"] == 70, "reply 'send slip image' -> photo")
    # reply to a slip message without buttons, resolved from the stored ref
    s.editor.remember(1, 80, {"section": "history", "slip_id": rec["id"], "legs": rec["data"]["legs"]})
    user = Msg(chat, "generate image of this slip", message_id=81, reply_to=Msg(chat, "x", message_id=80, from_bot=True))
    upd.effective_message = user
    await H.free_text(upd, ctx)
    check(len(chat.photos) == n + 2, "reply 'generate image of this slip' (stored ref) -> photo")
    # a code in the text (not in History) -> SportyBet share payload, read only
    user = Msg(chat, "image of P45SST", message_id=90)
    upd.effective_message = user
    await H.free_text(upd, ctx)
    check(len(chat.photos) == n + 3 and "P45SST" in chat.photos[-1][1]["caption"], "'image of CODE' -> photo of that code")
    user = Msg(chat, "image of ZZZ999", message_id=91)
    upd.effective_message = user
    await H.free_text(upd, ctx)
    check(len(chat.photos) == n + 3 and user.replies and "couldn't load" in user.replies[0], "invalid code -> honest note")
    # no reply, recent slip shown in the chat -> that slip
    user = Msg(chat, "send slip image", message_id=92)
    upd.effective_message = user
    await H.free_text(upd, ctx)
    check(len(chat.photos) == n + 4, "'send slip image' without reply -> last slip shown in the chat")
    check(hist.saves == 0 and s.bookmaker.booked == 0, "still nothing saved, nothing booked")


async def webapp_tests():
    print("mini app")
    import httpx
    from aiohttp import web
    from webapp import MiniApp
    s, hist, rec = world()
    s.tracker.bot = None
    os.environ["ADMIN_TOKEN"] = "t" * 32
    app = web.Application()
    MiniApp(s, bot_token="123:fake", owner_id=1).register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8097).start()
    adm = {"X-Admin-Token": "t" * 32}
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8097", timeout=30, trust_env=False) as c:
            check((await c.post("/api/slipimg", json={"history_id": rec["id"]})).status_code == 403, "/api/slipimg 403 without auth")
            r = await c.post("/api/slipimg", json={"history_id": rec["id"]}, headers=adm)
            j = r.json()
            check(r.status_code == 200 and j["url"].startswith("/slipimg/") and j["width"] == SI.W and j["legs"] == 2,
                  f"history slip -> image url {j.get('url')}")
            r2 = await c.get(j["url"])
            check(r2.status_code == 200 and r2.headers["content-type"] == "image/png" and r2.content[:4] == b"\x89PNG",
                  "public /slipimg/<token>.png serves the PNG (no initData needed)")
            r3 = await c.get(j["jpg"])
            check(r3.status_code == 200 and r3.content[:2] == b"\xff\xd8", "…and a JPEG copy (prepared messages)")
            check((await c.get("/slipimg/" + "x" * 24 + ".png")).status_code == 404, "unknown token 404")
            r = await c.post("/api/slipimg", json={"slip": {"label": "My slip", "legs": [leg(0), {"pick": "<b>x</b>", "odds": "bad"}]}},
                             headers=adm)
            check(r.status_code == 200 and r.json()["legs"] == 2, "app slip (client legs, sanitised) -> preview image")
            r = await c.post("/api/slipimg", json={"history_id": rec["id"], "format": "png"}, headers=adm)
            check(r.status_code == 200 and r.content[:4] == b"\x89PNG", "format=png answers the PNG itself")
            check((await c.post("/api/slipimg", json={"slip_id": "nope"}, headers=adm)).status_code == 404, "expired slip 404")
            r = await c.post("/api/slipimg/send", json={"token": j["token"]}, headers=adm)
            check(r.status_code == 400, "admin token never sends to a chat")
            r = await c.post("/api/slipimg/prepare", json={"token": j["token"]}, headers=adm)
            check(r.status_code == 400, "admin token never prepares a share message")
    finally:
        await runner.cleanup()
    check(hist.saves == 0, "Mini App images: nothing saved")


def main():
    render_tests()
    spec_tests()
    phrase_tests()
    keyboard_tests()
    asyncio.run(telegram_tests())
    asyncio.run(webapp_tests())
    print("\nFAILURES:", len(FAILS))
    if FAILS:
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
