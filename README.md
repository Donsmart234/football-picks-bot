# Private Football Picks Bot (Telegram)

A single-owner Telegram bot that lists football matches and odds from **SportyBet Nigeria** (all leagues),
estimates outcomes from **de-margined SportyBet prices plus a backtested, calibrated goals model (model v2)**, keeps a bet
slip, and turns the slip into a **SportyBet booking code**. No money is ever placed by the bot. A booking
code is just a shareable slip you load on SportyBet yourself.

> Every prediction is an estimate, never a guarantee. 18+. Bet only what you can afford to lose.

## What's new (2026-11-01): smooth Mini App, Live view, model v2 (backtested)
**Model v2** (`model.py`, `model_params.json`, `backtest/`). Leg estimate = `calibrate_g(logit-blend_g(market, stats))`:
the stats model is now a time-decayed (half-life 90 days) Poisson from both teams' last 10 games with league-specific
home/away goal averages (15 leagues) and a Dixon-Coles low-score correction (rho −0.12); the blend weight per market group and
a per-group calibration map (Platt / isotonic / none, chosen by 5-fold CV; at most ±3 pts) are tuned by `backtest/backtest.py`
on 9,761 matches (24/25 + 25/26, 15 European leagues: Sportradar-feed results + football-data.co.uk market-average prices,
11,012 joined matches) and tested out-of-sample on 1,251 matches of 26/27 (to 29 Oct). **The Safe ≥80% bar now applies to the
calibrated estimate.** Honest findings (`backtest/REPORT.md`): the old stats nudge (−8/+4 pts) and the −2 pt shrink made every
market *worse* than the de-margined prices alone (e.g. 1X2 log loss 0.9962 vs 0.9921, O/U 2.5 0.6700 vs 0.6655); team stats add
almost nothing over the prices (tuned weight 0 for every group except BTTS 0.10, also with a league-ratings model tried), so v2 is
essentially "market + calibration". v2 beats the old estimate on every market tested (1X2 0.9914, O/U 2.5 0.6655, DNB 0.5831 vs
0.5897, team totals, DC) and is level with the plain market (small gains on 1X2/DNB/team totals, tiny losses on away O/U 1.5).
At the Safe bar (legs 1.05–1.35, est ≥80%): old 2,351 legs, est 84.5%, hit 84.5%; v2 2,840 legs, est 84.4%, hit 84.1%:
both well calibrated, v2 qualifies ~20% more legs at about the same hit rate, so it is not "safer", just as honest with more
choice. Lines without a historical price (O/U 0.5/1.5/3.5/4.5, BTTS, team totals, halves, DC) were tested against the Poisson
fit to 1X2 + O/U 2.5, corners/cards/props aren't backtested and keep their market estimate. Estimates, not guarantees.
Re-run: `python backtest/fetch.py` (in a data dir) then `python backtest/backtest.py <data dir>`.

