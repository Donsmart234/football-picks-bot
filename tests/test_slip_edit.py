"""Reply-context slip edits: intent parsing (incl. Nigerian English), replace leg N under rollover constraints, the
impossible case, reply resolution (stored ref / buttons / text / after a restart), in-section delivery (rollover
preview with Confirm / Keep, scan result), nothing booked or saved without confirm, the AI path (Gemini edit_slip tool,
Gemini failure -> rule-based edit, never a generic slip) and the empty-reply recovery. Offline: SQLite history, fake
scanner / bookmaker / Gemini. Nothing is booked for real and nothing is sent.

    python tests/test_slip_edit.py
"""
import asyncio
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

import slipedit  # noqa: E402
from storage import Storage  # noqa: E402

NOW = datetime.now(timezone.utc)
FUT = NOW + timedelta(hours=8)


def L(eid, home, away, odds, est=0.85, mid="18", oid="12", spec="total=0.5", pick="Over 0.5", market="2nd Half - Over/Under",
      verdict="confirmed", ko=None, tags=("halves", "over", "totals")):
    return {"event_id": f"sr:match:{eid}", "home": home, "away": away, "match": f"{home} vs {away}",
            "league": "England · Premier League", "category": "England", "kickoff": (ko or FUT).isoformat(),
            "kickoff_local": "Mon 21:00", "market_id": mid, "specifier": spec, "outcome_id": oid, "market": market,
            "pick": pick, "odds": odds, "est": est, "verdict": verdict, "tags": list(tags)}


# the slip from the screenshot (YNAP2N refresh preview, total 1.96)
SLIP = [L(1, "Genclerbirligi SK", "Alanyaspor", 1.16, 0.83, "16", "1714", "hcp=-1.5", "Alanyaspor (+1.5)", "Asian Handicap -1.5",
          tags=("handicap",)),
        L(2, "Sassuolo", "Fiorentina", 1.18, 0.82),
        L(3, "Lorient", "Brest", 1.12, 0.85, "14", "1711", "hcp=2:0", "Lorient (2:0)", "Handicap 2:0", tags=("handicap",)),
        L(4, "Newcastle", "Everton", 1.13, 0.85, "16", "1714", "hcp=1.5", "Newcastle (+1.5)", "Asian Handicap 1.5",
          tags=("handicap",)),
        L(5, "Benfica", "Alverca Futebol", 1.13, 0.85)]
RULES = {"lo": 1.94, "hi": 2.40, "target": 2.0, "min_p": 0.80, "min_odds": 1.12, "max_odds": 2.40, "no_unders": True,
         "require_confirmed": True, "top_only": True, "strict": True, "label": "rollover"}


def unit_parse():
    cases = {
        "Change the first selection": ("replace", 0), "change 1st one": ("replace", 0), "swap number 2": ("replace", 1),
        "replace the last game": ("replace", 4), "pls change benfica": ("replace", 4), "I no like the third game": ("replace", 2),
        "abeg change am, the second one": ("replace", 1), "change 4": ("replace", 3),
        "comot the second one": ("remove", 1), "remove Newcastle": ("remove", 3), "drop the weakest": ("remove", None),
        "make it safer": ("safer", None), "abeg make e safer": ("safer", None), "make the second one safer": ("safer", 1),
        "this Newcastle game no go enter": ("safer", 3),
        "change leg 3 to over 1.5": ("change_market", 2), "change Sassuolo to double chance": ("change_market", 1),
        "change the market on leg 2": ("change_market", 1),
        "add one more game": ("add", None), "give me different picks": ("rebuild", None), "change all": ("rebuild", None),
    }
    for text, (act, idx) in cases.items():
        op = slipedit.parse_edit(text, SLIP)
        assert op and op["action"] == act and op["index"] == idx, (text, op)
    for text in ("what is the total?", "who wins Arsenal vs Chelsea", "thanks"):
        assert slipedit.parse_edit(text, SLIP) is None, text
    print(f"parse: {len(cases)} edit phrasings + 3 non-edits OK")


