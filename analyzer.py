"""Bet analyzer: load any SportyBet NG booking code and rate the slip with the scanner's own model.

Per leg (matches not started yet): the full market book of the match is re-read from SportyBet and scored exactly
like the scanner does (de-margined price + Betradar feed probability + Poisson fit, then model v2's stats blend and
calibration, `scanner.apply_stats`), so the estimate is the same number the scanner would show for that leg. On top:
fair odds (1 / est), value (est x odds - 1), a risk rating (Strong / OK / Weak / Avoid), a one-line reason (form,
H2H, goals trend, team news from the Sportradar feed) and safer alternatives in the same match.
Legs we can't model (player props, specials, single-price markets, other sports, matches in play) are shown as
"Not rated"; the slip's combined chance then counts them at the bookmaker's de-margined price and says so.

Slip: total odds, combined estimated hit chance (legs treated as independent), weakest legs, an overall grade A-F,
and concrete suggestions (drop a leg / safer swap in the same match) with the improved combined chance. An
improved slip can be re-booked, but only when the user explicitly taps the button. The analysis ends with a
plain verdict: ✅ go / ⚠️ go only after a change / ❌ skip. Analyzed codes are not saved or tracked.

Everything here is an estimate, never a guarantee.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone

import leagues
from constraints import Constraints, leg_tags, parse_text, verify
from markets import FAMILIES, pretty_pick, spec_value
from scanner import Leg, _rec, apply_stats, uid_of

log = logging.getLogger(__name__)

DISCLAIMER = "Estimates from prices + team stats, not guarantees. Combined chance assumes independent legs. 18+."
RATINGS = ("Strong", "OK", "Weak", "Avoid")
RATING_POINTS = {"Strong": 100, "OK": 75, "Weak": 45, "Avoid": 10, "Not rated": 50}
NOT_RATED = "Not rated"
CACHE_TTL = 90           # seconds a fresh analysis of the same code is reused
KEEP_RESULTS = 6 * 3600  # how long an analysis can still be re-booked / narrated
MAX_LEGS = 60

_CODE_BARE = re.compile(r"^\s*([A-Za-z0-9]{6})\s*$")
_CODE_ANY = re.compile(r"(?:sharecode=|code[:\s]+|^|\s)([A-Za-z0-9]{5,10})(?=$|[\s&#?.,!])", re.I)


# ------------------------------------------------------------------ code detection
def bare_code(text: str) -> str | None:
    """A message that is just a 6-character booking code (auto-detect in chat). It must contain a digit or be all
    capitals, so ordinary 6-letter words ('thanks', 'Arsenal') still go to the AI."""
    m = _CODE_BARE.match(text or "")
    if not m:
        return None
    c = m.group(1)
    if any(ch.isdigit() for ch in c) or c.isupper():
        return c.upper()
    return None


def extract_code(text: str) -> str | None:
    """Code from '/analyze N6RXGP', 'N6RXGP', or a share link '...?shareCode=N6RXGP'."""
    t = (text or "").strip()
    m = re.search(r"sharecode=([A-Za-z0-9]{5,10})", t, re.I)
    if m:
        return m.group(1).upper()
    for tok in re.split(r"[\s,;]+", t):
        if re.fullmatch(r"[A-Za-z0-9]{5,10}", tok) and (any(ch.isdigit() for ch in tok) or tok.isupper()
                                                         or len(tok) == 6):
            return tok.upper()
    return None


# ------------------------------------------------------------------ share payload
def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_share(data: dict) -> list[dict]:
    """Legs of a /orders/share/<code> payload, in slip order. Each: ids, teams, league, kickoff, market, pick, the
    current price, match state (upcoming / live / ended / cancelled / unavailable), score and settled result."""
    legs: list[dict] = []
    for o in (data.get("outcomes") or [])[:MAX_LEGS]:
        mk = (o.get("markets") or [{}])[0]
        oc = (mk.get("outcomes") or [{}])[0]
        sport = o.get("sport") or {}
        cat = sport.get("category") or {}
        tour = cat.get("tournament") or {}
        status = int(o.get("status") or 0)
        ms = str(o.get("matchStatus") or "")
        msl = ms.lower()
        if any(w in msl for w in ("cancel", "abandon", "postpon", "interrupt")):
            state = "cancelled"
        elif status == 0:
            state = "upcoming"
        elif status in (1, 2):
            state = "live"
        else:
            state = "ended"
        result = None
        refund = _f(oc.get("refundFactor")) or 0.0
        if state == "ended" and oc.get("isWinning") is not None:
            result = "void" if refund >= 0.999 else ("won" if int(oc.get("isWinning") or 0) == 1 else "lost")
        elif state == "cancelled":
            result = "void"
        start = int(o.get("estimateStartTime") or 0)
        mid, spec, oid = str(mk.get("id") or ""), mk.get("specifier") or "", str(oc.get("id") or "")
        fam = FAMILIES.get(mid)
        home, away = o.get("homeTeamName") or "?", o.get("awayTeamName") or "?"
        desc = oc.get("desc") or ""
        legs.append({
            "event_id": o.get("eventId") or "", "market_id": mid, "specifier": spec, "outcome_id": oid,
            "home": home, "away": away, "match": f"{home} vs {away}",
            "league": f"{cat.get('name')} · {tour.get('name')}" if cat.get("name") else (tour.get("name") or ""),
            "category": cat.get("name") or "", "tournament_id": tour.get("id") or "",
            "category_id": cat.get("id") or "",
            "sport": sport.get("id") or "sr:sport:1",
            "kickoff": datetime.fromtimestamp(start / 1000, timezone.utc).isoformat() if start else "",
            "market": mk.get("desc") or mk.get("name") or (fam.name if fam else f"Market {mid}"),
            "pick": pretty_pick(fam, desc, home, away) if fam else desc,
            "odds": _f(oc.get("odds")), "feed_prob": _f(oc.get("probability")),
            "active": bool(int(oc.get("isActive") or 0)) and int(mk.get("status") or 0) == 0,
            "group": mk.get("group") or "",
            "state": state, "match_status": ms, "score": o.get("setScore") or "",
            "minute": o.get("playedSeconds") or "", "result": result,
        })
    for u in (data.get("unavailableOutcomes") or [])[:MAX_LEGS]:
        legs.append({"event_id": u.get("eventId") or "", "market_id": str(u.get("marketId") or ""),
                     "specifier": u.get("specifier") or "", "outcome_id": str(u.get("outcomeId") or ""),
                     "home": "", "away": "", "match": u.get("eventId") or "Unavailable selection", "league": "",
                     "category": "", "tournament_id": "", "category_id": "", "sport": "", "kickoff": "", "market": "", "pick": "",
                     "odds": None, "feed_prob": None, "active": False, "group": "", "state": "unavailable",
                     "match_status": "", "score": "", "minute": "", "result": None})
    return legs


# ------------------------------------------------------------------ rating / grading (pure)
def rate_leg(est: float | None, value: float | None, verdict: str = "", rated: bool = True) -> str:
    """Strong / OK / Weak / Avoid. Base on the estimate (>=80% Strong, >=65% OK, >=45% Weak, else Avoid); one step
    down when the stats check demoted the leg or the price is well under fair (value < -8%); a leg the stats check
    dropped (form red flag / stats far below the price) is at best Weak."""
    if not rated or est is None:
        return NOT_RATED
    i = 0 if est >= 0.80 else 1 if est >= 0.65 else 2 if est >= 0.45 else 3
    if verdict == "demoted" or (value is not None and value < -0.08):
        i += 1
    if verdict == "dropped":
        i = max(i, 2) + (1 if est < 0.65 else 0)
    return RATINGS[min(i, 3)]


def leg_chance(l: dict) -> tuple[float, bool]:
    """(probability this leg lands, exact?) for the combined chance. Settled legs are 1 / 0; rated legs use our
    estimate; not-rated legs the bookmaker's de-margined price (approximate)."""
    if l.get("result") in ("won", "void"):
        return 1.0, True
    if l.get("result") == "lost":
        return 0.0, True
    if l.get("rated") and l.get("est") is not None:
        return float(l["est"]), True
    if l.get("state") == "unavailable":
        return 1.0, False          # dropped from the slip by SportyBet: it won't count
    o = l.get("odds")
    if o and o > 1:
        return min(0.98, 1 / (o * 1.06)), False
    return 0.5, False


