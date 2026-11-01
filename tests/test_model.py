"""Offline tests for model v2 (model.py + model_params.json) and its use by the scanner.

    python tests/test_model.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model  # noqa: E402
from markets import FAMILIES, Grid, model_prob  # noqa: E402
from scanner import Leg, apply_stats  # noqa: E402
from srstats import H2H, Game, MatchForm, TeamForm  # noqa: E402

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  PASS " if cond else "  FAIL ") + msg)
    FAILS += 0 if cond else 1


def main() -> None:
    p = model.PARAMS
    check(str(p.get("version", "")).startswith("v2"), f"params loaded ({p.get('version')})")
    check(len(p.get("leagues", {})) >= 10, f"league goal averages for {len(p.get('leagues', {}))} leagues")
    check(-0.3 <= p["rho"] <= 0.0 and p["half_life_days"] > 0 and p["shrink"] > 0, "stats params in range")
    # calibration maps are monotone and stay in (0, 1)
    for g in sorted(p.get("calibration", {})):
        xs = [i / 100 for i in range(1, 100)]
        ys = [model.calibrate(x, g.split(":")[-1], market_only=g.startswith("mkt:")) for x in xs]
        check(all(b >= a - 1e-9 for a, b in zip(ys, ys[1:])) and 0 < min(ys) and max(ys) < 1, f"calibration {g} monotone")
    check(model.calibrate(0.83, "no-such-group") == 0.83, "unknown group -> identity")
    # Dixon-Coles grid is a distribution and lifts 0-0
    g0, g1 = Grid(1.3, 1.1), Grid(1.3, 1.1, -0.12)
    check(abs(sum(c[2] for c in g1.cells) - 1) < 1e-9, "DC grid sums to 1")
    check(g1.prob(lambda i, j: i == 0 and j == 0) > g0.prob(lambda i, j: i == 0 and j == 0), "rho<0 lifts 0-0")
    check(abs(model_prob(FAMILIES["18"], "total=2.5", "12", 1.3, 1.1, rho=-0.12)
              + model_prob(FAMILIES["18"], "total=2.5", "13", 1.3, 1.1, rho=-0.12) - 1) < 1e-9, "O/U complementary")
    # stats lambdas: stronger attack -> more goals; too few games -> None
    now = time.time()
    strong = [Game("H" if i % 2 else "A", 3, 0, None, None, "x", int(now - i * 7 * 86400)) for i in range(10)]
    weak = [Game("H" if i % 2 else "A", 0, 2, None, None, "x", int(now - i * 7 * 86400)) for i in range(10)]
    lh, la = model.stats_lambdas_v2(strong, weak, "17", now)
    check(lh > 2 * la, f"strong home vs weak away -> xG {lh:.2f}-{la:.2f}")
    check(model.stats_lambdas_v2(strong[:3], weak, "17", now) is None, "fewer than 4 games -> None")
    # the scanner's estimate is the calibrated one and Safe compares it to 0.80
    f = MatchForm(TeamForm("1", "A", strong), TeamForm("2", "B", weak), H2H())
    leg = Leg(event_id="sr:match:1", match="A vs B", home="A", away="B", league="England · Premier League",
              kickoff="2026-11-01T15:00:00+00:00", kickoff_local="", market_id="10", specifier="", outcome_id="9",
              market="Double Chance", pick="A or Draw", odds=1.12, p_market=0.86, p_feed=None,
              p_fit=0.86, base=0.86, est=0.84, side="home", utid="17")
    apply_stats(leg, f)
    raw, cal = model.estimate(0.86, leg.p_stats, "dc")
    check(abs(leg.est - cal) < 1e-9 and leg.p_raw is not None, f"leg est = calibrated blend ({leg.est:.3f}, raw {leg.p_raw:.3f})")
    leg2 = Leg(**{**leg.__dict__} if hasattr(leg, "__dict__") else {k: getattr(leg, k) for k in leg.__slots__})
    apply_stats(leg2, None)
    check(leg2.verdict == "odds only" and abs(leg2.est - (model.estimate(0.86, None, "dc")[1] - 0.02)) < 1e-9,
          "no stats -> calibrated market estimate - 2 pts")
    print("FAILURES:", FAILS)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
