"""Structured slip constraints: one object that the AI's tools, /scan text, the Mini App and rollover all speak.

The scanner enforces it at the candidate stage (legs that break a rule never compete) and `verify()` re-checks the
finished slip, so a slip handed to the owner or booked never violates a stated rule. If the rules can't be met at
the safety bar the slip stops short and says so instead of bending them.

Market types (a leg can carry several tags; exclude = drop a leg carrying ANY excluded tag, include = keep only legs
carrying at least one included tag):
  under, over          direction of any total/count line (goals, team, half, corners, cards, shots, Asian, combos)
  totals               goals Over/Under lines (match, halves, minutes; not team totals)
  team_totals          one team's goals Over/Under
  1x2, double_chance, dnb, handicap (European + Asian, incl. corner/booking handicaps), btts (GG/NG markets)
  corners, cards (bookings, cards, sending off), player_props, halves (1st/2nd half markets), combo, correct_score,
  exact_goals (exact, range, multigoals, goal bounds), odd_even, minutes, specials (shots, fouls, offsides, leads,
  "in a row", 1UP/2UP and other non-standard markets)

What counts as an UNDER selection: any pick labelled Under (Under 2.5, Liverpool Under 1.5, 1st Half Under, Corners
Under, Bookings Under, Asian Under, "Home & Under 2.5" combos), "Yes" on a market named "... Under ..." (Both Halves
Under 1.5, Home Team or Under 2.5), "No" on a yes/no market named "... Over ...", and upper-bound picks that are an
under in disguise: goal/corner/booking bounds or ranges that start at 0 ("0", "0-1", "0-3") and "No Goal".
GG/NG "No", clean sheets and win-to-nil are their own market types (btts / specials), not unders.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.LOCAL_TZ)

MARKET_TYPES = ["under", "over", "totals", "team_totals", "1x2", "double_chance", "dnb", "handicap", "btts",
                "corners", "cards", "player_props", "halves", "combo", "correct_score", "exact_goals", "odd_even",
                "minutes", "specials"]

# natural words -> market type
MARKET_ALIASES: dict[str, str] = {
    "under": "under", "unders": "under", "under goals": "under", "under selections": "under",
    "over": "over", "overs": "over", "over goals": "over",
    "totals": "totals", "total goals": "totals", "goals over/under": "totals", "o/u": "totals", "over/under": "totals",
    "team totals": "team_totals", "team total": "team_totals", "team goals": "team_totals",
    "1x2": "1x2", "match result": "1x2", "straight win": "1x2", "straight wins": "1x2", "home win": "1x2",
    "away win": "1x2", "wins": "1x2", "match winner": "1x2", "draws": "1x2",
    "double chance": "double_chance", "dc": "double_chance", "double chances": "double_chance",
    "dnb": "dnb", "draw no bet": "dnb",
    "handicap": "handicap", "handicaps": "handicap", "asian handicap": "handicap", "asian handicaps": "handicap",
    "ah": "handicap", "spreads": "handicap",
    "btts": "btts", "gg": "btts", "ng": "btts", "gg/ng": "btts", "both teams to score": "btts", "both teams score": "btts",
    "corner": "corners", "corners": "corners",
    "card": "cards", "cards": "cards", "booking": "cards", "bookings": "cards", "red cards": "cards",
    "yellow cards": "cards",
    "player props": "player_props", "player prop": "player_props", "props": "player_props", "players": "player_props",
    "player markets": "player_props", "goalscorer": "player_props", "goalscorers": "player_props",
    "scorers": "player_props",
    "halves": "halves", "half markets": "halves", "1st half": "halves", "first half": "halves",
    "2nd half": "halves", "second half": "halves", "half time": "halves", "halftime": "halves",
    "combo": "combo", "combos": "combo", "combination": "combo", "combinations": "combo",
    "correct score": "correct_score", "correct scores": "correct_score",
    "exact goals": "exact_goals", "multigoals": "exact_goals", "multi goals": "exact_goals",
    "goal range": "exact_goals", "goal bounds": "exact_goals",
    "odd/even": "odd_even", "odd even": "odd_even", "odd or even": "odd_even",
    "minutes": "minutes", "minute markets": "minutes", "early goals": "minutes",
    "specials": "specials", "special": "specials", "special markets": "specials",
}
MARKET_ALIASES.update({t: t for t in MARKET_TYPES})
MARKET_ALIASES.update({t.replace("_", " "): t for t in MARKET_TYPES})

TOP_LEAGUES_KEY = "top leagues"
LEAGUE_ALIASES = {
    "epl": "England · Premier League", "premier league": "England · Premier League",
    "english premier league": "England · Premier League", "championship": "England · Championship",
    "la liga": "Spain · LaLiga", "laliga": "Spain · LaLiga", "serie a": "Italy · Serie A",
    "bundesliga": "Germany · Bundesliga", "ligue 1": "France · Ligue 1",
    "champions league": "UEFA Champions League", "ucl": "UEFA Champions League",
    "europa league": "UEFA Europa League", "uel": "UEFA Europa League",
    "conference league": "UEFA Conference League", "eredivisie": "Netherlands · Eredivisie",
    "liga portugal": "Portugal · Liga Portugal", "primeira liga": "Portugal · Liga Portugal",
    "mls": "USA · MLS", "super lig": "Turkiye · Super Lig", "scottish premiership": "Scotland · Premiership",
    "npfl": "Nigeria · NPFL", "saudi pro league": "Saudi Arabia · Saudi Pro League",
}

DAYS = {"today", "tomorrow", "24h", "48h"}
TOTAL_LO, TOTAL_HI = 0.97, 1.25     # a slip "hits" a target when total odds land in [0.97x, 1.25x]


# ------------------------------------------------------------------ market classification
_RANGE0 = re.compile(r"^0(\s*-\s*\d+)?$")
_PLUS = re.compile(r"(^|\s)\d+\+$")
_STATS_WORDS = ("shot", "foul", "offside", "throw", "goal kick", "save", "tackle", "post", "crossbar",
                "substitution", "penalt")


def _strip_teams(s: str, home: str, away: str) -> str:
    out = s.lower()
    for t in (home, away):
        if t:
            out = out.replace(t.lower(), " ")
    return out


def market_tags(market_id: str, market: str, pick: str, group: str = "", specifier: str = "",
                home: str = "", away: str = "") -> tuple[str, ...]:
    """Market-type tags of one selection (see module docstring)."""
    mid = str(market_id or "")
    name = _strip_teams(market or "", home, away)
    name = re.sub(r"\s+", " ", name).strip()
    p = re.sub(r"\s+", " ", _strip_teams(pick or "", home, away)).strip()
    g = (group or "").lower()
    tags: set[str] = set()

    # direction
    if re.search(r"\bunder\b", p):
        tags.add("under")
    elif re.search(r"\bover\b", p):
        tags.add("over")
    elif p in ("yes", "no"):
        has_u, has_o = bool(re.search(r"\bunder\b", name)), bool(re.search(r"\bover\b", name))
        if has_u and not has_o:
            tags.add("under" if p == "yes" else "over")
        elif has_o and not has_u:
            tags.add("over" if p == "yes" else "under")
    elif p == "no goal":
        tags.add("under")
    elif _RANGE0.match(p) and any(x in name for x in ("bound", "range", "exact", "goals", "corner", "booking")) \
            and "excluded" not in name:
        tags.add("under")
    elif _PLUS.search(p) and not any(x in name for x in ("correct score", "margin", "half time/full time")):
        tags.add("over")

    is_corner = "corner" in name or g == "corners"
    is_card = g == "bookings" or any(x in name for x in ("booking", "card", "sending off", "sent off"))
    is_player = g == "players" or "playerprops" in (specifier or "") or name.startswith("player") or \
        "goalscorer" in name or name.startswith("most ") or "to score" in name and g == "players"
    is_combo = g == "combo" or " & " in name or re.search(r"\b(team|draw|away|home)? ?or (over|under|gg|any)\b", name)
    if is_corner:
        tags.add("corners")
    if is_card:
        tags.add("cards")
    if is_player:
        tags.add("player_props")
    if is_combo:
        tags.add("combo")
    if g == "minutes" or "minute" in name or "early goals" in name:
        tags.add("minutes")
    if g == "half" or re.search(r"(1st|2nd|first|second) half|half-?time|half time|both halves|either half|"
                                r"halftime|highest scoring half", name):
        tags.add("halves")
    if "handicap" in name or mid in ("14", "16", "65", "66", "87", "88"):
        tags.add("handicap")
    if "gg/ng" in name or "both teams to score" in name or "teams to score" in name or mid in ("29", "75", "95"):
        tags.add("btts")
    if "correct score" in name or "multiscore" in name:
        tags.add("correct_score")
    if any(x in name for x in ("exact goals", "goal range", "multigoal", "goal bounds", "excluded number",
                               "team goals")) or mid in ("21", "23", "24", "25", "71", "93"):
        tags.add("exact_goals")
    if "odd/even" in name or "odd or even" in name:
        tags.add("odd_even")
    stats_mkt = (g in ("match", "teams") and not is_card and not is_corner) or \
        any(w in name for w in _STATS_WORDS)
    if not (is_corner or is_card or is_player or is_combo or stats_mkt):
        if mid in ("1", "60", "83", "60100", "60200", "60210") or name in ("1x2", "1st half - 1x2", "2nd half - 1x2"):
            tags.add("1x2")
        if mid in ("10", "63", "85", "60110") or name.endswith("double chance"):
            tags.add("double_chance")
        if mid in ("11", "12", "13", "64", "86") or "no bet" in name:
            tags.add("dnb")
        if ("over/under" in name or "total goals" in name or re.search(r"\b(over|under) \d", name)) \
                and "odd" not in name:
            team_tot = mid in ("19", "20", "69", "70", "91", "92") or re.search(r"\b(home|away) team\b", name)
            tags.add("team_totals" if team_tot and mid not in ("18", "68", "90") else "totals")
    if mid in ("18", "68", "90"):
        tags.add("totals")
    if mid in ("19", "20", "69", "70", "91", "92"):
        tags.discard("totals")
        tags.add("team_totals")
    primary = tags - {"under", "over", "halves"}
    if not primary or stats_mkt and not (is_corner or is_card or is_player):
        tags.add("specials")
    return tuple(sorted(tags))


def leg_tags(l) -> tuple[str, ...]:
    """Tags of a Leg object or leg dict (uses the cached `tags` when present)."""
    get = (lambda k, d="": l.get(k, d)) if isinstance(l, dict) else (lambda k, d="": getattr(l, k, d))
    t = get("tags", None)
    if t:
        return tuple(t)
    return market_tags(get("market_id"), get("market"), get("pick"), get("group"), get("specifier"),
                       get("home"), get("away"))


def is_under(l) -> bool:
    return "under" in leg_tags(l)


# ------------------------------------------------------------------ constraints
def _norm_list(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = re.split(r"[,;/]| and ", v)
    return [str(x).strip()[:60] for x in v if str(x).strip()][:20]


def norm_market(x: str) -> str | None:
    s = str(x or "").strip().lower().replace("-", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    if s in MARKET_ALIASES:
        return MARKET_ALIASES[s]
    if s.endswith("s") and s[:-1] in MARKET_ALIASES:
        return MARKET_ALIASES[s[:-1]]
    return None


def _num(v, lo=None, hi=None, cast=float):
    if v in (None, ""):
        return None
    try:
        x = cast(float(str(v).replace(",", ".").rstrip("x×")))
    except (TypeError, ValueError):
        return None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if lo is not None and x < lo:
        return None
    if hi is not None and x > hi:
        return None
    return x


@dataclass
class Constraints:
    tier: str | None = None
    target_odds: float | None = None
    min_leg_prob: float | None = None
    min_legs: int | None = None
    max_legs: int | None = None
    min_leg_odds: float | None = None
    max_leg_odds: float | None = None
    include_markets: list[str] = field(default_factory=list)
    exclude_markets: list[str] = field(default_factory=list)
    include_leagues: list[str] = field(default_factory=list)
    exclude_leagues: list[str] = field(default_factory=list)
    include_countries: list[str] = field(default_factory=list)
    exclude_countries: list[str] = field(default_factory=list)
    include_teams: list[str] = field(default_factory=list)
    exclude_teams: list[str] = field(default_factory=list)
    day: str | None = None
    kickoff_after: str | None = None      # local "HH:MM" or "YYYY-MM-DD HH:MM" (Africa/Lagos)
    kickoff_before: str | None = None
    stats_confirmed_only: bool = False
    all_leagues: bool = False             # lift the default top-leagues allowlist (leagues.py)
    unknown: list[str] = field(default_factory=list)   # market words we couldn't map (reported back, not ignored)

    # ---- construction ---------------------------------------------------
    @classmethod
    def from_dict(cls, a: dict | None) -> "Constraints":
        a = dict(a or {})
        c = cls()
        tier = str(a.get("tier") or "").lower().strip()
        if tier:
            from scanner import TIER_ALIASES
            c.tier = TIER_ALIASES.get(tier) or ("safe" if tier in ("sure", "banker", "surest") else None)
        c.target_odds = _num(a.get("target_odds", a.get("target")), 1.01, 100000)
        p = _num(a.get("min_leg_prob"), 0, 100)
        if p is not None:
            c.min_leg_prob = round(p / 100 if p > 1 else p, 4)
        c.min_legs = _num(a.get("min_legs"), 1, 40, int)
        c.max_legs = _num(a.get("max_legs"), 1, 40, int)
        c.min_leg_odds = _num(a.get("min_leg_odds"), 1.0, 1000)
        c.max_leg_odds = _num(a.get("max_leg_odds"), 1.0, 1000)
        for k in ("include_markets", "exclude_markets"):
            out = []
            for x in _norm_list(a.get(k)):
                m = norm_market(x)
                if m and m not in out:
                    out.append(m)
                elif not m:
                    c.unknown.append(x)
            setattr(c, k, out)
        for k in ("include_leagues", "exclude_leagues", "include_countries", "exclude_countries",
                  "include_teams", "exclude_teams"):
            setattr(c, k, _norm_list(a.get(k)))
        d = str(a.get("day") or "").lower()
        c.day = d if d in DAYS else None
        c.kickoff_after = _clock(a.get("kickoff_after"))
        c.kickoff_before = _clock(a.get("kickoff_before"))
        c.stats_confirmed_only = bool(a.get("stats_confirmed_only"))
        c.all_leagues = bool(a.get("all_leagues"))
        return c

    def to_dict(self, compact: bool = True) -> dict:
        d = asdict(self)
        if compact:
            d = {k: v for k, v in d.items() if v not in (None, [], False, "")}
        return d

    def empty(self) -> bool:
        return not self.to_dict()

    def merge_hard(self, user: "Constraints") -> "Constraints":
        """Fold rules parsed deterministically from the user's own words into the model's arguments, so a rule
        the model forgot is still enforced. Exclusions are unioned; user-stated numbers win."""
        for k in ("exclude_markets", "exclude_leagues", "exclude_countries", "exclude_teams"):
            cur = getattr(self, k)
            for x in getattr(user, k):
                if x not in cur:
                    cur.append(x)
        for k in ("include_markets", "include_leagues", "include_countries", "include_teams"):
            if getattr(user, k) and not getattr(self, k):
                setattr(self, k, list(getattr(user, k)))
        for k in ("tier", "target_odds", "min_leg_prob", "min_legs", "max_legs", "min_leg_odds", "max_leg_odds",
                  "day", "kickoff_after", "kickoff_before"):
            uv = getattr(user, k)
            if uv is not None:
                setattr(self, k, uv)
        self.stats_confirmed_only = self.stats_confirmed_only or user.stats_confirmed_only
        self.all_leagues = self.all_leagues or user.all_leagues
        # an include that the user excluded makes no sense: the exclusion wins
        self.include_markets = [m for m in self.include_markets if m not in self.exclude_markets]
        return self

    # ---- leg checks -------------------------------------------------------
    def leg_problem(self, l, min_p: float | None = None) -> str:
        """'' when the leg satisfies every per-leg rule, else a short reason."""
        get = (lambda k, d=None: l.get(k, d)) if isinstance(l, dict) else (lambda k, d=None: getattr(l, k, d))
        tags = set(leg_tags(l))
        ex = tags & set(self.exclude_markets)
        if ex:
            return f"excluded market type ({', '.join(sorted(ex))})"
        if self.include_markets and not tags & set(self.include_markets):
            return "market type not in " + ", ".join(self.include_markets)
        est, odds = float(get("est") or 0), float(get("odds") or 0)
        bar = max(x for x in (min_p, self.min_leg_prob, 0.0) if x is not None)
        if est + 1e-9 < bar:
            return f"est {est:.0%} below {bar:.0%}"
        if self.min_leg_odds and odds + 1e-9 < self.min_leg_odds:
            return f"odds {odds:.2f} below {self.min_leg_odds:.2f}"
        if self.max_leg_odds and odds - 1e-9 > self.max_leg_odds:
            return f"odds {odds:.2f} above {self.max_leg_odds:.2f}"
        if self.stats_confirmed_only and get("verdict") != "confirmed":
            return "not stats-confirmed"
        if get("verdict") == "dropped":
            return "dropped by the stats check"
        league = str(get("league") or "").lower()
        country = str(get("category") or "").lower() or league.split(" · ")[0]
        if self.include_leagues and not any(_league_match(x, league, l) for x in self.include_leagues):
            return "league not included"
        if any(_league_match(x, league, l) for x in self.exclude_leagues):
            return "league excluded"
        if self.include_countries and not any(x.lower() in (country, league.split(" · ")[0]) or
                                              x.lower() in league for x in self.include_countries):
            return "country not included"
        if any(x.lower() == country or league.startswith(x.lower() + " ·") for x in self.exclude_countries):
            return "country excluded"
        home, away = str(get("home") or ""), str(get("away") or "")
        if self.include_teams and not any(_team_match(t, home) or _team_match(t, away) for t in self.include_teams):
            return "team not included"
        if any(_team_match(t, home) or _team_match(t, away) for t in self.exclude_teams):
            return "team excluded"
        ko = _parse_iso(get("kickoff"))
        if ko and (self.kickoff_after or self.kickoff_before):
            loc = ko.astimezone(TZ)
            if self.kickoff_after and not _after(loc, self.kickoff_after):
                return f"kicks off before {self.kickoff_after}"
            if self.kickoff_before and _after(loc, self.kickoff_before, strict=True):
                return f"kicks off after {self.kickoff_before}"
        return ""

    def match_rules(self) -> "Constraints":
        """Only the match-level rules (leagues, countries, teams, kick-off window)."""
        return Constraints(include_leagues=self.include_leagues, exclude_leagues=self.exclude_leagues,
                           include_countries=self.include_countries, exclude_countries=self.exclude_countries,
                           include_teams=self.include_teams, exclude_teams=self.exclude_teams,
                           kickoff_after=self.kickoff_after, kickoff_before=self.kickoff_before,
                           all_leagues=self.all_leagues)

    def widens_leagues(self) -> bool:
        """True when the user explicitly asked beyond the default top-leagues allowlist: all leagues, a named
        league / country / team (the scan then reads those matches, top league or not)."""
        named = [x for x in self.include_leagues if x.lower().strip() != TOP_LEAGUES_KEY]
        return bool(self.all_leagues or named or self.include_countries or self.include_teams)

    def match_key(self) -> str:
        d = self.match_rules().to_dict()
        return "" if not d else repr(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in d.items()))

    def event_ok(self, e) -> bool:
        """Match-level rules against a bookmaker Event (used to pick which matches the scan reads)."""
        if not self.match_key():
            return True
        probe = {"league": e.league_full, "category": e.category, "home": e.home, "away": e.away,
                 "tournament_id": e.tournament_id, "category_id": getattr(e, "category_id", ""),
                 "kickoff": e.start.isoformat(), "est": 1.0, "odds": 2.0, "tags": ("1x2",)}
        return not self.match_rules().leg_problem(probe)

    def leg_ok(self, l, min_p: float | None = None) -> bool:
        return not self.leg_problem(l, min_p)

    def describe(self) -> str:
        parts = []
        if self.tier:
            parts.append({"safe": "Safe (every leg ≥80%)", "mid": "Mid-safe", "risk": "Risk",
                          "high": "High risk"}.get(self.tier, self.tier))
        if self.target_odds:
            parts.append(f"~{self.target_odds:g} odds")
        if self.exclude_markets:
            parts.append("no " + ", ".join(_label(m) for m in self.exclude_markets))
        if self.include_markets:
            parts.append("only " + ", ".join(_label(m) for m in self.include_markets))
        if self.min_leg_prob:
            parts.append(f"legs ≥{self.min_leg_prob:.0%}")
        if self.min_legs or self.max_legs:
            if self.min_legs and self.max_legs:
                parts.append(f"{self.min_legs}–{self.max_legs} legs" if self.min_legs != self.max_legs
                             else f"{self.min_legs} legs")
            else:
                parts.append(f"≥{self.min_legs} legs" if self.min_legs else f"≤{self.max_legs} legs")
        if self.min_leg_odds:
            parts.append(f"leg odds ≥{self.min_leg_odds:g}")
        if self.max_leg_odds:
            parts.append(f"leg odds ≤{self.max_leg_odds:g}")
        for k, lab in (("include_leagues", "leagues"), ("include_countries", "countries"),
                       ("include_teams", "teams")):
            if getattr(self, k):
                parts.append(f"{lab}: " + ", ".join(getattr(self, k)))
        for k, lab in (("exclude_leagues", "not"), ("exclude_countries", "not"), ("exclude_teams", "not")):
            if getattr(self, k):
                parts.append(f"{lab} " + ", ".join(getattr(self, k)))
        if self.kickoff_after:
            parts.append(f"kick-off ≥{self.kickoff_after}")
        if self.kickoff_before:
            parts.append(f"kick-off ≤{self.kickoff_before}")
        if self.stats_confirmed_only:
            parts.append("stats-confirmed only")
        if self.all_leagues:
            parts.append("all leagues")
        return " · ".join(parts)


def _label(m: str) -> str:
    return {"under": "unders", "over": "overs", "1x2": "1X2", "btts": "GG/NG", "dnb": "draw no bet"}.get(
        m, m.replace("_", " "))


def _league_match(q: str, league: str, leg=None) -> bool:
    q = q.lower().strip()
    if q == TOP_LEAGUES_KEY:
        import leagues
        return leg is not None and leagues.is_top(leg)
    q = LEAGUE_ALIASES.get(q, q).lower()
    if " · " in q:
        cat, _, lg = q.partition(" · ")
        lcat, _, llg = league.partition(" · ")
        return lcat == cat and (llg == lg or llg.startswith(lg) and not re.match(r"\s*\d", llg[len(lg):]))
    return q in league


def _team_match(q: str, team: str) -> bool:
    if not q or not team:
        return False
    if q.lower() in team.lower():
        return True
    try:
        from matching import team_score
        return team_score(q, team) >= 85
    except Exception:
        return False


def _parse_iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


_CLOCK = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})[ t])?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)


def _clock(v) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip().lower().replace(".", ":")
    m = _CLOCK.match(s)
    if not m:
        return None
    date, h, mi, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{date + ' ' if date else ''}{h:02d}:{mi:02d}"


def _after(loc: datetime, spec: str, strict: bool = False) -> bool:
    """loc >= spec (or > when strict). spec 'HH:MM' compares the clock on loc's own day."""
    if " " in spec:
        ref = datetime.fromisoformat(spec).replace(tzinfo=TZ)
    else:
        h, m = (int(x) for x in spec.split(":"))
        ref = datetime.combine(loc.date(), dtime(h, m), TZ)
    return loc > ref if strict else loc >= ref


