"""AI analyst: Google Gemini with function calling over the bot's own tools (fixtures, predictions, team stats,
market scanner, booking codes). Short per-chat history in SQLite. Everything it says is an estimate."""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime

import httpx

from bookmakers import BookingError, Selection
from markets import valid_token
from scanner import TIER_ALIASES, apply_stats
from services import TZ, Services

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta"
MODEL_PREFS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-flash-lite",
               "gemini-2.0-flash-lite"]
HISTORY_TURNS = 10
MAX_ROUNDS = 6
DISCLAIMER = "Estimates, not guarantees. 18+, bet responsibly."

SYSTEM = """You are Fra, a sharp, concise football betting analyst inside a private Telegram bot for one user in
Nigeria. Bookmaker: SportyBet Nigeria. Times are Africa/Lagos. Today is {today}.

Rules:
- Use tools for anything about real fixtures, odds, probabilities, team form/H2H or booking codes. Never invent
  matches, odds, stats or codes. If a tool fails or returns nothing, say so plainly.
- "Analyze games and give me a safe 5 odds code" => call scan_markets(tier="safe", target_odds=5, day=...), then
  build_booking_code(slip_id=...) and give the code. Tiers: safe (legs est. >=85%, odds ~1.05-1.35), mid
  (>=70%, 1.30-1.70), risk (>=55%, 1.60-2.30), high (value legs 2.0+), audit (full report).
- "Who wins X vs Y?" => predict_match. Mention the key numbers (1X2 %, best pick) and the form/H2H facts.
- Each leg already passed a team-stats check (form, home/away record, goals, H2H); quote its short "why".
- If a slip falls short of the target, say so honestly instead of padding it.
- Be brief: Telegram-friendly, short lines, light **bold**, no tables, no headings. Show a slip as one line per leg:
  kickoff · match · pick @odds (est %). Then total odds, est. hit chance, booking code.
- Never call anything a sure win, lock or guaranteed. "Safe" means lower-risk. Combined chance assumes the legs are
  independent. You can answer general football or SportyBet questions from knowledge, flagging uncertainty.
- Load codes at https://www.sportybet.com/ng/m/code-hub/load-code (or Load Code in the SportyBet app)."""

TOOLS = [{"functionDeclarations": [
    {"name": "list_matches",
     "description": "Upcoming football matches on SportyBet with 1X2 odds. Filter by day, league and/or team.",
     "parameters": {"type": "object", "properties": {
         "day": {"type": "string", "enum": ["today", "tomorrow", "next3"], "description": "default today"},
         "league": {"type": "string", "description": "league/country text, e.g. 'Premier League', 'Spain'"},
         "team": {"type": "string", "description": "team name"},
         "limit": {"type": "integer", "description": "max matches (default 20)"}}}},
    {"name": "predict_match",
     "description": ("Full analysis of one match: model probabilities (1X2, O/U 2.5, BTTS, double chance), best pick, "
                     "team form, home/away records, goals and H2H, plus the strongest priced legs across all markets."),
     "parameters": {"type": "object", "properties": {
         "match": {"type": "string", "description": "'Team A vs Team B', one team name, or an event id sr:match:..."}},
         "required": ["match"]}},
    {"name": "team_stats",
     "description": "Recent form (last 5/10), home/away records, goals for/against, over 2.5 / BTTS rates and H2H for a match.",
     "parameters": {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]}},
    {"name": "scan_markets",
     "description": ("Scan every bookable market of up to 80 matches, check shortlisted matches against team stats, and "
                     "build an accumulator for a risk tier to a target total odds (one pick per match). Returns a "
                     "slip_id for booking. tier=audit returns best leg per match, top value bets and a slip per tier."),
     "parameters": {"type": "object", "properties": {
         "tier": {"type": "string", "enum": ["safe", "mid", "risk", "high", "audit"]},
         "target_odds": {"type": "number", "description": "total odds target (defaults: safe 5, mid 10, risk 20, high 50)"},
         "day": {"type": "string", "enum": ["today", "tomorrow", "24h", "48h"], "description": "default today"},
         "leagues": {"type": "array", "items": {"type": "string"}, "description": "optional league filters"}},
         "required": ["tier"]}},
    {"name": "build_booking_code",
     "description": "Create a SportyBet booking code from a scanned slip (slip_id) or explicit selections.",
     "parameters": {"type": "object", "properties": {
         "slip_id": {"type": "string"},
         "selections": {"type": "array", "items": {"type": "object", "properties": {
             "event_id": {"type": "string"}, "market_id": {"type": "string"}, "specifier": {"type": "string"},
             "outcome_id": {"type": "string"}}}}}}},
]}]


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if isinstance(x, (int, float)) else "n/a"


