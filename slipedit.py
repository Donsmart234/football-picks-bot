"""Edits of a slip the user is looking at (replying to): replace / remove / add a leg, change the market on a leg,
make it safer, or rebuild it. Pure code (no network): the caller passes the slip's legs, the candidate legs that
already satisfy the section's rules (rollover: top leagues, stats-confirmed, est >= 80%, no unders, not started; scan:
the tier + the user's constraints) and the window the total odds must stay in.

parse_edit(text, legs) turns natural phrasing (incl. Nigerian English: "comot the second one", "change am",
"abeg make e safer", "this Newcastle game no go enter") into one op:
    {"action": replace|remove|add|change_market|safer|rebuild, "index": 0-based or None, "market": hint or None,
     "same_match": bool}
apply_edit(legs, op, cands, rules) -> {"ok", "legs", "total_odds", "est_prob", "note", "change", "removed", "added"}
Nothing here books or saves anything.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from constraints import leg_tags
from matching import normalize

try:
    from rapidfuzz import fuzz
except Exception:            # pragma: no cover
    fuzz = None

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
            "ninth": 9, "9th": 9, "tenth": 10, "10th": 10, "eleventh": 11, "11th": 11, "twelfth": 12, "12th": 12}
CARDINALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12}
NOUN = r"(?:leg|game|match|selection|pick|number|no\.?|option|bet|line|event|fixture|one)"

RE_REMOVE = re.compile(r"\b(remove|drop|delete|comot|commot|take (?:it |am |that |the \w+ )?(?:out|off)|cancel|"
                       r"scrap|cut|knock (?:it |am )?off|yank|clear|without)\b")
RE_ADD = re.compile(r"\b(add|include|put (?:another|one more|more|extra|in another)|one more (?:game|leg|match|selection|pick)|"
                    r"another (?:game|leg|match|selection) (?:to|on)|extra (?:game|leg|match|selection|pick))\b")
RE_SAFER = re.compile(r"\b(safer|safe|safest|sure[r]?|surest|less risk(?:y)?|lower risk|reduce (?:the )?risk|"
                      r"more secure|banker|low risk|risky|no go (?:enter|land)|won'?t (?:enter|land|cut)|"
                      r"e no sure|not sure|too risky|weak(?:est)?|shaky|dodgy)\b")
RE_REBUILD = re.compile(r"\b(different picks|new picks|fresh picks|new slip|fresh slip|another slip|change (?:all|everything|"
                        r"the whole(?: slip| thing)?|them all|all of them)|replace (?:all|everything|them all)|rebuild|"
                        r"start (?:again|afresh|over)|redo)\b")
RE_REPLACE = re.compile(r"\b(change|chang|replace|swap|switch|edit|different|another|substitute|sub|instead|"
                        r"don'?t like|dont like|no like|i no like|remove and replace|update|modify|fix|use another)\b")
RE_MARKET = re.compile(r"\b(market|same (?:match|game)|on that (?:match|game)|in that (?:match|game)|"
                       r"to (?:over|under|gg|btts|ng|dc|double chance|1x|x2|12|draw no bet|dnb|home|away|win|handicap|"
                       r"both teams)|make (?:it|am) (?:over|under|gg|btts|dc|double chance|1x|x2|12|draw no bet|dnb))\b")

MARKET_HINTS = [
    (r"double chance|\bdc\b|\b1x\b|\bx2\b|\b12\b", {"tags": ["double_chance"]}),
    (r"draw no bet|\bdnb\b", {"tags": ["dnb"]}),
    (r"\bgg\b|btts|both teams(?: to)? score|\bng\b", {"tags": ["btts"]}),
    (r"handicap|\bah\b|\beh\b", {"tags": ["handicap"]}),
    (r"corner", {"tags": ["corners"]}),
    (r"card|booking", {"tags": ["cards"]}),
    (r"1x2|straight win|to win|home win|away win|\bwin\b", {"tags": ["1x2"]}),
    (r"\bover\b", {"tags": ["over"]}),
    (r"\bunder\b", {"tags": ["under"]}),
]


def _norm_text(t: str) -> str:
    t = (t or "").lower().replace("’", "'")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def total_odds(legs: list[dict]) -> float:
    return math.prod(float(l.get("odds") or 1.0) for l in legs) if legs else 1.0


def combined(legs: list[dict]) -> float:
    if not legs:
        return 0.0
    p = 1.0
    for l in legs:
        e = l.get("est")
        p *= float(e) if isinstance(e, (int, float)) else (1.0 / float(l.get("odds") or 1.0)) / 1.06
    return p


def sel_key(l: dict) -> str:
    return f"{l.get('event_id')}|{l.get('market_id')}|{l.get('specifier') or ''}|{l.get('outcome_id')}"


def leg_name(l: dict) -> str:
    if l.get("home") and l.get("away"):
        return f"{l['home']} v {l['away']}"
    return (l.get("match") or "").replace(" vs ", " v ")


def leg_short(l: dict) -> str:
    return f"{leg_name(l)} {l.get('pick', '')} @{float(l.get('odds') or 0):.2f}"


# ------------------------------------------------------------------ intent
def _team_index(text: str, legs: list[dict]) -> int | None:
    """Leg whose team the message names ('the Newcastle game', 'remove benfica')."""
    t = normalize(text)
    words = set(t.split())
    best, best_s = None, 0.0
    for i, l in enumerate(legs):
        for team in (l.get("home"), l.get("away")):
            n = normalize(team or "")
            if not n:
                continue
            toks = [w for w in n.split() if len(w) >= 4]
            s = 0.0
            if n in t:
                s = 100.0
            elif toks and any(w in words for w in toks):
                s = 92.0
            elif fuzz is not None and len(n) >= 5:
                cand = max((fuzz.ratio(w, tok) for w in words for tok in (toks or [n]) if len(w) >= 4), default=0)
                s = cand if cand >= 88 else 0.0
            if s > best_s:
                best, best_s = i, s
    return best if best_s >= 88 else None


def parse_index(text: str, legs: list[dict]) -> int | None:
    t = _norm_text(text)
    n = len(legs)
    if not n:
        return None
    m = re.search(r"\b(last|final|bottom|lowest one|end one)\b", t)
    if m and not re.search(r"\blast (?:night|week|time|season|match day)\b", t):
        return n - 1
    for w, k in ORDINALS.items():
        if re.search(rf"\b{w}\b", t):
            return k - 1 if k <= n else None
    m = re.search(rf"\b{NOUN}\s*(?:#|no\.?\s*)?(\d{{1,2}})(?![\d.])", t) or re.search(r"#\s*(\d{1,2})\b", t) or \
        re.search(r"\b(?:change|replace|swap|remove|drop|comot|switch|edit)\s+(?:no\.?\s*)?(\d{1,2})(?![\d.])", t) or \
        re.search(r"^\s*(\d{1,2})\s*$", t)
    if m:
        k = int(m.group(1))
        return k - 1 if 1 <= k <= n else None
    for w, k in CARDINALS.items():
        if re.search(rf"\b{NOUN} {w}\b", t) or re.search(rf"\bnumber {w}\b", t):
            return k - 1 if k <= n else None
    return _team_index(text, legs)


def parse_market(text: str) -> dict | None:
    t = _norm_text(text)
    # the part after "to"/"make it" names the target market ("change leg 2 to over 1.5")
    m = re.search(r"\b(?:to|make (?:it|am)|into|for)\s+(.+)$", t)
    seg = m.group(1) if m else t
    for rx, hint in MARKET_HINTS:
        if re.search(rx, seg):
            out = dict(hint)
            line = re.search(r"\b(over|under)\s*(\d+(?:\.5)?)\b", seg)
            if line:
                out["line"] = float(line.group(2))
            dc = re.search(r"\b(1x|x2|12)\b", seg)
            if dc:
                out["pick"] = dc.group(1).upper()
            out["text"] = seg[:40]
            return out
    return None


def parse_edit(text: str, legs: list[dict]) -> dict | None:
    """One edit op from the user's words, or None when the message isn't recognisably an edit."""
    t = _norm_text(text)
    if not t:
        return None
    idx = parse_index(t, legs)
    if RE_REBUILD.search(t):
        return {"action": "rebuild", "index": None, "market": None, "same_match": False}
    mk = parse_market(t) if RE_MARKET.search(t) else None
    if mk and idx is not None and (RE_MARKET.search(t) or "market" in t):
        return {"action": "change_market", "index": idx, "market": mk, "same_match": True}
    if RE_MARKET.search(t) and idx is not None and re.search(r"\bmarket\b|same (?:match|game)", t):
        return {"action": "change_market", "index": idx, "market": parse_market(t), "same_match": True}
    if RE_REMOVE.search(t) and not re.search(r"\b(replace|swap|change|switch|put another|instead)\b", t):
        if idx is not None:
            return {"action": "remove", "index": idx, "market": None, "same_match": False}
        if RE_SAFER.search(t):
            return {"action": "remove", "index": None, "market": None, "same_match": False, "weakest": True}
        return None
    if RE_ADD.search(t) and idx is None:
        return {"action": "add", "index": None, "market": None, "same_match": False}
    if RE_SAFER.search(t) and not RE_REPLACE.search(t.replace("make it", "")):
        return {"action": "safer", "index": idx, "market": None, "same_match": False}
    if RE_SAFER.search(t) and idx is None:
        return {"action": "safer", "index": None, "market": None, "same_match": False}
    if idx is not None and (RE_REPLACE.search(t) or RE_SAFER.search(t) or len(t.split()) <= 4):
        return {"action": "replace", "index": idx, "market": None, "same_match": False}
    return None


# ------------------------------------------------------------------ engine
def _kick(l: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(l.get("kickoff") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def leg_problem(c: dict, rules: dict, now: datetime | None = None) -> str:
    """'' when a candidate leg obeys the section's rules, else why not."""
    now = now or datetime.now(timezone.utc)
    est = c.get("est")
    if not isinstance(est, (int, float)) or est < rules.get("min_p", 0.0) - 1e-9:
        return f"est below {rules.get('min_p', 0):.0%}"
    o = float(c.get("odds") or 0)
    if o < rules.get("min_odds", 1.01) - 1e-9 or o > rules.get("max_odds", 1000.0) + 1e-9:
        return "odds outside the band"
    if rules.get("no_unders") and "under" in leg_tags(c):
        return "under selection"
    if rules.get("require_confirmed") and c.get("verdict") != "confirmed":
        return "not confirmed by the stats check"
    if c.get("verdict") == "dropped":
        return "dropped by the stats check"
    k = _kick(c)
    if k is None or k <= now + timedelta(minutes=5):
        return "kick-off too close or started"
    return ""


