"""Glue between the bookmaker adapter, the stats client and the predictor (no Telegram code here)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from bookmakers import BookmakerAdapter, Event, SportyBetNG
from matching import rank_events
from predictor import Prediction, predict
from scanner import Scanner
from srstats import MatchForm, SportradarStats
from stats import ApiFootball, QuotaExceeded
from storage import Storage

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)

# Big leagues first when browsing; everything else alphabetical.
_PRIORITY = [
    ("England", "Premier League"), ("Spain", "LaLiga"), ("Italy", "Serie A"), ("Germany", "Bundesliga"),
    ("France", "Ligue 1"), ("International Clubs", "UEFA Champions League"),
    ("International Clubs", "UEFA Europa League"), ("England", "Championship"), ("Nigeria", "NPFL"),
    ("Netherlands", "Eredivisie"), ("Portugal", "Liga Portugal"),
]


class Services:
    def __init__(self, storage: Storage | None = None, bookmaker: BookmakerAdapter | None = None,
                 stats: ApiFootball | None = None):
        self.storage = storage or Storage()
        self.bookmaker = bookmaker or SportyBetNG()
        self.stats = stats or ApiFootball(self.storage)
        self.sr = SportradarStats(self.storage)
        self.scanner = Scanner(self.bookmaker, self.sr, self.stats)
        self._pred_cache: dict[str, tuple[float, Prediction]] = {}
        self._form_cache: dict[str, MatchForm | None] = {}

    async def aclose(self) -> None:
        await self.bookmaker.aclose()
        await self.stats.aclose()
        await self.sr.aclose()

    # ---- events -----------------------------------------------------------
    async def events(self, scope: str) -> list[Event]:
        """scope 't' = rest of today (local time), 'u' = next 3 days."""
        now = datetime.now(timezone.utc)
        if scope == "t":
            today = now.astimezone(TZ).date()
            return [e for e in await self.bookmaker.list_events(24)
                    if e.start > now and e.start.astimezone(TZ).date() == today]
        end = now + timedelta(days=3)
        return [e for e in await self.bookmaker.list_events(72) if now < e.start <= end]

    @staticmethod
    def leagues(events: list[Event]) -> list[tuple[str, str, int]]:
        """[(tournament_id, display name, match count)] in browse order."""
        groups: dict[str, list] = {}
        for e in events:
            g = groups.setdefault(e.tournament_id, [e.league_full, 0, e.category, e.league])
            g[1] += 1

        def order(item):
            tid, (name, n, cat, league) = item
            for i, (c, l) in enumerate(_PRIORITY):
                if cat == c and league == l:
                    return (0, i, "")
            return (1, 0, name.lower())

        return [(tid, v[0], v[1]) for tid, v in sorted(groups.items(), key=order)]

    async def find_event(self, event_id: str) -> Event | None:
        return await self.bookmaker.get_event(event_id)

    async def search(self, query: str, limit: int = 5) -> list[tuple[float, Event]]:
        events = await self.bookmaker.list_events(72)
        now = datetime.now(timezone.utc)
        return rank_events(query, [e for e in events if e.start > now], limit)

    # ---- predictions -------------------------------------------------------
    async def match_form(self, event: Event) -> MatchForm | None:
        """Free team stats (form, home/away, goals, H2H) from the Sportradar feed. Cached in SQLite."""
        try:
            return await self.scanner.match_form(event)
        except Exception as exc:
            log.warning("form lookup failed for %s: %s", event.name, exc)
            return None

    async def predict(self, event: Event) -> Prediction | None:
        hit = self._pred_cache.get(event.event_id)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        form = await self.match_form(event)
        ms = form.to_match_stats() if form else None
        # API-Football only as a fallback when the free feed has nothing, and only with budget to spare.
        if ms is None and self.stats.enabled and self.stats.remaining_today() >= 20:
            try:
                ms = await self.stats.match_stats(event.home, event.away, event.start)
            except QuotaExceeded:
                ms = None
            except Exception as exc:  # stats are optional: never let them break a prediction
                log.warning("stats lookup failed for %s: %s", event.name, exc)
        pred = predict(event, ms)
        if pred:
            pred.form = form
            self._pred_cache[event.event_id] = (time.time(), pred)
        return pred
