"""5-minute draws as ONE booked SportyBet code (user rule 2026-11-06: "I just need the code ... Only 50 per code ...
It should bring new teams when I refresh", no German leagues).

book_draw5(services, owner, period, text, refresh):
    1. the ranked finder result for the window (draw5.Draw5Finder; German leagues already left out there)
    2. its open_pool: every pick whose 5-minute market is OPEN on SportyBet (booking refuses suspended outcomes),
       top leagues first by est, then wider leagues by est (widened only to reach 50)
    3. first request: the top 50 of the pool. Refresh: the top 50 that were in NO earlier 5-min-draw code of this owner
       for this window (used event ids persisted in the history store, kind 'd5used', reset when the window rolls over)
    4. books them as one code (POST /orders/share through the relay), saves it to History as a normal booked slip
       (label "5-min draw Weekend" etc.), and returns a short result for the chat / Mini App.
When SportyBet has the market suspended on every listed match, nothing is booked and the result says so plainly
(status "closed"), with when it usually reopens (REOPEN_HINT, measured) and when the bot last saw it open.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta, timezone

import draw5
from bookmakers import BookingError, Selection, sportybet_share_url

log = logging.getLogger(__name__)

CODE_SIZE = draw5.CODE_SIZE
USED_KIND = "d5used"
META_KIND = "d5meta"
META_ID = "d5meta_market900069"

# What we measured (relay snapshots, work/draw5_market_status.log): on Thu 05 Nov ~22:25 WAT the 5-minute line was open
# on the weekend matches; from ~22:40 WAT SportyBet suspended the short lines (minute=5 to minute=30) on every listed
# match, while minute=35 to 85 stayed open on part of them, and the prices kept updating (feed live, betting closed).
# It was still closed at 05:5x and later on Fri 06 Nov, including on a match 4 h before kickoff, so it does not simply
# open near kickoff. Updated from later checks in the notes file.
REOPEN_HINT = ("SportyBet switched it off for every match at once on Thursday around 22:40 WAT (prices still move, bets "
               "just aren't taken) and it doesn't open by kickoff time, so there is no fixed hour I can promise.")
WATCH_EVERY_S = 1800      # background check of the market's open/closed state (one list read, ~4 relay pages)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def window_key(period: str, win: dict) -> str:
    """Identity of the window the used-ids belong to: the weekend's / midweek's last day; for the rolling full week, the
    Monday of the current week (WAT). A new key = a fresh start for Refresh."""
    if period == "week":
        loc = win["start"].astimezone(draw5.TZ)
        return "week:" + (loc - timedelta(days=loc.weekday())).strftime("%Y-%m-%d")
    return f"{period}:" + win["end"].astimezone(draw5.TZ).strftime("%Y-%m-%d")


class UsedStore:
    """Event ids already put in a 5-min-draw code, per owner + window. Persisted in the history store (Supabase in
    production, SQLite locally) as one record per owner+period; an in-memory copy is used when the store is down."""

    def __init__(self, history=None):
        self.history = history
        self._mem: dict[str, dict] = {}

    @staticmethod
    def rid(owner, period: str) -> str:
        return f"d5u_{owner}_{period}"

    async def load(self, owner, period: str, wkey: str) -> dict:
        rid = self.rid(owner, period)
        data = None
        if self.history is not None:
            try:
                rec = await self.history.get(rid)
                data = (rec or {}).get("data")
            except Exception as exc:
                log.warning("draw5 used-ids load failed: %s", exc.__class__.__name__)
                data = self._mem.get(rid)
        else:
            data = self._mem.get(rid)
        if not data or data.get("window") != wkey:
            return {}
        now = _now()
        out = {}
        for eid, ko in (data.get("events") or {}).items():
            try:
                if datetime.fromisoformat(ko) < now - timedelta(hours=3):
                    continue        # long kicked off: can't be booked again anyway
            except (TypeError, ValueError):
                pass
            out[eid] = ko
        return out

    async def add(self, owner, period: str, wkey: str, legs: list[dict], used: dict | None = None) -> None:
        rid = self.rid(owner, period)
        events = dict(used or {})
        for l in legs:
            events[l["event_id"]] = l.get("kickoff") or ""
        data = {"window": wkey, "owner": str(owner), "period": period, "events": events, "updated_ts": time.time()}
        self._mem[rid] = data
        if self.history is not None:
            try:
                await self.history.save({"id": rid, "kind": USED_KIND, "status": "active", "data": data})
            except Exception as exc:
                log.warning("draw5 used-ids save failed: %s", exc.__class__.__name__)


async def _meta(history) -> dict:
    if history is None:
        return {}
    try:
        rec = await history.get(META_ID)
        return (rec or {}).get("data") or {}
    except Exception:
        return {}


async def _mark_open(history, n_open: int) -> None:
    if history is None or not n_open:
        return
    try:
        meta = await _meta(history)
        meta.update(last_open=_now().isoformat(timespec="seconds"), open_count=n_open)
        await history.save({"id": META_ID, "kind": META_KIND, "status": "active", "data": meta})
    except Exception:
        pass


async def check_market(services) -> dict:
    """Read the upcoming list once (market 900069 only) and record open/closed transitions in the meta record, so the
    bot learns when SportyBet opens the 5-minute line. Returns {offered, open, state}."""
    evs = await services.bookmaker.list_events(72, market_ids=draw5.MARKET_ID)
    now = _now()
    offered = opened = 0
    open_h, closed_h = [], []
    for e in evs:
        outs = e.markets.get((draw5.MARKET_ID, draw5.SPEC))
        if not outs or e.start <= now or draw5.is_german(e):
            continue
        offered += 1
        h = round((e.start - now).total_seconds() / 3600, 1)
        if all(o.active for o in outs.values()) and len(outs) == 3:
            opened += 1
            open_h.append(h)
        else:
            closed_h.append(h)
    state = "open" if opened else ("closed" if offered else "none")
    history = getattr(getattr(services, "tracker", None), "history", None)
    if history is not None:
        meta = await _meta(history)
        at = now.isoformat(timespec="seconds")
        meta.update(last_check=at, last_state=state, last_open_count=opened, last_offered=offered)
        if opened:
            meta["last_open"] = at
            meta["open_count"] = opened
        trans = list(meta.get("transitions") or [])
        if not trans or trans[-1].get("state") != state:
            trans.append({"at": at, "state": state, "open": opened, "offered": offered,
                          "open_hours_to_ko": [min(open_h), max(open_h)] if open_h else None,
                          "closed_hours_to_ko": [min(closed_h), max(closed_h)] if closed_h else None})
            meta["transitions"] = trans[-40:]
        try:
            await history.save({"id": META_ID, "kind": META_KIND, "status": "active", "data": meta})
        except Exception:
            pass
    return {"offered": offered, "open": opened, "state": state}


async def watch(services) -> None:
    """Background loop (main.py): check_market every WATCH_EVERY_S. Never sends messages."""
    import asyncio
    await asyncio.sleep(90)
    while True:
        try:
            r = await check_market(services)
            log.info("draw5 market 900069 minute=5: %s (%d open of %d)", r["state"], r["open"], r["offered"])
        except Exception as exc:
            log.warning("draw5 market check failed: %s", exc.__class__.__name__)
        await asyncio.sleep(WATCH_EVERY_S)


def _fmt_seen(iso: str) -> str:
    try:
        return draw5.fmt_local(datetime.fromisoformat(iso)) + " WAT"
    except (TypeError, ValueError):
        return ""


def choose(res: dict, used: dict | set, refresh: bool, n: int = CODE_SIZE) -> list[dict]:
    """The legs for one code: the top n of the open pool; on Refresh only events not in an earlier code."""
    pool = res.get("open_pool") or []
    if refresh:
        pool = [p for p in pool if p["event_id"] not in used]
    out, seen = [], set()
    for p in pool:
        if p["event_id"] in seen or draw5.is_german(_Ev(p)):
            continue
        seen.add(p["event_id"])
        out.append(p)
        if len(out) >= n:
            break
    return out


class _Ev:       # is_german() on a pick dict (belt and braces: the finder already left German leagues out)
    def __init__(self, p: dict):
        self.category_id, self.tournament_id, self.category = p.get("category_id"), p.get("tournament_id"), \
            p.get("category")


def _product(legs: list[dict], key: str) -> float:
    x = 1.0
    for l in legs:
        x *= float(l.get(key) or 1.0)
    return x


def rules_text(res: dict, n: int, legs: list[dict], refresh: bool) -> str:
    wider = sum(1 for l in legs if l.get("wider"))
    scope = "your leagues only" if res.get("scope") == "filter" else \
        (f"top leagues + {wider} from other leagues to reach {CODE_SIZE}" if wider else "top leagues")
    return (f"{res.get('label', '')} · {n} selections · no German leagues · {scope} · "
            f"SportyBet '1X2 from 1 to 5 minute' = Draw" + (" · new matches only" if refresh else ""))


async def book_draw5(services, owner, period: str, text: str = "", refresh: bool = False, test: bool = False,
                     fresh: bool = False) -> dict:
    """Returns {status, period, label, rules, message, code?, url?, legs, n, total_odds, est_prob, history_id?,
    open_in_window, offered_in_window, used_before}. status: booked | closed | none | exhausted | error."""
    period = period if period in draw5.PERIODS else "weekend"
    finder = services.draw5
    tracker = getattr(services, "tracker", None)
    history = getattr(tracker, "history", None)
    used_store = getattr(services, "draw5_used", None)
    if used_store is None:
        used_store = UsedStore(history)
        try:
            services.draw5_used = used_store
        except Exception:
            pass
    plabel = draw5.PERIOD_LABEL[period]
    base = {"period": period, "period_label": plabel, "legs": [], "n": 0, "code": None, "url": None,
            "total_odds": 0, "est_prob": 0, "history_id": None, "refresh": refresh}
    try:
        res = await finder.find(period, text, fresh=fresh)
    except Exception as exc:
        log.warning("draw5 find failed: %s", exc, exc_info=True)
        return {**base, "status": "error", "label": plabel, "rules": "",
                "message": "SportyBet didn't answer the 5-minute draw search. Tap Try again in a minute."}
    base.update(label=res.get("label", plabel), offered_in_window=res.get("offered_in_window", 0),
                open_in_window=res.get("open_in_window", len(res.get("open_pool") or [])))
    win = {"start": datetime.fromisoformat(res["window_start"]), "end": datetime.fromisoformat(res["window_end"])} \
        if res.get("window_start") else draw5.period_window(period)
    wkey = window_key(period, win)
    used = {} if test else await used_store.load(owner, period, wkey)
    base["used_before"] = len(used)
    if base["open_in_window"]:
        await _mark_open(history, base["open_in_window"])
    if not base["offered_in_window"]:
        return {**base, "status": "none", "rules": "",
                "message": f"No match in the {plabel.lower()} window ({res.get('label', '')}) offers SportyBet's "
                           "5-minute market yet (German leagues left out). Try again later."}
    if not base["open_in_window"]:
        meta = await _meta(history)
        seen = _fmt_seen(meta.get("last_open", ""))
        return {**base, "status": "closed", "rules": "",
                "message": (f"SportyBet has the 5-minute market closed right now on all {base['offered_in_window']} "
                            f"matches that offer it in the {plabel.lower()} window, so there is no code to book yet. "
                            + REOPEN_HINT + (f" Last seen open by me: {seen}." if seen else "")
                            + " Tap Try again later.")}
    legs = choose(res, used, refresh)
    if not legs:
        return {**base, "status": "exhausted", "rules": "",
                "message": (f"No new matches left: all {base['open_in_window']} open 5-minute draw picks in the "
                            f"{plabel.lower()} window were already in your earlier codes. New ones appear as SportyBet "
                            "opens the market on more matches.")}
    code, err = None, ""
    for attempt in range(2):
        try:
            code = await services.bookmaker.book(
                [Selection(l["event_id"], l["market_id"], l["specifier"], l["outcome_id"]) for l in legs])
            break
        except BookingError as exc:
            err = str(exc)
            log.warning("draw5 booking failed (attempt %d): %s", attempt + 1, err)
            if attempt == 0:      # a market may have closed since the list was read: re-read it once, fresh
                try:
                    res = await finder.find(period, text, fresh=True)
                except Exception:
                    break
                legs = choose(res, used, refresh)
                if not legs:
                    break
    if not code:
        return {**base, "status": "error", "rules": "",
                "message": f"SportyBet didn't create the code: {err or 'the market closed while booking'}. "
                           "Tap Try again."}
    n = len(legs)
    tot, prob = round(_product(legs, "odds"), 2), _product(legs, "est")
    rules = rules_text(res, n, legs, refresh)
    label = f"5-min draw {plabel}"
    rid = None
    if tracker is not None:
        rec = await tracker.record(legs, code, "draw5", tier="custom", label=label, total_odds=tot, est_prob=prob,
                                   test=test)
        rid = rec["id"] if rec else None
    if not test:
        await used_store.add(owner, period, wkey, legs, used)
    short = ""
    if n < CODE_SIZE:
        short = (f"Only {n} matches have the 5-minute market open right now"
                 + (" that weren't in your earlier codes" if refresh else "")
                 + f" (all leagues checked except Germany), so this code has {n}, not {CODE_SIZE}.")
    return {**base, "status": "booked", "code": code, "url": sportybet_share_url(code), "legs": legs, "n": n,
            "total_odds": tot, "est_prob": prob, "history_id": rid, "rules": rules, "label": res.get("label", plabel),
            "code_label": label, "short_note": short, "wider": sum(1 for l in legs if l.get("wider")),
            "message": short}


# ------------------------------------------------------------------ output
def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def code_html(out: dict) -> str:
    """Short Telegram reply: Rules line, the code, total odds, the open link (or the plain reason)."""
    if out.get("status") != "booked":
        return _e(out.get("message") or "No code this time.")
    rows = [f"<b>Rules:</b> {_e(out['rules'])}",
            "",
            f"<b>SportyBet code: <code>{_e(out['code'])}</code></b>",
            f"{out['n']} selections · total odds <b>{out['total_odds']:,.2f}</b>",
            f"Open in SportyBet: {_e(out['url'])}"]
    if out.get("short_note"):
        rows.append(f"Note: {_e(out['short_note'])}")
    rows.append("<i>Saved to History. Estimates, not guarantees. 18+.</i>" if out.get("history_id")
                else "<i>Estimates, not guarantees. 18+.</i>")
    return "\n".join(rows)


def code_plain(out: dict) -> str:
    return html.unescape(draw5.html_to_plain(code_html(out)))


def code_file_text(out: dict) -> str:
    lines = [f"Rules: {out.get('rules', '')}", f"SportyBet code: {out.get('code')}  ({out.get('url')})",
             f"{out.get('n', 0)} selections · total odds {out.get('total_odds', 0):,.2f}", ""]
    if out.get("short_note"):
        lines += [f"Note: {out['short_note']}", ""]
    for i, p in enumerate(out.get("legs") or [], 1):
        lines.append(f"{i:>3}. {p['kickoff_local']} WAT · {p['match']} · {p['league']}"
                     + (" (other league, added to reach 50)" if p.get("wider") else ""))
        lines.append(f"     Draw (1-5 min) @{p['odds']:.2f} · est {p['est']:.1%} · market {p['p_market']:.1%}")
        if p.get("why"):
            lines.append(f"     {p['why']}")
    lines += ["", "Est. = chance the score is level after minute 5 (estimate, not a guarantee). 18+."]
    return "\n".join(lines)
