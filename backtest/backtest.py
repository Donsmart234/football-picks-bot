"""Backtest + tuning of the prediction model (writes ../model_params.json and backtest/REPORT.md).

Data (downloaded by backtest/fetch.py into DATA_DIR):
  * results: free Sportradar feed (the one SportyBet's widgets use), stats_season_fixtures2/<season>/1, 15 leagues,
    seasons 24/25, 25/26 and 26/27 so far (FT + HT scores, team uids = SportyBet competitor ids)
  * pre-match prices: football-data.co.uk market-average 1X2 and O/U 2.5 odds (AvgH/AvgD/AvgA, Avg>2.5/Avg<2.5),
    joined to the Sportradar results by league + date (±1 day) + team names + score. Historical SportyBet prices are
    not available, so the market average stands in for SportyBet's (both are Betradar-style books; de-margined they
    agree within ~1 pt). Lines without a historical price (O/U 0.5/1.5/3.5/4.5, BTTS, team totals, 1st half, DNB,
    DC) use the Poisson fit to the 1X2 + O/U 2.5 prices as their "market" part, exactly as the scanner computes p_fit.
  * team stats as of kickoff: each team's last 10 games before the match inside this dataset (all 15 leagues, so
    promoted/relegated sides keep their history). Production uses the last 10 games in all competitions.

Split: train = 24/25 + 25/26 (tuning, weights, calibration), test = 26/27 so far (out-of-sample, reported).

    python backtest/backtest.py [DATA_DIR]
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import model  # noqa: E402
from markets import FAMILIES, Grid, model_prob  # noqa: E402
from predictor import stats_lambdas  # noqa: E402
from srstats import H2H, Game, MatchForm, TeamForm  # noqa: E402

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "work" / "backtest_data"
OUT_PARAMS = ROOT / "model_params.json"
OUT_REPORT = ROOT / "backtest" / "REPORT.md"
OUT_JSON = ROOT / "backtest" / "metrics.json"
SAFE_ODDS = (1.05, 1.35)
MARGIN = 1.06        # price stand-in for lines with no historical odds: 1 / (p_market * MARGIN)


# ------------------------------------------------------------------ load + join
def norm(s: str) -> str:
    s = s.lower()
    for a, b in (("utd", "united"), ("man ", "manchester "), ("nott'm", "nottingham"), ("ath ", "athletic "),
                 ("sp ", "sporting "), ("st ", "saint "), ("fc ", ""), (" fc", ""), ("ü", "u"), ("ö", "o"),
                 ("é", "e"), ("ç", "c"), ("ş", "s"), ("ğ", "g"), ("ı", "i"), ("á", "a"), ("í", "i"), ("ó", "o")):
        s = s.replace(a, b)
    return s.strip()


def fnum(row, *keys):
    for k in keys:
        try:
            v = float(row.get(k) or "")
            if v > 1.0:
                return v
        except ValueError:
            pass
    return None


def load():
    seasons = json.loads((DATA / "seasons.json").read_text())
    matches = []
    join_stats = defaultdict(lambda: [0, 0])
    for ut, v in seasons.items():
        for idx, (sid, sname) in enumerate(v["seasons"]):
            gis = json.loads((DATA / f"gismo_{ut}_{sid}.json").read_text())
            yr = {0: "2627", 1: "2526", 2: "2425"}[idx]
            fd = []
            with open(DATA / f"fd_{v['code']}_{yr}.csv", encoding="utf-8-sig", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    if not r.get("HomeTeam") or not r.get("Date"):
                        continue
                    d = r["Date"]
                    try:
                        dt = datetime.strptime(d, "%d/%m/%Y" if len(d.split("/")[-1]) == 4 else "%d/%m/%y")
                    except ValueError:
                        continue
                    fd.append({"day": dt.date().toordinal(), "home": norm(r["HomeTeam"]), "away": norm(r["AwayTeam"]),
                               "fh": int(r["FTHG"]) if r.get("FTHG", "").isdigit() else None,
                               "fa": int(r["FTAG"]) if r.get("FTAG", "").isdigit() else None,
                               "o1": fnum(r, "AvgH", "B365H"), "ox": fnum(r, "AvgD", "B365D"),
                               "o2": fnum(r, "AvgA", "B365A"), "oo": fnum(r, "Avg>2.5", "B365>2.5"),
                               "ou": fnum(r, "Avg<2.5", "B365<2.5")})
            by_day = defaultdict(list)
            for r in fd:
                by_day[r["day"]].append(r)
            for m in gis:
                if m["fh"] is None:
                    continue
                join_stats[v["code"]][0] += 1
                day = datetime.fromtimestamp(m["uts"], tz=timezone.utc).date().toordinal()
                best, bs = None, 0
                for dd in (day, day - 1, day + 1):
                    for r in by_day.get(dd, []):
                        s = fuzz.token_set_ratio(norm(m["home"]), r["home"]) + fuzz.token_set_ratio(norm(m["away"]), r["away"])
                        if r["fh"] == m["fh"] and r["fa"] == m["fa"]:
                            s += 30
                        if s > bs:
                            best, bs = r, s
                odds = None
                if best and bs >= 150 and best["fh"] == m["fh"] and best["fa"] == m["fa"] and best["o1"] and best["ox"] and best["o2"]:
                    odds = best
                    join_stats[v["code"]][1] += 1
                matches.append({**m, "utid": str(ut), "code": v["code"], "season_idx": idx, "odds": odds})
    matches.sort(key=lambda m: m["uts"])
    return matches, dict(join_stats)


# ------------------------------------------------------------------ features
def build_history(matches):
    """team uid -> list of (uts, Game) in time order; plus h2h lookup."""
    hist = defaultdict(list)
    for m in matches:
        hist[m["huid"]].append(m)
        hist[m["auid"]].append(m)
    return hist


def team_games(hist, uid, before_uts, n=10):
    out = []
    for m in reversed(hist[uid]):
        if m["uts"] >= before_uts:
            continue
        home = m["huid"] == uid
        out.append(Game("H" if home else "A", m["fh"] if home else m["fa"], m["fa"] if home else m["fh"],
                        (m["hh"] if home else m["ha"]), (m["ha"] if home else m["hh"]),
                        m["away"] if home else m["home"], m["uts"]))
        if len(out) >= n:
            break
    return out   # newest first


def h2h_games(hist, huid, auid, before_uts, n=10):
    out = []
    for m in reversed(hist[huid]):
        if m["uts"] >= before_uts:
            continue
        if {m["huid"], m["auid"]} == {huid, auid}:
            out.append((m["fh"], m["fa"], m["uts"]) if m["huid"] == huid else (m["fa"], m["fh"], m["uts"]))
            if len(out) >= n:
                break
    return out


# ------------------------------------------------------------------ market items (binary outcomes)
# (name, family id, specifier, outcome id, result fn(fh, fa, hh, ha) -> 1/0/None)
ITEMS = [
    ("1X2 home", "1", "", "1", lambda a, b, c, d: int(a > b)),
    ("1X2 draw", "1", "", "2", lambda a, b, c, d: int(a == b)),
    ("1X2 away", "1", "", "3", lambda a, b, c, d: int(a < b)),
    ("DC 1X", "10", "", "9", lambda a, b, c, d: int(a >= b)),
    ("DC X2", "10", "", "11", lambda a, b, c, d: int(a <= b)),
    ("DC 12", "10", "", "10", lambda a, b, c, d: int(a != b)),
    ("DNB home", "11", "", "4", lambda a, b, c, d: None if a == b else int(a > b)),
    ("DNB away", "11", "", "5", lambda a, b, c, d: None if a == b else int(a < b)),
    ("BTTS yes", "29", "", "74", lambda a, b, c, d: int(a > 0 and b > 0)),
    ("BTTS no", "29", "", "76", lambda a, b, c, d: int(not (a > 0 and b > 0))),
]
for _l in ("0.5", "1.5", "2.5", "3.5", "4.5"):
    _v = float(_l)
    ITEMS.append((f"Over {_l}", "18", f"total={_l}", "12", (lambda v: lambda a, b, c, d: int(a + b > v))(_v)))
    ITEMS.append((f"Under {_l}", "18", f"total={_l}", "13", (lambda v: lambda a, b, c, d: int(a + b < v))(_v)))
for _l in ("0.5", "1.5"):
    _v = float(_l)
    ITEMS.append((f"Home over {_l}", "19", f"total={_l}", "12", (lambda v: lambda a, b, c, d: int(a > v))(_v)))
    ITEMS.append((f"Home under {_l}", "19", f"total={_l}", "13", (lambda v: lambda a, b, c, d: int(a < v))(_v)))
    ITEMS.append((f"Away over {_l}", "20", f"total={_l}", "12", (lambda v: lambda a, b, c, d: int(b > v))(_v)))
    ITEMS.append((f"Away under {_l}", "20", f"total={_l}", "13", (lambda v: lambda a, b, c, d: int(b < v))(_v)))
    ITEMS.append((f"1H over {_l}", "68", f"total={_l}", "12",
                  (lambda v: lambda a, b, c, d: None if c is None else int(c + d > v))(_v)))
    ITEMS.append((f"1H under {_l}", "68", f"total={_l}", "13",
                  (lambda v: lambda a, b, c, d: None if c is None else int(c + d < v))(_v)))

REPORT_MARKETS = {   # primary outcome reported per market
    "O/U 2.5": "Over 2.5", "BTTS": "BTTS yes", "DC 1X": "DC 1X", "DC X2": "DC X2", "DC 12": "DC 12",
    "O/U 1.5": "Over 1.5", "O/U 3.5": "Over 3.5", "O/U 4.5": "Over 4.5", "O/U 0.5": "Over 0.5",
    "Home O/U 0.5": "Home over 0.5", "Away O/U 0.5": "Away over 0.5", "Home O/U 1.5": "Home over 1.5",
    "Away O/U 1.5": "Away over 1.5", "1H O/U 0.5": "1H over 0.5", "1H O/U 1.5": "1H over 1.5",
    "DNB home": "DNB home",
}


# ------------------------------------------------------------------ fast numpy Poisson fit (identical grid to scanner.fit_lambdas)
_K = np.arange(11)
_LF = np.array([math.lgamma(k + 1) for k in _K])


def _pmf_np(lam):
    lam = np.asarray(lam, dtype=float)[..., None]
    return np.exp(-lam + _K * np.log(lam) - _LF)


def _fit_errs(cands, t):
    lh, la = cands[:, 0], cands[:, 1]
    ph, pa = _pmf_np(lh), _pmf_np(la)
    g = ph[:, :, None] * pa[:, None, :]
    s = g.sum(axis=(1, 2))
    i, j = np.meshgrid(_K, _K, indexing="ij")
    h = (g * (i > j)).sum(axis=(1, 2)) / s
    d = (g * (i == j)).sum(axis=(1, 2)) / s
    o = (g * (i + j >= 3)).sum(axis=(1, 2)) / s
    a = 1 - h - d
    e = (h - t["h"]) ** 2 + (d - t["d"]) ** 2 + (a - t["a"]) ** 2
    if "o" in t:
        e = e + (o - t["o"]) ** 2
    return e


_COARSE = np.array([(h / 5, a / 5) for h in range(1, 21) for a in range(1, 21)])


def fit_np(t):
    e = _fit_errs(_COARSE, t)
    bh, ba = _COARSE[int(np.argmin(e))]
    fine = np.array([(bh + dh / 20, ba + da / 20) for dh in range(-4, 5) for da in range(-4, 5)
                     if bh + dh / 20 > 0.05 and ba + da / 20 > 0.05])
    e2 = _fit_errs(fine, t)
    return tuple(fine[int(np.argmin(e2))])


def devig(odds):
    inv = [1 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


# ------------------------------------------------------------------ per-match probabilities
def market_parts(m):
    """De-margined market probs and the Poisson fit (scanner's p_fit) for every ITEM."""
    o = m["odds"]
    h, d, a = devig([o["o1"], o["ox"], o["o2"]])
    t = {"h": h, "d": d, "a": a}
    if o["oo"] and o["ou"]:
        t["o"] = devig([o["oo"], o["ou"]])[0]
    lam = fit_np(t)
    priced = {("1", "1"): (h, o["o1"]), ("1", "2"): (d, o["ox"]), ("1", "3"): (a, o["o2"])}
    if "o" in t:
        priced[("18", "total=2.5", "12")] = (t["o"], o["oo"])
        priced[("18", "total=2.5", "13")] = (1 - t["o"], o["ou"])
    return lam, priced


def item_probs(m, lam_fit, priced, lam_old, lam_new, rho):
    """For each ITEM: dict with pm (market consensus), odds, old_est, old_card, ps_new."""
    out = {}
    gc_fit, gc_old, gc_new = {}, {}, {}
    for name, fid, spec, oid, fn in ITEMS:
        fam = FAMILIES[fid]
        y = fn(m["fh"], m["fa"], m["hh"], m["ha"])
        if y is None:
            continue
        pfit = model_prob(fam, spec, oid, *lam_fit, grid_cache=gc_fit)
        key = (fid, oid) if fid == "1" else (fid, spec, oid)
        if key in priced:
            pmk, odds = priced[key]
            base = (0.55 * pmk + 0.30 * pfit) / 0.85      # scanner: devig + fit (feed prob not available)
        else:
            base, odds = pfit, None
            pmk = pfit
        if odds is None:
            odds = 1 / (max(base, 1e-3) * MARGIN)
        # current production (scanner.apply_stats v1)
        if lam_old:
            ps_old = model_prob(fam, spec, oid, *lam_old, grid_cache=gc_old)
            old_est = base + max(-0.08, min(0.04, 0.35 * (ps_old - base)))
            old_card = 0.35 * ps_old + 0.65 * (pmk if key in priced else pfit)
        else:
            ps_old, old_est, old_card = None, base - 0.02, 0.5 * pfit + 0.5 * (pmk if key in priced else pfit)
        ps_new = model_prob(fam, spec, oid, *lam_new, grid_cache=gc_new, rho=rho) if lam_new else None
        out[name] = {"y": y, "base": base, "odds": odds, "old_est": old_est, "old_card": old_card,
                     "ps_old": ps_old, "ps_new": ps_new, "kind": fam.kind, "period": fam.period,
                     "group": model.group_of(fam.kind, fam.period)}
    return out


# ------------------------------------------------------------------ metrics
def ll(p, y):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(p, y):
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def ece(p, y, bins=10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    e = 0.0
    for b in range(bins):
        s = idx == b
        if s.any():
            e += s.mean() * abs(p[s].mean() - y[s].mean())
    return float(e)


def cal_table(p, y, edges=(0, .5, .6, .7, .75, .8, .85, .9, .95, 1.0001)):
    p, y = np.asarray(p, float), np.asarray(y, float)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = (p >= lo) & (p < hi)
        if s.sum():
            rows.append({"bin": f"{lo:.2f}-{min(hi, 1):.2f}", "n": int(s.sum()), "mean_est": round(float(p[s].mean()), 3),
                         "hit": round(float(y[s].mean()), 3)})
    return rows


# ------------------------------------------------------------------ calibration fitting
def pav(x, y, min_block=150):
    o = np.argsort(x)
    x, y = np.asarray(x, float)[o], np.asarray(y, float)[o]
    blocks = []   # [sum_y, n, sum_x]
    for xi, yi in zip(x, y):
        blocks.append([yi, 1, xi])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            a = blocks.pop()
            blocks[-1] = [blocks[-1][0] + a[0], blocks[-1][1] + a[1], blocks[-1][2] + a[2]]
    # merge small blocks with a neighbour (keeps monotonicity)
    merged = True
    while merged and len(blocks) > 1:
        merged = False
        for i, b in enumerate(blocks):
            if b[1] < min_block:
                j = i + 1 if i + 1 < len(blocks) else i - 1
                a, c = sorted((i, j))
                blocks[a] = [blocks[a][0] + blocks[c][0], blocks[a][1] + blocks[c][1], blocks[a][2] + blocks[c][2]]
                blocks.pop(c)
                merged = True
                break
    xs = [b[2] / b[1] for b in blocks]
    ys = [min(0.995, max(0.005, b[0] / b[1])) for b in blocks]
    for i in range(1, len(ys)):          # monotone after clipping
        ys[i] = max(ys[i], ys[i - 1])
    return {"kind": "isotonic", "x": [round(v, 5) for v in xs], "y": [round(v, 5) for v in ys]}


def platt(x, y, iters=50):
    z = np.array([model.logit(v) for v in x])
    y = np.asarray(y, float)
    a, b = 1.0, 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(a * z + b)))
        w = p * (1 - p) + 1e-9
        ga, gb = np.sum((p - y) * z), np.sum(p - y)
        haa, hab, hbb = np.sum(w * z * z), np.sum(w * z), np.sum(w)
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-9:
            break
    return {"kind": "platt", "a": round(float(a), 5), "b": round(float(b), 5)}


