"""Tidy Telegram (HTML) formatting shared by commands and the result tracker."""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.LOCAL_TZ)
RULE = "━━━━━━━━━━━━━━━"
FLAGS = {
    "england": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "wales": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "spain": "🇪🇸", "italy": "🇮🇹", "germany": "🇩🇪",
    "france": "🇫🇷", "netherlands": "🇳🇱", "portugal": "🇵🇹", "belgium": "🇧🇪", "turkiye": "🇹🇷", "turkey": "🇹🇷",
    "usa": "🇺🇸", "brazil": "🇧🇷", "argentina": "🇦🇷", "nigeria": "🇳🇬", "austria": "🇦🇹", "switzerland": "🇨🇭",
    "denmark": "🇩🇰", "norway": "🇳🇴", "sweden": "🇸🇪", "greece": "🇬🇷", "saudi arabia": "🇸🇦", "poland": "🇵🇱",
    "czechia": "🇨🇿", "japan": "🇯🇵", "mexico": "🇲🇽", "international clubs": "🌍", "international": "🌍",
    "croatia": "🇭🇷", "serbia": "🇷🇸", "romania": "🇷🇴", "ukraine": "🇺🇦", "russia": "🇷🇺", "ireland": "🇮🇪",
    "australia": "🇦🇺", "china": "🇨🇳", "korea republic": "🇰🇷", "egypt": "🇪🇬", "south africa": "🇿🇦",
    "morocco": "🇲🇦", "ghana": "🇬🇭", "colombia": "🇨🇴", "chile": "🇨🇱", "uruguay": "🇺🇾", "israel": "🇮🇱",
    "finland": "🇫🇮", "iceland": "🇮🇸", "hungary": "🇭🇺", "slovakia": "🇸🇰", "slovenia": "🇸🇮", "bulgaria": "🇧🇬",
    "cyprus": "🇨🇾", "qatar": "🇶🇦", "uae": "🇦🇪", "united arab emirates": "🇦🇪", "canada": "🇨🇦", "peru": "🇵🇪",
    "ecuador": "🇪🇨", "paraguay": "🇵🇾", "bolivia": "🇧🇴", "venezuela": "🇻🇪", "kenya": "🇰🇪", "tunisia": "🇹🇳",
    "algeria": "🇩🇿", "iran": "🇮🇷", "india": "🇮🇳", "indonesia": "🇮🇩", "thailand": "🇹🇭", "vietnam": "🇻🇳",
}
STATUS_ICON = {"won": "✅", "lost": "❌", "void": "➖", "check": "❔", "live": "🔴", "pending": "⏳"}


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def flag(category: str, league: str = "") -> str:
    c = (category or "").lower()
    if not c and "·" in (league or ""):
        c = league.split("·")[0].strip().lower()
    return FLAGS.get(c, "⚽")


def short_league(league: str) -> str:
    return league.split("·", 1)[1].strip() if "·" in (league or "") else (league or "")


def kick_local(l: dict) -> str:
    iso = l.get("kickoff") or l.get("start")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%a %H:%M")
        except ValueError:
            pass
    return (l.get("kickoff_local") or l.get("when") or "")[:9]


def teams(l: dict) -> str:
    if l.get("home") and l.get("away"):
        return f"{l['home']} v {l['away']}"
    return (l.get("match") or "").replace(" vs ", " v ")


def leg_lines(l: dict, i: int | None = None, why: bool = False, status: bool = False) -> str:
    head = f"{i}. " if i is not None else ""
    icon = (STATUS_ICON.get(l.get("status") or "pending", "") + " ") if status else ""
    est = f" · est {l['est']:.0%}" if isinstance(l.get("est"), (int, float)) else ""
    chk = {"confirmed": " ✓", "demoted": " ·"}.get(l.get("verdict", ""), "") if not status else ""
    score = (f" · <i>{esc(l['score'])}</i>" if status and l.get("score") else "") + (
        f" · <i>{esc(l['note'][:60])}</i>" if status and l.get("note") else "")
    out = (f"{icon}{head}<b>{esc(teams(l))}</b>\n"
           f"    {flag(l.get('category', ''), l.get('league', ''))} {esc(kick_local(l))} · "
           f"{esc(short_league(l.get('league', '')))}\n"
           f"    {esc(l.get('market', ''))}: <b>{esc(l.get('pick', ''))}</b> @ <code>{(l.get('odds') or 0):.2f}</code>"
           f"{est}{chk}{score}")
    if why and l.get("why"):
        out += f"\n    <i>{esc(l['why'][:220])}</i>"
    return out


def slip_text(sl: dict, title: str, why: bool = True) -> str:
    lines = [title, RULE]
    for i, l in enumerate(sl.get("legs") or [], 1):
        lines.append(leg_lines(l, i, why=why))
    if sl.get("legs"):
        lines.append(RULE)
        lines.append(f"<b>Total odds <code>{sl['total_odds']:.2f}</code></b> · est. hit chance "
                     f"<b>{sl['est_prob']:.0%}</b> · {len(sl['legs'])} legs")
    if sl.get("note"):
        lines.append("⚠️ " + esc(sl["note"]))
    lines.append("<i>Estimates from prices + team stats, not guarantees. 18+.</i>")
    return "\n".join(lines)


def record_text(rec: dict, title: str, limit_legs: int = 25) -> str:
    d = rec["data"]
    lines = [title]
    if d.get("code"):
        lines.append(f"Code <code>{esc(d['code'])}</code> · total odds <b>{d.get('total_odds') or 0:.2f}</b>")
    lines.append(RULE)
    for i, l in enumerate((d.get("legs") or [])[:limit_legs], 1):
        lines.append(leg_lines(l, i, status=True))
    return "\n".join(lines)