def combined(legs: list[dict]) -> dict:
    p, approx = 1.0, 0
    for l in legs:
        x, exact = leg_chance(l)
        p *= x
        approx += 0 if exact else 1
    return {"chance": p, "approx_legs": approx}


def total_odds(legs: list[dict]) -> float:
    t = 1.0
    for l in legs:
        if l.get("result") == "void" or l.get("state") == "unavailable":
            continue
        o = l.get("booked_odds") or l.get("odds")
        if o and o > 1:
            t *= o
    return t


def grade_slip(legs: list[dict]) -> dict:
    """Overall grade A-F for the part of the slip still to play. Blends leg quality (rating points) with the
    chance of landing (log scale: 50% ~ 87, 10% ~ 57, 1% ~ 13) and a penalty when the slip as a whole is priced
    well under fair. Any Avoid leg caps the grade at C."""
    open_ = [l for l in legs if l.get("result") is None and l.get("state") not in ("unavailable",)]
    if not open_:
        return {"grade": None, "score": None}
    q = sum(RATING_POINTS.get(l.get("rating") or NOT_RATED, 50) for l in open_) / len(open_)
    p = combined(open_)["chance"]
    cs = 0.0 if p <= 0 else max(0.0, min(100.0, 100 * (1 + math.log10(p) / 2.3)))
    t = total_odds(open_)
    ev = p * t - 1
    pen = min(20.0, max(0.0, (-0.15 - ev) * 60)) if ev < -0.15 else 0.0
    score = 0.55 * q + 0.45 * cs - pen
    if any(l.get("rating") == "Avoid" for l in open_):
        score = min(score, 69.0)
    g = "A" if score >= 82 else "B" if score >= 70 else "C" if score >= 58 else "D" if score >= 45 else "F"
    return {"grade": g, "score": round(score, 1), "quality": round(q, 1), "chance_score": round(cs, 1),
            "slip_value": round(ev, 3)}


# ------------------------------------------------------------------ safer alternatives (pure on Leg objects)
def _line(spec: str) -> float | None:
    v = spec_value(spec or "", "total")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _dir(pick: str) -> str:
    p = (pick or "").lower()
    if " over " in f" {p} " or p.startswith("over"):
        return "over"
    if " under " in f" {p} " or p.startswith("under"):
        return "under"
    return ""


