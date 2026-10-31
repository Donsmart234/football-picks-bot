"""Fuzzy team / match name matching."""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_STOP = {"fc", "cf", "sc", "afc", "ac", "fk", "sk", "cd", "ud", "sd", "if", "bk", "club", "de", "the",
         "calcio", "sv", "vfb", "vfl", "rc", "ca", "as", "us", "ss", "ssc", "aj"}
_ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "spurs": "tottenham", "wolves": "wolverhampton", "psg": "paris saint germain",
    "inter": "inter milan", "barca": "barcelona", "atleti": "atletico madrid", "bayern": "bayern munich",
    "nottm forest": "nottingham forest", "sheff utd": "sheffield united", "sheff wed": "sheffield wednesday",
}
_SPLIT = re.compile(r"\s+(?:vs\.?|v\.?|x|-|–|—|against|and)\s+", re.I)


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _ALIASES.get(s, s)
    words = [w for w in s.split() if w not in _STOP]
    return " ".join(words) or s


def team_score(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    return max(fuzz.token_set_ratio(na, nb), fuzz.partial_ratio(na, nb) * 0.95, fuzz.WRatio(na, nb) * 0.97)


def split_query(text: str) -> tuple[str, str | None]:
    parts = _SPLIT.split(text.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None


def rank_events(query: str, events: list, limit: int = 5) -> list[tuple[float, object]]:
    """Rank events (objects with .home/.away) against 'Team A vs Team B' or a single team name."""
    a, b = split_query(query)
    scored = []
    for e in events:
        if b is None:
            s = max(team_score(a, e.home), team_score(a, e.away))
        else:
            straight = (team_score(a, e.home) + team_score(b, e.away)) / 2
            swapped = (team_score(a, e.away) + team_score(b, e.home)) / 2 - 3  # small penalty for reversed order
            s = max(straight, swapped)
        scored.append((s, e))
    scored.sort(key=lambda x: (-x[0], x[1].start))
    return scored[:limit]
