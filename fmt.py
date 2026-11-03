"""Tidy Telegram (HTML) formatting shared by commands and the result tracker."""
from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import config

TZ = ZoneInfo(config.LOCAL_TZ)
RULE = "━━━━━━━━━━━━━━━"
# Plain-text status labels (no emoji anywhere in the bot: tests/test_no_emoji.py).
STATUS_LABEL = {"won": "WON", "lost": "LOST", "void": "VOID", "check": "CHECK", "live": "LIVE", "pending": "RUNNING",
                "replaced": "REPLACED", "cancelled": "CANCELLED", "no_slip": "NO SLIP", "skipped": "SKIPPED"}
STATUS_ICON = STATUS_LABEL          # old name, kept for imports

# Emoji and emoji-style pictographs (flags, dingbats, keycaps, variation selector-16). Text symbols such as
# · • – → ≥ × stay. Used to strip AI output and by the no-emoji test.
EMOJI_CLASS = ("\U0001F000-\U0001FAFF\U000E0000-\U000E007F\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u20E3"
               "\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE"
               "\u2934\u2935\u3030\u303D\u3297\u3299\u200D")
EMOJI_RE = re.compile(f"[{EMOJI_CLASS}]")
_EMO_LEAD = re.compile(f"(?m)^([ \\t>*_-]*)[{EMOJI_CLASS}]+[ \\t]*")
_EMO_ANY = re.compile(f"[ \\t]*[{EMOJI_CLASS}]+")