def related(orig: Leg, cand: Leg) -> bool:
    """Is `cand` the same idea as `orig`, only safer (lower total line, double chance instead of a win, ...)?"""
    if cand.event_id != orig.event_id or (cand.market_id, cand.specifier, cand.outcome_id) == (
            orig.market_id, orig.specifier, orig.outcome_id):
        return False
    fo, fc = FAMILIES.get(orig.market_id), FAMILIES.get(cand.market_id)
    lo, lc = _line(orig.specifier), _line(cand.specifier)
    do, dc_ = _dir(orig.pick), _dir(cand.pick)
    # same line market (goals, team goals, corners, bookings ...): same direction, line further from the edge
    if lo is not None and lc is not None and do and do == dc_ and orig.market_id == cand.market_id:
        return lc < lo if do == "over" else lc > lo
    if not fo or not fc or fo.period != fc.period:
        if fo and fo.kind == "btts" and orig.outcome_id == "74" and fc and fc.kind == "total" and fc.period == "ft":
            return dc_ == "over" and (lc or 9) <= 1.5
        return False
    if fo.kind in ("1x2", "dnb", "ah", "eh") and orig.side in ("home", "away"):
        return fc.kind in ("dc", "dnb") and cand.side == orig.side and fc.kind != fo.kind
    if fo.kind == "1x2" and orig.side == "draw":
        return fc.kind == "dc" and cand.outcome_id in ("9", "11")
    if fo.kind == "btts" and orig.outcome_id == "74":
        return fc.kind == "total" and dc_ == "over" and (lc or 9) <= 1.5
    return False


def best_swaps(orig: Leg, pool: list[Leg], rules: Constraints | None = None, n: int = 2) -> list[Leg]:
    """Up to n safer legs in the same match: est >= orig + 7 pts and >= 60%, odds >= 1.08, not dropped by the
    stats check, not a single-price leg, obeying the user's rules. Best = most odds per unit of risk."""
    out = []
    for c in pool:
        if not related(orig, c):
            continue
        if c.est < max(0.60, orig.est + 0.07) or c.odds < 1.08 or c.verdict == "dropped" or c.level == "thin":
            continue
        if "under" in leg_tags(c) and "under" not in leg_tags(orig):
            continue
        if rules is not None and not rules.leg_ok(c):
            continue
        out.append(c)
    out.sort(key=lambda c: -(math.log(c.odds) / -math.log(min(c.est, 0.995))))
    return out[:n]


# ------------------------------------------------------------------ reasons
def reason_line(leg: Leg, form, news: dict | None) -> str:
    """One short line: form, H2H / goals trend, stats vs price, red flag, team news."""
    parts = []
    fam = FAMILIES.get(leg.market_id)
    if form is not None and getattr(form, "ok", False):
        h, a = form.home, form.away
        hh = form.h2h.summary()
        if fam and fam.kind in ("total", "team_total", "btts"):
            def tot(t, v):
                g = t._sub(v)
                return (sum(x.gf + x.ga for x in g) / len(g)) if g else None
            th, ta = tot(h, "H"), tot(a, "A")
            s = []
            if th is not None:
                s.append(f"{h.name} home games avg {th:.1f} goals")
            if ta is not None:
                s.append(f"{a.name} away {ta:.1f}")
            if hh.get("n"):
                s.append(f"H2H avg {hh['avg_goals']:.1f}")
            if s:
                parts.append(", ".join(s))
        else:
            parts.append(f"{h.name} {h.form(5) or '-'} ({_rec(h)}), {a.name} {a.form(5) or '-'} ({_rec(a)})")
            if hh.get("n"):
                parts.append(f"H2H {hh['home_w']}-{hh['draws']}-{hh['away_w']}")
    if leg.p_stats is not None:
        parts.append(f"stats model {leg.p_stats:.0%} vs price {leg.p_market:.0%}")
    elif leg.verdict == "market only":
        parts.append("market-only estimate (stats can't price this market)")
    elif leg.verdict == "odds only":
        parts.append("odds-only (no team stats in the feed)")
    if "⚠" in (leg.why or ""):
        parts.append("⚠ " + leg.why.split("⚠", 1)[1].strip())
    if news:
        out = []
        for side, name in (("home", leg.home), ("away", leg.away)):
            miss = news.get(f"{side}_missing") or []
            if miss:
                names = ", ".join(p["name"] for p in miss[:2]) + (f" +{len(miss) - 2}" if len(miss) > 2 else "")
                out.append(f"{name} out: {names}")
        if out:
            parts.append("; ".join(out))
        if news.get("lineups"):
            parts.append("line-ups confirmed")
    return " · ".join(parts)[:260]


