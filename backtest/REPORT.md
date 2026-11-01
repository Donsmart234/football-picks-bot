# Model backtest

Train: 24/25 + 25/26 (9761 matches, 15 leagues). Test (out-of-sample): 26/27 to 2026-10-29 (1251 matches). Results: free Sportradar feed; pre-match prices: football-data.co.uk market averages (SportyBet history isn't available). Lines without a historical price use the Poisson fit to the 1X2 + O/U 2.5 prices as their market part. Lower is better for Brier / log loss / ECE. *current* = the scanner's leg estimate before this change (market blend, stats nudge −8/+4 pts); *market* = de-margined prices only; *new* = model v2 (time-decayed Dixon-Coles stats model, tuned logit blend, per-group calibration).

Tuned on train: half-life 90 days, shrinkage 8.0, Dixon-Coles rho -0.12, stats weights {'1x2': 0.0, 'btts': 0.1, 'dc': 0.0, 'dnb': 0.0, 'h1_total': 0.0, 'team_total': 0.0, 'total': 0.0}.

## Test metrics per market

| Market | n | Brier current | Brier market | Brier new | LogLoss current | LogLoss market | LogLoss new | ECE current | ECE new |
|---|---|---|---|---|---|---|---|---|---|
| 1X2 (3-way) | 1251 | 0.59403 | 0.59159 | 0.59122 | 0.99617 | 0.99209 | 0.99144 | – | – |
| O/U 2.5 | 1251 | 0.23862 | 0.2365 | 0.2365 | 0.66997 | 0.66546 | 0.66546 | 0.0515 | 0.0501 |
| BTTS | 1251 | 0.24664 | 0.2441 | 0.24412 | 0.68639 | 0.68126 | 0.68127 | 0.0354 | 0.0321 |
| DC 1X | 1251 | 0.19665 | 0.19569 | 0.19569 | 0.57663 | 0.57372 | 0.57372 | 0.0405 | 0.0215 |
| DC X2 | 1251 | 0.21573 | 0.21343 | 0.21343 | 0.62039 | 0.61483 | 0.61483 | 0.0456 | 0.0443 |
| DC 12 | 1251 | 0.18292 | 0.18287 | 0.18287 | 0.55117 | 0.55107 | 0.55107 | 0.0108 | 0.0131 |
| DNB home | 945 | 0.20308 | 0.20128 | 0.201 | 0.58967 | 0.58477 | 0.58312 | 0.0458 | 0.0352 |
| O/U 0.5 | 1251 | 0.05199 | 0.05184 | 0.05184 | 0.21074 | 0.21037 | 0.21037 | 0.0094 | 0.0055 |
| O/U 1.5 | 1251 | 0.16746 | 0.16486 | 0.16486 | 0.51633 | 0.50955 | 0.50955 | 0.0266 | 0.0174 |
| O/U 3.5 | 1251 | 0.22289 | 0.22141 | 0.22141 | 0.63667 | 0.63349 | 0.63349 | 0.0439 | 0.0358 |
| O/U 4.5 | 1251 | 0.14521 | 0.14386 | 0.14386 | 0.46224 | 0.45775 | 0.45775 | 0.0222 | 0.0209 |
| Home O/U 0.5 | 1251 | 0.16292 | 0.16105 | 0.161 | 0.49967 | 0.49428 | 0.49363 | 0.0268 | 0.0212 |
| Away O/U 0.5 | 1251 | 0.18847 | 0.18727 | 0.18685 | 0.56132 | 0.55855 | 0.55752 | 0.0328 | 0.0197 |
| Home O/U 1.5 | 1251 | 0.22767 | 0.22501 | 0.22476 | 0.64679 | 0.64096 | 0.64036 | 0.0367 | 0.0305 |
| Away O/U 1.5 | 1251 | 0.22579 | 0.22377 | 0.22427 | 0.64302 | 0.63861 | 0.63989 | 0.0326 | 0.0383 |
| 1H O/U 0.5 | 1251 | 0.19373 | 0.19285 | 0.19285 | 0.57486 | 0.57278 | 0.57278 | 0.0176 | 0.024 |
| 1H O/U 1.5 | 1251 | 0.23192 | 0.23056 | 0.23056 | 0.65595 | 0.65313 | 0.65313 | 0.0189 | 0.0192 |

## The Safe bar (legs priced 1.05–1.35 with est. ≥ 80%), test

- **current**: 2351 of 8401 band legs qualified; mean est 0.8446, actual hit rate 0.8447
  - est 0.80-0.85: n 1421, mean est 0.823, hit 0.825
  - est 0.85-0.90: n 801, mean est 0.872, hit 0.871
  - est 0.90-0.95: n 129, mean est 0.909, hit 0.891
- **new**: 2840 of 8401 band legs qualified; mean est 0.8442, actual hit rate 0.8408
  - est 0.80-0.85: n 1699, mean est 0.823, hit 0.816
  - est 0.85-0.90: n 1078, mean est 0.874, hit 0.877
  - est 0.90-0.95: n 63, mean est 0.909, hit 0.905

## Calibration choice per group (5-fold CV log loss on train)

- 1x2: platt {'identity': 0.5805, 'platt': 0.58022, 'isotonic': 0.58013}
- mkt:1x2: platt {'identity': 0.5805, 'platt': 0.58022, 'isotonic': 0.58013}
- btts: identity {'identity': 0.68564, 'platt': 0.68576, 'isotonic': 0.686}
- mkt:btts: identity {'identity': 0.68578, 'platt': 0.68589, 'isotonic': 0.68616}
- dc: identity {'identity': 0.58126, 'platt': 0.58115, 'isotonic': 0.58062}
- mkt:dc: identity {'identity': 0.58126, 'platt': 0.58115, 'isotonic': 0.58064}
- dnb: platt {'identity': 0.57535, 'platt': 0.57318, 'isotonic': 0.57349}
- mkt:dnb: platt {'identity': 0.57535, 'platt': 0.57318, 'isotonic': 0.57357}
- h1_total: identity {'identity': 0.61345, 'platt': 0.61336, 'isotonic': 0.61371}
- mkt:h1_total: identity {'identity': 0.61345, 'platt': 0.61336, 'isotonic': 0.61371}
- team_total: platt {'identity': 0.58519, 'platt': 0.58488, 'isotonic': 0.58494}
- mkt:team_total: platt {'identity': 0.58519, 'platt': 0.58488, 'isotonic': 0.58491}
- total: identity {'identity': 0.48732, 'platt': 0.48727, 'isotonic': 0.48761}
- mkt:total: identity {'identity': 0.48732, 'platt': 0.48727, 'isotonic': 0.48759}

_Estimates, not guarantees. Past calibration doesn't promise future hit rates._