def unit_engine():
    # candidates: the only valid one is sr:match:20 (1.18, 86%); every other one breaks a rollover rule
    good = L(20, "Arsenal", "Burnley", 1.18, 0.86)
    cands = [
        good,
        L(21, "Chelsea", "Wolves", 1.18, 0.95, mid="18", oid="13", pick="Under 3.5", tags=("totals", "under")),  # under
        L(22, "Milan", "Lecce", 1.18, 0.78),                                   # est below 80%
        L(23, "Lyon", "Metz", 1.18, 0.90, verdict="odds only"),               # not stats-confirmed
        L(24, "PSV", "Heracles", 1.18, 0.92, ko=NOW + timedelta(minutes=3)),  # kicks off within 5 min
        L(2, "Sassuolo", "Fiorentina", 1.17, 0.93, oid="99", pick="Over 0.5 alt"),  # duplicate match (leg 2)
        L(25, "Porto", "Arouca", 1.60, 0.88),                                  # total would leave the window
        L(1, "Genclerbirligi SK", "Alanyaspor", 1.15, 0.84, oid="77"),        # same match as the replaced leg
    ]
    op = slipedit.parse_edit("Change the first selection", SLIP)
    r = slipedit.apply_edit(SLIP, op, cands, RULES)
    assert r["ok"], r
    assert r["legs"][0]["event_id"] == "sr:match:20" and [l["event_id"] for l in r["legs"][1:]] == \
        [l["event_id"] for l in SLIP[1:]], r["legs"]
    assert RULES["lo"] <= r["total_odds"] <= RULES["hi"] and len({l["event_id"] for l in r["legs"]}) == 5
    assert r["removed"][0]["home"] == "Genclerbirligi SK" and "Arsenal v Burnley" in r["change"]
    assert all(l["est"] >= 0.80 for l in r["legs"]) and not any("under" in l["tags"] for l in r["legs"])
    print(f"engine: replace leg 1 under rollover rules -> Arsenal v Burnley, total {r['total_odds']} OK")

    # impossible: nothing valid -> plain message, slip unchanged
    bad = [c for c in cands if c is not good]
    r = slipedit.apply_edit(SLIP, op, bad, RULES)
    assert not r["ok"] and r["legs"] == SLIP and "unchanged" in r["note"] and "1.69" not in r["change"], r
    assert "No qualifying replacement for leg 1" in r["note"], r["note"]
    print("engine: impossible replacement -> 'No qualifying replacement…, your slip is unchanged' OK")

    # two-leg fill when one leg can't reach the window
    two = [L(30, "A", "B", 1.25, 0.9), L(31, "C", "D", 1.25, 0.9)]
    rules2 = dict(RULES, lo=2.50, hi=2.80, max_odds=2.8)
    r = slipedit.apply_edit(SLIP, op, two, rules2)
    assert r["ok"] and len(r["legs"]) == 6 and rules2["lo"] <= r["total_odds"] <= rules2["hi"], r

    # remove under a strict target -> refused (would fall below the window); non-strict -> allowed
    r = slipedit.apply_edit(SLIP, {"action": "remove", "index": 0}, cands, RULES)
    assert not r["ok"] and "change leg 1" in r["note"]
    r = slipedit.apply_edit(SLIP, {"action": "remove", "index": 0}, cands, dict(RULES, strict=False))
    assert r["ok"] and len(r["legs"]) == 4
    # safer: weakest leg (Sassuolo 82%) replaced only by a stronger qualifying pick
    r = slipedit.apply_edit(SLIP, {"action": "safer", "index": None}, cands, RULES)
    assert r["ok"] and r["removed"][0]["home"] == "Sassuolo" and r["added"][0]["est"] > 0.82, r
    # safer never swaps one leg for two weaker ones (0.86 x 0.86 < 0.82): no single stronger leg fits -> refused
    pair_only = [L(32, "E", "F", 1.09, 0.86), L(33, "G", "H", 1.08, 0.86)]
    r = slipedit.apply_edit(SLIP, {"action": "safer", "index": 1}, pair_only, dict(RULES, min_odds=1.05))
    assert not r["ok"] and "already the strongest" in r["note"], r
    # change market on the same match (hint 'over 1.5')
    same = [L(3, "Lorient", "Brest", 1.12, 0.86, oid="12", spec="total=1.5", pick="Over 1.5", market="Over/Under")]
    r = slipedit.apply_edit(SLIP, slipedit.parse_edit("change leg 3 to over 1.5", SLIP), same + cands, RULES)
    assert r["ok"] and r["legs"][2]["pick"] == "Over 1.5" and r["legs"][2]["event_id"] == "sr:match:3", r
    # leg number out of range
    r = slipedit.apply_edit(SLIP, {"action": "replace", "index": 7}, cands, RULES)
    assert not r["ok"] and "no leg 8" in r["note"]
    print("engine: 2-leg fill, strict remove refused, safer, same-match market change, bad index OK")


