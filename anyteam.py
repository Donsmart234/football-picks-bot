"""football.com "Any Team Over 1.5" slip builder: one booked football.com code (user request 2026-11-13).

Market 60045 "Any Team Over X.5" (specifier total=1.5, outcome 74 = Yes, 76 = No) is only on football.com (SportyBet's
event detail doesn't carry it) and only in the event detail, not the upcoming list. So:

    1. list the upcoming matches on football.com (same Sportradar/SportyBet feed, bookmakers.FootballComNG)
    2. keep the matches in scope: national teams (Sportradar category International, senior sides) when they are on,
       else the top-leagues allowlist (leagues.py); a league / competition / team named in the text is a HARD filter
       (only those, never padded with others); "all leagues" lifts the scope
    3. read each candidate's event detail (12 at a time) and keep the ones whose Yes is open at leg odds lo-hi
    4. rank by the estimated chance: the de-margined Yes from the 60045 Yes/No pair (power method); a Poisson fit of
       the match's 1X2 + Over/Under 2.5 is shown alongside as a check (P(either team scores 2+))
    5. book the top N (one leg per match) as one football.com code, save it to History (bookmaker football.com) and
       answer with the short reply. Refresh = N matches that were in no earlier Any Team code of this owner for the
       same window (used ids in the history store, kind 'atused').

Defaults agreed 2026-11-13: 10 legs, leg odds 1.15-1.35, Yes only, one leg per match, not started, next 72 h.
Window option (2026-11-14): "3d" (next 3 days, default), "1w" (next 7 days), "2w" (next 14 days). football.com lists
games ~1-2 weeks ahead, so a week window takes whatever is listed inside it. In a week window the automatic scope is
decided per WAT day (national teams on days with >= 4 senior national-team matches, top leagues on the other days), so
an international break followed by league weekends gives both; the 3-day window keeps the whole-window rule.
Estimates, not guarantees: the combined chance assumes the legs are independent.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import leagues
from bookmakers import BookingError, Selection, footballcom_share_url

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)

MARKET_ID = "60045"
SPEC = "total=1.5"
YES, NO = "74", "76"
MARKET_NAME = "Any Team Over 1.5"
PICK = "Yes"
BOOKMAKER_KEY = "footballcom"
BOOKMAKER_LABEL = "football.com"

DEFAULT_LEGS = 10
DEFAULT_LO, DEFAULT_HI = 1.15, 1.35
DEFAULT_HOURS = 72
MAX_LEGS = 50
MIN_LEAD = timedelta(minutes=5)          # matches must start at least this far ahead
DETAIL_CONCURRENCY = 12
MAX_DETAILS = 260                         # cap on event-detail reads per search (top leagues over 72 h ~ 150)
MAX_DETAILS_LONG = 300                    # week windows: the 300 likeliest candidates (list Poisson pre-check)
DETAIL_TTL = 600                          # event details cached 10 min (1w and 2w share reads, football.com rate-limits)
WINDOWS = {"3d": 72, "1w": 168, "2w": 336}
WINDOW_TEXT = {"3d": "next 3 days", "1w": "next 7 days", "2w": "next 14 days"}
AUTO_NATIONAL_MIN = 4                     # national-team matches in the window that switch the default scope on
RESULT_TTL = 300
USED_PREFIX, USED_KIND = "atu", "atused"
LABEL = "Any Team Over 1.5"

NATIONAL_CATEGORY = "sr:category:4"       # Sportradar "International" (senior national teams)
YOUTH_CATEGORY = "sr:category:392"        # "International Youth" (U17-U23): only when asked
_EXCLUDE = ("simulated", "srl", "esoccer", "virtual", "ebattle", "e-football")
_WOMEN = re.compile(r"\bwomen\b|\bwomens\b|\bfemenin|\bfeminin|\bladies\b|\(w\)", re.I)
_YOUTH = re.compile(r"\bu-?(1[5-9]|2[0-3])\b|\byouth\b", re.I)

# national-team competitions by words (Sportradar unique tournament ids, leagues.py)
COMPETITION_WORDS: list[tuple[re.Pattern, str, set[int]]] = [
    (re.compile(r"\b(afcon|africa(n)?\s+cup(\s+of\s+nations)?)\s*(qualifiers?|qualification|quali|q)\b", re.I),
     "AFCON qualifiers", {1848}),
    (re.compile(r"\b(afcon|africa(n)?\s+cup(\s+of\s+nations)?)\b", re.I), "AFCON", {270, 1848}),
    (re.compile(r"\b(world\s*cup\s*(qualifiers?|qualification|quali)|wcq)\b", re.I), "World Cup qualifiers",
     {11, 295, 13, 308, 14, 20434}),
    (re.compile(r"\bworld\s*cup\b", re.I), "World Cup", {16, 11, 295, 13, 308, 14, 20434}),
    (re.compile(r"\bconcacaf\s+nations\s+league\b", re.I), "CONCACAF Nations League", {27420}),
    (re.compile(r"\b(uefa\s+)?nations\s+league\b", re.I), "Nations League", {23755, 27420}),
    (re.compile(r"\beuros?\s*(qualifiers?|qualification|quali)\b", re.I), "Euro qualifiers", {27}),
    (re.compile(r"\b(international\s+)?friendl(y|ies)\b", re.I), "friendlies", {851}),
    (re.compile(r"\basian\s+cup\b", re.I), "Asian Cup", {246, 28}),
    (re.compile(r"\bgold\s+cup\b", re.I), "Gold Cup", {140}),
]

_WANTS = re.compile(r"\bany\s*-?\s*team\s*(to\s+score\s+)?(over|o)\s*1\.5\b|\banyteam\b|\bany\s*team\s*o1\.5\b|"
                    r"\bany\s+team\s+over\b", re.I)


def wants_anyteam(text: str) -> bool:
    """Chat phrases routed to this builder: "any team over 1.5", "anyteam 10 legs", "any team o1.5 code"."""
    return bool(_WANTS.search(text or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b %H:%M")


def _id(x) -> int | None:
    s = str(x or "").rsplit(":", 1)[-1]
    return int(s) if s.isdigit() else None


# ------------------------------------------------------------------ request parsing
@dataclass
class Request:
    n: int = DEFAULT_LEGS
    lo: float = DEFAULT_LO
    hi: float = DEFAULT_HI
    hours: int = DEFAULT_HOURS
    day: str = ""                  # "" (rolling hours) | today | tomorrow | weekend
    scope: str = "auto"            # auto | national | top | all
    youth: bool = False
    women: bool = False
    competitions: list = None      # [(label, ids)]
    leagues_c: object = None       # constraints.Constraints with include_leagues / include_countries only
    team_text: str = ""            # normalised text the team names are looked up in
    refresh: bool = False
    n_given: bool = False
    odds_given: bool = False
    win: str = "3d"                # 3d | 1w | 2w ("" = custom hours)
    win_given: bool = False

    @property
    def multi_day(self) -> bool:
        """Week windows: the automatic scope is decided per day."""
        return not self.day and self.hours > DEFAULT_HOURS

    @property
    def named(self) -> bool:
        lc = self.leagues_c
        return bool(self.competitions or (lc is not None and (lc.include_leagues or lc.include_countries)))


_WIN_2W = re.compile(r"\b(?:2|two)\s*-?\s*weeks?\b|\b14\s*-?\s*days?\b|\bfortnight\b|\b2\s*w(?:ks?)?\b", re.I)
_WIN_1W = re.compile(r"\b(?:1|one|a)\s*-?\s*week\b|\b7\s*-?\s*days?\b|\b1\s*w(?:k)?\b|\bweek\b", re.I)
_WIN_3D = re.compile(r"\b(?:3|three)\s*-?\s*days?\b|\b3\s*d\b", re.I)


def parse_window(text: str) -> tuple[str, str]:
    """('1w', text without the window words) for "for the week" / "1 week" / "7 days"; '2w' for "2 weeks" /
    "14 days" / "fortnight"; '3d' for "3 days"; ('', text) when no window is named. "weekend" / "midweek" are not
    week windows."""
    t = text or ""
    for key, rx in (("2w", _WIN_2W), ("1w", _WIN_1W), ("3d", _WIN_3D)):
        m = rx.search(t)
        if m:
            return key, t[:m.start()] + " " + t[m.end():]
    return "", t


def norm_window(w) -> str:
    """API / Mini App value -> '3d' | '1w' | '2w' | '' (unknown)."""
    s = str(w or "").strip().lower().replace(" ", "")
    if s in WINDOWS:
        return s
    k, _ = parse_window(str(w or ""))
    return k or {"72h": "3d", "168h": "1w", "336h": "2w", "week": "1w", "7d": "1w", "14d": "2w"}.get(s, "")


def parse_request(text: str, n: int | None = None, lo: float | None = None, hi: float | None = None,
                  win: str | None = None) -> Request:
    """'/anyteam 8 1.2-1.3 nigeria egypt today' -> Request. Numbers given as arguments win over the text.
    Window words: "week" / "1 week" / "7 days" -> 1w, "2 weeks" / "14 days" / "fortnight" -> 2w, "3 days" -> 3d."""
    t = " " + (text or "").lower().replace("\u2013", "-").replace("\u2014", "-").replace(",", " , ") + " "
    r = Request(competitions=[])
    r.refresh = bool(re.search(r"\b(refresh|new\s+(picks|code|games|matches)|another|different)\b", t))
    t = re.sub(r"\bany\s*-?\s*team\s*(to\s+score\s+)?(over|o)\s*1\.5\b|\banyteam\b", " ", t)
    t = re.sub(r"/anyteam(@\w+)?", " ", t)
    m = re.search(r"(?<![\d.])(\d\.\d{1,2})\s*(?:-|to)\s*(\d(?:\.\d{1,2})?)(?![\d.])", t)
    if m:
        a, b = sorted((float(m.group(1)), float(m.group(2))))
        if 1.0 < a and b <= 20:
            r.lo, r.hi, r.odds_given = a, b, True
        t = t[:m.start()] + " " + t[m.end():]
    else:
        m1 = re.search(r"\bmin(?:imum)?\s*(?:leg\s*)?odds?\s*:?\s*(\d\.\d{1,2})", t)
        m2 = re.search(r"\bmax(?:imum)?\s*(?:leg\s*)?odds?\s*:?\s*(\d\.\d{1,2})", t)
        if m1:
            r.lo, r.odds_given = float(m1.group(1)), True
            t = t.replace(m1.group(0), " ")
        if m2:
            r.hi, r.odds_given = float(m2.group(1)), True
            t = t.replace(m2.group(0), " ")
    wk, t = parse_window(t)
    if win is not None and norm_window(win):
        wk = norm_window(win)
    if wk:
        r.win, r.win_given, r.hours = wk, True, WINDOWS[wk]
    mh = re.search(r"\b(?:next\s+)?(\d{1,3})\s*(?:h|hrs?|hours?)\b", t)
    if mh:
        if not r.win_given:
            r.hours = max(1, min(WINDOWS["2w"], int(mh.group(1))))
            r.win = next((k for k, v in WINDOWS.items() if v == r.hours), "")
        t = t[:mh.start()] + " " + t[mh.end():]
    for d in ("today", "tomorrow", "weekend"):
        if re.search(rf"\b{d}\b", t):
            r.day = d
            break
    ml = re.search(r"\b(\d{1,2})\s*(?:legs?|selections?|games?|matches|picks?|teams?|odds?\s+legs?)\b", t)
    if not ml:
        ml = re.search(r"(?<![\w.])(\d{1,2})(?![\w.])", t)
    if ml and 1 <= int(ml.group(1)) <= 99:
        r.n, r.n_given = int(ml.group(1)), True
        t = t[:ml.start()] + " " + t[ml.end():]
    if n is not None:
        r.n, r.n_given = int(n), True
    if lo is not None:
        r.lo, r.odds_given = float(lo), True
    if hi is not None:
        r.hi, r.odds_given = float(hi), True
    r.n = max(1, min(MAX_LEGS, r.n))
    if r.lo > r.hi:
        r.lo, r.hi = r.hi, r.lo
    r.youth = bool(re.search(r"\bu-?(1[5-9]|2[0-3])s?\b|\byouth\b|\bunder[- ]?(1[5-9]|2[0-3])\b", t))
    r.women = bool(re.search(r"\bwomen'?s?\b|\bladies\b", t))
    if re.search(r"(?<![a-z])(all|any|every|other)[- ](leagues?|competitions?|matches)(?![a-z])", t):
        r.scope = "all"
    elif re.search(r"\b(top|big|major|club)\s+(leagues?|football|games|matches)\b|\bleagues?\s+only\b|\bclubs?\b", t):
        r.scope = "top"
    elif re.search(r"\b(national|international|internationals|country|countries)\b", t):
        r.scope = "national"
    for rx, label, ids in COMPETITION_WORDS:
        mm = rx.search(t)
        if mm:
            r.competitions.append((label, set(ids)))
            t = t[:mm.start()] + " " + t[mm.end():]
    from constraints import Constraints, parse_text
    pc = parse_text(t)
    lc = Constraints()
    lc.include_leagues = [x for x in pc.include_leagues if x.lower() != "top"]
    lc.include_countries = list(pc.include_countries)
    r.leagues_c = lc
    r.team_text = t
    return r


def detect_teams(text: str, names) -> tuple[list[str], list[str]]:
    """Team names (from the window's real matches) written in the text: whole normalised name, word-bounded.
    'no X' / 'without X' / 'except X' excludes. Returns (include, exclude)."""
    from matching import normalize
    import unicodedata
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", plain)) + " "
    inc, exc, spans = [], [], []
    for name in sorted({str(n) for n in names if n}, key=len, reverse=True):
        forms = {normalize(name), re.sub(r"[^a-z0-9 ]+", " ", unicodedata.normalize("NFKD", name).encode(
            "ascii", "ignore").decode().lower()).strip()}
        for f in sorted(forms, key=len, reverse=True):
            f = re.sub(r"\s+", " ", f).strip()
            if len(f) < 4:
                continue
            m = re.search(r"(?<![a-z0-9])" + re.escape(f) + r"(?![a-z0-9])", t)
            if m and not any(m.start() < e and m.end() > b for b, e in spans):
                spans.append(m.span())
                before = t[max(0, m.start() - 12):m.start()]
                (exc if re.search(r"\b(no|not|without|except|exclude|minus|avoid)\s+$", before) else inc).append(name)
                break
    return inc, exc


# ------------------------------------------------------------------ window / scope
def window(req: Request, now: datetime | None = None) -> dict:
    now = (now or _now()).astimezone(timezone.utc)
    loc = now.astimezone(TZ)
    if req.day == "today":
        end = loc.replace(hour=23, minute=59, second=59, microsecond=0)
        label = "today"
    elif req.day == "tomorrow":
        d0 = (loc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {"start": d0.astimezone(timezone.utc), "end": (d0 + timedelta(hours=23, minutes=59, seconds=59))
                .astimezone(timezone.utc), "label": "tomorrow", "key": "tomorrow:" + d0.strftime("%Y-%m-%d")}
    elif req.day == "weekend":
        days = (6 - loc.weekday()) % 7
        end = (loc + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
        label = "this weekend"
    else:
        end = loc + timedelta(hours=req.hours)
        label = WINDOW_TEXT.get(req.win) if WINDOWS.get(req.win) == req.hours else f"next {req.hours} h"
    key = (req.day or f"{req.hours}h") + ":" + loc.strftime("%Y-%m-%d")
    return {"start": now, "end": end.astimezone(timezone.utc), "label": label, "key": key}


def _excluded(e) -> bool:
    s = f"{e.category} {e.league} {e.home} {e.away}".lower()
    return any(x in s for x in _EXCLUDE)


def is_national(e, youth: bool = False, women: bool = False) -> bool:
    """A national-team match: Sportradar category International (senior), or International Youth when asked.
    Women's sides only when asked."""
    cat = str(getattr(e, "category_id", "") or "")
    name = f"{e.league} {e.home} {e.away}"
    if not women and _WOMEN.search(name):
        return False
    if cat == NATIONAL_CATEGORY or (not cat and (e.category or "").strip().lower() == "international"):
        return youth or not _YOUTH.search(name)
    if youth and (cat == YOUTH_CATEGORY or (e.category or "").strip().lower() == "international youth"):
        return True
    return False


def in_filter(e, req: Request, teams: list[str]) -> bool:
    """Hard filter (named competitions / leagues / countries / teams): the match must hit at least one of them."""
    ut = _id(e.tournament_id)
    for _, ids in req.competitions or []:
        if ut in ids:
            return True
    lc = req.leagues_c
    if lc is not None and (lc.include_leagues or lc.include_countries) and lc.event_ok(e):
        return True
    return bool(teams) and (e.home in teams or e.away in teams)


def scope_events(events: list, req: Request, now: datetime, win: dict) -> tuple[str, list, dict]:
    """(scope, candidate events, info). scope: filter | national | top | all."""
    evs = [e for e in events if win["start"] <= e.start <= win["end"] and e.start > now + MIN_LEAD
           and not _excluded(e)]
    inc, exc = detect_teams(req.team_text, {n for e in evs for n in (e.home, e.away)})
    info = {"teams": inc, "excluded_teams": exc,
            "competitions": [c[0] for c in req.competitions or []],
            "leagues": list(getattr(req.leagues_c, "include_leagues", []) or [])
            + list(getattr(req.leagues_c, "include_countries", []) or [])}
    if exc:
        evs = [e for e in evs if e.home not in exc and e.away not in exc]
    if req.named or inc:
        return "filter", [e for e in evs if in_filter(e, req, inc)], info
    national = [e for e in evs if is_national(e, req.youth, req.women)]
    if req.scope == "auto" and req.multi_day:
        # week windows: per WAT day, national teams when >= 4 senior national-team matches are on, else top leagues
        by_day: dict = {}
        for e in national:
            by_day.setdefault(e.start.astimezone(TZ).date(), []).append(e)
        nat_days = {d for d, xs in by_day.items() if len(xs) >= AUTO_NATIONAL_MIN}
        out, used = [], set()
        for e in evs:
            d = e.start.astimezone(TZ).date()
            if d in nat_days:
                if is_national(e, req.youth, req.women):
                    out.append(e)
                    used.add("national")
            elif leagues.is_top(e):
                out.append(e)
                used.add("top")
        info["national_days"] = sorted(x.isoformat() for x in nat_days)
        sc = "mixed" if len(used) == 2 else (used.pop() if used else ("national" if nat_days else "top"))
        return sc, out, info
    if req.scope == "all":
        return "all", [e for e in evs if req.women or not _WOMEN.search(f"{e.league} {e.home} {e.away}")], info
    if req.scope == "national" or (req.scope == "auto" and len(national) >= AUTO_NATIONAL_MIN):
        return "national", national, info
    return "top", [e for e in evs if leagues.is_top(e)], info


SCOPE_TEXT = {"national": "national teams", "top": "top leagues", "all": "all leagues",
              "mixed": "national teams on international days, top leagues on the others"}


def scope_label(scope: str, info: dict) -> str:
    if scope != "filter":
        return SCOPE_TEXT.get(scope, scope)
    bits = list(info.get("competitions") or []) + [x.title() for x in info.get("leagues") or []] \
        + list(info.get("teams") or [])
    return "only " + ", ".join(bits) if bits else "your filter"


# ------------------------------------------------------------------ pricing
def devig_power(odds: list[float]) -> list[float] | None:
    from draw5 import devig_power as dp
    return dp(odds)


def p_any_team_2plus(lh: float, la: float) -> float:
    """P(home >= 2 or away >= 2) with independent Poisson goals."""
    ph = math.exp(-lh) * (1 + lh)
    pa = math.exp(-la) * (1 + la)
    return 1 - ph * pa


def _outs(markets: list[dict], mid: str, spec: str = "") -> dict | None:
    for m in markets:
        if m.get("id") == mid and (m.get("specifier") or "") == spec:
            return {o["id"]: o for o in m.get("outcomes") or []}
    return None


def model_check(markets: list[dict]) -> float | None:
    """Poisson fit of the event's 1X2 + Over/Under 2.5 (de-margined) -> P(either team scores 2+). None without them."""
    from draw5 import devig
    from scanner import fit_lambdas
    o1 = _outs(markets, "1")
    if not o1:
        return None
    p = devig([(o1.get(k) or {}).get("odds") for k in ("1", "2", "3")])
    if not p:
        return None
    t = {"h": p[0], "d": p[1], "a": p[2]}
    ou = _outs(markets, "18", "total=2.5")
    if ou:
        q = devig([(ou.get("12") or {}).get("odds"), (ou.get("13") or {}).get("odds")])
        if q:
            t["o"] = q[0]
    lam = fit_lambdas(t)
    return p_any_team_2plus(*lam) if lam else None


def leg_from_detail(e, markets: list[dict], lo: float, hi: float) -> tuple[dict | None, str]:
    """The Yes leg of 60045 total=1.5 for one event detail, or (None, reason). reason: none | closed | range."""
    outs = _outs(markets, MARKET_ID, SPEC)
    if not outs or YES not in outs:
        return None, "none"
    y, n = outs[YES], outs.get(NO) or {}
    if not y.get("active") or not y.get("odds"):
        return None, "closed"
    oy = float(y["odds"])
    if not (lo - 1e-9 <= oy <= hi + 1e-9):
        return None, "range"
    fair = devig_power([oy, float(n["odds"])]) if n.get("odds") and n.get("active", True) else None
    est = fair[0] if fair else (1 / oy) / 1.05        # Yes alone: assume a 5% margin
    pm = model_check(markets)
    from scanner import uid_of
    return {"event_id": e.event_id, "match": f"{e.home} vs {e.away}", "home": e.home, "away": e.away,
            "home_uid": uid_of(e.home_id), "away_uid": uid_of(e.away_id),
            "league": e.league_full, "category": e.category, "category_id": e.category_id,
            "tournament_id": e.tournament_id, "kickoff": e.start.isoformat(), "kickoff_local": fmt_local(e.start),
            "market": MARKET_NAME, "market_id": MARKET_ID, "specifier": SPEC, "outcome_id": YES, "pick": PICK,
            "odds": oy, "odds_no": float(n["odds"]) if n.get("odds") else None, "est": round(est, 4),
            "p_feed": y.get("prob"), "p_model": round(pm, 4) if pm is not None else None,
            "national": is_national(e, True, True), "verdict": "market"}, ""


def rank(legs: list[dict]) -> list[dict]:
    """Highest estimated chance first; ties: the Poisson check, then the earlier kickoff."""
    return sorted(legs, key=lambda l: (-l["est"], -(l.get("p_model") or 0), l["kickoff"]))


def choose(pool: list[dict], n: int, used=None) -> list[dict]:
    """Top n of the ranked pool, one leg per match, skipping event ids in `used`."""
    used = used or {}
    out, seen = [], set()
    for l in pool:
        if l["event_id"] in seen or l["event_id"] in used:
            continue
        seen.add(l["event_id"])
        out.append(l)
        if len(out) >= n:
            break
    return out


def _product(legs: list[dict], key: str) -> float:
    x = 1.0
    for l in legs:
        x *= float(l.get(key) or 1.0)
    return x


# ------------------------------------------------------------------ finder
def list_p(e) -> float | None:
    """P(either team scores 2+) from the list's 1X2 + O/U 2.5 (Poisson), or None without those prices."""
    from draw5 import devig
    from scanner import fit_lambdas
    p = devig([e.odds("1", k) for k in ("1", "2", "3")])
    if not p:
        return None
    t = {"h": p[0], "d": p[1], "a": p[2]}
    q = devig([e.odds("18", "12", "total=2.5"), e.odds("18", "13", "total=2.5")])
    if q:
        t["o"] = q[0]
    lam = fit_lambdas(t)
    return p_any_team_2plus(*lam) if lam else None


def list_prefilter(e, hi: float) -> bool:
    """Cheap pre-check from the list's 1X2 + O/U 2.5 (no extra request): keep the match when a Poisson fit says an
    Any Team Over 1.5 Yes at odds <= hi is plausible (loose margin: 0.15 below the price's implied chance). Matches
    without those prices are kept."""
    from draw5 import devig
    from scanner import fit_lambdas
    p = devig([e.odds("1", k) for k in ("1", "2", "3")])
    if not p:
        return True
    t = {"h": p[0], "d": p[1], "a": p[2]}
    q = devig([e.odds("18", "12", "total=2.5"), e.odds("18", "13", "total=2.5")])
    if q:
        t["o"] = q[0]
    lam = fit_lambdas(t)
    return lam is None or p_any_team_2plus(*lam) >= 1 / hi - 0.15


class AnyTeamFinder:
    """lister = the adapter whose upcoming list is read (SportyBet through its relay by default: same Sportradar
    match ids, already cached by the rest of the bot, and it keeps football.com's request count low; football.com's
    own list is the fallback). Only the event details (60045 lives there) and the booking go to football.com."""

    def __init__(self, bookmaker, lister=None):
        self.bm = bookmaker            # FootballComNG
        self.lister = lister
        self.last_source = ""
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._details: dict[str, tuple[float, dict]] = {}

    async def find(self, req: Request, fresh: bool = False, now: datetime | None = None) -> dict:
        """Every qualifying Yes leg in scope, ranked: {scope, scope_label, window, pool, offered, checked, ...}."""
        now = (now or _now()).astimezone(timezone.utc)
        win = window(req, now)
        key = repr((win["key"], req.hours, req.lo, req.hi, req.scope, req.youth, req.women, req.team_text.strip(),
                    tuple(c[0] for c in req.competitions or [])))
        hit = self._cache.get(key)
        if hit and not fresh and time.time() - hit[0] < RESULT_TTL:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and not fresh and time.time() - hit[0] < RESULT_TTL:
                return hit[1]
            res = await self._find(req, win, now)
            self._cache[key] = (time.time(), res)
            if len(self._cache) > 20:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:10]:
                    self._cache.pop(k, None)
            return res

    async def _find(self, req: Request, win: dict, now: datetime) -> dict:
        t0 = time.time()
        hours = min(340, max(24, math.ceil((win["end"] - now).total_seconds() / 3600) + 2))
        hours = next((h for h in (24, 48, 72, 168, 200, 340) if h >= hours), 340)
        events = None
        if self.lister is not None:
            try:
                events = await self.lister.list_events(hours)
                self.last_source = getattr(self.lister, "BRAND", "list")
            except Exception as exc:
                log.warning("anyteam list via %s failed (%s), using football.com's list",
                            getattr(self.lister, "BRAND", "lister"), exc.__class__.__name__)
        if events is None:
            events = await self.bm.list_events(hours)
            self.last_source = "football.com"
        scope, cands, info = scope_events(events, req, now, win)
        n_scope = len(cands)
        cands = [e for e in cands if list_prefilter(e, req.hi)]
        n_pre = len(cands)
        if req.multi_day and len(cands) > MAX_DETAILS_LONG:
            # bounded reads over a week: the likeliest by the list's Poisson check (no price -> neutral 1/hi)
            cands = sorted(cands, key=lambda e: -(list_p(e) if list_p(e) is not None else 1 / req.hi))
            cands = cands[:MAX_DETAILS_LONG]
        cands = sorted(cands, key=lambda e: e.start)[:MAX_DETAILS_LONG if req.multi_day else MAX_DETAILS]
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        cache_hits = 0

        async def one(e):
            nonlocal cache_hits
            hit = self._details.get(e.event_id)
            if hit and time.time() - hit[0] < DETAIL_TTL:
                cache_hits += 1
                return e, hit[1]
            async with sem:
                raw = await self.bm._event_raw(e.event_id)
            if raw is not None:
                self._details[e.event_id] = (time.time(), raw)
            return e, raw

        rows = await asyncio.gather(*(one(e) for e in cands))
        pool, counts = [], {"offered": 0, "closed": 0, "out_of_range": 0, "no_answer": 0, "not_open": 0}
        for e, raw in rows:
            if raw is None:
                counts["no_answer"] += 1
                continue
            if not raw or raw.get("status", 0) != 0:
                counts["not_open"] += 1
                continue
            markets = self.bm.parse_markets(raw)
            leg, why = leg_from_detail(e, markets, req.lo, req.hi)
            if why != "none":
                counts["offered"] += 1
            if why == "closed":
                counts["closed"] += 1
            elif why == "range":
                counts["out_of_range"] += 1
            if leg:
                pool.append(leg)
        pool = rank(pool)
        for i, l in enumerate(pool, 1):
            l["rank"] = i
        if len(self._details) > 1500:
            cut = time.time() - DETAIL_TTL
            self._details = {k: v for k, v in self._details.items() if v[0] >= cut}
        kos = sorted(l["kickoff"] for l in pool)
        return {"scope": scope, "scope_label": scope_label(scope, info), "filter": info,
                "window_label": win["label"], "window_key": f"{scope}|{win['key']}|{scope_label(scope, info)}",
                "window_start": win["start"].isoformat(), "window_end": win["end"].isoformat(),
                "window": req.win or f"{req.hours}h", "window_hours": req.hours,
                "pool_first_kickoff": kos[0] if kos else None, "pool_last_kickoff": kos[-1] if kos else None,
                "national_days": info.get("national_days"), "prefiltered": n_pre, "detail_cache_hits": cache_hits,
                "lo": req.lo, "hi": req.hi, "matches_in_scope": n_scope, "checked": len(rows),
                "list_source": self.last_source,
                "qualifying": len(pool), **counts, "pool": pool, "bookmaker": BOOKMAKER_LABEL,
                "market": {"id": MARKET_ID, "specifier": SPEC, "outcome": YES, "name": MARKET_NAME},
                "generated_at": _now().isoformat(timespec="seconds"), "took_s": round(time.time() - t0, 1),
                "method": ("est = football.com's Yes/No price on 'Any Team Over 1.5' with the margin removed (power "
                           "method); model = Poisson fit of the 1X2 + Over/Under 2.5 prices, P(either team scores "
                           "2+), shown as a check. Combined chance assumes independent legs. Estimates, not "
                           "guarantees.")}


