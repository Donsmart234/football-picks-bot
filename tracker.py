"""History recording, result tracking, owner pings and rollover plans.

- Every booked code (chat /scan + Book, /book, AI build_booking_code, Mini App book, rollover days) is saved as a
  'slip' record in the durable history (history.py).
- A background loop (every 5 min while anything is pending, 15 min otherwise) settles each started leg from the
  Sportradar result feed (settle.py). One final Telegram message per slip: "Entered" when the whole slip lands, a short "lost"
  when it loses (sent as soon as one leg loses), "check" when the rest won but a market needs a manual look.
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
from scanner import sel_key
from settle import ResultsClient, early_settle, effective_odds, needs_stats, settle_leg, slip_status

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)
MORNING_HOUR = 9
LAST_GEN_HOUR = 20
ROLLOVER_TARGETS = (2.0, 3.0, 4.0, 5.0)
EARLY_CONFIRM_S = 60          # an early call must hold on two looks at least this far apart


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
        self.live_now = 0
        self._previews: dict[str, dict] = {}

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
            await asyncio.sleep(120 if self.live_now else 300 if busy else 900)

    def owner(self) -> int | None:
        return config.OWNER_TELEGRAM_ID or self.s.storage.get_owner()

    async def send(self, text: str, test: bool = False, markup=None, ref: dict | None = None) -> bool:
        """ref: slip reference stored under the sent message id, so a reply to it edits that slip (editflow.py)."""
        if not self.bot or not self.owner():
            return False
        if test:
            text = "<b>TEST</b> (tracker check, not a real result)\n" + text
        try:
            msg = await self.bot.send_message(self.owner(), text[:4096], parse_mode=ParseMode.HTML,
                                              disable_web_page_preview=True, reply_markup=markup)
            ed = getattr(self.s, "editor", None)
            if ref and ed is not None and getattr(msg, "message_id", None):
                try:
                    ed.remember(self.owner(), msg.message_id, ref)
                except Exception as exc:
                    log.warning("slip ref save failed: %s", exc)
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
        if not code and source not in ("rollover",):
            # History = booked slips only: scanner / AI / app slips are saved when a booking code exists
            log.info("history: not saving unbooked %s slip", source)
            return None
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
            # slips settled early (a leg locked from the live score) are re-checked once their matches end
            since = (now - timedelta(days=4)).isoformat(timespec="seconds")
            try:
                recent = await self.history.list("slip", ["won", "lost"], limit=60, since=since)
            except Exception:
                recent = []
            pending += [r for r in recent if r["data"].get("early_open")]
            if only_id:
                pending = [r for r in pending if r["id"] == only_id]
            cache: dict[str, object] = {}
            settled = 0
            for rec in pending:
                changed = await self._settle_record(rec, now, cache)
                settled += int(changed)
            busy = bool(await self.history.list("slip", ["pending"], limit=1))
            self.live_now = sum(1 for rec in pending for l in rec["data"].get("legs") or []
                                if l.get("status") == "live" or l.get("lock_seen")
                                or ((l.get("early") or {}) and not l["early"].get("verified")))
            if not only_id:
                await self._rollover_jobs(now)
            self.last_tick_summary = {"pending": len(pending), "updated": settled, "matches_checked": len(cache),
                                      "live_legs": self.live_now}
            return busy

    async def _result(self, l: dict, cache: dict):
        key = (l["event_id"], needs_stats(l))
        if key not in cache:
            cache[key] = await self.results.result(l["event_id"], need_stats=needs_stats(l))
        return cache[key]

    @staticmethod
    def _score(res) -> str:
        score = res.score
        if res.ht and res.state == "ended":
            score += f" (HT {res.ht[0]}-{res.ht[1]})"
        return score

    async def _settle_record(self, rec: dict, now: datetime, cache: dict) -> bool:
        d = rec["data"]
        legs = d.get("legs") or []
        changed = False
        corrected = False
        for l in legs:
            st0 = l.get("status")
            early = l.get("early") or {}
            if early and not early.get("verified") and st0 in ("won", "lost"):
                # locked early from the live score: confirm against the final result (VAR, feed corrections)
                res = await self._result(l, cache)
                if res.state in ("ended", "void"):
                    final, _ = settle_leg(l, res)
                    if final in ("won", "lost", "void"):
                        early.update(verified=True, final=final)
                        l["score"] = self._score(res) or l.get("score", "")
                        if final != st0:
                            l.update(status=final, note=f"Corrected at full time (was {st0} early)")
                            corrected = True
                        changed = True
                continue
            if st0 in ("won", "lost", "void", "check") or rec["status"] != "pending":
                continue
            ko = _parse(l.get("kickoff", ""))
            if ko and now < ko + timedelta(minutes=2):
                continue
            res = await self._result(l, cache)
            st, note = settle_leg(l, res)
            if st == "live":
                if res.minute:
                    note = f"{res.status_name} {res.minute}'".strip()
                e = early_settle(l, res)
                seen = l.get("lock_seen") or {}
                ts = now.timestamp()
                if e and seen.get("st") == e[0] and ts - float(seen.get("ts") or ts) >= EARLY_CONFIRM_S:
                    st, note = e        # certain on two looks at least a minute apart: lock it now
                    l["early"] = {"min": res.minute, "score": res.score, "at": now_iso(), "verified": False}
                    l.pop("lock_seen", None)
                elif e and seen.get("st") != e[0]:
                    l["lock_seen"] = {"st": e[0], "ts": ts}
                    changed = True
                elif not e and seen:
                    l.pop("lock_seen", None)
                    changed = True
            if st == "pending" and ko and now > ko + timedelta(hours=72):
                st, note = "check", "no result after 72 h: check SportyBet"
            score = self._score(res)
            if (st, note, score) != (l.get("status"), l.get("note"), l.get("score")):
                l.update(status=st, note=note, score=score)
                changed = True
        d["early_open"] = any((l.get("early") or {}) and not l["early"].get("verified") for l in legs)
        status = slip_status(legs)
        if rec["status"] == "pending" and status != "pending":
            rec["status"] = status
            d["settled_at"] = now_iso()
            d["effective_odds"] = effective_odds(legs)
            if any(l.get("early") for l in legs):
                d["settled_early"] = True
            changed = True
        elif corrected and status != rec["status"] and status != "pending":
            old = rec["status"]
            rec["status"] = status
            d["effective_odds"] = effective_odds(legs)
            d["corrected"] = f"{old} -> {status}"
            changed = True
        if changed:
            await self.history.save(rec)
        if rec["status"] != "pending" and not d.get("notified"):
            d["notified"] = True
            await self.history.save(rec)            # mark first: never ping twice, even after a crash
            d["ping_ok"] = await self._notify(rec)
            await self.history.save(rec)
            if d.get("rollover_id"):
                await self._rollover_result(rec)
        elif corrected and d.get("corrected") and not d.get("correction_sent"):
            d["correction_sent"] = True
            await self.history.save(rec)
            await self.send(f"<b>Correction</b> · {esc(d.get('label') or 'Slip')}"
                            + (f" <code>{esc(d['code'])}</code>" if d.get("code") else "")
                            + f": a leg called early changed at full time; the slip is now <b>{esc(rec['status'])}</b>.",
                            test=d.get("test"), markup=img_markup(rec["id"]))
            if d.get("rollover_id"):
                await self._rollover_correction(rec)
        return changed

    async def _notify(self, rec: dict) -> bool:
        d, st = rec["data"], rec["status"]
        name = f"{d.get('label') or 'Slip'}" + (f" · Rollover day {d['rollover_day']}" if d.get("rollover_id") else "")
        if st == "won":
            early = [l for l in d["legs"] if l.get("early")]
            running = [l for l in early if not (l.get("early") or {}).get("verified")]
            if running:
                title = (f"<b>WON · Entered!</b> Your {esc(name)} slip is in.\n"
                         f"Every leg is already certain from the live score{'s' if len(running) > 1 else ''} "
                         f"({len(running)} match{'es' if len(running) > 1 else ''} still playing) · total odds "
                         f"<b>{d.get('effective_odds') or d['total_odds']:.2f}</b>.")
            else:
                title = (f"<b>WON · Entered!</b> <b>Your {esc(name)} slip landed!</b>\n"
                         f"Every leg came in at total odds <b>{d.get('effective_odds') or d['total_odds']:.2f}</b>.")
            return await self.send(record_text(rec, title) + f"\n{RULE}\n<i>Estimates, not guarantees — enjoy this one. 18+.</i>",
                                   test=d.get("test"), markup=img_markup(rec["id"]))
        elif st == "lost":
            lost = [l for l in d["legs"] if l.get("status") == "lost"]
            l = lost[0] if lost else {}
            more = f" (+{len(lost) - 1} more)" if len(lost) > 1 else ""
            return await self.send(f"<b>LOST · {esc(name)} slip lost</b>"
                            + (f" · <code>{esc(d['code'])}</code>" if d.get("code") else "")
                            + f"\n{esc(l.get('home', ''))} v {esc(l.get('away', ''))} · {esc(l.get('pick', ''))} "
                            f"({esc(l.get('score', ''))}){more}", test=d.get("test"), markup=img_markup(rec["id"]))
        elif st == "check":
            n = sum(1 for l in d["legs"] if l.get("status") == "check")
            return await self.send(f"<b>CHECK · {esc(name)} slip</b>" + (f" <code>{esc(d['code'])}</code>" if d.get("code") else "")
                            + f": no leg lost, but {n} leg(s) use a market I can't settle automatically. "
                            "Check it on SportyBet.", test=d.get("test"))
        elif st == "void":
            return await self.send(f"<b>VOID</b> · {esc(name)} slip: every match was cancelled or voided.", test=d.get("test"))
        return False

    # ---- rollover ----------------------------------------------------------------
    async def active_rollover(self) -> dict | None:
        rows = await self.history.list("rollover", ["active"], limit=1)
        return rows[0] if rows else None

    async def rollovers(self, limit: int = 10) -> list[dict]:
        return await self.history.list("rollover", None, limit=limit)

    async def create_rollover(self, target: float, days: int, stake: float | None = None,
                              autobook: bool = True, generate: bool = True, test: bool = False,
                              push: bool = True, allow_unders: bool = False) -> tuple[dict, dict | None]:
        if target not in ROLLOVER_TARGETS:
            raise ValueError("Daily target must be 2, 3, 4 or 5.")
        if not 3 <= days <= 15:
            raise ValueError("Days must be between 3 and 15.")
        cur = await self.active_rollover()
        if cur:
            raise ValueError("A rollover is already running. Stop it first (/rollover stop).")
        rec = {"id": new_id("ro"), "kind": "rollover", "status": "active", "data": {
            "target": target, "days": days, "stake": stake, "value": stake, "current_day": 1,
            "log": [], "last_gen_date": "", "autobook": autobook, "test": test, "push": push, "started": now_iso(),
            "allow_unders": bool(allow_unders)}}
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
        res = await self.s.scanner.rollover_slip(float(d["target"]), allow_unders=bool(d.get("allow_unders")))
        n, m = d["current_day"], d["days"]
        entry = {"day": n, "date": today, "status": "no_slip", "note": res.get("note", ""), "slip_id": "", "code": "",
                 "total_odds": res["total_odds"], "est_prob": res["est_prob"], "legs": len(res["legs"])}
        if d.get("value") and res["legs"]:
            entry.update(stake=d["value"], expected_return=round(d["value"] * res["total_odds"], 2))
        d["last_gen_date"] = today
        if not res["legs"]:
            d["log"].append(entry)
            await self.history.save(rec)
            if push:
                await self.send(f"<b>Rollover day {n}/{m}</b> · target {d['target']:g}\n{RULE}\n"
                                f"No slip today: {esc(res['note'])}\nI'll try again tomorrow morning. The plan stays on day {n}.",
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
            lines = [f"<b>Rollover day {n}/{m}</b> · target {d['target']:g}" + ("" if not code else
                     f"\nCode <code>{esc(code)}</code>"), RULE]
            for i, l in enumerate(res["legs"], 1):
                lines.append(leg_lines(l, i, why=True))
            lines += [RULE, f"<b>Total odds <code>{res['total_odds']:.2f}</code></b> · est. hit chance "
                      f"<b>{res['est_prob']:.0%}</b>{stake}",
                      "<i>Top leagues only, stats-confirmed legs ≥80% est."
                      + ("" if d.get("allow_unders") else " No under selections.")
                      + " Estimates, not guarantees. 18+.</i>"]
            markup = day_markup(srec["id"], bool(code), code) if srec else None
            ref = {"section": "rollover_day", "slip_id": srec["id"], "plan_id": rec["id"], "day": n,
                   "target": float(d["target"]), "allow_unders": bool(d.get("allow_unders")),
                   "code": code, "legs": res["legs"]} if srec else None
            await self.send("\n".join(lines), test=d.get("test"), markup=markup, ref=ref)
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
            entry["settled_at"] = now_iso()
            early = [l["early"]["min"] for l in sd.get("legs") or [] if (l.get("early") or {}).get("min")]
            if early and slip["status"] == "won":
                entry["won_early"] = max(early)
        n, m = d["current_day"], d["days"]
        if slip["status"] in ("won", "void"):
            odds = sd.get("effective_odds") or sd["total_odds"]
            if d.get("value"):
                d["value"] = round(d["value"] * odds, 2)
            if entry is not None and d.get("value"):
                entry["bankroll"] = d["value"]
            d["current_day"] = n + 1
            if n >= m:
                rec["status"] = "completed"
                d["ended"] = now_iso()
                await self.send(f"<b>ROLLOVER COMPLETE!</b> {m}/{m} days landed at {d['target']:g} odds a day."
                                + (f"\nStake grew from <b>{d['stake']:,.0f}</b> to <b>{d['value']:,.0f}</b>." if d.get("stake") else ""),
                                test=d.get("test"))
            else:
                await self.send(f"Rollover: day {n}/{m} WON, done. Day {n + 1} slip comes tomorrow morning"
                                + (f" · stake now <b>{d['value']:,.0f}</b>" if d.get("value") else "") + ".",
                                test=d.get("test"))
        elif slip["status"] == "lost":
            rec["status"] = "busted"
            d["ended"] = now_iso()
            await self.send(f"<b>LOST</b> · Rollover busted on day {n}/{m}. Start a fresh plan any time with /rollover.",
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


    # ---- rollover tickets ---------------------------------------------------------
    async def rollover_tickets(self, plan: dict) -> list[dict]:
        """Every day's ticket of a plan (also replaced ones), oldest first, with status and the running bankroll.
        Status per ticket: pending | live | won | lost | check | void | replaced | no_slip | skipped."""
        slips = {r["id"]: r for r in await self.history.list_field("slip", "rollover_id", plan["id"], limit=200)}
        d = plan["data"]
        bank = d.get("stake")
        now = datetime.now(timezone.utc)
        log_ = d.get("log") or []
        latest = next((x for x in reversed(log_) if x.get("slip_id")), None)
        out = []
        for x in log_:
            rec = slips.get(x.get("slip_id") or "")
            sd = rec["data"] if rec else {}
            legs = sd.get("legs") or []
            st = rec["status"] if rec else x.get("status", "")
            if st == "pending" and any(l.get("status") in ("live", "won", "lost") or l.get("early") for l in legs):
                st = "live"
            t = {"day": x.get("day"), "date": x.get("date"), "status": st, "slip_id": x.get("slip_id") or "",
                 "code": sd.get("code") or x.get("code") or "", "total_odds": sd.get("total_odds") or x.get("total_odds"),
                 "effective_odds": sd.get("effective_odds"), "est_prob": sd.get("est_prob", x.get("est_prob")),
                 "note": x.get("note", ""), "legs": legs, "n_legs": len(legs) or x.get("legs") or 0,
                 "created_at": rec.get("created_at") if rec else "", "settled_at": sd.get("settled_at", ""),
                 "replaced_by": sd.get("replaced_by", ""), "replaces": sd.get("replaces", ""),
                 "refreshed": bool(sd.get("replaces")), "corrected": sd.get("corrected", "")}
            early = [l["early"]["min"] for l in legs if (l.get("early") or {}).get("min")]
            t["won_early"] = max(early) if early and st in ("won", "live") and all(
                l.get("status") in ("won", "void") for l in legs) else None
            if bank is not None and st not in ("no_slip", "skipped", "replaced"):
                odds = t["effective_odds"] or t["total_odds"] or 1.0
                t["stake"] = round(bank, 2)
                t["expected_return"] = round(bank * (t["total_odds"] or 1.0), 2)
                if st in ("won", "void"):
                    bank = round(bank * odds, 2)
                elif st == "lost":
                    bank = 0.0
                t["bankroll"] = bank
            elif bank is not None:
                t["stake"] = None
                t["bankroll"] = bank
            t["can_refresh"] = bool(plan["status"] == "active" and rec and rec["status"] == "pending"
                                    and latest is x and self._not_started(legs, now))
            out.append(t)
        return out

    @staticmethod
    def _not_started(legs: list[dict], now: datetime) -> bool:
        kos = [_parse(l.get("kickoff", "")) for l in legs]
        if not legs or any(k is None for k in kos):
            return False
        return now < min(kos) and all(l.get("status") in ("pending", None, "") for l in legs)

    # ---- refresh picks -------------------------------------------------------------
    async def _refresh_target(self, slip_id: str) -> tuple[dict, dict, dict]:
        slip = await self.history.get(slip_id)
        if not slip or not slip["data"].get("rollover_id"):
            raise ValueError("That rollover slip wasn't found.")
        plan = await self.history.get(slip["data"]["rollover_id"])
        if not plan or plan["status"] != "active":
            raise ValueError("That plan isn't running any more.")
        if slip["status"] != "pending":
            raise ValueError("Only a pending day can be refreshed." if slip["status"] != "replaced"
                             else "That slip was already replaced: refresh the newest one.")
        latest = next((x for x in reversed(plan["data"]["log"]) if x.get("slip_id")), None)
        if not latest or latest.get("slip_id") != slip_id:
            raise ValueError("Only the current day's slip can be refreshed.")
        if not self._not_started(slip["data"]["legs"], datetime.now(timezone.utc)):
            raise ValueError("A match on this slip has already kicked off, so it can't be refreshed.")
        return slip, plan, latest

    async def refresh_day(self, slip_id: str, exclude_prev: bool = False) -> dict:
        """Rebuild today's rollover slip under the same rules with fresh prices.
        exclude_prev=True leaves out every previous selection so the new slip is different.
        Old slip unbooked -> replaced at once. Old slip booked -> preview; a new code only on refresh_confirm()."""
        slip, plan, _ = await self._refresh_target(slip_id)
        d = plan["data"]
        prev = {sel_key(l) for l in slip["data"]["legs"]}
        res = await self.s.scanner.rollover_slip(float(d["target"]), allow_unders=bool(d.get("allow_unders")),
                                                 exclude=prev if exclude_prev else None, fresh=True)
        if not res["legs"]:
            return {"status": "none", "note": ("No different valid slip exists right now under the same rules "
                                               f"(top leagues, every leg ≥80% est., ~{d['target']:g} odds"
                                               + ("" if d.get("allow_unders") else ", no unders")
                                               + "). Your current slip stays."), "slip": res}
        if {sel_key(l) for l in res["legs"]} == prev:
            return {"status": "same", "note": ("Fresh prices give the same picks: they're still the strongest. "
                                               "Tap Different picks to leave them out."), "slip": res}
        if slip["data"].get("code"):
            token = new_id("rf")
            self._previews = {k: v for k, v in self._previews.items() if time.time() - v["ts"] < 900}
            self._previews[token] = {"slip_id": slip_id, "res": res, "ts": time.time()}
            return {"status": "preview", "token": token, "slip": res, "old_code": slip["data"]["code"],
                    "old_id": slip_id}
        new = await self._replace_day(slip, plan, res, "")
        return {"status": "replaced", "record": new, "slip": res, "old_id": slip_id}

    def edit_preview(self, slip_id: str, res: dict) -> str:
        """Store an edited rollover slip (AI / reply edit) as a preview; it is booked and replaces the day only on
        refresh_confirm(token), exactly like a refreshed slip."""
        token = new_id("rf")
        self._previews = {k: v for k, v in self._previews.items() if time.time() - v["ts"] < 900}
        self._previews[token] = {"slip_id": slip_id, "res": res, "ts": time.time(), "edited": True}
        return token

    def preview(self, token: str) -> dict | None:
        pv = self._previews.get(token)
        return pv if pv and time.time() - pv["ts"] <= 900 else None

    async def refresh_confirm(self, token: str) -> dict:
        """Explicit confirm: book the previewed slip (new code), mark the old booked slip replaced."""
        pv = self._previews.get(token)
        if not pv or time.time() - pv["ts"] > 900:
            raise ValueError("That preview expired. Tap Refresh picks again.")
        slip, plan, _ = await self._refresh_target(pv["slip_id"])
        res = pv["res"]
        now = datetime.now(timezone.utc)
        if not self._not_started(res["legs"], now + timedelta(minutes=2)):
            raise ValueError("A match on the new slip is about to start. Tap Refresh picks again.")
        self._previews.pop(token, None)
        code = await self.s.bookmaker.book([Selection(l["event_id"], l["market_id"], l.get("specifier") or "",
                                                      l["outcome_id"]) for l in res["legs"]])
        new = await self._replace_day(slip, plan, res, code)
        return {"status": "replaced", "record": new, "slip": res, "code": code, "old_id": slip["id"],
                "old_code": slip["data"].get("code")}

    async def _replace_day(self, slip: dict, plan: dict, res: dict, code: str) -> dict:
        d = plan["data"]
        n = slip["data"].get("rollover_day") or d["current_day"]
        new = await self.record(res["legs"], code, "rollover", "rollover", f"Rollover {d['target']:g}x",
                                res["total_odds"], res["est_prob"], test=d.get("test"), rollover_id=plan["id"],
                                rollover_day=n, target=float(d["target"]))
        if not new:
            raise ValueError("The history store didn't answer: nothing was replaced.")
        new["data"]["replaces"] = slip["id"]
        await self.history.save(new)
        slip["status"] = "replaced"
        slip["data"].update(replaced_by=new["id"], replaced_at=now_iso(), notified=True)
        await self.history.save(slip)
        for x in d["log"]:
            if x.get("slip_id") == slip["id"]:
                x["status"] = "replaced"
                x["replaced_by"] = new["id"]
        entry = {"day": n, "date": self._today(), "status": "pending", "note": "Refreshed picks", "slip_id": new["id"],
                 "code": code, "total_odds": res["total_odds"], "est_prob": res["est_prob"], "legs": len(res["legs"]),
                 "refreshed": True}
        if d.get("value"):
            entry.update(stake=d["value"], expected_return=round(d["value"] * res["total_odds"], 2))
        d["log"].append(entry)
        await self.history.save(plan)
        return new

    async def _rollover_correction(self, slip: dict) -> None:
        """A day called early changed at full time (rare: VAR / feed correction). Keep the plan honest."""
        rec = await self.history.get(slip["data"]["rollover_id"])
        if not rec:
            return
        d = rec["data"]
        entry = next((x for x in d["log"] if x.get("slip_id") == slip["id"]), None)
        if entry:
            entry["status"] = slip["status"]
            entry["note"] = "Corrected at full time"
        if slip["status"] == "lost" and rec["status"] in ("active", "completed"):
            rec["status"] = "busted"
            d["ended"] = now_iso()
            d["end_reason"] = "corrected at full time"
        await self.history.save(rec)


def day_markup(slip_id: str, booked: bool, code: str | None = None):
    """Telegram buttons under a rollover day's slip: open the code in the SportyBet app (url button, so the OS hands
    the app-claimed link to the app), book (if no code yet) and refresh."""
    from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
    from bookmakers import sportybet_share_url
    rows = []
    if code:
        rows.append([Btn(f"Open {code} in SportyBet", url=sportybet_share_url(code))])
    if not booked:
        rows.append([Btn("Book this day on SportyBet", callback_data=f"HB|{slip_id}")])
    rows.append([Btn("Refresh picks", callback_data=f"RF|{slip_id}"),
                 Btn("Different picks", callback_data=f"RF|{slip_id}|x")])
    rows.append([Btn("Slip image", callback_data=f"IMG|h|{slip_id}"[:64])])
    return Kb(rows)


def img_markup(record_id: str):
    """Slip image button under a result ping (won / lost / correction) for that history record."""
    from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
    return Kb([[Btn("Slip image", callback_data=f"IMG|h|{record_id}"[:64])]])