# ------------------------------------------------------------------ suggestions (pure on leg dicts)
def suggestions(legs: list[dict], rules: Constraints | None = None) -> dict:
    """Drop / swap suggestions and the improved slip. Only legs whose match hasn't started can change.
    Avoid legs: swap to a safer leg in the same match if one reaches >= 70%, else drop. Weak legs: swap when a
    safer leg exists. Unavailable selections are dropped. The improved slip keeps every other open leg as is."""
    base = combined(legs)["chance"]
    t0 = total_odds(legs)
    drops = []
    open_rated = [l for l in legs if l.get("state") == "upcoming" and l.get("rated") and l.get("est") is not None]
    for l in sorted(open_rated, key=lambda x: x["est"])[:3]:
        if l["est"] >= 0.80 and l.get("rating") in ("Strong",):
            continue
        if len(legs) <= 1:
            break
        new_p = base / l["est"] if l["est"] > 0 else base
        new_t = t0 / (l.get("odds") or 1)
        drops.append({"idx": l["idx"], "match": l["match"], "pick": l["pick"], "est": l["est"],
                      "rating": l["rating"], "chance": round(min(new_p, 1.0), 4), "total_odds": round(new_t, 2)})
    swaps = []
    for l in legs:
        for i, alt in enumerate(l.get("alternatives") or []):
            if i > 0:
                break
            new_p = base / l["est"] * alt["est"] if l.get("est") else base
            new_t = t0 / (l.get("odds") or 1) * alt["odds"]
            swaps.append({"idx": l["idx"], "match": l["match"], "from": f"{l['pick']} @{l['odds']:.2f}",
                          "to": f"{alt['pick']} @{alt['odds']:.2f}", "from_est": l["est"], "to_est": alt["est"],
                          "market": alt["market"], "chance": round(min(new_p, 1.0), 4),
                          "total_odds": round(new_t, 2)})
    # improved slip: only possible when every leg kept is still pre-match
    improved, changes = [], []
    for l in legs:
        st = l.get("state")
        if st == "unavailable":
            changes.append(f"drop unavailable selection {l['match']}")
            continue
        if st != "upcoming":
            continue
        alt = (l.get("alternatives") or [None])[0]
        if l.get("rating") == "Avoid":
            if alt and alt["est"] >= 0.70:
                improved.append(alt)
                changes.append(f"swap {l['match']}: {l['pick']} → {alt['pick']}")
            else:
                changes.append(f"drop {l['match']}: {l['pick']}")
            continue
        if l.get("rating") == "Weak" and alt:
            improved.append(alt)
            changes.append(f"swap {l['match']}: {l['pick']} → {alt['pick']}")
            continue
        improved.append(l)
    started = [l for l in legs if l.get("state") in ("live", "ended", "cancelled")]
    imp = None
    if changes and improved:
        p = combined(improved)["chance"]
        t = total_odds(improved)
        imp = {"legs": [{k: v for k, v in x.items() if k not in ("alternatives",)} for x in improved],
               "changes": changes, "chance": round(p, 4), "total_odds": round(t, 2),
               "base_chance": round(combined([l for l in legs if l.get("state") == "upcoming"])["chance"], 4),
               "base_total_odds": round(total_odds([l for l in legs if l.get("state") == "upcoming"]), 2),
               "excluded_started": len(started), "bookable": True, "constraint_check": None}
        if rules is not None and not rules.empty():
            chk = verify({"legs": [x for x in imp["legs"] if x.get("est") is not None]},
                         Constraints.from_dict({k: v for k, v in rules.to_dict().items()
                                                if k not in ("target_odds", "day", "tier")}))
            imp["constraint_check"] = chk
            imp["bookable"] = chk["ok"]
        if any(not x.get("event_id") or not x.get("outcome_id") for x in imp["legs"]):
            imp["bookable"] = False
    return {"drops": drops, "swaps": swaps, "improved": imp}


# ------------------------------------------------------------------ narrative grounding
_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def grounded(text: str, facts: dict) -> bool:
    """Every percentage the narrative mentions must be (within 1 pt) a percentage in the facts."""
    allowed = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for m in _NUM.finditer(x):
                allowed.add(float(m.group(1)))
    walk(facts)
    for m in _NUM.finditer(text or ""):
        v = float(m.group(1))
        if not any(abs(v - a) <= 1.0 for a in allowed):
            return False
    return True


def facts_for_narrative(res: dict) -> dict:
    return {
        "code": res["code"], "grade": res["summary"]["grade"], "total_odds": f"{res['summary']['total_odds']:.2f}",
        "combined_chance": f"{res['summary']['chance'] * 100:.0f}%",
        "legs": [{"match": l["match"], "pick": l["pick"], "odds": l.get("odds"), "state": l["state"],
                  "est": f"{l['est'] * 100:.0f}%" if l.get("est") is not None else "not rated",
                  "rating": l["rating"], "value": f"{l['value'] * 100:+.0f}%" if l.get("value") is not None else None,
                  "reason": l.get("reason", "")} for l in res["legs"]],
        "verdict": res.get("recommendation", {}).get("text"),
        "weakest": [l["match"] + " " + l["pick"] for l in res["legs"] if l["idx"] in res["summary"]["weakest"]],
        "suggestions": {"drops": [f"drop {d['match']} {d['pick']} -> chance {d['chance'] * 100:.0f}%"
                                  for d in res["suggestions"]["drops"]],
                        "swaps": [f"{s['match']}: {s['from']} -> {s['to']} -> chance {s['chance'] * 100:.0f}%"
                                  for s in res["suggestions"]["swaps"]],
                        "improved": (f"{res['suggestions']['improved']['chance'] * 100:.0f}% at "
                                     f"{res['suggestions']['improved']['total_odds']:.2f} odds")
                        if res["suggestions"].get("improved") else None},
    }


