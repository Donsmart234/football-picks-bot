"""What "Safe" means (2026-11-14, after ladder code QAHE0V lost on "Mozambique Over 0.5" at est 81%).

The old Safe bar was est >= 80% where est is (by backtest weights) the calibrated, de-margined bookmaker price, and
"stats-confirmed" only meant the stats model was not more than 3 pts below the price. So an 81% leg at 1.18 was the
bookmaker's own number with no independent check, and the AFCON qualifier between two FIFA ~100-145 sides passed
"top leagues" because every national-team competition id was on the allowlist.

A Safe leg (Scanner Safe tier, /scan safe, rollover, ladder, slip edits of those) now needs ALL of:
  1. League quality: on the top-leagues allowlist (leagues.py) AND not a domestic cup (amateur / lower-tier sides play
     in them) AND, for national teams, both sides in the FIFA top ~50 (TOP_NATIONS), or a UEFA / CONMEBOL / World Cup
     competition with the leg backing a top-50 side (e.g. Germany Over 0.5 v Luxembourg). A user who names leagues /
     teams widens this (constraints layer).
  2. Two independent probabilities >= SAFE_MIN_P (85%): the market estimate (est) AND the stats model's own Poisson
     probability (p_stats, Dixon-Coles from both teams' last 10 games). Either missing -> not Safe.
  3. A market-specific stats rule on the raw last-10 record of both teams (rules below). Fewer than MIN_GAMES games in
     the feed for either team -> not Safe (missing stats never pass by default).
  4. Full match only; no unders (and BTTS No counts as an under) unless the user asked for unders.

Every check returns a reason string ("" = passes) so a dropped leg can say why.
"""
from __future__ import annotations

import os
import re
import unicodedata

import leagues
from markets import FAMILIES, spec_value

SAFE_MIN_P = float(os.environ.get("SAFE_MIN_P") or 0.85)
MIN_GAMES = 8

# FIFA men's ranking, top ~50 (approximate, late 2025 / 2026). Extra names: TOP_NATIONS_EXTRA="Bosnia,Iceland".
_TOP_NATIONS = """
spain argentina france england brazil portugal netherlands belgium croatia italy germany morocco colombia mexico
uruguay usa|united states switzerland japan senegal iran|ir iran denmark korea republic|south korea|korea ecuador
austria australia turkiye|turkey canada ukraine norway panama poland egypt wales algeria scotland serbia nigeria
paraguay hungary ivory coast|cote divoire|cote d ivoire tunisia sweden czechia|czech republic greece slovakia
venezuela cameroon romania costa rica peru uzbekistan mali chile slovenia qatar
"""
# competitions where a top-50 side backed against anyone is acceptable (opponent quality checked by the stats rule)
STRONG_NATIONAL = {16, 11, 295, 20434, 1, 27, 23755, 133, 36497}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]+", " ", s.replace("'", ""))
    return re.sub(r"\s+", " ", s).strip()


def _nations() -> set[str]:
    out: set[str] = set()
    # multi-word names are kept whole: split on whitespace only between entries that are single tokens
    raw = _TOP_NATIONS.replace("\n", " ")
    multi = ["costa rica", "ivory coast", "cote divoire", "cote d ivoire", "korea republic", "south korea",
             "united states", "czech republic", "ir iran", "saudi arabia"]
    for m in multi:
        if m in raw:
            out.add(_norm(m))
            raw = raw.replace(m, " ")
    for tok in re.split(r"[\s|]+", raw):
        if tok.strip():
            out.add(_norm(tok))
    for x in re.split(r"[,;]", os.environ.get("TOP_NATIONS_EXTRA", "")):
        if x.strip():
            out.add(_norm(x))
    return out


TOP_NATIONS = _nations()


def top_nation(name: str) -> bool:
    n = _norm(name)
    return n in TOP_NATIONS


def _get(l, k, d=None):
    return l.get(k, d) if isinstance(l, dict) else getattr(l, k, d)