# ------------------------------------------------------------------ fakes for the flows
class FakeBookmaker:
    load_code_url = "https://example/load"

    def __init__(self):
        self.booked = []

    async def book(self, sels):
        self.booked.append(sels)
        return f"NEW{len(self.booked):03d}"


class FakeScanner:
    def __init__(self, day_legs, cands):
        self.day_legs, self.cands, self.calls = day_legs, cands, []

    async def rollover_slip(self, target, allow_unders=False, exclude=None, fresh=False, **kw):
        legs = [l for l in self.day_legs if not (exclude and slipedit.sel_key(l) in exclude)]
        tot = slipedit.total_odds(legs)
        return {"legs": legs, "total_odds": round(tot, 2), "est_prob": round(slipedit.combined(legs), 4), "note": ""}

    async def edit_candidates(self, mode, day="today", **kw):
        self.calls.append((mode, day, kw))
        return [dict(c) for c in self.cands]


class FakeServices:
    def __init__(self, st, scanner):
        self.storage = st
        self.bookmaker = FakeBookmaker()
        self.scanner = scanner

    async def find_event(self, eid):
        return None


class Msg(SimpleNamespace):
    pass


def make_world():
    import tracker as T
    from ai import Analyst
    from editflow import SlipEditor
    d = tempfile.mkdtemp()
    st = Storage(os.path.join(d, "t.sqlite3"))
    sc = FakeScanner(SLIP, [L(20, "Arsenal", "Burnley", 1.18, 0.86), L(26, "Inter", "Como", 1.15, 0.84)])
    s = FakeServices(st, sc)
    s.tracker = T.Tracker(s)
    s.ai = Analyst(s, api_key="")
    s.editor = SlipEditor(s)
    return s