**Live view** (`live.py`, Matches → Live). In-play list from SportyBet `factsCenter/liveOrPrematchEvents` (score, minute, live
1X2, LIVE badges, "N matches in play" banner), per match: live odds from `factsCenter/event?productId=1` (1X2, next goal, O/U
lines nearest 50/50, rest of match, DC, GG/NG, DNB, with de-margined %), our own animated tracker drawn from the free Sportradar
feed (`match_timeline` ball position + attack/dangerous situations, momentum per minute, `match_detailsextended` stats),
timeline (goals, cards, corners, subs, shots). Auto-refresh 8 s (list 20 s), paused when the app is hidden; score change flashes
+ haptic. **No unlicensed video, ever.** The Sportradar Live Match Tracker widget is licensed per client and domain-locked
(`widgets.sir.sportradar.com/sportybet/widgetloader` → 403), so it isn't embedded. Video: `youtube.py` searches YouTube's
public results page (Live filter, no API key) and embeds a stream only when it is live, from a **verified** channel whose
name is one of the two clubs, the competition or a known official free broadcaster, the title names both teams (no
"live score"/simulation/watch-along words) and oEmbed allows embedding (`youtube-nocookie` iframe, muted autoplay). On
2026-11-01 every "LIVE" result for the J1/K-League games checked was a score ticker or re-stream (one from a verified
but unrelated channel), so none were shown. Otherwise the view shows **Where to watch** (or **Watch on SportyBet** when
SportyBet's event JSON flags a stream; none seen today) linking to the official `sportybet.com/ng/sport/football/live/...` page.

**Smooth Mini App.** Server: today's events list warmed every 10 min (07:00–24:00 WAT), stale-while-revalidate (≤30 min copy
answered at once, refresh in background), ETag/304, compact rows (`[id, home, away, start, home_uid, away_uid, 1, X, 2]`:
today 216 KB → 113 KB raw, 44 → 38 KB gzip), content-hashed asset URLs with `immutable` caching + gzip (JS 60 → 25 KB, CSS
26 → 7 KB; relaunch transfer 157 → 60 KB). App: last copies of events / history / rollover in localStorage (instant views,
quiet refresh), background prefetch of the other tabs, skeleton markup in the HTML, deferred scripts, cached `Intl` formatters,
`content-visibility` on long lists, small crests (4 KB vs 7 KB), search filters in place (keeps focus), per-view scroll memory,
one throttled haptic tick per tap, Telegram viewport/safe-area/content-safe-area events → CSS vars. Measured (headless Chromium,
4× CPU throttle, 150 ms / 1.6 Mbps): relaunch Matches tap → list 460–640 ms → 19–35 ms; first paint 0.84–1.15 s → 0.67–0.88 s;
cold server first Matches load is still ~7 s (SportyBet pages via the relay), which the warm-up loop hides in production.

## What's new: scanner, AI analyst, fintech UI
**Market scanner** (`scanner.py`, `markets.py`). For a window (today / tomorrow / next 24h / 48h) it picks up to 80 matches
(major and reliable leagues first, then by market depth; SRL/virtual/eSoccer excluded), fetches **every open market** of each
match from `GET /api/ng/factsCenter/event?eventId=<sr:match:..>&productId=3` (360–1,430 markets per match: main, goals,
halves, minutes, combos, corners, bookings, player props, early-payout 1UP/2UP), 6 at a time, through the relay, and scores
every outcome:
- **Full books** (outcomes cover the event, overround ≤ 1.05 + 0.035·n): proportional de-margin; Double Chance-type books
  normalise to 2. **Overlapping / partial / single-outcome markets** (goal bounds, multigoals, player props, …): each price on its
  own (implied ÷ 1.06). Betradar's own outcome `probability` from the feed is blended in when present.
- **Goals markets with a model** (1X2, DC, DNB, all O/U x.5 lines, GG/NG, team totals, Asian x.5 / European handicaps, 1st/2nd
  half versions): + a Poisson model fitted to that match's 1X2 + O/U 2.5 prices.