def fallback_narrative(res: dict) -> str:
    s = res["summary"]
    if s["grade"] is None:
        return f"Every leg is settled: the slip {s['status']}."
    weak = [l for l in res["legs"] if l["idx"] in s["weakest"]]
    txt = (f"Grade {s['grade']}: {len(res['legs'])} legs at {s['total_odds']:.2f} odds with an estimated "
           f"{s['chance'] * 100:.0f}% chance of landing.")
    if weak:
        w = weak[0]
        txt += f" Weakest: {w['match']} ({w['pick']}, {w['rating']}" + (
            f", est {w['est'] * 100:.0f}%)." if w.get("est") is not None else ").")
    imp = res["suggestions"].get("improved")
    if imp:
        txt += f" The improved slip lifts it to {imp['chance'] * 100:.0f}% at {imp['total_odds']:.2f} odds."
    return txt + " Estimates, not guarantees."


# ------------------------------------------------------------------ service
class Analyzer:
    def __init__(self, services):
        self.s = services
        self._cache: dict[str, tuple[float, dict]] = {}
        self.results: dict[str, dict] = {}     # analysis id -> {ts, res}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, aid: str) -> dict | None:
        x = self.results.get(aid)
        return x["res"] if x and time.time() - x["ts"] < KEEP_RESULTS else None

    def _remember(self, res: dict) -> str:
        aid = uuid.uuid4().hex[:10]
        self.results[aid] = {"ts": time.time(), "res": res}
        for k in [k for k, v in self.results.items() if time.time() - v["ts"] > KEEP_RESULTS]:
            self.results.pop(k, None)
        res["id"] = aid
        return aid

    async def analyze(self, code: str, rules_text: str = "", use_cache: bool = True) -> dict:
        """{ok, code, legs, summary, suggestions, recommendation, ...} or {ok: False, error, kind}.
        Analyzed codes are NOT saved to the history or tracked (history = bot-generated / booked slips only).
        Only an improved slip the user explicitly rebooks is recorded, like any other booking."""
        code = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5,10}", code):
            return {"ok": False, "kind": "invalid", "error": "That doesn't look like a SportyBet booking code."}
        key = f"{code}|{(rules_text or '').strip().lower()}"
        hit = self._cache.get(key)
        if use_cache and hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if use_cache and hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
            res = await self._analyze(code, rules_text)
            if res.get("ok"):
                self._remember(res)
                self._cache[key] = (time.time(), res)
            if len(self._locks) > 200:
                self._locks.clear()
            return res

    async def _analyze(self, code: str, rules_text: str) -> dict:
        t0 = time.time()
        bm = self.s.bookmaker
        sh = await bm.share(code)
        if not sh["ok"]:
            return {"ok": False, "code": code, **{k: sh[k] for k in ("error", "kind")}}
        data = sh["data"]
        legs = parse_share(data)
        if not legs:
            return {"ok": False, "code": code, "kind": "empty", "error": "That code loads no selections."}
        rules = parse_text(rules_text) if rules_text else None
        if rules is not None:
            rules.tier = rules.target_odds = rules.day = None
            rules = None if rules.empty() else rules
        booked = await self._booked_odds(code)
        for i, l in enumerate(legs):
            l["idx"] = i
            b = booked.get((l["event_id"], l["market_id"], l["specifier"], l["outcome_id"]))
            if b:
                l["booked_odds"] = b
            if l["state"] in ("ended", "cancelled") and b:
                l["odds"] = b                       # finished matches show 1.01 / 0.00: keep the booked price
            elif l["state"] in ("ended", "cancelled"):
                l["odds"] = None

        todo = [l for l in legs if l["state"] == "upcoming" and l["sport"] == "sr:sport:1"]
        ev_ids = list(dict.fromkeys(l["event_id"] for l in todo))
        details = await asyncio.gather(*(bm.event_detail(e) for e in ev_ids), return_exceptions=True)
        detail = {e: (d if not isinstance(d, Exception) else (None, None, {})) for e, d in zip(ev_ids, details)}
        events = {e: d[0] for e, d in detail.items() if d[0] is not None}
        forms_l = await asyncio.gather(*(self._form(ev) for ev in events.values()), return_exceptions=True)
        forms = {e: (f if not isinstance(f, Exception) else None) for e, f in zip(events, forms_l)}
        news_l = await asyncio.gather(*(self._news(ev) for ev in events.values()), return_exceptions=True)
        news = {e: (n if not isinstance(n, Exception) else None) for e, n in zip(events, news_l)}

        for l in legs:
            l.update(rated=False, est=None, fair_odds=None, value=None, rating=NOT_RATED, verdict="",
                     level="", reason="", why="", alternatives=[], p_market=None, p_stats=None,
                     home_uid=l.get("home_uid", ""), away_uid=l.get("away_uid", ""))
            if l["state"] == "unavailable":
                l["reason"] = "SportyBet no longer offers this selection; it won't load with the code."
                continue
            if l["sport"] != "sr:sport:1":
                l["reason"] = "Not football: not rated."
                continue
            ev = events.get(l["event_id"])
            if ev is not None:
                l["home_uid"], l["away_uid"] = uid_of(ev.home_id), uid_of(ev.away_id)
            if l["state"] == "live":
                l["reason"] = "In play: the pre-match model doesn't rate live legs; chance uses the live price."
                continue
            if l["state"] in ("ended", "cancelled"):
                l["reason"] = {"won": "Settled: won.", "lost": "Settled: lost.",
                               "void": "Void / cancelled."}.get(l["result"] or "", "Match over; result pending.")
                continue
            mk = detail.get(l["event_id"], (None, None, {}))[1]
            if ev is None or not mk:
                l["reason"] = "SportyBet didn't return this match's markets just now: not rated."
                continue
            self._score_leg(l, ev, mk, forms.get(l["event_id"]), news.get(l["event_id"]), rules)

        for l in legs:
            l["top_league"] = bool(l["sport"] == "sr:sport:1" and leagues.is_top_ids(
                l.get("tournament_id"), l.get("category_id"), l.get("home", ""), l.get("away", "")))
            if l["state"] != "unavailable" and not l["top_league"]:
                l["flags"] = ["outside top leagues"]
        res = self.build(code, legs, data, rules)
        res["elapsed_s"] = round(time.time() - t0, 1)
        return res

    def _score_leg(self, l: dict, ev, mk: list[dict], form, news, rules) -> None:
        all_legs = self.s.scanner.event_legs(ev, mk, keep=10 ** 6, odds_range=(1.001, 1000.0))
        me = next((x for x in all_legs if (x.market_id, x.specifier, x.outcome_id) ==
                   (l["market_id"], l["specifier"], l["outcome_id"])), None)
        if me is None:
            l["reason"] = "That market is closed or suspended on SportyBet right now: not rated."
            return
        apply_stats(me, form)
        l.update(odds=me.odds, market=me.market, pick=me.pick, league=ev.league_full, category=ev.category,
                 kickoff=ev.start.isoformat(), verdict=me.verdict, level=me.level, why=me.why,
                 p_market=round(me.p_market, 4), p_stats=None if me.p_stats is None else round(me.p_stats, 4),
                 tags=list(me.tags))
        if news:
            l["news"] = {k: v for k, v in news.items() if v}
        if me.level == "thin":
            l["reason"] = ("Player prop / special / single-price market: no model, not rated "
                           f"(price implies ~{me.p_market:.0%}).")
            return
        l["rated"] = True
        l["est"] = round(me.est, 4)
        l["fair_odds"] = round(1 / me.est, 2) if me.est > 0 else None
        l["value"] = round(me.est * me.odds - 1, 4)
        l["rating"] = rate_leg(me.est, l["value"], me.verdict)
        l["reason"] = reason_line(me, form, news)
        if l["rating"] != "Strong" or me.est < 0.85:
            pool = [x for x in all_legs if related(me, x)]
            for x in pool:
                apply_stats(x, form)
            l["alternatives"] = [{**x.to_json(), "rating": rate_leg(x.est, x.est * x.odds - 1, x.verdict),
                                  "fair_odds": round(1 / x.est, 2) if x.est > 0 else None,
                                  "value": round(x.est * x.odds - 1, 4), "rated": True, "state": "upcoming",
                                  "idx": l["idx"], "result": None}
                                 for x in best_swaps(me, pool, rules)]

    def build(self, code: str, legs: list[dict], data: dict, rules: Constraints | None) -> dict:
        comb = combined(legs)
        g = grade_slip(legs)
        open_ = [l for l in legs if l["result"] is None and l["state"] != "unavailable"]
        weakest = [l["idx"] for l in sorted([l for l in open_ if l.get("est") is not None], key=lambda x: x["est"])
                   if l.get("rating") in ("Weak", "Avoid", "OK")][:3]
        settled = [l["result"] for l in legs if l["result"]]
        status = ("lost" if "lost" in settled else
                  "won" if legs and all(l["result"] in ("won", "void") or l["state"] == "unavailable" for l in legs)
                  else "running" if any(l["state"] in ("live", "ended") for l in legs) else "not started")
        counts = {r: sum(1 for l in open_ if l["rating"] == r) for r in (*RATINGS, NOT_RATED)}
        odds_known = all((l.get("booked_odds") or l.get("odds")) for l in legs
                         if l["state"] != "unavailable" and l["result"] != "void")
        sug = suggestions(legs, rules)
        deadline = data.get("deadline")
        summary = {
            "legs": len(legs), "total_odds": round(total_odds(legs), 2), "odds_known": odds_known, "chance": round(comb["chance"], 4),
            "approx_legs": comb["approx_legs"], "grade": g["grade"], "grade_detail": g, "weakest": weakest,
            "status": status, "ratings": counts,
            "rated": sum(1 for l in legs if l["rated"]), "started": sum(1 for l in legs if l["state"] != "upcoming"),
            "outside_top": sum(1 for l in legs if l.get("flags")),
            "valid_until": datetime.fromtimestamp(deadline / 1000, timezone.utc).isoformat() if deadline else None,
        }
        out = {"ok": True, "code": code, "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "legs": legs, "summary": summary, "suggestions": sug,
                "rules": rules.to_dict() if rules else None, "rules_text": rules.describe() if rules else "",
                "verdict": verdict_line(summary), "disclaimer": DISCLAIMER,
                "load_url": getattr(self.s.bookmaker, "load_code_url", "")}
        out["recommendation"] = recommendation(out)
        return out

    async def _form(self, ev):
        try:
            return await self.s.scanner.match_form(ev)
        except Exception as exc:
            log.debug("form failed %s: %s", ev.event_id, exc)
            return None

    async def _news(self, ev):
        try:
            return await self.s.sr.match_news(ev.event_id, uid_of(ev.home_id), uid_of(ev.away_id))
        except Exception as exc:
            log.debug("news failed %s: %s", ev.event_id, exc)
            return None

    # ---- history -----------------------------------------------------------
    async def _booked_odds(self, code: str) -> dict:
        tr = getattr(self.s, "tracker", None)
        if not tr:
            return {}
        try:
            rec = await tr.history.find_code(code)
        except Exception:
            return {}
        out = {}
        for l in ((rec or {}).get("data") or {}).get("legs") or []:
            if l.get("odds"):
                out[(l.get("event_id"), str(l.get("market_id")), l.get("specifier") or "", str(l.get("outcome_id")))] = \
                    float(l["odds"])
        return out

    # ---- improved slip -----------------------------------------------------------
    async def rebook(self, aid: str, save: bool = True) -> dict:
        """Book the improved slip of analysis `aid`. Only ever called from an explicit user tap."""
        from bookmakers import BookingError, Selection
        res = self.get(aid)
        if not res:
            raise ValueError("That analysis expired. Analyze the code again.")
        imp = (res.get("suggestions") or {}).get("improved")
        if not imp or not imp.get("legs"):
            raise ValueError("There's no improved slip for this code.")
        if not imp.get("bookable"):
            raise ValueError("The improved slip breaks your rules; not booked.")
        now = datetime.now(timezone.utc)
        for l in imp["legs"]:
            try:
                if l.get("kickoff") and datetime.fromisoformat(l["kickoff"]) <= now:
                    raise ValueError(f"{l['match']} has started: analyze the code again.")
            except (TypeError, ValueError) as exc:
                if "started" in str(exc):
                    raise
        if imp.get("code"):
            return {"code": imp["code"], "url": self.s.bookmaker.load_code_url, "legs": len(imp["legs"]),
                    "total_odds": imp["total_odds"], "chance": imp["chance"], "reused": True}
        sels = [Selection(l["event_id"], l["market_id"], l.get("specifier") or "", l["outcome_id"]) for l in imp["legs"]]
        try:
            code = await self.s.bookmaker.book(sels)
        except BookingError:
            raise
        imp["code"] = code
        check = await self.s.bookmaker.load_code(code)
        tr = getattr(self.s, "tracker", None)
        saved = None
        if tr and save:
            saved = await tr.record(imp["legs"], code, "analyzer", "custom", f"Improved {res['code']}",
                                    imp["total_odds"], imp["chance"])
        return {"code": code, "url": self.s.bookmaker.load_code_url, "legs": len(sels),
                "loaded": check["legs"] if check else None, "total_odds": imp["total_odds"],
                "chance": imp["chance"], "history_id": saved["id"] if saved else None}

    # ---- narrative -----------------------------------------------------------------
    async def narrative(self, aid: str) -> dict:
        res = self.get(aid)
        if not res:
            raise ValueError("That analysis expired. Analyze the code again.")
        if res.get("narrative"):
            return {"text": res["narrative"], "source": res.get("narrative_source", "ai")}
        ai = getattr(self.s, "ai", None)
        facts = facts_for_narrative(res)
        text, src = "", "rules"
        if ai and ai.enabled:
            try:
                text = await ai.narrate(facts)
                src = "ai"
            except Exception as exc:
                log.warning("analyzer narrative failed: %s", exc)
                text = ""
            if text and not grounded(text, facts):
                log.info("analyzer narrative dropped: number not in the facts")
                text = ""
        if not text:
            text, src = fallback_narrative(res), "rules"
        res["narrative"], res["narrative_source"] = text, src
        return {"text": text, "source": src}

    # ---- compact form for the AI tool ---------------------------------------------
    @staticmethod
    def compact(res: dict) -> dict:
        if not res.get("ok"):
            return {"error": res.get("error"), "kind": res.get("kind")}
        s = res["summary"]
        imp = res["suggestions"].get("improved")
        return {
            "code": res["code"], "verdict": res.get("recommendation", {}).get("text"), "grade": s["grade"],
            "status": s["status"], "total_odds": s["total_odds"],
            "est_hit_chance": f"{s['chance']:.0%}", "not_rated_legs_in_chance": s["approx_legs"],
            "legs": [{"kickoff": l.get("kickoff"), "match": l["match"], "league": l["league"], "market": l["market"],
                      "pick": l["pick"], "odds": l.get("odds"), "state": l["state"], "score": l.get("score") or None,
                      "result": l.get("result"),
                      "est": f"{l['est']:.0%}" if l.get("est") is not None else "not rated",
                      "fair_odds": l.get("fair_odds"),
                      "value": f"{l['value']:+.0%}" if l.get("value") is not None else None,
                      "rating": l["rating"], "reason": l["reason"],
                      "outside_top_leagues": bool(l.get("flags")),
                      "safer_swap": (f"{l['alternatives'][0]['pick']} @{l['alternatives'][0]['odds']:.2f} "
                                     f"(est {l['alternatives'][0]['est']:.0%})") if l.get("alternatives") else None}
                     for l in res["legs"]],
            "drop_suggestions": [f"drop {d['match']} {d['pick']}: chance -> {d['chance']:.0%}, odds -> "
                                 f"{d['total_odds']:.2f}" for d in res["suggestions"]["drops"]],
            "improved_slip": ({"changes": imp["changes"], "est_hit_chance": f"{imp['chance']:.0%}",
                               "total_odds": imp["total_odds"],
                               "note": "Booked only if the user taps 'Rebook improved slip'."} if imp else None),
        }


