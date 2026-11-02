"""AI analyst: Google Gemini with function calling over the bot's own tools (fixtures, predictions, team stats,
market scanner, booking codes). Short per-chat history in SQLite. Everything it says is an estimate."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime

import httpx

from bookmakers import BookingError, Selection
from markets import valid_token
from constraints import MARKET_TYPES, Constraints, parse_text, verify
from scanner import TIER_ALIASES, apply_stats
from services import TZ, Services

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta"
# 2.5 models are closed to new API keys (404 "no longer available to new users", checked 2026-10-31).
MODEL_PREFS = ["gemini-3.5-flash", "gemini-flash-latest", "gemini-3-flash-preview", "gemini-3.5-flash-lite",
               "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash"]
HISTORY_TURNS = 10
MAX_ROUNDS = 6
DISCLAIMER = "Estimates, not guarantees. 18+, bet responsibly."

SYSTEM = """You are Fra, a sharp, concise football betting analyst inside a private Telegram bot for one user in
Nigeria. Bookmaker: SportyBet Nigeria. Times are Africa/Lagos. Today is {today}.

Rules:
- Use tools for anything about real fixtures, odds, probabilities, team form/H2H or booking codes. Never invent
  matches, odds, stats or codes. If a tool fails or returns nothing, say so plainly.
- "Analyze games and give me a safe 5 odds code" => call scan_markets(tier="safe", target_odds=5, day=...), then
  build_booking_code(slip_id=...) and give the code. Tiers: safe (EVERY leg est. >=80% after the stats check,
  odds ~1.05-1.35; 80% is an estimate, not a promised accuracy), mid
  (>=70%, 1.30-1.70), risk (>=55%, 1.60-2.30), high (value legs 2.0+), audit (full report).
- FOLLOW THE USER'S INSTRUCTIONS EXACTLY. Translate every rule in the request into scan_markets arguments:
  target_odds, tier, min_leg_prob, min_legs/max_legs, min_leg_odds/max_leg_odds, include_markets/exclude_markets
  (market types: under, over, totals, team_totals, 1x2, double_chance, dnb, handicap, btts, corners, cards,
  player_props, halves, combo, correct_score, exact_goals, odd_even, minutes, specials), include/exclude leagues,
  countries, teams, day, kickoff_after/kickoff_before (local HH:MM). "Sure", "safe", "banker", "low risk" = tier
  safe (every leg est. >=80%). "Without unders / no under selections" => exclude_markets=["under"] (this removes
  every Under line: match, team, half, corners, cards, Asian, and combos containing an Under). "Only overs" =>
  include_markets=["over"]. Example: "give me sure 5 odds without under selections" => scan_markets(tier="safe",
  target_odds=5, exclude_markets=["under"]).
- The scanner enforces those rules and returns constraint_check (ok, violations, target_reached). Before you reply
  or book, read it: only present/book a slip whose constraint_check.ok is true, and state the rules you applied in
  one short line. If target_reached is false or no legs qualify, say plainly that the rules can't be met at the
  safety bar right now (give what it reached) and offer options (lower target, relax a rule, another day). Never
  break or quietly drop a rule the user gave, and never swap in weaker legs to hit the odds.
- Scans use only the TOP LEAGUES by default (big-5 leagues + cups, UEFA club cups, Eredivisie, Primeira Liga,
  Championship, Scottish Prem, Süper Lig, Belgian Pro League, MLS, Brasileirão, Argentine Primera, major national-team
  competitions). Widen only when the user names a league/country/team (include_leagues/countries/teams) or explicitly
  asks for all leagues (all_leagues=true).
- A SportyBet booking code to check ("analyze/check/rate this code N6RXGP") => analyze_booking_code. Lead with its verdict line, then the grade,
  total odds, combined chance, each leg's rating in one short line, the weakest legs and the suggested swaps/drops,
  and flag legs outside the top leagues. Never book the improved slip yourself: a button under your reply does that.
