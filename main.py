"""Entry point. Webhook mode when WEBHOOK_URL (or RENDER_EXTERNAL_URL) is set, else long polling.
A tiny aiohttp server serves GET/HEAD /health (200) for keep-alive pings, plus the Telegram Mini App
(/app UI and /api/* JSON), in both modes when PORT is set."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import signal

from aiohttp import web
from telegram import Update
from telegram.ext import Application
from telegram.request import HTTPXRequest

import config
import handlers
from ai import Analyst
from services import Services
from webapp import MiniApp

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs, which include the bot token
log = logging.getLogger("main")

WEBHOOK_PATH = "/telegram"


def _secret_token() -> str:
    if config.WEBHOOK_SECRET:
        return config.WEBHOOK_SECRET
    return hashlib.sha256(("whsec:" + config.TELEGRAM_BOT_TOKEN).encode()).hexdigest()[:48]


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app(webhook: bool) -> Application:
    req = HTTPXRequest(connect_timeout=20, read_timeout=30, write_timeout=30, pool_timeout=10)
    builder = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(handlers.post_init)
               .request(req).get_updates_request(HTTPXRequest(connect_timeout=20, read_timeout=40)))
    if webhook:
        builder = builder.updater(None)
    app = builder.build()
    services = Services()
    services.ai = Analyst(services)
    app.bot_data["services"] = services
    handlers.register(app)
    return app


async def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")
    webhook = bool(config.WEBHOOK_URL)
    app = build_app(webhook)
    services: Services = app.bot_data["services"]
    log.info("Stats: Sportradar feed (free) + %s", "API-Football fallback" if services.stats.enabled else "no fallback")
    log.info("AI analyst: %s", "Gemini" if services.ai.enabled else "disabled (GEMINI_API_KEY unset)")
    log.info("Owner: %s", "fixed by OWNER_TELEGRAM_ID" if config.OWNER_TELEGRAM_ID else
             ("claimed" if services.storage.get_owner() else "unclaimed – first /start becomes owner"))

    web_app = web.Application()
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/", health)

    MiniApp(services).register(web_app)  # /app (UI) + /api/* (initData-protected)
    secret = _secret_token()

    async def telegram_webhook(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret):
            return web.Response(status=403)
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400)
        await app.update_queue.put(Update.de_json(data, app.bot))
        return web.Response(text="ok")

    if webhook:
        web_app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    runner = None
    port = config.PORT or (8080 if webhook else None)
    if port:  # bind the port first so the host's health check passes while Telegram warms up
        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info("HTTP server on :%s (/health, /app, /api%s)", port, ", " + WEBHOOK_PATH if webhook else "")
    for attempt in range(1, 9):
        try:
            await app.initialize()
            break
        except Exception as e:  # network blips (TimedOut/NetworkError) on cold start
            wait = min(60, 5 * attempt)
            log.warning("Telegram init failed (attempt %s): %s; retrying in %ss", attempt, type(e).__name__, wait)
            await asyncio.sleep(wait)
    else:
        raise SystemExit("Could not reach Telegram after retries.")
    # post_init only runs automatically inside run_polling/run_webhook; this manual lifecycle must call it.
    if app.post_init:
        try:
            await app.post_init(app)
        except Exception as e:
            log.warning("post_init failed: %s", type(e).__name__)
    await app.start()
    if webhook:
        await app.bot.set_webhook(config.WEBHOOK_URL + WEBHOOK_PATH, secret_token=secret,
                                  allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        log.info("Webhook mode: Telegram -> <WEBHOOK_URL>%s", WEBHOOK_PATH)
    else:
        await app.bot.delete_webhook(drop_pending_updates=False)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Polling mode")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await stop.wait()

    log.info("Shutting down")
    if not webhook:
        await app.updater.stop()
    await app.stop()
    if runner:
        await runner.cleanup()
    await services.ai.aclose()
    await services.aclose()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