def verdict_line(s: dict) -> str:
    if s["grade"] is None:
        return {"won": "Every leg landed.", "lost": "This slip has lost."}.get(s["status"], "Every leg is settled.")
    c = s["chance"]
    word = {"A": "Solid", "B": "Decent", "C": "Risky", "D": "Weak", "F": "Long shot"}[s["grade"]]
    extra = ""
    r = s["ratings"]
    if r.get("Avoid"):
        extra = f" · {r['Avoid']} leg{'s' if r['Avoid'] > 1 else ''} to avoid"
    elif r.get("Weak"):
        extra = f" · {r['Weak']} weak leg{'s' if r['Weak'] > 1 else ''}"
    lost = " · already lost" if s["status"] == "lost" else ""
    out = s.get("outside_top") or 0
    top = f" · {out} outside top leagues" if out else ""
    cs = f"{c * 100:.1f}%" if c < 0.095 else f"{c:.0%}"
    return f"{word} slip: est. {cs} to land{extra}{top}{lost}"


def _change_words(c: str) -> str:
    """'drop A vs B: Pick' -> 'dropping A v B (Pick)'; 'swap A vs B: x → y' -> 'switching A v B to y'."""
    c = c.replace(" vs ", " v ")
    if c.startswith("drop unavailable"):
        return "removing the unavailable selection"
    if c.startswith("drop "):
        m, _, pick = c[5:].partition(": ")
        return f"dropping {m} ({pick})"
    if c.startswith("swap "):
        m, _, rest = c[5:].partition(": ")
        return f"switching {m} to {rest.split(' → ')[-1]}"
    return c