# ------------------------------------------------------------------ slip verification
def verify(slip: dict, c: Constraints, min_p: float | None = None) -> dict:
    """Re-check a finished slip against every rule. {ok, violations[], target_reached, warnings[]}."""
    legs = slip.get("legs") or []
    viol, warn = [], []
    for l in legs:
        why = c.leg_problem(l, min_p)
        if why:
            viol.append(f"{l.get('match')}: {l.get('pick')} ({why})")
    events = [l.get("event_id") for l in legs]
    if len(events) != len(set(events)):
        viol.append("two legs from the same match")
    n = len(legs)
    if c.max_legs and n > c.max_legs:
        viol.append(f"{n} legs > max {c.max_legs}")
    if c.min_legs and n < c.min_legs:
        viol.append(f"{n} legs < min {c.min_legs}")
    total = math.prod(float(l["odds"]) for l in legs) if legs else 0.0
    target = c.target_odds or slip.get("target")
    reached = bool(legs) and (not target or total >= target * TOTAL_LO)
    if target and legs and total > target * TOTAL_HI:
        warn.append(f"total odds {total:.2f} overshoot the {target:g} target")
    if target and not reached:
        warn.append(f"total odds {total:.2f} short of the {target:g} target")
    return {"ok": not viol, "violations": viol, "target_reached": reached, "warnings": warn,
            "total_odds": round(total, 2), "legs": n}


