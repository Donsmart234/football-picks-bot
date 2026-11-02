"""Image understanding (vision.py): reading JSON parsing, image prep, leg -> SportyBet market mapping, routing with a
mocked Gemini (booking code -> Bet Analyzer, legs -> mapped + "couldn't match", unreadable, model failure, non-betting
image, one of our own slips -> reply-style edit, fixtures -> tool loop without booking tools), the Telegram handler
(photo, reply to a photo) and the Mini App endpoint. Offline: fake bookmaker / Gemini / SQLite; nothing is booked,
nothing is written to History, nothing is sent.

    python tests/test_vision.py
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("FRA_WARMER", "0")

import vision  # noqa: E402
from bookmakers import Event  # noqa: E402
from matching import rank_events  # noqa: E402
from scanner import Scanner  # noqa: E402
from storage import Storage  # noqa: E402

NOW = datetime.now(timezone.utc)
UPLOADS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "files", "uploads")


# ------------------------------------------------------------------ fixtures
def mk(mid, spec, outs, desc):
    return {"id": mid, "specifier": spec, "desc": desc, "group": "", "source": "",
            "outcomes": [{"id": oid, "desc": d, "odds": o, "active": True, "prob": None} for oid, d, o in outs]}


def ev(eid, home, away, hours=6, tid="sr:tournament:17", cid="sr:category:1"):
    return Event(event_id=f"sr:match:{eid}", home=home, away=away, start=NOW + timedelta(hours=hours),
                 league="Premier League", category="England", tournament_id=tid, home_id=f"sr:competitor:{eid}1",
                 away_id=f"sr:competitor:{eid}2", category_id=cid)


EVENTS = {
    "sr:match:1": (ev(1, "Arsenal", "Chelsea"), [
        mk("1", "", [("1", "Home", 1.90), ("2", "Draw", 3.60), ("3", "Away", 4.20)], "1X2"),
        mk("10", "", [("9", "Home or Draw", 1.22), ("10", "Home or Away", 1.30), ("11", "Draw or Away", 1.95)],
           "Double Chance"),
        mk("18", "total=2.5", [("12", "Over 2.5", 1.80), ("13", "Under 2.5", 2.02)], "Over/Under"),
        mk("29", "", [("74", "Yes", 1.70), ("76", "No", 2.10)], "GG/NG"),
        mk("166", "total=9.5", [("12", "Over 9.5", 1.85), ("13", "Under 9.5", 1.90)], "Corners - Over/Under"),
    ]),
    "sr:match:2": (ev(2, "Genclerbirligi SK", "Alanyaspor", tid="sr:tournament:52", cid="sr:category:46"), [
        mk("1", "", [("1", "Home", 3.10), ("2", "Draw", 3.30), ("3", "Away", 2.30)], "1X2"),
        mk("16", "hcp=-1.5", [("1714", "Home (-1.5)", 6.50), ("1715", "Away (+1.5)", 1.16)], "Asian Handicap"),
    ]),
    "sr:match:3": (ev(3, "Lorient", "Brest", tid="sr:tournament:34", cid="sr:category:7"), [
        mk("1", "", [("1", "Home", 2.40), ("2", "Draw", 3.30), ("3", "Away", 3.00)], "1X2"),
        mk("14", "hcp=2:0", [("1711", "Home (2:0)", 1.12), ("1712", "Draw (2:0)", 9.0), ("1713", "Away (2:0)", 17.0)],
           "Handicap"),
    ]),
}


class FakeBM:
    load_code_url = "https://example/load"

    def __init__(self):
        self.booked, self.shared = [], []

    async def book(self, sels):
        self.booked.append(sels)
        raise AssertionError("nothing may be booked from an image")

    async def event_detail(self, eid):
        e = EVENTS.get(eid)
        return (e[0], e[1], {}) if e else (None, None, {})

    async def share(self, code):
        self.shared.append(code)
        return {"ok": False, "kind": "invalid", "error": "The code is invalid."}

    async def list_events(self, hours):
        return [e for e, _ in EVENTS.values()]


class FakeScanner(Scanner):
    def __init__(self):
        super().__init__(None, None)

    async def match_form(self, e):
        return None


class FakeAnalyzer:
    """Records analyze() calls for the code route; delegates analyze_legs to the real analyzer."""

    def __init__(self, real):
        self.real, self.codes = real, []

    async def analyze(self, code, rules=""):
        self.codes.append(code)
        if code == "N6RXGP":
            legs = [{"idx": 0, "match": "A vs B", "state": "upcoming", "result": None, "rating": "Strong", "est": 0.86,
                     "odds": 1.15, "event_id": "sr:match:1", "rated": True}]
            res = self.real.build("N6RXGP", legs, {}, None)
            self.real._remember(res)
            return res
        return {"ok": False, "kind": "invalid", "error": "The code is invalid."}

    async def analyze_legs(self, *a, **k):
        return await self.real.analyze_legs(*a, **k)


class FakeSR:
    async def match_news(self, *a):
        return None


def world():
    import tracker as T
    from ai import Analyst
    from analyzer import Analyzer
    from editflow import SlipEditor
    st = Storage(os.path.join(tempfile.mkdtemp(), "v.sqlite3"))
    s = SimpleNamespace(storage=st, bookmaker=FakeBM(), scanner=FakeScanner(), sr=FakeSR())

    async def search(q, limit=5):
        return rank_events(q, [e for e, _ in EVENTS.values()], limit)

    async def find_event(eid):
        e = EVENTS.get(eid)
        return e[0] if e else None

    async def predict(e):
        return None
    s.search, s.find_event, s.predict = search, find_event, predict
    s.tracker = T.Tracker(s)
    s.analyzer = FakeAnalyzer(Analyzer(s))
    s.ai = Analyst(s, api_key="fake")
    s.editor = SlipEditor(s)
    return s


class FakeGemini:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def __call__(self, contents, system=None, tools=True, max_tokens=8192, **kw):
        self.calls.append({"tools": tools, "system": system or "", "kw": kw,
                           "text": " ".join(p.get("text", "") for c in contents for p in c.get("parts", [])),
                           "has_image": any("inlineData" in p for c in contents for p in c.get("parts", []))})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _txt(t, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"role": "model", "parts": [{"text": t}] if t else []}}]}


def read(**kw):
    d = {"readable": True, "kind": "other", "bookmaker": "", "booking_code": "", "legs": [], "matches": [],
         "header": "", "summary": "", "answer": ""}
    d.update(kw)
    return _txt(json.dumps(d))


def LEG(home, away, kind, sel, line="", market="", pick="", odds=None, period="ft", team=""):
    return {"home": home, "away": away, "kind": kind, "selection": sel, "line": line, "market": market,
            "pick": pick, "odds": odds, "period": period, "team": team}


def jpeg(w=40, h=30):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (20, 120, 60)).save(b, "JPEG")
    return b.getvalue()


# ------------------------------------------------------------------ unit
def unit_parse_and_prep():
    r = vision.parse_read('```json\n{"readable": true, "kind": "betslip", "booking_code": " n6rx gp ", "legs": '
                          '[{"home": "Arsenal", "away": "Chelsea", "odds": "1,90", "kind": "1x2", "selection": "home"},'
                          ' {"home": "", "away": ""}], "matches": []}\n```')
    assert r["kind"] == "betslip" and r["booking_code"] == "N6RXGP" and len(r["legs"]) == 1 and r["legs"][0]["odds"] == 1.9
    assert vision.parse_read('{"kind": "weird", "booking_code": "ab"}')["kind"] == "other"
    assert vision.parse_read('{"kind": "betslip", "booking_code": "AB-12"}')["booking_code"] == ""
    assert vision.parse_read("I can't help with that") is None
    print("parse: JSON fences, code normalized/validated, empty legs dropped, non-JSON -> None OK")

    data, mime = vision.prepare_image(jpeg())
    assert mime == "image/jpeg" and data[:3] == b"\xff\xd8\xff"
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (4200, 3000), (200, 10, 10)).save(b, "PNG")
    data, mime = vision.prepare_image(b.getvalue(), "image/png")
    im = Image.open(io.BytesIO(data))
    assert mime == "image/jpeg" and max(im.size) == vision.MAX_SIDE and len(data) <= vision.MAX_BYTES
    for bad in (b"", b"%PDF-1.4 not an image"):
        try:
            vision.prepare_image(bad, "application/pdf")
            raise AssertionError("must refuse")
        except vision.ImageError:
            pass
    for f in ("IMG_1777-cff258a48cfa.jpeg", "IMG_1773-218fb61dccae.jpeg"):
        p = os.path.join(UPLOADS, f)
        if os.path.exists(p):
            d, m = vision.prepare_image(open(p, "rb").read())
            assert m == "image/jpeg" and len(d) <= vision.MAX_BYTES
    print("prepare: jpeg passthrough, 4200x3000 PNG -> 2048 px JPEG <= 5 MB, non-image refused, uploads OK")


def unit_mapping():
    e1, m1 = EVENTS["sr:match:1"]
    e2, m2 = EVENTS["sr:match:2"]
    e3, m3 = EVENTS["sr:match:3"]
    r = vision.map_leg(LEG("Arsenal", "Chelsea", "1x2", "home", odds=1.9), e1, m1)
    assert (r["market_id"], r["outcome_id"]) == ("1", "1")
    sw = LEG("Chelsea", "Arsenal", "double_chance", "X2")       # image lists Chelsea first: X2 = Arsenal or draw
    assert vision.is_swapped(sw, e1)
    r = vision.map_leg(sw, e1, m1, swapped=True)
    assert (r["market_id"], r["outcome_id"]) == ("10", "9"), r
    r = vision.map_leg(LEG("Arsenal", "Chelsea", "over_under", "over", "2.5"), e1, m1)
    assert (r["market_id"], r["specifier"], r["outcome_id"]) == ("18", "total=2.5", "12")
    r = vision.map_leg(LEG("Arsenal", "Chelsea", "btts", "Yes"), e1, m1)
    assert r["outcome_id"] == "74"
    r = vision.map_leg(LEG("Genclerbirligi", "Alanyaspor", "asian_handicap", "away", "+1.5",
                           pick="Alanyaspor (+1.5)", odds=1.16), e2, m2)
    assert (r["market_id"], r["specifier"], r["outcome_id"]) == ("16", "hcp=-1.5", "1715"), r
    r = vision.map_leg(LEG("Lorient", "Brest", "handicap", "home", "2:0", pick="Lorient (2:0)"), e3, m3)
    assert (r["market_id"], r["outcome_id"]) == ("14", "1711")
    r = vision.map_leg(LEG("Arsenal", "Chelsea", "other", "", market="Corners - Over/Under", pick="Over 9.5"), e1, m1)
    assert (r["market_id"], r["outcome_id"]) == ("166", "12"), r
    assert vision.map_leg(LEG("Arsenal", "Chelsea", "over_under", "over", "4.5"), e1, m1) is None
    assert vision.map_leg(LEG("Arsenal", "Chelsea", "other", "", market="Player to score", pick="Saka"), e1, m1) is None
    print("mapping: 1X2, DC (teams swapped), O/U 2.5, GG, AH +1.5 away -> hcp=-1.5/1715, EH 2:0, corners by text; "
          "missing line / unknown market -> None OK")


# ------------------------------------------------------------------ routing (mocked Gemini)
async def flow_routes():
    s = world()
    ai = s.ai
    img = jpeg()
    n_hist = len(await s.tracker.history.list("slip", None))

    # 1) betslip with a visible booking code -> Bet Analyzer on the code
    ai._generate = FakeGemini([read(kind="betslip", bookmaker="SportyBet", booking_code="N6RXGP",
                                    summary="A SportyBet betslip")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "analysis" and s.analyzer.codes == ["N6RXGP"] and out["analysis"]["code"] == "N6RXGP"
    call = ai._generate.calls[0]
    assert call["has_image"] and call["tools"] is None and call["kw"]["gen_extra"]["responseMimeType"] == "application/json"
    assert ai.last_image["finish"] == ["STOP"] and ai.last_image["kind"] == "betslip"
    print("route: betslip + code -> analyzer on N6RXGP (image inline, JSON mode, finishReason logged) OK")

    # 2) code read but invalid + legs visible -> legs mapped and analyzed; unmatched legs marked
    legs = [LEG("Arsenal", "Chelsea", "1x2", "home", odds=1.9, market="1X2", pick="Arsenal"),
            LEG("Genclerbirligi SK", "Alanyaspor", "asian_handicap", "away", "+1.5", "Asian Handicap",
                "Alanyaspor (+1.5)", 1.16),
            LEG("Real Madrid", "Getafe", "1x2", "home", odds=1.3, market="1X2", pick="Real Madrid"),
            LEG("Lorient", "Brest", "over_under", "over", "4.5", "Over/Under", "Over 4.5", 3.4)]
    ai._generate = FakeGemini([read(kind="betslip", booking_code="ZZZZZ9", legs=legs)])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "analysis", out
    res = out["analysis"]
    assert res["source"] == "image" and res["code"] == "" and res["summary"]["unmatched"] == 2, res["summary"]
    assert "isn't valid on SportyBet" in out["reply"] and "Matched 2 of 4" in out["reply"]
    um = [l for l in res["legs"] if l.get("unmatched")]
    assert {l["match"] for l in um} == {"Real Madrid vs Getafe", "Lorient vs Brest"}
    assert any("not found in SportyBet" in l["reason"] for l in um) and any("market not found" in l["reason"] for l in um)
    assert abs(res["summary"]["total_odds"] - round(1.9 * 1.16, 2)) < 0.02, res["summary"]["total_odds"]
    assert all(l["rated"] for l in res["legs"] if not l.get("unmatched")) and res["recommendation"]["text"]
    from fmt import analysis_messages
    msgs = analysis_messages(res)
    full = "\n".join(msgs)
    assert "couldn't match" in full and "from your screenshot" in full and "not guarantees" in full
    assert all(len(m) <= 4096 for m in msgs)
    imp = res["suggestions"].get("improved")
    assert imp is None or all(l.get("event_id") for l in imp["legs"])
    print("route: invalid code + legs -> 2 mapped (1X2, AH) + 2 'couldn't match' (not listed / no market), odds and "
          "chance exclude them, verdict line OK")

    # 3) unreadable
    ai._generate = FakeGemini([read(readable=False)])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "what is this")
    assert out["kind"] == "unreadable" and "can't read this image" in out["reply"]
    # 4) model failure / non-JSON twice -> plain error, nothing invented
    ai._generate = FakeGemini([RuntimeError("Gemini unavailable (HTTP 503)")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "error" and "Nothing was booked" in out["reply"]
    ai._generate = FakeGemini([_txt("", "MAX_TOKENS"), _txt("Sorry", "STOP")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "error" and ai.last_image["finish"] == ["MAX_TOKENS", "STOP"]
    # 5) non-betting image (the chat screenshot) -> brief honest answer
    ai._generate = FakeGemini([read(kind="other", summary="A chat about API limits",
                                    answer="It's a chat where someone asks if you pay for your API.")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "what do you see")
    assert out["kind"] == "answer" and out["reply"].startswith("It's a chat")
    # 6) betslip without code or legs -> says so plainly
    ai._generate = FakeGemini([read(kind="betslip")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "answer" and "can't make out" in out["reply"]
    # 7) every leg unmatched -> listed, no analysis
    ai._generate = FakeGemini([read(kind="betslip", legs=[LEG("Real Madrid", "Getafe", "1x2", "home")])])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert out["kind"] == "answer" and "couldn't match any" in out["reply"] and "Real Madrid" in out["reply"]
    print("route: unreadable, model error, empty MAX_TOKENS (finish logged), non-betting image, no code/legs, all "
          "unmatched -> plain answers OK")

    # 8) fixtures -> tool loop with the image, no booking tool offered, code never set
    ai._generate = FakeGemini([read(kind="fixtures", matches=[{"home": "Arsenal", "away": "Chelsea"}],
                                    summary="Two fixtures"),
                               _txt("Arsenal look stronger: 1X2 home est 50%.")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "who wins?")
    assert out["kind"] == "answer" and out["reply"].startswith("Arsenal look stronger")
    offered = {f["name"] for f in ai._generate.calls[1]["tools"][0]["functionDeclarations"]}
    assert "build_booking_code" not in offered and "predict_match" in offered and "scan_markets" in offered
    assert ai._generate.calls[1]["has_image"] and "Never book" in ai._generate.calls[1]["system"]
    assert "Estimates, not guarantees" in out["reply"]
    # fixtures, Gemini loop fails -> rule-based list from the matches read
    ai._generate = FakeGemini([read(kind="fixtures", matches=[{"home": "Arsenal", "away": "Chelsea"},
                                                               {"home": "Foo", "away": "Bar"}]),
                               RuntimeError("down")])
    out = await vision.handle_image(ai, 7, img, "image/jpeg", "")
    assert "rule_predict" in out["tools"] and "Foo v Bar: not on SportyBet" in out["reply"], out["reply"]
    print("route: fixtures -> tool loop with image (no booking tool), fallback lists matches honestly OK")

    assert not s.bookmaker.booked and len(await s.tracker.history.list("slip", None)) == n_hist
    hist = s.storage._exec("SELECT text FROM ai_history WHERE chat_id=7 ORDER BY ts", ())
    assert any(t.startswith("[image: betslip") for (t,) in hist) and not any("base64" in t for (t,) in hist)
    print("guard: 0 bookings, History unchanged, AI memory keeps a text note only OK")


async def flow_bot_slip():
    """Screenshot of the bot's own rollover preview (IMG_1777) -> resolved like a reply to it."""
    import test_slip_edit as T
    s = T.make_world()
    tr, ed, ai = s.tracker, s.editor, s.ai
    plan, _ = await tr.create_rollover(2.0, 5, None, autobook=False, generate=False, push=False)
    plan = await tr.history.get(plan["id"])
    await tr.generate_day(plan, force=True, push=False)
    day_id = (await tr.history.get(plan["id"]))["data"]["log"][-1]["slip_id"]
    rec = await tr.history.get(day_id)
    rec["data"]["code"] = "YNAP2N"
    await tr.history.save(rec)
    tok = tr.edit_preview(day_id, {"legs": T.SLIP, "total_odds": 1.96, "est_prob": 0.42})
    ed.remember(1, 501, {"section": "rollover_preview", "token": tok, "slip_id": day_id, "legs": T.SLIP})
    ed.remember(1, 400, {"section": "scan", "sid": "old", "legs": [T.L(9, "Inter", "Como", 1.2)]})
    n_hist = len(await tr.history.list("slip", None))
    ai.key = "fake"
    shot = [LEG("Genclerbirligi SK", "Alanyaspor", "asian_handicap", "away", "+1.5"),
            LEG("Sassuolo", "Fiorentina", "over_under", "over", "0.5", period="2h"),
            LEG("Lorient", "Brest", "handicap", "home", "2:0"), LEG("Newcastle", "Everton", "asian_handicap", "home", "+1.5"),
            LEG("Benfica", "Alverca Futebol", "over_under", "over", "0.5", period="2h")]
    hdr = "New picks for today (preview) Your current slip is booked as YNAP2N."
    # no caption -> says which slip, context returned (a reply to that message edits it)
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header=hdr, booking_code="YNAP2N")])
    out = await vision.handle_image(ai, 1, jpeg(), "image/jpeg", "")
    assert out["kind"] == "bot_slip" and out["context"]["section"] == "rollover_preview", out
    assert out["context"]["slip_id"] == day_id and "rollover day" in out["reply"] and "preview" in out["reply"] and "Genclerbirligi" in out["reply"]
    # caption "change the first selection" -> edit in the same section (preview with Confirm), Gemini edit fails -> rules
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header=hdr), RuntimeError("down")])
    out = await vision.handle_image(ai, 1, jpeg(), "image/jpeg", "Change the first selection")
    e = out["edit"]
    assert out["kind"] == "edit" and e and e["ok"] and e["section"] == "rollover_preview" and e["token"], out
    assert e["legs"][0]["home"] == "Arsenal" and e["old_code"] == "YNAP2N"
    # no stored refs (other chat) -> header text -> the plan's current day
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header=hdr)])
    out = await vision.handle_image(ai, 55, jpeg(), "image/jpeg", "")
    assert out["kind"] == "bot_slip" and out["context"]["section"] == "rollover_day" and out["context"]["slip_id"] == day_id
    # nothing matches at all -> honest message
    ai._generate = FakeGemini([read(kind="bot_slip", legs=[LEG("X", "Y", "1x2", "home")], header="SAFE SLIP")])
    out = await vision.handle_image(ai, 55, jpeg(), "image/jpeg", "")
    assert out["kind"] == "answer" and "can't find that slip" in out["reply"]
    assert not s.bookmaker.booked and len(await tr.history.list("slip", None)) == n_hist
    assert (await tr.history.get(day_id))["status"] == "pending"
    print("bot slip: screenshot -> stored preview ref (teams), caption edit -> rollover preview with Confirm, other chat "
          "-> header -> current day, unknown -> honest; 0 bookings OK")

    # Telegram: photo of the slip with a caption -> edit delivered with Confirm / Keep under the photo
    import handlers

    class Photo(SimpleNamespace):
        pass

    class File:
        async def download_as_bytearray(self):
            return bytearray(jpeg())

    class Bot:
        async def get_file(self, fid):
            assert fid == "big"
            return File()

    class Note(T.TgMsg):
        async def edit_text(self, text, **kw):
            self.text, self.reply_markup = text, kw.get("reply_markup")
            self.edited = True

    class UMsg(T.TgMsg):
        async def reply_text(self, text, **kw):
            m = Note(text, reply_markup=kw.get("reply_markup"), from_bot=True)
            self.sent.append(m)
            return m

    class Chat:
        id = 1

        async def send_action(self, *_a, **_k):
            pass
    photo = UMsg("")
    photo.photo = [Photo(file_id="small", width=90, height=160, file_size=2000),
                   Photo(file_id="big", width=1170, height=2532, file_size=350000)]
    photo.document, photo.caption = None, "change the first selection"
    upd = SimpleNamespace(effective_message=photo, effective_chat=Chat(), effective_user=SimpleNamespace(id=1))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"services": s}), bot=Bot())
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header=hdr), RuntimeError("down")])
    await handlers.on_image(upd, ctx)
    assert len(photo.sent) == 2, [m.text for m in photo.sent]
    assert photo.sent[0].text.startswith("📷 That's one of my slips")
    datas = [b.callback_data for row in photo.sent[1].reply_markup.inline_keyboard for b in row]
    assert photo.sent[1].text.startswith("🔄 <b>New picks for today</b>") and datas[0].startswith("RC|") and datas[1] == "NOOP"
    # a text reply to a photo -> the photo is read with that text (no caption on the photo)
    photo2 = UMsg("")
    photo2.photo, photo2.document, photo2.caption = photo.photo, None, None
    reply = UMsg("what is this?", reply_to=photo2)
    upd.effective_message = reply
    ai._generate = FakeGemini([read(kind="other", answer="A screenshot of a chat.")])
    await handlers.free_text(upd, ctx)
    assert reply.sent and reply.sent[0].text == "A screenshot of a chat."
    assert "User's caption: what is this?" in ai._generate.calls[0]["text"]
    # no-caption photo of the slip -> pointer message remembered: a reply to it edits that slip
    photo3 = UMsg("")
    photo3.photo, photo3.document, photo3.caption = photo.photo, None, None
    upd.effective_message = photo3
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header=hdr)])
    await handlers.on_image(upd, ctx)
    pointer = photo3.sent[0]
    ref = await ed.lookup(1, pointer.message_id)
    assert ref and ref["section"] == "rollover_preview" and "Reply to this message" in pointer.text
    assert not s.bookmaker.booked
    # the preview was confirmed (YNAP2N -> replaced): a screenshot read in a chat without refs, header code YNAP2N,
    # resolves to the slip that replaced it (its legs are the ones in the image), as the current rollover day
    done = await tr.refresh_confirm(tok)          # fake bookmaker in this test world
    ai._generate = FakeGemini([read(kind="bot_slip", legs=shot, header="SAFE SLIP", booking_code="YNAP2N")])
    out = await vision.handle_image(ai, 77, jpeg(), "image/jpeg", "")
    assert out["kind"] == "bot_slip" and out["context"]["slip_id"] == done["record"]["id"], out["context"]
    assert out["context"]["section"] == "rollover_day"
    print("telegram: photo + caption -> '📷 That's one of my slips' + preview with Confirm/Keep; reply to a photo -> "
          "read with the reply text; no-caption pointer message is reply-editable OK")


