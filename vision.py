"""Image understanding for the AI analyst (Telegram photos / image files and the Mini App AI tab).

One Gemini (multimodal) call reads the image into JSON: what it is (bookmaker betslip, one of this bot's own slips,
a fixture list / odds board, anything else), the booking code and legs when visible, matches, and a short answer.
Then the image is routed to the existing tools:

  betslip   booking code visible -> Bet Analyzer on the code (same output and verdict, not saved to History);
            no code (or the code is invalid) -> each leg is mapped to a SportyBet event + market (vision.map_leg)
            and the mapped legs are analyzed the same way; legs that can't be mapped are shown as "couldn't match".
  bot_slip  one of our slips -> resolved like a reply to that slip (editflow.SlipEditor.match_screenshot): with a
            caption the caption edits it in its own section (rollover -> preview with Confirm / Keep), without one
            the bot says which slip it is and a reply to that message edits it.
  fixtures  matches / odds board -> the normal tool loop (predict, scan under constraints) with the image attached;
            booking tools are not offered, so a slip only gets a code from an explicit Book tap.
  other     a brief honest description / answer. Unreadable -> said plainly.

Nothing is booked or written to History here. Matches, odds and codes are only ever taken from what is visible:
the reading prompt forbids guessing, codes are checked against SportyBet, legs against SportyBet's listings.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
from datetime import datetime

from analyzer import bare_code
from markets import FAMILIES, spec_value
from matching import team_score

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024          # what we send to Gemini
MAX_DOWNLOAD = 20 * 1024 * 1024      # Telegram getFile limit
MAX_SIDE = 2048                      # longer images are downscaled (Pillow) before sending
READ_TIMEOUT = 45
LOOP_TIMEOUT = 90
KINDS = ("betslip", "bot_slip", "fixtures", "other")
MAX_MAP = 20                          # legs mapped per screenshot


class ImageError(ValueError):
    pass


# ------------------------------------------------------------------ bytes
def sniff_mime(data: bytes, hint: str | None = None) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif", b"ftyphevc"):
        return "image/heic"
    h = (hint or "").lower()
    return h if h.startswith("image/") else None


def prepare_image(data: bytes, mime_hint: str | None = None) -> tuple[bytes, str]:
    """(bytes, mime) ready for Gemini: <= 5 MB, longest side <= 2048 px when Pillow can decode it.
    Raises ImageError for non-images and for files that are too big to shrink."""
    if not data:
        raise ImageError("The image was empty.")
    if len(data) > MAX_DOWNLOAD:
        raise ImageError("That file is over 20 MB; send a screenshot or a smaller photo.")
    mime = sniff_mime(data, mime_hint)
    if not mime:
        raise ImageError("That file isn't an image I can read (send a JPG, PNG or WebP).")
    if mime == "image/gif":
        mime = "image/gif"
    try:
        from PIL import Image  # optional dependency
    except Exception:          # pragma: no cover - Pillow missing
        Image = None
    if Image is not None and mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        try:
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            if max(w, h) > MAX_SIDE or len(data) > MAX_BYTES or mime == "image/gif":
                im = im.convert("RGB")
                if max(w, h) > MAX_SIDE:
                    r = MAX_SIDE / max(w, h)
                    im = im.resize((max(1, int(w * r)), max(1, int(h * r))))
                for q in (88, 80, 70, 60):
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=q, optimize=True)
                    if buf.tell() <= MAX_BYTES:
                        return buf.getvalue(), "image/jpeg"
        except Exception as exc:
            log.info("image downscale skipped: %s", exc.__class__.__name__)
    if len(data) > MAX_BYTES:
        raise ImageError("That image is over 5 MB and I couldn't shrink it; send a screenshot instead.")
    return data, mime


# ------------------------------------------------------------------ reading prompt
READ_SYSTEM = """You read one image for a football-betting Telegram bot ("Fra Prediction", SportyBet Nigeria). Return
ONLY a JSON object, no prose, with these keys:
{
 "readable": true|false,            // false when the image is too blurry, dark, cropped or tiny to read
 "kind": "betslip"|"bot_slip"|"fixtures"|"other",
 "bookmaker": "",                   // e.g. "SportyBet", "Bet9ja", "" if not shown
 "booking_code": "",                // the booking / share code EXACTLY as printed (5-10 letters/digits), else ""
 "legs": [ {"home": "", "away": "", "market": "", "pick": "", "odds": null,
            "kind": "1x2"|"double_chance"|"dnb"|"over_under"|"team_over_under"|"btts"|"asian_handicap"|"handicap"|"other",
            "period": "ft"|"1h"|"2h",
            "selection": "home"|"draw"|"away"|"1X"|"X2"|"12"|"over"|"under"|"yes"|"no"|"",
            "team": "home"|"away"|"",       // whose total, for team_over_under
            "line": "" } ],                 // "2.5" for totals, the PICKED team's handicap for asian_handicap ("+1.5"),
                                            // "2:0" for handicap (European); "" otherwise
 "matches": [ {"home": "", "away": "", "kickoff": "", "league": "", "odds": ""} ],
 "header": "",                      // first line(s) of text of the slip / message as shown (for bot_slip)
 "summary": "",                     // one short sentence: what the image shows
 "answer": ""                       // for kind "other" only: a brief, honest answer to the user's caption (or a
                                    // 1-2 sentence description when there is no caption), from what is visible
}
Kinds:
- "bot_slip": a message from the bot "Fra Prediction" (or "FraPredictbot") showing a slip: headers like "New picks
  for today", "Rollover day", "SAFE SLIP", "BET ANALYZER", "Edited slip", legs with "est 83% ✓", "Estimates, not
  guarantees", buttons like "Confirm: book new code". Fill legs and header. Takes priority over "betslip".
