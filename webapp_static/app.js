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
    safe: { label: "Safe", target: 5, quick: [2, 3, 5, 8, 10], rule: "Legs est. ≥85% · odds 1.05–1.35 · strongest, stats-checked legs" },
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
  };

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
    return { ...sel(l), label: l.pick, market: l.market, odds: l.odds, est: l.est, match: l.match, when: l.kickoff_local, start: l.kickoff };
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
    ({ home: renderHome, matches: renderMatches, ai: renderAI, slip: renderSlip })[state.tab]();
  }
  const backLink = () => (tg && tg.BackButton && tg.isVersionAtLeast && tg.isVersionAtLeast("6.1") ? "" : `<button class="back" data-act="mback">‹ Back</button>`);

  // ---------------------------------------------------------------- shared pieces
  function verdictChip(v) {
    if (v === "confirmed") return `<span class="vchip v-confirmed">✓ Stats</span>`;
    if (v === "demoted") return `<span class="vchip v-demoted">Demoted</span>`;
    if (v === "market only") return `<span class="vchip v-odds">Market</span>`;
    return `<span class="vchip v-odds">Odds only</span>`;
  }
  function legCard(l, opts = {}) {
    const inSlip = slip.has(l.event_id, l.market_id, l.outcome_id, l.specifier);
    const tick = `<button class="tick btn-tick ${tierOfOdds(l.odds)}${inSlip ? " sel" : ""}" data-act="addleg" data-k="${esc(opts.key || "")}" data-i="${opts.i}">${f2(l.odds)}</button>`;
    const color = l.est >= 0.8 ? "var(--accent)" : l.est >= 0.6 ? "var(--mid)" : l.est >= 0.45 ? "var(--amber)" : "var(--red)";
    return `<div class="leg"><div class="top"><div class="grow">
        <div class="meta">${esc(l.kickoff_local)} · ${esc(l.league)}</div>
        <div class="match ellip">${esc(l.match)}</div>
        <div class="mkt">${esc(l.market)} · <span class="pick">${esc(l.pick)}</span>${verdictChip(l.verdict)}</div>
      </div>${tick}</div>
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
        `<div class="card flush">${s.legs.map((l, i) => legCard(l, { key: k, i })).join("")}</div>`;
    }
    if (r.tier === "audit") return auditOut(r);
    return portfolio(r, "scan") + (r.legs.length ? `<div class="section"><span class="label">Legs</span><span class="sub">tap a price to add</span></div><div class="card flush">${r.legs.map((l, i) => legCard(l, { key: "scan", i })).join("")}</div>` : "");
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
      state.scan = await api("/api/scan", { tier: state.tier, target: state.tier === "audit" ? null : state.target, day: state.day });
      hapN("success");
    } catch (e) { state.scanErr = e.message; hapN("error"); }
    state.scanning = false;
    if (state.tab === "home") renderHome();
  }

  // ---------------------------------------------------------------- booking sheet
  async function bookLegs(legs, total, prob) {
    if (!legs.length) return;
    openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div><div class="scanning"><div class="radar"></div>Creating your code…</div>`);
    try {
      const r = await api("/api/book", { selections: legs.map(sel) });
      hapN("success");
      openSheet(`<div class="label" style="text-align:center">SportyBet booking code</div>
        <div class="code" id="codeText">${esc(r.code)}</div>
        <div class="sub" style="text-align:center">${legs.length} legs · total odds ${f2(total)}${prob != null ? ` · est. hit ${pct(prob, 1)}` : ""}${r.loaded != null && r.loaded !== legs.length ? ` · ⚠ ${r.loaded}/${legs.length} loaded` : ""}</div>
        <div class="btnrow"><button class="btn" data-act="copycode">Copy code</button><button class="btn secondary" data-act="openhub">Open SportyBet</button></div>
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
      <div class="grow"><div class="teams ellip">${esc(m.home)}</div><div class="teams ellip">${esc(m.away)}</div>${sub ? `<div class="sub ellip">${esc(sub)}</div>` : ""}</div>
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
    html += leagues.length ? `<div class="card flush">${leagues.slice(0, 150).map((l) => `<button class="row" data-act="league" data-id="${esc(l.id)}"><div class="grow ellip">${esc(l.name)}</div><span class="badge">${l.count}</span><span class="chev">›</span></button>`).join("")}</div>` : `<div class="empty">Nothing matches “${esc(state.mQuery)}”.</div>`;
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
    $view.innerHTML = `${backLink()}<div style="margin:4px 2px 10px"><div class="label">${l.count} matches</div><h2>${esc(l.name)}</h2></div><div class="card flush">${l.matches.map((m) => mrow(m, state.mRange === "upcoming")).join("")}</div>`;
  }
  async function renderMatch(eid) {
    const k = findMatch(eid);
    state.pred = null; state.markets = null;
    $view.innerHTML = backLink() + (k ? `<div class="card"><div class="sub">${esc(k.league.name)}</div><h2>${esc(k.m.home)} vs ${esc(k.m.away)}</h2><div class="sub">${esc(k.m.day)}, ${esc(k.m.time)} WAT</div></div>` : "") +
      `<div class="card">${[0, 1, 2, 3].map(() => `<div class="sk sk-line"></div>`).join("")}</div>`;
    const isCur = () => { const c = state.mStack[state.mStack.length - 1]; return state.tab === "matches" && c && c.name === "match" && c.arg === eid; };
    let p;
    try { p = await api("/api/predict", { event_id: eid }); } catch (e) { if (isCur()) $view.innerHTML = `${backLink()}<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="match" data-id="${esc(eid)}">Try again</button>`; return; }
    if (!isCur()) return;
    state.pred = p; drawMatch();
    try { state.markets = await api(`/api/match_markets?event_id=${encodeURIComponent(eid)}`); } catch (e) { state.markets = { legs: [], error: e.message }; }
    if (isCur()) drawMatch();
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
    let h = `${backLink()}<div class="card"><div class="sub">${esc(e.league)}</div><h2>${esc(e.home)} vs ${esc(e.away)}</h2>
      <div class="sub">${esc(e.day)}, ${esc(e.time)} WAT${p.xg ? ` · xG ${p.xg[0]}–${p.xg[1]}` : ""} · ${esc(p.source)}</div>
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
  function addPred(mid, oid) {
    const p = state.pred; if (!p) return; let o = null;
    for (const mk of p.markets) for (const x of mk.outcomes) if (x.market_id === mid && x.outcome_id === oid) o = x;
    if (!o || !o.odds) return;
    const e = p.event;
    const rep = slip.add({ event_id: e.id, market_id: mid, outcome_id: oid, specifier: o.specifier || "", label: o.label, market: "", odds: o.odds, est: o.prob, match: `${e.home} vs ${e.away}`, when: `${e.day.slice(0, 3)} ${e.time}`, start: e.start });
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
        return `<div class="leg"><div class="top"><div class="grow"><div class="meta">${esc(p.when || "")}${started ? ' · <span style="color:var(--red)">started</span>' : ""}</div>
          <div class="match ellip">${esc(p.match)}</div><div class="mkt">${p.market ? esc(p.market) + " · " : ""}<span class="pick">${esc(p.label)}</span>${p.est != null ? ` · est ${pct(p.est)}` : ""}</div></div>
          <div class="tick ${tierOfOdds(p.odds)}">${f2(p.odds)}</div><button class="chev" style="padding:0 2px 0 8px" data-act="remove" data-id="${esc(p.event_id)}" aria-label="Remove">✕</button></div></div>`;
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
    else if (a === "viewslip") { state.viewSlip = t.dataset.k; renderHome(); window.scrollTo(0, 0); }
    else if (a === "auditback") { state.viewSlip = null; renderHome(); }
    else if (a === "bookslip") {
      const k = t.dataset.k; const legs = legsFor(k);
      let s = k === "scan" ? state.scan : k.startsWith("audit:") ? state.scan.slips[k.slice(6)] : (state.chat[Number(k.slice(5))] || {}).slip;
      hap("medium"); bookLegs(legs, s ? s.total_odds : legs.reduce((x, l) => x * l.odds, 1), s ? s.est_prob : null);
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
  });
  document.querySelectorAll(".tabbtn").forEach((b) => b.addEventListener("click", () => { if (b.dataset.tab === state.tab && state.tab === "matches") { state.mStack = []; } setTab(b.dataset.tab); }));

  badge();
  if (!initData) {
    $view.innerHTML = `<div class="card" style="margin-top:20px;text-align:center"><img src="/app/logo.png" width="64" height="64" style="border-radius:16px"><h2 style="margin-top:12px">Fra Prediction</h2><div class="sub" style="margin-top:6px">Open this app from @FraPredictbot in Telegram (menu button or /start).</div></div>`;
    return;
  }
  render();
})();