# ------------------------------------------------------------------ booking flow
def _used_store(services):
    st = getattr(services, "anyteam_used", None)
    if st is None:
        from draw5book import UsedStore
        history = getattr(getattr(services, "tracker", None), "history", None)
        st = UsedStore(history, prefix=USED_PREFIX, kind=USED_KIND)
        try:
            services.anyteam_used = st
        except Exception:
            pass
    return st


def rules_text(req: Request, n: int, scope_lbl: str, win_lbl: str = "") -> str:
    return f"{LABEL} (Yes), {n} legs, odds {req.lo:.2f}-{req.hi:.2f}, {scope_lbl}" + (f", {win_lbl}" if win_lbl else "")


async def build(services, owner, text: str = "", refresh: bool = False, book: bool = True, test: bool = False,
                fresh: bool = False, n: int | None = None, lo: float | None = None, hi: float | None = None,
                win: str | None = None) -> dict:
    """Returns {status, rules, message, code?, url?, legs, n, requested, total_odds, est_prob, first_kickoff_local,
    history_id?, scope, ...}. status: booked | listed | none | exhausted | error."""
    req = parse_request(text, n, lo, hi, win)
    refresh = refresh or req.refresh
    finder = getattr(services, "anyteam", None)
    if finder is None:
        finder = AnyTeamFinder(services.footballcom, lister=getattr(services, "bookmaker", None))
    base = {"status": "error", "legs": [], "n": 0, "requested": req.n, "code": None, "url": None, "total_odds": 0,
            "est_prob": 0, "history_id": None, "refresh": refresh, "bookmaker": BOOKMAKER_LABEL, "text": text,
            "lo": req.lo, "hi": req.hi, "window": req.win or f"{req.hours}h"}
    try:
        res = await finder.find(req, fresh=fresh)
    except Exception as exc:
        log.warning("anyteam find failed: %s", exc, exc_info=True)
        return {**base, "rules": "", "message": "football.com didn't answer the Any Team Over 1.5 search. "
                                                "Tap Try again in a minute."}
    scope_lbl = res["scope_label"]
    wl = res["window_label"]
    base.update(scope=res["scope"], scope_label=scope_lbl, window_label=res["window_label"],
                qualifying=res["qualifying"], offered=res["offered"], matches_in_scope=res["matches_in_scope"],
                checked=res["checked"], window_start=res.get("window_start"), window_end=res.get("window_end"),
                pool_first_kickoff=res.get("pool_first_kickoff"), pool_last_kickoff=res.get("pool_last_kickoff"))
    store = _used_store(services)
    wkey = res["window_key"]
    used = {} if test else await store.load(owner, "anyteam", wkey)
    base["used_before"] = len(used)
    if not res["pool"] and res["checked"] and res["no_answer"] >= res["checked"]:
        return {**base, "rules": "", "message": "football.com didn't answer the match look-ups (it may be blocking "
                                                "this server right now). Tap Try again in a few minutes."}
    if not res["pool"]:
        what = scope_lbl if res["scope"] == "filter" else SCOPE_TEXT.get(res["scope"], "")
        msg = (f"No {what} match in the {res['window_label']} has football.com's Any Team Over 1.5 Yes at odds "
               f"{req.lo:.2f}-{req.hi:.2f} right now ({res['matches_in_scope']} matches in scope, "
               f"{res['offered']} of the {res['checked']} read offer the market).")
        return {**base, "status": "none", "rules": rules_text(req, 0, scope_lbl, wl), "message": msg}
    legs = choose(res["pool"], req.n, used if refresh else None)
    if not legs:
        return {**base, "status": "exhausted", "rules": rules_text(req, 0, scope_lbl, wl),
                "message": (f"No new matches left: all {res['qualifying']} qualifying {scope_lbl} matches in the "
                            f"{res['window_label']} were already in your earlier Any Team codes.")}
    if not book:
        out = _summary(base, req, legs, scope_lbl, res, refresh)
        out.update(status="listed", pool=res["pool"])
        return out
    code, err = None, ""
    bm = services.footballcom
    for attempt in range(2):
        try:
            code = await bm.book([Selection(l["event_id"], l["market_id"], l["specifier"], l["outcome_id"])
                                  for l in legs])
            break
        except BookingError as exc:
            err = str(exc)
            log.warning("anyteam booking failed (attempt %d): %s", attempt + 1, err)
            if attempt == 0:         # a price may have moved or closed: re-read fresh once
                try:
                    res = await finder.find(req, fresh=True)
                except Exception:
                    break
                legs = choose(res["pool"], req.n, used if refresh else None)
                if not legs:
                    break
    if not code:
        return {**base, "rules": rules_text(req, len(legs), scope_lbl, wl),
                "message": f"football.com didn't create the code: {err or 'the market closed while booking'}. "
                           "Tap Try again."}
    out = _summary(base, req, legs, scope_lbl, res, refresh)
    out.update(status="booked", code=code, url=footballcom_share_url(code))
    try:
        chk = await bm.load_code(code)
        if chk:
            out["loaded"], out["unavailable"] = chk["legs"], chk["unavailable"]
    except Exception:
        pass
    tracker = getattr(services, "tracker", None)
    if tracker is not None:
        rec = await tracker.record(legs, code, "anyteam", tier="custom", label=f"{LABEL} · football.com", total_odds=out["total_odds"],
                                   est_prob=out["est_prob"], test=test, bookmaker=BOOKMAKER_KEY)
        out["history_id"] = rec["id"] if rec else None
    if not test:
        await store.add(owner, "anyteam", wkey, legs, used)
    return out


