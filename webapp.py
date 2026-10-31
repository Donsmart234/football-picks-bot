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

import os

import config
from bookmakers import MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SELECTIONS, BookingError, Selection
from markets import valid_token
from scanner import DAYS, TIER_ALIASES, apply_stats
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

    def _admin(self, request: web.Request) -> bool:
        tok = os.environ.get("ADMIN_TOKEN", "").strip()
        got = request.headers.get("X-Admin-Token", "")
        return bool(tok) and len(tok) >= 24 and hmac.compare_digest(tok, got)

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if request.path.startswith("/api/"):
            user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), self.bot_token,
                                      self.owner_id())
            if user is None and self._admin(request):   # server-side diagnostics only
                user = {"id": self.owner_id() or 0, "admin": True}
            if request.path.startswith("/api/admin/") and not (user or {}).get("admin"):
                user = None
            if user is None:
                return web.json_response({"error": "forbidden"}, status=403)
            request["tg_user"] = user
        return await handler(request)

    def register(self, app: web.Application) -> None:
        app.middlewares.append(self.auth)
        app.router.add_get("/app", self.index)
        app.router.add_get("/app/", self.index)
        app.router.add_get("/app/{name:[a-z0-9_.-]+\\.(?:js|css|svg|png|jpg)}", self.asset)
        app.router.add_get("/api/events", self.events)
        app.router.add_post("/api/predict", self.predict)
        app.router.add_post("/api/book", self.book)
        app.router.add_post("/api/scan", self.scan)
        app.router.add_post("/api/ai", self.ai)
        app.router.add_post("/api/ai/reset", self.ai_reset)
        app.router.add_get("/api/match_markets", self.match_markets)
        app.router.add_get("/api/admin/diag", self.diag)

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
        out = _prediction_json(pred)
        out["stats"] = pred.form.to_json() if getattr(pred, "form", None) else None
        return web.json_response(out)

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
            spec = str(r.get("specifier") or "")
            known = SELECTIONS.get((mid, oid))
            if known and not spec:
                spec = known[1]
            if not eid.startswith("sr:match:") or not mid or not oid or not all(
                    valid_token(x) for x in (eid, mid, oid, spec)):
                return web.json_response({"error": "bad selection"}, status=400)
            if eid in seen:
                return web.json_response({"error": "Only one pick per match."}, status=400)
            seen.add(eid)
            sels.append(Selection(eid, mid, spec, oid))
        try:
            code = await self.s.bookmaker.book(sels)
        except BookingError as exc:
            return web.json_response({"error": f"{exc} A match may have started or a market closed: "
                                               "remove it from the slip and try again."}, status=502)
        log.info("Mini app booking code created (%d selections)", len(sels))
        check = await self.s.bookmaker.load_code(code)
        return web.json_response({"code": code, "url": self.s.bookmaker.load_code_url,
                                  "bookmaker": self.s.bookmaker.name, "requested": len(sels),
                                  "loaded": check["legs"] if check else None})

    # ---- scanner / AI / diagnostics ------------------------------------------
    async def scan(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        tier = TIER_ALIASES.get(str(data.get("tier") or "safe").lower(), "safe")
        day = str(data.get("day") or "today")
        day = day if day in DAYS else "today"
        try:
            target = float(data["target"]) if data.get("target") not in (None, "") else None
        except (TypeError, ValueError):
            return web.json_response({"error": "target must be a number"}, status=400)
        if target is not None and not (1.1 <= target <= 100000):
            return web.json_response({"error": "target odds must be between 1.1 and 100000"}, status=400)
        leagues = [str(x)[:40] for x in (data.get("leagues") or [])][:10] or None
        try:
            res = await self.s.scanner.scan(tier, target, day, leagues)
        except Exception as exc:
            log.warning("scan failed: %s", exc, exc_info=True)
            return web.json_response({"error": "The market scan failed (SportyBet didn't answer). Try again shortly."},
                                     status=502)
        ai = getattr(self.s, "ai", None)
        if ai:
            if tier == "audit":
                for sl in res["slips"].values():
                    ai.remember_slip(sl)
            else:
                ai.remember_slip(res)
        return self._gz(web.json_response(res))

    async def ai(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        msg = str(data.get("message") or "").strip()[:1500]
        if not msg:
            return web.json_response({"error": "Ask me something."}, status=400)
        ai = getattr(self.s, "ai", None)
        if not ai or not ai.enabled:
            return web.json_response({"error": "The AI analyst isn't configured on the server yet."}, status=503)
        chat = int(request["tg_user"].get("id") or 0)
        try:
            res = await ai.ask(chat, msg)
        except Exception as exc:
            log.warning("ai failed: %s", exc)
            return web.json_response({"error": f"The AI analyst is unavailable right now ({str(exc)[:120]})."},
                                     status=502)
        return self._gz(web.json_response(res))

    async def ai_reset(self, request: web.Request) -> web.Response:
        ai = getattr(self.s, "ai", None)
        if ai:
            ai.reset(int(request["tg_user"].get("id") or 0))
        return web.json_response({"ok": True})

    async def match_markets(self, request: web.Request) -> web.Response:
        eid = request.query.get("event_id", "")
        if not eid.startswith("sr:match:") or not valid_token(eid):
            return web.json_response({"error": "event_id required"}, status=400)
        event = await self.s.find_event(eid)
        if not event:
            return web.json_response({"error": "That match is no longer listed."}, status=404)
        mk = await self.s.bookmaker.event_markets(eid)
        if mk is None:
            return web.json_response({"error": "SportyBet didn't answer just now."}, status=502)
        form = await self.s.match_form(event)
        legs = self.s.scanner.event_legs(event, mk)
        for l in legs:
            apply_stats(l, form)
        good = sorted([l for l in legs if l.odds >= 1.15 and l.verdict != "dropped"], key=lambda l: -l.conf)
        return self._gz(web.json_response({"event_id": eid, "legs": [l.to_json() for l in good[:15]],
                                           "stats": form.to_json() if form else None}))

    async def diag(self, request: web.Request) -> web.Response:
        out: dict = {"time": datetime.now(timezone.utc).isoformat()}
        t = time.time()
        try:
            evs = await self.s.bookmaker.list_events(24)
            out["relay_events_24h"] = len(evs)
        except Exception as exc:
            out["relay_error"] = exc.__class__.__name__
            evs = []
        out["relay_s"] = round(time.time() - t, 2)
        ev = next((e for e in evs if e.home_id and "srl" not in e.league_full.lower()), None)
        if ev:
            t = time.time()
            mk = await self.s.bookmaker.event_markets(ev.event_id)
            out["event_markets"] = None if mk is None else len(mk)
            form = await self.s.match_form(ev)
            out["sportradar_ok"] = bool(form and form.ok)
            out["sportradar_failures"] = self.s.sr.failures
            out["detail_s"] = round(time.time() - t, 2)
        ai = getattr(self.s, "ai", None)
        out["ai_enabled"] = bool(ai and ai.enabled)
        if ai and ai.enabled:
            try:
                ms = await ai.list_models()
                out["gemini_models"] = [m for m in ms if "flash" in m][:15]
            except Exception as exc:
                out["gemini_error"] = str(exc)[:160]
            out["gemini_model"] = ai.model
        out["api_football_left"] = self.s.stats.remaining_today() if self.s.stats.enabled else None
        return web.json_response(out)