- "betslip": a bookmaker betslip / booking-code screen / bet history ticket (SportyBet, Bet9ja, 1xBet, ...). Fill
  booking_code (only if printed) and legs (one per selection).
- "fixtures": a list of matches, a single match card, an odds board or a league table of upcoming games. Fill
  matches.
- "other": anything else (a chat, a meme, a document, a photo...).
Rules: copy team names, odds and codes exactly as printed. NEVER guess or invent a team, odds value or code that is
not visible; leave the field empty (or null) instead. "home" is the team listed first. selection is from the home
team's point of view ("1X" = home or draw). If you can't read the image at all, set readable=false."""

_KIND_MAP = {"1x2": "1x2", "double_chance": "dc", "dc": "dc", "dnb": "dnb", "draw_no_bet": "dnb",
             "over_under": "total", "total": "total", "team_over_under": "team_total", "team_total": "team_total",
             "btts": "btts", "gg": "btts", "asian_handicap": "ah", "handicap": "eh", "european_handicap": "eh"}
_PERIOD = {"ft": "ft", "full": "ft", "1h": "h1", "h1": "h1", "first_half": "h1", "2h": "h2", "h2": "h2",
           "second_half": "h2"}
_CODE_RE = re.compile(r"^[A-Z0-9]{5,10}$")


def _str(x, n: int = 120) -> str:
    return str(x if x is not None else "").strip()[:n]


def _odds(x) -> float | None:
    try:
        v = float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if 1.0 < v < 10000 else None


def parse_read(text: str) -> dict | None:
    """Normalized reading from Gemini's JSON (None when it isn't JSON)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    kind = _str(d.get("kind"), 20).lower()
    code = re.sub(r"\s+", "", _str(d.get("booking_code"), 20)).upper()
    legs = []
    for l in (d.get("legs") or [])[:40]:
        if not isinstance(l, dict):
            continue
        leg = {"home": _str(l.get("home"), 60), "away": _str(l.get("away"), 60), "market": _str(l.get("market"), 80),
               "pick": _str(l.get("pick"), 80), "odds": _odds(l.get("odds")),
               "kind": _str(l.get("kind"), 30).lower(), "period": _str(l.get("period"), 12).lower() or "ft",
               "selection": _str(l.get("selection"), 10), "team": _str(l.get("team"), 10).lower(),
               "line": _str(l.get("line"), 12)}
        if leg["home"] or leg["away"] or leg["pick"]:
            legs.append(leg)
    matches = []
    for x in (d.get("matches") or [])[:40]:
        if isinstance(x, dict) and (_str(x.get("home")) or _str(x.get("away"))):
            matches.append({k: _str(x.get(k), 60) for k in ("home", "away", "kickoff", "league", "odds")})
    readable = d.get("readable")
    return {"readable": bool(readable) if readable is not None else True,
            "kind": kind if kind in KINDS else "other", "bookmaker": _str(d.get("bookmaker"), 40),
            "booking_code": code if _CODE_RE.match(code) else "", "legs": legs, "matches": matches,
            "header": _str(d.get("header"), 300), "summary": _str(d.get("summary"), 300),
            "answer": _str(d.get("answer"), 1500)}