def _summary(base: dict, req: Request, legs: list[dict], scope_lbl: str, res: dict, refresh: bool) -> dict:
    n = len(legs)
    first = min(datetime.fromisoformat(l["kickoff"]) for l in legs)
    last = max(datetime.fromisoformat(l["kickoff"]) for l in legs)
    short = ""
    if n < req.n:
        short = (f"Only {n} {scope_lbl if res['scope'] == 'filter' else SCOPE_TEXT.get(res['scope'], '')} "
                 f"match{'es' if n != 1 else ''} in the {res['window_label']} "
                 + ("that weren't in your earlier codes " if refresh else "")
                 + f"have Any Team Over 1.5 Yes at {req.lo:.2f}-{req.hi:.2f}, so this code has {n}, not {req.n}.")
    return {**base, "legs": legs, "n": n, "total_odds": round(_product(legs, "odds"), 2),
            "est_prob": round(_product(legs, "est"), 4), "first_kickoff": first.isoformat(),
            "first_kickoff_local": fmt_local(first), "last_kickoff": last.isoformat(),
            "last_kickoff_local": fmt_local(last), "window_label": res["window_label"],
            "window_start": res.get("window_start"), "window_end": res.get("window_end"),
            "rules": rules_text(req, n, scope_lbl, res["window_label"]), "short_note": short,
            "message": short, "label": LABEL}