async def flow_rollover():
    s = make_world()
    tr, ed = s.tracker, s.editor
    plan, _ = await tr.create_rollover(2.0, 5, 2000.0, autobook=False, generate=False, push=False)
    plan = await tr.history.get(plan["id"])
    await tr.generate_day(plan, force=True, push=False)
    plan = await tr.history.get(plan["id"])
    day_id = plan["data"]["log"][-1]["slip_id"]
    rec = await tr.history.get(day_id)
    rec["data"]["code"] = "YNAP2N"
    await tr.history.save(rec)
    n_hist = len(await tr.history.list("slip", None))

    # the screenshot: a refresh preview message (id 501) -> the user replies "Change the first selection"
    pv_token = tr.edit_preview(day_id, {"legs": SLIP, "total_odds": 1.96, "est_prob": 0.42})
    ed.remember(1, 501, {"section": "rollover_preview", "token": pv_token, "slip_id": day_id, "legs": SLIP})
    ctx = await ed.resolve(1, Msg(message_id=501, reply_markup=None, text=""))
    assert ctx and ctx["section"] == "rollover_preview" and ctx["plan_id"] == plan["id"] and ctx["target"] == 2.0
    assert ctx["code"] == "YNAP2N" and len(ctx["legs"]) == 5 and ctx["legs"][0]["home"] == "Genclerbirligi SK"
    print("resolve: stored ref of the preview message -> rollover plan/day, target, code, 5 legs OK")

    # resolution without a stored ref: from the replied message's buttons, then from its text, then after a restart
    from editflow import SlipEditor
    kb = {"inline_keyboard": [[{"text": "Confirm: book new code", "callback_data": f"RC|{pv_token}"}],
                              [{"text": "Keep current slip", "callback_data": "NOOP"}]]}
    c2 = await ed.resolve(1, Msg(message_id=999, reply_markup=kb, text=""))
    assert c2["section"] == "rollover_preview" and c2["slip_id"] == day_id and len(c2["legs"]) == 5
    c3 = await ed.resolve(1, Msg(message_id=998, reply_markup=None, text="New picks for today (preview) Your current…"))
    assert c3["section"] == "rollover_day" and c3["slip_id"] == day_id and c3["code"] == "YNAP2N"
    ed2 = SlipEditor(s)                       # fresh process: memory gone, SQLite copy remains
    c4 = await ed2.resolve(1, Msg(message_id=501, reply_markup=None, text=""))
    assert c4 and c4["section"] == "rollover_preview" and len(c4["legs"]) == 5
    assert await ed.resolve(1, Msg(message_id=997, reply_markup=None, text="hello")) is None
    print("resolve: from buttons (RC|token), from header text, after a restart (SQLite), none for plain text OK")

    # AI path without Gemini -> rule-based edit, delivered as a rollover preview; nothing booked, nothing saved
    res = await s.ai.ask(1, "Change the first selection", context=ctx)
    out = res["edit"]
    assert out and out["ok"] and out["section"] == "rollover_preview" and out["token"], res
    assert out["legs"][0]["home"] == "Arsenal" and out["old_code"] == "YNAP2N" and 1.94 <= out["total_odds"] <= 2.40
    assert "rule_edit" in res["tools"] and res["slip"] is None and res["code"] is None
    assert not s.bookmaker.booked and len(await tr.history.list("slip", None)) == n_hist
    assert (await tr.history.get(day_id))["status"] == "pending"
    assert s.scanner.calls[-1][0] == "rollover"
    print("ai: 'Change the first selection' on the preview -> edited rollover preview, 0 bookings, History unchanged OK")

    # Telegram rendering: same section, same buttons
    import handlers
    text, kb = handlers._edit_html(out)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == [f"RC|{out['token']}", "NOOP", f"IMG|p|{out['token']}"], datas
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["Confirm: book new code", "Keep current slip", "Slip image"]
    assert "New picks for today" in text and "YNAP2N" in text and "Arsenal v Burnley" in text and "replaced" in text
    print("render: edited preview keeps 'Confirm: book new code' / 'Keep current slip' OK")

    # follow-up reply on the edited preview chains on the edited legs
    ref = ed.outcome_ref(out, ctx)
    ed.remember(1, 502, ref)
    ctx2 = await ed.resolve(1, Msg(message_id=502, reply_markup=None, text=""))
    assert ctx2["legs"][0]["home"] == "Arsenal"
    res2 = await s.ai.ask(1, "change the last one", context=ctx2)
    assert res2["edit"]["ok"] and res2["edit"]["legs"][0]["home"] == "Arsenal" and res2["edit"]["legs"][4]["home"] == "Inter"

    # impossible -> plain message, current slip kept, nothing created
    s.scanner.cands = [L(40, "X", "Y", 1.9, 0.9)]
    res3 = await s.ai.ask(1, "Change the first selection", context=ctx)
    assert res3["edit"] and not res3["edit"]["ok"] and "unchanged" in res3["edit"]["note"] and res3["reply"].startswith("Not changed:")
    assert not s.bookmaker.booked
    print("ai: chained edit on the edited preview, impossible case -> plain message + slip kept OK")

    # only the explicit confirm books and replaces (old code marked replaced, not lost)
    done = await tr.refresh_confirm(out["token"])
    assert done["code"] == "NEW001" and len(s.bookmaker.booked) == 1
    assert (await tr.history.get(day_id))["status"] == "replaced"
    new = done["record"]
    assert new["data"]["legs"][0]["home"] == "Arsenal" and new["data"]["replaces"] == day_id
    assert len(await tr.history.list("slip", None)) == n_hist + 1
    print("confirm: new code only on Confirm, old slip -> replaced (not a loss) OK")

    # after kick-off: refused
    s.scanner.cands = [L(20, "Arsenal", "Burnley", 1.18, 0.86)]
    new["data"]["legs"][2]["kickoff"] = (NOW - timedelta(minutes=2)).isoformat()
    await tr.history.save(new)
    c5 = await ed.hydrate({"section": "rollover_day", "slip_id": new["id"]})
    r5 = await ed.run(c5, {"action": "replace", "index": 0})
    assert not r5["ok"] and "kicked off" in r5["note"]
    print("rollover: edit refused after the first kick-off OK")


