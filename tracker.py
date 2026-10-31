"""History recording, result tracking, owner pings and rollover plans.

- Every booked code (chat /scan + Book, /book, AI build_booking_code, Mini App book, rollover days) is saved as a
  'slip' record in the durable history (history.py).
- A background loop (every 5 min while anything is pending, 15 min otherwise) settles each started leg from the
  Sportradar result feed (settle.py). One final Telegram message per slip: 💥 when the whole slip lands, a short ❌
  when it loses (sent as soon as one leg loses), ❔ when the rest won but a market needs a manual look.
- Rollover plans: day N of M at a fixed daily target; each morning (≥ 09:00 Africa/Lagos) the day's slip is built
  from allowlisted top leagues (stats-confirmed legs ≥80% est.), booked, saved and pushed to the owner. A win
  advances the plan (stake compounds), a loss busts it; no qualifying slip = the day is skipped, not forced.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode

import config
from bookmakers import BookingError, Selection
from fmt import RULE, esc, leg_lines, record_text
from history import HistoryStore, new_id, now_iso, slip_record
from settle import ResultsClient, effective_odds, needs_stats, settle_leg, slip_status

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)
MORNING_HOUR = 9
LAST_GEN_HOUR = 20
ROLLOVER_TARGETS = (2.0, 3.0, 4.0, 5.0)


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class Tracker:
    def __init__(self, services, history: HistoryStore | None = None):
        self.s = services
        self.history = history or HistoryStore(services.storage)
        self.results = ResultsClient(services.bookmaker)
        self.bot = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_tick: str = ""
        self.last_tick_summary: dict = {}
        self.autobook = True

    # ---- lifecycle ---------------------------------------------------------
    def start(self, bot) -> None:
        self.bot = bot
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
        await self.results.aclose()
        await self.history.aclose()

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            busy = False
            try:
                busy = await self.tick()
            except Exception as exc:
                log.warning("tracker tick failed: %s", exc, exc_info=True)
            await asyncio.sleep(300 if busy else 900)

    def owner(self) -> int | None:
        return config.OWNER_TELEGRAM_ID or self.s.storage.get_owner()

    async def send(self, text: str, test: bool = False, markup=None) -> bool:
        if not self.bot or not self.owner():
            return False
        if test:
            text = "🧪 <b>TEST</b> (tracker check, not a real result)\n" + text
        try:
            await self.bot.send_message(self.owner(), text[:4096], parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=markup)
            return True
        except Exception as exc:
            log.warning("owner ping failed: %s", exc.__class__.__name__)
            return False

    # ---- recording -----------------------------------------------------------
    async def enrich(self, legs: list[dict]) -> list[dict]:
        """Fill teams / crest ids / kickoff / league for legs that only carry selection ids (chat slip, app slip)."""
        out = []
        for l in legs:
            l = dict(l)
            if not (l.get("home") and l.get("kickoff") and l.get("home_uid")):
                try:
                    e = await self.s.find_event(l["event_id"])
                except Exception:
                    e = None
                if e:
                    from scanner import uid_of
                    l.setdefault("home", e.home)
                    l.setdefault("away", e.away)
                    l["home"] = l.get("home") or e.home
                    l["away"] = l.get("away") or e.away
                    l["home_uid"] = l.get("home_uid") or uid_of(e.home_id)
                    l["away_uid"] = l.get("away_uid") or uid_of(e.away_id)
                    l["kickoff"] = l.get("kickoff") or e.start.isoformat()
                    l["league"] = l.get("league") or e.league_full
                    l["category"] = l.get("category") or e.category
                    l["match"] = l.get("match") or e.name
            out.append(l)
        return out

    async def record(self, legs: list[dict], code: str | None, source: str, tier: str = "custom", label: str = "",
                     total_odds: float | None = None, est_prob: float | None = None, test: bool = False,
                     rollover_id: str = "", rollover_day: int = 0, target: float | None = None) -> dict | None:
        try:
            legs = await self.enrich(legs)
            rec = slip_record(legs, code, source, tier, label, total_odds, est_prob, test, rollover_id,
                              rollover_day, target)
            ok = await self.history.save(rec)
            if ok:
                log.info("history: saved %s (%s, %d legs)", rec["id"], source, len(legs))
            return rec if ok else None
        except Exception as exc:
            log.warning("history record failed: %s", exc)
            return None

    # ---- settlement ------------------------------------------------------------
    async def tick(self, only_id: str | None = None) -> bool:
        """Settle pending slips and run rollover jobs. Returns True when something is still pending."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            self.last_tick = now.isoformat(timespec="seconds")
            pending = await self.history.list("slip", ["pending"], limit=200)
            if only_id:
                pending = [r for r in pending if r["id"] == only_id]
            cache: dict[str, object] = {}
            settled = 0
            for rec in pending:
                changed = await self._settle_record(rec, now, cache)
                settled += int(changed)
            busy = bool(await self.history.list("slip", ["pending"], limit=1))
            if not only_id:
                await self._rollover_jobs(now)
            self.last_tick_summary = {"pending": len(pending), "updated": settled, "matches_checked": len(cache)}
            return busy

    async def _settle_record(self, rec: dict, now: datetime, cache: dict) -> bool:
        d = rec["data"]
        legs = d.get("legs") or []
        changed = False
        for l in legs:
            if l.get("status") in ("won", "lost", "void", "check"):
                continue
            ko = _parse(l.get("kickoff", ""))
            if ko and now < ko + timedelta(minutes=2):
                continue
            key = (l["event_id"], needs_stats(l))
            if key not in cache:
                cache[key] = await self.results.result(l["event_id"], need_stats=needs_stats(l))
            res = cache[key]
            st, note = settle_leg(l, res)
            if st == "pending" and ko and now > ko + timedelta(hours=72):
                st, note = "check", "no result after 72 h: check SportyBet"
            score = res.score
            if res.ht and res.state == "ended":
                score += f" (HT {res.ht[0]}-{res.ht[1]})"
            if (st, note, score) != (l.get("status"), l.get("note"), l.get("score")):
                l.update(status=st, note=note, score=score)
                changed = True
        status = slip_status(legs)
        if status != "pending":
            rec["status"] = status
            d["settled_at"] = now_iso()
            d["effective_odds"] = effective_odds(legs)
            changed = True
        if changed:
            await self.history.save(rec)
        if status != "pending" and not d.get("notified"):
            d["notified"] = True
            await self.history.save(rec)            # mark first: never ping twice, even after a crash
            d["ping_ok"] = await self._notify(rec)
            await self.history.save(rec)
            if d.get("rollover_id"):
                await self._rollover_result(rec)
        return changed

    async def _notify(self, rec: dict) -> bool:
        d, st = rec["data"], rec["status"]
        name = f"{d.get('label') or 'Slip'}" + (f" · Rollover day {d['rollover_day']}" if d.get("rollover_id") else "")
        if st == "won":
            title = (f"💥💥 <b>BOOM! Your {esc(name)} slip landed!</b> 🎉\n"
                     f"Every leg came in at total odds <b>{d.get('effective_odds') or d['total_odds']:.2f}</b>.")
            return await self.send(record_text(rec, title) + f"\n{RULE}\n<i>Estimates, not guarantees — enjoy this one. 18+.</i>",
                                   test=d.get("test"))
        elif st == "lost":
            lost = [l for l in d["legs"] if l.get("status") == "lost"]
            l = lost[0] if lost else {}
            more = f" (+{len(lost) - 1} more)" if len(lost) > 1 else ""
            return await self.send(f"❌ <b>{esc(name)} slip lost</b>"
                            + (f" · <code>{esc(d['code'])}</code>" if d.get("code") else "")
                            + f"\n{esc(l.get('home', ''))} v {esc(l.get('away', ''))} · {esc(l.get('pick', ''))} "
                            f"({esc(l.get('score', ''))}){more}", test=d.get("test"))
        elif st == "check":
            n = sum(1 for l in d["legs"] if l.get("status") == "check")
            return await self.send(f"❔ <b>{esc(name)} slip</b>" + (f" <code>{esc(d['code'])}</code>" if d.get("code") else "")
                            + f": no leg lost, but {n} leg(s) use a market I can't settle automatically. "
                            "Check it on SportyBet.", test=d.get("test"))
        elif st == "void":
            return await self.send(f"➖ {esc(name)} slip: every match was cancelled or voided.", test=d.get("test"))
        return False

    # ---- rollover ----------------------------------------------------------------
    async def active_rollover(self) -> dict | None:
        rows = await self.history.list("rollover", ["active"], limit=1)
        return rows[0] if rows else None

    async def rollovers(self, limit: int = 10) -> list[dict]:
        return await self.history.list("rollover", None, limit=limit)

    async def create_rollover(self, target: float, days: int, stake: float | None = None,
                              autobook: bool = True, generate: bool = True, test: bool = False,
                              push: bool = True) -> tuple[dict, dict | None]:
        if target not in ROLLOVER_TARGETS:
            raise ValueError("Daily target must be 2, 3, 4 or 5.")
        if not 3 <= days <= 15:
            raise ValueError("Days must be between 3 and 15.")
        cur = await self.active_rollover()
        if cur:
            raise ValueError("A rollover is already running. Stop it first (/rollover stop).")
        rec = {"id": new_id("ro"), "kind": "rollover", "status": "active", "data": {
            "target": target, "days": days, "stake": stake, "value": stake, "current_day": 1,
            "log": [], "last_gen_date": "", "autobook": autobook, "test": test, "push": push, "started": now_iso()}}
        await self.history.save(rec)
        day = None
        if generate and datetime.now(TZ).hour < LAST_GEN_HOUR:
            day = await self.generate_day(rec, force=True, push=push)
        return rec, day

    async def stop_rollover(self, reason: str = "stopped") -> dict | None:
        rec = await self.active_rollover()
        if not rec:
            return None
        rec["status"] = "stopped"
        rec["data"]["ended"] = now_iso()
        rec["data"]["end_reason"] = reason
        await self.history.save(rec)
        return rec

    def _today(self) -> str:
        return datetime.now(TZ).strftime("%Y-%m-%d")

    async def generate_day(self, rec: dict, force: bool = False, push: bool = True) -> dict:
        """Build, (book), save and push today's slip for the plan. Returns the day log entry."""
        d = rec["data"]
        today = self._today()
        pend = [x for x in d["log"] if x.get("status") in ("pending", "check")]
        if pend:
            return {"status": "waiting", "note": "Yesterday's slip is still running; I'll build the next day after it settles."}
        res = await self.s.scanner.rollover_slip(float(d["target"]))
        n, m = d["current_day"], d["days"]
        entry = {"day": n, "date": today, "status": "no_slip", "note": res.get("note", ""), "slip_id": "", "code": "",
                 "total_odds": res["total_odds"], "est_prob": res["est_prob"], "legs": len(res["legs"])}
        d["last_gen_date"] = today
        if not res["legs"]:
            d["log"].append(entry)
            await self.history.save(rec)
            if push:
                await self.send(f"🔁 <b>Rollover day {n}/{m}</b> · target {d['target']:g}\n{RULE}\n"
                                f"⏸ {esc(res['note'])}\nI'll try again tomorrow morning. The plan stays on day {n}.",
                                test=d.get("test"))
            return entry
        code = ""
        if d.get("autobook", True):
            try:
                code = await self.s.bookmaker.book([Selection(l["event_id"], l["market_id"], l["specifier"],
                                                              l["outcome_id"]) for l in res["legs"]])
            except BookingError as exc:
                entry["note"] = f"Booking failed: {exc}"
        srec = await self.record(res["legs"], code, "rollover", "rollover", f"Rollover {d['target']:g}x",
                                 res["total_odds"], res["est_prob"], test=d.get("test"), rollover_id=rec["id"],
                                 rollover_day=n, target=float(d["target"]))
        entry.update(status="pending", slip_id=srec["id"] if srec else "", code=code)
        d["log"].append(entry)
        await self.history.save(rec)
        if push:
            stake = ""
            if d.get("value"):
                stake = f"\nStake in play: <b>{d['value']:,.0f}</b> → <b>{d['value'] * res['total_odds']:,.0f}</b> if it lands"
            lines = [f"🔁 <b>Rollover day {n}/{m}</b> · target {d['target']:g}" + ("" if not code else
                     f"\nCode <code>{esc(code)}</code>"), RULE]
            for i, l in enumerate(res["legs"], 1):
                lines.append(leg_lines(l, i, why=True))
            lines += [RULE, f"<b>Total odds <code>{res['total_odds']:.2f}</code></b> · est. hit chance "
                      f"<b>{res['est_prob']:.0%}</b>{stake}",
                      "<i>Top leagues only, stats-confirmed legs ≥80% est. Estimates, not guarantees. 18+.</i>"]
            markup = None
            if not code:
                from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
                if srec:
                    markup = Kb([[Btn("📒 Book this day on SportyBet", callback_data=f"HB|{srec['id']}")]])
            await self.send("\n".join(lines), test=d.get("test"), markup=markup)
        return entry

    async def book_record(self, rid: str) -> dict:
        """Book a saved slip that has no code yet (rollover day built without auto-booking). Updates the record."""
        rec = await self.history.get(rid)
        if not rec:
            raise ValueError("Slip not found.")
        if rec["data"].get("code"):
            return rec
        legs = rec["data"]["legs"]
        code = await self.s.bookmaker.book([Selection(l["event_id"], l["market_id"], l.get("specifier") or "",
                                                      l["outcome_id"]) for l in legs])
        rec["data"]["code"] = code
        await self.history.save(rec)
        ro = await self.history.get(rec["data"].get("rollover_id") or "") if rec["data"].get("rollover_id") else None
        if ro:
            for x in ro["data"]["log"]:
                if x.get("slip_id") == rid:
                    x["code"] = code
            await self.history.save(ro)
        return rec

    async def skip_day(self) -> dict | None:
        rec = await self.active_rollover()
        if not rec:
            return None
        d = rec["data"]
        d["log"].append({"day": d["current_day"], "date": self._today(), "status": "skipped",
                         "note": "Skipped by you", "slip_id": "", "code": ""})
        d["last_gen_date"] = self._today()
        await self.history.save(rec)
        return rec

    async def _rollover_result(self, slip: dict) -> None:
        sd = slip["data"]
        rec = await self.history.get(sd["rollover_id"])
        if not rec or rec["status"] != "active":
            return
        d = rec["data"]
        entry = next((x for x in d["log"] if x.get("slip_id") == slip["id"]), None)
        if entry:
            entry["status"] = slip["status"]
        n, m = d["current_day"], d["days"]
        if slip["status"] in ("won", "void"):
            odds = sd.get("effective_odds") or sd["total_odds"]
            if d.get("value"):
                d["value"] = round(d["value"] * odds, 2)
            d["current_day"] = n + 1
            if n >= m:
                rec["status"] = "completed"
                d["ended"] = now_iso()
                await self.send(f"🏆🎉 <b>ROLLOVER COMPLETE!</b> {m}/{m} days landed at {d['target']:g} odds a day."
                                + (f"\nStake grew from <b>{d['stake']:,.0f}</b> to <b>{d['value']:,.0f}</b>." if d.get("stake") else ""),
                                test=d.get("test"))
            else:
                await self.send(f"🔁 Rollover: day {n}/{m} ✅ done. Day {n + 1} slip comes tomorrow morning"
                                + (f" · stake now <b>{d['value']:,.0f}</b>" if d.get("value") else "") + ".",
                                test=d.get("test"))
        elif slip["status"] == "lost":
            rec["status"] = "busted"
            d["ended"] = now_iso()
            await self.send(f"💔 Rollover busted on day {n}/{m}. Start a fresh plan any time with /rollover.",
                            test=d.get("test"))
        await self.history.save(rec)

    async def _rollover_jobs(self, now: datetime) -> None:
        rec = await self.active_rollover()
        if not rec:
            return
        local = now.astimezone(TZ)
        d = rec["data"]
        if d.get("last_gen_date") == local.strftime("%Y-%m-%d"):
            return
        if not (MORNING_HOUR <= local.hour < LAST_GEN_HOUR):
            return
        if any(x.get("status") in ("pending", "check") for x in d["log"]):
            return
        await self.generate_day(rec)

    async def mark_day(self, result: str) -> dict | None:
        """Owner settles a day the tracker couldn't (status 'check'): result = won | lost."""
        rec = await self.active_rollover()
        if not rec or result not in ("won", "lost"):
            return None
        entry = next((x for x in reversed(rec["data"]["log"]) if x.get("status") in ("check", "pending")), None)
        if not entry or not entry.get("slip_id"):
            return None
        slip = await self.history.get(entry["slip_id"])
        if not slip:
            return None
        slip["status"] = result
        slip["data"]["manual"] = True
        slip["data"]["notified"] = True
        await self.history.save(slip)
        await self._rollover_result(slip)
        return await self.history.get(rec["id"])