def _utid(l) -> int | None:
    s = str(_get(l, "utid") or _get(l, "tournament_id") or "").rsplit(":", 1)[-1]
    return int(s) if s.isdigit() else None


def backed_side(l) -> str | None:
    """'home' / 'away' when the leg backs one team (win, DC 1X / X2, DNB, AH, that team's Over), else None."""
    fam = FAMILIES.get(str(_get(l, "market_id") or ""))
    if fam is None:
        return None
    if fam.kind == "team_total":
        return fam.side if "under" not in (_get(l, "tags") or ()) else None
    side = _get(l, "side")
    return side if side in ("home", "away") and fam.kind in ("1x2", "dc", "dnb", "ah", "eh") else None


def league_problem(l) -> str:
    """'' when the leg's competition counts as a top league for Safe; else why not."""
    home, away = _get(l, "home") or "", _get(l, "away") or ""
    if not leagues.is_top(l):
        return "not on the top-leagues list"
    ut = _utid(l)
    row = leagues.TOP_LEAGUES.get(ut) if ut is not None else None
    if not row:
        return ""                      # TOP_LEAGUES_EXTRA id the owner added on purpose
    kind = row[2]
    if kind == "cup":
        return f"domestic cup ({row[1]}): lower-tier / amateur sides"
    if kind == "national":
        ht, at = top_nation(home), top_nation(away)
        if ht and at:
            return ""
        side = backed_side(l)
        if ut in STRONG_NATIONAL and side and (ht if side == "home" else at):
            return ""
        weak = [n for n, ok in ((home, ht), (away, at)) if not ok]
        return f"national team outside the FIFA top ~50 ({', '.join(weak)})"
    return ""


def under_like(l) -> bool:
    tags = _get(l, "tags") or ()
    if "under" in tags:
        return True
    return str(_get(l, "market_id")) in ("29", "75", "95") and str(_get(l, "outcome_id")) == "76"   # BTTS No


# ---------------------------------------------------------------- stats rules
def _n(games, cond, n=None) -> int:
    g = games[:n] if n else games
    return sum(1 for x in g if cond(x))


def _strength(games) -> float:
    """Points per game + half the goal difference per game over the last 10 (a rough rank proxy)."""
    if not games:
        return 0.0
    pts = sum(3 if g.gf > g.ga else 1 if g.gf == g.ga else 0 for g in games)
    gd = sum(g.gf - g.ga for g in games)
    return pts / len(games) + 0.5 * gd / len(games)


def _avg_total(games) -> float:
    return sum(g.gf + g.ga for g in games) / len(games) if games else 0.0