async def flow_scan():
    s = make_world()
    sl = {"tier": "safe", "label": "Safe", "target": 2.0, "total_odds": 1.96, "est_prob": 0.42, "reached": True,
          "legs": [dict(l) for l in SLIP], "rules": {"min_p": 0.80, "odds": (1.05, 1.35)}, "day": "today",
          "scan_args": {"tier": "safe", "target": 2.0, "day": "today", "constraints": None}}
    sid = s.ai.remember_slip(sl)
    kb = {"inline_keyboard": [[{"text": "Book on SportyBet", "callback_data": f"SB|{sid}"}],
                              [{"text": "Refresh picks", "callback_data": f"SR|{sid}"}]]}
    ctx = await s.editor.resolve(1, Msg(message_id=700, reply_markup=kb, text=""))
    assert ctx["section"] == "scan" and ctx["sid"] == sid
    res = await s.ai.ask(1, "remove am, I mean swap the second one", context=ctx)
    out = res["edit"]
    assert out["ok"] and out["section"] == "scan" and out["sid"] != sid and out["legs"][1]["home"] == "Arsenal", out
    assert out["slip"]["scan_args"] == sl["scan_args"] and not s.bookmaker.booked
    assert len(await s.tracker.history.list("slip", None)) == 0
    import handlers
    text, kb2 = handlers._edit_html(out)
    datas = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert f"SB|{out['sid']}" in datas and f"SR|{out['sid']}" in datas and "Edited slip" in text
    print("scan: reply edit -> new scan result with Book / Refresh buttons, nothing booked or saved OK")


# ------------------------------------------------------------------ Gemini paths (fake model)
class FakeGemini:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def __call__(self, contents, system=None, tools=True, max_tokens=8192, **kw):
        self.calls.append({"tools": tools, "n": len(contents)})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _txt(t, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"role": "model", "parts": [{"text": t}] if t else []}}]}


def _call(name, args):
    return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
        {"functionCall": {"name": name, "args": args}, "thoughtSignature": "sig"}]}}]}


async def flow_gemini():
    s = make_world()
    tr, ed, ai = s.tracker, s.editor, s.ai
    ai.key = "fake"
    plan, _ = await tr.create_rollover(2.0, 5, None, autobook=False, generate=False, push=False)
    plan = await tr.history.get(plan["id"])
    await tr.generate_day(plan, force=True, push=False)
    plan = await tr.history.get(plan["id"])
    day_id = plan["data"]["log"][-1]["slip_id"]
    ctx = await ed.hydrate({"section": "rollover_day", "slip_id": day_id})

    # 1) Gemini calls edit_slip(replace, leg 1) -> outcome used; scan / booking tools are not even offered
    ai._generate = FakeGemini([_call("edit_slip", {"action": "replace", "leg": 1}), _txt("Swapped leg 1 for Arsenal.")])
    res = await ai.ask(1, "abeg change the first game", context=ctx)
    assert res["edit"]["ok"] and res["edit"]["legs"][0]["home"] == "Arsenal" and res["tools"] == ["edit_slip"]
    offered = ai._generate.calls[0]["tools"][0]["functionDeclarations"]
    assert {f["name"] for f in offered} == {"edit_slip", "predict_match", "team_stats"}
    assert not s.bookmaker.booked
    print("gemini: edit_slip tool used, only edit/predict/stats tools offered in reply context OK")

    # 1b) Gemini points at a team that isn't on the slip -> its plain answer, no edit of another leg
    ai._generate = FakeGemini([_call("edit_slip", {"action": "safer", "team": "Real Oviedo"}),
                               _txt("There's no Real Oviedo game on this slip.")])
    res = await ai.ask(1, "this Real Oviedo game no go enter", context=ctx)
    assert res["edit"] is None and "no Real Oviedo" in res["reply"], res
    assert slipedit.parse_edit("this Oviedo game no go enter", SLIP) is None
    # 1c) Gemini answers a question about the slip with text only -> text, no edit
    ai._generate = FakeGemini([_txt("Total is 1.96 at about 42%.")])
    res = await ai.ask(1, "what's the total?", context=ctx)
    assert res["edit"] is None and res["reply"].startswith("Total is 1.96")
    # 2) Gemini errors -> rule-based edit (never a generic slip)
    ai._generate = FakeGemini([RuntimeError("Gemini unavailable (HTTP 503)")])
    res = await ai.ask(1, "Change the first selection", context=ctx)
    assert res["edit"]["ok"] and "rule_edit" in res["tools"] and res["slip"] is None
    # 3) Gemini returns an empty MAX_TOKENS answer twice -> rule-based edit
    ai._generate = FakeGemini([_txt("", "MAX_TOKENS"), _txt("", "MAX_TOKENS")])
    res = await ai.ask(1, "Change the first selection", context=ctx)
    assert res["edit"]["ok"] and "rule_edit" in res["tools"]
    print("gemini: model error / empty answer -> deterministic edit of the same slip OK")

    # 4) no context: empty MAX_TOKENS answer after a scan -> recovery round writes the answer
    async def fake_tool(name, args, turn):
        turn["slip"] = {"tier": "safe", "label": "Safe", "legs": [dict(SLIP[0], kickoff_local="Mon 18:00")],
                        "total_odds": 1.16, "est_prob": 0.83}
        return {"slip_id": "x"}
    ai.run_tool = fake_tool
    ai._generate = FakeGemini([_call("scan_markets", {"tier": "safe"}), _txt("", "MAX_TOKENS"), _txt("Here is your slip.")])
    res = await ai.ask(5, "safe 2 odds")
    assert res["reply"].startswith("Here is your slip.") and ai._generate.calls[-1]["tools"] is None
    assert ai.last_turn["recovery"] and "MAX_TOKENS" in ai.last_turn["finish"]
    # 5) recovery also empty -> deterministic summary of the slip, never "I couldn't finish that analysis"
    ai._generate = FakeGemini([_call("scan_markets", {"tier": "safe"}), _txt("", "MAX_TOKENS"), _txt("", "MAX_TOKENS")])
    res = await ai.ask(5, "safe 2 odds")
    assert "Genclerbirligi" in res["reply"] and "couldn't finish" not in res["reply"]
    print("gemini: empty MAX_TOKENS reply -> recovery round, then slip summary fallback OK")


