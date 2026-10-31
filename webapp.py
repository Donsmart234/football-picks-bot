"""Telegram Mini App: static UI at /app plus a same-origin JSON API at /api/*.

Every /api/* call must carry the header X-Telegram-Init-Data (Telegram.WebApp.initData). It is validated with
HMAC-SHA256 as documented by Telegram (secret = HMAC_SHA256(key="WebAppData", msg=bot_token)), must be younger
than 24h, and its user.id must be the bot owner. Anything else gets 403.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import config
from bookmakers import MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SELECTIONS, BookingError, Selection
from services import TZ, Services

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"
MAX_AGE = 24 * 3600
EVENTS_CACHE_SECONDS = 300
MAX_SELECTIONS = 30
DISCLAIMER = "Estimate, not guaranteed. 18+. Bet only what you can afford to lose."


# ------------------------------------------------------------------ initData
def validate_init_data(init_data: str, bot_token: str, owner_id: int | None,
                       max_age: int = MAX_AGE, now: float | None = None) -> dict | None:
    """Return the parsed user dict when initData is authentic, fresh and from the owner, else None."""
    if not init_data or not bot_token or not owner_id:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    if len(data) != len(pairs):  # duplicate keys: reject
        return None
    received = data.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
        user = json.loads(data.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    now = time.time() if now is None else now
    if auth_date <= 0 or now - auth_date > max_age or auth_date - now > 300:
        return None
    if not isinstance(user, dict) or int(user.get("id", 0) or 0) != int(owner_id):
        return None
    return user


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Build a signed initData string (used by tests only)."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


# ------------------------------------------------------------------ serializers
def _odds_1x2(e) -> dict:
    return {"1": e.odds(MARKET_1X2, "1"), "X": e.odds(MARKET_1X2, "2"), "2": e.odds(MARKET_1X2, "3")}


def _event_json(e) -> dict:
    local = e.start.astimezone(TZ)
    return {"id": e.event_id, "home": e.home, "away": e.away, "start": e.start.isoformat(),
            "day": local.strftime("%a %d %b"), "time": local.strftime("%H:%M"), "odds": _odds_1x2(e)}


def _prediction_json(p) -> dict:
    e = p.event
    groups = [("1X2", [(MARKET_1X2, "1"), (MARKET_1X2, "2"), (MARKET_1X2, "3")]),
              ("Over/Under 2.5", [(MARKET_OU, "12"), (MARKET_OU, "13")]),
              ("Both teams to score", [(MARKET_BTTS, "74"), (MARKET_BTTS, "76")]),
              ("Double chance", [(MARKET_DC, "9"), (MARKET_DC, "10"), (MARKET_DC, "11")])]
    markets = []
    for title, keys in groups:
        rows = []
        for mid, oid in keys:
            label, spec = SELECTIONS[(mid, oid)]
            rows.append({"market_id": mid, "outcome_id": oid, "specifier": spec, "label": label,
                         "prob": round(p.probs[(mid, oid)], 4), "odds": e.odds(mid, oid, spec)})
        markets.append({"title": title, "outcomes": rows})
    best = None
    if p.best:
        b = p.best
        best = {"market_id": b.market_id, "outcome_id": b.outcome_id, "specifier": b.specifier,
                "label": b.label, "prob": round(b.prob, 4), "odds": b.odds}
    return {"event": {**_event_json(e), "league": e.league_full}, "markets": markets, "best": best,
            "reasoning": p.reasoning, "source": p.source,
            "xg": [round(p.xg[0], 2), round(p.xg[1], 2)] if p.xg else None, "disclaimer": DISCLAIMER}


# ------------------------------------------------------------------ app
class MiniApp:
    def __init__(self, services: Services, bot_token: str | None = None, owner_id: int | None = None):
        self.s = services
        self.bot_token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
        self._owner_id = owner_id
        self._events_cache: dict[str, tuple[float, bytes]] = {}

    def owner_id(self) -> int | None:
        return self._owner_id or config.OWNER_TELEGRAM_ID or self.s.storage.get_owner()

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if request.path.startswith("/api/"):
            user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), self.bot_token,
                                      self.owner_id())
            if user is None:
                return web.json_response({"error": "forbidden"}, status=403)
            request["tg_user"] = user
        return await handler(request)

    def register(self, app: web.Application) -> None:
        app.middlewares.append(self.auth)
        app.router.add_get("/app", self.index)
        app.router.add_get("/app/", self.index)
        app.router.add_get("/app/{name:[a-z0-9_.-]+\\.(?:js|css|svg)}", self.asset)
        app.router.add_get("/api/events", self.events)
        app.router.add_post("/api/predict", self.predict)
        app.router.add_post("/api/book", self.book)

    async def index(self, _: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def asset(self, request: web.Request) -> web.StreamResponse:
        path = STATIC_DIR / request.match_info["name"]
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def events(self, request: web.Request) -> web.Response:
        rng = request.query.get("range", "today")
        if rng not in ("today", "upcoming"):
            return web.json_response({"error": "range must be today or upcoming"}, status=400)
        hit = self._events_cache.get(rng)
        if hit and time.time() - hit[0] < EVENTS_CACHE_SECONDS:
            return self._gz(web.Response(body=hit[1], content_type="application/json"))
        try:
            evs = await self.s.events("t" if rng == "today" else "u")
        except Exception as exc:
            log.warning("mini app events failed: %s", exc)
            return web.json_response({"error": "SportyBet didn't answer just now. Try again in a minute."},
                                     status=502)
        by_t: dict[str, list] = {}
        for e in evs:
            by_t.setdefault(e.tournament_id, []).append(e)
        leagues = [{"id": tid, "name": name, "count": n, "matches": [_event_json(e) for e in by_t[tid]]}
                   for tid, name, n in self.s.leagues(evs)]
        body = json.dumps({"range": rng, "generated": datetime.now(timezone.utc).isoformat(),
                           "count": len(evs), "leagues": leagues}, separators=(",", ":")).encode()
        self._events_cache[rng] = (time.time(), body)
        return self._gz(web.Response(body=body, content_type="application/json"))

    @staticmethod
    def _gz(resp: web.Response) -> web.Response:
        resp.enable_compression()  # gzip/deflate when the webview accepts it (~300 KB JSON -> ~40 KB)
        return resp

    async def _json(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='{"error":"invalid JSON"}', content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='{"error":"invalid JSON"}', content_type="application/json")
        return data

    async def predict(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        eid = str(data.get("event_id") or "").strip()
        if not eid:
            return web.json_response({"error": "event_id required"}, status=400)
        try:
            event = await self.s.find_event(eid)
        except Exception as exc:
            log.warning("mini app find_event failed: %s", exc)
            return web.json_response({"error": "SportyBet didn't answer just now. Try again in a minute."},
                                     status=502)
        if not event:
            return web.json_response({"error": "That match is no longer listed (it may have started)."},
                                     status=404)
        pred = await self.s.predict(event)
        if not pred:
            return web.json_response({"error": "No 1X2 prices for this match yet, so I can't estimate it."},
                                     status=422)
        return web.json_response(_prediction_json(pred))

    async def book(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        raw = data.get("selections")
        if not isinstance(raw, list) or not raw:
            return web.json_response({"error": "Your slip is empty."}, status=400)
        if len(raw) > MAX_SELECTIONS:
            return web.json_response({"error": f"At most {MAX_SELECTIONS} selections."}, status=400)
        sels, seen = [], set()
        for r in raw:
            if not isinstance(r, dict):
                return web.json_response({"error": "bad selection"}, status=400)
            eid, mid, oid = str(r.get("event_id", "")), str(r.get("market_id", "")), str(r.get("outcome_id", ""))
            known = SELECTIONS.get((mid, oid))
            if not eid or not known:
                return web.json_response({"error": "bad selection"}, status=400)
            if eid in seen:
                return web.json_response({"error": "Only one pick per match."}, status=400)
            seen.add(eid)
            sels.append(Selection(eid, mid, known[1], oid))
        try:
            code = await self.s.bookmaker.book(sels)
        except BookingError as exc:
            return web.json_response({"error": f"{exc} A match may have started or a market closed: "
                                               "remove it from the slip and try again."}, status=502)
        log.info("Mini app booking code created (%d selections)", len(sels))
        return web.json_response({"code": code, "url": self.s.bookmaker.load_code_url,
                                  "bookmaker": self.s.bookmaker.name})
