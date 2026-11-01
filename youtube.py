"""Find an OFFICIAL, free, embeddable YouTube live stream of a match (no API key: the public results page with the
"Live" filter + oEmbed). Strict on purpose: most "LIVE" football videos on YouTube are score tickers, simulations
or re-streams, and even some verified channels post them. A video is only offered when ALL hold:

  1. YouTube marks it live now;
  2. the channel carries YouTube's verified badge;
  3. the channel is one of the match's own clubs, its competition, or a known official free broadcaster
     (normalised name match >= 90), never just any channel;
  4. the title names both teams (or the channel is one of the clubs and names the opponent);
  5. the title has none of the ticker / simulation / watch-along words;
  6. oEmbed answers 200 (embedding allowed).

Otherwise nothing is returned and the app shows the live tracker + "Where to watch" (SportyBet's official page).
Never embeds or links unlicensed streams."""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from urllib.parse import quote_plus

import httpx
from rapidfuzz import fuzz

log = logging.getLogger(__name__)
TTL = 600
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 "
      "Safari/537.36")
# official broadcasters / governing bodies that show some matches free on their own channels
OFFICIAL_BROADCASTERS = ["DAZN Football", "DAZN", "CBS Sports Golazo", "J.LEAGUE", "J.LEAGUE International",
                         "Concacaf", "FIFA", "CAF TV", "AFC", "SPFL", "Bundesliga", "LaLiga", "Serie A", "Ligue 1",
                         "MLS", "NWSL", "UEFA", "OneFootball", "Saudi Pro League", "K LEAGUE", "A-Leagues",
                         "Eredivisie", "Liga Portugal", "NPFL", "Women's Super League"]
BAD_WORDS = re.compile(r"score|ticker|simulat|efootball|e-football|fifa ?2\d|fc ?2\d|pes ?20|watch ?along|watchalong|"
                       r"reaction|prediction|preview|highlight|radio|commentary only|audio|play-?by-?play|stats|"
                       r"tracker|gameplay|video game|console", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|sc|afc|ac|official|tv|channel|club|football|futbol|soccer)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _runs(x) -> str:
    return "".join(r.get("text", "") for r in (x or {}).get("runs", []))


def parse_results(html: str) -> list[dict]:
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                run = ((v.get("ownerText") or {}).get("runs") or [{}])[0]
                out.append({"id": v.get("videoId"), "title": _runs(v.get("title")), "channel": _runs(v.get("ownerText")),
                            "channel_id": ((run.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"),
                            "verified": any("VERIFIED" in ((b.get("metadataBadgeRenderer") or {}).get("style") or "")
                                            for b in v.get("ownerBadges") or []),
                            "live": any("LIVE" in ((b.get("metadataBadgeRenderer") or {}).get("style") or "")
                                        for b in v.get("badges") or [])})
                return
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return out


def _named(title: str, team: str) -> bool:
    t, n = norm(title), norm(team)
    return bool(n) and (n in t or fuzz.partial_ratio(n, t) >= 88)


def official_match(v: dict, home: str, away: str, league: str) -> str | None:
    """Return why the video counts as official, or None."""
    if not (v.get("id") and v.get("live") and v.get("verified")):
        return None
    title, ch = v.get("title", ""), norm(v.get("channel", ""))
    if not ch or BAD_WORDS.search(title):
        return None
    lg_name = league.split("·")[-1] if league else ""
    candidates = [("club", home), ("club", away), ("competition", lg_name)] + [("broadcaster", b) for b in OFFICIAL_BROADCASTERS]
    kind = None
    for k, name in candidates:
        n = norm(name)
        if n and len(n) >= 3 and (fuzz.ratio(ch, n) >= 90 or (k != "club" and ch == n)):
            kind = k
            break
    if not kind:
        return None
    if kind == "club":
        other = away if fuzz.ratio(ch, norm(home)) >= 90 else home
        if not _named(title, other):
            return None
    elif not (_named(title, home) and _named(title, away)):
        return None
    return f"{kind} channel ({v.get('channel')}, verified)"


class YouTubeFinder:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True,
                                         headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                                         cookies={"SOCS": "CAI", "CONSENT": "YES+1"})
        self._cache: dict[str, tuple[float, dict]] = {}
        self.last_error = ""

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embeddable(self, vid: str) -> bool:
        try:
            r = await self._client.get("https://www.youtube.com/oembed",
                                       params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def find(self, eid: str, home: str, away: str, league: str) -> dict:
        """{"video": {id, title, channel, why} | None, "checked": n, "searched": bool}. Cached 10 min per match."""
        hit = self._cache.get(eid)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
        res: dict = {"video": None, "checked": 0, "searched": False}
        try:
            q = quote_plus(f"{home} vs {away} live")
            r = await self._client.get(f"https://www.youtube.com/results?search_query={q}&sp=EgJAAQ%253D%253D")
            vids = parse_results(r.text) if r.status_code == 200 else []
            res["searched"] = r.status_code == 200
            res["checked"] = len(vids)
            for v in vids[:25]:
                why = official_match(v, home, away, league)
                if why and await self.embeddable(v["id"]):
                    res["video"] = {"id": v["id"], "title": v["title"], "channel": v["channel"],
                                    "channel_id": v.get("channel_id"), "why": why}
                    break
        except httpx.HTTPError as exc:
            self.last_error = exc.__class__.__name__
            log.warning("youtube search failed: %s", self.last_error)
        self._cache[eid] = (time.time(), res)
        if len(self._cache) > 200:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:50]:
                self._cache.pop(k, None)
        return res
