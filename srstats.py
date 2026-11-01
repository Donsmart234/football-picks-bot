"""Free team stats from the Sportradar statistics feed that SportyBet's own match widgets use
(stats.fn.sportradar.com/sportybet/...). No API key, no quota. SportyBet competitor ids (sr:competitor:<uid>)
are the feed's team uids, so no name matching is needed.

Per team: last 10 results (all competitions) with full-time and half-time scores and venue.
Per match: recent head-to-head meetings. Cached in SQLite for 6 hours."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from stats import MatchStats, TeamStats
from storage import Storage

log = logging.getLogger(__name__)
BASE = "https://stats.fn.sportradar.com/sportybet/en/Africa:Lagos/gismo"
TTL = 6 * 3600
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36"),
           "Referer": "https://www.sportybet.com/", "Origin": "https://www.sportybet.com",
           "Accept": "application/json"}


@dataclass
class Game:
    venue: str        # "H" | "A" (team's own venue)
    gf: int
    ga: int
    ht_gf: int | None
    ht_ga: int | None
    opp: str
    uts: int

    @property
    def res(self) -> str:
        return "W" if self.gf > self.ga else "D" if self.gf == self.ga else "L"


@dataclass
class TeamForm:
    uid: str
    name: str
    games: list[Game] = field(default_factory=list)   # newest first

    def _sub(self, venue: str | None = None, n: int | None = None) -> list[Game]:
        g = [x for x in self.games if venue is None or x.venue == venue]
        return g[:n] if n else g

    def record(self, venue: str | None = None, n: int | None = None) -> tuple[int, int, int]:
        g = self._sub(venue, n)
        return (sum(x.res == "W" for x in g), sum(x.res == "D" for x in g), sum(x.res == "L" for x in g))

    def form(self, n: int = 5) -> str:
        """Newest first, e.g. 'WWDLW'."""
        return "".join(x.res for x in self.games[:n])

    def avg(self, attr: str, venue: str | None = None) -> float | None:
        g = self._sub(venue)
        return sum(getattr(x, attr) for x in g) / len(g) if g else None

    def rate(self, cond, venue: str | None = None) -> tuple[int, int]:
        g = self._sub(venue)
        return sum(1 for x in g if cond(x)), len(g)

    def to_json(self) -> dict:
        w, d, l = self.record(n=5)
        hw, hd, hl = self.record("H")
        aw, ad, al = self.record("A")
        o25 = self.rate(lambda x: x.gf + x.ga >= 3)
        btts = self.rate(lambda x: x.gf > 0 and x.ga > 0)
        cs = self.rate(lambda x: x.ga == 0)
        fts = self.rate(lambda x: x.gf == 0)
        r = lambda v: round(v, 2) if v is not None else None
        return {"name": self.name, "form5": self.form(5), "last5": f"W{w} D{d} L{l}",
                "home_record": f"W{hw} D{hd} L{hl}", "away_record": f"W{aw} D{ad} L{al}",
                "gf_avg": r(self.avg("gf")), "ga_avg": r(self.avg("ga")),
                "gf_home": r(self.avg("gf", "H")), "ga_home": r(self.avg("ga", "H")),
                "gf_away": r(self.avg("gf", "A")), "ga_away": r(self.avg("ga", "A")),
                "over25": f"{o25[0]}/{o25[1]}", "btts": f"{btts[0]}/{btts[1]}",
                "clean_sheets": f"{cs[0]}/{cs[1]}", "failed_to_score": f"{fts[0]}/{fts[1]}",
                "games": len(self.games)}


@dataclass
class H2H:
    games: list[tuple[int, int, int]] = field(default_factory=list)   # (today's home goals, today's away goals, uts)

    def summary(self, n: int = 6) -> dict:
        g = self.games[:n]
        if not g:
            return {"n": 0}
        hw = sum(1 for a, b, _ in g if a > b)
        dr = sum(1 for a, b, _ in g if a == b)
        aw = len(g) - hw - dr
        return {"n": len(g), "home_w": hw, "draws": dr, "away_w": aw,
                "avg_goals": round(sum(a + b for a, b, _ in g) / len(g), 2),
                "btts": sum(1 for a, b, _ in g if a and b), "over25": sum(1 for a, b, _ in g if a + b >= 3),
                "last": [f"{a}-{b}" for a, b, _ in g[:5]]}


@dataclass
class MatchForm:
    home: TeamForm | None
    away: TeamForm | None
    h2h: H2H

    @property
    def ok(self) -> bool:
        return bool(self.home and self.away and len(self.home.games) >= 4 and len(self.away.games) >= 4)

    def to_match_stats(self) -> MatchStats | None:
        """Convert to the predictor's MatchStats (home/away splits from the last 10 games)."""
        if not self.ok:
            return None

        def ts(t: TeamForm) -> TeamStats:
            hg, ag = t._sub("H"), t._sub("A")
            return TeamStats(team_id=int(t.uid) if t.uid.isdigit() else 0, name=t.name,
                             played_home=len(hg), played_away=len(ag),
                             gf_home=t.avg("gf", "H"), ga_home=t.avg("ga", "H"),
                             gf_away=t.avg("gf", "A"), ga_away=t.avg("ga", "A"),
                             form=t.form(5)[::-1])   # predictor expects oldest -> newest
        return MatchStats(fixture_id=0, league_id=0, season=0, home=ts(self.home), away=ts(self.away),
                          h2h=[(a, b) for a, b, _ in self.h2h.games[:10]])

    def to_json(self) -> dict:
        return {"home": self.home.to_json() if self.home else None,
                "away": self.away.to_json() if self.away else None, "h2h": self.h2h.summary()}