- "Who wins X vs Y?" => predict_match. Mention the key numbers (1X2 %, best pick) and the form/H2H facts.
- Each leg already passed a team-stats check (form, home/away record, goals, H2H); quote its short "why".
- If a slip falls short of the target, say so honestly instead of padding it. Never add a weaker leg to a Safe slip
  by hand: book Safe slips only via their slip_id (or pass tier="safe" so every leg is re-checked >=80%).
- Booked codes are saved to the history and tracked; the owner gets a ping when a slip lands or loses.
  recent_history shows past slips and results; rollover plans are managed with /rollover or the Rollover tab.
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
                     "build an accumulator for a risk tier to a target total odds (one pick per match) that obeys "
                     "every constraint given. Returns a slip_id for booking and constraint_check. tier=audit returns "
                     "best leg per match, top value bets and a slip per tier."),
     "parameters": {"type": "object", "properties": {
         "tier": {"type": "string", "enum": ["safe", "mid", "risk", "high", "audit"],
                  "description": "safe = every leg est. >=80% ('sure', 'safe', 'banker'); mid >=70%; risk >=55%; high = value"},
         "target_odds": {"type": "number", "description": "total odds target (defaults: safe 5, mid 10, risk 20, high 50)"},
         "day": {"type": "string", "enum": ["today", "tomorrow", "24h", "48h"], "description": "default today"},
         "leagues": {"type": "array", "items": {"type": "string"}, "description": "legacy: league text filters for which matches to scan"},
         "min_leg_prob": {"type": "number", "description": "minimum estimated probability per leg, 0-1 (e.g. 0.85)"},
         "min_legs": {"type": "integer"}, "max_legs": {"type": "integer"},
         "min_leg_odds": {"type": "number"}, "max_leg_odds": {"type": "number"},
         "include_markets": {"type": "array", "items": {"type": "string", "enum": MARKET_TYPES},
                             "description": "only legs of these market types"},
         "exclude_markets": {"type": "array", "items": {"type": "string", "enum": MARKET_TYPES},
                             "description": "never legs of these market types; 'under' removes every Under selection"},
         "include_leagues": {"type": "array", "items": {"type": "string"},
                             "description": "e.g. 'Premier League', 'LaLiga', 'Champions League', or 'top leagues'"},
         "exclude_leagues": {"type": "array", "items": {"type": "string"}},
         "include_countries": {"type": "array", "items": {"type": "string"}, "description": "e.g. 'England', 'Spain'"},
         "exclude_countries": {"type": "array", "items": {"type": "string"}},
         "include_teams": {"type": "array", "items": {"type": "string"}, "description": "only matches involving these teams"},
         "exclude_teams": {"type": "array", "items": {"type": "string"}},
         "kickoff_after": {"type": "string", "description": "local time HH:MM (Africa/Lagos), or 'YYYY-MM-DD HH:MM'"},
         "kickoff_before": {"type": "string", "description": "local time HH:MM (Africa/Lagos), or 'YYYY-MM-DD HH:MM'"},
         "stats_confirmed_only": {"type": "boolean", "description": "only legs confirmed by the team-stats check"},
         "all_leagues": {"type": "boolean",
                         "description": "true ONLY if the user explicitly asks for all / any / lower leagues; scans "
                                        "use the top-leagues allowlist by default"}},
         "required": ["tier"]}},
    {"name": "build_booking_code",
     "description": ("Create a SportyBet booking code from a scanned slip (slip_id) or explicit selections. The code "
                     "is saved to the history and its result tracked."),
     "parameters": {"type": "object", "properties": {
         "slip_id": {"type": "string"},
         "tier": {"type": "string", "enum": ["safe", "mid", "risk", "high", "custom"],
                  "description": "for explicit selections: 'safe' re-checks every leg is est. >=80% and refuses weaker legs"},
         "accept_short": {"type": "boolean",
                          "description": "true only if the user agreed to book a slip that fell short of its target"},
         "selections": {"type": "array", "items": {"type": "object", "properties": {
             "event_id": {"type": "string"}, "market_id": {"type": "string"}, "specifier": {"type": "string"},
             "outcome_id": {"type": "string"}}}}}}},
    {"name": "analyze_booking_code",
     "description": ("Load any SportyBet NG booking code (6 characters, e.g. N6RXGP) and analyze the slip: every leg's "
                     "estimated probability (same calibrated model as the scanner), fair odds, value, risk rating "
                     "Strong/OK/Weak/Avoid with a reason (form, H2H, goals trend, team news), legs outside the top "
                     "leagues, total odds, combined hit chance, grade A-F, legs to drop and safer swaps. Never books "
                     "anything: the user rebooks the improved slip with a button."),
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string", "description": "the booking code"},
         "rules": {"type": "string", "description": "optional rules for the suggested swaps, e.g. 'no unders'"}},
         "required": ["code"]}},
    {"name": "recent_history",
     "description": "Recently booked codes with their tier, total odds and result (pending / won / lost per leg).",
     "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
]}]


