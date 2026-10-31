"""Telegram handlers: owner lock, browsing, predictions, bet slip and booking codes."""
from __future__ import annotations

import html
import logging
import math

from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

from bookmakers import MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SELECTIONS, BookingError, Selection
from predictor import Prediction
from services import TZ, Services

log = logging.getLogger(__name__)
PAGE = 8

RG_NOTE = ("⚠️ <i>Predictions are statistical estimates, never guarantees. Bet only what you can afford "
           "to lose, 18+ only. If gambling stops being fun, take a break.</i>")
HELP = (
    "<b>Commands</b>\n"
    "/today – today's matches by league\n"
    "/upcoming – next 3 days by league\n"
    "/predict Team A vs Team B – analyse a match\n"
    "/slip – your picks and total odds\n"
    "/book – get a SportyBet booking code\n"
    "/clear – empty the slip"
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
        raise ApplicationHandlerStop  # silently ignore everyone else


# ---------------------------------------------------------------- commands
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 <b>Private football picks bot</b>\nOdds and fixtures from SportyBet NG, estimates from a "
        "Poisson goals model blended with the market.\n\n" + HELP + "\n\n" + RG_NOTE,
        parse_mode=ParseMode.HTML)


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


async def free_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if " vs " in text.lower() or " v " in text.lower():
        await _predict_query(update, ctx, text)
    else:
        await update.effective_message.reply_text("Try /predict Team A vs Team B, or /today.")


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
    url = s.bookmaker.load_code_url
    total = 1.0
    for p in picks:
        total *= p["odds"] or 1.0
    return {"text": (f"📒 <b>Booking code: <code>{esc(code)}</code></b>\n{len(picks)} selection(s), "
                     f"approx. total odds {total:.2f}.\nLoad it on {esc(s.bookmaker.name)}: {url}\n\n{RG_NOTE}"),
            "parse_mode": ParseMode.HTML, "disable_web_page_preview": True,
            "reply_markup": Kb([[Btn("Open SportyBet code hub", url=url)]])}


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
    elif kind == "CLR":
        s.storage.slip_clear(uid)
        await q.answer("Slip cleared")
        await q.edit_message_text("🗑 Slip cleared.")
    else:
        await q.answer()


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handler error: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong on my side. Please try again.")
        except Exception:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("today", "Today's matches by league"), BotCommand("upcoming", "Next 3 days"),
        BotCommand("predict", "Predict: Team A vs Team B"), BotCommand("slip", "Show bet slip"),
        BotCommand("book", "Get SportyBet booking code"), BotCommand("clear", "Clear slip"),
        BotCommand("start", "Help"),
    ])


def register(app: Application) -> None:
    app.add_handler(TypeHandler(Update, owner_gate), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("upcoming", upcoming))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("slip", slip_cmd))
    app.add_handler(CommandHandler("book", book_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    app.add_error_handler(on_error)
