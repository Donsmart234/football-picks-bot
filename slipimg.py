"""Branded slip images (PNG) in the Fra / FraPredict style.

Same structure as a bookmaker's share-slip picture (brand header, big booking code, total odds, an example bet, the
selections list) but in our own brand: dark navy + electric green, the Fra mark, "FraPredict". Extras: league tag
and estimated chance per leg, combined estimate, slip / leg status badges (won / lost / live / void / replaced),
team crests (img.sportradar.com, cached, initials badge fallback). 1-20 legs (the image grows), long names wrap /
ellipsize, unbooked slips say "Preview - not booked".

    spec = spec_from_record(rec) | spec_from_ctx(ctx) | spec_from_share(code, data) | spec_from_slip(slip)
    png  = render(spec, crests)            # pure, CPU only (~50-150 ms)
    await SlipImages(services).png_for(...) # spec + crests + render in a thread, cached

Nothing here books, saves or sends anything.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import config
from fmt import strip_emoji

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.LOCAL_TZ)
ASSETS = Path(__file__).resolve().parent / "assets"
FONT_DIR = ASSETS / "fonts"
MAX_LEGS = 20
W = 1080
PAD = 44
CREST_URL = "https://img.sportradar.com/ls/crest/medium/{uid}.png"
TELEGRAM_PHOTO_MAX = 2560          # Telegram scales photos to this longest side

# palette
BG = (8, 14, 30)
HEAD_A = (12, 24, 52)
HEAD_B = (6, 40, 58)
PANEL = (17, 27, 48)
BAR = (24, 37, 64)
LINE = (34, 48, 78)
WHITE = (240, 245, 252)
TEXT = (214, 222, 236)
MUTED = (134, 148, 176)
GREEN = (24, 230, 132)
GREEN_D = (12, 120, 74)
RED = (255, 84, 96)
AMBER = (255, 190, 70)
BLUE = (110, 170, 255)

STATUS_STYLE = {      # label, fg, bg
    "won": ("WON", (6, 30, 18), GREEN),
    "lost": ("LOST", WHITE, (200, 46, 62)),
    "live": ("LIVE", WHITE, (226, 50, 70)),
    "void": ("VOID", WHITE, (90, 100, 124)),
    "check": ("CHECK", (30, 22, 4), AMBER),
    "replaced": ("REPLACED", WHITE, (90, 100, 124)),
    "pending": ("RUNNING", (6, 30, 18), (120, 220, 170)),
}


# ------------------------------------------------------------------ fonts
_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_FILES = {"r": "Inter-Regular.ttf", "m": "Inter-SemiBold.ttf", "b": "Inter-Bold.ttf", "x": "Inter-ExtraBold.ttf"}
_FALLBACK = {"r": "DejaVuSans.ttf", "m": "DejaVuSans-Bold.ttf", "b": "DejaVuSans-Bold.ttf", "x": "DejaVuSans-Bold.ttf"}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    f = _fonts.get(key)
    if f is None:
        try:
            f = ImageFont.truetype(str(FONT_DIR / _FONT_FILES[weight]), size)
        except OSError:
            try:
                f = ImageFont.truetype(_FALLBACK[weight], size)
            except OSError:      # pragma: no cover
                f = ImageFont.load_default(size)
        _fonts[key] = f
    return f


def tw(f, s: str) -> float:
    return f.getlength(s)


def ellipsize(f, s: str, width: float) -> str:
    s = s or ""
    if tw(f, s) <= width:
        return s
    while s and tw(f, s + "…") > width:
        s = s[:-1]
    return s.rstrip() + "…"


def wrap(f, s: str, width: float, max_lines: int = 2) -> list[str]:
    """Greedy word wrap; long words are hard-split; the last line is ellipsized."""
    words = (s or "").split()
    lines: list[str] = []
    cur = ""
    i = 0
    while i < len(words):
        w = words[i]
        cand = (cur + " " + w).strip()
        if tw(f, cand) <= width:
            cur = cand
            i += 1
            continue
        if not cur:                      # one word wider than the line: hard split
            cut = len(w)
            while cut > 1 and tw(f, w[:cut]) > width:
                cut -= 1
            lines.append(w[:cut])
            words[i] = w[cut:]
            if len(lines) == max_lines:
                break
            continue
        lines.append(cur)
        cur = ""
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
        i = len(words)
    if i < len(words) and lines:
        rest = " ".join([lines[-1]] + words[i:])
        lines[-1] = ellipsize(f, rest, width)
    return lines or [""]


# ------------------------------------------------------------------ specs
def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dt(iso) -> datetime | None:
    if not iso:
        return None
    if isinstance(iso, (int, float)):
        return datetime.fromtimestamp(float(iso), timezone.utc)
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _uid(x) -> str:
    s = str(x or "").rsplit(":", 1)[-1]
    return s if s.isdigit() and len(s) <= 9 else ""


_GENERIC_CAT = ("international", "international clubs", "international youth", "world", "europe")


def _league(l: dict) -> str:
    """'Country · Competition' (country left out for international club / national-team competitions)."""
    lg = (l.get("league") or "").strip()
    cat = (l.get("category") or "").strip()
    if "·" in lg:
        cat2, lg = [p.strip() for p in lg.split("·", 1)]
        cat = cat or cat2
    if not lg:
        return cat
    if not cat or cat.lower() in _GENERIC_CAT or cat.lower() in lg.lower():
        return lg
    return f"{cat} · {lg}"


def norm_leg(l: dict) -> dict:
    """One selection row from any leg shape (scanner Leg JSON, history leg, analyzer leg, editflow compact leg)."""
    home, away = (l.get("home") or "").strip(), (l.get("away") or "").strip()
    match = (l.get("match") or "").strip()
    if not (home and away) and match:
        parts = re.split(r"\s+(?:vs\.?|v)\s+", match, maxsplit=1)
        if len(parts) == 2:
            home, away = home or parts[0], away or parts[1]
    st = (l.get("status") or "").lower()
    res = (l.get("result") or "")
    state = (l.get("state") or "").lower()
    if not st or st == "pending":
        if res in ("won", "lost", "void"):
            st = res
        elif state == "live":
            st = "live"
        elif state == "cancelled":
            st = "void"
        elif state == "unavailable":
            st = "void"
        else:
            st = st or "pending"
    early = l.get("early") or {}
    minute = ""
    if st == "live":
        m = str(l.get("minute") or "")
        minute = m.split(":")[0] + "'" if m and m.split(":")[0].isdigit() else ""
    est = _f(l.get("est"))
    if isinstance(l.get("est"), str) and l["est"].endswith("%"):
        est = _f(l["est"][:-1])
        est = est / 100 if est is not None else None
    if l.get("rated") is False:
        est = None
    home, away, match = strip_emoji(home), strip_emoji(away), strip_emoji(match)   # text only on the image
    return {
        "pick": strip_emoji((l.get("pick") or l.get("label") or "").strip()) or "Selection",
        "odds": _f(l.get("odds")) or _f(l.get("booked_odds")),
        "home": home, "away": away, "match": match,
        "home_uid": str(l.get("home_uid") or _uid(l.get("home_id")) or ""),
        "away_uid": str(l.get("away_uid") or _uid(l.get("away_id")) or ""),
        "market": strip_emoji((l.get("market") or "").strip()),
        "league": strip_emoji(_league(l)),
        "kickoff": _dt(l.get("kickoff") or l.get("start")),
        "est": est if est is not None and 0 < est <= 1 else None,
        "status": st if st in STATUS_STYLE else "pending",
        "score": str(l.get("score") or "").replace(":", "-"),
        "minute": minute,
        "early": early.get("min") if isinstance(early, dict) else None,
        "unavailable": state == "unavailable",
    }


def slip_status(legs: list[dict], record_status: str = "") -> str:
    rs = (record_status or "").lower()
    if rs in ("won", "lost", "void", "check", "replaced"):
        return rs
    sts = [l["status"] for l in legs]
    if any(s == "lost" for s in sts):
        return "lost"
    if sts and all(s in ("won", "void") for s in sts) and any(s == "won" for s in sts):
        return "won"
    if any(s == "live" for s in sts) or (any(s == "won" for s in sts)):
        return "live"
    return "pending" if rs == "pending" else ""


def make_spec(legs, code: str = "", total_odds=None, est_prob=None, label: str = "", when=None,
              status: str = "", preview: bool | None = None, stake: float = 100.0, note: str = "") -> dict:
    nl = [norm_leg(l) for l in (legs or [])][:MAX_LEGS]
    extra = max(0, len(legs or []) - MAX_LEGS)
    priced = [l for l in nl if l["odds"] and l["status"] != "void"]
    tot = _f(total_odds)
    if not tot:
        tot = 1.0
        for l in priced:
            tot *= l["odds"]
    rated = [l for l in nl if l["est"] is not None and l["status"] not in ("void",)]
    chance = _f(est_prob)
    if chance is None and rated and len(rated) == len([l for l in nl if l["status"] != "void"]):
        chance = 1.0
        for l in rated:
            chance *= l["est"]
    code = (code or "").strip().upper()
    return {
        "code": code, "preview": (not code) if preview is None else bool(preview),
        "total_odds": round(tot, 2) if priced else None, "est_prob": chance,
        "legs": nl, "more": extra, "label": strip_emoji((label or "").strip()),
        "when": _dt(when) or datetime.now(timezone.utc),
        "status": status or slip_status(nl), "stake": stake, "note": strip_emoji(note or ""),
        "unrated": len([l for l in nl if l["est"] is None and l["status"] != "void"]),
    }


def spec_from_record(rec: dict) -> dict:
    """A history slip record (booked code, rollover day)."""
    d = rec.get("data") or {}
    label = d.get("label") or (d.get("tier") or "").title()
    if d.get("rollover_id") and d.get("rollover_day"):
        label = f"Rollover day {d['rollover_day']}" + (f" · {float(d['target']):g}× target" if d.get("target") else "")
    legs = d.get("legs") or []
    nl = [norm_leg(l) for l in legs]
    st = slip_status(nl, rec.get("status") or "")
    eff = d.get("effective_odds") if rec.get("status") in ("won",) else None
    return make_spec(legs, d.get("code") or "", eff or d.get("total_odds"), d.get("est_prob"), label,
                     d.get("created_ts") or rec.get("created_at"), st)


def spec_from_slip(sl: dict, code: str = "") -> dict:
    """A scanner / AI / edited slip (not booked unless a code is passed)."""
    lab = sl.get("label") or ""
    if lab and not re.search(r"slip", lab, re.I):
        lab += " slip"
    if sl.get("target"):
        lab += f" · target {float(sl['target']):g}"
    return make_spec(sl.get("legs") or [], code or sl.get("code") or "", sl.get("total_odds"), sl.get("est_prob"), lab)


def spec_from_share(code: str, data: dict, label: str = "") -> dict:
    """A SportyBet share payload (/orders/share/<code>), read only."""
    from analyzer import parse_share
    legs = parse_share(data)
    return make_spec(legs, code, None, None, label or "Booking code")


def spec_from_analysis(res: dict) -> dict:
    legs = [l for l in res.get("legs") or []]
    summ = res.get("summary") or {}
    lab = "Analyzed slip" + (f" · grade {summ['grade']}" if summ.get("grade") else "")
    return make_spec(legs, res.get("code") or "", None, None, lab)


def spec_from_ctx(ctx: dict) -> dict:
    """A hydrated editflow slip context (any section)."""
    sec = ctx.get("section") or ""
    sl = ctx.get("slip") or {}
    code = ctx.get("code") or ""
    label = {"rollover_preview": "Rollover · new picks (preview)", "rollover_day": "Rollover day",
             "scan": "Scanner slip", "ai": "AI slip", "analysis": "Analyzed slip",
             "history": "Booked slip"}.get(sec, "Slip")
    if sec == "rollover_day" and ctx.get("day"):
        label = f"Rollover day {ctx['day']}" + (f" · {float(ctx['target']):g}× target" if ctx.get("target") else "")
    if sec in ("scan", "ai") and sl.get("label"):
        label = spec_from_slip({**sl, "legs": []})["label"]
    if sec == "rollover_preview":
        code = ""
    st = ctx.get("slip_status") if sec != "rollover_preview" else ""
    nl = [norm_leg(l) for l in ctx.get("legs") or []]
    return make_spec(ctx.get("legs") or [], code, sl.get("total_odds"), sl.get("est_prob"), label,
                     status=slip_status(nl, st or ""))


# ------------------------------------------------------------------ drawing helpers
def _initials(name: str) -> str:
    w = [x for x in re.split(r"[\s\-.]+", name or "") if x and x[0].isalnum()]
    if not w:
        return "?"
    if len(w) == 1:
        return w[0][:2].upper()
    skip = {"fc", "cf", "sc", "ac", "afc", "fk", "sk", "if", "bk", "cd", "ud", "sv", "vfl", "vfb", "rc", "as", "ss"}
    core = [x for x in w if x.lower() not in skip] or w
    return (core[0][0] + (core[1][0] if len(core) > 1 else core[0][1:2])).upper()


def _hue(name: str) -> tuple[int, int, int]:
    h = int(hashlib.md5((name or "?").encode()).hexdigest()[:6], 16)
    base = [(46, 92, 170), (34, 120, 110), (120, 70, 160), (150, 90, 40), (40, 110, 160), (110, 110, 50)]
    return base[h % len(base)]


def _crest(im: Image.Image, x: int, y: int, size: int, name: str, crest: Image.Image | None) -> None:
    d = ImageDraw.Draw(im)
    d.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), fill=BG)
    if crest is not None:
        d.ellipse((x, y, x + size, y + size), fill=(236, 240, 248))
        c = crest.copy()
        c.thumbnail((int(size * 0.74), int(size * 0.74)), Image.LANCZOS)
        im.paste(c, (x + (size - c.width) // 2, y + (size - c.height) // 2), c)
        return
    d.ellipse((x, y, x + size, y + size), fill=_hue(name))
    f = font("b", int(size * 0.38))
    t = _initials(name)
    d.text((x + size / 2, y + size / 2), t, font=f, fill=WHITE, anchor="mm")


def _ball(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, ring=WHITE) -> None:
    """Football icon: light disc, dark centre pentagon and seams, coloured ring (status)."""
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ring)
    ri = r * 0.84
    d.ellipse((cx - ri, cy - ri, cx + ri, cy + ri), fill=(236, 240, 248) if ring == WHITE else (236, 240, 248))
    dark = BG
    pts = [(cx + r * 0.36 * math.cos(math.radians(-90 + 72 * k)), cy + r * 0.36 * math.sin(math.radians(-90 + 72 * k)))
           for k in range(5)]
    for (px, py) in pts:
        ang = math.atan2(py - cy, px - cx)
        d.line((px, py, cx + ri * math.cos(ang), cy + ri * math.sin(ang)), fill=dark, width=max(2, int(r * 0.11)))
        ex, ey = cx + ri * 0.98 * math.cos(ang), cy + ri * 0.98 * math.sin(ang)
        d.chord((ex - r * 0.3, ey - r * 0.3, ex + r * 0.3, ey + r * 0.3), 0, 360, fill=dark)
    d.ellipse((cx - ri, cy - ri, cx + ri, cy + ri), outline=ring, width=max(2, int(r * 0.08)))
    d.polygon(pts, fill=dark)


def _check(d, cx, cy, s, fill):
    d.line((cx - s * 0.45, cy, cx - s * 0.12, cy + s * 0.33, cx + s * 0.48, cy - s * 0.35), fill=fill,
           width=max(3, int(s * 0.2)), joint="curve")


def _cross(d, cx, cy, s, fill):
    k = s * 0.34
    wd = max(3, int(s * 0.2))
    d.line((cx - k, cy - k, cx + k, cy + k), fill=fill, width=wd)
    d.line((cx - k, cy + k, cx + k, cy - k), fill=fill, width=wd)


def _badge(d, x_right: float, cy: float, status: str, text: str = "", size: int = 24) -> float:
    """Status pill right-aligned at x_right; returns its left x."""
    lab, fg, bg = STATUS_STYLE.get(status, STATUS_STYLE["pending"])
    t = text or lab
    f = font("b", size)
    icon = status in ("won", "lost", "live")
    w = tw(f, t) + size * 1.3 + (size * 1.05 if icon else 0)
    h = size * 1.7
    x0 = x_right - w
    d.rounded_rectangle((x0, cy - h / 2, x_right, cy + h / 2), radius=h / 2, fill=bg)
    tx = x0 + size * 0.65
    if icon:
        ic = tx + size * 0.4
        if status == "won":
            _check(d, ic, cy, size * 0.9, fg)
        elif status == "lost":
            _cross(d, ic, cy, size * 0.9, fg)
        else:
            d.ellipse((ic - size * 0.26, cy - size * 0.26, ic + size * 0.26, cy + size * 0.26), fill=fg)
        tx += size * 1.05
    d.text((tx, cy), t, font=f, fill=fg, anchor="lm")
    return x0


def _vgrad(w: int, h: int, a, b) -> Image.Image:
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        g.putpixel((0, y), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return g.resize((w, h))


def _hgrad(w: int, h: int, a, b) -> Image.Image:
    g = Image.new("RGB", (w, 1))
    for x in range(w):
        t = x / max(1, w - 1)
        g.putpixel((x, 0), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return g.resize((w, h))


_mark: Image.Image | None = None


def _logo(size: int) -> Image.Image | None:
    global _mark
    if _mark is None:
        try:
            _mark = Image.open(ASSETS / "fra_mark.png").convert("RGBA")
        except OSError:
            return None
    return _mark.resize((size, size), Image.LANCZOS)


def _fmt_odds(x) -> str:
    return f"{x:,.2f}" if x else "—"


def _pct(p) -> str:
    if p is None:
        return "—"
    if p < 0.01:
        return "<1%"
    return f"{p * 100:.0f}%"


def _settling(spec: dict) -> bool:
    return spec.get("status") in ("won", "lost", "live", "void", "check", "replaced")


def _chance_label(spec: dict) -> str:
    return "Pre-match est." if _settling(spec) else "Est. chance"


# ------------------------------------------------------------------ layout
F_PICK = ("b", 38)
F_ODDS = ("b", 40)
F_MATCH = ("r", 31)
F_MKT = ("r", 29)
F_TAG = ("m", 22)
F_EST = ("m", 24)


def _row_layout(l: dict) -> dict:
    """Pre-computed lines and height of one selection row."""
    x0 = PAD + 22
    odds_w = tw(font(*F_ODDS), _fmt_odds(l["odds"])) + 12
    pick_lines = wrap(font(*F_PICK), l["pick"], W - x0 - 66 - odds_w - PAD - 22, 2)
    mt = f"{l['home']} vs {l['away']}" if l["home"] and l["away"] else (l["match"] or "")
    crest_w = 0 if not (l["home"] and l["away"]) else 40 + 30 + 14
    match_lines = wrap(font(*F_MATCH), mt, W - x0 - crest_w - PAD - 22, 2)
    h = 26 + len(pick_lines) * 50 + 6 + len(match_lines) * 42 + 44 + 46 + 20
    return {"pick": pick_lines, "match": match_lines, "h": h, "crest_w": crest_w}


def render(spec: dict, crests: dict[str, Image.Image] | None = None) -> bytes:
    crests = crests or {}
    legs = spec["legs"]
    rows = [_row_layout(l) for l in legs]
    head_h = 150
    code_h = 250 + (54 if spec.get("label") else 0) + (62 if spec.get("status") else 0)
    odds_h = 150
    ex_h = 64 + 150
    sel_h = 64 + sum(r["h"] for r in rows) + (60 if spec.get("more") else 0) + (90 if not legs else 0)
    foot_h = 210 + (40 if spec.get("note") else 0)
    H = head_h + code_h + odds_h + ex_h + sel_h + foot_h
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # header bar
    im.paste(_hgrad(W, head_h, HEAD_A, HEAD_B), (0, 0))
    d.rectangle((0, head_h - 5, W, head_h), fill=GREEN)
    lg = _logo(88)
    x = PAD
    if lg is not None:
        im.paste(lg, (x, (head_h - 5 - 88) // 2), lg)
        x += 88 + 20
    fb = font("x", 46)
    cy = (head_h - 5) / 2
    d.text((x, cy), "Fra", font=fb, fill=GREEN, anchor="lm")
    x += tw(fb, "Fra")
    d.text((x, cy), "Predict", font=fb, fill=WHITE, anchor="lm")
    x += tw(fb, "Predict") + 22
    fy = cy - 13       # Nigerian flag + "Nigeria"
    d.rectangle((x, fy, x + 12, fy + 26), fill=(0, 135, 81))
    d.rectangle((x + 12, fy, x + 24, fy + 26), fill=WHITE)
    d.rectangle((x + 24, fy, x + 36, fy + 26), fill=(0, 135, 81))
    d.text((x + 46, cy), "Nigeria", font=font("r", 30), fill=TEXT, anchor="lm")
    when = spec["when"].astimezone(TZ)
    d.text((W - PAD, cy - 22), "Betslip", font=font("m", 30), fill=WHITE, anchor="rm")
    d.text((W - PAD, cy + 22), when.strftime("%d/%m/%Y %H:%M") + " WAT", font=font("r", 28), fill=TEXT, anchor="rm")

    # booking code
    y = head_h + 46
    if spec.get("label"):
        d.text((W / 2, y + 10), ellipsize(font("m", 26), spec["label"].upper(), W - 2 * PAD), font=font("m", 26),
               fill=BLUE, anchor="mm")
        y += 54
    if spec.get("code") and not spec.get("preview"):
        d.text((W / 2, y + 14), "Booking Code", font=font("r", 30), fill=TEXT, anchor="mm")
        d.text((W / 2, y + 112), spec["code"], font=font("x", 118), fill=GREEN, anchor="mm")
    else:
        d.text((W / 2, y + 14), "Booking Code", font=font("r", 30), fill=MUTED, anchor="mm")
        d.text((W / 2, y + 106), "Preview – not booked", font=font("x", 64), fill=AMBER, anchor="mm")
    y += 204
    if spec.get("status"):
        lab = STATUS_STYLE[spec["status"]][0]
        if spec["status"] == "won":
            lab = "SLIP WON"
        elif spec["status"] == "lost":
            lab = "SLIP LOST"
        elif spec["status"] == "live":
            lab = "IN PLAY"
        f = font("b", 28)
        wbadge = tw(f, lab) + 28 * 1.3 + (28 * 1.05 if spec["status"] in ("won", "lost", "live") else 0)
        _badge(d, W / 2 + wbadge / 2, y + 22, spec["status"], lab, 28)
        y += 62
    y = head_h + code_h

    # odds + chance boxes
    bw = (W - 2 * PAD - 24) // 2
    for i, (lab, val, col) in enumerate([("Odds", _fmt_odds(spec.get("total_odds")), WHITE),
                                         (_chance_label(spec), _pct(spec.get("est_prob")), GREEN)]):
        bx = PAD + i * (bw + 24)
        d.rounded_rectangle((bx, y, bx + bw, y + 112), radius=14, fill=PANEL)
        d.rectangle((bx + 10, y + 108, bx + bw - 10, y + 112), fill=GREEN if i == 0 else GREEN_D)
        d.text((bx + 28, y + 56), lab, font=font("m", 30), fill=TEXT, anchor="lm")
        vf = font("x", 54)
        v = val
        if tw(vf, v) > bw - 56 - tw(font("m", 30), lab) - 16:
            vf = font("x", 40)
        d.text((bx + bw - 28, y + 56), v, font=vf, fill=col, anchor="rm")
    y += odds_h

    # example bet
    def section(title: str, yy: int, right: str = "") -> int:
        d.rectangle((PAD, yy, W - PAD, yy + 64), fill=BAR)
        d.rectangle((PAD, yy, PAD + 6, yy + 64), fill=GREEN)
        d.text((PAD + 36, yy + 32), title, font=font("b", 30), fill=GREEN, anchor="lm")
        if right:
            d.text((W - PAD - 30, yy + 32), right, font=font("m", 26), fill=MUTED, anchor="rm")
        return yy + 64

    y = section("Example Bet", y)
    stake = float(spec.get("stake") or 100.0)
    payout = stake * spec["total_odds"] if spec.get("total_odds") else None
    d.text((PAD + 36, y + 46), "Stake", font=font("m", 34), fill=WHITE, anchor="lm")
    d.text((W - PAD - 36, y + 46), f"{stake:,.2f}", font=font("r", 34), fill=WHITE, anchor="rm")
    d.text((PAD + 36, y + 110), "Payout", font=font("m", 34), fill=WHITE, anchor="lm")
    d.text((W - PAD - 36, y + 110), f"{payout:,.2f}" if payout else "—", font=font("b", 34),
           fill=GREEN if payout else MUTED, anchor="rm")
    y += 150

    # selections
    y = section("Selections", y, f"{len(legs) + spec.get('more', 0)} leg{'s' if len(legs) + spec.get('more', 0) != 1 else ''}")
    if not legs:
        d.text((W / 2, y + 45), "No selections", font=font("r", 30), fill=MUTED, anchor="mm")
        y += 90
    for i, (l, r) in enumerate(zip(legs, rows)):
        x0 = PAD + 22
        yy = y + 26
        # ball / status icon + pick + odds
        ring = {"won": GREEN, "lost": RED, "live": (255, 120, 130), "void": MUTED}.get(l["status"], WHITE)
        _ball(d, x0 + 22, yy + 25, 22, ring)
        fp = font(*F_PICK)
        for j, pl in enumerate(r["pick"]):
            d.text((x0 + 62, yy + 25 + j * 50), pl, font=fp, fill=WHITE if l["status"] != "void" else MUTED, anchor="lm")
        d.text((W - PAD - 22, yy + 25), _fmt_odds(l["odds"]), font=font(*F_ODDS),
               fill=WHITE if l["status"] != "void" else MUTED, anchor="rm")
        yy += len(r["pick"]) * 50 + 6
        # crests + match
        mx = x0
        if r["crest_w"]:
            _crest(im, x0, yy + 1, 40, l["home"], crests.get(l["home_uid"]))
            _crest(im, x0 + 30, yy + 1, 40, l["away"], crests.get(l["away_uid"]))
            mx = x0 + r["crest_w"]
        for j, ml in enumerate(r["match"]):
            d.text((mx, yy + 21 + j * 42), ml, font=font(*F_MATCH), fill=TEXT, anchor="lm")
        yy += len(r["match"]) * 42
        # market (+ status badge right)
        right_x = W - PAD - 22
        if l["status"] != "pending":
            txt = ""
            if l["status"] == "live":
                txt = "LIVE" + (f" {l['minute']}" if l["minute"] else "") + (f" · {l['score']}" if l["score"] else "")
            elif l["status"] in ("won", "lost"):
                txt = STATUS_STYLE[l["status"]][0] + (f" · {l['score']}" if l["score"] else "")
                if l["status"] == "won" and l.get("early"):
                    txt = f"WON EARLY {l['early']}'"
            elif l["status"] == "void" and l.get("unavailable"):
                txt = "UNAVAILABLE"
            right_x = _badge(d, right_x, yy + 22, l["status"], txt, 21) - 14
        mk = l["market"] or "—"
        d.text((x0 + 22, yy + 22), ellipsize(font(*F_MKT), mk, right_x - x0 - 22), font=font(*F_MKT), fill=MUTED,
               anchor="lm")
        yy += 44
        # league tag + kickoff + estimate
        est_txt = f"est. {_pct(l['est'])}" if l["est"] is not None else "not rated"
        fe = font(*F_EST)
        ew = tw(fe, est_txt)
        bar_w = 110 if l["est"] is not None else 0
        ex = W - PAD - 22 - ew - (bar_w + 14 if bar_w else 0)
        if bar_w:
            bx = W - PAD - 22 - bar_w
            d.rounded_rectangle((bx, yy + 17, bx + bar_w, yy + 27), radius=5, fill=LINE)
            d.rounded_rectangle((bx, yy + 17, bx + max(10, int(bar_w * l["est"])), yy + 27), radius=5,
                                fill=GREEN if l["est"] >= 0.75 else AMBER if l["est"] >= 0.55 else RED)
        d.text((ex, yy + 22), est_txt, font=fe, fill=GREEN if (l["est"] or 0) >= 0.75 else TEXT if l["est"] else MUTED,
               anchor="lm")
        tx = x0
        ko = l["kickoff"].astimezone(TZ).strftime("%a %d %b %H:%M") if l["kickoff"] else ""
        ft = font(*F_TAG)
        room = ex - 20 - tx
        if ko:
            kw = tw(ft, ko)
        if l["league"]:
            lt = ellipsize(ft, l["league"], max(80, room - (kw + 20 if ko else 0) - 28))
            lw = tw(ft, lt) + 28
            d.rounded_rectangle((tx, yy + 5, tx + lw, yy + 39), radius=10, fill=BAR, outline=LINE)
            d.text((tx + 14, yy + 22), lt, font=ft, fill=TEXT, anchor="lm")
            tx += lw + 14
        if ko and tx + kw <= ex - 12:
            d.text((tx, yy + 22), ko, font=ft, fill=MUTED, anchor="lm")
        y += r["h"]
        if i < len(legs) - 1:
            d.line((PAD, y, W - PAD, y), fill=LINE, width=2)
    if spec.get("more"):
        d.text((W / 2, y + 30), f"+ {spec['more']} more selection(s) not shown", font=font("m", 28), fill=MUTED,
               anchor="mm")
        y += 60

    # footer
    y += 18
    d.rectangle((PAD, y, W - PAD, y + 3), fill=GREEN)
    y += 30
    chance = spec.get("est_prob")
    line = (f"Combined {'pre-match ' if _settling(spec) else ''}est. chance {_pct(chance)} (legs treated as independent)" if chance is not None else
            "Combined chance not estimated" + (f" ({spec['unrated']} leg(s) not rated)" if spec.get("unrated") else ""))
    d.text((PAD, y + 18), ellipsize(font("m", 26), line, W - 2 * PAD), font=font("m", 26), fill=TEXT, anchor="lm")
    y += 44
    if spec.get("note"):
        d.text((PAD, y + 18), ellipsize(font("r", 24), spec["note"], W - 2 * PAD), font=font("r", 24), fill=MUTED,
               anchor="lm")
        y += 40
    small = _logo(44)
    if small is not None:
        im.paste(small, (PAD, y + 20), small)
    d.text((PAD + 58, y + 42), "FraPredict · @FraPredictbot", font=font("b", 26), fill=WHITE, anchor="lm")
    d.text((W - PAD, y + 42), "Estimates, not guarantees · 18+", font=font("r", 24), fill=MUTED, anchor="rm")
    y += 90
    out = io.BytesIO()
    im = im.crop((0, 0, W, min(H, y + 10)))
    im.save(out, "PNG", optimize=False, compress_level=6)
    return out.getvalue()


def png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as im:
        return im.size


# ------------------------------------------------------------------ crests + service
class CrestCache:
    """Team crests from img.sportradar.com (uid = SportyBet's sr:competitor:<uid>), memory + disk cached.
    A missing crest (Sportradar answers a 1x1 gif) or any error -> None (initials badge)."""

    def __init__(self, directory: str | None = None, timeout: float = 3.0):
        self.dir = Path(directory or os.environ.get("CREST_CACHE_DIR") or (Path(config.DATA_DIR) / "crests"
                                                                           if hasattr(config, "DATA_DIR") else
                                                                           Path("data") / "crests"))
        self.timeout = timeout
        self._mem: dict[str, Image.Image | None] = {}
        self._miss: dict[str, float] = {}
        self._client = None

    def _http(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0 FraPredict"})
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @staticmethod
    def _decode(raw: bytes) -> Image.Image | None:
        try:
            im = Image.open(io.BytesIO(raw))
            if im.width < 8 or im.height < 8:
                return None
            im = im.convert("RGBA")
            im.thumbnail((96, 96), Image.LANCZOS)
            return im
        except Exception:
            return None

    async def get(self, uid: str) -> Image.Image | None:
        uid = str(uid or "")
        if not uid.isdigit():
            return None
        if uid in self._mem:
            return self._mem[uid]
        if time.time() - self._miss.get(uid, 0) < 6 * 3600:
            return None
        p = self.dir / f"{uid}.png"
        try:
            if p.is_file() and time.time() - p.stat().st_mtime < 14 * 86400:
                im = self._decode(p.read_bytes())
                self._mem[uid] = im
                return im
        except OSError:
            pass
        try:
            r = await self._http().get(CREST_URL.format(uid=uid))
            im = self._decode(r.content) if r.status_code == 200 else None
        except Exception:
            im = None
        if im is None:
            self._miss[uid] = time.time()
            return None
        self._mem[uid] = im
        if len(self._mem) > 400:
            self._mem.pop(next(iter(self._mem)))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            p.write_bytes(buf.getvalue())
        except OSError:
            pass
        return im

    async def many(self, uids, budget: float = 4.0) -> dict[str, Image.Image]:
        uids = [u for u in dict.fromkeys(str(x) for x in uids if x) if u.isdigit()][:40]
        if not uids:
            return {}
        sem = asyncio.Semaphore(8)

        async def one(u):
            async with sem:
                return u, await self.get(u)
        try:
            res = await asyncio.wait_for(asyncio.gather(*(one(u) for u in uids), return_exceptions=True), budget)
        except asyncio.TimeoutError:
            res = [(u, self._mem.get(u)) for u in uids]
        return {u: im for x in res if isinstance(x, tuple) for u, im in [x] if im is not None}


class SlipImages:
    """Build specs from the bot's slip sources and render them (cached by content, 15 min)."""
    TTL = 15 * 60

    def __init__(self, services, crests: CrestCache | None = None):
        self.s = services
        self.crests = crests or CrestCache()
        self._cache: dict[str, tuple[float, bytes, dict]] = {}
        self.renders = 0

    # ---- sources -----------------------------------------------------------
    async def spec_for_code(self, code: str) -> dict | None:
        """A code: from History when the bot booked it (results), else SportyBet's share payload (read only)."""
        code = (code or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{4,12}", code):
            return None
        tr = getattr(self.s, "tracker", None)
        try:
            rec = await tr.history.find_code(code) if tr else None
        except Exception:
            rec = None
        if rec and (rec.get("data") or {}).get("legs"):
            return spec_from_record(rec)
        bm = getattr(self.s, "bookmaker", None)
        if bm is None or not hasattr(bm, "share"):
            return None
        res = await bm.share(code)
        if not res.get("ok"):
            return None
        return spec_from_share(code, res["data"])

    async def spec_for(self, kind: str, key: str) -> dict | None:
        """kind: h = history record id, c = booking code, s = in-memory scan / AI slip id, a = analysis id,
        p = rollover preview token."""
        tr = getattr(self.s, "tracker", None)
        if kind == "h" and tr:
            rec = await tr.history.get(key)
            return spec_from_record(rec) if rec and rec.get("kind", "slip") == "slip" else None
        if kind == "c":
            return await self.spec_for_code(key)
        if kind == "s":
            ai = getattr(self.s, "ai", None)
            entry = ai.slips.get(key) if ai else None
            return spec_from_slip(entry["slip"]) if entry and entry["slip"].get("legs") else None
        if kind == "a":
            an = getattr(self.s, "analyzer", None)
            res = an.get(key) if an else None
            return spec_from_analysis(res) if res else None
        if kind == "p" and tr:
            pv = tr.preview(key)
            if pv:
                sp = spec_from_slip({**pv["res"], "label": "Rollover · new picks"})
                return sp
        return None

    # ---- render ------------------------------------------------------------
    @staticmethod
    def _key(spec: dict) -> str:
        legs = [(l["pick"], l["odds"], l["home"], l["away"], l["status"], l["score"], l["est"]) for l in spec["legs"]]
        raw = repr((spec["code"], spec["preview"], spec["total_odds"], spec["est_prob"], spec["label"], spec["status"],
                    spec["when"].strftime("%Y%m%d%H%M"), legs))
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    async def render(self, spec: dict) -> tuple[bytes, str]:
        k = self._key(spec)
        hit = self._cache.get(k)
        if hit and time.time() - hit[0] < self.TTL:
            return hit[1], k
        uids = [u for l in spec["legs"] for u in (l["home_uid"], l["away_uid"])]
        crests = await self.crests.many(uids)
        png = await asyncio.to_thread(render, spec, crests)
        self.renders += 1
        self._cache[k] = (time.time(), png, spec)
        for kk in [kk for kk, v in self._cache.items() if time.time() - v[0] > self.TTL]:
            self._cache.pop(kk, None)
        while len(self._cache) > 30:
            self._cache.pop(next(iter(self._cache)))
        return png, k

    def cached(self, key: str) -> bytes | None:
        hit = self._cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < self.TTL else None

    @staticmethod
    def filename(spec: dict) -> str:
        return f"FraPredict-{spec['code'] or 'preview'}-{spec['when'].astimezone(TZ).strftime('%Y%m%d-%H%M')}.png"

    @staticmethod
    def caption(spec: dict) -> str:
        bits = [spec["label"] if spec.get("label") else "FraPredict slip"]
        if spec.get("code") and not spec.get("preview"):
            bits.append(f"code {spec['code']}")
        else:
            bits.append("preview, not booked")
        if spec.get("total_odds"):
            bits.append(f"odds {spec['total_odds']:.2f}")
        bits.append(f"{len(spec['legs'])} legs")
        return " · ".join(bits) + "\nEstimates, not guarantees. 18+."

    async def aclose(self) -> None:
        await self.crests.aclose()


IMAGE_REQ = re.compile(
    r"\b(?:send|generate|make|create|give|show|share|render|draw|get|produce|export)\b[^.?!\n]{0,40}?"
    r"\b(?:image|picture|pic|photo|png|screenshot|jpe?g)\b"
    r"|\b(?:slip|ticket|betslip|code|booking)\s+(?:image|picture|pic|photo|png)\b"
    r"|\b(?:image|picture|pic|photo|png)\s+(?:of|for)\s+(?:this|that|the|my|it|today|code|slip|ticket|[A-Za-z0-9]{6}\b)"
    r"|\bscreenshot\s+(?:of\s+)?(?:this|that|the|my)\s+(?:slip|ticket|code|betslip)",
    re.I)


def wants_image(text: str) -> bool:
    """'send slip image', 'generate image of this slip', 'pic of this code', 'share this as an image'..."""
    t = (text or "").strip()
    if not t or len(t) > 220:
        return False
    if re.search(r"\b(?:read|analy[sz]e|check|what(?:'s| is) in|look at)\b[^.?!]{0,20}\b(?:image|photo|picture|screenshot)\b", t, re.I):
        return False
    return bool(IMAGE_REQ.search(t))