# ------------------------------------------------------------------ leg -> SportyBet market (pure)
_SEL = {"1x2": {"1": "home", "2": "draw", "3": "away"}, "dc": {"9": "1X", "10": "12", "11": "X2"},
        "dnb": {"4": "home", "5": "away"}, "total": {"12": "over", "13": "under"},
        "team_total": {"12": "over", "13": "under"}, "btts": {"74": "yes", "76": "no"},
        "ah": {"1714": "home", "1715": "away"}, "eh": {"1711": "home", "1712": "draw", "1713": "away"}}
_SWAP = {"home": "away", "away": "home", "1X": "X2", "X2": "1X", "draw": "draw", "12": "12", "over": "over",
         "under": "under", "yes": "yes", "no": "no"}


def _num(x) -> float | None:
    try:
        return round(float(str(x).replace(",", ".").replace("+", "")), 2)
    except (TypeError, ValueError):
        return None


def _eh_line(x: str) -> str | None:
    m = re.search(r"(\d+)\s*[:\-–]\s*(\d+)", str(x or ""))
    return f"{int(m.group(1))}:{int(m.group(2))}" if m else None


def market_key(market: dict, outcome: dict) -> tuple | None:
    """(kind, period, selection, line) of one SportyBet outcome (families in markets.FAMILIES only)."""
    fam = FAMILIES.get(str(market.get("id")))
    if not fam:
        return None
    oid = str(outcome.get("id"))
    sel = _SEL.get(fam.kind, {}).get(oid)
    if sel is None:
        return None
    spec = market.get("specifier") or ""
    line = None
    if fam.kind in ("total", "team_total"):
        line = _num(spec_value(spec, "total"))
        if line is None:
            return None
        if fam.kind == "team_total":
            sel = f"{fam.side}:{sel}"
    elif fam.kind == "ah":
        h = _num(spec_value(spec, "hcp"))
        if h is None:
            return None
        line = h if sel == "home" else round(-h, 2)
    elif fam.kind == "eh":
        line = _eh_line(spec_value(spec, "hcp") or "")
        if line is None:
            return None
    elif spec:
        return None
    return fam.kind, fam.period, sel, line


def read_key(leg: dict, swapped: bool = False) -> tuple | None:
    """Same key from a leg read off the image (None when the read is too vague to map)."""
    kind = _KIND_MAP.get(leg.get("kind") or "")
    if not kind:
        return None
    period = _PERIOD.get(leg.get("period") or "ft", "ft")
    sel = (leg.get("selection") or "").strip()
    sel = {"Home": "home", "Away": "away", "Draw": "draw", "x": "draw", "X": "draw", "1": "home", "2": "away",
           "Over": "over", "Under": "under", "Yes": "yes", "No": "no", "1x": "1X", "x2": "X2"}.get(sel, sel)
    if kind in ("total", "team_total", "btts") or sel in ("home", "away", "draw"):
        sel = sel.lower()
    if not sel:
        return None
    line = None
    if kind in ("total", "team_total"):
        line = _num(re.sub(r"[^0-9.+-]", "", leg.get("line") or "") or None)
        if line is None:
            m = re.search(r"(\d+(?:\.\d+)?)", leg.get("pick") or "")
            line = _num(m.group(1)) if m else None
        if line is None:
            return None
    elif kind == "ah":
        line = _num(re.sub(r"[^0-9.+-]", "", leg.get("line") or "") or None)
        if line is None:
            return None
    elif kind == "eh":
        line = _eh_line(leg.get("line") or "") or _eh_line(leg.get("pick") or "")
        if line is None:
            return None
    if swapped:
        sel = _SWAP.get(sel, sel)
        if kind == "eh" and line:
            a, b = line.split(":")
            line = f"{b}:{a}"
    if kind == "team_total":
        side = (leg.get("team") or "").lower()
        if side not in ("home", "away"):
            return None
        if swapped:
            side = _SWAP[side]
        sel = f"{side}:{sel}"
    return kind, period, sel, line


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9.+:]+", " ", (t or "").lower()).strip()