def strip_emoji(text: str) -> str:
    """Remove emoji from text (AI replies, anything model-written) without leaving stray spaces."""
    if not text or not EMOJI_RE.search(text):
        return text
    out = _EMO_ANY.sub("", _EMO_LEAD.sub(r"\1", text))
    return re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", out).strip()


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)



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
    est = f" · est {l['est']:.0%}" if isinstance(l.get("est"), (int, float)) else ""
    chk = {"confirmed": " · stats ok", "demoted": " · demoted"}.get(l.get("verdict", ""), "") if not status else ""
    score = (f" · <i>{esc(l['score'])}</i>" if status and l.get("score") else "") + (
        f" · <i>{esc(l['note'][:60])}</i>" if status and l.get("note") else "")
    st = (f" · <b>{STATUS_LABEL.get(l.get('status') or 'pending', 'RUNNING')}</b>") if status else ""
    out = (f"{head}<b>{esc(teams(l))}</b>{st}\n"
           f"    {esc(kick_local(l))} · {esc(l.get('league', ''))}\n"
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
        lines.append("Note: " + esc(sl["note"]))
    lines.append("<i>Estimates from prices + team stats, not guarantees. 18+.</i>")
    return "\n".join(lines)


def open_link(code: str | None, label: str = "Open in app") -> str:
    """Telegram-HTML link that opens a SportyBet booking code in the app (website fallback); '' without a code."""
    if not code:
        return ""
    from bookmakers import sportybet_share_url
    return f'<a href="{html.escape(sportybet_share_url(code), quote=True)}">{esc(label)}</a>'


def code_html(code: str | None, label: str = "Open in app") -> str:
    """The code itself (monospace: tap to copy) plus the app link."""
    if not code:
        return ""
    return f"<code>{esc(code)}</code> · {open_link(code, label)}"


def record_text(rec: dict, title: str, limit_legs: int = 25) -> str:
    d = rec["data"]
    lines = [title]
    if d.get("code"):
        lines.append(f"Code {code_html(d['code'])} · total odds <b>{d.get('total_odds') or 0:.2f}</b>")
    lines.append(RULE)
    for i, l in enumerate((d.get("legs") or [])[:limit_legs], 1):
        lines.append(leg_lines(l, i, status=True))
    return "\n".join(lines)


# ------------------------------------------------------------------ bet analyzer
def pc(x: float) -> str:
    """Percent with one decimal below 10% (1.4%), whole above (46%)."""
    return f"{x * 100:.1f}%" if x < 0.095 else f"{x:.0%}"




def analysis_leg(l: dict) -> str:
    i = l["idx"] + 1
    if l.get("unmatched"):
        io = l.get("image_odds")
        return (f"{i}. <b>{esc(l.get('match') or '?')}</b>\n    {esc(l.get('market') or '')}"
                + (": " if l.get("market") and l.get("pick") else "") + f"<b>{esc(l.get('pick') or '')}</b>"
                + (f" @ <code>{io:.2f}</code>" if io else "") + "\n    <i>Couldn't match"
                + (f" ({esc(l['unmatched_why'])})" if l.get("unmatched_why") else "") + ": not counted</i>")
    if l["state"] == "unavailable":
        return f"{i}. <i>Selection no longer available on SportyBet ({esc(l['event_id'])})</i>"
    odds = l.get("odds")
    price = f" @ <code>{odds:.2f}</code>" if odds else ""
    when = kick_local(l)
    out = (f"{i}. <b>{esc(teams(l))}</b>\n"
           f"    {esc(when)} · {esc(l.get('league', ''))}"
           + (" · <i>outside top leagues</i>" if l.get("flags") else "") + "\n"
           f"    {esc(l.get('market', ''))}: <b>{esc(l.get('pick', ''))}</b>{price}\n")
    if l["state"] in ("ended", "cancelled"):
        res = {"won": "<b>WON</b>", "lost": "<b>LOST</b>", "void": "<b>VOID</b>"}.get(l.get("result") or "", "Result pending")
        out += f"    {res}" + (f" · FT {esc(l['score'].replace(':', '-'))}" if l.get("score") and l["state"] == "ended"
                               else f" · {esc(l['score'].replace(':', '-'))}" if l.get("score") else "")
        return out
    if l["state"] == "live":
        out += (f"    <b>LIVE</b> {esc(l.get('score', '').replace(':', '-'))} {esc(l.get('minute', ''))}"
                f" · Not rated (in play)")
        return out
    if l.get("rated"):
        out += (f"    <b>{l['rating']}</b> · est {l['est']:.0%} · fair {l['fair_odds']:.2f}"
                f" · value {l['value']:+.0%}")
    else:
        out += "    Not rated"
    if l.get("reason"):
        out += f"\n    <i>{esc(l['reason'][:220])}</i>"
    if l.get("alternatives"):
        a = l["alternatives"][0]
        out += f"\n    Safer: {esc(a['pick'])} @ {a['odds']:.2f} (est {a['est']:.0%})"
    return out


def analysis_head(res: dict) -> str:
    s = res["summary"]
    g = s["grade"]
    r = s["ratings"]
    rat = " · ".join(f"{v} {k}" for k, v in r.items() if v)
    approx = (f" <i>({s['approx_legs']} not-rated leg{'s' if s['approx_legs'] > 1 else ''} at the bookmaker's "
              f"de-margined price)</i>" if s["approx_legs"] else "")
    src = (f"<code>{esc(res['code'])}</code>" if res.get("code") else "from your screenshot")
    lines = [f"<b>BET ANALYZER</b> · {src}",
             (f"Grade <b>{g}</b> · " if g else "") + esc(res["verdict"]),
             f"{s['legs']} legs · total odds <b>"
             + (f"{s['total_odds']:.2f}" if s.get("odds_known", True) else "n/a") + f"</b> · est. hit chance <b>{pc(s['chance'])}</b>"
             + approx]
    if rat:
        lines.append(rat)
    if s.get("unmatched"):
        lines.append(f"Note: {s['unmatched']} leg(s) from the screenshot couldn't be matched to SportyBet: left out of "
                     "the odds and the chance.")
    if s.get("started"):
        lines.append(f"<i>{s['started']} leg(s) started/finished: shown with their live score or result.</i>")
    return "\n".join(lines)


def analysis_suggestions(res: dict) -> str:
    sg = res["suggestions"]
    s = res["summary"]
    out = []
    for d in sg["drops"][:3]:
        out.append(f"• Drop {esc(teams(res['legs'][d['idx']]))} ({esc(d['pick'])}, {d['rating']} {d['est']:.0%}) → "
                   f"chance {pc(s['chance'])} → <b>{pc(d['chance'])}</b>, odds {d['total_odds']:.2f}")
    for w in sg["swaps"][:4]:
        out.append(f"• Swap {esc(w['match'].replace(' vs ', ' v '))}: {esc(w['from'])} → <b>{esc(w['to'])}</b> "
                   f"({w['from_est']:.0%} → {w['to_est']:.0%}) → chance <b>{pc(w['chance'])}</b>, odds {w['total_odds']:.2f}")
    imp = sg.get("improved")
    if imp:
        out.append(f"<b>Improved slip</b>: {len(imp['legs'])} legs · odds <b>{imp['total_odds']:.2f}</b> · est "
                   f"<b>{pc(imp['chance'])}</b> (was {pc(imp['base_chance'])} at {imp['base_total_odds']:.2f})"
                   + (f" · {imp['excluded_started']} started leg(s) left out" if imp.get("excluded_started") else ""))
        if imp.get("constraint_check") and not imp["constraint_check"]["ok"]:
            out.append("Note: the improved slip breaks your rules: " + esc("; ".join(imp["constraint_check"]["violations"][:2])))
    if not out:
        return ""
    return "<b>Suggestions</b>\n" + "\n".join(out)


def analysis_messages(res: dict, limit: int = 3900) -> list[str]:
    """Telegram HTML messages (split when a long slip doesn't fit in one)."""
    parts = [analysis_head(res), RULE] + [analysis_leg(l) for l in res["legs"]] + [RULE]
    sug = analysis_suggestions(res)
    if sug:
        parts.append(sug)
    if res.get("recommendation"):
        parts.append(RULE)
        parts.append(f"<b>{esc(res['recommendation']['text'])}</b>")
    parts.append("<i>Estimates from prices + team stats, not guarantees. Combined chance assumes independent legs. "
                 "Team news is context only. Not saved to your history. 18+.</i>")
    msgs, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > limit and cur:
            msgs.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + p
    if cur:
        msgs.append(cur)
    return msgs