def _fits(total: float, rules: dict) -> bool:
    return rules["lo"] - 1e-9 <= total <= rules["hi"] + 1e-9


def _pick_best(singles: list[dict], need_mid: float) -> dict:
    return max(singles, key=lambda c: (round(float(c["est"]), 3), -abs(math.log(float(c["odds"]) / need_mid))))


def _fill(kept: list[dict], pool: list[dict], rules: dict, max_new: int = 2) -> list[dict] | None:
    """Best 1 (else 2) new legs from `pool` (distinct matches, not already in `kept`) so the total lands in the
    window, maximising the combined estimate. None when nothing fits."""
    used = {l.get("event_id") for l in kept}
    pool = [c for c in pool if c.get("event_id") not in used]
    r = total_odds(kept)
    lo, hi = rules["lo"] / r, rules["hi"] / r
    target_mid = (rules.get("target") or math.sqrt(rules["lo"] * rules["hi"])) / r
    singles = [c for c in pool if lo - 1e-9 <= float(c["odds"]) <= hi + 1e-9]
    if singles:
        return [_pick_best(singles, max(target_mid, 1.01))]
    if max_new < 2 or hi < 1.02:
        return None
    top = sorted([c for c in pool if float(c["odds"]) <= hi], key=lambda c: -float(c["est"]))[:60]
    best, best_p = None, 0.0
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            a, b = top[i], top[j]
            if a.get("event_id") == b.get("event_id"):
                continue
            o = float(a["odds"]) * float(b["odds"])
            if lo - 1e-9 <= o <= hi + 1e-9:
                p = float(a["est"]) * float(b["est"])
                if p > best_p:
                    best, best_p = [a, b], p
    return best