- Confidence levels: `model` (Poisson + stats) › `market` (full-book de-margin, −1.5 pts) › `thin` (single price, −3 pts).
- **Stats check** (shortlist of the 40 strongest matches): free Sportradar feed → last-10 results per team (venue, FT/HT
  scores), home/away records, goals for/against, O2.5/BTTS/clean-sheet rates, last 6 H2H. A stats Poisson
  (`predictor.stats_lambdas`) prices each modelled leg: **confirmed** (stats ≥ market − 3 pts), **demoted** (−3…−10), **dropped**
  (< −10 pts, or a form red flag such as backing a team that lost 3 of its last 5). The estimate moves by at most −8/+4 pts.
  Corners/bookings/props/specials stay **market only** (stats can't price them) and say so. Every leg carries a short *why*
  (e.g. "Chelsea W2 D1 L2 last 5, home W4 D1 L1; Man Utd W0 D2 L3 last 5, away W0 D3 L2; H2H 2-1-3, avg 2.8 goals").
- **Tiers** (one pick per match, greedy by "odds gained per unit of hit probability lost", which maximises the slip's combined
  chance): Safe — **every leg est. ≥80% after the stats check** (an estimate, not a promised accuracy) at 1.05–1.35 (default target 5) · Mid-safe ≥70% at 1.30–1.70 (10) · Risk ≥55% at 1.60–2.30 (20) ·
  High risk value legs (est × odds ≥ 0.97, confirmed) at 2.0–8.0 (50) · **Grand audit** = best leg per match, top value bets and
  one slip per tier. If not enough legs qualify the slip says so and stops short instead of padding.
- Pools are cached 10 min (≈20–30 s cold, instant warm). Booking uses the feed's own `marketId` / `specifier` / `outcomeId`;
  after booking the code is read back (`GET /api/ng/orders/share/<code>`) to confirm every leg loaded. Verified 2026-10-31
  with 1st-half, 2nd-half, team totals, handicaps, corners, bookings, 1UP, specials, player props and minute markets.

**AI analyst** (`ai.py`): Google Gemini (auto-picks `gemini-3.5-flash` if listed; override with `GEMINI_MODEL`) with function
calling over `list_matches`, `predict_match` (probabilities + form/H2H + strongest legs across all markets), `team_stats`,
`scan_markets` (any tier / target / day / leagues) and `build_booking_code` (from a `slip_id`). Owner's non-command Telegram text
goes to the AI (`/ai <question>` also works, `/reset` clears memory); the Mini App has an AI tab. Last 10 messages per chat are
kept in SQLite. Without `GEMINI_API_KEY` free text falls back to the match finder.

**Stats sources.** Primary: `https://stats.fn.sportradar.com/sportybet/en/Africa:Lagos/gismo/stats_team_lastx/<uid>/10` and
`.../stats_team_versusrecent/<homeUid>/<awayUid>` (the feed SportyBet's own widgets use; no key, no quota; SportyBet's
`sr:competitor:<uid>` ids are its team uids; cached 6 h in SQLite). API-Football is now only a fallback for single-match predictions
when the free feed has nothing and ≥20 requests are left today, so the 100/day quota is never spent on scanning.

## What's new (2026-10-31, night): rules the AI and scanner obey exactly
**Constraint layer** (`constraints.py`). One structured object, `Constraints`, used by the AI tools, `/scan`, the Mini App and
rollover: `tier`, `target_odds`, `min_leg_prob`, `min_legs`/`max_legs`, `min_leg_odds`/`max_leg_odds`, `include_markets`/`exclude_markets`,
`include/exclude_leagues`, `include/exclude_countries`, `include/exclude_teams`, `day`, `kickoff_after`/`kickoff_before` (local HH:MM or
`YYYY-MM-DD HH:MM`, Africa/Lagos), `stats_confirmed_only`. The scanner drops rule-breaking legs **at the candidate stage** (so the slip is
built from the best remaining legs), match-level rules also choose which 80 matches are scanned, and `verify()` re-checks the finished slip:
`constraint_check = {ok, violations, target_reached, warnings}`. Rules that can't be met at the safety bar give a short slip with an
honest note, never a broken rule. A slip now "hits" a target when total odds land in 0.97–1.25× it; Safe/Mid/Risk slips use a
combination search (highest combined estimate inside 0.97–1.20×, one leg per match) and fall back to the greedy builder.

**Market types** (every leg carries `tags`): `under, over, totals, team_totals, 1x2, double_chance, dnb, handicap, btts, corners, cards,
player_props, halves, combo, correct_score, exact_goals, odd_even, minutes, specials`. **Under** = any pick labelled Under (match, team,
1st/2nd half, minutes, corners, bookings/booking points, shots, Asian totals, "Home & Under 2.5"-type combos), "Yes" on a market named
"… Under …" (Both Halves Under 1.5, Home Team or Under 2.5), "No" on a yes/no "… Over …" market, and upper-bound picks (goal / corner /
booking ranges or bounds starting at 0, exact 0, "No Goal"). GG/NG No, clean sheets and win-to-nil are not counted as unders.

**AI** (`ai.py`). `scan_markets` takes all constraint fields as function args; the system prompt tells Gemini to translate every rule
("sure"/"safe"/"banker" = Safe ≥80% per leg; "without unders" = `exclude_markets=["under"]`), read `constraint_check` before replying
or booking, and say plainly when rules can't be met. Server-side safety net: the user's own message is parsed deterministically
(`parse_text`) and merged into the model's args (exclusions unioned, stated numbers win), so a rule the model forgets is still enforced.
`build_booking_code` refuses a slip whose check failed or that breaks the rules in the current message, and won't book a slip that fell
short of its target unless `accept_short=true` (user agreed). Replies with a constrained slip end with a server line
"✅ Rules check: … · N legs · total X". Wording stays "estimates, not guarantees".

**/scan filters**: `/scan safe 5 nounders -corners -cards only:1x2,dc legs:4-8 minodds:1.15 maxodds:1.4 league:epl country:spain
team:arsenal notteam:chelsea after:18:00 before:22:00 minp:85` (plain words like "no unders" work too). **Mini App**: Filters chips under
the target (No unders / corners / cards / player props / handicaps / combos; remembered on the phone; `POST /api/scan` accepts
`exclude_markets`, `no_unders` or a full `constraints` object). **Rollover excludes unders by default** (every kind, at the candidate stage);
allow them with `/rollover 3 7 unders` or the "No unders (tap to allow)" chip; stored per plan as `allow_unders`.

## What's new (2026-10-31, afternoon): history, result pings, rollover, crests
**Safe = every leg ≥80%.** The scanner only admits Safe legs whose estimate *after* the stats adjustment is ≥80% (plus a hard
filter before the slip is returned). If those legs can't reach the target odds the slip stops short and says so. The AI's
`build_booking_code` books Safe slips by `slip_id` (the server's checked legs); explicit selections with `tier="safe"` are re-priced
and refused if any leg is under 80%. The Mini App books scanner slips by `slip_id` too, and flags "Below Safe bar" legs in the
Slip tab. 80% is an estimate, never a guarantee.

**History + result tracking** (`history.py`, `settle.py`, `tracker.py`). Every booked code (chat `/scan` → Book, `/book`, AI
`build_booking_code`, Mini App Book, rollover days) is saved with code, time, tier, total odds and each leg (event id, teams +
Sportradar crest ids, league, kickoff, market, pick, odds, est.). Storage: one table `public.fra_records` (id, kind
`slip|rollover`, status, created_at, updated_at, data jsonb; RLS on, no policies) in the Supabase project `ectglmhgqfiipexvczmh`,
written through PostgREST with the service key (`SUPABASE_URL` + `SUPABASE_SERVICE_KEY` on Render); SQLite fallback when unset.
A background loop (every 5 min while anything is pending, 15 min otherwise) settles legs from the free Sportradar feed
(`match_info/<id>`: status + regular-time and half-time score; `match_detailsextended/<id>`: corners/cards). Auto-settled: all
modelled goals markets (1X2, DC, DNB, O/U, team totals, GG/NG, Asian x.5/European handicaps, FT/1H/2H), odd/even, correct score,
exact goals, multigoals, corners (O/U, 1X2, handicap, team totals, odd/even) and bookings (O/U, 1X2, team totals; yellow 1, red 2).
Player props, specials, minute markets and combos become **Check** once the match ends. Cancelled/abandoned → void (odds drop out).
**Pings (one per slip, never repeated):** 💥 "BOOM" with code, total odds and every leg's score when the whole slip lands; a short ❌
as soon as a leg loses; ❔ when nothing lost but a leg needs a manual look. Per-leg wins are shown in History, not pinged.

**Rollover** (`/rollover`, Mini App Rollover tab). Pick a daily target (2, 3, 4 or 5) and 3–15 days, optional starting stake. Each
morning from 09:00 Africa/Lagos (until 20:00) the day's slip is built from an allowlist of top competitions (`ROLLOVER_LEAGUES` in
`scanner.py`: EPL, Championship, FA/EFL Cup, LaLiga, Copa del Rey, Serie A, Coppa Italia, Bundesliga, DFB Pokal, Ligue 1, Coupe de
France, UCL/UEL/UECL, Eredivisie, Liga Portugal, Scottish Premiership, Süper Lig, Belgian Pro League, MLS, Brasileirão, Argentina
Liga Profesional; women/youth/reserve sides excluded) using only **stats-confirmed** legs with est. ≥80% and odds ≥1.12; a
search picks the combination with the highest combined estimate whose total lands between 0.97× and 1.20× the target (fewest legs
on ties). It's booked, saved and pushed to the owner. A win moves to the next day (stake compounds), a loss busts the plan; when
nothing qualifies the bot says so and suggests skipping the day (the plan stays on the same day). `/rollover now|skip|stop`,
`/rollover won|lost` settles a day the tracker couldn't. Because every leg must be ≥80%, prices are short (≈1.12–1.25), so higher targets need more legs (31 Oct test: 4× = 10 legs, 5× = 12 legs, est. 22–28%).

**Crests + tidy layout.** Team crests from `https://img.sportradar.com/ls/crest/medium/<uid>.png` (uid = SportyBet's
`sr:competitor:<uid>`; missing crests return a 1×1 gif → coloured initials badge), country flags from `https://flagcdn.com/w40/<iso>.png`
(emoji in Telegram). Leg cards: flag · league · kickoff, both teams with crests, odds ticker, market + pick, probability bar; lists
grouped by kickoff day; History cards with per-leg status bars and scores. Telegram messages use one consistent block per leg.

**Keep-alive.** Supabase `pg_cron` job `fra-keepalive` (`*/10 * * * *`) calls `net.http_get('https://football-picks-bot.onrender.com/health')`
via `pg_net`, so the free instance doesn't sleep and the tracker / 09:00 rollover job keep running. Note: staying awake 24/7 uses
~744 of Render's 750 free instance-hours a month, so keep this the only free service on the account.

## Mini App (Telegram Web App)
Dark fintech UI (`webapp_static/`): **Scanner** (tier chips Safe / Mid-safe / Risk / High risk / Grand audit, day toggle, target
odds with quick picks, Generate → portfolio card with total odds, est. hit chance, risk meter, legs as price tickers with
probability bars, stats verdict chips and the *why*; one-tap **Book on SportyBet** → bottom sheet with the code, Copy and Open
SportyBet) · **Matches** (leagues → matches → prediction, team-stats card with form pills / home-away / goals / H2H, strongest legs
across all markets) · **Rollover** (setup, day N/M steps, today's slip, skip/stop) · **History** (won/lost/running, hit rate,
slip cards with leg results) · **AI** chat · **Slip** (portfolio of your picks in localStorage). Skeleton loaders, haptics, Telegram back
button, safe-area insets.

| Route (all `/api/*` need `X-Telegram-Init-Data`, else 403) | Meaning |
|---|---|
| `GET /app` (+ assets) | UI |
| `GET /api/events?range=today\|upcoming` | Leagues with compact match rows `[id, home, away, start, home_uid, away_uid, 1, X, 2]`; ETag / 304 |
| `GET /api/live` | In-play matches (score, minute, live 1X2, official SportyBet link, stream flag) |
| `GET /api/live/match?event_id=[&video=0]` | Live odds, tracker feed (ball, momentum, stats, timeline) and `video` (official YouTube stream or null) |
| `POST /api/predict {"event_id"}` | Prediction + `stats` (form/H2H) |
| `GET /api/match_markets?event_id=` | Strongest stats-checked legs across every market of one match |
| `POST /api/scan {"tier","target","day","leagues","exclude_markets"?,"no_unders"?,"constraints"?}` | Slip (or audit) JSON with `constraint_check` |
| `POST /api/book {"slip_id"}` or `{"selections":[…], "legs":[meta…]}` | `{code, url, requested, loaded, history_id}`; saved to history |
| `GET /api/history?limit=` | Saved slips (newest first) + current rollover |
| `POST /api/history/book {"id"}` | Book a saved slip that has no code yet (rollover day) |
| `GET /api/rollover` · `POST /api/rollover {"action": start\|now\|skip\|stop\|mark, target, days, stake, result}` | Rollover plan |
| `POST /api/admin/tick {"id"?}` · `POST /api/admin/test_settle {"legs"}` · `POST /api/admin/delete_test {"id"}` | Admin only: run the tracker now, settle a TEST slip of finished matches (pings once, marked TEST), delete TEST records |
| `POST /api/ai {"message"}` · `POST /api/ai/reset` | AI analyst |
| `GET /api/admin/diag` | Server diagnostics (relay, Sportradar, Gemini models); **only** with `X-Admin-Token` = `ADMIN_TOKEN` |

initData is validated server-side: HMAC-SHA256 with secret `HMAC_SHA256(key="WebAppData", msg=bot_token)`, `auth_date`
younger than 24h, `user.id` = owner. If `ADMIN_TOKEN` (≥24 chars) is set, the header `X-Admin-Token` also authorises `/api/*`
as the owner (used for production checks); leave it unset to disable.

## SportyBet relay
Render's egress IP gets an AWS WAF challenge from SportyBet (HTTP 202, empty body), and SportyBet's real responses
carry no CORS headers, so neither the server nor the phone can call SportyBet directly. All server-side SportyBet
calls therefore go through a tiny **Supabase Edge Function** (JWT verification off):
`GET {relay}?p=/api/ng/factsCenter/pcUpcomingEvents&<params>` and `POST {relay}?p=/api/ng/orders/share`.
It forwards to `https://www.sportybet.com<p>` with browser headers, only for paths starting `/api/ng/`, and returns
`x-upstream-status` / `x-upstream-waf` headers. Set `SPORTYBET_RELAY_URL=` (empty) to call SportyBet directly
(e.g. when running on a Nigerian home connection).

## Commands
| Command | What it does |
|---|---|
| `/start`, `/help` | Help + **Open Fra app** button (first `/start` claims the bot if `OWNER_TELEGRAM_ID` is unset) |
| `/scan [safe\|mid\|risk\|high\|audit] [target] [today\|tomorrow\|24h\|48h] [filters]` | e.g. `/scan safe 5`, `/scan safe 5 nounders`, `/scan high 100 tomorrow`, `/scan audit` → slip with *why* per leg + **Book on SportyBet** button |
| `/ai <question>` or any text | AI analyst, e.g. "Analyze today's games and give me a safe 5 odds Sporty code", "Who wins Arsenal vs Chelsea?" |
| `/today`, `/upcoming` | Browse by league (paged inline keyboards) |
| `/predict Arsenal vs Chelsea` | Classic prediction card with add-to-slip buttons |
| `/slip`, `/book`, `/clear` | Chat slip, booking code, clear |
| `/reset` | Clear the AI chat memory |
| `/history` | Last 12 booked codes with results + the latest running slip |
| `/rollover [2-5] [3-15] [stake] [unders]` · `/rollover now\|skip\|stop\|won\|lost` | Start / manage a rollover plan (no unders unless `unders`) |

Bot menu (set on start): /start, /scan, /ai, /today, /rollover, /history, /slip, /help. Profile photo, description and short description were set via
the Bot API (`setMyProfilePhoto` with an `InputProfilePhotoStatic` upload, `setMyDescription`, `setMyShortDescription`).

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
| `SPORTYBET_RELAY_URL` | no | Default `https://ectglmhgqfiipexvczmh.supabase.co/functions/v1/sb`. Empty string = direct SportyBet calls |
| `GEMINI_API_KEY` | for AI | Google AI Studio key; without it the AI analyst is off |
| `GEMINI_MODEL` | no | Force a model (default: first of gemini-3.5-flash, gemini-flash-latest, gemini-3-flash-preview… that the key lists; 2.5 models are closed to new keys) |
| `ADMIN_TOKEN` | no | ≥24-char secret enabling `X-Admin-Token` server diagnostics; unset = disabled |
| `SUPABASE_URL` | for durable history | `https://ectglmhgqfiipexvczmh.supabase.co` |
| `SUPABASE_SERVICE_KEY` | for durable history | Supabase service_role key (vault "Supabase love-diary service key (fra bot)"); without it history falls back to SQLite, which Render wipes on restart |
| `FRA_WARMER` | no | `0` disables the background warm-up of today's events list (tests) |
| `PUBLIC_BASE_URL` | no | https base for the Mini App; defaults to `WEBHOOK_URL` / `RENDER_EXTERNAL_URL`. Without an https base the app button and menu button are skipped |

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
3. Free services sleep after ~15 min idle. Keep-alive is the Supabase `pg_cron` job above (every 10 min → `/health`, answers
   200 for GET and HEAD). SQL used: `create extension pg_cron; create extension pg_net; select cron.schedule('fra-keepalive',
   '*/10 * * * *', $$select net.http_get(url:='https://football-picks-bot.onrender.com/health', timeout_milliseconds:=30000)$$);`

## How predictions work
1. **Market:** SportyBet 1X2, O/U 2.5, GG/NG and Double Chance odds are de-vigged (proportional). Poisson home/away
   expected goals are fitted to the market's 1X2 + O/U 2.5.
2. **Stats:** first the free Sportradar feed (last 10 games home/away goals, form, H2H; see above). Fallback, only if that has nothing and budget remains (API-Football): the fixture is found among that day's fixtures (one request covers
   the whole day), then each team's season stats (`/teams/statistics`: goals for/against per home/away game, form
   string) and the last 10 head-to-heads. Expected goals = attack × opponent defence ÷ league average, with small-sample
   shrinkage, a ±8% form multiplier (last 5 results) and a light ±10% H2H goal-total nudge.