async def flow_webapp():
    """Mini App: POST /api/ai with an image behind the initData owner check."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import webapp as W
    s = world()
    s.ai._generate = FakeGemini([read(kind="betslip", booking_code="N6RXGP")])
    app = web.Application(client_max_size=10 * 1024 ** 2)
    W.MiniApp(s, bot_token="123456:TEST-fake-token-not-real", owner_id=1).register(app)
    import time as _t
    fields = {"auth_date": str(int(_t.time())), "query_id": "x",
              "user": json.dumps({"id": 1, "first_name": "O"}, separators=(",", ":"))}
    init = W.sign_init_data(fields, "123456:TEST-fake-token-not-real")
    other = W.sign_init_data({**fields, "user": json.dumps({"id": 2, "first_name": "X"}, separators=(",", ":"))},
                             "123456:TEST-fake-token-not-real")
    body = {"message": "", "image": {"data": base64.b64encode(jpeg()).decode(), "mime": "image/jpeg"}}
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/api/ai", json=body, headers={"X-Telegram-Init-Data": other})
        assert r.status == 403
        r = await c.post("/api/ai", json=body, headers={"X-Telegram-Init-Data": init})
        j = await r.json()
        assert r.status == 200 and j["image_kind"] == "analysis" and j["analysis"]["code"] == "N6RXGP", j
        assert j["code"] is None and "guarantee" in j["reply"].lower(), j["reply"]
        r = await c.post("/api/ai", json={"message": "", "image": {"data": "!!notbase64", "mime": "image/jpeg"}},
                         headers={"X-Telegram-Init-Data": init})
        j = await r.json()
        assert (r.status == 400 and "decoded" in j["error"]) or j.get("image_kind") == "error", j
    assert not s.bookmaker.booked
    print("webapp: /api/ai image -> 403 for non-owner, analysis for owner, bad data -> plain error, 0 bookings OK")


async def main():
    unit_parse_and_prep()
    unit_mapping()
    await flow_routes()
    await flow_bot_slip()
    await flow_webapp()
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