def _result(ok: bool, legs: list[dict], note: str, change: str = "", removed=None, added=None, action: str = "") -> dict:
    return {"ok": ok, "action": action, "legs": legs, "total_odds": round(total_odds(legs), 2),
            "est_prob": round(combined(legs), 4), "note": note, "change": change,
            "removed": removed or [], "added": added or []}


def _hint_ok(c: dict, hint: dict | None) -> bool:
    if not hint:
        return True
    tags = set(leg_tags(c))
    if not set(hint.get("tags") or []) <= tags:
        return False
    pick = str(c.get("pick") or "").lower()
    if hint.get("line") is not None and not re.search(rf"\b{hint['line']:g}\b", pick + " " + str(c.get("specifier") or "")):
        return False
    if hint.get("pick") and hint["pick"].lower() not in pick.replace(" ", "").lower() and \
            hint["pick"].lower() not in str(c.get("pick") or "").lower():
        return False
    return True


def apply_edit(legs: list[dict], op: dict, cands: list[dict], rules: dict, now: datetime | None = None) -> dict:
    """Apply one edit op. `rules`: lo/hi (total-odds window), target, min_p, min_odds, max_odds, no_unders,
    require_confirmed, strict (window must hold after remove/add), label (for messages)."""
    now = now or datetime.now(timezone.utc)
    legs = [dict(l) for l in legs]
    action = op.get("action")
    prev = {sel_key(l) for l in legs} | set(rules.get("exclude_keys") or [])
    pool = [c for c in cands if not leg_problem(c, rules, now) and sel_key(c) not in prev]
    win = f"{rules['lo']:.2f}–{rules['hi']:.2f}"
    bar = (f"every leg est. ≥{rules.get('min_p', 0):.0%}" + (", stats-confirmed" if rules.get("require_confirmed") else "")
           + (", no unders" if rules.get("no_unders") else "") + (", top leagues" if rules.get("top_only") else "")
           + ", not kicked off")
    idx = op.get("index")
    if idx is not None and not (0 <= idx < len(legs)):
        return _result(False, legs, f"This slip has {len(legs)} legs; there's no leg {idx + 1}.", action=action)

    if action in ("replace", "safer", "change_market"):
        if idx is None and action == "safer":
            scored = [i for i, l in enumerate(legs) if isinstance(l.get("est"), (int, float))]
            if not scored:
                return _result(False, legs, "I can't rate these legs, so I can't tell which one is weakest.", action=action)
            idx = min(scored, key=lambda i: legs[i]["est"])
        if idx is None:
            return _result(False, legs, "Which leg? Say e.g. 'change the 2nd selection' or name the team.", action=action)
        old = legs[idx]
        kept = legs[:idx] + legs[idx + 1:]
        if action == "change_market":
            same = [c for c in pool if c.get("event_id") == old.get("event_id") and _hint_ok(c, op.get("market"))]
            new = _fill(kept, same, rules, max_new=1)
            if not new:
                what = (op.get("market") or {}).get("text") or "another market"
                return _result(False, legs, f"No {what} pick on {leg_name(old)} fits: it must keep the total in "
                               f"{win} with {bar}. Your slip is unchanged.", action=action)
        else:
            others = [c for c in pool if c.get("event_id") != old.get("event_id")] if action == "replace" else pool
            if action == "safer":
                others = [c for c in others if float(c["est"]) > float(old.get("est") or 0) + 0.005]
            new = _fill(kept, others, rules)
            if not new:
                if action == "safer":
                    return _result(False, legs, f"Leg {idx + 1} ({leg_short(old)}) is already the strongest pick that "
                                   f"keeps the total in {win} under the rules ({bar}). Your slip is unchanged.",
                                   action=action)
                return _result(False, legs, f"No qualifying replacement for leg {idx + 1} ({leg_short(old)}): the other "
                               f"legs total {total_odds(kept):.2f}, so the new pick needs odds about "
                               f"{rules['lo'] / total_odds(kept):.2f}–{rules['hi'] / total_odds(kept):.2f} with {bar}, "
                               "and none is on offer right now. Your slip is unchanged.", action=action)
        out = legs[:idx] + new + legs[idx + 1:]
        ch = (f"Leg {idx + 1} {leg_short(old)} → " + " + ".join(leg_short(n) for n in new))
        return _result(True, out, "", ch, [old], new, action)

    if action == "remove":
        if idx is None and op.get("weakest"):
            scored = [i for i, l in enumerate(legs) if isinstance(l.get("est"), (int, float))]
            idx = min(scored, key=lambda i: legs[i]["est"]) if scored else None
        if idx is None:
            return _result(False, legs, "Which leg should go? Say e.g. 'remove the 3rd one' or name the team.",
                           action=action)
        if len(legs) <= 1:
            return _result(False, legs, "That's the only leg on the slip.", action=action)
        old = legs[idx]
        kept = legs[:idx] + legs[idx + 1:]
        t = total_odds(kept)
        if rules.get("strict") and not _fits(t, rules):
            return _result(False, legs, f"Without leg {idx + 1} the slip totals {t:.2f}, outside the {win} the "
                           f"{rules.get('label', 'slip')} needs. Say 'change leg {idx + 1}' and I'll swap it for a "
                           "qualifying pick instead. Your slip is unchanged.", action=action)
        return _result(True, kept, "" if _fits(t, rules) else f"Total is now {t:.2f}.",
                       f"Removed leg {idx + 1} {leg_short(old)}", [old], [], action)

    if action == "add":
        t = total_odds(legs)
        if rules.get("strict"):
            new = _fill(legs, pool, rules, max_new=1)
            if not new:
                return _result(False, legs, f"Adding a leg would push the total ({t:.2f} now) outside the {win} the "
                               f"{rules.get('label', 'slip')} needs, with {bar}. Your slip is unchanged.", action=action)
        else:
            used = {l.get("event_id") for l in legs}
            free = [c for c in pool if c.get("event_id") not in used and float(c["odds"]) >= 1.10]
            if not free:
                return _result(False, legs, f"No qualifying leg to add right now ({bar}). Your slip is unchanged.",
                               action=action)
            new = [max(free, key=lambda c: (float(c["est"]), float(c["odds"])))]
        out = legs + new
        return _result(True, out, "", "Added " + " + ".join(leg_short(n) for n in new), [], new, action)

    return _result(False, legs, "I couldn't tell which change you want. Try 'change the 2nd selection', "
                   "'remove Newcastle', 'change leg 3 to over 1.5' or 'make it safer'.", action=action or "")