# ------------------------------------------------------------------ natural language / command text
_NEG = r"(?:no|without|w/o|excluding|exclude|except|avoid|minus|skip|zero|not|non|remove|drop|ban|never)"
_STOPWORDS = r"(?:with|only|from|in|for|at|legs?|odds|today|tomorrow|tonight|please|and give|give|code|slip|" \
             r"booking|between|max|min|each|per|after|before|that|which|where|but|then|so)"
_ALIAS_KEYS = sorted(MARKET_ALIASES, key=len, reverse=True)
_ALIAS_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in _ALIAS_KEYS) + r")(?![a-z0-9])")
_LEAGUE_KEYS = sorted(list(LEAGUE_ALIASES) + [TOP_LEAGUES_KEY, "top league", "big leagues", "major leagues"],
                      key=len, reverse=True)
_LEAGUE_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in _LEAGUE_KEYS) + r")(?![a-z0-9])")


def _league_key(k: str) -> str:
    return TOP_LEAGUES_KEY if k in ("top league", "big leagues", "major leagues") else k


def parse_text(text: str) -> Constraints:
    """Deterministic parse of explicit rules in a request ('give me sure 5 odds without under selections',
    '/scan safe 5 nounders -corners legs:4-8 after:18:00'). Only what is clearly stated is set."""
    c = Constraints()
    t = " " + (text or "").lower().replace("’", "'") + " "
    t = re.sub(r"\bunder[- ]free\b", "no unders", t)
    if re.search(r"(?<![a-z])(all|any|every|lower|other)[- ](leagues?|competitions?)(?![a-z])|(?<=\s)allleagues(?=\s)", t):
        c.all_leagues = True
    # command tokens: -under, nounders, no-unders, only:a,b, legs:4-8, minodds:1.2, maxodds:1.5, after:18:00 ...
    t = re.sub(r"(?<=\s)-(?=[a-z])", "no ", t)
    t = re.sub(r"(?<=\s)no-?(unders?|overs?|corners?|cards?|bookings?|props|combos?|handicaps?|btts|gg|halves|"
               r"specials|minutes)(?=\s)", r"no \1", t)
    for key, val in re.findall(r"(?<=\s)(only|exclude|league|leagues|country|team|notteam|notleague|legs|minodds|"
                               r"maxodds|after|before|minp|day):([^\s]+)", t):
        vals = [v for v in re.split(r"[,+]", val.replace("_", " ")) if v]
        if key == "only":
            for v in vals:
                m = norm_market(v)
                if m:
                    c.include_markets.append(m)
                elif v in LEAGUE_ALIASES or v == "top":
                    c.include_leagues.append(TOP_LEAGUES_KEY if v == "top" else v)
        elif key == "exclude":
            for v in vals:
                m = norm_market(v)
                if m:
                    c.exclude_markets.append(m)
        elif key in ("league", "leagues"):
            c.include_leagues += vals
        elif key == "notleague":
            c.exclude_leagues += vals
        elif key == "country":
            c.include_countries += vals
        elif key == "team":
            c.include_teams += vals
        elif key == "notteam":
            c.exclude_teams += vals
        elif key == "legs":
            m = re.match(r"(\d+)(?:-(\d+))?$", val)
            if m:
                c.min_legs = int(m.group(1)) if m.group(2) else None
                c.max_legs = int(m.group(2) or m.group(1))
                if not m.group(2):
                    c.min_legs = c.max_legs
        elif key == "minodds":
            c.min_leg_odds = _num(val, 1.0, 1000)
        elif key == "maxodds":
            c.max_leg_odds = _num(val, 1.0, 1000)
        elif key == "after":
            c.kickoff_after = _clock(val)
        elif key == "before":
            c.kickoff_before = _clock(val)
        elif key == "minp":
            p = _num(val.rstrip("%"), 0, 100)
            c.min_leg_prob = (p / 100 if p and p > 1 else p) if p else None
        elif key == "day" and val in DAYS:
            c.day = val
    t = re.sub(r"(?<=\s)(only|exclude|league|leagues|country|team|notteam|notleague|legs|minodds|maxodds|after|"
               r"before|minp|day):[^\s]+", " ", t)

    # tier
    if re.search(r"\bhigh[- ]?risk\b|\bvalue bets?\b|\blong ?shots?\b", t):
        c.tier = "high"
    elif re.search(r"\bmid[- ]?safe\b|\bmedium\b|\bmid\b", t):
        c.tier = "mid"
    elif re.search(r"\b(sure|surest|safe|safest|banker|bankers|low[- ]?risk|secure|certain)\b", t):
        c.tier = "safe"
    elif re.search(r"\brisky\b|\brisk\b", t):
        c.tier = "risk"
    elif re.search(r"\b(grand )?audit\b", t):
        c.tier = "audit"

    # target odds
    m = re.search(r"(?:total odds|odds of|target(?: odds)?|to)\s*(?:of\s*)?(\d+(?:\.\d+)?)\b(?!\s*(?:legs?|%|:))", t) \
        if re.search(r"total odds|odds of|target", t) else None
    m = m or re.search(r"\b(\d+(?:\.\d+)?)\s*(?:odds|odd|x|×)(?![a-z0-9])", t)
    if m:
        c.target_odds = _num(m.group(1), 1.01, 100000)

    # negated market words: "without unders and corners", "no under selections", "exclude cards, props"
    for nm in re.finditer(r"\b" + _NEG + r"\b\s+((?:(?:any|the|all|a|single|more|of)\s+)*)(.{0,60})", t):
        chunk = nm.group(2)
        stop = re.search(r"(?<![a-z])" + _STOPWORDS + r"(?![a-z])|[.!?;]", chunk)
        chunk = chunk[:stop.start()] if stop else chunk
        for am in _ALIAS_RE.finditer(chunk):
            mk = MARKET_ALIASES[am.group(1)]
            if mk not in c.exclude_markets:
                c.exclude_markets.append(mk)
        for lm in _LEAGUE_RE.finditer(chunk):
            k = _league_key(lm.group(1))
            if k not in c.exclude_leagues:
                c.exclude_leagues.append(k)
    # "only overs", "overs only", "just double chance"
    for om in re.finditer(r"\b(?:only|just|strictly)\s+(.{0,40})", t):
        chunk = om.group(1)
        stop = re.search(r"(?<![a-z])" + _STOPWORDS.replace("only|", "") + r"(?![a-z])|[.!?;]", chunk)
        chunk = chunk[:stop.start()] if stop else chunk
        for am in _ALIAS_RE.finditer(chunk):
            mk = MARKET_ALIASES[am.group(1)]
            if mk not in c.exclude_markets and mk not in c.include_markets:
                c.include_markets.append(mk)
        for lm in _LEAGUE_RE.finditer(chunk):
            k = _league_key(lm.group(1))
            if k not in c.include_leagues:
                c.include_leagues.append(k)
    for om in re.finditer(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in _ALIAS_KEYS) + r")\s+only\b", t):
        mk = MARKET_ALIASES[om.group(1)]
        if mk not in c.exclude_markets and mk not in c.include_markets:
            c.include_markets.append(mk)
    for lm in re.finditer(r"(?:from|in)\s+(?:the\s+)?(" + "|".join(re.escape(k) for k in _LEAGUE_KEYS) + r")"
                          r"(?![a-z0-9])|(?<![a-z0-9])(" + "|".join(re.escape(k) for k in _LEAGUE_KEYS) +
                          r")\s+(?:only|games|matches)\b", t):
        k = _league_key(lm.group(1) or lm.group(2))
        if k not in c.exclude_leagues and k not in c.include_leagues:
            c.include_leagues.append(k)

    # legs
    m = re.search(r"\b(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*(?:legs?|games|matches|selections)\b", t)
    if m:
        c.min_legs, c.max_legs = sorted((int(m.group(1)), int(m.group(2))))
    else:
        m = re.search(r"\b(?:max(?:imum)?|at most|not more than|no more than|up to|under)\s*(\d{1,2})\s*"
                      r"(?:legs?|games|matches|selections)\b", t)
        if m:
            c.max_legs = int(m.group(1))
        m2 = re.search(r"\b(?:min(?:imum)?|at least|no fewer than|not less than)\s*(\d{1,2})\s*"
                       r"(?:legs?|games|matches|selections)\b", t)
        if m2:
            c.min_legs = int(m2.group(1))
        if not m and not m2:
            m = re.search(r"\b(\d{1,2})\s*(?:legs?|games|matches|selections)\b", t)
            if m and int(m.group(1)) > 0:
                c.min_legs = c.max_legs = int(m.group(1))
    # leg odds
    m = re.search(r"(?:min(?:imum)?|at least)\s*(?:leg\s*)?odds\s*(?:of\s*)?(\d+(?:\.\d+)?)|(?:each|every)\s*"
                  r"(?:leg|game|selection)\s*(?:at least|above|over|>=?|min)\s*(\d+(?:\.\d+)?)(?:\s*odds)?", t)
    if m:
        c.min_leg_odds = _num(m.group(1) or m.group(2), 1.0, 1000)
    m = re.search(r"(?:max(?:imum)?|at most)\s*(?:leg\s*)?odds\s*(?:of\s*)?(\d+(?:\.\d+)?)|no\s*(?:leg|game|"
                  r"selection)\s*(?:above|over|higher than)\s*(\d+(?:\.\d+)?)", t)
    if m:
        c.max_leg_odds = _num(m.group(1) or m.group(2), 1.0, 1000)
    # leg probability
    m = re.search(r"(?:at least|min(?:imum)?|above|over|>=?)\s*(\d{2})\s*%", t)
    if m:
        c.min_leg_prob = int(m.group(1)) / 100
    # day / time
    if re.search(r"\btomorrow\b", t):
        c.day = "tomorrow"
    elif re.search(r"\b(today|tonight|this evening)\b", t):
        c.day = "today"
    elif re.search(r"\b(next|within)\s*48\s*h", t):
        c.day = "48h"
    elif re.search(r"\b(next|within)\s*24\s*h", t):
        c.day = "24h"
    m = re.search(r"\b(?:after|from|later than)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)(?![\d.])", t)
    if m and (":" in m.group(1) or "m" in m.group(1)):
        c.kickoff_after = _clock(m.group(1).replace(" ", ""))
    m = re.search(r"\b(?:before|earlier than|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)(?![\d.])", t)
    if m and (":" in m.group(1) or "m" in m.group(1)):
        c.kickoff_before = _clock(m.group(1).replace(" ", ""))
    if re.search(r"stats?[- ]confirmed only|only stats?[- ]confirmed|confirmed legs only", t):
        c.stats_confirmed_only = True
    c.include_markets = [x for x in c.include_markets if x not in c.exclude_markets]
    return c