def apply_cal(c, x):
    if c is None:
        return np.asarray(x, float)
    prm = {"calibration": {"g": c}}
    return np.array([model.calibrate(v, "g", prm) for v in x])


def choose_cal(x, y, seed=7):
    """5-fold CV log loss: identity vs Platt vs isotonic. Returns (choice, cv scores)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    rng = np.random.default_rng(seed)
    fold = rng.integers(0, 5, len(x))
    sc = {"identity": [], "platt": [], "isotonic": []}
    for f in range(5):
        tr, te = fold != f, fold == f
        sc["identity"].append(ll(x[te], y[te]))
        sc["platt"].append(ll(apply_cal(platt(x[tr], y[tr]), x[te]), y[te]))
        sc["isotonic"].append(ll(apply_cal(pav(x[tr], y[tr]), x[te]), y[te]))
    mean = {k: float(np.mean(v)) for k, v in sc.items()}
    best = min(mean, key=mean.get)
    if best == "isotonic" and mean["platt"] - mean["isotonic"] < 0.001:   # prefer the smooth map unless clearly better
        best = "platt"
    if best != "identity" and mean["identity"] - mean[best] < 0.0002:   # not worth it
        best = "identity"
    fit = None if best == "identity" else (platt(x, y) if best == "platt" else pav(x, y))
    return best, fit, mean


# ------------------------------------------------------------------ main
def main():
    t0 = time.time()
    matches, joins = load()
    hist = build_history(matches)
    print(f"loaded {len(matches)} results; odds joined: " + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in joins.items()))

    # league goal averages from training seasons only
    lg = defaultdict(lambda: [0, 0, 0])
    for m in matches:
        if m["season_idx"] >= 1:
            lg[m["utid"]][0] += m["fh"]
            lg[m["utid"]][1] += m["fa"]
            lg[m["utid"]][2] += 1
    leagues = {u: {"home": round(v[0] / v[2], 3), "away": round(v[1] / v[2], 3), "n": v[2]} for u, v in lg.items()}
    tot = [sum(v[i] for v in lg.values()) for i in range(3)]
    default_league = {"home": round(tot[0] / tot[2], 3), "away": round(tot[1] / tot[2], 3)}

    # usable matches: odds joined, both teams with >= 4 earlier games in the dataset
    rows = []
    for m in matches:
        if not m["odds"]:
            continue
        hg, ag = team_games(hist, m["huid"], m["uts"]), team_games(hist, m["auid"], m["uts"])
        if len(hg) < 4 or len(ag) < 4:
            continue
        rows.append((m, hg, ag))
    print(f"usable matches: {len(rows)} (train {sum(r[0]['season_idx'] >= 1 for r in rows)}, "
          f"test {sum(r[0]['season_idx'] == 0 for r in rows)})")

    # market parts + old stats lambdas (current production)
    pre = []
    for m, hg, ag in rows:
        lam_fit, priced = market_parts(m)
        hf, af = TeamForm(str(m["huid"]), m["home"], hg), TeamForm(str(m["auid"]), m["away"], ag)
        mf = MatchForm(hf, af, H2H(h2h_games(hist, m["huid"], m["auid"], m["uts"])))
        ms = mf.to_match_stats()
        lam_old = stats_lambdas(ms) if ms else None
        pre.append((m, hg, ag, lam_fit, priced, lam_old))
    print(f"market fits done {time.time() - t0:.0f}s")

    train = [p for p in pre if p[0]["season_idx"] >= 1]
    test = [p for p in pre if p[0]["season_idx"] == 0]

    # ---- 1) tune stats-model params on train (stats-only log loss on 1X2 + O/U 2.5 + BTTS)
    def stats_score(params, data):
        tot_ll, n = 0.0, 0
        for m, hg, ag, *_ in data:
            lam = model.stats_lambdas_v2(hg, ag, m["utid"], m["uts"], params)
            if not lam:
                continue
            g = Grid(*lam, params["rho"])
            ph = g.prob(lambda i, j: i > j)
            pd = g.prob(lambda i, j: i == j)
            pa = 1 - ph - pd
            po = g.prob(lambda i, j: i + j > 2.5)
            pb = g.prob(lambda i, j: i > 0 and j > 0)
            r = ph if m["fh"] > m["fa"] else pd if m["fh"] == m["fa"] else pa
            tot_ll += -math.log(max(r, 1e-6))
            for p, y in ((po, m["fh"] + m["fa"] > 2), (pb, m["fh"] > 0 and m["fa"] > 0)):
                p = min(max(p, 1e-6), 1 - 1e-6)
                tot_ll += -math.log(p if y else 1 - p)
            n += 1
        return tot_ll / max(n, 1)

    base_params = {**model.DEFAULTS, "leagues": leagues, "default_league": default_league}
    sub = train[::2]          # half the training rows for the grid search (speed)
    best = None
    for hl in (45, 90, 180, 365, 100000):
        for k in (2.0, 4.0, 8.0):
            prm = {**base_params, "half_life_days": float(hl), "shrink": k, "rho": 0.0}
            s = stats_score(prm, sub)
            if best is None or s < best[0]:
                best = (s, hl, k)
    _, hl, k = best
    best_r = None
    for r in (0.0, -0.03, -0.06, -0.09, -0.12, -0.15):
        s = stats_score({**base_params, "half_life_days": float(hl), "shrink": k, "rho": r}, sub)
        if best_r is None or s < best_r[0]:
            best_r = (s, r)
    rho = best_r[1]
    params = {**base_params, "half_life_days": float(hl), "shrink": k, "rho": rho}
    old_stats_ll = None
    print(f"stats params: half-life {hl} d, shrink {k}, rho {rho} ({time.time() - t0:.0f}s)")

    # ---- 2) per-item probabilities for every match
    def items_for(data):
        out = []
        for m, hg, ag, lam_fit, priced, lam_old in data:
            lam_new = model.stats_lambdas_v2(hg, ag, m["utid"], m["uts"], params)
            out.append((m, item_probs(m, lam_fit, priced, lam_old, lam_new, rho)))
        return out

    tr_items, te_items = items_for(train), items_for(test)
    print(f"item probs done {time.time() - t0:.0f}s")

    groups = sorted({v["group"] for _, it in tr_items for v in it.values()})

    def collect(items, group=None, name=None):
        rows_ = [v for _, it in items for nm, v in it.items()
                 if (group is None or v["group"] == group) and (name is None or nm == name)]
        return rows_

    # ---- 3) blend weight per group (train)
    weights = {}
    for g in groups:
        rs = [r for r in collect(tr_items, g) if r["ps_new"] is not None]
        y = [r["y"] for r in rs]
        best_w = min(((ll([model.blend(r["base"], r["ps_new"], g, {"weights": {g: w}}) for r in rs], y), w)
                      for w in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5)))
        weights[g] = best_w[1]
    params["weights"] = weights
    params["default_weight"] = float(np.median(list(weights.values())))
    print("weights", weights)

    # ---- 4) calibration per group (train, 5-fold CV choice) + market-only maps
    calibration, cal_choice = {}, {}
    for g in groups:
        rs = collect(tr_items, g)
        raw = [model.blend(r["base"], r["ps_new"], g, params) for r in rs]
        y = [r["y"] for r in rs]
        ch, fit, sc = choose_cal(raw, y)
        cal_choice[g] = {"choice": ch, "cv_logloss": {k: round(v, 5) for k, v in sc.items()}}
        if fit:
            calibration[g] = fit
        ch2, fit2, sc2 = choose_cal([r["base"] for r in rs], y)
        cal_choice["mkt:" + g] = {"choice": ch2, "cv_logloss": {k: round(v, 5) for k, v in sc2.items()}}
        if fit2:
            calibration["mkt:" + g] = fit2
    params["calibration"] = calibration
    print("calibration", {k: v["choice"] for k, v in cal_choice.items()})

    # ---- 5) evaluate on test
    def preds(items, name_or_group, by="name"):
        rs = collect(items, name=name_or_group) if by == "name" else collect(items, group=name_or_group)
        y = np.array([r["y"] for r in rs])
        P = {
            "market": np.array([r["base"] for r in rs]),
            "current": np.array([r["old_est"] for r in rs]),
            "new_raw": np.array([model.blend(r["base"], r["ps_new"], r["group"], params) for r in rs]),
        }
        P["new"] = np.array([model.calibrate(model.blend(r["base"], r["ps_new"], r["group"], params), r["group"], params,
                                             market_only=r["ps_new"] is None) for r in rs])
        return rs, y, P

    def summarize(items):
        res = {}
        # 1X2 three-class
        ys, pc, pn, pm, pcard = [], [], [], [], []
        for m, it in items:
            if "1X2 home" not in it:
                continue
            o = [it["1X2 home"], it["1X2 draw"], it["1X2 away"]]
            ys.append([r["y"] for r in o])
            pm.append([r["base"] for r in o])
            c = [r["old_est"] for r in o]
            pc.append([v / sum(c) for v in c])
            cc = [r["old_card"] for r in o]
            pcard.append([v / sum(cc) for v in cc])
            n = [model.calibrate(model.blend(r["base"], r["ps_new"], "1x2", params), "1x2", params,
                                 market_only=r["ps_new"] is None) for r in o]
            pn.append([v / sum(n) for v in n])
        Y = np.array(ys)

        def mc(P):
            P = np.clip(np.array(P), 1e-6, 1)
            return {"brier": round(float(np.mean(np.sum((P - Y) ** 2, axis=1))), 5),
                    "logloss": round(float(-np.mean(np.sum(Y * np.log(P), axis=1))), 5)}
        res["1X2 (3-way)"] = {"n": len(Y), "market": mc(pm), "current": mc(pc), "current_card": mc(pcard), "new": mc(pn)}
        for label, nm in [("O/U 2.5", "Over 2.5"), ("BTTS", "BTTS yes"), ("DC 1X", "DC 1X"), ("DC X2", "DC X2"),
                          ("DC 12", "DC 12"), ("DNB home", "DNB home"), ("O/U 0.5", "Over 0.5"), ("O/U 1.5", "Over 1.5"),
                          ("O/U 3.5", "Over 3.5"), ("O/U 4.5", "Over 4.5"), ("Home O/U 0.5", "Home over 0.5"),
                          ("Away O/U 0.5", "Away over 0.5"), ("Home O/U 1.5", "Home over 1.5"),
                          ("Away O/U 1.5", "Away over 1.5"), ("1H O/U 0.5", "1H over 0.5"), ("1H O/U 1.5", "1H over 1.5")]:
            rs, y, P = preds(items, nm)
            res[label] = {"n": len(y), **{k: {"brier": round(brier(v, y), 5), "logloss": round(ll(v, y), 5),
                                              "ece": round(ece(v, y), 4)} for k, v in P.items()}}
        return res

    test_metrics = summarize(te_items)
    train_metrics = summarize(tr_items)

    # ---- 6) the Safe bar: legs in the Safe odds band, est >= 0.80 (every outcome of every item, both sides)
    def safe_check(items):
        out = {}
        for key in ("current", "new"):
            ps, ys, n_band = [], [], 0
            for m, it in items:
                for nm, r in it.items():
                    if not (SAFE_ODDS[0] <= r["odds"] <= SAFE_ODDS[1]):
                        continue
                    n_band += 1
                    if key == "current":
                        p = r["old_est"]
                    else:
                        p = model.calibrate(model.blend(r["base"], r["ps_new"], r["group"], params), r["group"], params,
                                            market_only=r["ps_new"] is None)
                    if p >= 0.80:
                        ps.append(p)
                        ys.append(r["y"])
            ps, ys = np.array(ps), np.array(ys)
            out[key] = {"legs_in_band": n_band, "legs_ge_80": int(len(ps)),
                        "mean_est": round(float(ps.mean()), 4) if len(ps) else None,
                        "hit_rate": round(float(ys.mean()), 4) if len(ys) else None,
                        "brier": round(brier(ps, ys), 5) if len(ps) else None,
                        "table": cal_table(ps, ys)}
        return out

    safe_test, safe_train = safe_check(te_items), safe_check(tr_items)

    # pooled calibration tables per group on test
    cal_tables = {}
    for g in groups:
        rs, y, P = preds(te_items, g, by="group")
        cal_tables[g] = {"current": cal_table(P["current"], y), "new": cal_table(P["new"], y),
                         "n": len(y)}

    params["max_shift"] = model.DEFAULTS["max_shift"]
    params["version"] = "v2-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    params["trained_on"] = {"seasons": "24/25 + 25/26", "matches": len(train), "leagues": len(leagues),
                            "tested_on": f"26/27 to {datetime.fromtimestamp(max(m['uts'] for m, *_ in test), tz=timezone.utc).date()}",
                            "test_matches": len(test)}
    params["leagues"] = {u: {"home": v["home"], "away": v["away"]} for u, v in leagues.items()}
    OUT_PARAMS.write_text(json.dumps(params, indent=1))
    metrics = {"joins": joins, "params": {k: params[k] for k in ("half_life_days", "shrink", "rho", "weights")},
               "cal_choice": cal_choice, "test": test_metrics, "train": train_metrics, "safe_test": safe_test,
               "safe_train": safe_train, "cal_tables_test": cal_tables, "trained_on": params["trained_on"]}
    OUT_JSON.write_text(json.dumps(metrics, indent=1))
    write_report(metrics)
    print(f"done in {time.time() - t0:.0f}s -> {OUT_PARAMS.name}, {OUT_REPORT.name}")


def write_report(M):
    L = []
    t = M["trained_on"]
    L.append("# Model backtest\n")
    L.append(f"Train: {t['seasons']} ({t['matches']} matches, {t['leagues']} leagues). Test (out-of-sample): "
             f"{t['tested_on']} ({t['test_matches']} matches). Results: free Sportradar feed; pre-match prices: "
             "football-data.co.uk market averages (SportyBet history isn't available). Lines without a historical price "
             "use the Poisson fit to the 1X2 + O/U 2.5 prices as their market part. Lower is better for Brier / log loss "
             "/ ECE. *current* = the scanner's leg estimate before this change (market blend, stats nudge −8/+4 pts); "
             "*market* = de-margined prices only; *new* = model v2 (time-decayed Dixon-Coles stats model, tuned "
             "logit blend, per-group calibration).\n")
    p = M["params"]
    L.append(f"Tuned on train: half-life {p['half_life_days']:.0f} days, shrinkage {p['shrink']}, Dixon-Coles rho "
             f"{p['rho']}, stats weights {p['weights']}.\n")
    L.append("## Test metrics per market\n")
    L.append("| Market | n | Brier current | Brier market | Brier new | LogLoss current | LogLoss market | LogLoss new | ECE current | ECE new |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for k, v in M["test"].items():
        if k.startswith("1X2"):
            L.append(f"| {k} | {v['n']} | {v['current']['brier']} | {v['market']['brier']} | {v['new']['brier']} | "
                     f"{v['current']['logloss']} | {v['market']['logloss']} | {v['new']['logloss']} | – | – |")
        else:
            L.append(f"| {k} | {v['n']} | {v['current']['brier']} | {v['market']['brier']} | {v['new']['brier']} | "
                     f"{v['current']['logloss']} | {v['market']['logloss']} | {v['new']['logloss']} | "
                     f"{v['current']['ece']} | {v['new']['ece']} |")
    L.append("\n## The Safe bar (legs priced 1.05–1.35 with est. ≥ 80%), test\n")
    for k in ("current", "new"):
        s = M["safe_test"][k]
        L.append(f"- **{k}**: {s['legs_ge_80']} of {s['legs_in_band']} band legs qualified; mean est "
                 f"{s['mean_est']}, actual hit rate {s['hit_rate']}")
        for r in s["table"]:
            L.append(f"  - est {r['bin']}: n {r['n']}, mean est {r['mean_est']}, hit {r['hit']}")
    L.append("\n## Calibration choice per group (5-fold CV log loss on train)\n")
    for g, v in M["cal_choice"].items():
        L.append(f"- {g}: {v['choice']} {v['cv_logloss']}")
    L.append("\n_Estimates, not guarantees. Past calibration doesn't promise future hit rates._\n")
    OUT_REPORT.write_text("\n".join(L))


if __name__ == "__main__":
    main()
