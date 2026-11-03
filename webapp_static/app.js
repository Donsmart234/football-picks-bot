(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = (tg && tg.initData) || "";
  if (tg) {
    tg.ready(); tg.expand();
    try { tg.setHeaderColor("#070B14"); tg.setBackgroundColor("#070B14"); if (tg.setBottomBarColor) tg.setBottomBarColor("#090E19"); } catch (e) {}
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
    // keep CSS in sync with Telegram's viewport + safe areas (keyboard, fullscreen, notches)
    const root = document.documentElement.style;
    const syncVp = () => {
      try {
        if (tg.viewportStableHeight) root.setProperty("--vh-stable", tg.viewportStableHeight + "px");
        const sa = tg.safeAreaInset || {}, ca = tg.contentSafeAreaInset || {};
        ["top", "bottom", "left", "right"].forEach((k) => {
          if (sa[k] != null) root.setProperty(`--tg-safe-area-inset-${k}`, sa[k] + "px");
          if (ca[k] != null) root.setProperty(`--tg-content-safe-area-inset-${k}`, ca[k] + "px");
        });
      } catch (e) {}
    };
    syncVp();
    try { ["viewportChanged", "safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged"].forEach((ev) => tg.onEvent(ev, syncVp)); } catch (e) {}
  }
  const LOAD_URL = "https://www.sportybet.com/ng/m/code-hub/load-code";
  // SportyBet's own share link: www.sportybet.com is app-claimed (iOS universal link "Open share code", Android
  // verified App Link), so it opens the installed app on the code and the website (which loads it) otherwise.
  const codeUrl = (c) => { const k = String(c || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 16); return k ? `https://www.sportybet.com/?shareCode=${encodeURIComponent(k)}&c=ng` : LOAD_URL; };
  const SLIP_KEY = "fra_slip_v2";
  const TIERS = {
    safe: { label: "Safe", target: 5, quick: [2, 3, 5, 8, 10], rule: "Every leg est. ≥80% after the stats check · odds 1.05–1.35 · estimates, not guarantees" },
    mid: { label: "Mid-safe", target: 10, quick: [5, 10, 15, 20], rule: "Legs est. ≥70% · odds 1.30–1.70" },
    risk: { label: "Risk", target: 20, quick: [10, 20, 30, 50], rule: "Legs est. ≥55% · odds 1.60–2.30" },
    high: { label: "High risk", target: 50, quick: [20, 50, 100, 200], rule: "Value legs (est. × odds ≥ ~1) at odds 2.00+" },
    audit: { label: "Grand audit", target: null, quick: [], rule: "Every market of up to 80 matches: best leg per match, top value bets and one slip per tier" },
  };
  const $view = document.getElementById("view");
  const $toast = document.getElementById("toast");
  const $sheet = document.getElementById("sheet");
  const $sheetCard = document.getElementById("sheetCard");

  const state = {
    tab: "home", tier: "safe", target: 5, day: "today", scan: null, scanning: false, scanErr: "",
    mRange: "today", mData: {}, mStack: [], mQuery: "", pred: null, markets: null,
    live: null, liveAt: 0, liveErr: "", lm: null, lmErr: "", scroll: {},
    chat: [], aiBusy: false, aiCtx: null,
    hist: null, histErr: "", histFilter: "all", histOpen: {}, ro: null, roErr: "", roBusy: false, roForm: { target: 2, days: 7, stake: "", allowUnders: false },
    az: null, azBusy: false, azErr: "", azCode: "", azTake: null, azTakeBusy: false, allLg: (() => { try { return localStorage.getItem("fra_alllg") === "1"; } catch (e) { return false; } })(),
    ex: (() => { try { return JSON.parse(localStorage.getItem("fra_ex") || "[]"); } catch (e) { return []; } })(),
  };
  const EXCL = [["under", "No unders"], ["corners", "No corners"], ["cards", "No cards"], ["player_props", "No player props"], ["handicap", "No handicaps"], ["combo", "No combos"]];

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const f2 = (o) => (o ? Number(o).toFixed(2) : "–");
  const pct = (p, d = 0) => (p == null ? "–" : (p * 100).toFixed(d) + "%");
  let hapAt = 0;   // throttle: one buzz per tap even when a handler also asks for one
  const buzz = (fn) => { const n = Date.now(); if (n - hapAt < 70) return; hapAt = n; try { tg && tg.HapticFeedback && fn(tg.HapticFeedback); } catch (e) {} };
  const hap = (t) => buzz((h) => h.impactOccurred(t || "light"));
  const hapN = (t) => { hapAt = 0; buzz((h) => h.notificationOccurred(t || "success")); };
  const hapSel = () => buzz((h) => h.selectionChanged());
  const fmtCache = {};
  const fmt = (k, o) => fmtCache[k] || (fmtCache[k] = new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Lagos", ...o }));
  let toastT;
  function toast(m) { $toast.textContent = m; $toast.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => $toast.classList.add("hidden"), 2300); }
  function clock() { try { document.getElementById("clock").textContent = fmt("hm", { hour: "2-digit", minute: "2-digit" }).format(new Date()); } catch (e) {} }
  clock(); setInterval(clock, 20000);

  async function api(path, body) {
    const opts = { headers: { "X-Telegram-Init-Data": initData } };
    if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opts); } catch (e) { throw new Error("Network error. Check your connection and try again."); }
    let j = null; try { j = await r.json(); } catch (e) {}
    if (r.status === 403) throw new Error("Access denied. Open this app from @FraPredictbot in Telegram.");
    if (!r.ok) throw new Error((j && j.error) || `Server error (${r.status}). Try again in a minute.`);
    return j;
  }
  // ---------------------------------------------------------------- local cache (instant views, background refresh)
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem("fra_c_" + k) || "null"); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem("fra_c_" + k, JSON.stringify(v)); } catch (e) { try { localStorage.removeItem("fra_c_" + k); } catch (e2) {} } },
  };
  // GET with ETag: 304 -> our stored copy. Returns {json, fresh}
  async function apiCached(key, path) {
    const c = store.get(key);
    const opts = { headers: { "X-Telegram-Init-Data": initData } };
    if (c && c.etag) opts.headers["If-None-Match"] = c.etag;
    let r;
    try { r = await fetch(path, opts); } catch (e) { throw new Error("Network error. Check your connection and try again."); }
    if (r.status === 304 && c) { c.at = Date.now(); store.set(key, c); return c.json; }
    let j = null; try { j = await r.json(); } catch (e) {}
    if (r.status === 403) throw new Error("Access denied. Open this app from @FraPredictbot in Telegram.");
    if (!r.ok) throw new Error((j && j.error) || `Server error (${r.status}). Try again in a minute.`);
    store.set(key, { at: Date.now(), etag: r.headers.get("ETag"), json: j });
    return j;
  }
  const idle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 1500 }) : setTimeout(fn, 300));
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
    try { const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return ok; } catch (e) { return false; }
  }
  // tg.openLink leaves the Mini App webview (no Instant View) so the OS can route an app-claimed link to the app.
  const openLink = (u) => (tg && tg.openLink ? tg.openLink(u, { try_instant_view: false }) : window.open(u, "_blank"));
  // ---------------------------------------------------------------- crests & flags
  const CREST = (uid, big) => (uid && /^\d{1,9}$/.test(String(uid)) ? `https://img.sportradar.com/ls/crest/${big ? "medium" : "small"}/${uid}.png` : "");
  function initials(n) {
    const w = String(n || "?").replace(/\b(FC|CF|SC|AC|AFC|CD|SV|FK|SK|BK|IF|the)\b/gi, "").trim().split(/[\s\-]+/).filter(Boolean);
    return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 3)).toUpperCase();
  }
  function hue(n) { let h = 0; for (const c of String(n || "")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
  function crest(uid, name, cls = "") {
    const u = CREST(uid, cls === "xl");
    return `<span class="crest ${cls}" style="--h:${hue(name)}" aria-hidden="true"><b>${esc(initials(name))}</b>${u ? `<img src="${u}" alt="" loading="lazy" decoding="async" onload="if(this.naturalWidth<8)this.remove();else this.style.opacity=1" onerror="this.remove()">` : ""}</span>`;
  }
  const ISO = { england: "gb-eng", scotland: "gb-sct", wales: "gb-wls", "northern ireland": "gb-nir", spain: "es", italy: "it", germany: "de", france: "fr", netherlands: "nl", portugal: "pt", belgium: "be", turkiye: "tr", turkey: "tr", usa: "us", brazil: "br", argentina: "ar", nigeria: "ng", austria: "at", switzerland: "ch", denmark: "dk", norway: "no", sweden: "se", greece: "gr", "saudi arabia": "sa", poland: "pl", czechia: "cz", japan: "jp", mexico: "mx", croatia: "hr", serbia: "rs", romania: "ro", ukraine: "ua", russia: "ru", ireland: "ie", australia: "au", china: "cn", "korea republic": "kr", "republic of korea": "kr", "korea": "kr", egypt: "eg", "south africa": "za", morocco: "ma", ghana: "gh", colombia: "co", chile: "cl", uruguay: "uy", israel: "il", finland: "fi", iceland: "is", hungary: "hu", slovakia: "sk", slovenia: "si", bulgaria: "bg", cyprus: "cy", qatar: "qa", "united arab emirates": "ae", uae: "ae", canada: "ca", peru: "pe", ecuador: "ec", paraguay: "py", bolivia: "bo", venezuela: "ve", kenya: "ke", tunisia: "tn", algeria: "dz", iran: "ir", india: "in", indonesia: "id", thailand: "th", vietnam: "vn", estonia: "ee", latvia: "lv", lithuania: "lt", belarus: "by", georgia: "ge", armenia: "am", azerbaijan: "az", kazakhstan: "kz", bosnia: "ba", "bosnia & herzegovina": "ba", albania: "al", "north macedonia": "mk", montenegro: "me", luxembourg: "lu", malta: "mt", "faroe islands": "fo", "costa rica": "cr", honduras: "hn", guatemala: "gt", "el salvador": "sv", jamaica: "jm", "new zealand": "nz", singapore: "sg", malaysia: "my", "hong kong": "hk", jordan: "jo", kuwait: "kw", bahrain: "bh", oman: "om", iraq: "iq", uzbekistan: "uz", "cote d'ivoire": "ci", senegal: "sn", cameroon: "cm", zambia: "zm", tanzania: "tz", uganda: "ug", ethiopia: "et", rwanda: "rw" };
  function catOf(x) { return (x.category || (String(x.league || x.name || "").includes("·") ? String(x.league || x.name).split("·")[0].trim() : "")).toLowerCase(); }
  function flag(x) {
    const c = catOf(x), iso = ISO[c];
    if (iso) return `<img class="flag" src="https://flagcdn.com/w40/${iso}.png" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('⚽'))">`;
    return `<span class="flag emo">${c.startsWith("international") ? "🌍" : "⚽"}</span>`;
  }
  const leagueShort = (lg) => (String(lg || "").includes("·") ? String(lg).split("·").slice(1).join("·").trim() : String(lg || ""));
  const splitMatch = (l) => (l.home && l.away ? [l.home, l.away] : String(l.match || "").split(" vs "));
  function kickDay(iso) { try { return fmt("day", { weekday: "short", day: "2-digit", month: "short" }).format(new Date(iso)); } catch (e) { return ""; } }
  function kickTime(iso) { try { return fmt("hm", { hour: "2-digit", minute: "2-digit" }).format(new Date(iso)); } catch (e) { return ""; } }
  function teamsBlock(l, cls = "") {
    const [h, a] = splitMatch(l);
    return `<div class="tb ${cls}"><div class="tm">${crest(l.home_uid, h)}<span class="ellip">${esc(h)}</span></div><div class="tm">${crest(l.away_uid, a)}<span class="ellip">${esc(a)}</span></div></div>`;
  }
  // group leg cards under a "Sat 31 Oct" heading whenever the kickoff day changes
  function grouped(legs, render) {
    let out = "", last = "";
    legs.forEach((l, i) => { const d = l.kickoff ? kickDay(l.kickoff) : ""; if (d && d !== last) { out += `<div class="grp">${esc(d)}</div>`; last = d; } out += render(l, i); });
    return out;
  }
  const STATUS = { won: ["✓", "Won", "st-won"], lost: ["✕", "Lost", "st-lost"], void: ["–", "Void", "st-void"], check: ["?", "Check", "st-check"], live: ["●", "Live", "st-live"], pending: ["", "Pending", "st-pend"], replaced: ["↻", "Replaced", "st-void"], no_slip: ["⏸", "No slip", "st-void"], skipped: ["⏭", "Skipped", "st-void"] };

  const tierOfOdds = (o) => (o >= 2 ? "red" : o >= 1.6 ? "amber" : "");
  const sel = (l) => ({ event_id: l.event_id, market_id: l.market_id, specifier: l.specifier || "", outcome_id: l.outcome_id });

  // ---------------------------------------------------------------- slip (portfolio)
  const slip = {
    get() { try { return JSON.parse(localStorage.getItem(SLIP_KEY) || "[]"); } catch (e) { return []; } },
    save(items) { localStorage.setItem(SLIP_KEY, JSON.stringify(items)); badge(); },
    add(p) { const all = this.get(); const items = all.filter((x) => x.event_id !== p.event_id); const rep = items.length !== all.length; items.push(p); this.save(items); return rep; },
    remove(eid) { this.save(this.get().filter((p) => p.event_id !== eid)); },
    clear() { this.save([]); },
    total(items) { return (items || this.get()).reduce((t, p) => t * (p.odds || 1), 1); },
    prob(items) { const it = items || this.get(); if (!it.length || it.some((p) => p.est == null)) return null; return it.reduce((t, p) => t * p.est, 1); },
    has(eid, mid, oid, spec) { return this.get().some((p) => p.event_id === eid && p.market_id === mid && p.outcome_id === oid && (p.specifier || "") === (spec || "")); },
  };
  function legToSlip(l) {
    return { ...sel(l), label: l.pick, pick: l.pick, market: l.market, odds: l.odds, est: l.est, match: l.match, when: l.kickoff_local, start: l.kickoff, kickoff: l.kickoff,
      home: l.home, away: l.away, home_uid: l.home_uid, away_uid: l.away_uid, league: l.league, category: l.category, verdict: l.verdict };
  }
  function badge() {
    const n = slip.get().length, b = document.getElementById("slipBadge");
    b.textContent = n; b.classList.toggle("hidden", !n);
  }

  // ---------------------------------------------------------------- navigation
  const viewKey = () => state.tab + (state.tab === "matches" ? ":" + state.mRange + ":" + state.mStack.map((x) => x.name + x.arg).join("/") : "");
  function saveScroll() { state.scroll[viewKey()] = window.scrollY; }
  function restoreScroll() { const y = state.scroll[viewKey()] || 0; requestAnimationFrame(() => window.scrollTo(0, y)); }
  function setTab(t) {
    if (t === state.tab) { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
    saveScroll(); stopLive();
    state.tab = t; hapSel();
    document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    render(); restoreScroll();
  }
  function backVisible() { return state.tab === "matches" && state.mStack.length > 0; }
  function goBack() { stopLive(); state.mStack.pop(); render(); restoreScroll(); }
  if (tg && tg.BackButton) tg.BackButton.onClick(() => { if (!$sheet.classList.contains("hidden")) return closeSheet(); if (backVisible()) goBack(); });
  function render() {
    if (tg && tg.BackButton) (backVisible() ? tg.BackButton.show() : tg.BackButton.hide());
    badge();
    ({ home: renderHome, analyze: renderAnalyze, matches: renderMatches, ai: renderAI, slip: renderSlip, history: renderHistory, rollover: renderRollover })[state.tab]();
  }
  const backLink = () => (tg && tg.BackButton && tg.isVersionAtLeast && tg.isVersionAtLeast("6.1") ? "" : `<button class="back" data-act="mback">‹ Back</button>`);

  // ---------------------------------------------------------------- shared pieces
  function verdictChip(v) {
    if (v === "confirmed") return `<span class="vchip v-confirmed">✓ Stats</span>`;
    if (v === "demoted") return `<span class="vchip v-demoted">Demoted</span>`;
    if (v === "market only") return `<span class="vchip v-odds">Market</span>`;
    if (!v) return "";
    return `<span class="vchip v-odds">Odds only</span>`;
  }
  // "Queens Park Rangers Over/Under" + "Queens Park Rangers Under 2.5" -> "Team Over/Under" + full pick
  function mktLabel(l) {
    const m = String(l.market || ""), [h, a] = splitMatch(l);
    for (const t of [h, a]) if (t && m.startsWith(t + " ")) return "Team " + m.slice(t.length + 1);
    return m;
  }
  function legCard(l, opts = {}) {
    const inSlip = slip.has(l.event_id, l.market_id, l.outcome_id, l.specifier);
    const tick = `<button class="tick btn-tick ${tierOfOdds(l.odds)}${inSlip ? " sel" : ""}" data-act="addleg" data-k="${esc(opts.key || "")}" data-i="${opts.i}">${f2(l.odds)}</button>`;
    const color = l.est >= 0.8 ? "var(--accent)" : l.est >= 0.6 ? "var(--mid)" : l.est >= 0.45 ? "var(--amber)" : "var(--red)";
    const when = l.kickoff ? kickTime(l.kickoff) : esc(l.kickoff_local || "");
    return `<div class="leg"><div class="lg-meta">${flag(l)}<span class="ellip">${esc(leagueShort(l.league))}</span><span class="dot">·</span><span class="num">${when}</span>${verdictChip(l.verdict)}</div>
      <div class="top">${teamsBlock(l, "grow")}${tick}</div>
      <div class="mkt"><span class="ellip">${esc(mktLabel(l))}</span> <span class="pick">${esc(l.pick)}</span></div>
      <div class="prob"><div class="track"><div class="fill" style="width:${Math.round(l.est * 100)}%;background:${color}"></div></div><b>${pct(l.est)}</b></div>
      ${l.why ? `<div class="why">${esc(l.why)}</div>` : ""}</div>`;
  }
  function meter(prob) {
    const risk = prob == null ? 0.5 : 1 - prob;
    return `<div class="meter"><div class="bar"><i style="left:${Math.max(2, Math.min(98, risk * 100)).toFixed(1)}%"></i></div><div class="lbls"><span>Lower risk</span><span>Higher risk</span></div></div>`;
  }
  function portfolio(s, key) {
    const t = TIERS[s.tier] || TIERS.safe;
    return `<div class="card port t-${s.tier}">
      <div class="head"><span class="label">${esc(s.label)} slip · ${esc(s.day)}</span><span class="pill">${s.legs.length} legs</span></div>
      <div class="big num">${f2(s.total_odds)}<small>total odds</small></div>
      <div class="kpis"><div class="kpi"><b>${pct(s.est_prob, 1)}</b><span>Est. hit chance</span></div>
        <div class="kpi"><b>${s.target ? Number(s.target).toFixed(s.target % 1 ? 2 : 0) : "–"}</b><span>Target</span></div>
        <div class="kpi"><b>${s.with_stats}/${s.scanned}</b><span>Stats-checked</span></div></div>
      ${meter(s.est_prob)}
      ${s.constraints_text ? `<div class="rules">🎛 ${esc(s.constraints_text)}${s.constraint_check && s.constraint_check.ok && s.legs.length ? " · ✓ every leg re-checked" : ""}</div>` : ""}
      ${s.note ? `<div class="note">${esc(s.note)}</div>` : ""}
      ${s.legs.length ? `<div class="btnrow"><button class="btn" data-act="bookslip" data-k="${esc(key)}">Book on SportyBet</button>
        <button class="btn secondary" style="flex:.7" data-act="slipall" data-k="${esc(key)}">Add to slip</button></div>
        ${s.slip_id ? `<div class="btnrow">${imgBtn({ slip_id: s.slip_id })}</div>` : ""}` : ""}
      ${key === "scan" ? `<div class="btnrow"><button class="btn small secondary" data-act="scanrefresh" data-x="0" ${state.scanRefreshing ? "disabled" : ""}>${state.scanRefreshing ? "Refreshing…" : "🔄 Refresh picks"}</button><button class="btn small secondary" data-act="scanrefresh" data-x="1" ${state.scanRefreshing ? "disabled" : ""}>🔀 Different picks</button></div>
        ${s.slip_id && s.legs.length ? `<button class="btn ghost" data-act="aiedit" data-kind="scan" data-sid="${esc(s.slip_id)}">✏️ Edit with AI</button>` : ""}
        <div class="sub" style="text-align:center;margin-top:6px">Not saved to History until you book a code</div>` : ""}
    </div>`;
  }

  // ---------------------------------------------------------------- HOME / SCANNER
  function renderHome() {
    const t = TIERS[state.tier];
    let html = `<div style="margin:4px 2px 12px"><div class="label">Market scanner</div><h1>Build a slip</h1>
      <div class="sub">Every SportyBet market · top leagues · form, home/away &amp; H2H checked</div></div>
      <div class="chips">${Object.entries(TIERS).map(([k, v]) => `<button class="chip t-${k}${state.tier === k ? " on" : ""}" data-act="tier" data-t="${k}"><i></i>${v.label}</button>`).join("")}</div>
      <div class="card t-${state.tier}" style="margin-top:10px">
        <div class="seg"><button data-act="day" data-d="today" class="${state.day === "today" ? "on" : ""}">Today</button><button data-act="day" data-d="tomorrow" class="${state.day === "tomorrow" ? "on" : ""}">Tomorrow</button><button data-act="day" data-d="48h" class="${state.day === "48h" ? "on" : ""}">Next 48h</button></div>`;
    if (state.tier !== "audit") {
      html += `<div class="target"><div class="field"><span>Target odds</span><input id="target" inputmode="decimal" value="${state.target}" autocomplete="off"></div></div>
        <div class="quick">${t.quick.map((q) => `<button data-act="quick" data-q="${q}" class="${Number(state.target) === q ? "on" : ""}">${q}×</button>`).join("")}</div>`;
    }
    html += `<div class="label" style="margin-top:12px">Filters</div><div class="chips fchips">${EXCL.map(([k, v]) => `<button class="chip${state.ex.includes(k) ? " on" : ""}" data-act="excl" data-k="${k}" aria-pressed="${state.ex.includes(k)}">${state.ex.includes(k) ? "✓ " : ""}${v}</button>`).join("")}</div>`;
    html += `<div class="chips fchips" style="margin-top:6px"><button class="chip${state.allLg ? " on" : ""}" data-act="alllg" aria-pressed="${state.allLg}">${state.allLg ? "✓ All leagues" : "Top leagues only (tap for all)"}</button></div>`;
    html += `<div class="rules">${esc(t.rule)}</div>
        <button class="btn" data-act="scan" ${state.scanning ? "disabled" : ""}>${state.scanning ? "Scanning…" : state.tier === "audit" ? "Run grand audit" : "Generate slip"}</button></div>`;
    html += `<div id="scanOut">${scanOut()}</div>
      <div class="disc">Estimates from de-margined SportyBet prices, a Poisson goals model and team stats. Not guarantees: “Safe” means lower-risk, never certain. Combined chance assumes independent legs. 18+.</div>`;
    $view.innerHTML = html;
  }
  function scanOut() {
    if (state.scanning) return `<div class="card"><div class="scanning"><div class="radar"></div>Scanning every market…<div class="steps">fetch matches → price ~1,000 markets each → shortlist → form &amp; H2H check → build slip</div></div>
      ${[0, 1, 2].map(() => `<div class="leg"><div class="sk sk-line" style="width:40%"></div><div class="sk sk-line" style="width:75%"></div><div class="sk sk-line" style="width:55%"></div></div>`).join("")}</div>`;
    if (state.scanErr) return `<div class="error">${esc(state.scanErr)}</div>`;
    const r = state.scan;
    if (!r) return "";
    if (r.tier === "audit" && state.viewSlip && r.slips[state.viewSlip]) {
      const k = "audit:" + state.viewSlip, s = r.slips[state.viewSlip];
      return `<button class="back" data-act="auditback">‹ Back to audit</button>` + portfolio(s, k) +
        `<div class="card flush">${grouped(s.legs, (l, i) => legCard(l, { key: k, i }))}</div>`;
    }
    if (r.tier === "audit") return auditOut(r);
    return portfolio(r, "scan") + (r.legs.length ? `<div class="section"><span class="label">Legs</span><span class="sub">tap a price to add</span></div><div class="card flush">${grouped(r.legs, (l, i) => legCard(l, { key: "scan", i }))}</div>` : "");
  }
  function auditOut(r) {
    let h = `<div class="card port t-audit"><div class="label">Grand audit · ${esc(r.day)}</div>
      <div class="kpis"><div class="kpi"><b>${r.scanned}</b><span>Matches</span></div><div class="kpi"><b>${r.with_stats}</b><span>Stats-checked</span></div><div class="kpi"><b>${r.dropped_by_stats}</b><span>Legs dropped</span></div></div></div>`;
    h += `<div class="section"><span class="label">Suggested slips</span></div>`;
    for (const k of ["safe", "mid", "risk", "high"]) {
      const s = r.slips[k]; if (!s) continue;
      h += `<div class="card t-${k}"><div class="head" style="display:flex;justify-content:space-between;align-items:center">
          <span class="chip on t-${k}" style="padding:5px 10px"><i></i>${esc(s.label)}</span>
          <span class="num" style="font-weight:800;font-size:20px">${f2(s.total_odds)}</span></div>
        <div class="sub" style="margin-top:6px">${s.legs.length} legs · est. hit ${pct(s.est_prob, 1)}${s.note ? " · " + esc(s.note) : ""}</div>
        ${s.legs.length ? `<div class="btnrow"><button class="btn small" data-act="bookslip" data-k="audit:${k}">Book</button><button class="btn small secondary" data-act="viewslip" data-k="${k}">View legs</button></div>` : ""}</div>`;
    }
    h += `<div class="section"><span class="label">Top value</span><span class="sub">est. × odds</span></div><div class="card flush">${r.top_value.map((l, i) => legCard(l, { key: "value", i })).join("") || `<div class="empty">No confirmed value legs.</div>`}</div>`;
    h += `<div class="section"><span class="label">Best leg per match</span></div><div class="card flush">${r.best_per_match.map((l, i) => legCard(l, { key: "best", i })).join("")}</div>`;
    return h;
  }
  function legsFor(key) {
    const r = state.scan; if (!r) return [];
    if (key === "scan") return r.legs || [];
    if (key === "value") return r.top_value || [];
    if (key === "best") return r.best_per_match || [];
    if (key.startsWith("audit:")) return (r.slips[key.slice(6)] || {}).legs || [];
    if (key === "pm") return (state.markets && state.markets.legs) || [];
    if (key.startsWith("chat:")) { const m = state.chat[Number(key.slice(5))]; return (m && m.slip && m.slip.legs) || []; }
    return [];
  }
  async function runScan() {
    const inp = document.getElementById("target");
    if (inp) state.target = parseFloat(inp.value.replace(",", ".")) || TIERS[state.tier].target;
    state.scanning = true; state.scanErr = ""; state.scan = null; state.viewSlip = null; hap("medium"); renderHome();
    try {
      state.scanReq = { tier: state.tier, target: state.tier === "audit" ? null : state.target, day: state.day, exclude_markets: state.ex, all_leagues: state.allLg };
      state.scan = await api("/api/scan", state.scanReq);
      hapN("success");
    } catch (e) { state.scanErr = e.message; hapN("error"); }
    state.scanning = false;
    if (state.tab === "home") renderHome();
  }
  const selKey = (l) => `${l.event_id}|${l.market_id}|${l.specifier || ""}|${l.outcome_id}`;
  async function refreshScan(different) {
    const old = state.scan; if (!old || !state.scanReq || state.scanRefreshing) return;
    const prev = (old.legs || []).map(selKey);
    state.scanRefreshing = true; hap("medium"); renderHome(); toast("Re-reading prices… ~30 s");
    try {
      const r = await api("/api/scan", { ...state.scanReq, refresh: true, exclude: different ? prev : [] });
      const now = (r.legs || []).map(selKey);
      if (!now.length) { hapN("warning"); toast("No different valid slip under the same rules right now"); }
      else if (now.length === prev.length && now.every((k) => prev.includes(k))) { hapN("warning"); toast("Same picks are still the strongest · try 🔀 Different picks"); }
      else { state.scan = r; hapN("success"); const kept = now.filter((k) => prev.includes(k)).length; toast(`Refreshed · ${now.length - kept} new, ${kept} kept`); }
    } catch (e) { hapN("error"); toast(e.message); }
    state.scanRefreshing = false;
    if (state.tab === "home") renderHome();
  }

  // ---------------------------------------------------------------- booking sheet
  async function bookLegs(legs, total, prob, slipId) {
    if (!legs.length) return;
    openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div><div class="scanning"><div class="radar"></div>Creating your code…</div>`);
    try {
      const meta = legs.map((l) => ({ pick: l.pick || l.label, market: l.market || "", odds: l.odds, est: l.est, match: l.match, home: l.home, away: l.away, home_uid: l.home_uid, away_uid: l.away_uid, league: l.league, category: l.category, kickoff: l.kickoff || l.start, verdict: l.verdict }));
      const r = await api("/api/book", slipId ? { slip_id: slipId } : { selections: legs.map(sel), legs: meta });
      state.hist = null; store.set("hist", null);
      hapN("success");
      openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div>
        <div class="code" id="codeText">${esc(r.code)}</div>
        <div class="sub" style="text-align:center">${legs.length} legs · total odds ${f2(total)}${prob != null ? ` · est. hit ${pct(prob, 1)}` : ""}${r.loaded != null && r.loaded !== legs.length ? ` · ⚠ ${r.loaded}/${legs.length} loaded` : ""}</div>
        <div class="btnrow"><button class="btn" data-act="copycode">Copy code</button><button class="btn secondary" data-act="opencode" data-c="${esc(r.code)}">📲 Open in SportyBet</button></div>
        <div class="btnrow">${imgBtn(r.history_id ? { history_id: r.history_id } : { code: r.code })}</div>
        <div class="sub" style="text-align:center;margin-top:8px">${r.history_id ? "Saved to History · you'll get a Telegram ping when it lands" : ""}</div>
        <div class="disc">Opens in the SportyBet app if installed (website otherwise), or paste the code under Load Code. Check every selection before staking. Estimates, not guarantees. 18+.</div>`);
    } catch (e) {
      hapN("error");
      openSheet(`<div class="label" style="text-align:center">Booking failed</div><div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="closesheet">Close</button>`);
    }
  }
  function openSheet(html) { $sheetCard.innerHTML = html; $sheet.classList.remove("hidden"); if (tg && tg.BackButton) tg.BackButton.show(); }
  function closeSheet() { $sheet.classList.add("hidden"); if (tg && tg.BackButton) (backVisible() ? tg.BackButton.show() : tg.BackButton.hide()); }

  // ---------------------------------------------------------------- slip image (branded PNG, rendered server-side)
  const imgSrc = (o) => esc(JSON.stringify(o));
  const imgBtn = (o, label, cls) => `<button class="btn ${cls || "small secondary"}" data-act="slipimg" data-src="${imgSrc(o)}">${label || "🖼 Share image"}</button>`;
  const tgAtLeast = (v) => !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v));
  async function slipImage(src) {
    hap("light");
    openSheet(`<div class="label" style="text-align:center">Slip image</div><div class="scanning"><div class="radar"></div>Drawing your slip…</div>`);
    try {
      const r = await api("/api/slipimg", src);
      state.img = r;
      const abs = location.origin + r.url;
      openSheet(`<div class="label" style="text-align:center">Slip image · ${r.legs} leg${r.legs === 1 ? "" : "s"}</div>
        <div class="slipimg-wrap"><img class="slipimg" src="${esc(r.url)}" alt="Slip image" width="${r.width}" height="${r.height}"></div>
        <div class="btnrow"><button class="btn" data-act="imgsend">📤 Send to chat</button>${tgAtLeast("8.0") ? `<button class="btn secondary" data-act="imgshare">↗︎ Share…</button>` : ""}</div>
        <div class="btnrow"><button class="btn small secondary" data-act="imgsave" data-u="${esc(abs)}" data-n="${esc(r.file_name)}">⬇️ Save image</button><button class="btn small secondary" data-act="closesheet">Close</button></div>
        <div class="disc">Nothing is booked or saved. “Send to chat” posts the picture in your chat with the bot so you can forward it. Estimates, not guarantees. 18+.</div>`);
      hapN("success");
    } catch (e) { hapN("error"); openSheet(`<div class="label" style="text-align:center">Slip image</div><div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="closesheet">Close</button>`); }
  }
  async function imgSend(t) {
    if (!state.img) return; t.disabled = true; t.textContent = "Sending…";
    try { await api("/api/slipimg/send", { token: state.img.token }); hapN("success"); toast("Sent to your chat with the bot"); t.textContent = "✓ Sent"; }
    catch (e) { hapN("error"); toast(e.message); t.disabled = false; t.textContent = "📤 Send to chat"; }
  }
  async function imgShare(t) {
    if (!state.img) return; t.disabled = true;
    try { const r = await api("/api/slipimg/prepare", { token: state.img.token }); tg.shareMessage(r.id, (ok) => { if (ok) { hapN("success"); toast("Shared"); } }); }
    catch (e) { toast(e.message); }
    t.disabled = false;
  }
  function imgSave(t) {
    const u = t.dataset.u, n = t.dataset.n;
    if (tgAtLeast("8.0") && tg.downloadFile) { try { tg.downloadFile({ url: u, file_name: n }, () => {}); return; } catch (e) {} }
    openLink(u + "?dl=1");
  }

  // ---------------------------------------------------------------- MATCHES
  // compact rows [id, home, away, start, hu, au, o1, oX, o2] -> objects (day/time formatted once, cached formatters)
  function expandEvents(j) {
    if (!j || j.v !== 2) return j;
    const leagues = j.leagues.map((l) => ({ ...l, matches: l.matches.map((r) => {
      const d = new Date(r[3] * 1000);
      return { id: r[0], home: r[1], away: r[2], start: d.toISOString(), home_uid: r[4], away_uid: r[5],
        day: fmt("dayl", { weekday: "short", day: "2-digit", month: "short" }).format(d), time: fmt("hm", { hour: "2-digit", minute: "2-digit" }).format(d),
        odds: { "1": r[6], X: r[7], "2": r[8] } };
    }) }));
    return { ...j, leagues };
  }
  const EV_FRESH = 4 * 60 * 1000;
  function cachedEvents(range) {
    if (state.mData[range]) return state.mData[range];
    const c = store.get("ev_" + range);
    if (c && c.json && Date.now() - c.at < 6 * 3600 * 1000) { state.mData[range] = { at: c.at, json: expandEvents(c.json), stale: true }; return state.mData[range]; }
    return null;
  }
  const evInflight = {};
  function loadEvents(range, force) {
    const d = cachedEvents(range);
    if (d && !force && !d.stale && Date.now() - d.at < EV_FRESH) return Promise.resolve(d.json);
    if (evInflight[range]) return evInflight[range];
    evInflight[range] = apiCached("ev_" + range, `/api/events?range=${range}`).then((j) => {
      const json = expandEvents(j); state.mData[range] = { at: Date.now(), json }; return json;
    }).finally(() => { delete evInflight[range]; });
    return evInflight[range];
  }
  function findMatch(eid) {
    for (const r of ["today", "upcoming"]) { const d = cachedEvents(r); if (!d) continue; for (const l of d.json.leagues) for (const m of l.matches) if (m.id === eid) return { m, league: l }; }
    return null;
  }
  function renderMatches() {
    const cur = state.mStack[state.mStack.length - 1];
    if (!cur) return renderLeagues();
    if (cur.name === "league") return renderLeague(cur.arg);
    if (cur.name === "match") return renderMatch(cur.arg);
    if (cur.name === "live") return renderLiveMatch(cur.arg);
  }
  function mrow(m, showDay, sub) {
    return `<button class="row mrow" data-act="match" data-id="${esc(m.id)}"><div class="time">${showDay ? esc(m.day.slice(0, 3)) + "<br>" : ""}${esc(m.time)}</div>
      <div class="grow">${teamsBlock(m, "sm")}${sub ? `<div class="sub ellip" style="margin-top:3px">${esc(sub)}</div>` : ""}</div>
      <div class="odds3"><span>${f2(m.odds["1"])}</span><span>${f2(m.odds.X)}</span><span>${f2(m.odds["2"])}</span></div></button>`;
  }
  function leaguesHead(range) {
    const n = state.live ? state.live.matches.length : 0;
    return `<div style="margin:4px 2px 10px"><div class="label">Fixtures</div><h1>Matches</h1></div>
      <div class="seg"><button data-act="mrange" data-r="today" class="${range === "today" ? "on" : ""}">Today</button><button data-act="mrange" data-r="upcoming" class="${range === "upcoming" ? "on" : ""}">Next 3 days</button><button data-act="mrange" data-r="live" class="${range === "live" ? "on" : ""}"><span class="livedot"></span>Live${n ? ` <b class="cnt">${n}</b>` : ""}</button></div>
      ${range === "live" ? "" : `<input id="msearch" class="search" type="search" placeholder="Search league or team" value="${esc(state.mQuery)}" autocomplete="off" enterkeyhint="search">`}`;
  }
  const skRows = (n) => `<div class="card flush" style="margin-top:12px">${Array.from({ length: n }, () => `<div class="row"><div class="sk" style="width:16px;height:11px"></div><div class="grow"><div class="sk sk-line" style="width:60%"></div></div><div class="sk" style="width:28px;height:18px"></div></div>`).join("")}</div>`;
  function leaguesBody(data, range) {
    const q = state.mQuery.trim().toLowerCase();
    let html = "";
    const live = state.live && state.live.matches.length && !q ? state.live.matches.length : 0;
    if (live && range === "today") html += `<button class="card livebanner" data-act="mrange" data-r="live"><span class="lbadge">LIVE</span><b>${live} match${live > 1 ? "es" : ""} in play now</b><span class="sub">scores · tracker · live odds</span><span class="chev">›</span></button>`;
    if (q.length >= 2) {
      const hits = [];
      for (const l of data.leagues) for (const m of l.matches) if (m.home.toLowerCase().includes(q) || m.away.toLowerCase().includes(q)) hits.push([m, l]);
      if (hits.length) html += `<div class="section"><span class="label">Teams · ${hits.length}</span></div><div class="card flush">${hits.slice(0, 40).map(([m, l]) => mrow(m, range === "upcoming", l.name)).join("")}</div>`;
    }
    const leagues = q ? data.leagues.filter((l) => l.name.toLowerCase().includes(q)) : data.leagues;
    html += `<div class="section"><span class="label">${q ? "Leagues" : `${data.count} matches · ${data.leagues.length} leagues`}</span></div>`;
    html += leagues.length ? `<div class="card flush lgl">${leagues.slice(0, 150).map((l) => `<button class="row" data-act="league" data-id="${esc(l.id)}">${flag(l)}<div class="grow ellip">${esc(l.name)}</div><span class="badge">${l.count}</span><span class="chev">›</span></button>`).join("")}</div>` : `<div class="empty">Nothing matches “${esc(state.mQuery)}”.</div>`;
    return html;
  }
  async function renderLeagues() {
    const range = state.mRange;
    if (range === "live") return renderLiveList();
    const d = cachedEvents(range);
    $view.innerHTML = leaguesHead(range) + `<div id="mbody">${d ? leaguesBody(d.json, range) : skRows(8)}</div>`;
    bindSearch();
    if (!state.live || Date.now() - state.liveAt > 60000) loadLive().then(() => { if (state.tab === "matches" && !state.mStack.length && state.mRange === range) { const b = document.querySelector(".seg"); if (b) b.outerHTML = leaguesHead(range).match(/<div class="seg">[\s\S]*?<\/div>/)[0]; const mb = document.getElementById("mbody"); if (mb && state.mData[range]) mb.innerHTML = leaguesBody(state.mData[range].json, range); } }).catch(() => {});
    if (d && !d.stale && Date.now() - d.at < EV_FRESH) return;
    let data;
    try { data = await loadEvents(range, !!d); } catch (e) {
      if (!d) { const mb = document.getElementById("mbody"); if (mb && state.mRange === range) mb.innerHTML = `<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="mretry">Try again</button>`; }
      return;
    }
    if (state.tab !== "matches" || state.mStack.length || state.mRange !== range) return;
    const mb = document.getElementById("mbody");
    if (mb) mb.innerHTML = leaguesBody(data, range);   // in place: keeps scroll position and the search box focus
  }
  let searchT;
  function bindSearch() {
    const s = document.getElementById("msearch"); if (!s) return;
    s.addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(() => { state.mQuery = s.value; const d = state.mData[state.mRange]; const mb = document.getElementById("mbody"); if (d && mb) mb.innerHTML = leaguesBody(d.json, state.mRange); }, 200); });
  }
  function renderLeague(id) {
    const d = cachedEvents(state.mRange); const l = d && d.json.leagues.find((x) => x.id === id);
    if (!l) { $view.innerHTML = `${backLink()}<div class="empty">League not found.</div>`; return; }
    $view.innerHTML = `${backLink()}<div style="margin:4px 2px 10px"><div class="label">${l.count} matches</div><h2 class="lgh">${flag(l)}${esc(l.name)}</h2></div><div class="card flush">${l.matches.map((m) => mrow(m, state.mRange === "upcoming")).join("")}</div>`;
  }
  async function renderMatch(eid) {
    const k = findMatch(eid);
    state.pred = null; state.markets = null;
    $view.innerHTML = backLink() + (k ? matchHero({ ...k.m, league: k.league.name, category: k.league.category }, `${esc(k.m.day)}, ${esc(k.m.time)} WAT`) : "") +
      `<div class="card">${[0, 1, 2, 3].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`;
    const isCur = () => { const c = state.mStack[state.mStack.length - 1]; return state.tab === "matches" && c && c.name === "match" && c.arg === eid; };
    let p;
    try { p = await api("/api/predict", { event_id: eid }); } catch (e) { if (isCur()) $view.innerHTML = `${backLink()}<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="match" data-id="${esc(eid)}">Try again</button>`; return; }
    if (!isCur()) return;
    state.pred = p; drawMatch();
    try { state.markets = await api(`/api/match_markets?event_id=${encodeURIComponent(eid)}`); } catch (e) { state.markets = { legs: [], error: e.message }; }
    if (isCur()) drawMatch();
  }
  function matchHero(e, subline) {
    return `<div class="card hero"><div class="sub hl">${flag(e)}<span class="ellip">${esc(e.league)}</span></div>
      <div class="vs"><div class="side">${crest(e.home_uid, e.home, "xl")}<b>${esc(e.home)}</b></div><div class="mid"><span>VS</span><small>${subline}</small></div><div class="side">${crest(e.away_uid, e.away, "xl")}<b>${esc(e.away)}</b></div></div></div>`;
  }
  function formPills(s) { return `<span class="form">${(s || "").split("").map((c) => `<b class="${c}">${c}</b>`).join("")}</span>`; }
  function statsCard(st) {
    if (!st || !st.home || !st.away) return `<div class="card"><div class="label">Team stats</div><div class="sub" style="margin-top:6px">No stats available for this match (odds-only estimate).</div></div>`;
    const h = st.home, a = st.away, x = st.h2h || {};
    const row = (lh, label, la) => `<div class="r">${lh}</div><div class="c">${label}</div><div>${la}</div>`;
    return `<div class="card"><div class="label">Team stats · last ${Math.max(h.games, a.games)} games</div>
      <div class="stat-grid"><div class="r"><b>${esc(h.name)}</b></div><div class="c"></div><div><b>${esc(a.name)}</b></div>
      ${row(formPills(h.form5), "Form", formPills(a.form5))}
      ${row(`<b>${esc(h.home_record)}</b>`, "Home / Away", `<b>${esc(a.away_record)}</b>`)}
      ${row(`<b>${h.gf_home ?? "–"} / ${h.ga_home ?? "–"}</b>`, "Goals for/against (venue)", `<b>${a.gf_away ?? "–"} / ${a.ga_away ?? "–"}</b>`)}
      ${row(`<b>${esc(h.over25)}</b>`, "Over 2.5", `<b>${esc(a.over25)}</b>`)}
      ${row(`<b>${esc(h.btts)}</b>`, "BTTS", `<b>${esc(a.btts)}</b>`)}
      ${row(`<b>${esc(h.clean_sheets)}</b>`, "Clean sheets", `<b>${esc(a.clean_sheets)}</b>`)}</div>
      <div class="sub" style="margin-top:10px">${x.n ? `H2H last ${x.n}: ${x.home_w}-${x.draws}-${x.away_w} · avg ${x.avg_goals} goals · BTTS ${x.btts}/${x.n} · ${esc((x.last || []).join(", "))}` : "No recent head-to-head."}</div></div>`;
  }
  function drawMatch() {
    const p = state.pred; if (!p) return;
    const e = p.event, b = p.best;
    const isBest = (o) => b && b.market_id === o.market_id && b.outcome_id === o.outcome_id;
    let h = `${backLink()}${matchHero(e, `${esc(e.day)}, ${esc(e.time)} WAT${p.xg ? `<br>xG ${p.xg[0]}–${p.xg[1]}` : ""}`)}<div class="card"><div class="sub">${esc(p.source)}</div>
      ${b ? `<div class="note" style="background:var(--accent-soft);border-color:rgba(0,229,143,.3);color:var(--text)">Best pick: <b>${esc(b.label)}</b> · ${pct(b.prob)}${b.odds ? " @ " + f2(b.odds) : ""}</div>` : ""}
      <div class="why" style="white-space:pre-line">${esc(p.reasoning)}</div></div>`;
    h += statsCard(p.stats);
    for (const mk of p.markets) {
      h += `<div class="card"><div class="label">${esc(mk.title)}</div><div class="outs">${mk.outcomes.map((o) => `<button class="out${slip.has(e.id, o.market_id, o.outcome_id, o.specifier) ? " sel" : ""}${isBest(o) ? " is-best" : ""}" data-act="addpred" data-m="${o.market_id}" data-o="${o.outcome_id}" ${o.odds ? "" : "disabled"}>
        <div class="l">${esc(o.label)}</div><div class="p">${pct(o.prob)}</div><div class="o">${o.odds ? f2(o.odds) : "–"}</div></button>`).join("")}</div></div>`;
    }
    const m = state.markets;
    h += `<div class="section"><span class="label">Strongest legs · all markets</span></div>`;
    if (!m) h += `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`;
    else if (m.error) h += `<div class="error">${esc(m.error)}</div>`;
    else h += `<div class="card flush">${m.legs.map((l, i) => legCard(l, { key: "pm", i })).join("") || `<div class="empty">No priced legs.</div>`}</div>`;
    h += `<div class="disc">${esc(p.disclaimer)}</div>`;
    $view.innerHTML = h;
  }
  const mk0 = (mid) => ({ "1": "1X2", "18": "Over/Under", "10": "Double Chance", "29": "GG/NG" }[mid] || "");
  function addPred(mid, oid) {
    const p = state.pred; if (!p) return; let o = null;
    for (const mk of p.markets) for (const x of mk.outcomes) if (x.market_id === mid && x.outcome_id === oid) o = x;
    if (!o || !o.odds) return;
    const e = p.event;
    const rep = slip.add({ event_id: e.id, market_id: mid, outcome_id: oid, specifier: o.specifier || "", label: o.label, market: mk0(mid), odds: o.odds, est: o.prob, match: `${e.home} vs ${e.away}`, when: `${e.day.slice(0, 3)} ${e.time}`, start: e.start, kickoff: e.start, home: e.home, away: e.away, home_uid: e.home_uid, away_uid: e.away_uid, league: e.league, category: e.category });
    hap("medium"); toast(`${rep ? "Replaced" : "Added"}: ${o.label} @ ${f2(o.odds)}`); drawMatch();
  }


  // ---------------------------------------------------------------- LIVE (in-play: score, tracker, live odds, official video)
  let liveTimer = null;
  function stopLive() { if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; } }
  async function loadLive() {
    const j = await api("/api/live");
    state.live = j; state.liveAt = Date.now(); state.liveErr = "";
    return j;
  }
  function liveRow(m) {
    return `<button class="row mrow lrow" data-act="livematch" data-id="${esc(m.id)}"><div class="time"><span class="lbadge">LIVE</span><b class="lmin">${esc(m.minute || "")}</b></div>
      <div class="grow">${teamsBlock(m, "sm")}<div class="sub ellip" style="margin-top:3px">${flag(m)} ${esc(leagueShort(m.league))}${m.stream ? " · 📺 stream on SportyBet" : ""}</div></div>
      <div class="lscore num">${esc((m.score || "0–0").split("–")[0])}<br>${esc((m.score || "0–0").split("–")[1] || "0")}</div>
      <div class="odds3 v">${[0, 1, 2].map((i) => `<span>${f2(m.odds[i])}</span>`).join("")}</div></button>`;
  }
  async function renderLiveList() {
    stopLive();
    const draw = () => {
      if (state.tab !== "matches" || state.mStack.length || state.mRange !== "live") return false;
      const L = state.live;
      let h = leaguesHead("live");
      if (!L && !state.liveErr) h += skRows(6);
      else if (!L) h += `<div class="error">${esc(state.liveErr)}</div><button class="btn secondary" data-act="mrange" data-r="live">Try again</button>`;
      else if (!L.matches.length) h += `<div class="empty">No football in play right now. Live matches show up here with a LIVE badge.</div>`;
      else {
        const groups = {}; L.matches.forEach((m) => (groups[m.league] = groups[m.league] || []).push(m));
        h += `<div class="section"><span class="label">${L.matches.length} in play</span><span class="sub">updates every 20 s</span></div>`;
        h += `<div class="card flush">${L.matches.map(liveRow).join("")}</div>`;
      }
      h += `<div class="disc">Live scores, tracker and odds from SportyBet and the Sportradar feed. No unofficial streams: video only from official free channels or SportyBet's own page. 18+.</div>`;
      $view.innerHTML = h; return true;
    };
    draw();
    const tick = async () => {
      try { await loadLive(); } catch (e) { state.liveErr = e.message; }
      if (draw() && !document.hidden) liveTimer = setTimeout(tick, 20000);
    };
    if (!state.live || Date.now() - state.liveAt > 15000) tick(); else liveTimer = setTimeout(tick, 20000);
  }
  // pitch: home attacks to the right (Sportradar coordinates 0-100)
  function pitchSvg(ball, head) {
    const b = ball || {}, x = b.x == null ? 50 : b.x, y = b.y == null ? 50 : b.y;
    const sit = { attack: "Attack", dangerous: "Dangerous attack", safe: "In possession" }[b.situation] || "Waiting for play";
    const side = b.team === "away" ? (head && head.away) || "Away" : b.team === "home" ? (head && head.home) || "Home" : "";
    const zone = b.situation === "dangerous" ? (b.team === "home" ? "M75 0h25v60H75z" : "M0 0h25v60H0z") : b.situation === "attack" ? (b.team === "home" ? "M50 0h50v60H50z" : "M0 0h50v60H0z") : "";
    return `<div class="pitchwrap"><svg class="pitch" viewBox="0 0 100 60" preserveAspectRatio="none">
      <rect x="0" y="0" width="100" height="60" class="grass"/>${zone ? `<path d="${zone}" class="zone ${b.team === "away" ? "za" : "zh"}"/>` : ""}
      <g class="lines"><rect x="1" y="1" width="98" height="58"/><line x1="50" y1="1" x2="50" y2="59"/><circle cx="50" cy="30" r="8"/>
      <rect x="1" y="16" width="14" height="28"/><rect x="85" y="16" width="14" height="28"/><rect x="1" y="24" width="5" height="12"/><rect x="94" y="24" width="5" height="12"/></g>
      </svg><i class="ball ${b.team === "away" ? "ba" : "bh"}" style="left:${x}%;top:${y}%"></i>
      <div class="sit ${b.team === "away" ? "sa" : "sh"}">${side ? `<b>${esc(side)}</b> · ` : ""}${esc(sit)}</div>
      <div class="dirs"><span>${esc(head ? head.home : "Home")} →</span><span>← ${esc(head ? head.away : "Away")}</span></div></div>`;
  }
  function momentumSvg(mom) {
    if (!mom || !mom.length) return `<div class="sub">Momentum appears once play is tracked.</div>`;
    const n = Math.max(mom.length, 46), w = 100 / n, mx = Math.max(4, ...mom.map((v) => Math.abs(v)));
    const sm = mom.map((v, i) => (v + (mom[i - 1] || 0) * 0.6 + (mom[i + 1] || 0) * 0.6) / 2.2);
    const bars = sm.map((v, i) => { const hgt = Math.min(1, Math.abs(v) / mx) * 18; return `<rect x="${(i * w).toFixed(2)}" y="${v >= 0 ? 20 - hgt : 20}" width="${(w * 0.8).toFixed(2)}" height="${hgt.toFixed(2)}" class="${v >= 0 ? "mh" : "ma"}"/>`; }).join("");
    return `<svg class="mom" viewBox="0 0 100 40" preserveAspectRatio="none"><line x1="0" y1="20" x2="100" y2="20" class="mid"/><line x1="${(45 * 100 / n).toFixed(1)}" y1="0" x2="${(45 * 100 / n).toFixed(1)}" y2="40" class="ht"/>${bars}</svg>`;
  }
  const TL_ICON = { goal: "⚽", card: "🟨", corner: "⚑", substitution: "⇄", shotontarget: "◎", shotofftarget: "○", penalty_awarded: "◉", penalty_missed: "✕", periodstart: "▶", periodscore: "⏸", injurytimeshown: "+", match_ended: "■", video_assistant_referee: "VAR" };
  function liveBody(d) {
    const h = d.head || {}, f = d.feed || {};
    let out = "";
    out += `<div class="card"><div class="label">Live tracker</div>${pitchSvg(f.ball, h)}
      <div class="label" style="margin-top:12px">Momentum <span class="sub">(${esc(h.home || "home")} up · ${esc(h.away || "away")} down)</span></div>${momentumSvg(f.momentum)}</div>`;
    if (f.stats && f.stats.length) out += `<div class="card"><div class="label">Match stats</div>${f.stats.map((x) => { const t = (x.home + x.away) || 1; return `<div class="sbar"><b>${x.home}</b><span>${esc(x.label)}</span><b>${x.away}</b><div class="trk"><i style="width:${(x.home / t * 100).toFixed(1)}%"></i></div></div>`; }).join("")}</div>`;
    if (d.markets && d.markets.length) out += `<div class="section"><span class="label">Live odds · SportyBet</span><span class="sub">fair % = de-margined</span></div>` + d.markets.map((m) => `<div class="card"><div class="label">${esc(m.title)}</div><div class="outs">${m.outcomes.map((o) => `<div class="out ro"><div class="l">${esc(o.desc)}</div><div class="p">${o.prob != null ? pct(o.prob) : "–"}</div><div class="o">${o.odds ? f2(o.odds) : "🔒"}</div></div>`).join("")}</div></div>`).join("");
    else if (h.live === false) out += `<div class="note">This match isn't in play on SportyBet any more.</div>`;
    if (f.events && f.events.length) out += `<div class="section"><span class="label">Timeline</span></div><div class="card flush">${f.events.map((e) => `<div class="tl t-${esc(e.team)}"><span class="tmin num">${e.min != null && e.min >= 0 ? e.min + "'" : ""}</span><span class="tic ${e.type === "card" ? "c-" + esc(e.card || "yellow") : ""}">${e.type === "card" && e.card === "red" ? "🟥" : TL_ICON[e.type] || "•"}</span><div class="grow"><b>${esc(e.name)}${e.score ? " " + esc(e.score) : ""}</b>${e.player ? `<div class="sub">${esc(e.player)}${e.note ? " · " + esc(e.note) : ""}</div>` : ""}</div><span class="tteam sub">${e.team === "home" ? esc(h.home || "") : e.team === "away" ? esc(h.away || "") : ""}</span></div>`).join("")}</div>`;
    out += `<div class="disc">Tracker drawn from Sportradar's free event feed (not video). Odds move fast; estimates, not guarantees. 18+.</div>`;
    return out;
  }
  function liveHeroHtml(h) {
    return `<div class="card hero lv"><div class="sub hl">${flag(h)}<span class="ellip">${esc(h.league || "")}</span><span class="lbadge">${h.live === false ? "ENDED" : "LIVE"}</span></div>
      <div class="vs"><div class="side">${crest(h.home_uid, h.home, "xl")}<b>${esc(h.home)}</b></div><div class="mid"><span class="bigscore num" id="lscore">${esc(h.score || "0–0")}</span><small id="lmin" class="lmin">${esc(h.minute || "")}</small>${h.ht ? `<small class="sub">HT ${esc(String(h.ht).replace(":", "–"))}</small>` : ""}</div><div class="side">${crest(h.away_uid, h.away, "xl")}<b>${esc(h.away)}</b></div></div></div>`;
  }
  function videoHtml(d) {
    const v = d.video && d.video.video, h = d.head || {};
    if (v) return `<div class="card flush vidcard"><div class="vid"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(v.id)}?autoplay=1&mute=1&playsinline=1&rel=0" title="${esc(v.title)}" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen loading="lazy"></iframe></div>
      <div class="vmeta"><span class="vchip v-confirmed">Official</span><span class="ellip sub">${esc(v.channel)} · ${esc(v.title)}</span></div></div>`;
    return `<div class="card watch"><div class="grow"><b>${h.stream ? "Stream available on SportyBet" : "No official free stream found"}</b><div class="sub">${h.stream ? "SportyBet flags a live stream for this match: it plays in your own SportyBet account." : "We only embed official channels (clubs, leagues, broadcasters). Follow it with the tracker below, or see where it's shown."}</div></div>
      <button class="btn small" style="width:auto;margin:0;padding:0 14px" data-act="openurl" data-u="${esc(h.url || LOAD_URL)}">${h.stream ? "Watch on SportyBet" : "Where to watch"}</button></div>`;
  }
  async function renderLiveMatch(eid) {
    stopLive();
    const isCur = () => { const c = state.mStack[state.mStack.length - 1]; return state.tab === "matches" && c && c.name === "live" && c.arg === eid; };
    const fromList = state.live && state.live.matches.find((m) => m.id === eid);
    const first = state.lm && state.lm.event_id === eid ? state.lm : null;
    $view.innerHTML = backLink() + `<div id="lhero">${first && first.head ? liveHeroHtml(first.head) : fromList ? liveHeroHtml({ ...fromList, live: true }) : `<div class="card"><div class="sk sk-line"></div><div class="sk sk-line"></div></div>`}</div>
      <div id="lvideo">${first ? videoHtml(first) : `<div class="card"><div class="sk sk-line" style="width:70%"></div></div>`}</div><div id="lbody">${first ? liveBody(first) : `<div class="card">${[0, 1, 2, 3].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`}</div>`;
    let videoDone = !!first;
    const tick = async (n) => {
      let d;
      try { d = await api(`/api/live/match?event_id=${encodeURIComponent(eid)}${videoDone ? "&video=0" : ""}`); }
      catch (e) { if (isCur()) { const b = document.getElementById("lbody"); if (b && !state.lm) b.innerHTML = `<div class="error">${esc(e.message)}</div>`; liveTimer = setTimeout(() => tick(n + 1), 15000); } return; }
      if (!isCur()) return;
      if (videoDone && state.lm && state.lm.event_id === eid && state.lm.video) d.video = state.lm.video;
      const prevScore = state.lm && state.lm.event_id === eid && state.lm.head ? state.lm.head.score : null;
      state.lm = d;
      if (d.head) { const hs = document.getElementById("lscore"), hm = document.getElementById("lmin");
        if (hs && hm) { hs.textContent = d.head.score || "0–0"; hm.textContent = d.head.minute || ""; if (prevScore && prevScore !== d.head.score) { hs.classList.add("flash"); hapN("success"); setTimeout(() => hs.classList.remove("flash"), 1500); } }
        else document.getElementById("lhero").innerHTML = liveHeroHtml(d.head); }
      if (!videoDone) { document.getElementById("lvideo").innerHTML = videoHtml(d); videoDone = true; }   // iframe created once, never re-rendered
      const lb = document.getElementById("lbody"); if (lb) lb.innerHTML = liveBody(d);
      if (!document.hidden && !(d.head && d.head.live === false)) liveTimer = setTimeout(() => tick(n + 1), 8000);
    };
    tick(0);
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopLive(); else if (state.tab === "matches") { const c = state.mStack[state.mStack.length - 1]; if ((c && c.name === "live") || (!c && state.mRange === "live")) render(); } });

  // ---------------------------------------------------------------- AI
  const SUGGEST = ["Analyze today's games and give me a safe 5 odds Sporty code", "Who wins Arsenal vs Chelsea?", "Run a grand audit for today", "Best value bets this weekend?"];
  function mdLite(t) {
    let s = esc(t);
    s = s.replace(/`([^`]+)`/g, '<code data-act="copyinline">$1</code>').replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|\s)_(.+?)_(?=\s|$)/g, "$1<i>$2</i>");
    s = s.replace(/^\s*[\*\-]\s+/gm, "• ").replace(/^#{1,6}\s*/gm, "");
    return s;
  }
  function renderAI() {
    let h = `<div style="margin:4px 2px 12px"><div class="label">Gemini · function calling</div><h1>AI analyst</h1><div class="sub">Asks the scanner, stats and booking tools for real numbers</div></div><div class="chat" id="chat">`;
    if (!state.chat.length) h += `<div class="msg bot">Hi! Ask me to analyse a match, scan today's markets or build a booking code.</div><div class="suggest">${SUGGEST.map((s) => `<button data-act="suggest">${esc(s)}</button>`).join("")}</div>`;
    state.chat.forEach((m, i) => {
      if (m.role === "me") h += `<div class="msg me">${m.img ? `<img class="msgimg" src="${m.img}" alt="">` : ""}${esc(m.text)}</div>`;
      else {
        h += `<div class="msg bot">${mdLite(m.text)}${m.tools && m.tools.length ? `<span class="tools">⚙ ${esc([...new Set(m.tools)].join(" · "))}</span>` : ""}</div>`;
        if (m.edit && m.edit.ok) {
          h += `<div class="card" style="max-width:88%;margin:4px 0 8px">${(m.edit.legs || []).map((l, j) => `<div class="sub" style="margin:3px 0">${j + 1}. <b>${esc((l.home || "") + " v " + (l.away || ""))}</b> · ${esc(l.pick || "")} @ ${f2(l.odds)}${l.est != null ? " · est " + pct(l.est) : ""}</div>`).join("")}<div style="margin-top:6px"><b>${f2(m.edit.total_odds)}</b> total · est ${pct(m.edit.est_prob, 1)}</div></div>`;
          if (m.edit.section === "rollover_preview") h += `<div class="btnrow" style="max-width:88%"><button class="btn small" data-act="roconfirm" data-t="${esc(m.edit.token)}">✅ Confirm: book new code</button><button class="btn small secondary" data-act="aikeep">Keep current slip</button></div>`;
        }
        if (m.analysis && m.analysis.summary) { const a = m.analysis, s2 = a.summary;
          h += `<div class="card" style="max-width:88%;margin:4px 0 8px"><div class="sub">${a.code ? "Code " + esc(a.code) : "📷 From your screenshot"} · ${s2.legs} legs${s2.unmatched ? " · " + s2.unmatched + " couldn't match" : ""}</div><div style="margin:4px 0"><b>Grade ${esc(s2.grade || "–")}</b> · ${esc(a.verdict || "")}</div><div class="sub">Total odds ${s2.odds_known === false ? "n/a" : f2(s2.total_odds)} · est. hit chance ${pc(s2.chance)}</div></div><div class="btnrow" style="max-width:88%"><button class="btn small" data-act="aiaz" data-k="${i}">Open full analysis</button></div>`; }
        if (m.slip && m.slip.legs && m.slip.legs.length && !m.code) h += `<div class="btnrow" style="max-width:88%"><button class="btn small" data-act="bookslip" data-k="chat:${i}">Book this slip</button><button class="btn small secondary" data-act="slipall" data-k="chat:${i}">Add to slip</button>${m.slip.slip_id ? imgBtn({ slip_id: m.slip.slip_id }, "🖼 Image") : ""}</div>`;
        if (m.code) h += `<div class="btnrow" style="max-width:88%"><button class="btn small" data-act="copytext" data-t="${esc(m.code)}">Copy ${esc(m.code)}</button><button class="btn small secondary" data-act="opencode" data-c="${esc(m.code)}">📲 Open in SportyBet</button>${imgBtn({ code: m.code }, "🖼 Image")}</div>`;
        else if (m.analysis && m.analysis.id && !(m.slip && m.slip.legs && m.slip.legs.length)) h += `<div class="btnrow" style="max-width:88%">${imgBtn({ analysis_id: m.analysis.id }, "🖼 Slip image")}</div>`;
      }
    });
    if (state.aiBusy) h += `<div class="msg bot"><span class="typing"><i></i><i></i><i></i></span> <span class="sub">analysing… scans take ~30 s</span></div>`;
    if (state.aiCtx) h += `<div class="rules" style="margin:8px 2px">✏️ Editing: <b>${esc(state.aiCtx.label)}</b> · try “change the first selection”, “make it safer” <button class="btn small secondary" style="width:auto;margin:0 0 0 6px;padding:0 10px" data-act="aictxclear">✕</button></div>`;
    if (state.aiImg) h += `<div class="rules imgchip" style="margin:8px 2px"><img src="${state.aiImg.data}" alt=""><span>📷 Image attached · add a caption (optional) and send</span><button class="btn small secondary" style="width:auto;margin:0 0 0 6px;padding:0 10px" data-act="aiimgclear">✕</button></div>`;
    h += `</div><div class="composer"><form id="aiForm"><button type="button" class="attach" data-act="aiattach" aria-label="Attach image" ${state.aiBusy ? "disabled" : ""}><svg viewBox="0 0 24 24"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg></button><input type="file" id="aiFile" accept="image/*" hidden><input id="aiInput" placeholder="${state.aiImg ? "What should I do with this image?" : state.aiCtx ? "What should I change on this slip?" : "Ask about matches, slips, codes…"}" autocomplete="off" ${state.aiBusy ? "disabled" : ""}>
      <button type="submit" aria-label="Send" ${state.aiBusy ? "disabled" : ""}><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button></form></div>`;
    $view.innerHTML = h;
    document.getElementById("aiForm").addEventListener("submit", (ev) => { ev.preventDefault(); const v = document.getElementById("aiInput").value.trim(); if (v || state.aiImg) askAI(v); });
    document.getElementById("aiFile").addEventListener("change", (ev) => { const f = ev.target.files && ev.target.files[0]; ev.target.value = ""; if (f) attachImage(f); });
    window.scrollTo(0, document.body.scrollHeight);
  }
  // image attach: camera or gallery, downscaled on the phone (longest side 1600 px, JPEG) before upload
  function attachImage(file) {
    if (!/^image\//.test(file.type || "image/")) { toast("That's not an image"); return; }
    if (file.size > 25 * 1024 * 1024) { toast("Image too large"); return; }
    const url = URL.createObjectURL(file), im = new Image();
    im.onload = () => {
      const k = Math.min(1, 1600 / Math.max(im.naturalWidth, im.naturalHeight));
      const c = document.createElement("canvas"); c.width = Math.round(im.naturalWidth * k); c.height = Math.round(im.naturalHeight * k);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      let q = 0.85, d = c.toDataURL("image/jpeg", q);
      while (d.length > 6.5e6 && q > 0.4) { q -= 0.15; d = c.toDataURL("image/jpeg", q); }
      state.aiImg = { data: d }; hap("light"); renderAI(); const inp = document.getElementById("aiInput"); if (inp) inp.focus();
    };
    im.onerror = () => { URL.revokeObjectURL(url); toast("Couldn't open that image"); };
    im.src = url;
  }
  async function askAI(text) {
    if (state.aiBusy) return;
    const img = state.aiImg; state.aiImg = null;
    state.chat.push({ role: "me", text: text || (img ? "" : ""), img: img ? img.data : null }); state.aiBusy = true; hap("light"); renderAI();
    try {
      const body = { message: text }; if (state.aiCtx && !img) body.context = state.aiCtx;
      if (img) body.image = { data: img.data.split(",")[1], mime: "image/jpeg" };
      const r = await api("/api/ai", body);
      const ed = r.edit || null;
      const eslip = ed && ed.ok && ed.slip && ed.slip.legs ? { ...ed.slip, slip_id: ed.sid } : null;
      state.chat.push({ role: "bot", text: r.reply, slip: eslip || (r.slip && r.slip.legs ? r.slip : null), code: r.code, tools: r.tools, edit: ed, analysis: r.analysis || null });
      if (r.context) state.aiCtx = r.context;
      if (ed && ed.ok) state.aiCtx = ed.section === "rollover_preview" ? { kind: "preview", token: ed.token, slip_id: ed.slip_id, label: `Rollover day ${ed.day || ""} preview` } : { kind: ed.section === "scan" ? "scan" : "ai", sid: ed.sid, label: "Edited slip" };
      hapN("success");
    } catch (e) { state.chat.push({ role: "bot", text: "⚠ " + e.message }); hapN("error"); }
    state.aiBusy = false;
    if (state.tab === "ai") renderAI();
  }

  // ---------------------------------------------------------------- ANALYZER
  const RCLS = { Strong: "r-Strong", OK: "r-OK", Weak: "r-Weak", Avoid: "r-Avoid", "Not rated": "r-NR" };
  const pc = (x) => (x == null ? "–" : x < 0.095 ? (x * 100).toFixed(1) + "%" : Math.round(x * 100) + "%");
  const rchip = (r) => `<span class="rchip ${RCLS[r] || "r-NR"}">${esc(r)}</span>`;
  const azRecent = { get() { try { return JSON.parse(localStorage.getItem("fra_az_recent") || "[]"); } catch (e) { return []; } },
    add(c) { const a = this.get().filter((x) => x !== c); a.unshift(c); try { localStorage.setItem("fra_az_recent", JSON.stringify(a.slice(0, 6))); } catch (e) {} } };
  function azLeg(l) {
    const [h, a] = splitMatch(l);
    if (l.unmatched) return `<div class="leg azleg dim"><div class="lg-meta"><span>Selection ${l.idx + 1}</span>${rchip("Not rated")}</div><div><b>${esc(l.match || "?")}</b></div><div class="mkt"><span class="ellip">${esc(l.market || "")}</span> <span class="pick">${esc(l.pick || "")}</span>${l.image_odds ? " @ " + f2(l.image_odds) : ""}</div><div class="why">⚠️ Couldn't match${l.unmatched_why ? " (" + esc(l.unmatched_why) + ")" : ""}: not counted</div></div>`;
    if (l.state === "unavailable") return `<div class="leg azleg dim"><div class="lg-meta"><span>Selection ${l.idx + 1}</span>${rchip("Not rated")}</div><div class="why">${esc(l.reason)}</div></div>`;
    const when = l.kickoff ? kickDay(l.kickoff) + " " + kickTime(l.kickoff) : "";
    const topc = l.flags && l.flags.length ? `<span class="rchip r-top" style="margin-left:6px">Outside top leagues</span>` : "";
    let body = "";
    if (l.state === "ended" || l.state === "cancelled") {
      const [ic, t, c] = STATUS[l.result] || STATUS.pending;
      body = `<div class="state ${c === "st-won" ? "c-acc" : c === "st-lost" ? "c-red" : ""}">${ic} ${t || "Result pending"}${l.score ? " · " + esc(String(l.score).replace(":", "-")) : ""}</div>`;
    } else if (l.state === "live") {
      body = `<div class="state c-live">● Live ${esc(String(l.score || "").replace(":", "-"))} ${esc(l.minute || "")} · not rated in play</div>`;
    } else if (l.rated) {
      const color = l.est >= 0.8 ? "var(--accent)" : l.est >= 0.65 ? "var(--mid)" : l.est >= 0.45 ? "var(--amber)" : "var(--red)";
      body = `<div class="prob"><div class="track"><div class="fill" style="width:${Math.round(l.est * 100)}%;background:${color}"></div></div><b>${pct(l.est)}</b></div>
        <div class="nums"><div><b class="num">${f2(l.fair_odds)}</b><span>Fair odds</span></div><div><b class="num ${l.value >= 0 ? "c-acc" : l.value < -0.08 ? "c-red" : ""}">${(l.value >= 0 ? "+" : "") + Math.round(l.value * 100)}%</b><span>Value</span></div><div><b class="num">${l.booked_odds && Math.abs(l.booked_odds - l.odds) > 0.004 ? f2(l.booked_odds) : "–"}</b><span>Booked at</span></div></div>`;
    }
    const alt = (l.alternatives || [])[0];
    return `<div class="leg azleg${l.state !== "upcoming" ? " dim" : ""}"><div class="lg-meta">${flag(l)}<span class="ellip">${esc(leagueShort(l.league))}</span><span class="dot">·</span><span class="num">${esc(when)}</span>${topc}${l.state === "upcoming" ? rchip(l.rating) : ""}</div>
      <div class="top">${teamsBlock(l, "grow")}<div class="tick ${tierOfOdds(l.odds)}">${f2(l.odds)}</div></div>
      <div class="mkt"><span class="ellip">${esc(mktLabel(l))}</span> <span class="pick">${esc(l.pick)}</span></div>
      ${body}${l.reason && l.state === "upcoming" ? `<div class="why">${esc(l.reason)}</div>` : l.state === "unavailable" ? "" : ""}
      ${alt ? `<div class="alt">↪ Safer in this match: <b>${esc(alt.pick)}</b> @ ${f2(alt.odds)} · est ${pct(alt.est)} <span class="sub">(${esc(mktLabel(alt))})</span></div>` : ""}</div>`;
  }
  function azOut() {
    if (state.azBusy) return `<div class="card"><div class="scanning"><div class="radar"></div>Loading the code…<div class="steps">read slip → re-price every leg's market → form, H2H &amp; team news → grade</div></div>${[0, 1].map(() => `<div class="leg"><div class="sk sk-line" style="width:40%"></div><div class="sk sk-line" style="width:75%"></div><div class="sk sk-line" style="width:55%"></div></div>`).join("")}</div>`;
    if (state.azErr) return `<div class="error">${esc(state.azErr)}</div>`;
    const r = state.az; if (!r) return `<div class="empty">Paste any SportyBet NG booking code to see each leg's estimated chance, fair odds, value and risk, plus safer swaps.</div>`;
    const s = r.summary, sg = r.suggestions, imp = sg.improved;
    const counts = Object.entries(s.ratings || {}).filter(([, v]) => v).map(([k, v]) => `<span class="rchip ${RCLS[k]}">${v} ${esc(k)}</span>`).join("");
    let h = `<div class="card port t-${s.grade === "A" ? "safe" : s.grade === "B" ? "mid" : s.grade ? "risk" : "audit"}">
      <div class="head"><span class="label">${r.code ? `Code <button class="codechip" data-act="copytext" data-t="${esc(r.code)}">${esc(r.code)}</button>` : "📷 Screenshot slip"}</span><span class="pill">${s.legs} legs</span></div>
      <div class="az-sum" style="margin-top:12px"><div class="grade g-${s.grade || "none"}">${s.grade || "–"}</div><div><div class="verdict">${esc(r.verdict)}</div><div class="sub" style="margin-top:3px">Grade blends leg quality with the chance of landing</div></div></div>
      <div class="kpis"><div class="kpi"><b>${s.odds_known === false ? "n/a" : f2(s.total_odds)}</b><span>Total odds</span></div><div class="kpi"><b>${pc(s.chance)}</b><span>Est. hit chance</span></div><div class="kpi"><b>${s.rated}/${s.legs}</b><span>Rated legs</span></div></div>
      ${s.grade ? meter(s.chance) : ""}
      ${counts ? `<div class="rchips">${counts}${s.outside_top ? `<span class="rchip r-top">${s.outside_top} outside top leagues</span>` : ""}</div>` : ""}
      ${s.approx_legs ? `<div class="note">${s.approx_legs} leg(s) can't be rated (props, specials, in play): the combined chance counts them at the bookmaker's de-margined price.</div>` : ""}
      ${s.started ? `<div class="sub" style="margin-top:8px">${s.started} leg(s) started or finished; the rest can still change.</div>` : ""}
      <div class="btnrow"><button class="btn small secondary" data-act="aztake" ${state.azTakeBusy ? "disabled" : ""}>${state.azTakeBusy ? "Thinking…" : "🧠 AI take"}</button><button class="btn small secondary" data-act="opencode" data-c="${esc(r.code || "")}">${r.code ? "📲 Open in SportyBet" : "Open SportyBet"}</button></div>
      ${r.id ? `<div class="btnrow">${imgBtn({ analysis_id: r.id })}</div>` : ""}
      ${state.azTake ? `<div class="note ai-take" style="background:var(--bg2);border-color:var(--line);color:var(--text)">${esc(state.azTake)}</div>` : ""}</div>`;
    h += `<div class="section"><span class="label">Legs</span><span class="sub">est. · fair odds · value</span></div><div class="card flush">${r.legs.map(azLeg).join("")}</div>`;
    const rows = [];
    sg.drops.slice(0, 3).forEach((d) => rows.push(`<div class="sugg"><span class="ic">✂️</span><div class="grow">Drop <b>${esc(r.legs[d.idx].match.replace(" vs ", " v "))}</b> · ${esc(d.pick)} <span class="sub">(${esc(d.rating)} ${pct(d.est)})</span><div class="sub">chance ${pc(s.chance)} → <span class="up">${pc(d.chance)}</span> · odds ${f2(d.total_odds)}</div></div></div>`));
    sg.swaps.slice(0, 4).forEach((w) => rows.push(`<div class="sugg"><span class="ic">🔁</span><div class="grow"><b>${esc(w.match.replace(" vs ", " v "))}</b>: ${esc(w.from)} → <b class="c-acc">${esc(w.to)}</b><div class="sub">${pct(w.from_est)} → ${pct(w.to_est)} · slip chance <span class="up">${pc(w.chance)}</span> · odds ${f2(w.total_odds)}</div></div></div>`));
    if (rows.length) h += `<div class="section"><span class="label">Suggestions</span></div><div class="card flush">${rows.join("")}</div>`;
    if (imp) h += `<div class="card port imp t-safe"><div class="head"><span class="label">Improved slip</span><span class="pill">${imp.legs.length} legs</span></div>
      <div class="kpis"><div class="kpi"><b>${f2(imp.total_odds)}</b><span>Total odds</span></div><div class="kpi"><b class="c-acc">${pc(imp.chance)}</b><span>Est. hit chance</span></div><div class="kpi"><b>${pc(imp.base_chance)}</b><span>Was</span></div></div>
      <div class="chg">${imp.changes.map((c) => "• " + esc(c)).join("<br>")}${imp.excluded_started ? `<br>• ${imp.excluded_started} started leg(s) left out` : ""}</div>
      ${imp.bookable ? `<button class="btn" data-act="azrebook">Rebook improved slip</button>` : `<div class="note">The improved slip breaks your rules, so it can't be booked.</div>`}</div>`;
    if (r.recommendation) { const k = r.recommendation.kind; h += `<div class="card verdict-card v-${esc(k)}"><div class="label">My verdict</div><div class="vtext">${esc(r.recommendation.text)}</div></div>`; }
    h += `<div class="disc">${esc(r.disclaimer)} Team news is context only (the model doesn't use it). Analyzed codes aren't saved to History.</div>`;
    return h;
  }
  function renderAnalyze() {
    const rec = azRecent.get();
    $view.innerHTML = `<div style="margin:4px 2px 12px"><div class="label">Bet analyzer</div><h1>Check any code</h1><div class="sub">Every leg re-priced with the scanner's calibrated model · form, H2H &amp; team news</div></div>
      <div class="card"><form id="azForm" class="az-row"><div class="field"><input id="azCode" value="${esc(state.azCode)}" placeholder="N6RXGP" maxlength="40" autocomplete="off" autocapitalize="characters" spellcheck="false" enterkeyhint="go"></div><button type="button" class="paste" data-act="azpaste">Paste</button></form>
        <button class="btn" data-act="azgo" ${state.azBusy ? "disabled" : ""}>${state.azBusy ? "Analyzing…" : "Analyze code"}</button>
        ${rec.length ? `<div class="az-recent">${rec.map((c) => `<button class="chip" data-act="azrecent" data-c="${esc(c)}" style="padding:5px 10px;font-size:12.5px">${esc(c)}</button>`).join("")}</div>` : ""}</div>
      <div id="azOut">${azOut()}</div>`;
    const f = document.getElementById("azForm");
    f.addEventListener("submit", (ev) => { ev.preventDefault(); runAnalyze(); });
  }
  async function runAnalyze(code) {
    const inp = document.getElementById("azCode");
    const raw = (code || (inp ? inp.value : state.azCode) || "").trim();
    if (!raw) { toast("Paste a booking code first"); return; }
    state.azCode = raw.toUpperCase(); state.azBusy = true; state.azErr = ""; state.az = null; state.azTake = null; hap("medium");
    if (inp) inp.blur();
    renderAnalyze();
    try { state.az = await api("/api/analyze", { code: raw }); azRecent.add(state.az.code); state.azCode = state.az.code; hapN("success"); }
    catch (e) { state.azErr = e.message; hapN("error"); }
    state.azBusy = false;
    if (state.tab === "analyze") renderAnalyze();
  }
  async function azPaste() {
    let t = "";
    try { if (navigator.clipboard && navigator.clipboard.readText) t = await navigator.clipboard.readText(); } catch (e) {}
    if (!t && tg && tg.readTextFromClipboard) { try { t = await new Promise((res) => tg.readTextFromClipboard((x) => res(x || ""))); } catch (e) {} }
    const m = String(t || "").match(/sharecode=([A-Za-z0-9]{5,10})/i) || String(t || "").match(/\b([A-Za-z0-9]{6})\b/);
    if (m) { state.azCode = m[1].toUpperCase(); const inp = document.getElementById("azCode"); if (inp) inp.value = state.azCode; runAnalyze(state.azCode); }
    else { toast("Clipboard is empty here: long-press the box and Paste"); const inp = document.getElementById("azCode"); if (inp) inp.focus(); }
  }
  function azRebookSheet() {
    const imp = state.az && state.az.suggestions.improved; if (!imp) return;
    openSheet(`<div class="label" style="text-align:center">Rebook improved slip</div>
      <div class="sub" style="text-align:center;margin:6px 0 10px">${imp.legs.length} legs · total odds ${f2(imp.total_odds)} · est. hit ${pc(imp.chance)}</div>
      <div class="card flush" style="max-height:40vh;overflow:auto">${imp.legs.map((l) => `<div class="row"><div class="grow"><b>${esc(l.match.replace(" vs ", " v "))}</b><div class="sub">${esc(mktLabel(l))} · ${esc(l.pick)}${l.est != null ? " · est " + pct(l.est) : ""}</div></div><span class="num" style="font-weight:800">${f2(l.odds)}</span></div>`).join("")}</div>
      <div class="btnrow"><button class="btn" data-act="azconfirm">Confirm &amp; book</button><button class="btn secondary" data-act="closesheet">Cancel</button></div>
      <div class="disc">Creates a new SportyBet booking code (free, no money). Estimates, not guarantees. 18+.</div>`);
  }
  async function azConfirm() {
    openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div><div class="scanning"><div class="radar"></div>Creating your code…</div>`);
    try {
      const r = await api("/api/analyze/rebook", { id: state.az.id });
      state.hist = null; store.set("hist", null); hapN("success");
      openSheet(`<div class="label" style="text-align:center">Improved slip booked</div><div class="code" id="codeText">${esc(r.code)}</div>
        <div class="sub" style="text-align:center">${r.legs} legs · total odds ${f2(r.total_odds)} · est. hit ${pc(r.chance)}${r.loaded != null && r.loaded !== r.legs ? ` · ⚠ ${r.loaded}/${r.legs} loaded` : ""}</div>
        <div class="btnrow"><button class="btn" data-act="copycode">Copy code</button><button class="btn secondary" data-act="opencode" data-c="${esc(r.code)}">📲 Open in SportyBet</button></div>
        <div class="btnrow">${imgBtn(r.history_id ? { history_id: r.history_id } : { code: r.code })}</div>
        <div class="sub" style="text-align:center;margin-top:8px">${r.history_id ? "Saved to History · you'll get a Telegram ping when it lands" : ""}</div>`);
    } catch (e) { hapN("error"); openSheet(`<div class="label" style="text-align:center">Rebook failed</div><div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="closesheet">Close</button>`); }
  }
  async function azTake() {
    if (!state.az || state.azTakeBusy) return;
    state.azTakeBusy = true; renderAnalyze();
    try { const r = await api("/api/analyze/narrative", { id: state.az.id }); state.azTake = r.text; } catch (e) { toast(e.message); }
    state.azTakeBusy = false; if (state.tab === "analyze") renderAnalyze();
  }

  // ---------------------------------------------------------------- HISTORY
  function fmtWhen(iso) { try { return new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Lagos" }); } catch (e) { return ""; } }
  function statusPill(st) { const [i, t, c] = STATUS[st] || STATUS.pending; return `<span class="spill ${c}">${i ? `<i>${i}</i>` : ""}${t}</span>`; }
  function histLeg(l) {
    const [ic, , cls] = STATUS[l.status] || STATUS.pending;
    return `<div class="hleg"><span class="sicon ${cls}">${ic || "·"}</span><div class="grow">
        <div class="lg-meta">${flag(l)}<span class="ellip">${esc(leagueShort(l.league))}</span><span class="dot">·</span><span class="num">${esc(l.kickoff ? kickDay(l.kickoff) + " " + kickTime(l.kickoff) : l.kickoff_local || "")}</span></div>
        ${teamsBlock(l, "sm")}
        <div class="mkt"><span class="ellip">${esc(mktLabel(l))}</span> <span class="pick">${esc(l.pick)}</span></div>
        ${l.score || l.note ? `<div class="score">${esc(l.score)}${l.note ? ` <span class="sub">${esc(l.note)}</span>` : ""}</div>` : ""}</div>
      <div class="hodds num">${f2(l.odds)}${l.est != null ? `<small>${pct(l.est)}</small>` : ""}</div></div>`;
  }
  function openChip(c) { return ` <button class="codechip open" data-act="opencode" data-c="${esc(c)}" aria-label="Open ${esc(c)} in the SportyBet app">📲 Open</button>`; }
  function histCard(r) {
    const d = r.data, legs = d.legs || [];
    const won = legs.filter((l) => l.status === "won").length, lost = legs.filter((l) => l.status === "lost").length, live = legs.filter((l) => l.status === "live").length;
    const open = !!state.histOpen[r.id];
    const early = legs.filter((l) => l.early && l.early.min).map((l) => l.early.min);
    const tag = (d.rollover_id ? `Rollover · day ${d.rollover_day}` : (d.label || d.tier || "Slip")) + (early.length && r.status === "won" ? ` · won early (${Math.max(...early)}')` : "");
    const seg = legs.map((l) => `<i class="${(STATUS[l.status] || STATUS.pending)[2]}"></i>`).join("");
    return `<div class="card hcard s-${r.status}">
      <div class="hhead"><div class="grow"><div class="label">${esc(tag)}${d.test ? " · test" : ""}</div><div class="hcode">${d.code ? `<button class="codechip" data-act="copytext" data-t="${esc(d.code)}">${esc(d.code)}</button>${r.status === "pending" ? openChip(d.code) : ""}` : `<span class="sub">no code</span>`}<span class="sub">${esc(fmtWhen(r.created_at))}</span></div></div>
        <div class="hright"><div class="num hbig">${f2(d.effective_odds || d.total_odds)}</div>${statusPill(r.status)}</div></div>
      <div class="segbar">${seg}</div>
      <div class="hstats"><span>${won}/${legs.length} won</span>${lost ? `<span class="c-red">${lost} lost</span>` : ""}${live ? `<span class="c-live">${live} live</span>` : ""}${d.est_prob != null ? `<span>est ${pct(d.est_prob, 1)}</span>` : ""}
        <button class="linkbtn" data-act="slipimg" data-src="${imgSrc({ history_id: r.id })}">🖼 Image</button><button class="linkbtn" data-act="histtoggle" data-id="${esc(r.id)}">${open ? "Hide legs" : "Show legs"}</button></div>
      ${open ? `<div class="hlegs">${legs.map(histLeg).join("")}</div>` : ""}</div>`;
  }
  async function loadHistory(force) {
    if (state.hist && !force) return;
    state.histErr = "";
    try { state.hist = await api("/api/history?limit=60"); store.set("hist", { at: Date.now(), json: state.hist }); state.histFresh = Date.now(); } catch (e) { if (!state.hist) state.histErr = e.message; }
  }
  async function renderHistory() {
    let h = `<div style="margin:4px 2px 12px" class="hrow"><div><div class="label">Tracked codes</div><h1>History</h1><div class="sub">Results every 2–5 min · early wins locked from the live score · Telegram ping when a slip lands</div></div><button class="iconbtn" data-act="histrefresh" aria-label="Refresh">↻</button></div>`;
    if (!state.hist) { const c = store.get("hist"); if (c) state.hist = c.json; }   // instant: last copy on this phone
    if (!state.hist && !state.histErr) { $view.innerHTML = h + `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`; await loadHistory(); if (state.tab !== "history") return; }
    else if (!state.histFresh || Date.now() - state.histFresh > 60000) { state.histFresh = Date.now(); loadHistory(true).then(() => { if (state.tab === "history") { const y = window.scrollY; renderHistory().then(() => window.scrollTo(0, y)); } }); }
    if (state.histErr) { $view.innerHTML = h + `<div class="error">${esc(state.histErr)}</div><button class="btn secondary" data-act="histrefresh">Try again</button>`; return; }
    const all = state.hist.slips || [];
    const c = (k) => all.filter((r) => r.status === k).length;
    const done = c("won") + c("lost");
    h += `<div class="kpis k4"><div class="kpi"><b class="c-acc">${c("won")}</b><span>Won</span></div><div class="kpi"><b class="c-red">${c("lost")}</b><span>Lost</span></div><div class="kpi"><b>${c("pending")}</b><span>Running</span></div><div class="kpi"><b>${done ? Math.round((c("won") / done) * 100) + "%" : "–"}</b><span>Hit rate</span></div></div>`;
    const ro = state.hist.rollover;
    if (ro && ro.status === "active") h += `<button class="card rocta t-safe" data-act="tab" data-t="rollover"><span class="label">Rollover running</span><b>Day ${Math.min(ro.data.current_day, ro.data.days)}/${ro.data.days} · ${ro.data.target}× a day</b><span class="chev">›</span></button>`;
    h += `<div class="seg" style="margin:12px 0 10px">${[["all", "All"], ["pending", "Running"], ["won", "Won"], ["lost", "Lost"]].map(([k, t]) => `<button data-act="histfilter" data-f="${k}" class="${state.histFilter === k ? "on" : ""}">${t}</button>`).join("")}</div>`;
    const rows = state.histFilter === "all" ? all : all.filter((r) => r.status === state.histFilter || (state.histFilter === "pending" && r.status === "check"));
    h += rows.length ? rows.map(histCard).join("") : `<div class="empty">${all.length ? "Nothing here yet." : "No booked codes yet. Book a slip from the Scanner, the AI tab or your Slip and it shows up here."}</div>`;
    h += `<div class="disc">Settled from official match results (regular time). Corners/bookings use match stats; player props and specials show “Check”. Estimates, not guarantees. 18+.</div>`;
    $view.innerHTML = h;
  }

  // ---------------------------------------------------------------- ROLLOVER
  async function loadRollover(force) {
    if (state.ro && !force) return;
    state.roErr = "";
    try { state.ro = await api("/api/rollover"); store.set("ro", { at: Date.now(), json: state.ro }); state.roFresh = Date.now(); } catch (e) { if (!state.ro) state.roErr = e.message; }
  }
  function roSteps(d) {
    const byDay = {}; (d.log || []).forEach((x) => { if (["won", "lost", "pending", "check"].includes(x.status) || !byDay[x.day]) byDay[x.day] = x; });
    let h = `<div class="steps-ro">`;
    for (let i = 1; i <= d.days; i++) {
      const x = byDay[i]; const st = x ? x.status : i === d.current_day ? "next" : "todo";
      h += `<div class="st st-${st}${i === d.current_day ? " cur" : ""}"><span>${st === "won" ? "✓" : st === "lost" ? "✕" : i}</span></div>`;
    }
    return h + `</div>`;
  }
  function ticketsHtml(tix, d) {
    if (!tix.length) return "";
    let h = `<div class="section"><span class="label">Tickets</span><span class="sub">${tix.filter((t) => t.status !== "replaced").length} day${tix.length > 1 ? "s" : ""}${d && d.stake ? " · bankroll " + Number(d.value || 0).toLocaleString("en-GB", { maximumFractionDigits: 0 }) : ""}</span></div>`;
    for (const t of tix.slice().reverse()) {
      const open = !!state.histOpen["t:" + (t.slip_id || t.day + t.date + t.status)];
      const legs = t.legs || [];
      const seg = legs.map((l) => `<i class="${(STATUS[l.status] || STATUS.pending)[2]}"></i>`).join("");
      const odds = t.effective_odds || t.total_odds;
      h += `<div class="card hcard tix s-${t.status}${t.status === "replaced" ? " dim" : ""}">
        <div class="hhead"><div class="grow"><div class="label">Day ${t.day} · ${esc(t.date || "")}${t.refreshed ? " · refreshed" : ""}${t.won_early ? ` · won early (${t.won_early}')` : ""}</div>
          <div class="hcode">${t.code ? `<button class="codechip" data-act="copytext" data-t="${esc(t.code)}">${esc(t.code)}</button>${t.status === "pending" || t.status === "live" ? openChip(t.code) : ""}` : `<span class="sub">${t.status === "no_slip" || t.status === "skipped" ? esc((t.note || "").slice(0, 70)) : "no code"}</span>`}</div></div>
          <div class="hright">${odds ? `<div class="num hbig">${f2(odds)}</div>` : ""}${statusPill(t.status)}</div></div>
        ${legs.length ? `<div class="segbar">${seg}</div>` : ""}
        <div class="hstats">${t.stake != null && t.status !== "replaced" ? `<span>Stake ${Math.round(t.stake).toLocaleString("en-GB")} → ${Math.round(t.expected_return).toLocaleString("en-GB")}</span>` : ""}${t.bankroll != null && ["won", "lost", "void"].includes(t.status) ? `<span>Bankroll ${Math.round(t.bankroll).toLocaleString("en-GB")}</span>` : ""}${t.status === "replaced" ? `<span>Replaced by a refresh · not a loss</span>` : ""}
          ${legs.length && t.slip_id ? `<button class="linkbtn" data-act="slipimg" data-src="${imgSrc({ history_id: t.slip_id })}">🖼 Image</button>` : ""}${legs.length ? `<button class="linkbtn" data-act="histtoggle" data-id="t:${esc(t.slip_id || t.day + t.date + t.status)}" data-ro="1">${open ? "Hide legs" : `${legs.filter((l) => l.status === "won").length}/${legs.length} legs`}</button>` : ""}</div>
        ${open ? `<div class="hlegs">${legs.map(histLeg).join("")}</div>` : ""}</div>`;
    }
    return h;
  }
  function renderPlan() {
    const p = state.roPlan, d = p.plan.data;
    let h = `<button class="back" data-act="roplanback">‹ Rollover</button><div style="margin:4px 2px 12px"><div class="label">Plan · ${esc(fmtWhen(p.plan.created_at))}</div><h1>${d.target}× · ${d.days} days</h1><div class="sub">${esc({ completed: "Completed", busted: "Busted", stopped: "Stopped", active: "Running" }[p.plan.status] || p.plan.status)} · reached day ${Math.min(d.current_day, d.days)}${d.stake ? ` · stake ${Number(d.stake).toLocaleString("en-GB")} → ${Math.round(p.plan.status === "busted" ? 0 : d.value || 0).toLocaleString("en-GB")}` : ""}</div></div>`;
    h += roSteps(d) + ticketsHtml(p.tickets, null);
    if (!p.tickets.length) h += `<div class="empty">No tickets in this plan.</div>`;
    h += `<div class="disc">Estimates, not guarantees. 18+.</div>`;
    $view.innerHTML = h;
  }
  async function openPlan(id) {
    state.roPlan = { plan: (state.ro.past || []).find((r) => r.id === id) || { data: {} }, tickets: [], loading: true };
    $view.innerHTML = `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`;
    try { const r = await api("/api/rollover/tickets?id=" + encodeURIComponent(id)); state.roPlan = r; renderPlan(); window.scrollTo(0, 0); }
    catch (e) { state.roPlan = null; toast(e.message); renderRollover(); }
  }
  async function refreshPicks(id, different, booked) {
    if (state.roPicking) return;
    state.roPicking = true; hap("medium"); renderRollover(); toast("Re-reading today's top-league prices… ~30–60 s");
    try {
      const r = await api("/api/rollover", { action: "refresh", id, exclude_prev: different });
      if (r.status === "preview") {
        const s = r.slip;
        openSheet(`<div class="label" style="text-align:center">New picks · preview</div>
          <div class="big num" style="text-align:center">${f2(s.total_odds)}<small> total odds · est ${pct(s.est_prob, 1)}</small></div>
          <div class="note">Your current slip is booked as <b>${esc(r.old_code)}</b>. Confirm to book a new code; the old one is marked replaced (not a loss) and stops being tracked.</div>
          <div class="hlegs">${(s.legs || []).map(histLeg).join("")}</div>
          <div class="btnrow"><button class="btn" data-act="roconfirm" data-t="${esc(r.token)}">Confirm: book new code</button><button class="btn secondary" data-act="closesheet">Keep current</button></div>
          <div class="btnrow">${imgBtn({ token: r.token }, "🖼 Preview image")}</div>`);
      } else if (r.status === "replaced") { hapN("success"); toast("Picks refreshed · old slip marked replaced"); }
      else { hapN("warning"); toast(r.note || "No different slip"); }
    } catch (e) { hapN("error"); toast(e.message); }
    state.roPicking = false; state.ro = null; state.hist = null; store.set("ro", null); store.set("hist", null);
    if (state.tab === "rollover") renderRollover();
  }
  async function renderRollover() {
    if (state.roPlan && !state.roPlan.loading) { renderPlan(); return; }
    let h = `<div style="margin:4px 2px 12px" class="hrow"><div><div class="label">Daily compounding plan</div><h1>Rollover</h1><div class="sub">Top leagues · stats-confirmed legs ≥80% each</div></div><button class="iconbtn" data-act="rorefresh" aria-label="Refresh">↻</button></div>`;
    if (!state.ro && !state.roBusy) { const c = store.get("ro"); if (c) state.ro = c.json; }
    if (!state.ro && !state.roErr) { $view.innerHTML = h + `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`; await loadRollover(); if (state.tab !== "rollover") return; }
    else if (!state.roBusy && (!state.roFresh || Date.now() - state.roFresh > 60000)) { state.roFresh = Date.now(); loadRollover(true).then(() => { if (state.tab === "rollover" && !state.roBusy) renderRollover(); }); }
    if (state.roErr) { $view.innerHTML = h + `<div class="error">${esc(state.roErr)}</div><button class="btn secondary" data-act="rorefresh">Try again</button>`; return; }
    const act = state.ro.active;
    if (state.roBusy) h += `<div class="card"><div class="scanning"><div class="radar"></div>Scanning today's top-league matches…<div class="steps">every market → stats check → fewest strong legs to the target</div></div></div>`;
    if (act) {
      const d = act.data, n = Math.min(d.current_day, d.days);
      h += `<div class="card port t-safe"><div class="head"><span class="label">Day ${n} of ${d.days}</span><span class="pill">${d.target}× a day${d.allow_unders ? "" : " · no unders"}</span></div>
        <div class="big num">${d.value ? Number(d.value).toLocaleString("en-GB", { maximumFractionDigits: 0 }) : f2(Math.pow(d.target, Math.max(0, d.current_day - 1)))}<small>${d.value ? "stake value" : "odds compounded"}</small></div>
        ${roSteps(d)}
        <div class="kpis"><div class="kpi"><b>${d.stake ? Number(d.stake).toLocaleString("en-GB") : "–"}</b><span>Start stake</span></div><div class="kpi"><b>${d.stake ? Math.round(d.stake * Math.pow(d.target, d.days)).toLocaleString("en-GB") : f2(Math.pow(d.target, d.days))}</b><span>If all ${d.days} land</span></div><div class="kpi"><b>${(d.log || []).filter((x) => x.status === "won").length}</b><span>Days won</span></div></div>
        <div class="btnrow"><button class="btn small" data-act="ronow" ${state.roBusy ? "disabled" : ""}>Build today's slip</button><button class="btn small secondary" data-act="roskip">Skip today</button></div>
        <button class="btn ghost" data-act="rostop">Stop plan</button></div>`;
      const ds = state.ro.day_slip;
      if (ds) {
        const dd = ds.data;
        h += `<div class="section"><span class="label">Day ${dd.rollover_day} slip</span>${statusPill(ds.status)}</div>
          <div class="card"><div class="hhead"><div class="grow">${dd.code ? `<button class="codechip" data-act="copytext" data-t="${esc(dd.code)}">${esc(dd.code)}</button>${ds.status === "pending" ? openChip(dd.code) : ""}` : `<button class="btn small" style="margin:0;width:auto;padding:0 14px" data-act="robook" data-id="${esc(ds.id)}">Book code</button>`}</div><div class="hright"><div class="num hbig">${f2(dd.total_odds)}</div><span class="sub">est ${pct(dd.est_prob, 1)}</span></div></div>
          <div class="hlegs">${(dd.legs || []).map(histLeg).join("")}</div>
          <div class="btnrow">${imgBtn({ history_id: ds.id })}</div>
          ${ds.can_refresh ? `<div class="btnrow"><button class="btn small secondary" data-act="ropicks" data-id="${esc(ds.id)}" data-x="0" ${state.roPicking ? "disabled" : ""}>${state.roPicking ? "Refreshing…" : "🔄 Refresh picks"}</button><button class="btn small secondary" data-act="ropicks" data-id="${esc(ds.id)}" data-x="1" ${state.roPicking ? "disabled" : ""}>🔀 Different picks</button></div><button class="btn ghost" data-act="aiedit" data-kind="rollover" data-id="${esc(ds.id)}" data-day="${esc(dd.rollover_day)}">✏️ Edit with AI</button><div class="sub" style="text-align:center;margin-top:6px">Same rules · only before the first kick-off${dd.code ? " · a new code only after you confirm" : ""}</div>` : ""}</div>`;
      }
      h += ticketsHtml(state.ro.tickets || [], d);
      const notes = (d.log || []).filter((x) => x.status === "no_slip" || x.status === "skipped").slice(-3);
      if (notes.length) h += notes.map((x) => `<div class="note">${x.status === "skipped" ? "⏭" : "⏸"} Day ${x.day} · ${esc(x.date)}: ${esc(x.note)}</div>`).join("");
    } else {
      const f = state.roForm;
      h += `<div class="card t-safe"><div class="label">Daily target odds</div>
        <div class="seg" style="margin-top:8px">${[2, 3, 4, 5].map((t) => `<button data-act="rotarget" data-v="${t}" class="${f.target === t ? "on" : ""}">${t}×</button>`).join("")}</div>
        <div class="label" style="margin-top:14px">Days</div>
        <div class="stepper"><button data-act="rodays" data-v="-1" aria-label="Fewer days">−</button><b class="num">${f.days}</b><button data-act="rodays" data-v="1" aria-label="More days">+</button><span class="sub">3–15 days</span></div>
        <div class="target"><div class="field"><span>Starting stake (optional)</span><input id="rostake" inputmode="decimal" value="${esc(f.stake)}" placeholder="₦" autocomplete="off"></div></div>
        <div class="chips fchips" style="margin-top:12px"><button class="chip${f.allowUnders ? " on" : ""}" data-act="rounders" aria-pressed="${f.allowUnders}">${f.allowUnders ? "✓ Unders allowed" : "No unders (tap to allow)"}</button></div>
        <div class="rules">Each morning (~09:00 WAT) I build that day's slip from the top leagues (EPL, LaLiga, Serie A, Bundesliga, Ligue 1, UEFA cups, Eredivisie, Primeira Liga, Championship, Scottish Prem, Süper Lig, Pro League, MLS, Brasileirão, Argentina…) using only stats-confirmed legs at ≥80% est.${f.allowUnders ? "" : ", no under selections"}, as close to ${f.target}× as possible. A win moves you to the next day, a loss ends the plan. If nothing qualifies I'll suggest skipping the day. All ${f.days} days at ${f.target}× = ${f2(Math.pow(f.target, f.days))}× overall.</div>
        <button class="btn" data-act="rostart" ${state.roBusy ? "disabled" : ""}>Start rollover</button></div>`;
    }
    const past = (state.ro.past || []).filter((r) => !act || r.id !== act.id);
    if (past.length) h += `<div class="section"><span class="label">Past plans</span><span class="sub">tap for tickets</span></div><div class="card flush">${past.map((r) => `<div class="row" data-act="roplan" data-id="${esc(r.id)}" role="button"><div class="grow"><b>${r.data.target}× · ${r.data.days} days</b><div class="sub">${esc(fmtWhen(r.created_at))} · reached day ${Math.min(r.data.current_day, r.data.days)}</div></div><span class="spill ${{ completed: "st-won", busted: "st-lost" }[r.status] || "st-void"}">${esc({ completed: "Completed", busted: "Busted", stopped: "Stopped" }[r.status] || r.status)}</span></div>`).join("")}</div>`;
    h += `<div class="disc">Each leg is an estimate, not a guarantee; a rollover multiplies the risk of every day. 18+. Stake only what you can afford to lose.</div>`;
    $view.innerHTML = h;
  }
  async function roAction(body, busy) {
    if (busy) { state.roBusy = true; renderRollover(); }
    try { const r = await api("/api/rollover", body); hapN("success"); if (r.day && r.day.status === "no_slip") toast("No slip met the bar today: skip suggested"); else if (r.day && r.day.status === "waiting") toast(r.day.note); }
    catch (e) { hapN("error"); toast(e.message); }
    state.roBusy = false; state.ro = null; state.hist = null; store.set("ro", null); store.set("hist", null);
    if (state.tab === "rollover") renderRollover();
  }

  // ---------------------------------------------------------------- SLIP
  function renderSlip() {
    const items = slip.get();
    const total = slip.total(items), prob = slip.prob(items);
    let h = `<div style="margin:4px 2px 12px"><div class="label">Portfolio</div><h1>Your slip</h1></div>`;
    if (!items.length) { $view.innerHTML = h + `<div class="card"><div class="empty">No positions yet. Generate a slip in the Scanner or tap a price on any match.</div><button class="btn" data-act="tab" data-t="home">Open scanner</button></div>`; return; }
    const now = Date.now();
    h += `<div class="card port t-safe"><div class="head"><span class="label">${items.length} position${items.length > 1 ? "s" : ""}</span><span class="pill">one pick per match</span></div>
      <div class="big num">${f2(total)}<small>total odds</small></div>
      <div class="kpis"><div class="kpi"><b>${prob == null ? "–" : pct(prob, 1)}</b><span>Est. hit chance</span></div><div class="kpi"><b>${items.length}</b><span>Legs</span></div><div class="kpi"><b>${items.filter((p) => p.start && Date.parse(p.start) <= now).length}</b><span>Started</span></div></div>
      ${meter(prob)}
      <button class="btn" data-act="bookmine">Book on SportyBet</button><div class="btnrow">${imgBtn({ slip: { label: "My slip", legs: items.map((p) => ({ pick: p.label, odds: p.odds, est: p.est, home: p.home, away: p.away, match: p.match, home_uid: p.home_uid, away_uid: p.away_uid, market: p.market, league: p.league, category: p.category, kickoff: p.start })) } }, "🖼 Share image (preview)")}</div><button class="btn ghost" data-act="clear">Clear slip</button></div>
      <div class="card flush">${items.map((p) => {
        const started = p.start && Date.parse(p.start) <= now;
        const weak = p.est != null && p.est < 0.8;
        return `<div class="leg"><div class="lg-meta">${flag(p)}<span class="ellip">${esc(leagueShort(p.league) || "")}</span><span class="dot">·</span><span class="num">${esc(p.start ? kickDay(p.start) + " " + kickTime(p.start) : p.when || "")}</span>${started ? '<span class="vchip v-lost">Started</span>' : ""}${weak ? '<span class="vchip v-demoted">Below Safe bar</span>' : ""}</div>
          <div class="top">${teamsBlock(p, "grow")}<div class="tick ${tierOfOdds(p.odds)}">${f2(p.odds)}</div><button class="xbtn" data-act="remove" data-id="${esc(p.event_id)}" aria-label="Remove">✕</button></div>
          <div class="mkt">${p.market ? `<span class="ellip">${esc(p.market)}</span> ` : ""}<span class="pick">${esc(p.label)}</span>${p.est != null ? ` · est ${pct(p.est)}` : ""}</div></div>`;
      }).join("")}</div><div class="disc">Prices are from when each pick was added; SportyBet shows the live price when you load the code. Estimates, not guarantees. 18+.</div>`;
    $view.innerHTML = h;
  }

  // ---------------------------------------------------------------- events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-act]"); if (!t) return;
    const a = t.dataset.act;
    if (!["scan", "bookslip", "addleg", "addpred", "bookmine", "closesheet"].includes(a)) hapSel();   // light tick on every tap
    if (a === "tab") setTab(t.dataset.t);
    else if (a === "tier") { state.tier = t.dataset.t; if (TIERS[state.tier].target) state.target = TIERS[state.tier].target; hapSel(); renderHome(); }
    else if (a === "day") { state.day = t.dataset.d; hapSel(); renderHome(); }
    else if (a === "quick") { state.target = Number(t.dataset.q); hapSel(); renderHome(); }
    else if (a === "scan") runScan();
    else if (a === "excl") { const k = t.dataset.k; state.ex = state.ex.includes(k) ? state.ex.filter((x) => x !== k) : state.ex.concat([k]); try { localStorage.setItem("fra_ex", JSON.stringify(state.ex)); } catch (e) {} hapSel(); renderHome(); }
    else if (a === "rounders") { const si = document.getElementById("rostake"); if (si) state.roForm.stake = si.value; state.roForm.allowUnders = !state.roForm.allowUnders; hapSel(); renderRollover(); }
    else if (a === "azgo") runAnalyze();
    else if (a === "azpaste") azPaste();
    else if (a === "azrecent") { state.azCode = t.dataset.c; runAnalyze(t.dataset.c); }
    else if (a === "azrebook") { hap("medium"); azRebookSheet(); }
    else if (a === "azconfirm") azConfirm();
    else if (a === "aztake") azTake();
    else if (a === "alllg") { state.allLg = !state.allLg; try { localStorage.setItem("fra_alllg", state.allLg ? "1" : "0"); } catch (e) {} hapSel(); renderHome(); }
    else if (a === "viewslip") { state.viewSlip = t.dataset.k; renderHome(); window.scrollTo(0, 0); }
    else if (a === "auditback") { state.viewSlip = null; renderHome(); }
    else if (a === "bookslip") {
      const k = t.dataset.k; const legs = legsFor(k);
      let s = k === "scan" ? state.scan : k.startsWith("audit:") ? state.scan.slips[k.slice(6)] : (state.chat[Number(k.slice(5))] || {}).slip;
      hap("medium"); bookLegs(legs, s ? s.total_odds : legs.reduce((x, l) => x * l.odds, 1), s ? s.est_prob : null, s && s.slip_id);
    }
    else if (a === "slipall") { const legs = legsFor(t.dataset.k); slip.save(legs.map(legToSlip)); hapN("success"); toast(`Slip replaced with ${legs.length} legs`); }
    else if (a === "addleg") {
      const l = legsFor(t.dataset.k)[Number(t.dataset.i)]; if (!l) return;
      if (slip.has(l.event_id, l.market_id, l.outcome_id, l.specifier)) { slip.remove(l.event_id); toast("Removed from slip"); }
      else { const rep = slip.add(legToSlip(l)); toast(`${rep ? "Replaced" : "Added"}: ${l.pick} @ ${f2(l.odds)}`); }
      hap("medium"); t.classList.toggle("sel");
    }
    else if (a === "bookmine") { const it = slip.get(); bookLegs(it, slip.total(it), slip.prob(it)); }
    else if (a === "remove") { slip.remove(t.dataset.id); hap("light"); renderSlip(); }
    else if (a === "clear") { const doIt = () => { slip.clear(); renderSlip(); toast("Slip cleared"); }; if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm("Clear all picks?", (ok) => ok && doIt()); else if (confirm("Clear all picks?")) doIt(); }
    else if (a === "closesheet") closeSheet();
    else if (a === "slipimg") { let src = null; try { src = JSON.parse(t.dataset.src || "null"); } catch (e) {} if (src) slipImage(src); }
    else if (a === "imgsend") imgSend(t);
    else if (a === "imgshare") imgShare(t);
    else if (a === "imgsave") imgSave(t);
    else if (a === "copycode") { const c = document.getElementById("codeText").textContent; toast((await copy(c)) ? "Code copied" : "Press and hold the code to copy"); hap("light"); }
    else if (a === "copytext" || a === "copyinline") { const c = t.dataset.t || t.textContent; toast((await copy(c)) ? `Copied ${c}` : "Couldn't copy"); hap("light"); }
    else if (a === "openhub") openLink(LOAD_URL);
    else if (a === "opencode") { hap("light"); openLink(codeUrl(t.dataset.c)); }
    else if (a === "mrange") { saveScroll(); stopLive(); state.mRange = t.dataset.r; renderLeagues(); restoreScroll(); }
    else if (a === "livematch") { saveScroll(); state.lm = null; state.mStack.push({ name: "live", arg: t.dataset.id }); render(); window.scrollTo(0, 0); }
    else if (a === "openurl") openLink(t.dataset.u);
    else if (a === "mretry") renderLeagues();
    else if (a === "league") { saveScroll(); state.mStack.push({ name: "league", arg: t.dataset.id }); render(); window.scrollTo(0, 0); }
    else if (a === "match") { if (state.tab !== "matches") { state.tab = "matches"; document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === "matches")); } saveScroll(); const c = state.mStack[state.mStack.length - 1]; if (!(c && c.name === "match" && c.arg === t.dataset.id)) state.mStack.push({ name: "match", arg: t.dataset.id }); render(); window.scrollTo(0, 0); }
    else if (a === "mback") goBack();
    else if (a === "addpred") addPred(t.dataset.m, t.dataset.o);
    else if (a === "suggest") askAI(t.textContent);
    else if (a === "aiedit") { state.aiCtx = t.dataset.kind === "rollover" ? { kind: "rollover", slip_id: t.dataset.id, label: `Rollover day ${t.dataset.day || ""} slip` } : { kind: "scan", sid: t.dataset.sid, label: "Scanner slip" }; setTab("ai"); }
    else if (a === "aictxclear") { state.aiCtx = null; renderAI(); }
    else if (a === "aiattach") { const f = document.getElementById("aiFile"); if (f) f.click(); }
    else if (a === "aiimgclear") { state.aiImg = null; renderAI(); }
    else if (a === "aiaz") { const m = state.chat[Number(t.dataset.k)]; if (m && m.analysis) { state.az = m.analysis; state.azErr = ""; state.azTake = null; state.azCode = m.analysis.code || ""; setTab("analyze"); } }
    else if (a === "aikeep") { toast("Kept your current slip"); }
    else if (a === "histrefresh") { state.hist = null; state.histFresh = 0; store.set("hist", null); renderHistory(); }
    else if (a === "histfilter") { state.histFilter = t.dataset.f; hapSel(); renderHistory(); }
    else if (a === "histtoggle") { state.histOpen[t.dataset.id] = !state.histOpen[t.dataset.id]; if (t.dataset.ro) renderRollover(); else renderHistory(); }
    else if (a === "rorefresh") { state.ro = null; state.roFresh = 0; store.set("ro", null); renderRollover(); }
    else if (a === "rotarget") { state.roForm.target = Number(t.dataset.v); hapSel(); renderRollover(); }
    else if (a === "rodays") { const inp = document.getElementById("rostake"); if (inp) state.roForm.stake = inp.value; state.roForm.days = Math.max(3, Math.min(15, state.roForm.days + Number(t.dataset.v))); hapSel(); renderRollover(); }
    else if (a === "rostart") { const inp = document.getElementById("rostake"); const st = inp ? parseFloat(inp.value.replace(/[,₦\s]/g, "")) : NaN; roAction({ action: "start", target: state.roForm.target, days: state.roForm.days, stake: isNaN(st) ? null : st, allow_unders: !!state.roForm.allowUnders }, true); }
    else if (a === "ronow") roAction({ action: "now" }, true);
    else if (a === "roskip") roAction({ action: "skip" });
    else if (a === "rostop") { const doIt = () => roAction({ action: "stop" }); if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm("Stop this rollover plan?", (ok) => ok && doIt()); else if (confirm("Stop this rollover plan?")) doIt(); }
    else if (a === "scanrefresh") refreshScan(t.dataset.x === "1");
    else if (a === "ropicks") refreshPicks(t.dataset.id, t.dataset.x === "1");
    else if (a === "roconfirm") {
      t.disabled = true; t.textContent = "Booking…";
      try { const r = await api("/api/rollover", { action: "refresh_confirm", token: t.dataset.t }); hapN("success"); state.hist = null; store.set("hist", null);
        openSheet(`<div class="label" style="text-align:center">New booking code</div><div class="code" id="codeText">${esc(r.code)}</div><div class="sub" style="text-align:center">${esc(r.old_code || "")} is marked replaced · not a loss</div><div class="btnrow"><button class="btn" data-act="copycode">Copy code</button><button class="btn secondary" data-act="opencode" data-c="${esc(r.code)}">📲 Open in SportyBet</button></div>`); }
      catch (e) { hapN("error"); openSheet(`<div class="label" style="text-align:center">Refresh failed</div><div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="closesheet">Close</button>`); }
      state.ro = null; store.set("ro", null); if (state.tab === "rollover") renderRollover();
    }
    else if (a === "roplan") openPlan(t.dataset.id);
    else if (a === "roplanback") { state.roPlan = null; renderRollover(); window.scrollTo(0, 0); }
    else if (a === "robook") { try { const r = await api("/api/history/book", { id: t.dataset.id }); hapN("success"); toast(`Code ${r.code} created`); } catch (e) { toast(e.message); } state.ro = null; renderRollover(); }
  });
  document.querySelectorAll(".tabbtn").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.tab === state.tab && state.tab === "matches" && state.mStack.length) { stopLive(); state.mStack = []; render(); restoreScroll(); hapSel(); return; }
    setTab(b.dataset.tab);
  }));

  badge();
  if (!initData) {
    $view.innerHTML = `<div class="card" style="margin-top:20px;text-align:center"><img src="/app/logo.png" width="64" height="64" style="border-radius:16px"><h2 style="margin-top:12px">Fra Prediction</h2><div class="sub" style="margin-top:6px">Open this app from @FraPredictbot in Telegram (menu button or /start).</div></div>`;
    return;
  }
  const hm = location.hash.match(/^#analyze(?:=([A-Za-z0-9]{5,10}))?$/);
  if (hm) { state.tab = "analyze"; document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === "analyze")); if (hm[1]) { state.azCode = hm[1].toUpperCase(); setTimeout(() => runAnalyze(state.azCode), 0); } }
  else if (location.hash === "#history" || location.hash === "#rollover" || location.hash === "#matches") { state.tab = location.hash.slice(1); document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab)); }
  render();
  // warm the other tabs in the background so switching is instant (cached copy first, then a quiet refresh)
  idle(() => {
    loadEvents("today").catch(() => {});
    loadLive().catch(() => {});
    setTimeout(() => { if (!state.hist) loadHistory().catch(() => {}); }, 1200);
  });
})();