# ------------------------------------------------------------------ output (no emojis anywhere)
def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def by_kickoff(legs: list[dict]) -> list[dict]:
    """Legs in kickoff order (the reply lists them by date; the booked selection set is the same)."""
    return sorted(legs, key=lambda l: (l.get("kickoff") or "", l.get("home") or ""))


def leg_line(i: int, l: dict) -> str:
    return f"{i}. {l['home']} v {l['away']} @{float(l['odds']):.2f}, {l['kickoff_local']} WAT"


def code_html(out: dict) -> str:
    """Telegram reply: Rules line, the football.com code, total odds, first kickoff, combined estimate, legs."""
    st = out.get("status")
    if st not in ("booked", "listed"):
        rows = [f"<b>Rules:</b> {_e(out['rules'])}"] if out.get("rules") else []
        rows.append(_e(out.get("message") or "No code this time."))
        return "\n".join(rows)
    rows = [f"<b>Rules:</b> {_e(out['rules'])}", ""]
    if st == "booked":
        rows.append(f"<b>football.com code: <code>{_e(out['code'])}</code></b>")
    else:
        rows.append("<b>Picks only (nothing booked)</b>")
    rows += [f"Total odds <b>{out['total_odds']:,.2f}</b>",
             f"First kickoff: {_e(out['first_kickoff_local'])} WAT",
             f"Last kickoff: {_e(out.get('last_kickoff_local') or out['first_kickoff_local'])} WAT",
             f"Combined estimated chance: {out['est_prob']:.1%} (estimates, not guarantees)", ""]
    rows += [_e(leg_line(i, l)) for i, l in enumerate(by_kickoff(out.get("legs") or []), 1)]
    if out.get("short_note"):
        rows += ["", f"Note: {_e(out['short_note'])}"]
    if st == "booked" and out.get("unavailable"):
        rows.append(f"Note: football.com shows {out['unavailable']} of the legs as unavailable now.")
    tail = "Saved to History. " if out.get("history_id") else ""
    rows.append(f"<i>{tail}18+.</i>")
    return "\n".join(rows)


def code_plain(out: dict) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", code_html(out)))
