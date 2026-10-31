(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = (tg && tg.initData) || "";
  if (tg) {
    tg.ready(); tg.expand();
    try { tg.setHeaderColor("#070B14"); tg.setBackgroundColor("#070B14"); if (tg.setBottomBarColor) tg.setBottomBarColor("#090E19"); } catch (e) {}
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
  }
  const LOAD_URL = "https://www.sportybet.com/ng/m/code-hub/load-code";
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
    chat: [], aiBusy: false,
    hist: null, histErr: "", histFilter: "all", histOpen: {}, ro: null, roErr: "", roBusy: false, roForm: { target: 2, days: 7, stake: "", allowUnders: false },
    ex: (() => { try { return JSON.parse(localStorage.getItem("fra_ex") || "[]"); } catch (e) { return []; } })(),
  };
  const EXCL = [["under", "No unders"], ["corners", "No corners"], ["cards", "No cards"], ["player_props", "No player props"], ["handicap", "No handicaps"], ["combo", "No combos"]];

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const f2 = (o) => (o ? Number(o).toFixed(2) : "–");
  const pct = (p, d = 0) => (p == null ? "–" : (p * 100).toFixed(d) + "%");
  const hap = (t) => { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(t || "light"); } catch (e) {} };
  const hapN = (t) => { try { tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(t || "success"); } catch (e) {} };
  const hapSel = () => { try { tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged(); } catch (e) {} };
  let toastT;
  function toast(m) { $toast.textContent = m; $toast.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => $toast.classList.add("hidden"), 2300); }
  function clock() { try { document.getElementById("clock").textContent = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Africa/Lagos" }); } catch (e) {} }
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
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
    try { const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return ok; } catch (e) { return false; }
  }
  const openLink = (u) => (tg && tg.openLink ? tg.openLink(u) : window.open(u, "_blank"));
  // ---------------------------------------------------------------- crests & flags
  const CREST = (uid) => (uid && /^\d{1,9}$/.test(String(uid)) ? `https://img.sportradar.com/ls/crest/medium/${uid}.png` : "");
  function initials(n) {
    const w = String(n || "?").replace(/\b(FC|CF|SC|AC|AFC|CD|SV|FK|SK|BK|IF|the)\b/gi, "").trim().split(/[\s\-]+/).filter(Boolean);
    return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 3)).toUpperCase();
  }
  function hue(n) { let h = 0; for (const c of String(n || "")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
  function crest(uid, name, cls = "") {
    const u = CREST(uid);
    return `<span class="crest ${cls}" style="--h:${hue(name)}" aria-hidden="true"><b>${esc(initials(name))}</b>${u ? `<img src="${u}" alt="" loading="lazy" decoding="async" onload="if(this.naturalWidth<8)this.remove();else this.style.opacity=1" onerror="this.remove()">` : ""}</span>`;
  }
  const ISO = { england: "gb-eng", scotland: "gb-sct", wales: "gb-wls", "northern ireland": "gb-nir", spain: "es", italy: "it", germany: "de", france: "fr", netherlands: "nl", portugal: "pt", belgium: "be", turkiye: "tr", turkey: "tr", usa: "us", brazil: "br", argentina: "ar", nigeria: "ng", austria: "at", switzerland: "ch", denmark: "dk", norway: "no", sweden: "se", greece: "gr", "saudi arabia": "sa", poland: "pl", czechia: "cz", japan: "jp", mexico: "mx", croatia: "hr", serbia: "rs", romania: "ro", ukraine: "ua", russia: "ru", ireland: "ie", australia: "au", china: "cn", "korea republic": "kr", egypt: "eg", "south africa": "za", morocco: "ma", ghana: "gh", colombia: "co", chile: "cl", uruguay: "uy", israel: "il", finland: "fi", iceland: "is", hungary: "hu", slovakia: "sk", slovenia: "si", bulgaria: "bg", cyprus: "cy", qatar: "qa", "united arab emirates": "ae", uae: "ae", canada: "ca", peru: "pe", ecuador: "ec", paraguay: "py", bolivia: "bo", venezuela: "ve", kenya: "ke", tunisia: "tn", algeria: "dz", iran: "ir", india: "in", indonesia: "id", thailand: "th", vietnam: "vn", estonia: "ee", latvia: "lv", lithuania: "lt", belarus: "by", georgia: "ge", armenia: "am", azerbaijan: "az", kazakhstan: "kz", bosnia: "ba", "bosnia & herzegovina": "ba", albania: "al", "north macedonia": "mk", montenegro: "me", luxembourg: "lu", malta: "mt", "faroe islands": "fo", "costa rica": "cr", honduras: "hn", guatemala: "gt", "el salvador": "sv", jamaica: "jm", "new zealand": "nz", singapore: "sg", malaysia: "my", "hong kong": "hk", jordan: "jo", kuwait: "kw", bahrain: "bh", oman: "om", iraq: "iq", uzbekistan: "uz", "cote d'ivoire": "ci", senegal: "sn", cameroon: "cm", zambia: "zm", tanzania: "tz", uganda: "ug", ethiopia: "et", rwanda: "rw" };
  function catOf(x) { return (x.category || (String(x.league || x.name || "").includes("·") ? String(x.league || x.name).split("·")[0].trim() : "")).toLowerCase(); }
  function flag(x) {
    const c = catOf(x), iso = ISO[c];
    if (iso) return `<img class="flag" src="https://flagcdn.com/w40/${iso}.png" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('⚽'))">`;
    return `<span class="flag emo">${c.startsWith("international") ? "🌍" : "⚽"}</span>`;
  }
  const leagueShort = (lg) => (String(lg || "").includes("·") ? String(lg).split("·").slice(1).join("·").trim() : String(lg || ""));
  const splitMatch = (l) => (l.home && l.away ? [l.home, l.away] : String(l.match || "").split(" vs "));
  function kickDay(iso) { try { return new Date(iso).toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", timeZone: "Africa/Lagos" }); } catch (e) { return ""; } }
  function kickTime(iso) { try { return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Africa/Lagos" }); } catch (e) { return ""; } }
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
  const STATUS = { won: ["✓", "Won", "st-won"], lost: ["✕", "Lost", "st-lost"], void: ["–", "Void", "st-void"], check: ["?", "Check", "st-check"], live: ["●", "Live", "st-live"], pending: ["", "Pending", "st-pend"] };

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
  function setTab(t) {
    state.tab = t; hapSel();
    document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    render(); window.scrollTo(0, 0);
  }
  function backVisible() { return state.tab === "matches" && state.mStack.length > 0; }
  if (tg && tg.BackButton) tg.BackButton.onClick(() => { if (!$sheet.classList.contains("hidden")) return closeSheet(); if (backVisible()) { state.mStack.pop(); render(); } });
  function render() {
    if (tg && tg.BackButton) (backVisible() ? tg.BackButton.show() : tg.BackButton.hide());
    badge();
    ({ home: renderHome, matches: renderMatches, ai: renderAI, slip: renderSlip, history: renderHistory, rollover: renderRollover })[state.tab]();
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
        <button class="btn secondary" style="flex:.7" data-act="slipall" data-k="${esc(key)}">Add to slip</button></div>` : ""}
    </div>`;
  }

  // ---------------------------------------------------------------- HOME / SCANNER
  function renderHome() {
    const t = TIERS[state.tier];
    let html = `<div style="margin:4px 2px 12px"><div class="label">Market scanner</div><h1>Build a slip</h1>
      <div class="sub">Every SportyBet market · up to 80 matches · form, home/away &amp; H2H checked</div></div>
      <div class="chips">${Object.entries(TIERS).map(([k, v]) => `<button class="chip t-${k}${state.tier === k ? " on" : ""}" data-act="tier" data-t="${k}"><i></i>${v.label}</button>`).join("")}</div>
      <div class="card t-${state.tier}" style="margin-top:10px">
        <div class="seg"><button data-act="day" data-d="today" class="${state.day === "today" ? "on" : ""}">Today</button><button data-act="day" data-d="tomorrow" class="${state.day === "tomorrow" ? "on" : ""}">Tomorrow</button><button data-act="day" data-d="48h" class="${state.day === "48h" ? "on" : ""}">Next 48h</button></div>`;
    if (state.tier !== "audit") {
      html += `<div class="target"><div class="field"><span>Target odds</span><input id="target" inputmode="decimal" value="${state.target}" autocomplete="off"></div></div>
        <div class="quick">${t.quick.map((q) => `<button data-act="quick" data-q="${q}" class="${Number(state.target) === q ? "on" : ""}">${q}×</button>`).join("")}</div>`;
    }
    html += `<div class="label" style="margin-top:12px">Filters</div><div class="chips fchips">${EXCL.map(([k, v]) => `<button class="chip${state.ex.includes(k) ? " on" : ""}" data-act="excl" data-k="${k}" aria-pressed="${state.ex.includes(k)}">${state.ex.includes(k) ? "✓ " : ""}${v}</button>`).join("")}</div>`;
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
      state.scan = await api("/api/scan", { tier: state.tier, target: state.tier === "audit" ? null : state.target, day: state.day, exclude_markets: state.ex });
      hapN("success");
    } catch (e) { state.scanErr = e.message; hapN("error"); }
    state.scanning = false;
    if (state.tab === "home") renderHome();
  }

  // ---------------------------------------------------------------- booking sheet
  async function bookLegs(legs, total, prob, slipId) {
    if (!legs.length) return;
    openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div><div class="scanning"><div class="radar"></div>Creating your code…</div>`);
    try {
      const meta = legs.map((l) => ({ pick: l.pick || l.label, market: l.market || "", odds: l.odds, est: l.est, match: l.match, home: l.home, away: l.away, home_uid: l.home_uid, away_uid: l.away_uid, league: l.league, category: l.category, kickoff: l.kickoff || l.start, verdict: l.verdict }));
      const r = await api("/api/book", slipId ? { slip_id: slipId } : { selections: legs.map(sel), legs: meta });
      state.hist = null;
      hapN("success");
      openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div>
        <div class="code" id="codeText">${esc(r.code)}</div>
        <div class="sub" style="text-align:center">${legs.length} legs · total odds ${f2(total)}${prob != null ? ` · est. hit ${pct(prob, 1)}` : ""}${r.loaded != null && r.loaded !== legs.length ? ` · ⚠ ${r.loaded}/${legs.length} loaded` : ""}</div>
        <div class="btnrow"><button class="btn" data-act="copycode">Copy code</button><button class="btn secondary" data-act="openhub">Open SportyBet</button></div>
        <div class="sub" style="text-align:center;margin-top:8px">${r.history_id ? "Saved to History · you'll get a Telegram ping when it lands" : ""}</div>
        <div class="disc">Load it under Load Code on SportyBet and check every selection before staking. Estimates, not guarantees. 18+.</div>`);
    } catch (e) {
      hapN("error");
      openSheet(`<div class="label" style="text-align:center">Booking failed</div><div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="closesheet">Close</button>`);
    }
  }
  function openSheet(html) { $sheetCard.innerHTML = html; $sheet.classList.remove("hidden"); if (tg && tg.BackButton) tg.BackButton.show(); }
  function closeSheet() { $sheet.classList.add("hidden"); if (tg && tg.BackButton) (backVisible() ? tg.BackButton.show() : tg.BackButton.hide()); }

  // ---------------------------------------------------------------- MATCHES
  async function loadEvents(range) {
    const d = state.mData[range];
    if (d && Date.now() - d.at < 5 * 60 * 1000) return d.json;
    const json = await api(`/api/events?range=${range}`);
    state.mData[range] = { at: Date.now(), json }; return json;
  }
  function findMatch(eid) {
    for (const r of ["today", "upcoming"]) { const d = state.mData[r]; if (!d) continue; for (const l of d.json.leagues) for (const m of l.matches) if (m.id === eid) return { m, league: l }; }
    return null;
  }
  function renderMatches() {
    const cur = state.mStack[state.mStack.length - 1];
    if (!cur) return renderLeagues();
    if (cur.name === "league") return renderLeague(cur.arg);
    if (cur.name === "match") return renderMatch(cur.arg);
  }
  function mrow(m, showDay, sub) {
    return `<button class="row mrow" data-act="match" data-id="${esc(m.id)}"><div class="time">${showDay ? esc(m.day.slice(0, 3)) + "<br>" : ""}${esc(m.time)}</div>
      <div class="grow">${teamsBlock(m, "sm")}${sub ? `<div class="sub ellip" style="margin-top:3px">${esc(sub)}</div>` : ""}</div>
      <div class="odds3"><span>${f2(m.odds["1"])}</span><span>${f2(m.odds.X)}</span><span>${f2(m.odds["2"])}</span></div></button>`;
  }
  async function renderLeagues() {
    const range = state.mRange;
    const head = `<div style="margin:4px 2px 10px"><div class="label">Fixtures</div><h1>Matches</h1></div>
      <div class="seg"><button data-act="mrange" data-r="today" class="${range === "today" ? "on" : ""}">Today</button><button data-act="mrange" data-r="upcoming" class="${range === "upcoming" ? "on" : ""}">Next 3 days</button></div>
      <input id="msearch" class="search" type="search" placeholder="Search league or team" value="${esc(state.mQuery)}" autocomplete="off">`;
    if (!state.mData[range]) $view.innerHTML = head + `<div class="card flush" style="margin-top:12px">${[0, 1, 2, 3, 4, 5].map(() => `<div class="row"><div class="grow"><div class="sk sk-line" style="width:60%"></div></div><div class="sk" style="width:28px;height:18px"></div></div>`).join("")}</div>`;
    let data;
    try { data = await loadEvents(range); } catch (e) { $view.innerHTML = head + `<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="mretry">Try again</button>`; bindSearch(); return; }
    if (state.tab !== "matches" || state.mStack.length || state.mRange !== range) return;
    const q = state.mQuery.trim().toLowerCase();
    let html = head;
    if (q.length >= 2) {
      const hits = [];
      for (const l of data.leagues) for (const m of l.matches) if (m.home.toLowerCase().includes(q) || m.away.toLowerCase().includes(q)) hits.push([m, l]);
      if (hits.length) html += `<div class="section"><span class="label">Teams · ${hits.length}</span></div><div class="card flush">${hits.slice(0, 40).map(([m, l]) => mrow(m, range === "upcoming", l.name)).join("")}</div>`;
    }
    const leagues = q ? data.leagues.filter((l) => l.name.toLowerCase().includes(q)) : data.leagues;
    html += `<div class="section"><span class="label">${q ? "Leagues" : `${data.count} matches · ${data.leagues.length} leagues`}</span></div>`;
    html += leagues.length ? `<div class="card flush">${leagues.slice(0, 150).map((l) => `<button class="row" data-act="league" data-id="${esc(l.id)}">${flag(l)}<div class="grow ellip">${esc(l.name)}</div><span class="badge">${l.count}</span><span class="chev">›</span></button>`).join("")}</div>` : `<div class="empty">Nothing matches “${esc(state.mQuery)}”.</div>`;
    $view.innerHTML = html; bindSearch();
  }
  let searchT;
  function bindSearch() {
    const s = document.getElementById("msearch"); if (!s) return;
    s.addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(() => { state.mQuery = s.value; renderLeagues().then(() => { const n = document.getElementById("msearch"); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }); }, 220); });
  }
  function renderLeague(id) {
    const d = state.mData[state.mRange]; const l = d && d.json.leagues.find((x) => x.id === id);
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
      if (m.role === "me") h += `<div class="msg me">${esc(m.text)}</div>`;
      else {
        h += `<div class="msg bot">${mdLite(m.text)}${m.tools && m.tools.length ? `<span class="tools">⚙ ${esc([...new Set(m.tools)].join(" · "))}</span>` : ""}</div>`;
        if (m.slip && m.slip.legs && m.slip.legs.length && !m.code) h += `<div class="btnrow" style="max-width:88%"><button class="btn small" data-act="bookslip" data-k="chat:${i}">Book this slip</button><button class="btn small secondary" data-act="slipall" data-k="chat:${i}">Add to slip</button></div>`;
        if (m.code) h += `<div class="btnrow" style="max-width:88%"><button class="btn small" data-act="copytext" data-t="${esc(m.code)}">Copy ${esc(m.code)}</button><button class="btn small secondary" data-act="openhub">Open SportyBet</button></div>`;
      }
    });
    if (state.aiBusy) h += `<div class="msg bot"><span class="typing"><i></i><i></i><i></i></span> <span class="sub">analysing… scans take ~30 s</span></div>`;
    h += `</div><div class="composer"><form id="aiForm"><input id="aiInput" placeholder="Ask about matches, slips, codes…" autocomplete="off" ${state.aiBusy ? "disabled" : ""}>
      <button type="submit" aria-label="Send" ${state.aiBusy ? "disabled" : ""}><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button></form></div>`;
    $view.innerHTML = h;
    document.getElementById("aiForm").addEventListener("submit", (ev) => { ev.preventDefault(); const v = document.getElementById("aiInput").value.trim(); if (v) askAI(v); });
    window.scrollTo(0, document.body.scrollHeight);
  }
  async function askAI(text) {
    if (state.aiBusy) return;
    state.chat.push({ role: "me", text }); state.aiBusy = true; hap("light"); renderAI();
    try {
      const r = await api("/api/ai", { message: text });
      state.chat.push({ role: "bot", text: r.reply, slip: r.slip && r.slip.legs ? r.slip : null, code: r.code, tools: r.tools });
      hapN("success");
    } catch (e) { state.chat.push({ role: "bot", text: "⚠ " + e.message }); hapN("error"); }
    state.aiBusy = false;
    if (state.tab === "ai") renderAI();
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
  function histCard(r) {
    const d = r.data, legs = d.legs || [];
    const won = legs.filter((l) => l.status === "won").length, lost = legs.filter((l) => l.status === "lost").length, live = legs.filter((l) => l.status === "live").length;
    const open = !!state.histOpen[r.id];
    const tag = d.rollover_id ? `Rollover · day ${d.rollover_day}` : (d.label || d.tier || "Slip");
    const seg = legs.map((l) => `<i class="${(STATUS[l.status] || STATUS.pending)[2]}"></i>`).join("");
    return `<div class="card hcard s-${r.status}">
      <div class="hhead"><div class="grow"><div class="label">${esc(tag)}${d.test ? " · test" : ""}</div><div class="hcode">${d.code ? `<button class="codechip" data-act="copytext" data-t="${esc(d.code)}">${esc(d.code)}</button>` : `<span class="sub">no code</span>`}<span class="sub">${esc(fmtWhen(r.created_at))}</span></div></div>
        <div class="hright"><div class="num hbig">${f2(d.effective_odds || d.total_odds)}</div>${statusPill(r.status)}</div></div>
      <div class="segbar">${seg}</div>
      <div class="hstats"><span>${won}/${legs.length} won</span>${lost ? `<span class="c-red">${lost} lost</span>` : ""}${live ? `<span class="c-live">${live} live</span>` : ""}${d.est_prob != null ? `<span>est ${pct(d.est_prob, 1)}</span>` : ""}
        <button class="linkbtn" data-act="histtoggle" data-id="${esc(r.id)}">${open ? "Hide legs" : "Show legs"}</button></div>
      ${open ? `<div class="hlegs">${legs.map(histLeg).join("")}</div>` : ""}</div>`;
  }
  async function loadHistory(force) {
    if (state.hist && !force) return;
    state.histErr = "";
    try { state.hist = await api("/api/history?limit=60"); } catch (e) { state.histErr = e.message; }
  }
  async function renderHistory() {
    let h = `<div style="margin:4px 2px 12px" class="hrow"><div><div class="label">Tracked codes</div><h1>History</h1><div class="sub">Results update every 5 min · Telegram ping when a slip lands</div></div><button class="iconbtn" data-act="histrefresh" aria-label="Refresh">↻</button></div>`;
    if (!state.hist && !state.histErr) { $view.innerHTML = h + `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`; await loadHistory(); if (state.tab !== "history") return; }
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
    try { state.ro = await api("/api/rollover"); } catch (e) { state.roErr = e.message; }
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
  async function renderRollover() {
    let h = `<div style="margin:4px 2px 12px" class="hrow"><div><div class="label">Daily compounding plan</div><h1>Rollover</h1><div class="sub">Top leagues · stats-confirmed legs ≥80% each</div></div><button class="iconbtn" data-act="rorefresh" aria-label="Refresh">↻</button></div>`;
    if (!state.ro && !state.roErr) { $view.innerHTML = h + `<div class="card">${[0, 1, 2].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`; await loadRollover(); if (state.tab !== "rollover") return; }
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
          <div class="card"><div class="hhead"><div class="grow">${dd.code ? `<button class="codechip" data-act="copytext" data-t="${esc(dd.code)}">${esc(dd.code)}</button>` : `<button class="btn small" style="margin:0;width:auto;padding:0 14px" data-act="robook" data-id="${esc(ds.id)}">Book code</button>`}</div><div class="hright"><div class="num hbig">${f2(dd.total_odds)}</div><span class="sub">est ${pct(dd.est_prob, 1)}</span></div></div>
          <div class="hlegs">${(dd.legs || []).map(histLeg).join("")}</div></div>`;
      }
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
    if (past.length) h += `<div class="section"><span class="label">Past plans</span></div><div class="card flush">${past.map((r) => `<div class="row"><div class="grow"><b>${r.data.target}× · ${r.data.days} days</b><div class="sub">${esc(fmtWhen(r.created_at))} · reached day ${Math.min(r.data.current_day, r.data.days)}</div></div><span class="spill ${{ completed: "st-won", busted: "st-lost" }[r.status] || "st-void"}">${esc({ completed: "Completed", busted: "Busted", stopped: "Stopped" }[r.status] || r.status)}</span></div>`).join("")}</div>`;
    h += `<div class="disc">Each leg is an estimate, not a guarantee; a rollover multiplies the risk of every day. 18+. Stake only what you can afford to lose.</div>`;
    $view.innerHTML = h;
  }
  async function roAction(body, busy) {
    if (busy) { state.roBusy = true; renderRollover(); }
    try { const r = await api("/api/rollover", body); hapN("success"); if (r.day && r.day.status === "no_slip") toast("No slip met the bar today: skip suggested"); else if (r.day && r.day.status === "waiting") toast(r.day.note); }
    catch (e) { hapN("error"); toast(e.message); }
    state.roBusy = false; state.ro = null; state.hist = null;
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
      <button class="btn" data-act="bookmine">Book on SportyBet</button><button class="btn ghost" data-act="clear">Clear slip</button></div>
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
    if (a === "tab") setTab(t.dataset.t);
    else if (a === "tier") { state.tier = t.dataset.t; if (TIERS[state.tier].target) state.target = TIERS[state.tier].target; hapSel(); renderHome(); }
    else if (a === "day") { state.day = t.dataset.d; hapSel(); renderHome(); }
    else if (a === "quick") { state.target = Number(t.dataset.q); hapSel(); renderHome(); }
    else if (a === "scan") runScan();
    else if (a === "excl") { const k = t.dataset.k; state.ex = state.ex.includes(k) ? state.ex.filter((x) => x !== k) : state.ex.concat([k]); try { localStorage.setItem("fra_ex", JSON.stringify(state.ex)); } catch (e) {} hapSel(); renderHome(); }
    else if (a === "rounders") { const si = document.getElementById("rostake"); if (si) state.roForm.stake = si.value; state.roForm.allowUnders = !state.roForm.allowUnders; hapSel(); renderRollover(); }
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
    else if (a === "copycode") { const c = document.getElementById("codeText").textContent; toast((await copy(c)) ? "Code copied" : "Press and hold the code to copy"); hap("light"); }
    else if (a === "copytext" || a === "copyinline") { const c = t.dataset.t || t.textContent; toast((await copy(c)) ? `Copied ${c}` : "Couldn't copy"); hap("light"); }
    else if (a === "openhub") openLink(LOAD_URL);
    else if (a === "mrange") { state.mRange = t.dataset.r; hapSel(); renderLeagues(); }
    else if (a === "mretry") renderLeagues();
    else if (a === "league") { state.mStack.push({ name: "league", arg: t.dataset.id }); render(); window.scrollTo(0, 0); }
    else if (a === "match") { if (state.tab !== "matches") { state.tab = "matches"; document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === "matches")); } const c = state.mStack[state.mStack.length - 1]; if (!(c && c.name === "match" && c.arg === t.dataset.id)) state.mStack.push({ name: "match", arg: t.dataset.id }); render(); window.scrollTo(0, 0); }
    else if (a === "mback") { state.mStack.pop(); render(); }
    else if (a === "addpred") addPred(t.dataset.m, t.dataset.o);
    else if (a === "suggest") askAI(t.textContent);
    else if (a === "histrefresh") { state.hist = null; renderHistory(); }
    else if (a === "histfilter") { state.histFilter = t.dataset.f; hapSel(); renderHistory(); }
    else if (a === "histtoggle") { state.histOpen[t.dataset.id] = !state.histOpen[t.dataset.id]; renderHistory(); }
    else if (a === "rorefresh") { state.ro = null; renderRollover(); }
    else if (a === "rotarget") { state.roForm.target = Number(t.dataset.v); hapSel(); renderRollover(); }
    else if (a === "rodays") { const inp = document.getElementById("rostake"); if (inp) state.roForm.stake = inp.value; state.roForm.days = Math.max(3, Math.min(15, state.roForm.days + Number(t.dataset.v))); hapSel(); renderRollover(); }
    else if (a === "rostart") { const inp = document.getElementById("rostake"); const st = inp ? parseFloat(inp.value.replace(/[,₦\s]/g, "")) : NaN; roAction({ action: "start", target: state.roForm.target, days: state.roForm.days, stake: isNaN(st) ? null : st, allow_unders: !!state.roForm.allowUnders }, true); }
    else if (a === "ronow") roAction({ action: "now" }, true);
    else if (a === "roskip") roAction({ action: "skip" });
    else if (a === "rostop") { const doIt = () => roAction({ action: "stop" }); if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm("Stop this rollover plan?", (ok) => ok && doIt()); else if (confirm("Stop this rollover plan?")) doIt(); }
    else if (a === "robook") { try { const r = await api("/api/history/book", { id: t.dataset.id }); hapN("success"); toast(`Code ${r.code} created`); } catch (e) { toast(e.message); } state.ro = null; renderRollover(); }
  });
  document.querySelectorAll(".tabbtn").forEach((b) => b.addEventListener("click", () => { if (b.dataset.tab === state.tab && state.tab === "matches") { state.mStack = []; } setTab(b.dataset.tab); }));

  badge();
  if (!initData) {
    $view.innerHTML = `<div class="card" style="margin-top:20px;text-align:center"><img src="/app/logo.png" width="64" height="64" style="border-radius:16px"><h2 style="margin-top:12px">Fra Prediction</h2><div class="sub" style="margin-top:6px">Open this app from @FraPredictbot in Telegram (menu button or /start).</div></div>`;
    return;
  }
  if (location.hash === "#history" || location.hash === "#rollover") { state.tab = location.hash.slice(1); document.querySelectorAll(".tabbtn").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab)); }
  render();
})();