def map_leg(leg: dict, ev, markets: list[dict], swapped: bool = False) -> dict | None:
    """The SportyBet outcome a leg read from an image refers to: structured key first, then a strict text match
    on market + pick (corners, cards, combos...). None when nothing matches."""
    want = read_key(leg, swapped)
    best = None
    for m in markets or []:
        for o in m.get("outcomes") or []:
            if not o.get("active", True) or not o.get("odds"):
                continue
            if want is not None and market_key(m, o) == want:
                score = 100.0
            else:
                if want is not None and FAMILIES.get(str(m.get("id"))) and _KIND_MAP.get(leg.get("kind") or ""):
                    continue            # a modeled family was read: only the exact key counts
                md, od = _norm(m.get("desc") or ""), _norm(o.get("desc") or "")
                lm, lp = _norm(leg.get("market") or ""), _norm(leg.get("pick") or "")
                if not lm or not lp or not md or not od:
                    continue
                od_t = _norm((o.get("desc") or "").replace("Home", ev.home if ev else "").replace(
                    "Away", ev.away if ev else ""))
                from rapidfuzz import fuzz
                sm = fuzz.token_set_ratio(md, lm)
                sp = max(fuzz.ratio(od, lp), fuzz.ratio(od_t, lp))
                if sm < 90 or sp < 92:
                    continue
                score = (sm + sp) / 2 - 5
            io = leg.get("odds")
            if io and o.get("odds"):
                score -= min(5.0, abs(float(o["odds"]) - io) * 5)
            if best is None or score > best[0]:
                best = (score, m, o)
    if best is None:
        return None
    _, m, o = best
    return {"event_id": ev.event_id if ev else "", "market_id": str(m["id"]), "specifier": m.get("specifier") or "",
            "outcome_id": str(o["id"]), "odds": o.get("odds"), "market": m.get("desc") or "", "pick": o.get("desc") or "",
            "image_odds": leg.get("odds")}


def is_swapped(leg: dict, ev) -> bool:
    if not leg.get("home") or not leg.get("away"):
        return False
    straight = team_score(leg["home"], ev.home) + team_score(leg["away"], ev.away)
    rev = team_score(leg["home"], ev.away) + team_score(leg["away"], ev.home)
    return rev > straight + 10


async def find_event(services, home: str, away: str):
    """Upcoming SportyBet event for the teams read from the image (None below a confident match)."""
    if home and away:
        ranked = await services.search(f"{home} vs {away}", limit=3)
        if ranked and ranked[0][0] >= 75:
            return ranked[0][1]
        return None
    one = home or away
    if not one:
        return None
    ranked = await services.search(one, limit=2)
    if ranked and ranked[0][0] >= 88 and (len(ranked) < 2 or ranked[1][0] < ranked[0][0] - 5):
        return ranked[0][1]
    return None


