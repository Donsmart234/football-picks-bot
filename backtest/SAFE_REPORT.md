# Safe filter backtest (2026-11-14)

Old = production until 2026-11-14: est >= 80% (calibrated de-margined market), 'confirmed' = stats model not >3 pts below it, top-leagues allowlist, no unders/halves. New = safety.py: market est AND stats model each >= 85%, market-specific last-10 record rule, missing stats out, no unders/BTTS No, league quality. Ladder = legs priced 1.12-2.15, one 2.00 slip (1.90-2.15, max 4 legs) per WAT match day; Scan Safe = legs 1.05-1.35.

Caveat: only 1X2 and O/U 2.5 have real historical prices; other lines are priced from the Poisson fit at a 6% margin. No Asian handicaps, national teams or cups in the data.

## test_2627 (1251 matches, 64 top-league match days)

| Legs | n | hit rate | mean est | mean odds |
|---|---|---|---|---|
| old_ladder | 396 | 0.8258 | 0.8209 | 1.16 |
| new_ladder | 0 | None | None | None |
| old_scan_safe | 1769 | 0.8457 | 0.843 | 1.127 |
| new_scan_safe | 120 | 0.9 | 0.8786 | 1.081 |

| 2.00 slips | slips | won | hit rate | days held | mean odds | mean legs |
|---|---|---|---|---|---|---|
| old | 14 | 7 | 0.5 | 50 | 1.917 | 4.0 |
| new_4legs | 0 | 0 | None | 64 | None | None |
| new_6legs_whatif | 0 | 0 | None | 64 | None | None |
| new_rules_at_80_whatif | 5 | 3 | 0.6 | 59 | 1.91 | 4.0 |
| new_10legs_from_1.05_whatif | 0 | 0 | None | 64 | None | None |

## all_seasons (11012 matches, 489 top-league match days)

| Legs | n | hit rate | mean est | mean odds |
|---|---|---|---|---|
| old_ladder | 2868 | 0.8264 | 0.8204 | 1.162 |
| new_ladder | 5 | 1.0 | 0.8575 | 1.136 |
| old_scan_safe | 15427 | 0.8534 | 0.8455 | 1.124 |
| new_scan_safe | 962 | 0.9106 | 0.8805 | 1.079 |

| 2.00 slips | slips | won | hit rate | days held | mean odds | mean legs |
|---|---|---|---|---|---|---|
| old | 135 | 68 | 0.5037 | 354 | 1.912 | 4.0 |
| new_4legs | 0 | 0 | None | 489 | None | None |
| new_6legs_whatif | 0 | 0 | None | 489 | None | None |
| new_rules_at_80_whatif | 42 | 18 | 0.4286 | 447 | 1.913 | 4.0 |
| new_10legs_from_1.05_whatif | 7 | 2 | 0.2857 | 482 | 1.941 | 8.14 |

_Estimates, not guarantees. Past hit rates don't promise future ones._