"""Telegram handlers: owner lock, browsing, predictions, bet slip and booking codes."""
from __future__ import annotations

import html
import logging
import math
import re

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, MenuButtonWebApp, Update,
                      WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

import config
from bookmakers import MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SELECTIONS, BookingError, Selection, code_link
from ai import md_to_html
from analyzer import bare_code, extract_code
from fmt import RULE, STATUS_ICON, analysis_messages, code_html, record_text, slip_text
from predictor import Prediction
from constraints import parse_text
from scanner import TIER_ALIASES
from services import TZ, Services

log = logging.getLogger(__name__)
PAGE = 8

RG_NOTE = ("⚠️ <i>Predictions are statistical estimates, never guarantees. Bet only what you can afford "
           "to lose, 18+ only. If gambling stops being fun, take a break.</i>")
HELP = (
    "<b>Commands</b>\n"
    "/scan safe 5 – scan markets, build a slip (tiers: safe, mid, risk, high, audit). Filters: nounders, -corners, "
    "-cards, -props, only:1x2,dc, legs:4-8, minodds:1.2, league:epl, after:18:00, allleagues (top leagues by default)\n"
    "/ai question – ask the AI analyst (or just type any message)\n"
    "📷 Send a screenshot (betslip, booking code, fixtures, one of my slips) and I'll read it; add a caption to say what you want\n"
    "/today – today's matches by league\n"
    "/upcoming – next 3 days\n"
    "/predict Team A vs Team B – analyse a match\n"
    "/slip – your picks · /book – booking code · /clear – empty slip\n"
    "/analyze CODE – analyze any SportyBet booking code (or just paste the 6-character code)\n"
    "/history – booked codes and results (pinged when a slip lands)\n"
    "/rollover 2 7 – rollover plan: daily target 2–5, 3–15 days (+ optional stake) · /rollover tickets – every day's ticket\n"
    "/reset – clear the AI chat memory"
)


def svc(ctx: ContextTypes.DEFAULT_TYPE) -> Services:
    return ctx.application.bot_data["services"]


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


# ---------------------------------------------------------------- owner lock
async def owner_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    s = svc(ctx)
    owner = s.storage.get_owner()
    if owner is None:
        msg = update.effective_message
        if msg and msg.text and msg.text.split()[0].split("@")[0] == "/start" and s.storage.claim_owner(user.id):
            log.info("Owner claimed by first /start")
            return
        raise ApplicationHandlerStop
    if user.id != owner:
        if _analyzer_update(update) and config.analyzer_allowed(user.id, owner):
            return                   # ANALYZER_ACCESS=allowlist|public opens the analyzer only
        raise ApplicationHandlerStop  # silently ignore everyone else


def _analyzer_update(update: Update) -> bool:
    q = update.callback_query
    if q is not None:
        return (q.data or "").startswith("AZ")
    msg = update.effective_message
    t = (msg.text or "").strip() if msg else ""
    return t.split()[0].split("@")[0].lower() == "/analyze" if t.startswith("/") else bool(bare_code(t))


def is_owner(ctx, uid: int) -> bool:
    return uid == svc(ctx).storage.get_owner()


def app_url() -> str | None:
    base = config.PUBLIC_BASE_URL
    return f"{base}/app" if base.startswith("https://") else None


