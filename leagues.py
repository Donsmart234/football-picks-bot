"""Top-leagues allowlist, matched by Sportradar ids (never by names).

SportyBet's `sr:tournament:<id>` is Sportradar's *unique tournament* id and `sr:category:<id>` its real-category id
(checked 2026-11-01 against SportyBet's event list and the Sportradar feed `gismo/uniquetournament_info/<id>`, which
returned exactly the names below). Women's, youth, reserve and friendly competitions have their own ids (e.g. Serie A
Women 20808, FA Cup Women 27725, U19 National 26276, Int. Friendly Games 851), so they're excluded by construction;
a team-name guard also drops "U19 / Women / II / Reserves" sides that sometimes appear inside a senior cup.

Used by default by every scan (all tiers, /scan, Mini App Scanner, AI scans) and by rollover. A user can widen it by
naming leagues / countries / teams (constraints layer) or asking for all leagues. The analyzer rates any league but
flags legs outside this list. Extra ids can be added without a deploy-time code change: TOP_LEAGUES_EXTRA="384,357".
"""
from __future__ import annotations

import os
import re

# utid -> (category id, name, kind)
TOP_LEAGUES: dict[int, tuple[int, str, str]] = {
    # England
    17: (1, "Premier League", "league"), 18: (1, "Championship", "league"),
    19: (1, "FA Cup", "cup"), 21: (1, "EFL Cup", "cup"),
    # Spain
    8: (32, "LaLiga", "league"), 329: (32, "Copa del Rey", "cup"),
    # Italy
    23: (31, "Serie A", "league"), 328: (31, "Coppa Italia", "cup"),
    # Germany
    35: (30, "Bundesliga", "league"), 217: (30, "DFB Pokal", "cup"),
    # France
    34: (7, "Ligue 1", "league"), 335: (7, "Coupe de France", "cup"),
    # UEFA club competitions
    7: (393, "UEFA Champions League", "uefa"), 679: (393, "UEFA Europa League", "uefa"),
    34480: (393, "UEFA Conference League", "uefa"),
    # Netherlands
    37: (35, "Eredivisie", "league"), 330: (35, "KNVB beker", "cup"),
    # Portugal
    238: (44, "Liga Portugal (Primeira Liga)", "league"), 336: (44, "Taca de Portugal", "cup"),
    327: (44, "Taca da Liga", "cup"),
    # Scotland
    36: (22, "Premiership", "league"), 347: (22, "Scottish Cup", "cup"), 332: (22, "League Cup", "cup"),
    # Turkiye
    52: (46, "Super Lig", "league"), 96: (46, "Turkiye Kupasi", "cup"),
    # Belgium
    38: (33, "Pro League", "league"), 326: (33, "Beker van Belgie", "cup"),
    # Americas
    242: (26, "MLS", "league"), 325: (13, "Brasileiro Serie A", "league"), 373: (13, "Copa do Brasil", "cup"),
    155: (48, "Primera LPF (Liga Profesional)", "league"),
    # National teams (category 4 = International)
    16: (4, "FIFA World Cup", "national"), 11: (4, "World Cup Qualification, UEFA", "national"),
    295: (4, "World Cup Qualification, CONMEBOL", "national"), 13: (4, "World Cup Qualification, CAF", "national"),
    308: (4, "World Cup Qualification, AFC", "national"), 14: (4, "World Cup Qualification, CONCACAF", "national"),
    20434: (4, "World Cup Qualification, Inter-Confederation Playoffs", "national"),
    1: (4, "UEFA Euro", "national"), 27: (4, "UEFA Euro Qualification", "national"),
    23755: (4, "UEFA Nations League", "national"), 270: (4, "Africa Cup of Nations", "national"),
    1848: (4, "Africa Cup of Nations Qualification", "national"), 133: (4, "Copa America", "national"),
    140: (4, "CONCACAF Gold Cup", "national"), 246: (4, "AFC Asian Cup", "national"),
    28: (4, "AFC Asian Cup Qualification", "national"), 27420: (4, "CONCACAF Nations League", "national"),
    36497: (4, "Finalissima", "national"),
}


def _extra() -> set[int]:
    out = set()
    for x in re.split(r"[,\s]+", os.environ.get("TOP_LEAGUES_EXTRA", "")):
        if x.isdigit():
            out.add(int(x))
    return out


EXTRA = _extra()
_SIDE_GUARD = re.compile(r"(\bu-?(1[5-9]|2[0-3])\b|\bwomen\b|\bwomens\b|\bfemenin|\bfeminin|\bladies\b|\breserves?\b|"
                         r"\byouth\b|\bacademy\b|\bii\b|\bb team\b|\bsrl\b|\bsimulated\b)", re.I)


def _id(x) -> int | None:
    s = str(x or "").rsplit(":", 1)[-1]
    return int(s) if s.isdigit() else None


def is_top_ids(tournament_id, category_id=None, home: str = "", away: str = "") -> bool:
    """True when the unique tournament id is on the allowlist (and the category id matches when known)."""
    ut = _id(tournament_id)
    if ut is None:
        return False
    if ut in EXTRA:
        ok = True
    else:
        row = TOP_LEAGUES.get(ut)
        if not row:
            return False
        cat = _id(category_id)
        ok = cat is None or cat == row[0]
    if ok and (home or away) and _SIDE_GUARD.search(f"{home} {away}"):
        return False
    return ok


def is_top(x) -> bool:
    """Event, scanner Leg or leg dict."""
    get = (lambda k: x.get(k)) if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    tid = get("tournament_id") or get("utid")
    return is_top_ids(tid, get("category_id"), get("home") or "", get("away") or "")


def describe() -> str:
    groups: dict[str, list[str]] = {}
    for ut, (_, name, kind) in TOP_LEAGUES.items():
        groups.setdefault(kind, []).append(name)
    return "; ".join(f"{k}: {', '.join(v)}" for k, v in groups.items())