def stats_problem(l, form) -> str:
    """Market-specific check on both teams' raw last-10 record. '' = passes."""
    if not form or not getattr(form, "ok", False) or not form.home or not form.away:
        return "missing team stats"
    hg, ag = list(form.home.games[:10]), list(form.away.games[:10])
    if len(hg) < MIN_GAMES or len(ag) < MIN_GAMES:
        return f"too few recent games in the stats feed ({len(hg)}/{len(ag)}, need {MIN_GAMES})"
    fam = FAMILIES.get(str(_get(l, "market_id") or ""))
    if fam is None:
        return "no stats rule for this market"
    kind, oid, spec = fam.kind, str(_get(l, "outcome_id")), _get(l, "specifier") or ""
    hn, an = form.home.name or "home", form.away.name or "away"
    side = _get(l, "side")

    def team(s):
        return (hg, ag, hn, an) if s == "home" else (ag, hg, an, hn)

    if kind == "team_total":
        line = float(spec_value(spec, "total") or "nan")
        t, o, tn, on = team(fam.side)
        if oid == "12":   # Over
            s10, s5 = _n(t, lambda g: g.gf > line), _n(t, lambda g: g.gf > line, 5)
            c10 = _n(o, lambda g: g.ga > line)
            if s10 < 8 or s5 < 4:
                return f"{tn} scored over {line:g} in only {s10}/{len(t)} (last 5: {s5}/5); need 8/10 and 4/5"
            if c10 < 6:
                return f"{on} conceded over {line:g} in only {c10}/{len(o)}; need 6/10"
            return ""
        u10 = _n(t, lambda g: g.gf < line)
        return "" if u10 >= 8 else f"{tn} stayed under {line:g} in only {u10}/{len(t)}; need 8/10"
    if kind == "total":
        line = float(spec_value(spec, "total") or "nan")
        avg = (_avg_total(hg) + _avg_total(ag)) / 2
        if oid == "12":   # Over
            need10, need5, need_avg = (9, 4, 2.0) if line < 1 else (7, 3, 2.6) if line < 2 else (7, 4, line + 1.0)
            for g_, nm in ((hg, hn), (ag, an)):
                a10, a5 = _n(g_, lambda g: g.gf + g.ga > line), _n(g_, lambda g: g.gf + g.ga > line, 5)
                if a10 < need10 or a5 < need5:
                    return (f"{nm} games over {line:g}: {a10}/{len(g_)} (last 5: {a5}/5); need {need10}/10 and "
                            f"{need5}/5")
            if avg < need_avg:
                return f"avg match goals {avg:.1f} below {need_avg:.1f}"
            return ""
        for g_, nm in ((hg, hn), (ag, an)):
            u = _n(g_, lambda g: g.gf + g.ga < line)
            if u < 8:
                return f"{nm} games under {line:g}: only {u}/{len(g_)}; need 8/10"
        return ""
    if kind == "btts":
        if oid == "74":
            for g_, nm in ((hg, hn), (ag, an)):
                b, sc = _n(g_, lambda g: g.gf > 0 and g.ga > 0), _n(g_, lambda g: g.gf > 0)
                if b < 7 or sc < 8:
                    return f"{nm}: BTTS {b}/{len(g_)}, scored in {sc}/{len(g_)}; need 7/10 and 8/10"
            return ""
        for g_, nm in ((hg, hn), (ag, an)):
            nb = _n(g_, lambda g: not (g.gf > 0 and g.ga > 0))
            if nb < 7:
                return f"{nm}: no-BTTS in only {nb}/{len(g_)}; need 7/10"
        return ""
    sh, sa = _strength(hg), _strength(ag)
    if kind == "dc":
        if oid == "10":   # 12: no draw
            d = max(_n(hg, lambda g: g.gf == g.ga), _n(ag, lambda g: g.gf == g.ga))
            if d > 2 or abs(sh - sa) < 0.5:
                return f"draw risk: up to {d}/10 draws, strength gap {abs(sh - sa):.1f} (need <=2 draws, gap >=0.5)"
            return ""
        t, o, tn, on = team(side)
        st, so = (sh, sa) if side == "home" else (sa, sh)
        l10, l5 = _n(t, lambda g: g.gf < g.ga), _n(t, lambda g: g.gf < g.ga, 5)
        if l10 > 2 or l5 > 1:
            return f"{tn} lost {l10}/10 (last 5: {l5}); need <=2 and <=1"
        if st < so + 0.3:
            return f"{tn} not clearly stronger than {on} (form strength {st:.1f} v {so:.1f}, need +0.3)"
        return ""
    if kind in ("1x2", "dnb"):
        if side not in ("home", "away"):
            return "draw picks are never Safe"
        t, o, tn, on = team(side)
        st, so = (sh, sa) if side == "home" else (sa, sh)
        w10, l5, l10 = _n(t, lambda g: g.gf > g.ga), _n(t, lambda g: g.gf < g.ga, 5), _n(t, lambda g: g.gf < g.ga)
        if kind == "1x2" and (w10 < 7 or l5 > 1 or st < so + 0.6):
            return f"{tn} won {w10}/10, lost {l5} of last 5, strength {st:.1f} v {so:.1f}; need 7/10, <=1, +0.6"
        if kind == "dnb" and (l10 > 2 or l5 > 1 or st < so + 0.3):
            return f"{tn} lost {l10}/10 ({l5} of last 5), strength {st:.1f} v {so:.1f}; need <=2, <=1, +0.3"
        return ""
    if kind == "ah":
        hcp = float(spec_value(spec, "hcp") or "nan")
        if side not in ("home", "away") or hcp != hcp:
            return "no stats rule for this handicap"
        h = hcp if side == "home" else -hcp
        t, o, tn, on = team(side)
        st, so = (sh, sa) if side == "home" else (sa, sh)
        if h > 0:
            big10, big5 = _n(t, lambda g: g.ga - g.gf > h), _n(t, lambda g: g.ga - g.gf > h, 5)
            if big10 > 1 or big5 > 0:
                return f"{tn} lost by more than {h:g} in {big10}/10 (last 5: {big5}); need <=1 and 0"
            if h >= 1.5 and st < so:
                thr = _n(o, lambda g: g.gf - g.ga > h)
                if thr > 1:
                    return f"{on} won by more than {h:g} in {thr}/10; an underdog +{h:g} needs <=1"
            return ""
        if h < 0:
            m10 = _n(t, lambda g: g.gf - g.ga > -h)
            if m10 < 8 or st < so + 0.6:
                return f"{tn} won by more than {-h:g} in {m10}/10, strength {st:.1f} v {so:.1f}; need 8/10, +0.6"
            return ""
        return "level handicap: use draw no bet"
    return "no stats rule for this market"