async def map_legs(services, legs: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """(mapped, unmatched, details) for legs read from a screenshot."""
    bm = services.bookmaker
    evs = await asyncio.gather(*(find_event(services, l.get("home", ""), l.get("away", ""))
                                 for l in legs[:MAX_MAP]), return_exceptions=True)
    ids = list(dict.fromkeys(e.event_id for e in evs if e is not None and not isinstance(e, Exception)))
    got = await asyncio.gather(*(bm.event_detail(e) for e in ids), return_exceptions=True)
    details = {e: d for e, d in zip(ids, got) if not isinstance(d, Exception) and d and d[0] is not None}
    mapped, unmatched, seen = [], [], set()
    for leg, ev in zip(legs[:MAX_MAP], evs):
        base = {"home": leg.get("home"), "away": leg.get("away"), "market": leg.get("market"),
                "pick": leg.get("pick"), "odds": leg.get("odds")}
        if ev is None or isinstance(ev, Exception):
            unmatched.append({**base, "why": "match not found in SportyBet's upcoming list"})
            continue
        det = details.get(ev.event_id)
        if not det or not det[1]:
            unmatched.append({**base, "why": "SportyBet shows no open markets for it"})
            continue
        m = map_leg(leg, det[0], det[1], swapped=is_swapped(leg, det[0]))
        if m is None:
            unmatched.append({**base, "why": "market not found"})
            continue
        key = (m["event_id"], m["market_id"], m["specifier"], m["outcome_id"])
        if key in seen:
            continue
        seen.add(key)
        mapped.append(m)
    for leg in legs[MAX_MAP:]:
        unmatched.append({"home": leg.get("home"), "away": leg.get("away"), "market": leg.get("market"),
                          "pick": leg.get("pick"), "odds": leg.get("odds"), "why": f"only {MAX_MAP} legs are mapped"})
    return mapped, unmatched, details


# ------------------------------------------------------------------ Gemini calls
async def read_image(ai, data: bytes, mime: str, caption: str) -> dict:
    """One Gemini call -> normalized reading. Raises on model failure / non-JSON (logged with finishReason)."""
    dbg = {"t": datetime.now().isoformat(timespec="seconds"), "bytes": len(data), "mime": mime}
    ai.last_image = dbg
    contents = [{"role": "user", "parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}},
        {"text": f"User's caption: {caption.strip() or '(none)'}\nRead the image and return the JSON object."}]}]
    last = None
    for attempt in range(2):
        t0 = time.time()
        resp = await asyncio.wait_for(ai._generate(contents, system=READ_SYSTEM, tools=None, max_tokens=8192,
                                                   timeout=READ_TIMEOUT,
                                                   gen_extra={"responseMimeType": "application/json",
                                                              "temperature": 0.1}),
                                      timeout=READ_TIMEOUT + 15)
        cand = (resp.get("candidates") or [{}])[0]
        finish = cand.get("finishReason") or (resp.get("promptFeedback") or {}).get("blockReason") or "?"
        dbg.setdefault("finish", []).append(finish)
        dbg.setdefault("gemini_s", []).append(round(time.time() - t0, 1))
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        info = parse_read(text)
        if info is not None:
            dbg["kind"] = info["kind"]
            dbg["readable"] = info["readable"]
            log.info("image read: kind=%s readable=%s legs=%d matches=%d code=%s finish=%s", info["kind"],
                     info["readable"], len(info["legs"]), len(info["matches"]), bool(info["booking_code"]), finish)
            return info
        last = finish
        log.warning("image read: no JSON (finish=%s, %d chars)", finish, len(text))
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "IMAGE_SAFETY", "OTHER"):
            break
    raise RuntimeError(f"no readable answer from the model (finish={last})")


def _read_note(info: dict) -> str:
    bits = []
    if info.get("summary"):
        bits.append(info["summary"])
    return " ".join(bits)[:300]


IMAGE_RULES = """

IMAGE: the user sent an image (attached) with the message below. A first pass read it as listed in [Image read].
Use only matches, odds and codes that are visible in the image; never invent any. Look the matches up with the tools
(predict_match / team_stats / list_matches; scan_markets for a slip, restricted to the visible teams with
include_teams when the user wants a slip from them). If a visible match isn't on SportyBet, say so. Never book: the
bot shows a Book button under a slip and books only when the user taps it. Keep it short."""


def _tools_without_booking():
    from ai import TOOLS
    decl = [f for f in TOOLS[0]["functionDeclarations"] if f["name"] != "build_booking_code"]
    return [{"functionDeclarations": decl}]