def recommendation(res: dict) -> dict:
    """The analyzer's last line: ✅ go / ⚠️ go only after a change / ❌ skip, from the grade, the combined chance
    and whether the suggested fix turns it into an A/B slip. Settled slips just say how they ended."""
    s = res["summary"]
    legs = res["legs"]
    if s["status"] == "lost":
        return {"kind": "settled", "icon": "❌", "text": "❌ Already lost: a leg has gone down, nothing left to decide."}
    if s["grade"] is None:
        return {"kind": "settled", "icon": "✅" if s["status"] == "won" else "➖",
                "text": "✅ Already landed." if s["status"] == "won" else "➖ Every leg is settled."}
    open_ = [l for l in legs if l["result"] is None and l["state"] != "unavailable"]
    bad = [l for l in open_ if l.get("rating") in ("Avoid", "Weak")]
    if s["grade"] in ("A", "B") and not bad:
        return {"kind": "go", "icon": "✅", "text": f"✅ I'd go for this (grade {s['grade']}, est. {_p(s['chance'])} "
                                                     "to land; still an estimate, not a guarantee)."}
    fix_txt, fix_grade, fix_p = "", None, None
    imp = (res.get("suggestions") or {}).get("improved")
    if imp and imp.get("bookable") and imp.get("legs"):
        g = grade_slip(imp["legs"])["grade"]
        n_chg = sum(1 for c in imp["changes"] if not c.startswith("drop unavailable"))
        if g in ("A", "B") and n_chg > max(1, len(open_) // 2):
            why = f"grade {s['grade']}, est. {_p(s['chance'])} to land"
            return {"kind": "skip", "icon": "❌",
                    "text": f"❌ I'd skip this as built ({why}). The improved slip changes {n_chg} of {len(open_)} "
                            f"legs (grade {g}, est. {_p(imp['chance'])}): really a different slip."}
        if g in ("A", "B"):
            words = [_change_words(c) for c in imp["changes"]]
            fix_txt = ", ".join(words[:2]) + (f" (+{len(words) - 2} more)" if len(words) > 2 else "")
            fix_grade, fix_p = g, imp["chance"]
    if not fix_txt:
        for d in (res.get("suggestions") or {}).get("drops") or []:
            rest = [l for l in open_ if l["idx"] != d["idx"]]
            g = grade_slip(rest)["grade"] if rest else None
            if g in ("A", "B"):
                fix_txt = f"dropping {d['match'].replace(' vs ', ' v ')} ({d['pick']})"
                fix_grade, fix_p = g, d["chance"]
                break
    if fix_txt:
        return {"kind": "fix", "icon": "⚠️", "text": f"⚠️ I'd go for this only after {fix_txt}: grade {fix_grade}, "
                                                      f"est. {_p(fix_p)} to land."}
    if s["grade"] == "B":
        w = bad[0] if bad else None
        return {"kind": "fix", "icon": "⚠️",
                "text": "⚠️ Borderline: I'd only go for this if you're happy carrying "
                        + (f"{w['match'].replace(' vs ', ' v ')} ({w['pick']}, {w['rating']})." if w else "the risk.")}
    why = f"grade {s['grade']}, est. {_p(s['chance'])} to land"
    if s["ratings"].get("Avoid"):
        why += f", {s['ratings']['Avoid']} leg(s) to avoid"
    return {"kind": "skip", "icon": "❌", "text": f"❌ I'd skip this ({why}) and no simple fix makes it solid."}


def _p(x: float) -> str:
    return f"{x * 100:.1f}%" if x < 0.095 else f"{x:.0%}"