# ---------------------------------------------------------------- commands
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    url = app_url()
    kb = Kb([[Btn("📈 Open Fra app", web_app=WebAppInfo(url))]]) if url else None
    await update.effective_message.reply_text(
        "👋 <b>Fra Prediction</b> – private football analytics desk\nScans SportyBet markets, checks team form, "
        "home/away records and H2H, and builds Safe / Mid-safe / Risk / High-risk slips to your target odds. "
        "Type any question to ask the AI analyst.\n\n"
        + ("Tap <b>Open Fra app</b> (or the menu button) for the full app.\n\n" if url else "")
        + HELP + "\n\n" + RG_NOTE,
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_leagues(update, ctx, "t", 0, edit=False)


async def upcoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_leagues(update, ctx, "u", 0, edit=False)


async def predict_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.effective_message.reply_text("Usage: /predict Arsenal vs Chelsea")
        return
    await _predict_query(update, ctx, query)


def _remember(ctx, msg, ref: dict | None) -> None:
    """Store which slip a sent bot message shows, so a reply to it edits that slip (editflow.py)."""
    ed = getattr(svc(ctx), "editor", None)
    if ed is None or msg is None or not ref:
        return
    try:
        ed.remember(msg.chat_id, msg.message_id, ref)
    except Exception as exc:
        log.warning("slip ref not stored: %s", exc)


async def _slip_context(update: Update, ctx, text: str) -> dict | None:
    """The slip a message refers to: the bot message it replies to, or (no reply) the last slip shown when the
    message names a leg ("change the first selection")."""
    ed = getattr(svc(ctx), "editor", None)
    msg = update.effective_message
    if ed is None:
        return None
    chat_id = update.effective_chat.id
    rt = msg.reply_to_message
    try:
        if rt is not None and (rt.from_user is None or rt.from_user.is_bot):
            return await ed.resolve(chat_id, rt)
        if rt is None:
            from slipedit import parse_edit
            ref = await ed.last_ref(chat_id)
            if ref and ref.get("legs"):
                op = parse_edit(text, ref["legs"])
                if op and op.get("index") is not None:
                    return await ed.hydrate(ref)
    except Exception as exc:
        log.warning("reply context failed: %s", exc, exc_info=True)
    return None


async def free_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    code = bare_code(text)
    if code:                      # a pasted 6-character booking code: analyze it
        await _run_analysis(update, ctx, code, "")
        return
    rt = update.effective_message.reply_to_message
    if rt is not None and _image_ref(rt) is not None:   # a text reply to a photo: read the photo with this text
        await _handle_image(update, ctx, rt, text)
        return
    sctx = await _slip_context(update, ctx, text)
    ai = getattr(svc(ctx), "ai", None)
    if sctx is not None and ai is not None:
        await _ask_ai(update, ctx, text, sctx)
    elif ai and ai.enabled:
        await _ask_ai(update, ctx, text)
    elif " vs " in text.lower() or " v " in text.lower():
        await _predict_query(update, ctx, text)
    else:
        await update.effective_message.reply_text("Try /scan safe 5, /predict Team A vs Team B, or /today.")


async def ai_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(ctx.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Ask me anything, e.g. <i>Analyze today's games and give me a safe 5 odds Sporty code</i> or "
            "<i>Who wins Arsenal vs Chelsea?</i>. You can also just type without /ai.", parse_mode=ParseMode.HTML)
        return
    await _ask_ai(update, ctx, text)


async def analyze_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = " ".join(ctx.args or []).strip()
    code = extract_code(args.split()[0]) if args else None
    if not code:
        await update.effective_message.reply_text(
            "Send <code>/analyze CODE</code> (e.g. <code>/analyze N6RXGP</code>) or just paste the 6-character "
            "SportyBet booking code. Add rules for the suggested swaps if you like: <code>/analyze N6RXGP no unders</code>.",
            parse_mode=ParseMode.HTML)
        return
    await _run_analysis(update, ctx, code, " ".join(args.split()[1:]))


def open_code_kb(bookmaker, code: str | None) -> Kb:
    """One url button that opens the booking code in the SportyBet app when installed (website otherwise).
    A url button (not a web_app one) so Telegram hands the link to the OS, which routes the app-claimed link."""
    if not code:
        return Kb([[Btn("Open SportyBet code hub", url=code_link(bookmaker, None))]])
    return Kb([[Btn(f"📲 Open {code} in SportyBet", url=code_link(bookmaker, code))]])


def _analysis_kb(res: dict) -> Kb | None:
    rows = []
    imp = (res.get("suggestions") or {}).get("improved")
    if imp and imp.get("legs") and imp.get("bookable"):
        rows.append([Btn(f"♻️ Rebook improved slip ({imp['total_odds']:.2f} · {imp['chance']:.0%})",
                         callback_data=f"AZR|{res['id']}")])
    rows.append([Btn("🧠 AI take", callback_data=f"AZN|{res['id']}"),
                 Btn("📲 Open in SportyBet" if res.get("code") else "Open SportyBet",
                     url=res.get("load_url") or code_link(None, res.get("code")))])
    return Kb(rows)


async def _run_analysis(update: Update, ctx, code: str, rules: str) -> None:
    s = svc(ctx)
    an = getattr(s, "analyzer", None)
    if an is None:
        await update.effective_message.reply_text("The analyzer isn't available right now.")
        return
    note = await update.effective_message.reply_text(
        f"🔍 Loading <code>{esc(code)}</code> and rating every leg (form, H2H, team news)… ~10 s",
        parse_mode=ParseMode.HTML)
    try:
        res = await an.analyze(code, rules)   # not saved / tracked
    except Exception as exc:
        log.warning("analyze failed: %s", exc, exc_info=True)
        await note.edit_text("The analysis failed on my side (SportyBet or the stats feed didn't answer). "
                             "Send the code again in a minute.")
        return
    if not res.get("ok"):
        icon = "⛔" if res.get("kind") == "invalid" else "⚠️"
        await note.edit_text(f"{icon} <code>{esc(code)}</code>: {esc(res.get('error', 'could not load it'))}",
                             parse_mode=ParseMode.HTML)
        return
    await _send_analysis(update, ctx, res, note)


async def _send_analysis(update: Update, ctx, res: dict, note, lead: str = "") -> None:
    """Analyzer result (code or screenshot) as Telegram messages, the first one edited into `note`."""
    msgs = analysis_messages(res)
    if lead:
        msgs[0] = lead + "\n" + msgs[0]
    kb = _analysis_kb(res)
    await note.edit_text(msgs[0][:4096], parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                         reply_markup=kb if len(msgs) == 1 else None)
    sent = [note]
    for i, m in enumerate(msgs[1:], 1):
        sent.append(await update.effective_message.reply_text(m, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                                              reply_markup=kb if i == len(msgs) - 1 else None))
    ref = {"section": "analysis", "aid": res.get("id"), "code": res.get("code"),
           "legs": [l for l in res.get("legs") or [] if l.get("event_id")]}
    for m in sent:
        _remember(ctx, m, ref)


async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ai = getattr(svc(ctx), "ai", None)
    if ai:
        ai.reset(update.effective_chat.id)
    await update.effective_message.reply_text("🧹 AI chat memory cleared.")


async def _typing_loop(chat, stop):
    import asyncio
    while not stop.is_set():
        try:
            await chat.send_action("typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


async def _ask_ai(update: Update, ctx, text: str, sctx: dict | None = None) -> None:
    import asyncio
    s = svc(ctx)
    stop = asyncio.Event()
    typer = asyncio.create_task(_typing_loop(update.effective_chat, stop))
    try:
        res = await (s.ai.ask(update.effective_chat.id, text, context=sctx) if sctx else
                     s.ai.ask(update.effective_chat.id, text))
    except Exception as exc:
        log.warning("AI failed: %s", exc)
        res = {"reply": f"The AI analyst is unavailable right now ({esc(str(exc)[:120])}). /scan still works.",
               "slip": None, "code": None}
    finally:
        stop.set()
        await typer
    if sctx is not None and res.get("edit") is not None:
        await _send_edit(update, ctx, res["edit"], sctx)
        return
    await _deliver_ai(update, ctx, res)


async def _deliver_ai(update: Update, ctx, res: dict, note=None) -> None:
    """Send an AI answer (text + Book / Rebook / code buttons) and remember the slip it shows."""
    s = svc(ctx)
    kb = None
    rows = []
    an_res = res.get("analysis")
    if an_res and an_res.get("id"):
        imp = (an_res.get("suggestions") or {}).get("improved")
        if imp and imp.get("legs") and imp.get("bookable"):
            rows.append([Btn(f"♻️ Rebook improved slip ({imp['total_odds']:.2f} · {imp['chance']:.0%})",
                             callback_data=f"AZR|{an_res['id']}")])
    if res.get("code"):
        rows.append([Btn(f"📲 Open {res['code']} in SportyBet", url=code_link(s.bookmaker, res["code"]))])
    elif res.get("slip") and res["slip"].get("slip_id") and res["slip"].get("legs"):
        rows.append([Btn("📒 Book this slip on SportyBet", callback_data=f"SB|{res['slip']['slip_id']}")])
    if rows:
        kb = Kb(rows)
    html_text = md_to_html(res["reply"])
    sent = None
    for i in range(0, len(html_text), 3900):
        chunk_kb = kb if i + 3900 >= len(html_text) else None
        if i == 0 and note is not None:
            await note.edit_text(html_text[i:i + 3900], parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                 reply_markup=chunk_kb)
            sent = note
            continue
        sent = await update.effective_message.reply_text(html_text[i:i + 3900], parse_mode=ParseMode.HTML,
                                                         disable_web_page_preview=True, reply_markup=chunk_kb)
    sl = res.get("slip")
    if sl and sl.get("slip_id") and sl.get("legs") and not res.get("code"):
        _remember(ctx, sent, {"section": "scan" if sl.get("tier") in ("safe", "mid", "risk", "high") else "ai",
                              "sid": sl["slip_id"], "legs": sl["legs"],
                              "slip": {k: v for k, v in sl.items() if k != "legs"}})
    elif an_res and an_res.get("id"):
        _remember(ctx, sent, {"section": "analysis", "aid": an_res["id"], "code": an_res.get("code"),
                              "legs": [l for l in an_res.get("legs") or [] if l.get("event_id")]})


# ---------------------------------------------------------------- images (vision.py)
def _image_ref(msg) -> tuple[str, int, str] | None:
    """(file_id, size, mime) of the image a message carries (largest photo size, or an image document)."""
    if msg is None:
        return None
    photos = getattr(msg, "photo", None) or ()
    if photos:
        p = max(photos, key=lambda x: (x.width or 0) * (x.height or 0))
        return p.file_id, int(p.file_size or 0), "image/jpeg"
    doc = getattr(msg, "document", None)
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return doc.file_id, int(doc.file_size or 0), doc.mime_type
    return None


async def on_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await _handle_image(update, ctx, msg, (msg.caption or "").strip())


async def _handle_image(update: Update, ctx, img_msg, caption: str) -> None:
    import asyncio
    import vision
    s = svc(ctx)
    msg = update.effective_message
    ref = _image_ref(img_msg)
    if ref is None:
        return
    file_id, size, mime = ref
    if size and size > vision.MAX_DOWNLOAD:
        await msg.reply_text("⚠️ That file is over 20 MB; send a screenshot or a smaller photo.")
        return
    ai = getattr(s, "ai", None)
    if ai is None:
        await msg.reply_text("The AI analyst isn't available right now.")
        return
    note = await msg.reply_text("📷 Reading your image…")
    stop = asyncio.Event()
    typer = asyncio.create_task(_typing_loop(update.effective_chat, stop))
    try:
        f = await ctx.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
        out = await asyncio.wait_for(vision.handle_image(ai, update.effective_chat.id, data, mime, caption),
                                     timeout=150)
    except asyncio.TimeoutError:
        out = {"kind": "error", "reply": "Reading that image took too long, so I stopped. Nothing was booked. Send it "
                                         "again, or paste the booking code as text."}
    except Exception as exc:
        log.warning("image handling failed: %s", exc, exc_info=True)
        out = {"kind": "error", "reply": "I couldn't process that image on my side. Nothing was booked. Send it again "
                                         "in a minute."}
    finally:
        stop.set()
        await typer
    await _send_image_result(update, ctx, out, note)


async def _send_image_result(update: Update, ctx, out: dict, note) -> None:
    kind = out.get("kind")
    if kind == "analysis" and out.get("analysis"):
        res = out["analysis"]
        lead = (f"📷 Read <code>{esc(out['code_read'])}</code> from your screenshot." if out.get("code_read")
                else "📷 " + esc(out["reply"].split("\n")[0]))
        await _send_analysis(update, ctx, res, note, lead)
        return
    if kind == "edit":
        ed_out, sctx = out.get("edit"), out.get("context") or {}
        if ed_out is not None:
            await note.edit_text("📷 That's one of my slips: " + esc(out.get("reply") or ""), parse_mode=ParseMode.HTML)
            await _send_edit(update, ctx, ed_out, sctx)
            return
        await note.edit_text(md_to_html(out.get("reply") or "")[:4096], parse_mode=ParseMode.HTML)
        _remember(ctx, note, sctx)
        return
    if kind == "bot_slip":
        await note.edit_text(md_to_html(out["reply"])[:4096], parse_mode=ParseMode.HTML)
        _remember(ctx, note, out.get("context"))     # a reply to this message edits that slip
        return
    if kind == "answer" and out.get("slip"):
        await _deliver_ai(update, ctx, {"reply": out["reply"], "slip": out["slip"], "code": None}, note)
        return
    await note.edit_text(md_to_html(out.get("reply") or "I couldn't read that image.")[:4096], parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


def _edit_html(out: dict) -> tuple[str, Kb | None]:
    """Telegram message for an edited slip, in its own section (rollover preview / scan result / custom slip)."""
    from fmt import leg_lines
    legs = out["legs"]
    body = "\n".join(leg_lines(l, i, why=False) for i, l in enumerate(legs, 1))
    change = f"✏️ <i>{esc(out.get('change') or '')}</i>"
    tail = (f"{RULE}\n<b>Total odds <code>{out['total_odds']:.2f}</code></b> · est. hit chance "
            f"<b>{out['est_prob']:.0%}</b>")
    if out["section"] == "rollover_preview":
        if out.get("old_code"):
            lead = (f"Your current slip is booked as <code>{esc(out['old_code'])}</code>. Confirm to book a <b>new code</b>; "
                    "the old one is marked <i>replaced</i> (not counted as a loss) and stops being tracked.")
        else:
            lead = ("Confirm to book these picks as today's code; the current (unbooked) slip is marked <i>replaced</i>.")
        text = (f"🔄 <b>New picks for today</b> (preview) · Rollover day {out.get('day') or ''} · target "
                f"{(out.get('target') or 0):g}\n{change}\n{lead}\n{RULE}\n{body}\n{tail}\n<i>Same rules: top leagues, "
                "every leg ≥80% est." + ("" if out.get("allow_unders") else " No unders.")
                + " Nothing is booked until you confirm. Estimates, not guarantees. 18+.</i>")
        kb = Kb([[Btn("✅ Confirm: book new code", callback_data=f"RC|{out['token']}")],
                 [Btn("Keep current slip", callback_data="NOOP")]])
        return text[:4096], kb
    sl = out.get("slip") or {}
    if sl.get("tier") in ("safe", "mid", "risk", "high"):
        head = f"✏️ <b>Edited slip</b> · not saved until you book\n{change}\n"
        return _scan_msg(sl, head), _scan_kb(out.get("sid"), sl)
    title = ("✏️ <b>Edited slip</b>" + (f" (from <code>{esc(out.get('based_on'))}</code>)" if out.get("based_on") else "")
             + f" · not saved until you book\n{change}")
    text = f"{title}\n{RULE}\n{body}\n{tail}\n<i>Estimates, not guarantees. 18+.</i>"
    kb = Kb([[Btn("📒 Book this slip on SportyBet", callback_data=f"SB|{out['sid']}")]]) if out.get("sid") else None
    return text[:4096], kb


async def _send_edit(update: Update, ctx, out: dict, sctx: dict) -> None:
    msg = update.effective_message
    if not out.get("ok"):
        await msg.reply_text(f"⚠️ {esc(out.get('note') or 'That change is not possible right now.')}",
                             parse_mode=ParseMode.HTML)
        return
    text, kb = _edit_html(out)
    sent = await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    ed = getattr(svc(ctx), "editor", None)
    if ed is not None:
        _remember(ctx, sent, ed.outcome_ref(out, sctx))


def format_slip(sl: dict, why: bool = True) -> str:
    rule = f"Each leg est. ≥{sl['rules']['min_p']:.0%} after the stats check" if sl.get("rules", {}).get("min_p") else ""
    title = (f"🧮 <b>{esc(sl['label']).upper()} SLIP</b> · target {sl['target']:g} · {esc(sl.get('day', ''))}"
             + (f"\n<i>{rule} · {sl.get('candidates', 0)} qualified · {sl.get('with_stats', 0)} matches stats-checked</i>"
               if rule and not sl.get("edited") else f"\n<i>{rule}</i>" if rule else ""))
    if sl.get("constraints_text"):
        chk = sl.get("constraint_check") or {}
        title += (f"\n🎛 Your rules: {esc(sl['constraints_text'])}"
                  + (" · ✅ every leg re-checked" if chk.get("ok") and sl.get("legs") else ""))
    return slip_text(sl, title, why=why)


async def _record(ctx, legs, code, source, **kw) -> None:
    tr = getattr(svc(ctx), "tracker", None)
    if tr:
        await tr.record(legs, code, source, **kw)


async def scan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.lower() for a in (ctx.args or [])]
    tier, target, day = "safe", None, "today"
    for a in args:
        if a in TIER_ALIASES:
            tier = TIER_ALIASES[a]
        elif a in ("today", "tomorrow", "24h", "48h"):
            day = a
        elif re.fullmatch(r"\d+(\.\d+)?(odds|x)?", a) and target is None:
            target = float(a.replace("odds", "").replace("x", ""))
    # filters: nounders / -corners / only:1x2,dc / legs:4-8 / minodds:1.2 / maxodds:1.5 / league:epl /
    # country:england / team:arsenal / after:18:00 / before:21:00 / minp:85 (or plain words: "no unders")
    c = parse_text(" ".join(args))
    c.tier, c.target_odds = None, None
    ai_ = getattr(svc(ctx), "ai", None)
    if ai_ is not None and args:
        try:     # team names typed after /scan ("/scan safe 5 arsenal bayern") are a hard scope too
            hr = await ai_.hard_rules("picks " + " ".join(a for a in args if ":" not in a))
            c.include_teams = c.include_teams or hr.include_teams
            c.exclude_teams = c.exclude_teams or hr.exclude_teams
        except Exception:
            pass
    if c.day:
        day = c.day
    c = c if not c.empty() else None
    s = svc(ctx)
    rules = f" · {esc(c.describe())}" if c else ""
    note = await update.effective_message.reply_text(
        f"🔎 Scanning up to 80 {'' if c and c.widens_leagues() else 'top-league '}matches ({day}){rules}, every market, "
        "then checking form/H2H… ~30 s",
        parse_mode=ParseMode.HTML)
    try:
        res = await s.scanner.scan(tier, target, day, None, c)
    except Exception as exc:
        log.warning("scan failed: %s", exc, exc_info=True)
        await note.edit_text("The market scan failed (SportyBet didn't answer). Try again in a minute.")
        return
    ai = getattr(s, "ai", None)
    if tier == "audit":
        parts = [f"📊 <b>Grand audit</b> · {res['scanned']} matches scanned, {res['with_stats']} stats-checked, "
                 f"{res['dropped_by_stats']} legs dropped by stats"]
        parts.append("\n<b>Best leg per match</b>")
        for l in res["best_per_match"][:10]:
            parts.append(f"• {esc(l['kickoff_local'][4:])} {esc(l['match'])}: <b>{esc(l['pick'])}</b> "
                         f"({esc(l['market'])}) @ {l['odds']:.2f} · {l['est']:.0%}")
        parts.append("\n<b>Top value</b>")
        for l in res["top_value"][:6]:
            parts.append(f"• {esc(l['match'])}: <b>{esc(l['pick'])}</b> @ {l['odds']:.2f} · est {l['est']:.0%} "
                         f"(EV {l['ev']:.2f})")
        rows = []
        for k, sl in res["slips"].items():
            sid = ai.remember_slip(sl) if ai else None
            parts.append(f"\n<b>{esc(sl['label'])}</b>: {len(sl['legs'])} legs, total {sl['total_odds']:.2f}, "
                         f"est {sl['est_prob']:.0%}" + (f" – {esc(sl['note'])}" if sl["note"] else ""))
            if sid and sl["legs"]:
                rows.append([Btn(f"📒 Book {sl['label']} ({sl['total_odds']:.2f})", callback_data=f"SB|{sid}")])
        parts.append("\n<i>Estimates, not guarantees. 18+.</i>")
        await note.edit_text("\n".join(parts)[:4000], parse_mode=ParseMode.HTML, reply_markup=Kb(rows) if rows else None)
        return
    res["scan_args"] = {"tier": tier, "target": target, "day": day, "constraints": c.to_dict() if c else None}
    sid = ai.remember_slip(res) if ai else None
    await note.edit_text(_scan_msg(res), parse_mode=ParseMode.HTML, reply_markup=_scan_kb(sid, res))
    if sid and res.get("legs"):
        _remember(ctx, note, _scan_ref(sid, res))


def _scan_ref(sid: str, res: dict) -> dict:
    return {"section": "scan", "sid": sid, "legs": res["legs"], "slip": {k: v for k, v in res.items() if k != "legs"}}


def _scan_msg(res: dict, head: str = "") -> str:
    text = head + format_slip(res)
    if len(text) > 4000:
        text = head + format_slip(res, why=False)
    return text[:4096]


def _scan_kb(sid: str | None, res: dict) -> Kb | None:
    if not sid:
        return None
    rows = [[Btn("📒 Book on SportyBet", callback_data=f"SB|{sid}")]] if res["legs"] else []
    rows.append([Btn("🔄 Refresh picks", callback_data=f"SR|{sid}"), Btn("🔀 Different picks", callback_data=f"SR|{sid}|x")])
    return Kb(rows)


async def _scan_refresh(q, ctx, sid: str, exclude_prev: bool) -> None:
    """Re-run a /scan slip with fresh prices and the same rules; nothing is saved until the user books."""
    from constraints import Constraints
    from scanner import sel_key
    s = svc(ctx)
    ai = getattr(s, "ai", None)
    entry = ai.slips.get(sid) if ai else None
    args = (entry or {}).get("slip", {}).get("scan_args")
    if not entry or not args:
        await q.answer("That slip expired. Run /scan again.", show_alert=True)
        return
    await q.answer("Refreshing picks… ~30 s")
    old = entry["slip"]
    prev = {sel_key(l) for l in old.get("legs") or []}
    c = Constraints.from_dict(args["constraints"]) if args.get("constraints") else None
    if c is not None:
        c.tier, c.target_odds = None, None
    try:
        res = await s.scanner.scan(args["tier"], args.get("target"), args["day"], None, c,
                                   exclude=prev if exclude_prev else None, fresh=True)
    except Exception as exc:
        log.warning("scan refresh failed: %s", exc, exc_info=True)
        await q.message.reply_text("The refresh failed (SportyBet didn't answer). Try again in a minute.")
        return
    new = {sel_key(l) for l in res["legs"]}
    if not res["legs"]:
        await q.message.reply_text("🔄 No different valid slip exists right now under the same rules"
                                   + (" without the previous picks" if exclude_prev else "") + ". The last slip stands.")
        return
    if new == prev:
        await q.message.reply_text("🔄 Fresh prices give the same picks: they're still the strongest. "
                                   "Tap 🔀 Different picks to leave them out.")
        return
    res["scan_args"] = args
    nsid = ai.remember_slip(res)
    kept = len(new & prev)
    head = f"🔄 <b>Refreshed</b> · {len(new) - kept} new pick(s), {kept} kept · not saved until you book\n"
    sent = await q.message.reply_text(_scan_msg(res, head), parse_mode=ParseMode.HTML, reply_markup=_scan_kb(nsid, res))
    _remember(ctx, sent, _scan_ref(nsid, res))


async def slip_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await _render_slip(update.effective_user.id, ctx)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def book_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(**await _book(update.effective_user.id, ctx))


async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    svc(ctx).storage.slip_clear(update.effective_user.id)
    await update.effective_message.reply_text("🗑 Slip cleared.")


# ---------------------------------------------------------------- browse
async def _send_leagues(update: Update, ctx, scope: str, page: int, edit: bool) -> None:
    s = svc(ctx)
    try:
        events = await s.events(scope)
    except Exception as exc:
        log.warning("event fetch failed: %s", exc)
        await _reply_or_edit(update, "SportyBet didn't answer just now. Try again in a minute.", None, edit)
        return
    leagues = s.leagues(events)
    title = "Today" if scope == "t" else "Next 3 days"
    if not leagues:
        await _reply_or_edit(update, f"No upcoming matches found for {title.lower()}.", None, edit)
        return
    pages = math.ceil(len(leagues) / PAGE)
    page = max(0, min(page, pages - 1))
    rows = [[Btn(f"{name} ({n})"[:60], callback_data=f"LM|{scope}|{tid}|0")]
            for tid, name, n in leagues[page * PAGE:(page + 1) * PAGE]]
    rows.append(_pager(f"LL|{scope}", page, pages))
    text = f"⚽ <b>{title}</b>: {len(events)} matches in {len(leagues)} leagues. Pick a league:"
    await _reply_or_edit(update, text, Kb([r for r in rows if r]), edit)


async def _send_matches(update: Update, ctx, scope: str, tid: str, page: int) -> None:
    s = svc(ctx)
    events = [e for e in await s.events(scope) if e.tournament_id == tid]
    if not events:
        await _reply_or_edit(update, "No upcoming matches left in this league.", None, True)
        return
    pages = math.ceil(len(events) / PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for e in events[page * PAGE:(page + 1) * PAGE]:
        t = e.start.astimezone(TZ).strftime("%a %H:%M" if scope == "u" else "%H:%M")
        rows.append([Btn(f"{t} {e.home} – {e.away}"[:60], callback_data=f"P|{e.event_id}")])
    rows.append(_pager(f"LM|{scope}|{tid}", page, pages))
    rows.append([Btn("⬅️ Leagues", callback_data=f"LL|{scope}|0")])
    await _reply_or_edit(update, f"🏆 <b>{esc(events[0].league_full)}</b> – pick a match:",
                         Kb([r for r in rows if r]), True)


def _pager(prefix: str, page: int, pages: int) -> list[Btn]:
    row = []
    if page > 0:
        row.append(Btn("◀️ Prev", callback_data=f"{prefix}|{page - 1}"))
    if pages > 1:
        row.append(Btn(f"{page + 1}/{pages}", callback_data="NOOP"))
    if page < pages - 1:
        row.append(Btn("Next ▶️", callback_data=f"{prefix}|{page + 1}"))
    return row


async def _reply_or_edit(update: Update, text: str, kb, edit: bool) -> None:
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- predictions
async def _predict_query(update: Update, ctx, query: str) -> None:
    s = svc(ctx)
    try:
        ranked = await s.search(query)
    except Exception as exc:
        log.warning("search failed: %s", exc)
        await update.effective_message.reply_text("SportyBet didn't answer just now. Try again in a minute.")
        return
    if not ranked or ranked[0][0] < 60:
        await update.effective_message.reply_text(
            "I couldn't find that match in SportyBet's next 3 days of football. Check the team names or use /upcoming.")
        return
    top_score, top = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0
    if top_score >= 85 and top_score - second >= 5:
        await _send_prediction(update, ctx, top.event_id, edit=False)
        return
    rows = [[Btn(f"{e.start.astimezone(TZ).strftime('%a %H:%M')} {e.home} – {e.away}"[:60],
                 callback_data=f"P|{e.event_id}")] for sc, e in ranked if sc >= 60]
    await update.effective_message.reply_text("Which match did you mean?", reply_markup=Kb(rows))


def format_prediction(p: Prediction) -> str:
    e, pr = p.event, p.probs

    def line(mid: str, oid: str) -> str:
        label, spec = SELECTIONS[(mid, oid)]
        odds = e.odds(mid, oid, spec)
        return f"{label}: <b>{pr[(mid, oid)]:.0%}</b>" + (f" @ {odds:.2f}" if odds else "")

    when = e.start.astimezone(TZ).strftime("%a %d %b, %H:%M")
    parts = [
        f"⚽ <b>{esc(e.home)} vs {esc(e.away)}</b>",
        f"🏆 {esc(e.league_full)} · 🕒 {when} (Lagos)",
        "",
        "<b>1X2</b>  " + " | ".join(line(MARKET_1X2, o) for o in ("1", "2", "3")),
        "<b>O/U 2.5</b>  " + " | ".join(line(MARKET_OU, o) for o in ("12", "13")),
        "<b>BTTS</b>  " + " | ".join(line(MARKET_BTTS, o) for o in ("74", "76")),
        "<b>Double chance</b>  " + " | ".join(line(MARKET_DC, o) for o in ("9", "10", "11")),
        "",
    ]
    if p.best:
        odds = f" @ {p.best.odds:.2f}" if p.best.odds else ""
        parts.append(f"⭐ <b>Best pick: {p.best.label}</b> ({p.best.prob:.0%}{odds})")
    parts.append(esc(p.reasoning))
    parts.append(f"<i>Model: {p.source}. Estimate only – not guaranteed.</i>")
    return "\n".join(parts)


def prediction_keyboard(p: Prediction) -> Kb:
    eid = p.event.event_id

    def b(mid: str, oid: str) -> Btn:
        label, spec = SELECTIONS[(mid, oid)]
        odds = p.event.odds(mid, oid, spec)
        short = label.split(" (")[0]
        return Btn(f"➕ {short}" + (f" {odds:.2f}" if odds else ""), callback_data=f"A|{mid}|{oid}|{eid}")

    rows = []
    if p.best and p.best.odds:
        rows.append([Btn(f"⭐ Add best pick: {p.best.label.split(' (')[0]} @ {p.best.odds:.2f}",
                         callback_data=f"A|{p.best.market_id}|{p.best.outcome_id}|{eid}")])
    groups = [[(MARKET_1X2, "1"), (MARKET_1X2, "2"), (MARKET_1X2, "3")],
              [(MARKET_OU, "12"), (MARKET_OU, "13")],
              [(MARKET_BTTS, "74"), (MARKET_BTTS, "76")],
              [(MARKET_DC, "9"), (MARKET_DC, "10"), (MARKET_DC, "11")]]
    for g in groups:
        row = [b(m, o) for m, o in g if p.event.odds(m, o, SELECTIONS[(m, o)][1])]
        if row:
            rows.append(row)
    rows.append([Btn("🧾 View slip", callback_data="SLIP")])
    return Kb(rows)


async def _send_prediction(update: Update, ctx, event_id: str, edit: bool) -> None:
    s = svc(ctx)
    event = await s.find_event(event_id)
    if not event:
        await _reply_or_edit(update, "That match is no longer listed (it may have started).", None, edit)
        return
    pred = await s.predict(event)
    if not pred:
        await _reply_or_edit(update, f"No 1X2 prices for {esc(event.name)} yet, so I can't estimate it.", None, edit)
        return
    # Send predictions as a new message so the browse list stays usable.
    await update.effective_message.reply_text(format_prediction(pred), parse_mode=ParseMode.HTML,
                                              reply_markup=prediction_keyboard(pred))


# ---------------------------------------------------------------- slip / booking
async def _render_slip(user_id: int, ctx) -> tuple[str, Kb | None]:
    s = svc(ctx)
    picks = s.storage.slip_get(user_id)
    if not picks:
        return "🧾 Your slip is empty. Add picks from a prediction.", None
    lines, total, rows = ["🧾 <b>Your slip</b>"], 1.0, []
    for i, p in enumerate(picks, 1):
        odds = p["odds"]
        try:  # refresh price from the (5-min cached) feed when available
            ev = await s.find_event(p["event_id"])
            live = ev.odds(p["market_id"], p["outcome_id"], p["specifier"]) if ev else None
            if live:
                odds = live
        except Exception:
            pass
        total *= odds or 1.0
        lines.append(f"{i}. {esc(p['match_name'])}\n    {esc(p['label'])} @ <b>{odds:.2f}</b>")
        rows.append([Btn(f"❌ Remove {i}", callback_data=f"R|{p['event_id']}")])
    lines.append(f"\n<b>Total odds: {total:.2f}</b> ({len(picks)} selection{'s' if len(picks) > 1 else ''})")
    rows.append([Btn("📒 Get booking code", callback_data="BOOK"), Btn("🗑 Clear", callback_data="CLR")])
    return "\n".join(lines), Kb(rows)


async def _book(user_id: int, ctx) -> dict:
    s = svc(ctx)
    picks = s.storage.slip_get(user_id)
    if not picks:
        return {"text": "🧾 Your slip is empty. Add picks from a prediction first."}
    sels = [Selection(p["event_id"], p["market_id"], p["specifier"], p["outcome_id"]) for p in picks]
    try:
        code = await s.bookmaker.book(sels)
    except BookingError as exc:
        return {"text": f"Couldn't create a booking code: {esc(str(exc))}\nA match may have started or "
                        "a market closed: check /slip and remove it.", "parse_mode": ParseMode.HTML}
    total = 1.0
    for p in picks:
        total *= p["odds"] or 1.0
    await _record(ctx, [{**p, "pick": p["label"], "market": _mkt_name(p["market_id"]),
                         "match": p["match_name"]} for p in picks], code, "chat-slip", total_odds=total)
    return {"text": (f"📒 <b>Booking code: <code>{esc(code)}</code></b>\n{len(picks)} selection(s), "
                     f"approx. total odds {total:.2f}.\nTap below to open it in the {esc(s.bookmaker.name)} app (or the website if "
                     f"the app isn't installed), or copy the code into Load Code.\n\n{RG_NOTE}"),
            "parse_mode": ParseMode.HTML, "disable_web_page_preview": True,
            "reply_markup": open_code_kb(s.bookmaker, code)}


def _mkt_name(mid: str) -> str:
    return {MARKET_1X2: "1X2", MARKET_OU: "Over/Under", MARKET_DC: "Double Chance", MARKET_BTTS: "GG/NG"}.get(mid, "")


# ---------------------------------------------------------------- history / rollover
def _code_cell(code: str | None, live: bool) -> str:
    """A code in a list: monospace (tap to copy); running slips also get the open-in-SportyBet-app link."""
    if not code:
        return "<code>—</code>"
    return code_html(code, "📲 open") if live else f"<code>{esc(code)}</code>"


def _hist_line(r: dict) -> str:
    d = r["data"]
    legs = d.get("legs") or []
    won = sum(1 for l in legs if l.get("status") == "won")
    when = (r.get("created_at") or "")[:16].replace("T", " ")
    try:
        from datetime import datetime
        when = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(TZ).strftime("%d %b %H:%M")
    except Exception:
        pass
    tag = f"Rollover d{d['rollover_day']}" if d.get("rollover_id") else (d.get("label") or d.get("tier") or "Slip")
    early = [l["early"]["min"] for l in legs if (l.get("early") or {}).get("min")]
    if early and r["status"] == "won":
        tag += f" · won early ({max(early)}')"
    return (f"{({'replaced': '♻️'}).get(r['status']) or STATUS_ICON.get(r['status'], '⏳')} <b>{esc(tag)}</b> · {_code_cell(d.get('code'), r['status'] == 'pending')} · "
            f"<b>{d.get('total_odds') or 0:.2f}</b> · {won}/{len(legs)} legs won · <i>{esc(when)}</i>")


async def history_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tr = getattr(svc(ctx), "tracker", None)
    try:
        rows = await tr.history.list("slip", None, limit=12) if tr else []
    except Exception:
        await update.effective_message.reply_text("The history store didn't answer just now. Try again shortly.")
        return
    if not rows:
        await update.effective_message.reply_text("📚 No booked codes yet. Book one from /scan, /slip or the app.")
        return
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("won", "lost", "pending", "check")}
    lines = [f"📚 <b>History</b> · last {len(rows)} codes", f"✅ {counts['won']} won · ❌ {counts['lost']} lost · "
             f"⏳ {counts['pending']} running" + (f" · ❔ {counts['check']} to check" if counts["check"] else ""), RULE]
    lines += [_hist_line(r) for r in rows]
    live = next((r for r in rows if r["status"] == "pending"), None)
    if live:
        lines += [RULE, record_text(live, "⏳ <b>Latest running slip</b>")]
    url = app_url()
    kb = Kb([[Btn("📈 Open History in the app", web_app=WebAppInfo(url + "#history"))]]) if url else None
    await update.effective_message.reply_text("\n".join(lines)[:4096], parse_mode=ParseMode.HTML, reply_markup=kb,
                                              disable_web_page_preview=True)


def _ro_text(rec: dict | None) -> str:
    if not rec:
        return ("🔁 <b>Rollover</b>\nNo plan running. Pick a daily target (2, 3, 4 or 5 odds) and 3–15 days, e.g.\n"
                "<code>/rollover 2 7</code> or with a starting stake <code>/rollover 3 5 1000</code>.\n"
                "Each morning (~09:00) I build that day's slip from top leagues only, stats-confirmed legs ≥80% est., "
                "no under selections (add <code>unders</code> to allow them, e.g. <code>/rollover 3 7 unders</code>), "
                "as close to the target as possible. A win moves the plan to the next day; a loss ends it. "
                "If nothing qualifies, I'll say so and suggest skipping the day.\n<i>Estimates, not guarantees. 18+.</i>")
    d = rec["data"]
    icon = {"active": "▶️", "completed": "🏆", "busted": "💔", "stopped": "⏹"}.get(rec["status"], "")
    lines = [f"🔁 <b>Rollover</b> {icon} {esc(rec['status'])} · target <b>{d['target']:g}</b>/day · "
             f"day <b>{min(d['current_day'], d['days'])}/{d['days']}</b>"
             + (" · unders allowed" if d.get("allow_unders") else " · no unders")]
    if d.get("stake"):
        lines.append(f"Stake {d['stake']:,.0f} → now <b>{(d.get('value') or 0):,.0f}</b>")
    lines.append(RULE)
    for x in d.get("log", [])[-15:]:
        st = {"no_slip": "⏸", "skipped": "⏭"}.get(x["status"], STATUS_ICON.get(x["status"], ""))
        lines.append(f"{st} Day {x['day']} · {esc(x['date'])}" + (f" · {_code_cell(x['code'], x.get('status') == 'pending')}" if x.get("code") else "")
                     + (f" · {x['total_odds']:.2f}" if x.get("total_odds") else "")
                     + (f" · won early ({x['won_early']}')" if x.get("won_early") else "")
                     + (" · replaced" if x["status"] == "replaced" else "")
                     + (f" · bankroll {x['bankroll']:,.0f}" if x.get("bankroll") else "")
                     + (f" · <i>{esc(x['note'][:90])}</i>" if x.get("note") and x["status"] in ("no_slip", "skipped") else ""))
    if not d.get("log"):
        lines.append("No day generated yet: the first slip comes at ~09:00.")
    lines.append("<i>Estimates, not guarantees. 18+.</i>")
    return "\n".join(lines)


def _ro_kb(rec: dict | None) -> Kb:
    if not rec or rec["status"] != "active":
        rows = [[Btn(f"Start {t}× · 5 days", callback_data=f"RO|start|{t}|5") for t in (2, 3)],
                [Btn(f"Start {t}× · 5 days", callback_data=f"RO|start|{t}|5") for t in (4, 5)]]
        if rec:
            rows.insert(0, [Btn("🎟 Tickets of this plan", callback_data=f"RO|tix|{rec['id']}")])
        return Kb(rows)
    rows = [[Btn("🎟 Tickets", callback_data=f"RO|tix|{rec['id']}"), Btn("⚡ Build today's slip now", callback_data="RO|now")]]
    last = next((x for x in reversed(rec["data"].get("log") or []) if x.get("slip_id")), None)
    if last and last.get("status") == "pending":
        rows.append([Btn("🔄 Refresh picks", callback_data=f"RF|{last['slip_id']}"),
                     Btn("🔀 Different picks", callback_data=f"RF|{last['slip_id']}|x")])
    rows.append([Btn("⏭ Skip today", callback_data="RO|skip"), Btn("⏹ Stop plan", callback_data="RO|stop")])
    return Kb(rows)


TICKET_ICON = {**STATUS_ICON, "live": "🔴", "replaced": "♻️", "no_slip": "⏸", "skipped": "⏭"}


def _tickets_msgs(plan: dict, tickets: list[dict]) -> list[str]:
    """Day-by-day tickets of one rollover plan (Telegram HTML, split under the 4096 limit)."""
    d = plan["data"]
    head = [f"🎟 <b>Rollover tickets</b> · {d['target']:g}× a day · {d['days']} days · {esc(plan['status'])}"]
    if d.get("stake"):
        head.append(f"Bankroll {d['stake']:,.0f} → now <b>{(d.get('value') or 0):,.0f}</b>"
                    + ("" if plan["status"] != "busted" else " (plan busted: 0)"))
    parts = ["\n".join(head), RULE]
    if not tickets:
        parts.append("No tickets yet.")
    for t in tickets:
        st = t["status"]
        ln = (f"{TICKET_ICON.get(st, '')} <b>Day {t['day']}</b> · {esc(t['date'])} · {esc(st.replace('_', ' '))}"
              + (f" · won early ({t['won_early']}')" if t.get("won_early") else "")
              + (f" · {_code_cell(t['code'], st in ('pending', 'live'))}" if t.get("code") else "")
              + (f" · <b>{(t.get('effective_odds') or t['total_odds'] or 0):.2f}</b>" if t.get("total_odds") else "")
              + (f" · {t['n_legs']} legs" if t.get("n_legs") else ""))
        if st == "replaced":
            ln += "\n    <i>Replaced by a refresh: not a loss" + (", that code isn't tracked" if t.get("code") else "") + "</i>"
        elif st in ("no_slip", "skipped"):
            ln += f"\n    <i>{esc((t.get('note') or '')[:120])}</i>"
        if t.get("stake") is not None and st not in ("replaced", "no_slip", "skipped"):
            ln += (f"\n    Stake {t['stake']:,.0f} → {t['expected_return']:,.0f}"
                   + (f" · bankroll {t['bankroll']:,.0f}" if st in ("won", "lost", "void") else ""))
        for l in t.get("legs") or []:
            ic = STATUS_ICON.get(l.get("status") or "pending", "⏳")
            ln += (f"\n    {ic} {esc(l.get('home', ''))} v {esc(l.get('away', ''))} · {esc(l.get('pick', ''))} "
                   f"@ {(l.get('odds') or 0):.2f}"
                   + (f" · {esc(l['score'])}" if l.get("score") else "")
                   + (f" · <i>{esc(l['note'][:40])}</i>" if l.get("note") and l.get("status") != "pending" else ""))
        parts.append(ln)
    parts.append("<i>Estimates, not guarantees. 18+.</i>")
    msgs, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > 3900 and cur:
            msgs.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + p
    return msgs + ([cur] if cur else [])


async def _send_tickets(msg, ctx, plan_id: str | None) -> None:
    tr = svc(ctx).tracker
    plan = await tr.history.get(plan_id) if plan_id else (await tr.active_rollover()
                                                           or next(iter(await tr.rollovers(1)), None))
    if not plan or plan.get("kind") != "rollover":
        await msg.reply_text("No rollover plan yet. Start one with /rollover 2 7.")
        return
    tickets = await tr.rollover_tickets(plan)
    msgs = _tickets_msgs(plan, tickets)
    url = app_url()
    for i, m in enumerate(msgs):
        kb = None
        if i == len(msgs) - 1:
            rows = []
            cur = next((t for t in reversed(tickets) if t.get("can_refresh")), None)
            if cur:
                rows.append([Btn("🔄 Refresh picks", callback_data=f"RF|{cur['slip_id']}"),
                             Btn("🔀 Different picks", callback_data=f"RF|{cur['slip_id']}|x")])
            past = [r for r in await tr.rollovers(6) if r["id"] != plan["id"]][:3]
            for r in past:
                rows.append([Btn(f"🎟 {r['data']['target']:g}× · {r['data']['days']} days · {r['status']} "
                                 f"({(r.get('created_at') or '')[:10]})", callback_data=f"RO|tix|{r['id']}")])
            if url:
                rows.append([Btn("📱 Open Rollover in the app", web_app=WebAppInfo(url + "#rollover"))])
            kb = Kb(rows) if rows else None
        await msg.reply_text(m, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def _ro_refresh(q, ctx, slip_id: str, exclude_prev: bool) -> None:
    from fmt import leg_lines
    from tracker import day_markup
    tr = svc(ctx).tracker
    await q.answer("Refreshing picks… ~30–60 s")
    note = await q.message.reply_text("🔄 Re-reading today's top-league prices for this rollover day… ~30–60 s")
    try:
        out = await tr.refresh_day(slip_id, exclude_prev=exclude_prev)
    except ValueError as exc:
        await note.edit_text(f"⚠️ {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        log.warning("rollover refresh failed: %s", exc, exc_info=True)
        await note.edit_text("The refresh failed (scanner or history store didn't answer). Your slip is unchanged.")
        return
    if out["status"] in ("none", "same"):
        kb = None
        if out["status"] == "same":
            kb = Kb([[Btn("🔀 Different picks", callback_data=f"RF|{slip_id}|x")]])
        await note.edit_text(("🔄 " if out["status"] == "same" else "⚠️ ") + esc(out["note"]), parse_mode=ParseMode.HTML,
                             reply_markup=kb)
        return
    res = out["slip"]
    body = "\n".join([leg_lines(l, i, why=False) for i, l in enumerate(res["legs"], 1)])
    tail = (f"{RULE}\n<b>Total odds <code>{res['total_odds']:.2f}</code></b> · est. hit chance <b>{res['est_prob']:.0%}</b>"
            "\n<i>Same rules: top leagues, every leg ≥80% est. Estimates, not guarantees. 18+.</i>")
    if out["status"] == "preview":
        _remember(ctx, note, {"section": "rollover_preview", "token": out["token"], "slip_id": slip_id,
                              "code": out.get("old_code"), "legs": res["legs"]})
        await note.edit_text(
            f"🔄 <b>New picks for today</b> (preview)\nYour current slip is booked as <code>{esc(out['old_code'])}</code>. "
            "Confirm to book a <b>new code</b>; the old one is marked <i>replaced</i> (not counted as a loss) and "
            f"stops being tracked.\n{RULE}\n{body}\n{tail}", parse_mode=ParseMode.HTML,
            reply_markup=Kb([[Btn("✅ Confirm: book new code", callback_data=f"RC|{out['token']}")],
                             [Btn("Keep current slip", callback_data="NOOP")]]))
        return
    rec = out["record"]
    _remember(ctx, note, {"section": "rollover_day", "slip_id": rec["id"], "legs": res["legs"]})
    await note.edit_text(f"🔄 <b>Picks refreshed</b> · Rollover day {rec['data']['rollover_day']}\n"
                         f"The previous slip is marked replaced.\n{RULE}\n{body}\n{tail}", parse_mode=ParseMode.HTML,
                         reply_markup=day_markup(rec["id"], bool(rec["data"].get("code")), rec["data"].get("code")))


async def _ro_refresh_confirm(q, ctx, token: str) -> None:
    from tracker import day_markup
    tr = svc(ctx).tracker
    await q.answer("Booking the new slip…")
    try:
        try:
            out = await tr.refresh_confirm(token)
        except ValueError as exc:
            # preview lost (redeploy / >15 min): rebuild it from the slip stored with this message, if recent
            ed = getattr(svc(ctx), "editor", None)
            ref = await ed.lookup(q.message.chat_id, q.message.message_id) if ed and "expired" in str(exc) else None
            import time as _t
            if not ref or ref.get("section") != "rollover_preview" or not ref.get("legs") or not ref.get("slip_id") \
                    or _t.time() - ref.get("ts", 0) > 3600:
                raise
            from slipedit import combined, total_odds
            legs = ref["legs"]
            tok = tr.edit_preview(ref["slip_id"], {"tier": "rollover", "label": "Rollover", "legs": legs,
                                                   "total_odds": round(total_odds(legs), 2),
                                                   "est_prob": round(combined(legs), 4), "reached": True})
            out = await tr.refresh_confirm(tok)
    except (ValueError, BookingError) as exc:
        await q.message.reply_text(f"⚠️ {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    rec = out["record"]
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    sent = await q.message.reply_text(
        f"📒 <b>New code: <code>{esc(out['code'])}</code></b> · Rollover day {rec['data']['rollover_day']} · "
        f"{len(rec['data']['legs'])} legs · total odds {rec['data']['total_odds']:.2f}\n"
        f"<code>{esc(out.get('old_code') or '')}</code> is marked replaced (not a loss). Tracking the new one.",
        parse_mode=ParseMode.HTML, reply_markup=day_markup(rec["id"], True, out["code"]))
    _remember(ctx, sent, {"section": "rollover_day", "slip_id": rec["id"], "legs": rec["data"]["legs"]})


async def rollover_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    tr = svc(ctx).tracker
    args = [a.lower() for a in (ctx.args or [])]
    msg = update.effective_message
    try:
        if not args:
            rec = await tr.active_rollover() or next(iter(await tr.rollovers(1)), None)
            await msg.reply_text(_ro_text(rec), parse_mode=ParseMode.HTML, reply_markup=_ro_kb(rec))
            return
        if args[0] in ("tickets", "view", "ticket", "history"):
            await _send_tickets(msg, ctx, None)
            return
        if args[0] in ("stop", "skip", "now", "won", "lost"):
            await _ro_action(msg, ctx, args[0])
            return
        allow_unders = any(a in ("unders", "allow-unders", "+unders", "with-unders") for a in args)
        args = [a for a in args if a not in ("unders", "allow-unders", "+unders", "with-unders", "nounders",
                                             "no-unders", "-unders")]
        target, days = float(args[0].rstrip("x×")), int(args[1]) if len(args) > 1 else 5
        stake = float(args[2].replace(",", "")) if len(args) > 2 else None
    except (ValueError, IndexError):
        await msg.reply_text("Usage: /rollover <target 2|3|4|5> <days 3–15> [stake] [unders] · /rollover tickets|stop|skip|now")
        return
    await _ro_start(msg, ctx, target, days, stake, allow_unders)


async def _ro_start(msg, ctx, target: float, days: int, stake: float | None, allow_unders: bool = False) -> None:
    tr = svc(ctx).tracker
    note = await msg.reply_text(f"🔁 Starting a {target:g}× · {days}-day rollover and scanning today's top-league "
                                "matches… ~30–60 s")
    try:
        rec, day = await tr.create_rollover(target, days, stake, allow_unders=allow_unders)
    except ValueError as exc:
        await note.edit_text(f"⚠️ {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        log.warning("rollover start failed: %s", exc, exc_info=True)
        await note.edit_text("The rollover couldn't start (scanner or history store didn't answer). Try again shortly.")
        return
    tail = "" if day else "\nToday's window has passed: the first slip comes tomorrow at ~09:00."
    await note.edit_text(_ro_text(await tr.history.get(rec["id"]) or rec) + tail, parse_mode=ParseMode.HTML,
                         reply_markup=_ro_kb(rec))


async def _ro_action(msg, ctx, act: str) -> None:
    tr = svc(ctx).tracker
    if act == "stop":
        rec = await tr.stop_rollover()
        await msg.reply_text("⏹ Rollover stopped." if rec else "No rollover is running.")
    elif act == "skip":
        rec = await tr.skip_day()
        await msg.reply_text("⏭ Today skipped. Next slip tomorrow at ~09:00." if rec else "No rollover is running.")
    elif act in ("won", "lost"):
        rec = await tr.mark_day(act)
        await msg.reply_text(_ro_text(rec) if rec else "Nothing to mark.", parse_mode=ParseMode.HTML)
    elif act == "now":
        rec = await tr.active_rollover()
        if not rec:
            await msg.reply_text("No rollover is running. Start one with /rollover 2 7.")
            return
        if rec["data"].get("last_gen_date") == tr._today() and any(
                x["date"] == tr._today() and x["status"] != "no_slip" for x in rec["data"]["log"]):
            await msg.reply_text("Today's slip was already built: see /rollover or /history.")
            return
        note = await msg.reply_text("🔎 Building today's rollover slip… ~30–60 s")
        entry = await tr.generate_day(rec, force=True)
        if entry.get("status") == "waiting":
            await note.edit_text("⏳ " + entry["note"])
        else:
            await note.delete()


# ---------------------------------------------------------------- callbacks
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    parts = data.split("|")
    kind = parts[0]
    s = svc(ctx)
    uid = update.effective_user.id
    if kind == "NOOP":
        await q.answer()
    elif kind == "LL":
        await q.answer()
        await _send_leagues(update, ctx, parts[1], int(parts[2]), edit=True)
    elif kind == "LM":
        await q.answer()
        await _send_matches(update, ctx, parts[1], parts[2], int(parts[3]))
    elif kind == "P":
        await q.answer("Analysing…")
        await _send_prediction(update, ctx, parts[1], edit=False)
    elif kind == "A":
        mid, oid, eid = parts[1], parts[2], "|".join(parts[3:])
        ev = await s.find_event(eid)
        label, spec = SELECTIONS.get((mid, oid), (None, ""))
        odds = ev.odds(mid, oid, spec) if ev else None
        if not ev or not label or not odds:
            await q.answer("That price is no longer available.", show_alert=True)
            return
        replaced = s.storage.slip_add(uid, eid, mid, spec, oid, label, odds, ev.name)
        n = len(s.storage.slip_get(uid))
        await q.answer(f"{'Replaced' if replaced else 'Added'}: {label} @ {odds:.2f} · {n} on slip")
    elif kind == "R":
        s.storage.slip_remove(uid, "|".join(parts[1:]))
        await q.answer("Removed")
        text, kb = await _render_slip(uid, ctx)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif kind == "SLIP":
        await q.answer()
        text, kb = await _render_slip(uid, ctx)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif kind == "BOOK":
        await q.answer("Booking…")
        await q.message.reply_text(**await _book(uid, ctx))
    elif kind == "SB":
        ai = getattr(s, "ai", None)
        entry = ai.slips.get(parts[1]) if ai else None
        if not entry:
            await q.answer("That slip expired. Run /scan again.", show_alert=True)
            return
        await q.answer("Booking…")
        legs = entry["slip"]["legs"]
        sels = [Selection(l["event_id"], l["market_id"], l["specifier"], l["outcome_id"]) for l in legs]
        try:
            code = await s.bookmaker.book(sels)
        except BookingError as exc:
            await q.message.reply_text(f"Couldn't create a booking code: {esc(str(exc))}", parse_mode=ParseMode.HTML)
            return
        sl = entry["slip"]
        await _record(ctx, legs, code, "chat-scan", tier=sl.get("tier", "custom"), label=sl.get("label", ""),
                      total_odds=sl.get("total_odds"), est_prob=sl.get("est_prob"), target=sl.get("target"))
        await q.message.reply_text(
            f"📒 <b>Booking code: <code>{esc(code)}</code></b>\n{len(sels)} legs · total odds "
            f"{entry['slip']['total_odds']:.2f} · est. hit chance {entry['slip']['est_prob']:.0%}\n"
            f"Saved to /history: I'll ping you when it lands.\n\n{RG_NOTE}",
            parse_mode=ParseMode.HTML, reply_markup=open_code_kb(s.bookmaker, code))
    elif kind == "RF":
        await _ro_refresh(q, ctx, parts[1], len(parts) > 2 and parts[2] == "x")
    elif kind == "RC":
        await _ro_refresh_confirm(q, ctx, parts[1])
    elif kind == "SR":
        await _scan_refresh(q, ctx, parts[1], len(parts) > 2 and parts[2] == "x")
    elif kind == "RO":
        await q.answer()
        if parts[1] == "tix":
            await _send_tickets(q.message, ctx, parts[2] if len(parts) > 2 else None)
        elif parts[1] == "start":
            await _ro_start(q.message, ctx, float(parts[2]), int(parts[3]), None)
        else:
            await _ro_action(q.message, ctx, parts[1])
    elif kind == "HB":
        await q.answer("Booking…")
        try:
            rec = await s.tracker.book_record(parts[1])
        except (BookingError, ValueError) as exc:
            await q.message.reply_text(f"Couldn't create a booking code: {esc(str(exc))}", parse_mode=ParseMode.HTML)
            return
        d = rec["data"]
        await q.message.reply_text(f"📒 <b>Booking code: <code>{esc(d['code'])}</code></b> · {len(d['legs'])} legs · "
                                   f"total odds {d['total_odds']:.2f}", parse_mode=ParseMode.HTML,
                                   reply_markup=open_code_kb(s.bookmaker, d["code"]))
    elif kind in ("AZR", "AZC", "AZN"):
        await _analyzer_callback(q, ctx, kind, parts[1] if len(parts) > 1 else "")
    elif kind == "CLR":
        s.storage.slip_clear(uid)
        await q.answer("Slip cleared")
        await q.edit_message_text("🗑 Slip cleared.")
    else:
        await q.answer()


def _az_src(res: dict) -> str:
    return f"<code>{esc(res['code'])}</code>" if res.get("code") else "your screenshot"


async def _analyzer_callback(q, ctx, kind: str, aid: str) -> None:
    s = svc(ctx)
    an = getattr(s, "analyzer", None)
    res = an.get(aid) if an else None
    if not res:
        await q.answer("That analysis expired. Send the code again.", show_alert=True)
        return
    if kind == "AZN":
        await q.answer("Thinking…")
        try:
            out = await an.narrative(aid)
        except Exception as exc:
            await q.message.reply_text(f"No AI take right now ({esc(str(exc)[:100])}).", parse_mode=ParseMode.HTML)
            return
        tag = "🧠 <b>AI take</b>" if out["source"] == "ai" else "🧠 <b>Summary</b>"
        await q.message.reply_text(f"{tag} · {_az_src(res)}\n{esc(out['text'])}",
                                   parse_mode=ParseMode.HTML)
        return
    imp = (res.get("suggestions") or {}).get("improved")
    if not imp:
        await q.answer("No improved slip for this code.", show_alert=True)
        return
    if kind == "AZR":            # step 1: show exactly what would be booked
        await q.answer()
        lines = [f"♻️ <b>Improved slip</b> from {_az_src(res)}"]
        lines += [f"• {esc(c)}" for c in imp["changes"]]
        lines.append(RULE)
        for i, l in enumerate(imp["legs"], 1):
            lines.append(f"{i}. {esc(l['match'].replace(' vs ', ' v '))}: <b>{esc(l['pick'])}</b> @ "
                         f"<code>{l['odds']:.2f}</code>" + (f" · est {l['est']:.0%}" if l.get("est") is not None else ""))
        lines.append(f"Total odds <b>{imp['total_odds']:.2f}</b> · est. hit chance <b>{imp['chance']:.0%}</b>")
        lines.append("<i>Tap Confirm to create the new booking code. Estimates, not guarantees.</i>")
        await q.message.reply_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML,
                                   reply_markup=Kb([[Btn("✅ Confirm rebook", callback_data=f"AZC|{aid}")]]))
        return
    await q.answer("Booking…")      # AZC: explicit confirmation tap
    try:
        out = await an.rebook(aid, save=is_owner(ctx, q.from_user.id))
    except (BookingError, ValueError) as exc:
        await q.message.reply_text(f"Couldn't rebook: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    await q.message.reply_text(
        f"📒 <b>New booking code: <code>{esc(out['code'])}</code></b>\n{out['legs']} legs · total odds "
        f"{out['total_odds']:.2f} · est. hit chance {out['chance']:.0%}"
        + ("\nSaved to /history: I'll ping you when it lands." if out.get("history_id") else "")
        + f"\n\n{RG_NOTE}", parse_mode=ParseMode.HTML,
        reply_markup=open_code_kb(s.bookmaker, out["code"]))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handler error: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong on my side. Please try again.")
        except Exception:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Open the app + help"), BotCommand("scan", "Scan markets: /scan safe 5"),
        BotCommand("ai", "Ask the AI analyst"), BotCommand("analyze", "Analyze a booking code: /analyze CODE"), BotCommand("today", "Today's matches by league"),
        BotCommand("rollover", "Rollover plan: /rollover 2 7"), BotCommand("history", "Booked codes + results"),
        BotCommand("slip", "Your slip and total odds"), BotCommand("help", "How it works"),
    ])
    url = app_url()
    if url:
        button = MenuButtonWebApp(text="Open", web_app=WebAppInfo(url))
        try:
            await app.bot.set_chat_menu_button(menu_button=button)  # default for all private chats
            owner = config.OWNER_TELEGRAM_ID or app.bot_data["services"].storage.get_owner()
            if owner:
                await app.bot.set_chat_menu_button(chat_id=owner, menu_button=button)
            log.info("Chat menu button set to the Mini App")
        except Exception as exc:
            log.warning("Could not set chat menu button: %s", exc)


def register(app: Application) -> None:
    app.add_handler(TypeHandler(Update, owner_gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("upcoming", upcoming))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("slip", slip_cmd))
    app.add_handler(CommandHandler("book", book_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("rollover", rollover_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    app.add_error_handler(on_error)