def compact_leg(l: dict) -> dict:
    return {"kickoff": l["kickoff_local"], "match": l["match"], "league": l["league"], "market": l["market"],
            "pick": l["pick"], "odds": l["odds"], "est": _pct(l["est"]), "stats_check": l["verdict"],
            "why": l["why"]}


class Analyst:
    def __init__(self, services: Services, api_key: str | None = None):
        self.s = services
        self.key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        self.model = os.environ.get("GEMINI_MODEL", "").strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self.slips: dict[str, dict] = {}
        self._models_checked = False
        self.s.storage._exec("CREATE TABLE IF NOT EXISTS ai_history (chat_id INTEGER, role TEXT, text TEXT, ts REAL)")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- slips registry (shared with /scan and the Mini App) ------------
    def remember_slip(self, slip: dict) -> str:
        sid = uuid.uuid4().hex[:8]
        self.slips[sid] = {"ts": time.time(), "slip": slip}
        for k in [k for k, v in self.slips.items() if time.time() - v["ts"] > 6 * 3600]:
            self.slips.pop(k, None)
        slip["slip_id"] = sid
        return sid

    # ---- history ----------------------------------------------------------
    def history(self, chat_id: int) -> list[dict]:
        rows = self.s.storage._exec("SELECT role, text FROM ai_history WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                                    (chat_id, HISTORY_TURNS))
        return [{"role": r, "parts": [{"text": t}]} for r, t in reversed(rows)]

    def _save(self, chat_id: int, role: str, text: str) -> None:
        self.s.storage._exec("INSERT INTO ai_history VALUES (?,?,?,?)", (chat_id, role, text[:4000], time.time()))
        self.s.storage._exec("DELETE FROM ai_history WHERE chat_id=? AND ts NOT IN (SELECT ts FROM ai_history "
                             "WHERE chat_id=? ORDER BY ts DESC LIMIT ?)", (chat_id, chat_id, HISTORY_TURNS * 2))

    def reset(self, chat_id: int) -> None:
        self.s.storage._exec("DELETE FROM ai_history WHERE chat_id=?", (chat_id,))

    # ---- Gemini -----------------------------------------------------------
    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": self.key})
        r.raise_for_status()
        return [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                if "generateContent" in (m.get("supportedGenerationMethods") or [])]

    async def _models(self) -> list[str]:
        if self.model:
            return [self.model] + [m for m in MODEL_PREFS if m != self.model]
        if not self._models_checked:
            self._models_checked = True
            try:
                avail = set(await self.list_models())
                picks = [m for m in MODEL_PREFS if m in avail]
                if picks:
                    self.model = picks[0]
                    log.info("Gemini model: %s", self.model)
                    return picks
            except Exception as exc:
                log.warning("Gemini models list failed: %s", exc.__class__.__name__)
        return MODEL_PREFS

    async def _generate(self, contents: list[dict]) -> dict:
        body = {"systemInstruction": {"parts": [{"text": SYSTEM.format(
                    today=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M"))}]},
                "contents": contents, "tools": TOOLS,
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048}}
        last = None
        errs = []
        for model in await self._models():
            cfg = dict(body)
            if "2.5" in model:
                cfg["generationConfig"] = {**body["generationConfig"], "thinkingConfig": {"thinkingBudget": 512}}
            try:
                r = await self._client.post(f"{API}/models/{model}:generateContent", json=cfg,
                                            headers={"x-goog-api-key": self.key})
            except httpx.HTTPError as exc:
                last = f"network error ({exc.__class__.__name__})"
                continue
            if r.status_code in (404, 429, 500, 503):
                last = f"{model}: HTTP {r.status_code}"
                errs.append(f"{model}: {r.status_code} {r.text[:160]}")
                log.warning("Gemini %s -> %s %s", model, r.status_code, r.text[:300])
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
            self.model = model
            return r.json()
        self.last_errors = errs
        raise RuntimeError(f"Gemini unavailable ({last})")

    async def ask(self, chat_id: int, text: str) -> dict:
        """Returns {reply, slip, code, tools}."""
        if not self.enabled:
            return {"reply": "The AI analyst isn't configured (GEMINI_API_KEY missing).", "slip": None, "code": None}
        contents = self.history(chat_id) + [{"role": "user", "parts": [{"text": text}]}]
        turn = {"slip": None, "code": None, "tools": []}
        reply = ""
        for _ in range(MAX_ROUNDS):
            data = await self._generate(contents)
            cand = (data.get("candidates") or [{}])[0]
            content = cand.get("content") or {"role": "model", "parts": []}
            parts = content.get("parts") or []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                reply = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                break
            contents.append({"role": "model", "parts": parts})
            responses = []
            for c in calls:
                name, args = c.get("name", ""), c.get("args") or {}
                turn["tools"].append(name)
                try:
                    result = await self.run_tool(name, args, turn)
                except Exception as exc:
                    log.warning("tool %s failed: %s", name, exc)
                    result = {"error": f"{name} failed: {exc.__class__.__name__}: {str(exc)[:160]}"}
                responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
            contents.append({"role": "user", "parts": responses})
        if not reply:
            reply = "I couldn't finish that analysis. Try a simpler question."
        if turn["code"] and turn["code"] not in reply:
            reply += f"\n\nBooking code: `{turn['code']}`"
        if "estimate" not in reply.lower() and "guarantee" not in reply.lower():
            reply += f"\n\n_{DISCLAIMER}_"
        self._save(chat_id, "user", text)
        self._save(chat_id, "model", reply)
        return {"reply": reply, "slip": turn["slip"], "code": turn["code"], "tools": turn["tools"],
                "model": self.model}

    # ---- tools ------------------------------------------------------------
    async def _find(self, match: str):
        match = (match or "").strip()
        if match.startswith("sr:match:"):
            return await self.s.find_event(match)
        ranked = await self.s.search(match, limit=3)
        if ranked and ranked[0][0] >= 60:
            return ranked[0][1]
        return None

    async def run_tool(self, name: str, a: dict, turn: dict) -> dict:
        if name == "list_matches":
            day = a.get("day") or "today"
            if day == "next3":
                evs = await self.s.events("u")
            elif day == "tomorrow":
                evs = await self.s.scanner.window_events("tomorrow")
            else:
                evs = await self.s.events("t")
            if a.get("league"):
                q = a["league"].lower()
                evs = [e for e in evs if q in e.league_full.lower()]
            if a.get("team"):
                from matching import team_score
                evs = [e for e in evs if max(team_score(a["team"], e.home), team_score(a["team"], e.away)) >= 80]
            evs = [e for e in evs if "srl" not in e.league_full.lower() and "simulated" not in e.league_full.lower()]
            from scanner import _prio
            evs.sort(key=lambda e: (_prio(e)[:2], e.start))
            lim = max(1, min(int(a.get("limit") or 20), 40))
            return {"count": len(evs), "matches": [{
                "event_id": e.event_id, "kickoff": e.start.astimezone(TZ).strftime("%a %d %b %H:%M"),
                "match": e.name, "league": e.league_full,
                "odds_1x2": [e.odds("1", "1"), e.odds("1", "2"), e.odds("1", "3")]} for e in evs[:lim]]}
        if name in ("predict_match", "team_stats"):
            e = await self._find(a.get("match", ""))
            if not e:
                return {"error": "Match not found in SportyBet's next 3 days."}
            form = await self.s.match_form(e)
            out = {"event_id": e.event_id, "match": e.name, "league": e.league_full,
                   "kickoff": e.start.astimezone(TZ).strftime("%a %d %b %H:%M"),
                   "stats": form.to_json() if form else "stats feed unavailable for this match"}
            if name == "team_stats":
                return out
            p = await self.s.predict(e)
            if p:
                pr = p.probs
                out.update({"model": p.source, "xg": [round(x, 2) for x in p.xg] if p.xg else None,
                            "probs": {"home": _pct(pr[("1", "1")]), "draw": _pct(pr[("1", "2")]),
                                      "away": _pct(pr[("1", "3")]), "over2.5": _pct(pr[("18", "12")]),
                                      "btts_yes": _pct(pr[("29", "74")]), "1X": _pct(pr[("10", "9")]),
                                      "X2": _pct(pr[("10", "11")])},
                            "odds_1x2": [e.odds("1", "1"), e.odds("1", "2"), e.odds("1", "3")],
                            "best_pick": ({"pick": p.best.label, "prob": _pct(p.best.prob), "odds": p.best.odds}
                                          if p.best else None), "reasoning": p.reasoning})
            mk = await self.s.bookmaker.event_markets(e.event_id)
            if mk:
                legs = self.s.scanner.event_legs(e, mk)
                for l in legs:
                    apply_stats(l, form)
                strong = sorted([l for l in legs if l.verdict != "dropped" and l.odds >= 1.2],
                                key=lambda l: -l.conf)[:6]
                out["strongest_legs"] = [{"market": l.market, "pick": l.pick, "odds": l.odds, "est": _pct(l.est),
                                          "stats_check": l.verdict} for l in strong]
            return out
        if name == "scan_markets":
            tier = TIER_ALIASES.get(str(a.get("tier") or "safe").lower(), "safe")
            target = a.get("target_odds")
            res = await self.s.scanner.scan(tier, float(target) if target else None, a.get("day") or "today",
                                            a.get("leagues") or None)
            if tier == "audit":
                for sl in res["slips"].values():
                    self.remember_slip(sl)
                turn["slip"] = res
                return {"scanned": res["scanned"], "with_stats": res["with_stats"],
                        "dropped_by_stats": res["dropped_by_stats"],
                        "best_per_match": [compact_leg(l) for l in res["best_per_match"][:10]],
                        "top_value": [compact_leg(l) for l in res["top_value"][:6]],
                        "slips": {k: {"slip_id": v["slip_id"], "total_odds": v["total_odds"],
                                      "est_hit": _pct(v["est_prob"]), "legs": len(v["legs"]), "note": v["note"]}
                                  for k, v in res["slips"].items()}}
            sid = self.remember_slip(res)
            turn["slip"] = res
            return {"slip_id": sid, "tier": res["label"], "target": res["target"], "total_odds": res["total_odds"],
                    "est_hit_chance": _pct(res["est_prob"]), "reached_target": res["reached"],
                    "note": res["note"], "matches_scanned": res["scanned"], "with_stats": res["with_stats"],
                    "legs": [compact_leg(l) for l in res["legs"]]}
        if name == "build_booking_code":
            sels = []
            if a.get("slip_id") and a["slip_id"] in self.slips:
                slip = self.slips[a["slip_id"]]["slip"]
                sels = [Selection(l["event_id"], l["market_id"], l["specifier"], l["outcome_id"])
                        for l in slip["legs"]]
            elif a.get("selections"):
                for x in a["selections"][:30]:
                    vals = [str(x.get(k) or "") for k in ("event_id", "market_id", "specifier", "outcome_id")]
                    if all(valid_token(v) for v in vals) and vals[0] and vals[1] and vals[3]:
                        sels.append(Selection(*vals))
            if not sels:
                return {"error": "No selections: run scan_markets first and pass its slip_id."}
            try:
                code = await self.s.bookmaker.book(sels)
            except BookingError as exc:
                return {"error": str(exc)}
            turn["code"] = code
            check = await self.s.bookmaker.load_code(code)
            return {"code": code, "selections": len(sels), "verified_on_load": check,
                    "load_at": self.s.bookmaker.load_code_url}
        return {"error": f"unknown tool {name}"}


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_IT = re.compile(r"(?<![\w*])_(.+?)_(?!\w)", re.S)
_MD_CODE = re.compile(r"`([^`]+)`")


def md_to_html(text: str) -> str:
    """Tiny, safe Markdown -> Telegram HTML (bold, italics, inline code, bullets)."""
    import html
    t = html.escape(text, quote=False)
    t = _MD_CODE.sub(r"<code>\1</code>", t)
    t = _MD_BOLD.sub(r"<b>\1</b>", t)
    t = _MD_IT.sub(r"<i>\1</i>", t)
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", t)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    return t