3. **Blend:** final probabilities = 35% stats-Poisson + 65% market (odds-only mode: Poisson fit + market).
   Double Chance is derived from the final 1X2 so all numbers are consistent.
4. **Best pick:** among selections with ≥50% and odds ≥1.25, the highest expected value (prob × odds); if none
   qualify, the most likely priced selection.

API-Football cost: up to 4 requests the first time a match is analysed (1 shared by every match that day), 0 after
(cached 24h in SQLite). A daily counter (synced with the API's rate-limit headers) stops at 90.

## Data sources (unofficial endpoints, may change without notice; called via the relay above)
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
python tests/test_webapp.py        # initData validation unit tests (fake token)
python tests/test_webapp.py --live # + local server: /app, 403s, /api/events, /api/predict through the relay
python tests/test_webapp.py --book # + ONE real anonymous booking code via /api/book
python tests/test_constraints.py   # offline: Under tagging, rule parsing, scanner/rollover enforcement, AI rule merge + booking refusals
python tests/test_tracker.py       # 26 settlement cases + tracker run on real finished matches (SQLite, fake bot, 1 ping per slip)
```

## Files
`main.py` (run modes, /health, webhook) · `webapp.py` (Mini App API + initData auth) · `webapp_static/` (UI, logo) ·
`handlers.py` (Telegram UX, owner lock, /scan, /history, /rollover, AI chat) · `tracker.py` (history recording, result loop, pings,
rollover plans) · `settle.py` (Sportradar results → won/lost/void/check) · `history.py` (Supabase PostgREST / SQLite store) ·
`fmt.py` (Telegram formatting, flags) · `constraints.py` (slip rules, market-type tags, parsing, verification) · `ai.py` (Gemini analyst + tools) · `scanner.py` (market scanner, tiers,
stats check) · `markets.py` (market catalogue, de-margin, Poisson pricing per market) · `srstats.py` (free Sportradar form/H2H) ·
`services.py` (glue) · `bookmakers.py` (`BookmakerAdapter` + `SportyBetNG`: events, full event markets, booking, load code) ·
`stats.py` (API-Football fallback) · `predictor.py` (model) · `matching.py` · `storage.py` (SQLite: owner, cache, quota, slip,
AI history) · `config.py`
