# Private Football Picks Bot (Telegram)

A single-owner Telegram bot that lists football matches and odds from **SportyBet Nigeria** (all leagues),
estimates outcomes with a **Poisson goals model blended with the de-vigged SportyBet prices**, keeps a bet
slip, and turns the slip into a **SportyBet booking code**. No money is ever placed by the bot. A booking
code is just a shareable slip you load on SportyBet yourself.

> Every prediction is an estimate, never a guarantee. 18+. Bet only what you can afford to lose.

## Commands
| Command | What it does |
|---|---|
| `/start` | Help + responsible-gambling note. The first user to send it becomes the owner if `OWNER_TELEGRAM_ID` is unset |
| `/today` | Rest of today's matches (Africa/Lagos time): league → match browse, paged inline keyboards |
| `/upcoming` | Same for the next 3 days |
| `/predict Arsenal vs Chelsea` | Fuzzy-matches against SportyBet's next 3 days. You can also just type `Arsenal vs Chelsea` |
| `/slip` | Picks with current odds and total odds; remove buttons |
| `/book` | Creates a SportyBet booking code and gives the load link https://www.sportybet.com/ng/m/code-hub/load-code |
| `/clear` | Empties the slip |

After a prediction, buttons let you add the best pick or any 1X2 / O/U 2.5 / GG-NG / Double Chance selection
(one pick per match: adding another pick for the same match replaces it).

## Environment variables
| Name | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From @BotFather |
| `API_FOOTBALL_KEY` | no | API-Football v3 key (free plan: 100 req/day). Without it, or when the daily budget is used, the bot runs **odds-only** |
| `OWNER_TELEGRAM_ID` | recommended | Your numeric Telegram user id. If unset, the first `/start` claims the bot (stored in SQLite). **Set it on Render:** the free disk is wiped on every redeploy/restart |
| `WEBHOOK_URL` | no | Public base URL, e.g. `https://football-picks-bot.onrender.com`. If set → webhook mode at `<WEBHOOK_URL>/telegram`; if unset → long polling. On Render, `RENDER_EXTERNAL_URL` is used automatically when this is empty |
| `PORT` | auto | HTTP port (Render sets it). The `/health` route is served whenever `PORT` is set |
| `WEBHOOK_SECRET` | no | Telegram webhook secret token; derived from the bot token if unset |
| `API_FOOTBALL_DAILY_LIMIT` | no | Self-imposed daily request cap (default 90) |
| `BOT_TIMEZONE` | no | Default `Africa/Lagos` |
| `DATA_DIR` | no | Where the SQLite file lives (default `./data`) |

Never commit secrets: `.env` and `data/` are git-ignored.

## Run locally (polling)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...        # plus optional API_FOOTBALL_KEY / OWNER_TELEGRAM_ID
python main.py
```

## Deploy on Render (free web service)
1. Push this folder to a GitHub repo, then in Render choose **New → Blueprint** and pick the repo (`render.yaml`).
2. Fill in `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, optionally `API_FOOTBALL_KEY`. `WEBHOOK_URL` can stay empty.
3. Free services sleep after ~15 min idle. Add a free keep-alive ping (e.g. cron-job.org or UptimeRobot)
   to `https://<your-service>.onrender.com/health` every 10 minutes. It answers 200 for GET and HEAD.

## How predictions work
1. **Market:** SportyBet 1X2, O/U 2.5, GG/NG and Double Chance odds are de-vigged (proportional). Poisson home/away
   expected goals are fitted to the market's 1X2 + O/U 2.5.
2. **Stats (if API-Football is available):** the fixture is found among that day's fixtures (one request covers
   the whole day), then each team's season stats (`/teams/statistics`: goals for/against per home/away game, form
   string) and the last 10 head-to-heads. Expected goals = attack × opponent defence ÷ league average, with small-sample
   shrinkage, a ±8% form multiplier (last 5 results) and a light ±10% H2H goal-total nudge.
3. **Blend:** final probabilities = 45% stats-Poisson + 55% market (odds-only mode: Poisson fit + market).
   Double Chance is derived from the final 1X2 so all numbers are consistent.
4. **Best pick:** among selections with ≥50% and odds ≥1.25, the highest expected value (prob × odds); if none
   qualify, the most likely priced selection.

API-Football cost: up to 4 requests the first time a match is analysed (1 shared by every match that day), 0 after
(cached 24h in SQLite). A daily counter (synced with the API's rate-limit headers) stops at 90.

## Data sources (unofficial endpoints, may change without notice)
- Fixtures/odds: `GET https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr:sport:1&marketId=1,18,10,29&pageSize=100&pageNum=N&option=1&timeline=<hours>`.
  Needs browser headers (Chrome User-Agent, Referer/Origin sportybet.com); a plain UA gets a CloudFront 403. Cached 5 min.
- Booking: `POST https://www.sportybet.com/api/ng/orders/share` with `{"selections":[{"eventId","marketId","specifier","outcomeId"}]}` → `data.shareCode`.
- Market/outcome ids (verified live 2026-10-31): 1X2 = market 1 (1 Home, 2 Draw, 3 Away); O/U = market 18, specifier
  `total=2.5` (12 Over, 13 Under); Double Chance = market 10 (9 = 1X, 10 = 12, 11 = X2); GG/NG = market 29 (74 Yes, 76 No).

## Tests
```bash
python tests/live_test.py          # live SportyBet fetch, id verification, 3 predictions, offline checks
python tests/live_test.py --book   # + creates ONE real (anonymous, free) booking code with 2 selections
python tests/test_stats_mock.py    # API-Football path with mocked HTTP (no key or quota used)
```

## Files
`main.py` (run modes, /health, webhook) · `handlers.py` (Telegram UX, owner lock) · `services.py` (glue) ·
`bookmakers.py` (`BookmakerAdapter` ABC + `SportyBetNG`) · `stats.py` (API-Football client) ·
`predictor.py` (model) · `matching.py` (fuzzy names) · `storage.py` (SQLite: owner, cache, quota, slip) · `config.py`
