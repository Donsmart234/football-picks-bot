# Savings ladder band backtest (2026-11-14)

Leg banding: strongest leg per match per odds band, bands split at 1.05/1.08/1.11/1.15/1.20/1.35/1.55/1.80 (scanner.ladder_banded, also used by production since 14 Nov). Daily code: 1-4 legs (one per match), highest combined estimate inside the band, same search order as Scanner.ladder_slip (home/neutral preferred markets, then home/neutral, then any side). Old rules = est >= 80% (market only, production until 14 Nov). New rules = safety.py (market est AND stats model each >= 85%, last-10 record rule, no unders). Ladders: start 1,000, the band's 16-day template (1.50-1.80: 39,600 winning + 36,150 savings if all 16 days win; 2.00: 1,000,000 + 568,500), real odds, restart 2 days back from savings, ends when day 1 can't be covered.

Caveat: only 1X2 and O/U 2.5 have real historical prices; other lines priced from the Poisson fit at a 6% margin. No Asian handicaps, national teams or cups in the data. Held days are skipped (no money moves).

## test_2627 (1251 matches, 64 top-league match days)

| Config | codes | days held | won | hit rate | mean odds | mean est | mean legs |
|---|---|---|---|---|---|---|---|
| old rules, 2.00 (1.90-2.15), legs >=1.12 [production until 14 Nov] | 16 | 48 | 8 | 0.5 | 1.916 | 0.4408 | 4.0 |
| new rules, 2.00 (1.90-2.15), legs >=1.12 [14 Nov safe change] | 0 | 64 | 0 | None | None | None | None |
| new rules, 2.00 (1.90-2.15), legs >=1.05 | 0 | 64 | 0 | None | None | None | None |
| old rules, 1.50-1.80, legs >=1.12 | 31 | 33 | 18 | 0.5806 | 1.53 | 0.5571 | 3.03 |
| new rules, 1.50-1.80, legs >=1.12 | 0 | 64 | 0 | None | None | None | None |
| new rules, 1.50-1.80, legs >=1.08 | 1 | 63 | 1 | 1.0 | 1.516 | 0.5575 | 4.0 |
| new rules, 1.50-1.80, legs >=1.05 [new default] | 1 | 63 | 1 | 1.0 | 1.516 | 0.5575 | 4.0 |
| new rules, 1.50-1.80, legs >=1.03 | 1 | 63 | 1 | 1.0 | 1.516 | 0.5575 | 4.0 |
| what-if: new rules, 1.50-1.80, legs >=1.05, up to 6 legs | 7 | 57 | 5 | 0.7143 | 1.513 | 0.5126 | 5.0 |

## all_seasons (11012 matches, 489 top-league match days)

| Config | codes | days held | won | hit rate | mean odds | mean est | mean legs |
|---|---|---|---|---|---|---|---|
| old rules, 2.00 (1.90-2.15), legs >=1.12 [production until 14 Nov] | 143 | 346 | 69 | 0.4825 | 1.912 | 0.4402 | 4.0 |
| new rules, 2.00 (1.90-2.15), legs >=1.12 [14 Nov safe change] | 0 | 489 | 0 | None | None | None | None |
| new rules, 2.00 (1.90-2.15), legs >=1.05 | 0 | 489 | 0 | None | None | None | None |
| old rules, 1.50-1.80, legs >=1.12 | 234 | 255 | 142 | 0.6068 | 1.536 | 0.5604 | 3.0 |
| new rules, 1.50-1.80, legs >=1.12 | 0 | 489 | 0 | None | None | None | None |
| new rules, 1.50-1.80, legs >=1.08 | 6 | 483 | 5 | 0.8333 | 1.516 | 0.5535 | 4.0 |
| new rules, 1.50-1.80, legs >=1.05 [new default] | 6 | 483 | 5 | 0.8333 | 1.511 | 0.5551 | 4.0 |
| new rules, 1.50-1.80, legs >=1.03 | 6 | 483 | 5 | 0.8333 | 1.511 | 0.5551 | 4.0 |
| what-if: new rules, 1.50-1.80, legs >=1.05, up to 6 legs | 48 | 441 | 27 | 0.5625 | 1.517 | 0.5066 | 5.23 |

Ladders (start 1,000). Real sequence = the codes in date order; bootstrap = 20,000 ladders on codes drawn at random from the same codes. With fewer than ~30 codes the bootstrap only repeats a handful of results and says little (the new-rules 1.50-1.80 rows rest on 6 codes).

| Config | real: ladders | completed | busted | running | mean savings at end | mean net | bootstrap: completed % | mean savings at end | mean net | median net | codes per ladder |
|---|---|---|---|---|---|---|---|---|---|---|---|
| old rules, 2.00 (1.90-2.15), legs >=1.12 [production until 14 Nov] | 44 | 0 | 43 | 1 | 213.0 | -787.0 | 0.01 | 292.0 | -565.0 | -1000.0 | 3.2 |
| new rules, 2.00 (1.90-2.15), legs >=1.12 [14 Nov safe change] | 0 | 0 | 0 | 0 | None | None | None | None | None | None | None |
| new rules, 2.00 (1.90-2.15), legs >=1.05 | 0 | 0 | 0 | 0 | None | None | None | None | None | None | None |
| old rules, 1.50-1.80, legs >=1.12 | 67 | 0 | 66 | 1 | 266.0 | -734.0 | 0.31 | 353.0 | -533.0 | -699.0 | 3.7 |
| new rules, 1.50-1.80, legs >=1.12 | 0 | 0 | 0 | 0 | None | None | None | None | None | None | None |
| new rules, 1.50-1.80, legs >=1.08 | 2 | 0 | 1 | 1 | 0.0 | -1000.0 | 35.41 | 7351.0 | 19239.0 | -278.79999999999995 | 12.7 |
| new rules, 1.50-1.80, legs >=1.05 [new default] | 2 | 0 | 1 | 1 | 0.0 | -1000.0 | 34.69 | 7060.0 | 18642.0 | -281.6 | 12.6 |
| new rules, 1.50-1.80, legs >=1.03 | 2 | 0 | 1 | 1 | 0.0 | -1000.0 | 34.69 | 7060.0 | 18642.0 | -281.6 | 12.6 |
| what-if: new rules, 1.50-1.80, legs >=1.05, up to 6 legs | 17 | 0 | 16 | 1 | 198.0 | -802.0 | 0.09 | 270.0 | -699.0 | -806.1 | 2.9 |

_Estimates, not guarantees. Past hit rates don't promise future ones._