async def _answer_fixtures(ai, chat_id: int, data: bytes, mime: str, caption: str, info: dict) -> dict:
    from ai import SYSTEM, TZ
    from constraints import parse_text
    ask = caption.strip() or "Give me a short prediction for the matches in this image."
    listed = "; ".join(f"{m['home']} v {m['away']}" + (f" ({m['kickoff']})" if m.get("kickoff") else "")
                       for m in info.get("matches") or []) or "none read"
    contents = ai.history(chat_id) + [{"role": "user", "parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}},
        {"text": f"[Image read] {info.get('summary', '')} Matches: {listed}\n\n[My message]\n{ask}"}]}]
    turn = {"slip": None, "code": None, "tools": [], "hard": parse_text(ask), "analysis": None}
    system = SYSTEM.format(today=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M")) + IMAGE_RULES
    reply = ""
    try:
        reply = await asyncio.wait_for(ai._loop(contents, turn, tools=_tools_without_booking(), system=system,
                                                timeout=40), timeout=LOOP_TIMEOUT)
    except Exception as exc:
        log.warning("image fixtures loop failed (%s): rule-based answer", exc.__class__.__name__)
    if not reply and turn.get("slip") and turn["slip"].get("tier") != "audit":
        reply = ai._slip_summary(turn["slip"])
    if not reply:
        turn["tools"].append("rule_predict")
        reply = await _predict_fallback(ai.s, info.get("matches") or [])
    turn["code"] = None                 # never booked from an image turn
    return {"reply": reply, "slip": turn.get("slip"), "tools": turn["tools"], "turn": turn}


async def _predict_fallback(services, matches: list[dict]) -> str:
    lines = []
    for m in matches[:6]:
        ev = await find_event(services, m.get("home", ""), m.get("away", ""))
        name = f"{m.get('home', '?')} v {m.get('away', '?')}"
        if ev is None:
            lines.append(f"• {name}: not on SportyBet's upcoming list")
            continue
        p = None
        try:
            p = await services.predict(ev)
        except Exception:
            p = None
        if p and p.best:
            lines.append(f"• {ev.home} v {ev.away}: best pick **{p.best.label}** @{p.best.odds} (est {p.best.prob:.0%})")
        else:
            lines.append(f"• {ev.home} v {ev.away}: no prediction available right now")
    if not lines:
        return ("I can see a fixtures image but couldn't read the team names clearly. Type the match "
                "(e.g. Arsenal vs Chelsea) and I'll analyse it.")
    return "From the matches in your image:\n" + "\n".join(lines)


# ------------------------------------------------------------------ entry point
async def handle_image(ai, chat_id: int, data: bytes, mime: str | None, caption: str = "") -> dict:
    """Route one image. Returns {kind, reply, analysis?, edit?, context?, slip?, tools, read}. kind:
    analysis | edit | bot_slip | answer | unreadable | error. Never books, never writes History."""
    caption = (caption or "").strip()[:1500]
    try:
        data, mime = prepare_image(data, mime)
    except ImageError as exc:
        return {"kind": "error", "reply": f"⚠️ {exc}", "tools": []}
    if not ai.enabled:
        code = bare_code(caption)
        msg = "I can't read images right now (the AI model isn't configured)."
        return {"kind": "error", "reply": msg + (" Send the booking code as text instead." if not code else ""),
                "tools": []}
    try:
        info = await read_image(ai, data, mime, caption)
    except Exception as exc:
        log.warning("image read failed: %s", exc)
        return {"kind": "error", "tools": ["read_image"],
                "reply": ("I couldn't read that image right now (the AI model didn't answer). Nothing was booked. If "
                          "it's a betslip, paste the booking code as text; otherwise send the image again.")}
    tools = ["read_image"]
    out = await _route(ai, chat_id, data, mime, caption, info, tools)
    out.setdefault("tools", tools)
    out["read"] = {k: info.get(k) for k in ("kind", "readable", "bookmaker", "booking_code", "summary")}
    out["read"]["legs"] = len(info.get("legs") or [])
    out["read"]["matches"] = len(info.get("matches") or [])
    try:
        ai._save(chat_id, "user", f"[image: {info['kind']}: {_read_note(info)}] {caption}".strip())
        ai._save(chat_id, "model", out.get("reply") or "")
    except Exception:
        pass
    return out


async def _route(ai, chat_id, data, mime, caption, info, tools) -> dict:
    s = ai.s
    if not info["readable"]:
        return {"kind": "unreadable", "reply": (
            "I can't read this image clearly (too blurry, dark or cropped). Send a sharper screenshot"
            " or type what you need, e.g. the booking code.")}
    kind = info["kind"]

    if kind == "bot_slip":
        ed = getattr(s, "editor", None)
        ctx = None
        if ed is not None:
            try:
                ctx = await ed.match_screenshot(chat_id, info.get("legs") or [], info.get("header", ""),
                                                info.get("booking_code", ""))
            except Exception as exc:
                log.warning("screenshot slip lookup failed: %s", exc, exc_info=True)
        if ctx is None:
            return {"kind": "answer", "reply": (
                "That's a screenshot of one of my slips, but I can't find that slip any more (older than 3 days or "
                "from another chat). Reply to the original message in the chat, or tell me what to change on your "
                "current slip.")}
        tools.append("resolve_slip")
        from slipedit import leg_name
        day = f" {ctx['day']}" if ctx.get("day") else ""
        label = {"rollover_preview": f"rollover day{day} preview", "rollover_day": f"rollover day{day} slip",
                 "scan": "scanner slip", "ai": "AI slip", "analysis": "analyzed slip",
                 "history": "booked ticket"}.get(ctx.get("section"), "slip")
        if caption:
            res = await ai._ask_edit(chat_id, caption, ctx)
            tools.extend(res.get("tools") or [])
            return {"kind": "edit", "reply": res["reply"], "edit": res.get("edit"), "context": ctx, "tools": tools}
        legs = ctx.get("legs") or []
        lines = [f"📷 That's your **{label}** ({len(legs)} legs"
                 + (f", code `{ctx['code']}`" if ctx.get("code") else "") + "):"]
        lines += [f"{i}. {leg_name(l)} · {l.get('pick', '')} @{float(l.get('odds') or 0):.2f}"
                  for i, l in enumerate(legs[:12], 1)]
        lines.append("Reply to this message with what to change (e.g. \"change the first selection\", \"make it "
                     "safer\"). Nothing is booked until you confirm. Estimates, not guarantees.")
        return {"kind": "bot_slip", "reply": "\n".join(lines), "context": ctx}

    if kind == "betslip":
        an = getattr(s, "analyzer", None)
        code = info.get("booking_code") or bare_code(caption) or ""
        note = ""
        if code and an is not None:
            tools.append("analyze_booking_code")
            res = await an.analyze(code, _rules_from(caption))
            if res.get("ok"):
                return {"kind": "analysis", "analysis": res, "code_read": code,
                        "reply": f"📷 Read booking code `{code}` from your screenshot.\n{res['recommendation']['text']}"}
            if res.get("kind") == "invalid":
                note = (f"The code I read ({code}) isn't valid on SportyBet (misread, expired or from another "
                        "bookmaker).")
            else:
                note = f"SportyBet didn't load {code} just now."
        legs = info.get("legs") or []
        if not legs:
            msg = note or "I can see a betslip but can't make out a booking code or the selections."
            return {"kind": "answer", "reply": msg + " Paste the booking code as text, or send a clearer screenshot."}
        tools.append("map_legs")
        mapped, unmatched, details = await map_legs(s, legs)
        if not mapped:
            rows = "\n".join(f"• {u.get('home') or '?'} v {u.get('away') or '?'}: {u.get('pick') or u.get('market')}"
                             f" — couldn't match ({u.get('why')})" for u in unmatched[:12])
            return {"kind": "answer", "reply": ((note + "\n") if note else "") + (
                "I read these selections but couldn't match any of them to an upcoming SportyBet match, so I "
                "can't rate them:\n" + rows + "\nThey may have started already, or SportyBet doesn't offer them.")}
        if an is None:
            return {"kind": "error", "reply": "The analyzer isn't available right now."}
        tools.append("analyze_legs")
        res = await an.analyze_legs(mapped, unmatched, _rules_from(caption), details=details)
        if not res.get("ok"):
            return {"kind": "error", "reply": "The analysis failed on my side (SportyBet or the stats feed didn't "
                                              "answer). Send it again in a minute."}
        head = (note + " " if note else "") + (
            f"📷 Matched {len(mapped)} of {len(mapped) + len(unmatched)} legs from your screenshot to SportyBet"
            + (f"; {len(unmatched)} couldn't be matched." if unmatched else "."))
        return {"kind": "analysis", "analysis": res, "reply": head + "\n" + res["recommendation"]["text"]}

    if kind == "fixtures":
        tools.append("answer_fixtures")
        r = await _answer_fixtures(ai, chat_id, data, mime, caption, info)
        tools.extend(r["tools"])
        reply = r["reply"]
        if "estimate" not in reply.lower() and "guarantee" not in reply.lower():
            reply += "\n\n_Estimates, not guarantees. 18+, bet responsibly._"
        return {"kind": "answer", "reply": reply, "slip": r.get("slip"), "turn": r.get("turn")}

    ans = info.get("answer") or info.get("summary") or ""
    if not ans:
        ans = "I can see the image but there's nothing football- or betting-related I can read in it."
    return {"kind": "answer", "reply": ans}


def _rules_from(caption: str) -> str:
    """Swap rules stated in the caption ("no unders") for the analyzer; anything else is ignored there."""
    t = (caption or "").lower()
    return caption if re.search(r"\b(no|without|only)\b.*\b(under|over|draw|corner|card|prop)", t) else ""