def _uid(competitor_id: str) -> str:
    return (competitor_id or "").rsplit(":", 1)[-1]


class SportradarStats:
    source = "Sportradar (SportyBet stats feed)"

    def __init__(self, storage: Storage):
        self.storage = storage
        self._client = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(12.0))
        self._sem = asyncio.Semaphore(6)
        self.failures = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, ttl: int = TTL) -> dict | None:
        key = "sr:" + path
        hit = self.storage.cache_get(key)
        if hit is not None:
            return hit
        async with self._sem:
            try:
                r = await self._client.get(f"{BASE}/{path}")
                r.raise_for_status()
                doc = (r.json().get("doc") or [{}])[0]
            except (httpx.HTTPError, ValueError, IndexError, AttributeError) as exc:
                self.failures += 1
                log.warning("Sportradar %s failed: %s", path.split("/")[0], exc.__class__.__name__)
                return None
        if doc.get("event") == "exception":
            self.failures += 1
            return None
        data = doc.get("data") or {}
        self.storage.cache_set(key, data, ttl)
        return data

    async def team_form(self, competitor_id: str, name: str = "") -> TeamForm | None:
        uid = _uid(competitor_id)
        if not uid.isdigit():
            return None
        data = await self._get(f"stats_team_lastx/{uid}/10")
        if not data:
            return None
        tf = TeamForm(uid=uid, name=name or (data.get("team") or {}).get("name", ""))
        for m in data.get("matches") or []:
            if m.get("canceled") or m.get("postponed"):
                continue
            res, teams = m.get("result") or {}, m.get("teams") or {}
            if res.get("home") is None or res.get("away") is None:
                continue
            is_home = str((teams.get("home") or {}).get("uid")) == uid
            ft = ((m.get("periods") or {}).get("ft")) or res
            p1 = (m.get("periods") or {}).get("p1") or {}
            gh, ga = int(ft.get("home", res["home"])), int(ft.get("away", res["away"]))
            hh, ha = p1.get("home"), p1.get("away")
            opp = (teams.get("away" if is_home else "home") or {}).get("name", "")
            tf.games.append(Game("H" if is_home else "A", gh if is_home else ga, ga if is_home else gh,
                                 (hh if is_home else ha), (ha if is_home else hh), opp,
                                 int((m.get("time") or {}).get("uts") or 0)))
        tf.games.sort(key=lambda g: -g.uts)
        return tf

    async def h2h(self, home_id: str, away_id: str) -> H2H:
        hu, au = _uid(home_id), _uid(away_id)
        out = H2H()
        if not (hu.isdigit() and au.isdigit()):
            return out
        data = await self._get(f"stats_team_versusrecent/{hu}/{au}")
        for m in (data or {}).get("matches") or []:
            res, teams = m.get("result") or {}, m.get("teams") or {}
            if res.get("home") is None or res.get("away") is None or m.get("canceled"):
                continue
            ft = ((m.get("periods") or {}).get("ft")) or res
            gh, ga = int(ft.get("home", res["home"])), int(ft.get("away", res["away"]))
            if str((teams.get("home") or {}).get("uid")) == hu:
                out.games.append((gh, ga, int((m.get("time") or {}).get("uts") or 0)))
            else:
                out.games.append((ga, gh, int((m.get("time") or {}).get("uts") or 0)))
        out.games.sort(key=lambda g: -g[2])
        return out

    async def match_form(self, home_id: str, away_id: str, home: str = "", away: str = "") -> MatchForm:
        th, ta, hh = await asyncio.gather(self.team_form(home_id, home), self.team_form(away_id, away),
                                          self.h2h(home_id, away_id))
        return MatchForm(th, ta, hh)


    async def match_news(self, match_id: str, home_uid: str = "", away_uid: str = "") -> dict | None:
        """Team news for one match from the same feed: players listed missing / doubtful
        (match_missingplayers) and whether starting line-ups are published yet (match_squads). Context only:
        the probability model does not use it (the market price usually reflects known absences)."""
        mid = str(match_id or "").rsplit(":", 1)[-1]
        if not mid.isdigit():
            return None
        mp, sq = await asyncio.gather(self._get(f"match_missingplayers/{mid}", ttl=1800),
                                      self._get(f"match_squads/{mid}", ttl=900))
        if mp is None and sq is None:
            return None
        out = {"home_missing": [], "away_missing": [], "home_doubtful": [], "away_doubtful": [],
               "lineups": False}
        for p in (mp or {}).get("players") or []:
            try:
                team = str((p.get("uniqueteam") or {}).get("_id") or "")
                st = p.get("status") or {}
                pl = p.get("player") or {}
                name = str(pl.get("name") or "")
                if "," in name:                      # "Adorante, Andrea" -> "Adorante"
                    name = name.split(",", 1)[0].strip()
                pos = ((pl.get("position") or {}).get("abbr") or "")
            except AttributeError:
                continue
            side = "home" if team and team == str(home_uid) else "away" if team and team == str(away_uid) else ""
            if not side or not name:
                continue
            key = f"{side}_doubtful" if st.get("doubtful") and not st.get("missing") else f"{side}_missing"
            out[key].append({"name": name, "pos": pos, "why": str(st.get("name") or st.get("status") or "")})
        for side in ("home", "away"):
            lu = ((sq or {}).get(side) or {}).get("startinglineup")
            if isinstance(lu, list) and len(lu) >= 11:
                out["lineups"] = True
        return out