class TgMsg:
    _next = 900

    def __init__(self, text="", reply_to=None, message_id=None, reply_markup=None, from_bot=False):
        self.text, self.reply_to_message, self.reply_markup = text, reply_to, reply_markup
        self.message_id = message_id or TgMsg._next
        TgMsg._next += 1
        self.chat_id = 1
        self.from_user = SimpleNamespace(is_bot=from_bot)
        self.sent = []

    async def reply_text(self, text, **kw):
        m = TgMsg(text, reply_markup=kw.get("reply_markup"), from_bot=True)
        self.sent.append(m)
        return m


async def flow_handler():
    """Telegram wiring: a reply to the preview message goes to the edit flow and is answered in the same section."""
    import handlers
    s = make_world()
    tr, ed = s.tracker, s.editor
    plan, _ = await tr.create_rollover(2.0, 5, None, autobook=False, generate=False, push=False)
    plan = await tr.history.get(plan["id"])
    await tr.generate_day(plan, force=True, push=False)
    day_id = (await tr.history.get(plan["id"]))["data"]["log"][-1]["slip_id"]
    tok = tr.edit_preview(day_id, {"legs": SLIP, "total_odds": 1.96, "est_prob": 0.42})
    preview = TgMsg("New picks for today (preview)", message_id=501, from_bot=True)
    ed.remember(1, 501, {"section": "rollover_preview", "token": tok, "slip_id": day_id, "legs": SLIP})
    user_msg = TgMsg("Change the first selection", reply_to=preview)

    class Chat:
        id = 1

        async def send_action(self, *_a, **_k):
            pass
    upd = SimpleNamespace(effective_message=user_msg, effective_chat=Chat(), effective_user=SimpleNamespace(id=1))
    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={"services": s}))
    await handlers.free_text(upd, ctx)
    assert len(user_msg.sent) == 1, [m.text for m in user_msg.sent]
    out = user_msg.sent[0]
    datas = [b.callback_data for row in out.reply_markup.inline_keyboard for b in row]
    assert out.text.startswith("<b>New picks for today</b> (preview)") and datas[0].startswith("RC|") and datas[1] == "NOOP"
    assert "Arsenal v Burnley" in out.text and "Real Oviedo" not in out.text and not s.bookmaker.booked
    ref = await ed.lookup(1, out.message_id)
    assert ref and ref["section"] == "rollover_preview" and ref["legs"][0]["home"] == "Arsenal"
    # not a reply, but names a leg -> edits the last slip shown (the edited preview)
    m2 = TgMsg("now change the second selection")
    upd.effective_message = m2
    await handlers.free_text(upd, ctx)
    assert m2.sent and "New picks for today" in m2.sent[0].text and "Inter v Como" in m2.sent[0].text, m2.sent[0].text
    print("telegram: reply 'Change the first selection' -> rollover preview with Confirm/Keep under it; "
          "follow-up without reply edits the last slip OK")


async def main():
    unit_parse()
    unit_engine()
    await flow_rollover()
    await flow_scan()
    await flow_gemini()
    await flow_handler()
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
