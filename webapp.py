"""Telegram Mini App: static UI at /app plus a same-origin JSON API at /api/*.

Every /api/* call must carry the header X-Telegram-Init-Data (Telegram.WebApp.initData). It is validated with
HMAC-SHA256 as documented by Telegram (secret = HMAC_SHA256(key="WebAppData", msg=bot_token)), must be younger
than 24h, and its user.id must be the bot owner. Anything else gets 403.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import os

import config
from bookmakers import MARKET_1X2, MARKET_BTTS, MARKET_DC, MARKET_OU, SELECTIONS, BookingError, Selection, code_link
from markets import valid_token
from constraints import Constraints, norm_market
import model
from live import Live
from youtube import YouTubeFinder
from scanner import DAYS, TIER_ALIASES, apply_stats, uid_of
from services import TZ, Services

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"
MAX_AGE = 24 * 3600
EVENTS_CACHE_SECONDS = 300          # fresh window; older bodies are still served instantly while a refresh runs
EVENTS_STALE_MAX = 1800             # never serve an events list older than this
WARM_EVERY = 600                    # background warm-up of today's list (07:00-24:00 WAT) so the first open is instant
ASSET_TYPES = {".js": "application/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
               ".jpg": "image/jpeg"}
MAX_SELECTIONS = 30
DISCLAIMER = "Estimate, not guaranteed. 18+. Bet only what you can afford to lose."


# ------------------------------------------------------------------ initData
def validate_init_data(init_data: str, bot_token: str, owner_id: int | None,
                       max_age: int = MAX_AGE, now: float | None = None) -> dict | None:
    """Return the parsed user dict when initData is authentic, fresh and from the owner, else None."""
    if not owner_id:
        return None
    user = validate_init_data_any(init_data, bot_token, max_age, now)
    if user is None or int(user.get("id", 0) or 0) != int(owner_id):
        return None
    return user


def validate_init_data_any(init_data: str, bot_token: str, max_age: int = MAX_AGE,
                           now: float | None = None) -> dict | None:
    """Authentic + fresh initData from any Telegram user (the caller decides what that user may do)."""
    if not init_data or not bot_token:
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
    if not isinstance(user, dict) or not int(user.get("id", 0) or 0):
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


def _event_row(e) -> list:
    """Compact match row for /api/events: [id, home, away, start_epoch, home_uid, away_uid, odds1, oddsX, odds2].
    The app formats day/time itself (about half the bytes of the old object form)."""
    o = _odds_1x2(e)
    return [e.event_id, e.home, e.away, int(e.start.timestamp()), uid_of(e.home_id), uid_of(e.away_id),
            o["1"], o["X"], o["2"]]


def _event_json(e) -> dict:
    local = e.start.astimezone(TZ)
    return {"id": e.event_id, "home": e.home, "away": e.away, "start": e.start.isoformat(),
            "home_uid": uid_of(e.home_id), "away_uid": uid_of(e.away_id),
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
    return {"event": {**_event_json(e), "league": e.league_full, "category": e.category}, "markets": markets, "best": best,
            "reasoning": p.reasoning, "source": p.source,
            "xg": [round(p.xg[0], 2), round(p.xg[1], 2)] if p.xg else None, "disclaimer": DISCLAIMER}


# ------------------------------------------------------------------ app
class MiniApp:
    def __init__(self, services: Services, bot_token: str | None = None, owner_id: int | None = None):
        self.s = services
        self.bot_token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
        self._owner_id = owner_id
        self._events_cache: dict[str, tuple[float, bytes, str]] = {}
        self._events_busy: dict[str, asyncio.Task] = {}
        self.live = Live(services.bookmaker)
        self.yt = YouTubeFinder()
        self._assets: dict[str, tuple[bytes, bytes, str]] = {}
        self._index: bytes | None = None
        self._warm_task: asyncio.Task | None = None
        self._img_tokens: dict[str, tuple[float, str, str]] = {}    # token -> (ts, render key, file name)
        self._img_jpg: dict[str, bytes] = {}

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
            if user is None and request.path.startswith("/api/analyze") and config.ANALYZER_ACCESS != "owner":
                guest = validate_init_data_any(request.headers.get("X-Telegram-Init-Data", ""), self.bot_token)
                if guest and config.analyzer_allowed(int(guest["id"]), self.owner_id()):
                    user = {**guest, "guest": True}     # analyzer only (ANALYZER_ACCESS=allowlist|public)
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
        app.router.add_post("/api/draw5", self.draw5)
        app.router.add_post("/api/ai", self.ai)
        app.router.add_post("/api/ai/reset", self.ai_reset)
        app.router.add_get("/api/match_markets", self.match_markets)
        app.router.add_get("/api/admin/diag", self.diag)
        app.router.add_get("/api/history", self.history)
        app.router.add_get("/api/rollover", self.rollover_get)
        app.router.add_get("/api/rollover/tickets", self.rollover_tickets)
        app.router.add_post("/api/history/book", self.history_book)
        app.router.add_post("/api/rollover", self.rollover_post)
        app.router.add_post("/api/admin/tick", self.admin_tick)
        app.router.add_post("/api/admin/test_settle", self.admin_test_settle)
        app.router.add_post("/api/admin/delete_test", self.admin_delete_test)
        app.router.add_get("/api/live", self.live_list)
        app.router.add_post("/api/analyze", self.analyze)
        app.router.add_post("/api/analyze/rebook", self.analyze_rebook)
        app.router.add_post("/api/analyze/narrative", self.analyze_narrative)
        app.router.add_get("/api/live/match", self.live_match)
        app.router.add_post("/api/slipimg", self.slipimg)
        app.router.add_post("/api/slipimg/send", self.slipimg_send)
        app.router.add_post("/api/slipimg/prepare", self.slipimg_prepare)
        app.router.add_get("/slipimg/{token:[A-Za-z0-9_-]{16,40}}.{ext:png|jpg}", self.slipimg_file)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

    # ---- background warm-up ---------------------------------------------------
    async def _on_startup(self, _app) -> None:
        if os.environ.get("FRA_WARMER", "1") != "0":
            self._warm_task = asyncio.create_task(self._warm_loop())

    async def _on_cleanup(self, _app) -> None:
        if self._warm_task:
            self._warm_task.cancel()
        await self.live.aclose()
        await self.yt.aclose()

    async def _warm_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                hour = datetime.now(TZ).hour
                if hour >= 7:   # SportyBet relay calls are metered: skip the small hours
                    await self._build_events("today")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("events warm-up failed: %s", exc)
            await asyncio.sleep(WARM_EVERY)

    # ---- static -----------------------------------------------------------------
    def _version(self) -> str:
        h = hashlib.sha1()
        for n in ("app.js", "app.css"):
            p = STATIC_DIR / n
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()[:10]

    async def index(self, request: web.Request) -> web.StreamResponse:
        """index.html with content-hashed asset URLs (so the assets themselves can be cached for a year)."""
        if self._index is None:
            html = (STATIC_DIR / "index.html").read_text()
            self._index = re.sub(r"\?v=[A-Za-z0-9_]+", "?v=" + self._version(), html).encode()
        resp = web.Response(body=self._index, content_type="text/html", headers={"Cache-Control": "no-cache"})
        resp.enable_compression()
        return resp

    async def asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        hit = self._assets.get(name)
        if hit is None:
            path = STATIC_DIR / name
            if not path.is_file():
                raise web.HTTPNotFound()
            raw = path.read_bytes()
            etag = '"' + hashlib.sha1(raw).hexdigest()[:16] + '"'
            hit = (raw, gzip.compress(raw, 9) if path.suffix in (".js", ".css", ".svg") else b"", etag)
            self._assets[name] = hit
        raw, gz, etag = hit
        versioned = bool(request.query.get("v"))
        headers = {"ETag": etag, "Vary": "Accept-Encoding",
                   "Cache-Control": "public, max-age=31536000, immutable" if versioned else "public, max-age=3600"}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        ctype = ASSET_TYPES.get(Path(name).suffix, "application/octet-stream")
        if gz and "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return web.Response(body=gz, headers={**headers, "Content-Type": ctype})
        return web.Response(body=raw, headers={**headers, "Content-Type": ctype})

    async def _build_events(self, rng: str) -> tuple[float, bytes, str]:
        evs = await self.s.events("t" if rng == "today" else "u")
        by_t: dict[str, list] = {}
        for e in evs:
            by_t.setdefault(e.tournament_id, []).append(e)
        leagues = [{"id": tid, "name": name, "count": n, "category": by_t[tid][0].category,
                    "matches": [_event_row(e) for e in by_t[tid]]}
                   for tid, name, n in self.s.leagues(evs)]
        body = json.dumps({"range": rng, "v": 2, "generated": datetime.now(timezone.utc).isoformat(),
                           "count": len(evs), "leagues": leagues}, separators=(",", ":"),
                          ensure_ascii=False).encode()
        hit = (time.time(), body, '"' + hashlib.sha1(body).hexdigest()[:16] + '"')
        self._events_cache[rng] = hit
        return hit

    def _refresh_events(self, rng: str) -> None:
        t = self._events_busy.get(rng)
        if t and not t.done():
            return
        self._events_busy[rng] = asyncio.create_task(self._build_events(rng))

    async def events(self, request: web.Request) -> web.Response:
        """Stale-while-revalidate: a cached list (<= 30 min) is answered at once; when older than 5 min a refresh
        runs in the background. ETag/If-None-Match -> 304 so the phone re-uses its own copy."""
        rng = request.query.get("range", "today")
        if rng not in ("today", "upcoming"):
            return web.json_response({"error": "range must be today or upcoming"}, status=400)
        hit = self._events_cache.get(rng)
        age = time.time() - hit[0] if hit else None
        if hit is None or age > EVENTS_STALE_MAX:
            try:
                busy = self._events_busy.get(rng)
                hit = await busy if busy and not busy.done() else await self._build_events(rng)
            except Exception as exc:
                log.warning("mini app events failed: %s", exc)
                if not hit:
                    return web.json_response({"error": "SportyBet didn't answer just now. Try again in a minute."},
                                             status=502)
        elif age > EVENTS_CACHE_SECONDS:
            self._refresh_events(rng)
        ts, body, etag = hit
        headers = {"ETag": etag, "Cache-Control": "private, no-cache", "X-Data-Age": str(int(time.time() - ts))}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return self._gz(web.Response(body=body, content_type="application/json", headers=headers))

    # ---- live --------------------------------------------------------------
    async def live_list(self, request: web.Request) -> web.Response:
        try:
            rows = await self.live.list()
        except Exception as exc:
            log.warning("live list failed: %s", exc)
            return web.json_response({"error": "SportyBet's live list didn't answer just now."}, status=502)
        return self._gz(web.json_response({"count": len(rows), "matches": rows, "ended": self.live.ended(),
                                           "updated": int(time.time())},
                                          headers={"Cache-Control": "no-store"}))

    async def live_match(self, request: web.Request) -> web.Response:
        eid = request.query.get("event_id", "")
        if not eid.startswith("sr:match:") or not valid_token(eid):
            return web.json_response({"error": "event_id required"}, status=400)
        data = await self.live.match(eid)
        if not data:
            return web.json_response({"error": "No live data for this match right now."}, status=404)
        h = data.get("head") or {}
        if request.query.get("video", "1") != "0" and h.get("live") and h.get("home"):
            data["video"] = await self.yt.find(eid, h["home"], h["away"], h.get("league", ""))
        return self._gz(web.json_response(data, headers={"Cache-Control": "no-store"}))

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
        ai = getattr(self.s, "ai", None)
        meta: dict = {"tier": "custom", "label": "My slip"}
        sid = str(data.get("slip_id") or "")
        if sid and ai and sid in ai.slips:     # a scanner slip: book exactly the server's checked legs
            sl = ai.slips[sid]["slip"]
            legs_meta = list(sl["legs"])
            raw = [{k: l[k] for k in ("event_id", "market_id", "specifier", "outcome_id")} for l in legs_meta]
            meta = {"tier": sl.get("tier", "custom"), "label": sl.get("label", ""), "total_odds": sl.get("total_odds"),
                    "est_prob": sl.get("est_prob"), "target": sl.get("target")}
        else:
            raw = data.get("selections")
            legs_meta = data.get("legs") if isinstance(data.get("legs"), list) else None
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
        legs = []
        for i, s_ in enumerate(sels):
            m = legs_meta[i] if legs_meta and i < len(legs_meta) and isinstance(legs_meta[i], dict) else {}
            m = {k: v for k, v in m.items() if isinstance(v, (str, int, float)) or v is None}
            legs.append({**m, "event_id": s_.event_id, "market_id": s_.market_id, "specifier": s_.specifier,
                         "outcome_id": s_.outcome_id})
        saved = None
        tr = getattr(self.s, "tracker", None)
        if tr:
            if meta["tier"] == "custom" and str(data.get("tier") or "") in ("safe", "mid", "risk", "high"):
                meta["tier"] = str(data["tier"])
            saved = await tr.record(legs, code, "app", meta["tier"], meta.get("label", ""), meta.get("total_odds"),
                                    meta.get("est_prob"), target=meta.get("target"))
        return web.json_response({"code": code, "url": code_link(self.s.bookmaker, code),
                                  "bookmaker": self.s.bookmaker.name, "requested": len(sels),
                                  "loaded": check["legs"] if check else None,
                                  "history_id": saved["id"] if saved else None})

    # ---- history / rollover ------------------------------------------------
    async def history(self, request: web.Request) -> web.Response:
        tr = getattr(self.s, "tracker", None)
        if not tr:
            return web.json_response({"error": "history unavailable"}, status=503)
        try:
            limit = max(1, min(int(request.query.get("limit", "40")), 100))
            rows = await tr.history.list("slip", None, limit=limit)
            ro = await tr.active_rollover() or next(iter(await tr.rollovers(1)), None)
        except Exception:
            return web.json_response({"error": "The history store didn't answer just now."}, status=502)
        return self._gz(web.json_response({"slips": rows, "rollover": ro, "backend": tr.history.backend,
                                           "last_tick": tr.last_tick}))

    async def history_book(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        try:
            rec = await self.s.tracker.book_record(str(data.get("id") or ""))
        except (BookingError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=502)
        return web.json_response({"code": rec["data"]["code"], "record": rec})

    async def rollover_get(self, request: web.Request) -> web.Response:
        tr = self.s.tracker
        try:
            act = await tr.active_rollover()
            past = await tr.rollovers(6)
        except Exception:
            return web.json_response({"error": "The history store didn't answer just now."}, status=502)
        day_slip, tickets = None, []
        if act:
            last = next((x for x in reversed(act["data"]["log"]) if x.get("slip_id")), None)
            if last:
                day_slip = await tr.history.get(last["slip_id"])
            try:
                tickets = await tr.rollover_tickets(act)
            except Exception:
                tickets = []
            if day_slip:
                cur = next((t for t in tickets if t["slip_id"] == day_slip["id"]), None)
                day_slip["can_refresh"] = bool(cur and cur.get("can_refresh"))
        return self._gz(web.json_response({"active": act, "past": past, "day_slip": day_slip, "tickets": tickets,
                                           "targets": [2, 3, 4, 5], "days": [3, 15]}))

    async def rollover_tickets(self, request: web.Request) -> web.Response:
        """Every day's ticket of one plan (active or past): legs, status, stake / return, running bankroll."""
        tr = self.s.tracker
        pid = str(request.query.get("id") or "")[:40]
        try:
            plan = await tr.history.get(pid) if pid else None
            if not plan or plan.get("kind") != "rollover":
                return web.json_response({"error": "plan not found"}, status=404)
            tickets = await tr.rollover_tickets(plan)
        except Exception:
            return web.json_response({"error": "The history store didn't answer just now."}, status=502)
        return self._gz(web.json_response({"plan": plan, "tickets": tickets}))

    async def rollover_post(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        tr = self.s.tracker
        act = str(data.get("action") or "")
        admin = bool(request["tg_user"].get("admin"))
        try:
            if act == "start":
                stake = data.get("stake")
                stake = float(stake) if stake not in (None, "") else None
                if stake is not None and not (0 < stake < 1e9):
                    return web.json_response({"error": "stake must be a positive number"}, status=400)
                rec, day = await tr.create_rollover(float(data.get("target") or 0), int(data.get("days") or 0), stake,
                                                    autobook=bool(data.get("book", True)) or not admin,
                                                    test=bool(data.get("test")) and admin,
                                                    push=bool(data.get("push", True)) or not admin,
                                                    allow_unders=bool(data.get("allow_unders")))
                return web.json_response({"ok": True, "rollover": await tr.history.get(rec["id"]), "day": day})
            if act == "stop":
                return web.json_response({"ok": True, "rollover": await tr.stop_rollover()})
            if act == "skip":
                return web.json_response({"ok": True, "rollover": await tr.skip_day()})
            if act == "mark":
                return web.json_response({"ok": True, "rollover": await tr.mark_day(str(data.get("result")))})
            if act == "refresh":
                out = await tr.refresh_day(str(data.get("id") or ""), exclude_prev=bool(data.get("exclude_prev")))
                return web.json_response({"ok": True, **{k: v for k, v in out.items() if k != "record"},
                                          "record": out.get("record")})
            if act == "refresh_confirm":
                try:
                    out = await tr.refresh_confirm(str(data.get("token") or ""))
                except BookingError as exc:
                    return web.json_response({"error": f"Booking failed: {exc}"}, status=502)
                return web.json_response({"ok": True, **out})
            if act == "now":
                rec = await tr.active_rollover()
                if not rec:
                    return web.json_response({"error": "No rollover is running."}, status=400)
                day = await tr.generate_day(rec, force=True)
                return web.json_response({"ok": True, "day": day, "rollover": await tr.history.get(rec["id"])})
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            log.warning("rollover %s failed: %s", act, exc, exc_info=True)
            return web.json_response({"error": "That didn't work just now (scanner or history store). Try again."},
                                     status=502)
        return web.json_response({"error": "unknown action"}, status=400)

    async def admin_tick(self, request: web.Request) -> web.Response:
        if not request["tg_user"].get("admin"):
            return web.json_response({"error": "forbidden"}, status=403)
        data = await self._json(request)
        tr = self.s.tracker
        busy = await tr.tick(only_id=data.get("id") or None)
        return web.json_response({"busy": busy, "summary": tr.last_tick_summary, "backend": tr.history.backend,
                                  "record": await tr.history.get(data["id"]) if data.get("id") else None})

    async def admin_delete_test(self, request: web.Request) -> web.Response:
        """Remove a TEST record (data.test = true) after verification. Real records can't be deleted here."""
        if not request["tg_user"].get("admin"):
            return web.json_response({"error": "forbidden"}, status=403)
        data = await self._json(request)
        rec = await self.s.tracker.history.get(str(data.get("id") or ""))
        if not rec or not rec["data"].get("test"):
            return web.json_response({"error": "not a test record"}, status=400)
        await self.s.tracker.history.delete(rec["id"])
        return web.json_response({"ok": True})

    async def admin_test_settle(self, request: web.Request) -> web.Response:
        """TEST: save a slip made of already-finished legs (code 'TEST'), settle it now, ping the owner once."""
        if not request["tg_user"].get("admin"):
            return web.json_response({"error": "forbidden"}, status=403)
        data = await self._json(request)
        legs = data.get("legs") or []
        if not legs or len(legs) > 10:
            return web.json_response({"error": "1-10 legs"}, status=400)
        tr = self.s.tracker
        rec = await tr.record(legs, "TEST", "test", "test", "Test", test=True)
        if not rec:
            return web.json_response({"error": "history save failed", "detail": tr.history.last_error}, status=502)
        await tr.tick(only_id=rec["id"])
        return web.json_response({"record": await tr.history.get(rec["id"]), "backend": tr.history.backend})

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
        # structured rules (see constraints.py): {"constraints": {...}} and/or the Mini App toggles
        raw = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
        raw = {k: v for k, v in raw.items() if k not in ("tier", "target_odds", "day")}
        c = Constraints.from_dict(raw)
        for m in data.get("exclude_markets") or []:
            nm = norm_market(str(m))
            if nm and nm not in c.exclude_markets:
                c.exclude_markets.append(nm)
        if data.get("no_unders") and "under" not in c.exclude_markets:
            c.exclude_markets.append("under")
        if data.get("all_leagues"):
            c.all_leagues = True        # lift the default top-leagues allowlist
        if data.get("direction") is not None:      # Mini App goals selector: any | over | under
            c.set_direction(data.get("direction"))
            if c.direction and tier != "audit":
                tier = "safe"                       # Overs / Unders only: every leg at the Safe bar
        prev = {str(x)[:120] for x in (data.get("exclude") or []) if isinstance(x, str)}
        prev = prev if len(prev) <= 40 else set()
        try:
            res = await self.s.scanner.scan(tier, target, day, leagues, c if not c.empty() else None,
                                            exclude=prev or None, fresh=bool(data.get("refresh")))
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

    async def draw5(self, request: web.Request) -> web.Response:
        """5-minute draw finder: {"period": "weekend"|"midweek"|"week", "leagues": "free text", "refresh": bool}.
        Returns the ranked list (>= 50 picks, or all there are) plus preview slips for the top 5 / 10 / 20 open picks
        (slip ids for /api/book). Nothing is booked or saved here."""
        import draw5 as d5
        data = await self._json(request)
        period = str(data.get("period") or "weekend").lower()
        period = {"full": "week", "fullweek": "week", "full week": "week", "mid": "midweek"}.get(period, period)
        if period not in d5.PERIODS:
            return web.json_response({"error": "period must be weekend, midweek or week"}, status=400)
        text = str(data.get("leagues") or "")[:200]
        try:
            res = await self.s.draw5.find(period, text, fresh=bool(data.get("refresh")))
        except Exception as exc:
            log.warning("draw5 failed: %s", exc, exc_info=True)
            return web.json_response({"error": "The 5-minute draw search failed (SportyBet didn't answer). "
                                               "Try again shortly."}, status=502)
        out = dict(res)
        ai = getattr(self.s, "ai", None)
        slips = {}
        for n in (5, 10, 20):
            sl = d5.slip_from(res, n)
            if sl["legs"] and ai:
                slips[str(n)] = {"slip_id": ai.remember_slip(sl), "total_odds": sl["total_odds"],
                                 "est_prob": sl["est_prob"], "legs": len(sl["legs"])}
        out["slips"] = slips
        return self._gz(web.json_response(out))

    # ---- bet analyzer -------------------------------------------------------------
    async def analyze(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        from analyzer import extract_code
        raw = str(data.get("code") or "")[:200]
        code = extract_code(raw)
        if not code:
            return web.json_response({"error": "Paste a SportyBet booking code (6 characters, e.g. N6RXGP)."},
                                     status=400)
        an = getattr(self.s, "analyzer", None)
        if an is None:
            return web.json_response({"error": "The analyzer isn't available."}, status=503)
        try:   # analyzed codes are not saved or tracked; only an explicit rebook is
            res = await an.analyze(code, str(data.get("rules") or "")[:300], use_cache=not data.get("fresh"))
        except Exception as exc:
            log.warning("analyze failed: %s", exc, exc_info=True)
            return web.json_response({"error": "SportyBet or the stats feed didn't answer. Try again shortly."},
                                     status=502)
        if not res.get("ok"):
            return web.json_response({"error": res.get("error"), "kind": res.get("kind"), "code": code},
                                     status=404 if res.get("kind") in ("invalid", "empty") else 502)
        return self._gz(web.json_response(res))

    async def analyze_rebook(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        an = getattr(self.s, "analyzer", None)
        try:
            out = await an.rebook(str(data.get("id") or ""), save=not request["tg_user"].get("guest"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except BookingError as exc:
            return web.json_response({"error": f"{exc} A match may have started: analyze the code again."},
                                     status=502)
        return web.json_response(out)

    async def analyze_narrative(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        an = getattr(self.s, "analyzer", None)
        try:
            out = await an.narrative(str(data.get("id") or ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(out)

    async def ai(self, request: web.Request) -> web.Response:
        data = await self._json(request)
        msg = str(data.get("message") or "").strip()[:1500]
        img = data.get("image") if isinstance(data.get("image"), dict) else None
        if not msg and not img:
            return web.json_response({"error": "Ask me something."}, status=400)
        ai = getattr(self.s, "ai", None)
        if not ai or not ai.enabled:
            return web.json_response({"error": "The AI analyst isn't configured on the server yet."}, status=503)
        chat = int(request["tg_user"].get("id") or 0)
        if request["tg_user"].get("admin") and data.get("chat") is not None:
            chat = int(data["chat"])        # diagnostics: keep test turns out of the owner's AI memory
        if img:
            return await self._ai_image(chat, msg, img)
        sctx = None
        cx = data.get("context") if isinstance(data.get("context"), dict) else None
        ed = getattr(self.s, "editor", None)
        if cx and ed is not None:
            # the slip in view in the Mini App: {"kind": "rollover", "slip_id"} | {"kind": "preview", "token", "slip_id"}
            # | {"kind": "scan"|"ai", "sid"} | {"kind": "history", "slip_id"}
            kind = str(cx.get("kind") or "")
            ref = {"rollover": {"section": "rollover_day", "slip_id": str(cx.get("slip_id") or "")},
                   "preview": {"section": "rollover_preview", "token": str(cx.get("token") or ""),
                               "slip_id": str(cx.get("slip_id") or "")},
                   "scan": {"section": "scan", "sid": str(cx.get("sid") or "")},
                   "ai": {"section": "ai", "sid": str(cx.get("sid") or "")},
                   "history": {"section": "history", "slip_id": str(cx.get("slip_id") or "")},
                   "analysis": {"section": "analysis", "aid": str(cx.get("aid") or "")}}.get(kind)
            if ref:
                try:
                    sctx = await ed.hydrate(ref)
                except Exception as exc:
                    log.warning("ai context hydrate failed: %s", exc)
        try:
            res = await (ai.ask(chat, msg, context=sctx) if sctx else ai.ask(chat, msg))
        except Exception as exc:
            log.warning("ai failed: %s", exc)
            return web.json_response({"error": f"The AI analyst is unavailable right now ({str(exc)[:120]})."},
                                     status=502)
        return self._gz(web.json_response(res))

    async def _ai_image(self, chat: int, msg: str, img: dict) -> web.Response:
        """Image from the Mini App AI tab: {"image": {"data": base64, "mime"}, "message": caption}. Same routing as a
        Telegram photo (vision.py); nothing booked or saved."""
        import base64
        import binascii
        import vision
        raw = str(img.get("data") or "")
        if "," in raw[:80] and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        if len(raw) > 9_000_000:
            return web.json_response({"error": "That image is too large (max 5 MB)."}, status=413)
        try:
            blob = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return web.json_response({"error": "That image couldn't be decoded."}, status=400)
        try:
            out = await asyncio.wait_for(vision.handle_image(self.s.ai, chat, blob, str(img.get("mime") or ""), msg),
                                         timeout=150)
        except asyncio.TimeoutError:
            return web.json_response({"error": "Reading that image took too long. Nothing was booked; try again."},
                                     status=504)
        except Exception as exc:
            log.warning("ai image failed: %s", exc, exc_info=True)
            return web.json_response({"error": "I couldn't process that image right now."}, status=502)
        sc = out.get("context") or {}
        if out.get("kind") == "edit" and (out.get("edit") or {}).get("ok"):
            sc = {}
        app_ctx = None
        sec = sc.get("section")
        if sec == "rollover_preview" and sc.get("token"):
            app_ctx = {"kind": "preview", "token": sc["token"], "slip_id": sc.get("slip_id"),
                       "label": f"Rollover day {sc.get('day') or ''} preview"}
        elif sec in ("rollover_day", "rollover_preview") and sc.get("slip_id"):
            app_ctx = {"kind": "rollover", "slip_id": sc["slip_id"], "label": f"Rollover day {sc.get('day') or ''} slip"}
        elif sec in ("scan", "ai") and sc.get("sid"):
            app_ctx = {"kind": sec, "sid": sc["sid"], "label": "Scanner slip" if sec == "scan" else "AI slip"}
        elif sec == "history" and sc.get("slip_id"):
            app_ctx = {"kind": "history", "slip_id": sc["slip_id"], "label": f"Ticket {sc.get('code') or ''}"}
        elif sec == "analysis" and sc.get("aid"):
            app_ctx = {"kind": "analysis", "aid": sc["aid"], "label": f"Analyzed {sc.get('code') or 'slip'}"}
        reply = out.get("reply") or ""
        if out.get("kind") == "edit" and out.get("edit") is not None and not reply:
            reply = "Edited: " + (out["edit"].get("change") or "")
        if "estimate" not in reply.lower() and "guarantee" not in reply.lower() and out.get("kind") in ("analysis",
                                                                                                           "edit"):
            reply += "\n\n_Estimates, not guarantees. 18+._"
        slip = out.get("slip")
        return self._gz(web.json_response({
            "reply": reply, "image_kind": out.get("kind"), "read": out.get("read"), "tools": out.get("tools"),
            "analysis": out.get("analysis"), "edit": out.get("edit"), "context": app_ctx,
            "slip": slip if slip and slip.get("legs") and slip.get("tier") != "audit" else None, "code": None,
            "model": getattr(self.s.ai, "model", None)}))

    async def ai_reset(self, request: web.Request) -> web.Response:
        ai = getattr(self.s, "ai", None)
        if ai:
            chat = int(request["tg_user"].get("id") or 0)
            data = await self._json(request) if request.can_read_body else {}
            if request["tg_user"].get("admin"):
                if data.get("chat") is None:
                    return web.json_response({"error": "admin reset needs an explicit test chat"}, status=400)
                chat = int(data["chat"])
            ai.reset(chat)
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

    # ---- slip images (slipimg.py) ----------------------------------------------
    def _imgs(self):
        imgs = getattr(self.s, "slipimg", None)
        if imgs is None:
            from slipimg import SlipImages
            imgs = self.s.slipimg = SlipImages(self.s)
        return imgs

    async def _img_spec(self, data: dict) -> dict | None:
        """Spec for {history_id} | {code} | {slip_id} | {analysis_id} | {token} | {slip: {legs, label?}} (app slip)."""
        from slipimg import make_spec
        imgs = self._imgs()
        if data.get("history_id"):
            return await imgs.spec_for("h", str(data["history_id"])[:40])
        if data.get("slip_id"):
            return await imgs.spec_for("s", str(data["slip_id"])[:20])
        if data.get("analysis_id"):
            return await imgs.spec_for("a", str(data["analysis_id"])[:20])
        if data.get("token"):
            return await imgs.spec_for("p", str(data["token"])[:40])
        if data.get("code"):
            return await imgs.spec_for_code(str(data["code"])[:16])
        sl = data.get("slip")
        if isinstance(sl, dict) and isinstance(sl.get("legs"), list) and sl["legs"]:
            keep = ("pick", "label", "odds", "est", "home", "away", "match", "home_uid", "away_uid", "market",
                    "league", "category", "kickoff", "start", "status", "score")
            legs = []
            for l in sl["legs"][:30]:
                if isinstance(l, dict):
                    legs.append({k: (str(l[k])[:120] if isinstance(l[k], str) else l[k]) for k in keep
                                 if k in l and (isinstance(l[k], (str, int, float)) or l[k] is None)})
            code = str(sl.get("code") or "")[:12]
            if code and not re.fullmatch(r"[A-Za-z0-9]{4,12}", code):
                code = ""
            return make_spec(legs, code, None, None, str(sl.get("label") or "My slip")[:60])
        return None

    async def slipimg(self, request: web.Request) -> web.Response:
        """Render a slip image. JSON {url, jpg, token, file_name, width, height, caption}; {"format": "png"} answers
        the PNG itself (admin checks). Nothing is booked, saved or sent."""
        import secrets
        from slipimg import png_size
        data = await self._json(request)
        try:
            spec = await self._img_spec(data)
        except Exception as exc:
            log.warning("slip image spec failed: %s", exc, exc_info=True)
            spec = None
        if not spec or not spec.get("legs"):
            return web.json_response({"error": "That slip isn't available any more (expired or not found)."}, status=404)
        imgs = self._imgs()
        try:
            png, key = await imgs.render(spec)
        except Exception as exc:
            log.warning("slip image render failed: %s", exc, exc_info=True)
            return web.json_response({"error": "Couldn't draw that slip just now."}, status=500)
        if data.get("format") == "png":
            return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})
        tok = secrets.token_urlsafe(18)
        now = time.time()
        self._img_tokens = {k: v for k, v in self._img_tokens.items() if now - v[0] < imgs.TTL}
        self._img_tokens[tok] = (now, key, imgs.filename(spec))
        w, h = png_size(png)
        return web.json_response({"token": tok, "url": f"/slipimg/{tok}.png", "jpg": f"/slipimg/{tok}.jpg",
                                  "file_name": imgs.filename(spec), "width": w, "height": h,
                                  "caption": imgs.caption(spec), "legs": len(spec["legs"])})

    def _img_bytes(self, tok: str, ext: str) -> tuple[bytes, str] | None:
        hit = self._img_tokens.get(tok)
        imgs = self._imgs()
        if not hit or time.time() - hit[0] > imgs.TTL:
            return None
        png = imgs.cached(hit[1])
        if png is None:
            return None
        if ext == "png":
            return png, hit[2]
        jpg = self._img_jpg.get(hit[1])
        if jpg is None:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(png)) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
            jpg = buf.getvalue()
            self._img_jpg = {k: v for k, v in list(self._img_jpg.items())[-10:]}
            self._img_jpg[hit[1]] = jpg
        return jpg, hit[2].replace(".png", ".jpg")

    async def slipimg_file(self, request: web.Request) -> web.Response:
        """Short-lived public copy of a rendered slip (unguessable token, 15 min) for Telegram downloadFile /
        prepared messages, which fetch the file without the initData header."""
        ext = request.match_info["ext"]
        got = self._img_bytes(request.match_info["token"], ext)
        if got is None:
            return web.Response(status=404, text="expired")
        body, name = got
        disp = "attachment" if request.query.get("dl") else "inline"
        return web.Response(body=body, content_type="image/png" if ext == "png" else "image/jpeg",
                            headers={"Cache-Control": "private, max-age=900",
                                     "Content-Disposition": f'{disp}; filename="{name}"'})

    def _bot(self):
        tr = getattr(self.s, "tracker", None)
        return getattr(tr, "bot", None)

    async def slipimg_send(self, request: web.Request) -> web.Response:
        """The bot sends the rendered slip to the requesting user's chat with the bot (explicit tap only)."""
        data = await self._json(request)
        user = request["tg_user"]
        got = self._img_bytes(str(data.get("token") or ""), "png")
        bot = self._bot()
        if got is None:
            return web.json_response({"error": "That image expired: tap Share image again."}, status=404)
        if bot is None or not user.get("id") or user.get("admin"):
            return web.json_response({"error": "Sending isn't available here."}, status=400)
        png, name = got
        try:
            from slipimg import TELEGRAM_PHOTO_MAX, png_size
            w, h = png_size(png)
            if max(w, h) > TELEGRAM_PHOTO_MAX:
                await bot.send_document(int(user["id"]), document=png, filename=name,
                                        caption="FraPredict slip (full size). Estimates, not guarantees. 18+.")
            else:
                await bot.send_photo(int(user["id"]), photo=png, filename=name,
                                     caption="FraPredict slip. Estimates, not guarantees. 18+.")
        except Exception as exc:
            log.warning("slip image send failed: %s", exc.__class__.__name__)
            return web.json_response({"error": "Telegram didn't take the image just now."}, status=502)
        return web.json_response({"ok": True})

    async def slipimg_prepare(self, request: web.Request) -> web.Response:
        """A prepared inline message (Bot API savePreparedInlineMessage) so Telegram.WebApp.shareMessage can share
        the slip photo to any chat the user picks. Falls back (error) when the bot or Telegram doesn't support it."""
        data = await self._json(request)
        user = request["tg_user"]
        tok = str(data.get("token") or "")
        bot = self._bot()
        if self._img_bytes(tok, "jpg") is None:
            return web.json_response({"error": "That image expired: tap Share image again."}, status=404)
        base = (config.WEBHOOK_URL or f"https://{request.host}").rstrip("/")
        if bot is None or not base or not user.get("id") or user.get("admin"):
            return web.json_response({"error": "Sharing isn't available here."}, status=400)
        try:
            from telegram import InlineQueryResultPhoto
            url = f"{base}/slipimg/{tok}.jpg"
            res = InlineQueryResultPhoto(id=tok[:32], photo_url=url, thumbnail_url=url,
                                         caption="FraPredict slip · Estimates, not guarantees. 18+.")
            pm = await bot.save_prepared_inline_message(user_id=int(user["id"]), result=res, allow_user_chats=True,
                                                        allow_bot_chats=True, allow_group_chats=True,
                                                        allow_channel_chats=True)
        except Exception as exc:
            log.warning("prepared inline message failed: %s: %s", exc.__class__.__name__, str(exc)[:160])
            return web.json_response({"error": "Telegram sharing isn't available for this bot: use Send to chat."},
                                     status=502)
        return web.json_response({"id": pm.id})

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
            out["gemini_last_errors"] = getattr(ai, "last_errors", None)
            out["gemini_last_turn"] = getattr(ai, "last_turn", None)
            out["gemini_last_image"] = getattr(ai, "last_image", None)
        tr = getattr(self.s, "tracker", None)
        if tr:
            out["history_backend"] = tr.history.backend
            out["tracker_last_tick"] = tr.last_tick
            out["tracker_summary"] = tr.last_tick_summary
            try:
                out["history_slips"] = len(await tr.history.list("slip", None, limit=100))
            except Exception as exc:
                out["history_error"] = exc.__class__.__name__ + " " + tr.history.last_error
        out["api_football_left"] = self.s.stats.remaining_today() if self.s.stats.enabled else None
        out["model"] = model.info()
        out["events_cache_age_s"] = {k: int(time.time() - v[0]) for k, v in self._events_cache.items()}
        return web.json_response(out)