EDIT_RULES = """

REPLY CONTEXT: the user is replying to a slip the bot showed (given above their message, with its section, rules and
numbered legs). Treat the message as an edit of THAT slip unless it is plainly just a question about it.
- Call edit_slip: action replace (change/swap a leg), remove, add, change_market (other market on the same match),
  safer (stronger pick; with no leg = the weakest leg), rebuild (whole new set of picks). leg = the 1-based number in the
  list ("first" = 1, "last" = the highest number); or pass team when the user names a team. market = the market words
  they asked for ("over 1.5", "double chance").
- Understand casual and Nigerian English: "comot", "remove am", "change am", "abeg", "e no sure", "this one no go enter".
- The section's rules are enforced by the tool (rollover: target odds, top leagues, every leg >=80%, no unders unless the
  plan allows them, no started matches, no duplicate matches). If the tool says it's impossible, tell the user plainly
  and that the current slip stays. Never invent legs, never scan for an unrelated slip, never book: the bot shows the
  edited slip under the user's message with its own confirm / book buttons.
- If they ask for two changes, call edit_slip once per change (each call edits the result of the previous one).
- After the tool, reply in at most two short lines (what changed, new total). Do not list the legs again."""

EDIT_TOOLS = [{"functionDeclarations": [
    {"name": "edit_slip",
     "description": ("Edit the slip the user is replying to, under that slip's own rules, and return the edited slip "
                     "(not booked). Fails plainly when no qualifying pick exists."),
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["replace", "remove", "add", "change_market", "safer", "rebuild"]},
         "leg": {"type": "integer", "description": "1-based leg number in the slip as listed"},
         "team": {"type": "string", "description": "team name the user used to point at a leg"},
         "market": {"type": "string", "description": "target market for change_market, e.g. 'over 1.5', 'double chance'"}},
         "required": ["action"]}},
    {"name": "predict_match", "description": "Full analysis of one match (probabilities, form, H2H).",
     "parameters": {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]}},
    {"name": "team_stats", "description": "Recent form, home/away records, goals and H2H for a match.",
     "parameters": {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]}},
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

    async def _generate(self, contents: list[dict], system: str | None = None, tools=True,
                        max_tokens: int = 8192, timeout: float | None = None, gen_extra: dict | None = None) -> dict:
        body = {"systemInstruction": {"parts": [{"text": system or SYSTEM.format(
                    today=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M"))}]},
                "contents": contents, "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens,
                                                           **(gen_extra or {})}}
        if tools:
            body["tools"] = TOOLS if tools is True else tools
        last = None
        errs = []
        for model in await self._models():
            variants = []
            if "gemini-3" in model or "latest" in model:
                variants.append({**body["generationConfig"], "thinkingConfig": {"thinkingLevel": "low"}})
            elif "2.5" in model:
                variants.append({**body["generationConfig"], "thinkingConfig": {"thinkingBudget": 512}})
            variants.append(body["generationConfig"])
            for vi, gen in enumerate(variants):
                r = None
                for attempt in range(2):
                    try:
                        r = await self._client.post(f"{API}/models/{model}:generateContent",
                                                    json={**body, "generationConfig": gen},
                                                    headers={"x-goog-api-key": self.key},
                                                    **({"timeout": httpx.Timeout(timeout, connect=10.0)}
                                                       if timeout else {}))
                    except httpx.HTTPError as exc:
                        last = f"network error ({exc.__class__.__name__})"
                        r = None
                        break
                    if r.status_code == 503 and attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    break
                if r is None:
                    break
                if r.status_code == 400 and vi < len(variants) - 1:
                    continue   # thinking config not accepted by this model: retry without it
                if r.status_code in (404, 429, 500, 503):
                    last = f"{model}: HTTP {r.status_code}"
                    errs.append(f"{model}: {r.status_code} {r.text[:160]}")
                    log.warning("Gemini %s -> %s %s", model, r.status_code, r.text[:300])
                    break
                if r.status_code != 200:
                    raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
                self.model = model
                self.last_errors = errs
                return r.json()
        self.last_errors = errs
        raise RuntimeError(f"Gemini unavailable ({last})")

    async def _loop(self, contents: list[dict], turn: dict, tools=True, system: str | None = None,
                    timeout: float | None = None) -> str:
        """Gemini function-calling loop. Returns the final text ('' only when every recovery failed).
        Why replies came back empty before (2026-11-02, "I couldn't finish that analysis"): Gemini 3 counts its
        thinking against maxOutputTokens (was 2048), so after a large scan result it could end with
        finishReason MAX_TOKENS and no text; MALFORMED_FUNCTION_CALL / empty candidates and running out of rounds
        ended the same way. Now: 8192-token budget, finishReason logged, and one tool-free "answer now" round."""
        dbg = {"rounds": 0, "finish": [], "tools": turn["tools"]}
        self.last_turn = dbg
        reply = ""
        for _ in range(MAX_ROUNDS):
            dbg["rounds"] += 1
            t0 = time.time()
            data = await self._generate(contents, system=system, tools=tools, **({"timeout": timeout} if timeout else {}))
            dbg.setdefault("gemini_s", []).append(round(time.time() - t0, 1))
            cand = (data.get("candidates") or [{}])[0]
            dbg["finish"].append(cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "?")
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
                t0 = time.time()
                try:
                    result = await self.run_tool(name, args, turn)
                except Exception as exc:
                    log.warning("tool %s failed: %s", name, exc)
                    result = {"error": f"{name} failed: {exc.__class__.__name__}: {str(exc)[:160]}"}
                dbg.setdefault("tool_s", []).append(f"{name} {time.time() - t0:.1f}")
                responses.append({"functionResponse": {"name": name, "response": {"result": result}}})
            contents.append({"role": "user", "parts": responses})
        if not reply:
            # recovery: one more round without tools, asking for the answer from what is already there
            dbg["recovery"] = True
            log.warning("Gemini gave no text (finish=%s, tools=%s): asking for the final answer", dbg["finish"],
                        turn["tools"])
            try:
                data = await self._generate(contents + [{"role": "user", "parts": [{"text": (
                    "Write your final answer to my last message now, briefly, using only the tool results above. "
                    "No more tool calls.")}]}], system=system, tools=None, **({"timeout": timeout} if timeout else {}))
                cand = (data.get("candidates") or [{}])[0]
                dbg["finish"].append(cand.get("finishReason") or "?")
                parts = (cand.get("content") or {}).get("parts") or []
                reply = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            except Exception as exc:
                log.warning("Gemini recovery round failed: %s", exc)
        dbg["empty"] = not reply
        return reply

    @staticmethod
    def _slip_summary(sl: dict) -> str:
        """Plain reply built from a scanned slip when the model wrote nothing."""
        if not sl or not sl.get("legs"):
            return ""
        lines = [f"Here's the {sl.get('label', 'slip')} slip I built:"]
        for l in sl["legs"]:
            lines.append(f"• {l.get('kickoff_local', '')[4:]} · {l.get('match', '')} · {l.get('pick', '')} "
                         f"@{float(l.get('odds') or 0):.2f} ({_pct(l.get('est'))})")
        lines.append(f"Total odds **{sl.get('total_odds', 0):.2f}** · est. hit chance {_pct(sl.get('est_prob'))}")
        if sl.get("note"):
            lines.append(sl["note"])
        return "\n".join(lines)

    async def ask(self, chat_id: int, text: str, context: dict | None = None) -> dict:
        """Returns {reply, slip, code, tools, edit}. context = the slip the user is replying to (editflow.py):
        the message is then treated as an edit of THAT slip and answered inside its section."""
        if context and context.get("legs"):
            return await self._ask_edit(chat_id, text, context)
        if not self.enabled:
            return {"reply": "The AI analyst isn't configured (GEMINI_API_KEY missing).", "slip": None, "code": None}
        contents = self.history(chat_id) + [{"role": "user", "parts": [{"text": text}]}]
        turn = {"slip": None, "code": None, "tools": [], "hard": parse_text(text), "analysis": None}
        reply = await self._loop(contents, turn)
        if not reply:
            reply = self._slip_summary(turn["slip"]) if turn.get("slip") and turn["slip"].get("tier") != "audit" else ""
        if not reply:
            reply = ("I didn't get an answer back from the AI model this time, so nothing was built or booked. "
                     "Send it again, or use /scan safe 5 for a slip right away.")
        reply += self._rules_footer(turn)
        if turn["code"] and turn["code"] not in reply:
            reply += f"\n\nBooking code: `{turn['code']}`"
        if "estimate" not in reply.lower() and "guarantee" not in reply.lower():
            reply += f"\n\n_{DISCLAIMER}_"
        self._save(chat_id, "user", text)
        self._save(chat_id, "model", reply)
        an = turn.get("analysis")
        return {"reply": reply, "slip": turn["slip"], "code": turn["code"], "tools": turn["tools"],
                "model": self.model, "analysis": an}

    async def _ask_edit(self, chat_id: int, text: str, ctx: dict) -> dict:
        """Edit of the slip the user replied to. Gemini gets the slip and the edit_slip tool (no scan / booking
        tools); if it fails or doesn't edit, a rule-based parse of the message does the edit. Never falls back to
        an unrelated new slip, never books."""
        from slipedit import parse_edit
        ed = getattr(self.s, "editor", None)
        op = parse_edit(text, ctx["legs"])
        turn = {"slip": None, "code": None, "tools": [], "hard": None, "analysis": None, "edit": None,
                "ctx": dict(ctx), "edit_ops": 0}
        reply = ""
        sec_label = {"rollover_preview": "rollover preview", "rollover_day": "rollover day"}.get(ctx.get("section"),
                                                                                                ctx.get("section"))
        if self.enabled and ed is not None:
            ctx_txt = ed.context_text(ctx)
            contents = self.history(chat_id) + [{"role": "user", "parts": [{"text": (
                f"[I'm replying to this slip]\n{ctx_txt}\n\n[My message]\n{text}")}]}]
            system = SYSTEM.format(today=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M")) + EDIT_RULES
            warm = asyncio.create_task(ed.prefetch(ctx))       # candidate pool warms while Gemini reads the request
            try:
                reply = await asyncio.wait_for(self._loop(contents, turn, tools=EDIT_TOOLS, system=system, timeout=25),
                                               timeout=60)
            except Exception as exc:
                log.warning("AI edit loop failed (%s): using the rule-based edit", exc.__class__.__name__)
                reply = ""
                turn["edit_tool_called"] = False      # a cut-off loop: let the rule-based edit decide
            self._bg = getattr(self, "_bg", set())
            self._bg.add(warm)                 # let a running pool build finish (cancelling it would waste it)
            warm.add_done_callback(self._bg.discard)
        called = turn.get("edit_tool_called", "edit_slip" in turn["tools"])
        use_rules = op is not None and ed is not None and turn["edit"] is None and (
            not reply or (not called and op.get("index") is not None))
        if use_rules:
            turn["tools"].append("rule_edit")
            turn["edit"] = await ed.run(ctx, op)
        out = turn["edit"]
        if out is not None:
            reply = (f"✏️ {out['change']}" if out.get("ok") else f"⚠️ {out['note']}")
        elif not reply:
            reply = ("I couldn't tell which change you want on this slip. Try \"change the 2nd selection\", "
                     "\"remove Newcastle\", \"change leg 3 to over 1.5\" or \"make it safer\".")
        self._save(chat_id, "user", f"[reply to {sec_label} slip] {text}")
        self._save(chat_id, "model", reply)
        return {"reply": reply, "slip": None, "code": None, "tools": turn["tools"], "model": self.model,
                "analysis": None, "edit": out, "context_section": ctx.get("section")}

    async def narrate(self, facts: dict) -> str:
        """Short plain-English take on an analyzed slip, grounded only in the numbers given (no tools, no web)."""
        import json as _json
        system = ("You write a 3-4 sentence verdict on a football bet slip for a Telegram user in Nigeria. Use ONLY "
                  "the facts in the JSON: never add teams, players, injuries, stats or numbers that aren't there, and "
                  "copy percentages exactly as written. Say which legs carry the risk and what the suggestions change. "
                  "Plain text, no headings, no lists, at most 90 words. Call the numbers estimates; never promise a "
                  "win.")
        data = await self._generate([{"role": "user", "parts": [{"text": _json.dumps(facts, ensure_ascii=False)}]}],
                                    system=system, tools=False, max_tokens=2048)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if cand.get("finishReason") not in (None, "STOP") or not text.endswith((".", "!", ")", "”", '"')):
            return ""          # cut off: the caller falls back to the rule-based summary
        return text

    @staticmethod
    def _rules_footer(turn: dict) -> str:
        """Server-side check line appended when the user stated rules and a slip came back."""
        sl = turn.get("slip")
        if not sl or sl.get("tier") == "audit" or not sl.get("constraints"):
            return ""
        chk = sl.get("constraint_check") or {}
        if not chk.get("ok"):
            return ("\n\n⚠️ Rules check failed on this slip: " + "; ".join(chk.get("violations", [])[:3]) +
                    ". Don't use it; ask me to rebuild.")
        if not sl.get("legs"):
            return ""
        return (f"\n\n✅ Rules check: {sl.get('constraints_text') or 'ok'} · {len(sl['legs'])} legs · total "
                f"{sl['total_odds']:.2f}" + ("" if chk.get("target_reached") else " (short of target)"))

    # ---- tools ------------------------------------------------------------
    async def _find(self, match: str):
        match = (match or "").strip()
        if match.startswith("sr:match:"):
            return await self.s.find_event(match)
        ranked = await self.s.search(match, limit=3)
        if ranked and ranked[0][0] >= 60:
            return ranked[0][1]
        return None

    async def _price_selections(self, sels: list[dict], tier: str) -> tuple[list[dict], list[str]]:
        """Attach market / pick / odds / est to explicit selections; for tier 'safe' list every leg below 80%."""
        out, problems = [], []
        for x in sels:
            e = await self.s.find_event(x["event_id"])
            leg = None
            if e:
                mk = await self.s.bookmaker.event_markets(e.event_id)
                if mk:
                    form = await self.s.match_form(e)
                    for l in self.s.scanner.event_legs(e, mk, keep=100000):
                        if (l.market_id, l.specifier, l.outcome_id) == (x["market_id"], x["specifier"], x["outcome_id"]):
                            apply_stats(l, form)
                            leg = l.to_json()
                            break
            if leg is None:
                if tier == "safe":
                    problems.append(f"{x['event_id']} market {x['market_id']}: can't price it")
                out.append(x)
                continue
            if tier == "safe" and (leg["est"] < 0.80 or leg["verdict"] == "dropped"):
                problems.append(f"{leg['match']} {leg['pick']} est {leg['est']:.0%}")
            out.append(leg)
        return out, problems

    async def _tool_edit(self, a: dict, turn: dict) -> dict:
        from slipedit import _team_index, parse_market
        ed = getattr(self.s, "editor", None)
        ctx = turn.get("ctx")
        if ed is None or not ctx:
            return {"error": "No slip in this conversation to edit."}
        legs = ctx["legs"]
        action = str(a.get("action") or "replace")
        idx = None
        if a.get("leg") not in (None, ""):
            try:
                idx = int(a["leg"]) - 1
            except (TypeError, ValueError):
                idx = None
        if idx is None and a.get("team"):
            idx = _team_index(str(a["team"]), legs)
            if idx is None:
                return {"error": f"No leg on this slip involves {a['team']}."}
        if idx is not None and not 0 <= idx < len(legs):
            return {"error": f"The slip has {len(legs)} legs; there is no leg {idx + 1}."}
        market = parse_market("to " + str(a["market"])) if a.get("market") else None
        op = {"action": action, "index": idx, "market": market, "same_match": action == "change_market"}
        if action == "remove" and idx is None:
            op["weakest"] = True
        turn["edit_ops"] = turn.get("edit_ops", 0) + 1
        if turn["edit_ops"] > 4:
            return {"error": "Too many edits in one message; show the user what you have."}
        out = await ed.run(ctx, op)
        prev = turn.get("edit")
        if out.get("ok") or not (prev and prev.get("ok")):
            turn["edit"] = out
        if out.get("ok"):          # later calls in this turn edit the result of this one
            nref = ed.outcome_ref(out, ctx) or {}
            turn["ctx"] = {**ctx, **nref, "legs": out["legs"]}
            if out.get("section") == "rollover_preview":
                turn["ctx"]["section"] = "rollover_preview"
        return {"ok": out.get("ok"), "change": out.get("change"), "note": out.get("note"),
                "total_odds": out.get("total_odds"), "est_hit_chance": _pct(out.get("est_prob")),
                "legs": [f"{l.get('home', '')} v {l.get('away', '')}: {l.get('pick', '')} @{l.get('odds')}"
                         for l in out.get("legs") or []]}

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
            c = Constraints.from_dict(a)
            hard = turn.get("hard")
            if hard is not None:
                c.merge_hard(hard)      # rules stated in the user's own words are always enforced
            tier = c.tier or TIER_ALIASES.get(str(a.get("tier") or "safe").lower(), "safe")
            c.tier = tier
            day = c.day or (a.get("day") if a.get("day") in ("today", "tomorrow", "24h", "48h") else "today")
            c.day = day
            res = await self.s.scanner.scan(tier, c.target_odds, day, a.get("leagues") or None, c)
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
                    "applied_constraints": res.get("constraints"), "rules_text": res.get("constraints_text"),
                    "constraint_check": res.get("constraint_check"),
                    "unmapped_market_words": c.unknown or None,
                    "legs": [compact_leg(l) for l in res["legs"]]}
        if name == "build_booking_code":
            sels, legs, meta = [], [], {}
            if a.get("slip_id") and a["slip_id"] in self.slips:
                slip = self.slips[a["slip_id"]]["slip"]
                legs = list(slip["legs"])
                chk = slip.get("constraint_check")
                if chk and not chk.get("ok"):
                    return {"error": "Refused: this slip breaks the requested rules (" +
                            "; ".join(chk["violations"][:4]) + ")."}
                hard = turn.get("hard")
                if hard is not None and not hard.empty():
                    hv = verify(slip, Constraints.from_dict({k: v for k, v in hard.to_dict().items()
                                                             if k not in ("target_odds", "day", "tier")}),
                                0.80 if slip.get("tier") == "safe" else None)
                    if not hv["ok"]:
                        return {"error": "Refused: this slip breaks the rules in your request (" +
                                "; ".join(hv["violations"][:4]) + "). Run scan_markets with those rules."}
                if slip.get("legs") and not slip.get("reached", True) and not a.get("accept_short"):
                    return {"error": f"Not booked: the slip reaches {slip.get('total_odds')} odds, short of the "
                            f"{slip.get('target')} target. Tell the user and book only if they accept "
                            "(accept_short=true)."}
                meta = {"tier": slip.get("tier", "custom"), "label": slip.get("label", ""),
                        "total_odds": slip.get("total_odds"), "est_prob": slip.get("est_prob"),
                        "target": slip.get("target")}
            elif a.get("selections"):
                for x in a["selections"][:30]:
                    vals = [str(x.get(k) or "") for k in ("event_id", "market_id", "specifier", "outcome_id")]
                    if all(valid_token(v) for v in vals) and vals[0] and vals[1] and vals[3]:
                        legs.append(dict(zip(("event_id", "market_id", "specifier", "outcome_id"), vals)))
                tier = str(a.get("tier") or "custom").lower()
                legs, problems = await self._price_selections(legs, tier)
                hard = turn.get("hard")
                if hard is not None and not hard.empty() and not problems:
                    priced = [l for l in legs if "est" in l]
                    rules = Constraints.from_dict({k: v for k, v in hard.to_dict().items()
                                                   if k not in ("target_odds", "day", "tier", "min_legs", "max_legs")})
                    problems += [f"{l['match']} {l['pick']}: {rules.leg_problem(l)}" for l in priced
                                 if rules.leg_problem(l)]
                if problems:
                    return {"error": "Refused: " + "; ".join(problems[:6]) +
                            ". Every leg must clear the tier's bar (Safe = est. >=80% after the stats check) and the user's rules; "
                            "I won't add legs that break them."}
                meta = {"tier": tier, "label": tier.title()}
            if not legs:
                return {"error": "No selections: run scan_markets first and pass its slip_id."}
            sels = [Selection(l["event_id"], l["market_id"], l.get("specifier") or "", l["outcome_id"]) for l in legs]
            try:
                code = await self.s.bookmaker.book(sels)
            except BookingError as exc:
                return {"error": str(exc)}
            turn["code"] = code
            check = await self.s.bookmaker.load_code(code)
            tr = getattr(self.s, "tracker", None)
            saved = None
            if tr:
                saved = await tr.record(legs, code, "ai", meta.get("tier", "custom"), meta.get("label", ""),
                                        meta.get("total_odds"), meta.get("est_prob"), target=meta.get("target"))
            return {"code": code, "selections": len(sels), "verified_on_load": check,
                    "load_at": self.s.bookmaker.load_code_url, "saved_to_history": bool(saved)}
        if name == "analyze_booking_code":
            an = getattr(self.s, "analyzer", None)
            if an is None:
                return {"error": "analyzer unavailable"}
            from analyzer import Analyzer, extract_code
            code = extract_code(str(a.get("code") or "")) or str(a.get("code") or "").strip().upper()
            res = await an.analyze(code, str(a.get("rules") or ""))
            if res.get("ok"):
                turn["analysis"] = res
            return Analyzer.compact(res)
        if name == "edit_slip":
            return await self._tool_edit(a, turn)
        if name == "recent_history":
            tr = getattr(self.s, "tracker", None)
            if not tr:
                return {"error": "history unavailable"}
            rows = await tr.history.list("slip", None, limit=max(1, min(int(a.get("limit") or 10), 20)))
            return {"slips": [{"code": r["data"].get("code"), "created": r["created_at"], "status": r["status"],
                               "tier": r["data"].get("tier"), "total_odds": r["data"].get("total_odds"),
                               "legs": [f"{l.get('home')} v {l.get('away')}: {l.get('pick')} @{l.get('odds')} "
                                        f"-> {l.get('status')} {l.get('score', '')}" for l in r["data"].get("legs", [])]}
                              for r in rows]}
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
