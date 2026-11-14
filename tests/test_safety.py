"""What "Safe" means (safety.py, 2026-11-14). Offline: no network.

The Mozambique v Ethiopia case is the real pre-match record from the Sportradar feed (ladder code QAHE0V lost on
"Mozambique Over 0.5", 0-1 on 13 Nov 2026).

    python tests/test_safety.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ["SUPABASE_URL"] = ""
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.pop("SAFE_MIN_P", None)

import safety  # noqa: E402
from scanner import TIERS, Leg, Scanner, apply_stats  # noqa: E402
from srstats import H2H, Game, MatchForm, TeamForm  # noqa: E402

FAILS: list[str] = []
NOW = time.time()
DAY = 86400


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def team(name, results, venue="H"):
    """results: newest first, (gf, ga[, venue])."""
    games = []
    for i, r in enumerate(results):
        gf, ga = r[0], r[1]
        v = r[2] if len(r) > 2 else ("H" if i % 2 == 0 else "A")
        games.append(Game(v, gf, ga, None, None, f"Opp{i}", int(NOW - (i + 1) * 7 * DAY)))
    return TeamForm(uid=str(abs(hash(name)) % 10**6), name=name, games=games)


def form(h, a):
    return MatchForm(h, a, H2H())


def leg(mid, spec, oid, pick, odds, p, home="Arsenal", away="Everton", utid="17", cat="sr:category:1",
        side=None, tags=(), league="England · Premier League"):
    return Leg(event_id=f"sr:match:{abs(hash((home, away))) % 10**8}", match=f"{home} vs {away}", home=home,
               away=away, league=league, kickoff="2099-01-01T15:00:00+00:00", kickoff_local="Thu 01 Jan 16:00",
               market_id=mid, specifier=spec, outcome_id=oid, market="m", pick=pick, odds=odds, p_market=p,
               p_feed=None, p_fit=p, base=p, est=p, side=side, utid=utid, category_id=cat, tags=tuple(tags))


# real pre-match records (newest first), 13 Nov 2026
MOZ = [(4, 1, "H"), (1, 1, "H"), (0, 1, "A"), (1, 4, "A"), (0, 4, "A"), (1, 2, "H"), (3, 2, "A"), (0, 1, "A"),
       (1, 4, "A")]
ETH = [(0, 1, "H"), (0, 1, "A"), (1, 1, "H"), (1, 0, "H"), (1, 0, "H"), (3, 0, "A"), (1, 3, "A"), (1, 0, "H"),
       (0, 2, "A")]
STRONG = [(3, 1), (2, 0), (4, 2), (2, 1), (3, 0), (1, 1), (2, 0), (3, 2), (2, 1), (1, 0)]       # scores every game
LEAKY = [(1, 2), (2, 2), (0, 1), (1, 3), (2, 1), (1, 1), (0, 2), (2, 3), (1, 1), (0, 1)]        # concedes 9/10
GOALS_A = [(3, 2), (2, 2), (1, 3), (4, 1), (2, 1), (3, 3), (1, 2), (2, 2), (3, 1), (2, 3)]
GOALS_B = [(2, 2), (3, 1), (1, 2), (2, 3), (4, 2), (1, 1), (3, 2), (2, 1), (1, 3), (2, 2)]
TIGHT = [(0, 0), (1, 0), (0, 1), (1, 1), (0, 0), (2, 0), (0, 0), (1, 0), (0, 1), (1, 1)]


def mozambique():
    print("Mozambique v Ethiopia (QAHE0V)")
    f = form(team("Mozambique", MOZ), team("Ethiopia", ETH))
    l = leg("19", "total=0.5", "12", "Mozambique Over 0.5", 1.18, 0.81, home="Mozambique", away="Ethiopia",
            utid="1848", cat="sr:category:4", side="home", tags=("team_totals", "over"),
            league="International · Africa Cup of Nations Qualification")
    apply_stats(l, f)
    print("     p_stats", round(l.p_stats or 0, 3), "est", round(l.est, 3), "verdict", l.verdict, "|", l.safe_note)
    check(not safety.is_safe(l), "Mozambique Over 0.5 @1.18 is NOT Safe")
    check(l.safe_note != "", f"model / last-10 check rejects it: {l.safe_note}")
    check("FIFA top ~50" in safety.league_problem(l), f"league check: {safety.league_problem(l)}")
    check("scored over 0.5 in only 6/9" in safety.stats_problem(l, f), f"stats rule: {safety.stats_problem(l, f)}")
    # same record inside the Premier League: still not Safe (the last-10 rule, not just the league)
    l2 = leg("19", "total=0.5", "12", "Home Over 0.5", 1.18, 0.81, side="home", tags=("team_totals", "over"))
    apply_stats(l2, f)
    check(not safety.is_safe(l2), "same record in a top league: still not Safe")
    # the Scanner Safe tier drops it
    s = Scanner.__new__(Scanner)
    check(s.allowed([l, l2], "safe", None) == [], "Scanner Safe tier keeps neither")
    check(TIERS["safe"]["min_p"] == 0.85, "Safe tier bar is 85%")


def passes():
    print("legs that should pass")
    f = form(team("Arsenal", STRONG), team("Everton", LEAKY))
    l = leg("19", "total=0.5", "12", "Arsenal Over 0.5", 1.10, 0.90, side="home", tags=("team_totals", "over"))
    apply_stats(l, f)
    check(safety.is_safe(l), f"Arsenal Over 0.5 (scored 10/10, Everton conceded 9/10, model {l.p_stats:.0%}) is Safe"
          + (f" [{l.safe_note}]" if l.safe_note else ""))
    f2 = form(team("Arsenal", GOALS_A), team("Everton", GOALS_B))
    o = leg("18", "total=1.5", "12", "Over 1.5", 1.12, 0.88, tags=("totals", "over"))
    apply_stats(o, f2)
    check(safety.is_safe(o), f"match Over 1.5 between high-scoring sides is Safe (model {o.p_stats:.0%})"
          + (f" [{o.safe_note}]" if o.safe_note else ""))
    f3 = form(team("Arsenal", TIGHT), team("Everton", TIGHT))
    o2 = leg("18", "total=1.5", "12", "Over 1.5", 1.12, 0.88, tags=("totals", "over"))
    apply_stats(o2, f3)
    check(not safety.is_safe(o2), f"match Over 1.5 between low-scoring sides is not Safe ({o2.safe_note})")
    dc = leg("10", "", "9", "Arsenal or draw", 1.08, 0.91, side="home", tags=("double_chance",))
    apply_stats(dc, f)
    check(safety.stats_problem(dc, f) == "", f"DC 1X on a clearly stronger home side passes the form rule "
          f"({safety.stats_problem(dc, f)})")
    dc2 = leg("10", "", "9", "Everton or draw", 1.08, 0.91, side="home", home="Everton", away="Arsenal",
              tags=("double_chance",))
    check(safety.stats_problem(dc2, form(team("Everton", LEAKY), team("Arsenal", STRONG))) != "",
          "DC 1X on the weaker side fails the form rule")


def rules():
    print("market / league / data rules")
    f = form(team("Arsenal", STRONG), team("Everton", LEAKY))
    l = leg("19", "total=0.5", "12", "Arsenal Over 0.5", 1.10, 0.90, side="home", tags=("team_totals", "over"))
    apply_stats(l, None)
    check(not safety.is_safe(l) and "missing" in l.safe_note, f"missing stats drops the leg ({l.safe_note})")
    few = form(team("Arsenal", STRONG[:5]), team("Everton", LEAKY))
    apply_stats(l, few)
    check(not safety.is_safe(l), f"too few games drops the leg ({l.safe_note})")
    btts_no = leg("29", "", "76", "No", 1.15, 0.88, tags=("btts",))
    check(safety.under_like(btts_no), "BTTS No counts as an under")
    under = leg("18", "total=4.5", "13", "Under 4.5", 1.08, 0.92, tags=("totals", "under"))
    under.safe_note, under.p_stats = "", 0.92
    check(not safety.is_safe(under) and safety.is_safe(under, allow_unders=True),
          "unders out unless the user asked for them")
    half = leg("68", "total=0.5", "12", "1st half Over 0.5", 1.20, 0.86, tags=("totals", "over", "halves"))
    apply_stats(half, f)
    check(not safety.is_safe(half), f"half markets are never Safe ({half.safe_note})")
    cup = leg("16", "hcp=3.5", "1714", "Salamanca (+3.5)", 1.19, 0.86, home="Salamanca CF UDS", away="Celta",
              utid="329", cat="sr:category:32", side="home", tags=("handicap",), league="Spain · Copa del Rey")
    check("domestic cup" in safety.league_problem(cup), "Copa del Rey +3.5 (amateur / lower-tier side): cup rejected")
    cdf = leg("16", "hcp=3.5", "1714", "US St Philbert (+3.5)", 1.17, 0.82, home="US St Philbert de Grandlieu",
              away="La Roche Vendee Football", utid="335", cat="sr:category:7", side="home", tags=("handicap",))
    check("domestic cup" in safety.league_problem(cdf), "Coupe de France amateur tie rejected")
    aug = leg("18", "total=0.5", "12", "Over 0.5", 1.15, 0.86, home="Andorra", away="Gibraltar", utid="23755",
              cat="sr:category:4", tags=("totals", "over"))
    check("FIFA top ~50" in safety.league_problem(aug), "Andorra v Gibraltar (Nations League) rejected")
    ger = leg("19", "total=0.5", "12", "Germany Over 0.5", 1.03, 0.96, home="Germany", away="Luxembourg",
              utid="11", cat="sr:category:4", side="home", tags=("team_totals", "over"))
    check(safety.league_problem(ger) == "", "Germany Over 0.5 v Luxembourg (WC qual UEFA, backs a top-50 side) allowed")
    srb = leg("16", "hcp=3.5", "1714", "Serbia (+3.5)", 1.16, 0.83, home="Serbia", away="Germany", utid="23755",
              cat="sr:category:4", side="home", tags=("handicap",))
    check(safety.league_problem(srb) == "", "Serbia v Germany: both top 50, allowed")
    civ = leg("1", "", "1", "Cote d'Ivoire", 1.30, 0.80, home="Côte d'Ivoire", away="Nigeria", utid="1848",
              cat="sr:category:4", side="home")
    check(safety.league_problem(civ) == "", "Cote d'Ivoire v Nigeria (both top 50) allowed in AFCON qual")
    nl = leg("1", "", "1", "Home", 1.30, 0.80, utid="999999", cat="sr:category:1")
    check("top-leagues" in safety.league_problem(nl), "league off the allowlist rejected")
    weak = leg("16", "hcp=3.5", "1714", "Minnows (+3.5)", 1.17, 0.86, side="home", tags=("handicap",))
    fw = form(team("Minnows", [(0, 4), (1, 5), (0, 3), (1, 1), (0, 2), (0, 5), (1, 2), (0, 1), (2, 2), (0, 4)]),
              team("Giants", STRONG))
    check(safety.stats_problem(weak, fw) != "", f"underdog +3.5 that was thrashed recently fails "
          f"({safety.stats_problem(weak, fw)})")


def main():
    mozambique()
    passes()
    rules()
    print(f"\nFAILURES: {len(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
