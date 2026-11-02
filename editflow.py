"""Reply-context slip editing: which slip a bot message carries, and editing that slip inside its own section.

Every bot message that shows a slip (rollover day push, rollover refresh preview / edited preview, /scan result, AI
slip, analyzer result, history ticket) gets a *slip reference* stored under (chat id, message id): section, plan / day
/ record id / preview token / in-memory slip id, the legs and the rules. Stored in memory + SQLite + Supabase
(`fra_records`, kind 'msgref', pruned after 3 days) so replies still resolve after a redeploy. When nothing was stored
the reply is resolved from the replied message's buttons (RC|token, RF|slip, SR|sid, SB|sid, AZR|aid, HB|slip) and,
last, from its header text (rollover messages -> the plan's current day).

SlipEditor.run(ctx, op) applies one slipedit op under the section's constraints and returns an outcome; nothing is
booked or written to History here:
  rollover -> an edited preview (tracker.edit_preview): booked + replaces the day only on "✅ Confirm: book new code"
  scan / ai -> a new in-memory scan slip (same scan_args, constraint_check re-run) with Book / Refresh buttons
  analysis / history -> a new in-memory custom slip with a Book button
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import slipedit
from constraints import TOTAL_LO, Constraints, verify

log = logging.getLogger(__name__)
REF_TTL = 3 * 24 * 3600
LAST_TTL = 3 * 3600
LEG_KEYS = ("event_id", "market_id", "specifier", "outcome_id", "match", "home", "away", "home_uid", "away_uid",
            "league", "category", "kickoff", "kickoff_local", "market", "pick", "odds", "est", "verdict", "tags",
            "why", "state", "level")
SECTION_LABEL = {"rollover_preview": "rollover preview", "rollover_day": "rollover day", "scan": "scan result",
                 "ai": "AI slip", "analysis": "analyzed slip", "history": "history ticket"}


def compact_legs(legs) -> list[dict]:
    out = []
    for l in legs or []:
        d = l.to_json() if hasattr(l, "to_json") else dict(l)
        out.append({k: d.get(k) for k in LEG_KEYS if d.get(k) not in (None, "")})
    return out


class SlipEditor:
    def __init__(self, services):
        self.s = services
        self._mem: dict[tuple[int, int], dict] = {}
        self._last: dict[int, tuple[int, float]] = {}
        self._pruned = 0.0
        self._tasks: set = set()
        try:
            self.s.storage._exec("CREATE TABLE IF NOT EXISTS slip_refs (chat_id INTEGER, message_id INTEGER, ts REAL, "
                                 "data TEXT, PRIMARY KEY (chat_id, message_id))")
        except Exception as exc:          # pragma: no cover
            log.warning("slip_refs table: %s", exc)

    # ---- references ------------------------------------------------------------
    def remember(self, chat_id: int, message_id: int, ref: dict) -> dict:
        ref = dict(ref)
        ref["legs"] = compact_legs(ref.get("legs"))
        ref["ts"] = time.time()
        key = (int(chat_id), int(message_id))
        self._mem[key] = ref
        self._last[int(chat_id)] = (int(message_id), ref["ts"])
        for k in [k for k, v in self._mem.items() if time.time() - v.get("ts", 0) > REF_TTL]:
            self._mem.pop(k, None)
        try:
            self.s.storage._exec("INSERT OR REPLACE INTO slip_refs VALUES (?,?,?,?)",
                                 (key[0], key[1], ref["ts"], json.dumps(ref, default=str)))
            if time.time() - self._pruned > 3600:
                self._pruned = time.time()
                self.s.storage._exec("DELETE FROM slip_refs WHERE ts < ?", (time.time() - REF_TTL,))
        except Exception as exc:
            log.warning("slip ref sqlite: %s", exc)
        hist = self._history()
        if hist is not None and getattr(hist, "remote", False):
            try:
                loop = asyncio.get_running_loop()
                t = loop.create_task(self._save_remote(hist, key, ref))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
            except RuntimeError:
                pass
        return ref

    def _history(self):
        tr = getattr(self.s, "tracker", None)
        return getattr(tr, "history", None)

    @staticmethod
    def _rid(key: tuple[int, int]) -> str:
        return f"mr_{key[0]}_{key[1]}"

    async def _save_remote(self, hist, key, ref) -> None:
        try:
            await hist.save({"id": self._rid(key), "kind": "msgref", "status": "ref", "data": ref})
            if time.time() - getattr(self, "_rpruned", 0) > 6 * 3600:
                self._rpruned = time.time()
                before = (datetime.now(timezone.utc) - timedelta(seconds=REF_TTL)).isoformat(timespec="seconds")
                await hist.delete_kind_before("msgref", before)
        except Exception as exc:
            log.warning("slip ref remote save: %s", exc.__class__.__name__)

    async def lookup(self, chat_id: int, message_id: int) -> dict | None:
        key = (int(chat_id), int(message_id))
        if key in self._mem:
            return self._mem[key]
        try:
            rows = self.s.storage._exec("SELECT data FROM slip_refs WHERE chat_id=? AND message_id=?", key)
            if rows:
                ref = json.loads(rows[0][0])
                self._mem[key] = ref
                return ref
        except Exception:
            pass
        hist = self._history()
        if hist is not None and getattr(hist, "remote", False):
            try:
                rec = await hist.get(self._rid(key))
                if rec and rec.get("kind") == "msgref":
                    self._mem[key] = rec["data"]
                    return rec["data"]
            except Exception as exc:
                log.warning("slip ref remote get: %s", exc.__class__.__name__)
        return None

    async def last_ref(self, chat_id: int) -> dict | None:
        """The most recent slip the bot sent in this chat (for "change the first selection" sent without a reply)."""
        mid, ts = self._last.get(int(chat_id), (None, 0.0))
        if mid is None:
            try:
                rows = self.s.storage._exec("SELECT message_id, ts FROM slip_refs WHERE chat_id=? ORDER BY ts DESC "
                                            "LIMIT 1", (int(chat_id),))
                if rows:
                    mid, ts = rows[0]
            except Exception:
                pass
        if mid is None or time.time() - ts > LAST_TTL:
            return None
        return await self.lookup(chat_id, mid)

    @staticmethod
    def from_markup(markup) -> dict | None:
        """Slip identity from the replied message's inline buttons (works without a stored reference)."""
        if not markup:
            return None
        rows = markup.get("inline_keyboard") if isinstance(markup, dict) else getattr(markup, "inline_keyboard", None)
        datas = []
        for row in rows or []:
            for b in row:
                d = getattr(b, "callback_data", None) if not isinstance(b, dict) else b.get("callback_data")
                if d:
                    datas.append(str(d))
        for d in datas:
            p = d.split("|")
            if p[0] == "RC" and len(p) > 1:
                return {"section": "rollover_preview", "token": p[1]}
        for d in datas:
            p = d.split("|")
            if p[0] in ("RF", "HB") and len(p) > 1:
                return {"section": "rollover_day", "slip_id": p[1]}
            if p[0] in ("SR",) and len(p) > 1:
                return {"section": "scan", "sid": p[1]}
            if p[0] in ("AZR", "AZN") and len(p) > 1:
                return {"section": "analysis", "aid": p[1]}
        for d in datas:
            p = d.split("|")
            if p[0] == "SB" and len(p) > 1:
                return {"section": "ai", "sid": p[1]}
        return None

    @staticmethod
    def from_text(text: str) -> dict | None:
        t = text or ""
        if re.search(r"New picks for today|Rollover day \d|Picks refreshed|Edited rollover|Rollover tickets|🔁 Rollover", t):
            return {"section": "rollover_day", "slip_id": "", "from_text": True}
        return None

    async def resolve(self, chat_id: int, reply_msg) -> dict | None:
        """Hydrated slip context for a reply (None when the replied message carries no slip)."""
        if reply_msg is None:
            return None
        ref = await self.lookup(chat_id, getattr(reply_msg, "message_id", 0) or 0)
        if not ref:
            ref = self.from_markup(getattr(reply_msg, "reply_markup", None))
        if not ref:
            ref = self.from_text(getattr(reply_msg, "text", None) or getattr(reply_msg, "caption", None) or "")
        if not ref:
            return None
        return await self.hydrate(ref)

    async def hydrate(self, ref: dict) -> dict | None:
        """Fill legs / rules from the live objects (preview, history record, scan slip) when they still exist."""
        ctx = dict(ref)
        sec = ctx.get("section")
        tr = getattr(self.s, "tracker", None)
        ai = getattr(self.s, "ai", None)
        try:
            if sec == "rollover_preview":
                pv = tr.preview(ctx.get("token", "")) if tr else None
                if pv:
                    ctx["slip_id"] = pv["slip_id"]
                    ctx["legs"] = compact_legs(pv["res"]["legs"])
                if not ctx.get("slip_id"):
                    sec = ctx["section"] = "rollover_day"
                    ctx["slip_id"] = ""
            if sec in ("rollover_preview", "rollover_day") and tr:
                slip = await tr.history.get(ctx["slip_id"]) if ctx.get("slip_id") else None
                plan = None
                if slip and slip["data"].get("rollover_id"):
                    plan = await tr.history.get(slip["data"]["rollover_id"])
                if slip is None or slip.get("status") == "replaced":
                    plan = plan or await tr.active_rollover()
                    if plan:     # the day's newest slip (the replied one may have been replaced since)
                        last = next((x for x in reversed(plan["data"].get("log") or []) if x.get("slip_id")), None)
                        if last and last.get("slip_id") != ctx.get("slip_id"):
                            slip = await tr.history.get(last["slip_id"])
                            if sec == "rollover_day" or not ctx.get("legs"):
                                ctx["legs"] = []
                            ctx["slip_id"] = last["slip_id"]
                if not slip or not plan:
                    return ctx if ctx.get("legs") else None
                ctx["plan_id"] = plan["id"]
                ctx["day"] = slip["data"].get("rollover_day")
                ctx["target"] = float(plan["data"]["target"])
                ctx["allow_unders"] = bool(plan["data"].get("allow_unders"))
                ctx["code"] = slip["data"].get("code") or ""
                ctx["slip_status"] = slip["status"]
                if sec == "rollover_day" or not ctx.get("legs"):
                    ctx["legs"] = compact_legs(slip["data"]["legs"])
            elif sec in ("scan", "ai"):
                entry = ai.slips.get(ctx.get("sid", "")) if ai else None
                if entry:
                    sl = entry["slip"]
                    ctx["legs"] = compact_legs(sl.get("legs"))
                    ctx["slip"] = {k: v for k, v in sl.items() if k != "legs"}
                if not ctx.get("legs"):
                    return None
            elif sec == "analysis":
                an = getattr(self.s, "analyzer", None)
                res = an.get(ctx.get("aid", "")) if an else None
                if res:
                    ctx["legs"] = compact_legs([l for l in res["legs"] if l.get("event_id")])
                    ctx["code"] = res.get("code")
                if not ctx.get("legs"):
                    return None
            elif sec == "history" and tr:
                rec = await tr.history.get(ctx.get("slip_id", "")) if ctx.get("slip_id") else None
                if rec:
                    ctx["legs"] = compact_legs(rec["data"]["legs"])
                    ctx["code"] = rec["data"].get("code")
                    if rec["data"].get("rollover_id") and rec["status"] == "pending":
                        ctx["section"] = "rollover_day"
                        return await self.hydrate(ctx)
                if not ctx.get("legs"):
                    return None
        except Exception as exc:
            log.warning("slip ref hydrate failed: %s", exc, exc_info=True)
            return ctx if ctx.get("legs") else None
        return ctx if ctx.get("legs") else None

    # ---- context for the AI ----------------------------------------------------------
    @staticmethod
    def context_text(ctx: dict) -> str:
        sec = ctx.get("section")
        if sec in ("rollover_preview", "rollover_day"):
            head = (f"Section: ROLLOVER plan {ctx.get('plan_id', '')} day {ctx.get('day', '?')}, target "
                    f"{ctx.get('target', 0):g} odds. Rules for every leg: top leagues only, stats-confirmed, est >=80%, "
                    + ("unders allowed" if ctx.get("allow_unders") else "no Under selections")
                    + ", kick-off not started, one pick per match; total odds must stay within 0.97x-1.20x of the target.")
            if sec == "rollover_preview":
                head += (f" This is a PREVIEW of new picks (not booked). The day's current slip is booked as "
                         f"{ctx.get('code') or '(no code)'}.")
            else:
                head += f" Current slip, booked as {ctx.get('code') or '(no code yet)'}."
        elif sec in ("scan", "ai"):
            sl = ctx.get("slip") or {}
            head = (f"Section: SCAN slip ({sl.get('label', 'Safe')}, target {sl.get('target', '?')} odds"
                    + (f", rules: {sl.get('constraints_text')}" if sl.get("constraints_text") else "") + "). Not booked.")
        elif sec == "analysis":
            head = f"Section: ANALYZED booking code {ctx.get('code', '')} (the user's own slip)."
        else:
            head = f"Section: HISTORY ticket {ctx.get('code', '')} (already booked)."
        lines = [head, "Legs:"]
        for i, l in enumerate(ctx.get("legs") or [], 1):
            est = f" · est {l['est']:.0%}" if isinstance(l.get("est"), (int, float)) else ""
            lines.append(f"{i}. {slipedit.leg_name(l)} · {l.get('kickoff_local') or l.get('kickoff', '')[:16]} · "
                         f"{l.get('market', '')}: {l.get('pick', '')} @{float(l.get('odds') or 0):.2f}{est}")
        lines.append(f"Total odds {slipedit.total_odds(ctx.get('legs') or []):.2f}")
        return "\n".join(lines)

    # ---- edits -------------------------------------------------------------------------
    async def prefetch(self, ctx: dict) -> None:
        """Warm the candidate pool for this slip's section (runs while Gemini reads the message)."""
        try:
            sec = ctx.get("section")
            if sec in ("rollover_preview", "rollover_day"):
                await self.s.scanner.edit_candidates("rollover", allow_unders=bool(ctx.get("allow_unders")))
            elif sec in ("scan", "ai") and (ctx.get("slip") or {}).get("tier") in ("safe", "mid", "risk", "high"):
                sl = ctx["slip"]
                c = Constraints.from_dict(sl.get("constraints")) if sl.get("constraints") else None
                await self.s.scanner.edit_candidates("scan", (sl.get("scan_args") or {}).get("day") or sl.get("day")
                                                     or "today", tier=sl.get("tier"), c=c)
        except Exception as exc:
            log.info("edit prefetch failed: %s", exc.__class__.__name__)

    async def run(self, ctx: dict, op: dict) -> dict:
        sec = ctx.get("section")
        try:
            if sec in ("rollover_preview", "rollover_day"):
                return await self._run_rollover(ctx, op)
            if sec in ("scan", "ai") and (ctx.get("slip") or {}).get("tier") in ("safe", "mid", "risk", "high"):
                return await self._run_scan(ctx, op)
            return await self._run_custom(ctx, op)
        except ValueError as exc:
            return self._fail(ctx, str(exc))

    @staticmethod
    def _fail(ctx: dict, note: str) -> dict:
        legs = ctx.get("legs") or []
        return {"ok": False, "section": ctx.get("section"), "note": note, "change": "", "legs": legs,
                "total_odds": round(slipedit.total_odds(legs), 2), "est_prob": round(slipedit.combined(legs), 4)}

    async def _run_rollover(self, ctx: dict, op: dict) -> dict:
        tr = self.s.tracker
        if not ctx.get("slip_id"):
            raise ValueError("No running rollover day to edit: start a plan with /rollover.")
        slip, plan, _ = await tr._refresh_target(ctx["slip_id"])      # current pending day, before first kick-off
        d = plan["data"]
        target = float(d["target"])
        allow_unders = bool(d.get("allow_unders"))
        base = ctx.get("legs") or compact_legs(slip["data"]["legs"])
        rules = {"lo": target * 0.97, "hi": target * 1.20, "target": target, "min_p": 0.80, "min_odds": 1.12,
                 "max_odds": target * 1.20, "no_unders": not allow_unders, "require_confirmed": True,
                 "top_only": True, "strict": True, "label": "rollover"}
        if op.get("action") == "rebuild":
            prev = {slipedit.sel_key(l) for l in base} | {slipedit.sel_key(l) for l in slip["data"]["legs"]}
            res = await self.s.scanner.rollover_slip(target, allow_unders=allow_unders, exclude=prev, fresh=True)
            if not res["legs"]:
                return self._fail(ctx, "No different valid slip exists right now under the same rules (top leagues, "
                                       f"every leg ≥80% est., ~{target:g} odds" + ("" if allow_unders else ", no unders")
                                  + "). Your current slip stays.")
            r = {"ok": True, "legs": res["legs"], "change": "New picks for the whole day", "note": "",
                 "total_odds": res["total_odds"], "est_prob": res["est_prob"]}
        else:
            cands = await self.s.scanner.edit_candidates("rollover", allow_unders=allow_unders)
            r = slipedit.apply_edit(base, op, cands, rules)
            if not r["ok"]:
                return {**self._fail({**ctx, "legs": base}, r["note"]), "plan_id": plan["id"], "slip_id": slip["id"]}
        same = {slipedit.sel_key(l) for l in r["legs"]} == {slipedit.sel_key(l) for l in slip["data"]["legs"]}
        if same:
            return {**self._fail({**ctx, "legs": base}, "That gives back the day's current slip, so nothing changes."),
                    "plan_id": plan["id"], "slip_id": slip["id"]}
        res = {"tier": "rollover", "label": "Rollover", "target": target, "total_odds": r["total_odds"],
               "est_prob": r["est_prob"], "reached": True, "legs": r["legs"], "note": "", "edited": True,
               "rules": {"min_p": 0.80, "leagues": "allowlist", "no_unders": not allow_unders}}
        token = tr.edit_preview(slip["id"], res)
        return {"ok": True, "section": "rollover_preview", "token": token, "slip_id": slip["id"], "plan_id": plan["id"],
                "day": slip["data"].get("rollover_day"), "target": target, "allow_unders": allow_unders,
                "old_code": slip["data"].get("code") or "", "legs": r["legs"], "total_odds": r["total_odds"],
                "est_prob": r["est_prob"], "change": r["change"], "note": r.get("note", ""), "action": op.get("action")}

    async def _run_scan(self, ctx: dict, op: dict) -> dict:
        ai = self.s.ai
        sl = dict(ctx.get("slip") or {})
        base = ctx.get("legs") or []
        tier = sl.get("tier") if sl.get("tier") in ("safe", "mid", "risk", "high") else "safe"
        args = sl.get("scan_args") or {}
        c = Constraints.from_dict(sl.get("constraints")) if sl.get("constraints") else None
        if c is not None:
            c.tier, c.target_odds = None, None
        day = args.get("day") or sl.get("day") or "today"
        rl = sl.get("rules") or {}
        min_p = float(rl.get("min_p") or 0.80)
        lo_o, hi_o = (rl.get("odds") or (1.04, 10.0))
        target = float(sl.get("target") or slipedit.total_odds(base))
        cur = slipedit.total_odds(base)
        reached = bool(sl.get("reached", True))
        rules = {"lo": target * TOTAL_LO if reached else min(cur * 0.95, target * TOTAL_LO),
                 "hi": target * 1.20, "target": target, "min_p": min_p, "min_odds": float(lo_o),
                 "max_odds": float(hi_o), "no_unders": bool(c and "under" in (c.exclude_markets or [])),
                 "strict": reached, "label": f"{sl.get('label', 'scan')} slip", "top_only": not (c and c.widens_leagues())}
        if op.get("action") == "rebuild":
            prev = {slipedit.sel_key(l) for l in base}
            new = await self.s.scanner.scan(tier, args.get("target") or sl.get("target"), day, None, c, exclude=prev,
                                            fresh=True)
            if not new.get("legs"):
                return self._fail(ctx, "No different valid slip exists right now under the same rules. The last slip stands.")
            r = {"ok": True, "legs": new["legs"], "change": "New picks for the whole slip", "note": ""}
        else:
            cands = await self.s.scanner.edit_candidates("scan", day, tier=tier, c=c)
            r = slipedit.apply_edit(base, op, cands, rules)
            if not r["ok"]:
                return self._fail(ctx, r["note"])
        new = {**{k: v for k, v in sl.items() if k not in ("slip_id",)}, "legs": r["legs"],
               "total_odds": round(slipedit.total_odds(r["legs"]), 2), "est_prob": round(slipedit.combined(r["legs"]), 4),
               "edited": True}
        new["reached"] = new["total_odds"] >= target * TOTAL_LO
        new["note"] = "" if new["reached"] else f"Total {new['total_odds']:.2f}, short of the {target:g} target."
        if c is not None:
            new["constraint_check"] = verify(new, c, min_p)
        sid = ai.remember_slip(new)
        return {"ok": True, "section": ctx.get("section"), "sid": sid, "slip": new, "legs": r["legs"],
                "total_odds": new["total_odds"], "est_prob": new["est_prob"], "change": r["change"],
                "note": r.get("note", ""), "action": op.get("action")}

    async def _run_custom(self, ctx: dict, op: dict) -> dict:
        ai = self.s.ai
        base = [l for l in ctx.get("legs") or []]
        if op.get("action") == "rebuild":
            return self._fail(ctx, "I can only edit this slip leg by leg (change / remove / add a leg, change a "
                                   "market, make it safer). For a brand-new slip, ask me for one.")
        now = datetime.now(timezone.utc)
        idx = op.get("index")
        if idx is not None and 0 <= idx < len(base):
            k = slipedit._kick(base[idx])
            if base[idx].get("state") in ("live", "ended", "cancelled") or (k and k <= now):
                return self._fail(ctx, f"Leg {idx + 1} ({slipedit.leg_name(base[idx])}) has already started, so it "
                                       "can't be swapped.")
        no_unders = not any("under" in (l.get("tags") or slipedit.leg_tags(l)) for l in base)
        cur = slipedit.total_odds(base)
        rules = {"lo": cur * 0.85, "hi": cur * 1.25, "target": cur, "min_p": 0.75, "min_odds": 1.04, "max_odds": 10.0,
                 "no_unders": no_unders, "strict": False, "label": "slip", "top_only": True}
        kos = [slipedit._kick(l) for l in base]
        from services import TZ
        today = now.astimezone(TZ).date()
        day = "today" if all(k and k.astimezone(TZ).date() == today for k in kos) else "48h"
        cands = await self.s.scanner.edit_candidates("custom", day, min_p=0.75, allow_unders=not no_unders)
        if op.get("action") == "change_market" and idx is not None and 0 <= idx < len(base):
            eid = base[idx].get("event_id")
            if not any(c.get("event_id") == eid for c in cands):
                cands += await self._event_cands(eid)
        r = slipedit.apply_edit(base, op, cands, rules)
        if not r["ok"]:
            return self._fail(ctx, r["note"])
        new = {"tier": "custom", "label": "Edited slip", "target": None, "legs": r["legs"],
               "total_odds": r["total_odds"], "est_prob": r["est_prob"], "reached": True, "note": "", "edited": True,
               "based_on": ctx.get("code") or ""}
        sid = ai.remember_slip(new)
        return {"ok": True, "section": ctx.get("section"), "sid": sid, "slip": new, "legs": r["legs"],
                "total_odds": r["total_odds"], "est_prob": r["est_prob"], "change": r["change"],
                "note": r.get("note", ""), "action": op.get("action"), "based_on": ctx.get("code") or ""}

    async def _event_cands(self, event_id: str) -> list[dict]:
        from scanner import apply_stats
        try:
            e = await self.s.find_event(event_id)
            mk = await self.s.bookmaker.event_markets(event_id) if e else None
            if not mk:
                return []
            form = await self.s.match_form(e)
            legs = self.s.scanner.event_legs(e, mk)
            for l in legs:
                apply_stats(l, form)
            return [l.to_json() for l in legs]
        except Exception as exc:
            log.warning("event candidates failed: %s", exc)
            return []

    # ---- reference of an outcome (for the message that shows it) ----------------------------
    @staticmethod
    def outcome_ref(out: dict, ctx: dict) -> dict | None:
        if not out.get("ok"):
            return None
        sec = out.get("section")
        if sec == "rollover_preview":
            return {"section": "rollover_preview", "token": out["token"], "slip_id": out["slip_id"],
                    "plan_id": out.get("plan_id"), "day": out.get("day"), "target": out.get("target"),
                    "code": out.get("old_code"), "legs": out["legs"]}
        return {"section": "scan" if sec == "scan" else "ai", "sid": out.get("sid"), "legs": out["legs"],
                "slip": {k: v for k, v in (out.get("slip") or {}).items() if k != "legs"}}