def leg_problem(l, form, min_p: float | None = None) -> str:
    """Context-free Safe check of one scanner leg with its match stats (stored on Leg.safe_note). League and unders
    are checked where the context is known (safe_problem)."""
    min_p = SAFE_MIN_P if min_p is None else min_p
    fam = FAMILIES.get(str(_get(l, "market_id") or ""))
    if fam is None or _get(l, "p_fit") is None:
        return "no goals model for this market (market-only estimate)"
    if fam.period != "ft":
        return "half / period market"
    if fam.kind == "eh":
        return "European handicap: not used for Safe"
    ps, est = _get(l, "p_stats"), float(_get(l, "est") or 0)
    if ps is None:
        return "missing team stats (no model probability)"
    if est + 1e-9 < min_p:
        return f"market estimate {est:.0%} below {min_p:.0%}"
    if ps + 1e-9 < min_p:
        return f"stats model {ps:.0%} below {min_p:.0%} (market says {est:.0%})"
    if _get(l, "verdict") != "confirmed":
        return f"stats verdict: {_get(l, 'verdict')}"
    return stats_problem(l, form)


def safe_problem(l, allow_unders: bool = False, widened: bool = False) -> str:
    """Full Safe check of a leg (Leg or leg dict) whose safe_note was set by apply_stats. '' = Safe."""
    note = _get(l, "safe_note", "not checked")
    if note is None:
        note = "not checked"
    if note:
        return note
    ps, est = _get(l, "p_stats"), float(_get(l, "est") or 0)      # re-checked here so a stale / edited leg can't slip by
    if ps is None or ps + 1e-9 < SAFE_MIN_P:
        return "stats model below the Safe bar" if ps is not None else "missing team stats (no model probability)"
    if est + 1e-9 < SAFE_MIN_P:
        return f"market estimate {est:.0%} below {SAFE_MIN_P:.0%}"
    if not allow_unders and under_like(l):
        return "under / BTTS No (no unders unless asked)"
    if not widened:
        lp = league_problem(l)
        if lp:
            return lp
    return ""


def is_safe(l, allow_unders: bool = False, widened: bool = False) -> bool:
    return not safe_problem(l, allow_unders, widened)


def preferred_market(l) -> bool:
    """Markets with genuinely high hit rates: double chance, match totals Over, draw no bet / win and handicaps on a
    stronger side. Single-team goal lines come after them."""
    fam = FAMILIES.get(str(_get(l, "market_id") or ""))
    return bool(fam and fam.kind in ("dc", "total", "ah", "dnb", "1x2"))
