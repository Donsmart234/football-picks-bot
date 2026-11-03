"""One reading of "is this match in play?" for every place that shows or settles a live match.

SportyBet event fields (factsCenter liveOrPrematchEvents / event / orders/share outcomes), seen 2026-11-01..03:
  status        0 not started · 1 in play · 2 in play, book suspended · 3 ended (unsettled) · 4 ended/closed ·
                9 betting closed (dropped from the live list) while the clock still runs: higher numbers are read
                from matchStatus only (in play words -> live, else unknown)
  matchStatus   "Not start", "H1", "HT", "H2", "Awaiting extra time", "ET1"/"1st extra", "ET HT", "ET2", "Awaiting
                penalties", "Penalties", "Ended", "AET", "AP", "Interrupted", "Suspended", "Abandoned", "Postponed",
                "Cancelled", "Start delayed" (free text; matched by words so a new spelling still lands right)
  playedSeconds "mm:ss" of the match clock; frozen once a match stops

Sportradar gismo status ids (match_info / match_timeline): 0 not started · 6 1st half · 31 halftime · 7 2nd half ·
32 awaiting extra time · 41 1st extra · 33 extra time halftime · 42 2nd extra · 34 awaiting penalties · 50 penalties
· 100 ended · 110 AET · 120 AP · 60 postponed · 61 start delayed · 70 cancelled · 80 interrupted · 81 suspended ·
90 abandoned · 91 walkover · 92 retired · 93 removed. Names are matched as a fallback for ids not listed here.

States: live | ended | void (abandoned / cancelled / walkover: bookmakers void) | postponed | interrupted (stopped,
may resume) | notstarted | unknown. Badges for the UI: LIVE, HT, FT, AET, PEN, ABD, CANC, PPD, INT.
"""
from __future__ import annotations

import re

LIVE_CAP_MIN = 150        # kickoff + this many minutes: a "live" match must prove it's still moving
FROZEN_S = 300            # ...by a clock / score / status change in the last 5 minutes
ABS_CAP_MIN = 300         # kickoff + 5 h: never shown as live unless Sportradar says it's running

_VOID = re.compile(r"abandon|cancel|walk ?over|retired|removed|coverage", re.I)
_POSTP = re.compile(r"postpon|start delayed|^delayed", re.I)
_INTER = re.compile(r"interrupt|suspend", re.I)
_PEN_DONE = re.compile(r"^ap$|after pen|pen\w*\.? ?(ended|over|done)|ended after pen|^pen(alties)? ?ft$", re.I)
_AET = re.compile(r"^aet$|after extra|after et|ended after extra", re.I)
_ENDED = re.compile(r"^ended|^ft$|full ?time|^finished|^end$|^closed$|^over$", re.I)
_INPLAY = re.compile(r"^(h1|h2|ht|1h|2h|halftime|half time|pause|et\s*\d?|et ?ht|1st|2nd|extra|awaiting|pen|penalt)", re.I)
_NOTST = re.compile(r"^not ?start|^ns$", re.I)

SR_ENDED = {100: "FT", 110: "AET", 120: "PEN"}
SR_VOID = {70, 90, 91, 92, 93}
SR_POSTP = {60, 61}
SR_INTER = {80, 81}
SR_LIVE_PHASE = {6: "1h", 31: "ht", 7: "2h"}


def _ended_badge(ms: str) -> str | None:
    s = (ms or "").strip()
    if _PEN_DONE.search(s):
        return "PEN"
    if _AET.search(s):
        return "AET"
    if _ENDED.search(s):
        return "FT"
    return None


def sb_state(status, match_status) -> tuple[str, str]:
    """(state, badge) from SportyBet's status + matchStatus. The words win over the number: a feed that still says
    status 1 next to matchStatus "Ended" is ended."""
    try:
        st = int(status if status not in (None, "") else -1)
    except (TypeError, ValueError):
        st = -1
    ms = str(match_status or "").strip()
    if _VOID.search(ms):
        return "void", "CANC" if "cancel" in ms.lower() else "ABD"
    if _POSTP.search(ms):
        return "postponed", "PPD"
    b = _ended_badge(ms)
    if b:
        return "ended", b
    if _INTER.search(ms):
        return "interrupted", "INT"
    if st in (3, 4):
        return "ended", "FT"
    ht = ms.upper() in ("HT", "HALFTIME", "HALF TIME", "PAUSE")
    if st in (1, 2):
        return "live", "HT" if ht else "LIVE"
    if st >= 5 and _INPLAY.search(ms):
        # e.g. status 9 (seen 2026-11-03 on a Bet Genius match): betting closed, markets gone, clock still running
        return "live", "HT" if ht else "LIVE"
    if st == 0:
        return "notstarted", ""
    return "unknown", ""


def sr_state(status: dict | None, timeinfo: dict | None = None) -> tuple[str, str, str]:
    """(state, badge, phase) from a Sportradar status {_id, name}. phase (live only): 1h | ht | 2h | other.
    A match whose timeinfo says it ended is never live, whatever the status id says."""
    if not status:
        return "unknown", "", ""
    try:
        sid = int(status.get("_id", -1))
    except (TypeError, ValueError):
        sid = -1
    name = str(status.get("name") or "")
    if sid in SR_ENDED:
        return "ended", SR_ENDED[sid], ""
    if sid in SR_VOID or _VOID.search(name):
        return "void", "CANC" if (sid == 70 or "cancel" in name.lower()) else "ABD", ""
    if sid in SR_POSTP or _POSTP.search(name):
        return "postponed", "PPD", ""
    if sid in SR_INTER or _INTER.search(name):
        return "interrupted", "INT", ""
    b = _ended_badge(name)
    if b:
        return "ended", b, ""
    if sid == 0 or (sid < 0 and _NOTST.search(name)):
        return "notstarted", "", ""
    if sid < 0:
        return "unknown", "", ""
    if (timeinfo or {}).get("ended"):
        return "ended", "FT", ""
    phase = SR_LIVE_PHASE.get(sid, "other")
    return "live", "HT" if phase == "ht" else "LIVE", phase


def clock_min(played) -> int | None:
    """Minutes on a SportyBet "mm:ss" match clock."""
    try:
        return int(str(played).split(":")[0]) if played and ":" in str(played) else None
    except (TypeError, ValueError):
        return None


def clock_overrun(match_status, played) -> bool:
    """True when the clock is past anything a real half can reach: 1st half > 60', 2nd half > 105', extra time >
    135'. SportyBet's own (Bet Genius) clocks keep ticking after the final whistle (seen 2026-11-03: a match off the
    live list at 90:37 still showed status 1, H2, 97:56 and counting), so a ticking clock alone never proves play."""
    m = clock_min(played)
    if m is None:
        return False
    ms = str(match_status or "").strip().lower()
    if ms in ("h1", "1h", "1st half"):
        return m >= 60
    if ms in ("h2", "2h", "2nd half"):
        return m >= 105
    if ms.startswith(("et", "1st extra", "2nd extra", "extra")):
        return m >= 135
    return False


def is_sr_id(eid: str) -> bool:
    """Betradar match ids have a Sportradar feed; SportyBet's own (Bet Genius) ids are 15-digit 1111… numbers."""
    num = str(eid or "").rsplit(":", 1)[-1]
    return num.isdigit() and len(num) <= 10
