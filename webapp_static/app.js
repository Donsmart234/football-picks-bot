(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const initData = (tg && tg.initData) || "";
  if (tg) { document.documentElement.classList.add("tg"); tg.ready(); tg.expand(); }

  const LOAD_URL = "https://www.sportybet.com/ng/m/code-hub/load-code";
  const SLIP_KEY = "picks_slip_v1";
  const $view = document.getElementById("view");
  const $top = document.querySelector(".top");
  const $search = document.getElementById("search");
  const $slipBtn = document.getElementById("slipBtn");
  const $toast = document.getElementById("toast");

  const state = { range: "today", data: {}, stack: [], query: "" };

  // ---------------------------------------------------------------- helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (o) => (o ? Number(o).toFixed(2) : "–");
  const pct = (p) => Math.round(p * 100) + "%";
  const haptic = (t) => { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(t || "light"); } catch (e) {} };
  let toastTimer;
  function toast(msg) {
    $toast.textContent = msg; $toast.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $toast.classList.add("hidden"), 2200);
  }
  const loading = (msg) => `<div class="loading"><div class="spinner"></div>${esc(msg || "Loading…")}</div>`;

  async function api(path, body) {
    const opts = { headers: { "X-Telegram-Init-Data": initData } };
    if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opts); } catch (e) { throw new Error("Network error. Check your connection and try again."); }
    let j = null;
    try { j = await r.json(); } catch (e) {}
    if (r.status === 403) throw new Error("Access denied. Open this app from your picks bot in Telegram.");
    if (!r.ok) throw new Error((j && j.error) || `Server error (${r.status}). Try again in a minute.`);
    return j;
  }

  // ---------------------------------------------------------------- slip
  const slip = {
    get() { try { return JSON.parse(localStorage.getItem(SLIP_KEY) || "[]"); } catch (e) { return []; } },
    save(items) { localStorage.setItem(SLIP_KEY, JSON.stringify(items)); renderSlipBtn(); },
    add(pick) {
      const items = this.get().filter((p) => p.event_id !== pick.event_id);
      const replaced = items.length !== this.get().length;
      items.push(pick); this.save(items); return replaced;
    },
    remove(eid) { this.save(this.get().filter((p) => p.event_id !== eid)); },
    clear() { this.save([]); },
    total(items) { return (items || this.get()).reduce((t, p) => t * (p.odds || 1), 1); },
    has(eid, mid, oid) { return this.get().some((p) => p.event_id === eid && p.market_id === mid && p.outcome_id === oid); },
  };
  function renderSlipBtn() {
    const items = slip.get();
    document.getElementById("slipCount").textContent = items.length;
    document.getElementById("slipOdds").textContent = slip.total(items).toFixed(2);
    const onSlip = state.stack.length && ["slip", "code"].includes(state.stack[state.stack.length - 1].name);
    $slipBtn.classList.toggle("hidden", !items.length || onSlip);
  }

  // ---------------------------------------------------------------- navigation
  function go(name, arg) { state.stack.push({ name, arg }); render(); window.scrollTo(0, 0); }
  function back() { state.stack.pop(); render(); }
  if (tg && tg.BackButton) tg.BackButton.onClick(back);

  function render() {
    const cur = state.stack[state.stack.length - 1] || { name: "home" };
    const nested = state.stack.length > 0;
    $top.classList.toggle("compact", nested);
    if (tg && tg.BackButton) nested ? tg.BackButton.show() : tg.BackButton.hide();
    renderSlipBtn();
    ({ home: renderHome, league: renderLeague, match: renderMatch, slip: renderSlip, code: renderCode })[cur.name](cur.arg);
  }
  const backLink = () => (tg && tg.BackButton ? "" : `<button class="back" data-act="back">‹ Back</button>`);

  // ---------------------------------------------------------------- data
  async function loadEvents(range, force) {
    if (!force && state.data[range] && Date.now() - state.data[range].at < 5 * 60 * 1000) return state.data[range].json;
    const json = await api(`/api/events?range=${range}`);
    state.data[range] = { at: Date.now(), json };
    return json;
  }
  function findMatch(eid) {
    for (const r of ["today", "upcoming"]) {
      const d = state.data[r]; if (!d) continue;
      for (const l of d.json.leagues) for (const m of l.matches) if (m.id === eid) return { m, league: l };
    }
    return null;
  }

  // ---------------------------------------------------------------- screens
  function matchRow(m, showDay, leagueName) {
    return `<button class="row match" data-act="match" data-id="${esc(m.id)}">
      <div class="time">${showDay ? esc(m.day.slice(0, 3)) + "<br>" : ""}${esc(m.time)}</div>
      <div class="grow"><div class="teams">${esc(m.home)}</div><div class="teams">${esc(m.away)}</div>
        ${leagueName ? `<div class="sub">${esc(leagueName)}</div>` : ""}</div>
      <div class="odds"><span>${fmt(m.odds["1"])}</span><span>${fmt(m.odds.X)}</span><span>${fmt(m.odds["2"])}</span></div>
    </button>`;
  }

  async function renderHome() {
    const range = state.range;
    if (!state.data[range]) $view.innerHTML = loading("Fetching matches from SportyBet…");
    let data;
    try { data = await loadEvents(range); } catch (e) {
      $view.innerHTML = `<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="retry">Try again</button>`; return;
    }
    if (state.stack.length || state.range !== range) return; // navigated away meanwhile
    const q = state.query.trim().toLowerCase();
    if (!data.leagues.length) { $view.innerHTML = `<div class="empty">No upcoming matches for ${range === "today" ? "today" : "the next 3 days"}.</div>`; return; }
    let html = "";
    const leagues = q ? data.leagues.filter((l) => l.name.toLowerCase().includes(q)) : data.leagues;
    if (q && q.length >= 2) {
      const hits = [];
      for (const l of data.leagues) for (const m of l.matches)
        if (m.home.toLowerCase().includes(q) || m.away.toLowerCase().includes(q)) hits.push([m, l]);
      if (hits.length) {
        html += `<div class="section-title">Teams (${hits.length})</div><div class="card">` +
          hits.slice(0, 40).map(([m, l]) => matchRow(m, range === "upcoming", l.name)).join("") + `</div>`;
      }
    }
    html += `<div class="section-title">${q ? "Leagues" : `${data.count} matches · ${data.leagues.length} leagues`}</div>`;
    if (leagues.length) {
      html += `<div class="card">` + leagues.map((l) => `<button class="row" data-act="league" data-id="${esc(l.id)}">
        <div class="grow">${esc(l.name)}</div><span class="badge">${l.count}</span><span class="chev">›</span></button>`).join("") + `</div>`;
    } else if (!html.includes("Teams")) html += `<div class="empty">Nothing matches “${esc(state.query)}”.</div>`;
    $view.innerHTML = html;
  }

  function renderLeague(id) {
    const d = state.data[state.range];
    const l = d && d.json.leagues.find((x) => x.id === id);
    if (!l) { $view.innerHTML = `${backLink()}<div class="empty">League not found.</div>`; return; }
    $view.innerHTML = `${backLink()}<div class="section-title">${esc(l.name)} · ${l.count}</div><div class="card">` +
      l.matches.map((m) => matchRow(m, state.range === "upcoming")).join("") + `</div>`;
  }

  async function renderMatch(eid) {
    const known = findMatch(eid);
    $view.innerHTML = backLink() + (known ? `<div class="card head"><div class="sub">${esc(known.league.name)}</div>
      <h2>${esc(known.m.home)} vs ${esc(known.m.away)}</h2><div class="sub">${esc(known.m.day)}, ${esc(known.m.time)} (Lagos)</div></div>` : "") +
      loading("Analysing match…");
    let p;
    try { p = await api("/api/predict", { event_id: eid }); } catch (e) {
      const cur = state.stack[state.stack.length - 1];
      if (cur && cur.name === "match" && cur.arg === eid)
        $view.innerHTML = `${backLink()}<div class="error">${esc(e.message)}</div><button class="btn secondary" data-act="rematch" data-id="${esc(eid)}">Try again</button>`;
      return;
    }
    const cur = state.stack[state.stack.length - 1];
    if (!cur || cur.name !== "match" || cur.arg !== eid) return;
    state.pred = p;
    drawPrediction(p);
  }

  function drawPrediction(p) {
    const e = p.event, b = p.best;
    const isBest = (o) => b && b.market_id === o.market_id && b.outcome_id === o.outcome_id;
    let html = `${backLink()}<div class="card"><div class="head"><div class="sub">${esc(e.league)}</div>
      <h2>${esc(e.home)} vs ${esc(e.away)}</h2><div class="sub">${esc(e.day)}, ${esc(e.time)} (Lagos)${p.xg ? ` · xG ${p.xg[0]}–${p.xg[1]}` : ""}</div></div>`;
    if (b) html += `<div class="best">⭐ Best pick: <b>${esc(b.label)}</b> · ${pct(b.prob)}${b.odds ? " @ " + fmt(b.odds) : ""}
      ${b.odds ? `<button class="btn" data-act="add" data-m="${b.market_id}" data-o="${b.outcome_id}">Add best pick to slip</button>` : ""}</div>`;
    html += `<div class="reason">${esc(p.reasoning)}</div><div class="disclaimer">Model: ${esc(p.source)}. ${esc(p.disclaimer)}</div></div>`;
    for (const mk of p.markets) {
      html += `<div class="card"><div class="mkt-title">${esc(mk.title)}</div><div class="outs">` + mk.outcomes.map((o) => {
        const sel = slip.has(e.id, o.market_id, o.outcome_id);
        return `<button class="out${sel ? " sel" : ""}${isBest(o) ? " is-best" : ""}" data-act="add" data-m="${o.market_id}" data-o="${o.outcome_id}" ${o.odds ? "" : "disabled"}>
          <div class="l">${esc(o.label)}</div><div class="p">${pct(o.prob)}</div><div class="o">${o.odds ? "@ " + fmt(o.odds) : "no price"}</div></button>`;
      }).join("") + `</div></div>`;
    }
    html += `<div class="disclaimer" style="text-align:center">Tap a price to add it to your slip (one pick per match).</div>`;
    $view.innerHTML = html;
  }

  function addPick(mid, oid) {
    const p = state.pred; if (!p) return;
    let o = null;
    for (const mk of p.markets) for (const x of mk.outcomes) if (x.market_id === mid && x.outcome_id === oid) o = x;
    if (!o || !o.odds) return;
    const e = p.event;
    const replaced = slip.add({ event_id: e.id, market_id: mid, outcome_id: oid, specifier: o.specifier, label: o.label,
      odds: o.odds, match: `${e.home} vs ${e.away}`, when: `${e.day.slice(0, 3)} ${e.time}`, start: e.start });
    haptic("medium");
    toast(`${replaced ? "Replaced" : "Added"}: ${o.label} @ ${fmt(o.odds)} · ${slip.get().length} on slip`);
    drawPrediction(p);
  }

  function renderSlip() {
    const items = slip.get();
    if (!items.length) { $view.innerHTML = `${backLink()}<div class="empty">Your slip is empty. Add picks from a match.</div>`; return; }
    const now = Date.now();
    $view.innerHTML = `${backLink()}<div class="section-title">Your slip</div><div class="card">` + items.map((p) => {
      const started = p.start && Date.parse(p.start) <= now;
      return `<div class="row"><div class="grow"><div class="teams">${esc(p.match)}</div>
        <div class="sub">${esc(p.when || "")} · ${esc(p.label)}${started ? " · <span style='color:var(--danger)'>started</span>" : ""}</div></div>
        <b>${fmt(p.odds)}</b><button class="x" data-act="remove" data-id="${esc(p.event_id)}" aria-label="Remove">✕</button></div>`;
    }).join("") + `<div class="total"><span>Total odds (${items.length})</span><span>${slip.total(items).toFixed(2)}</span></div></div>
      <button class="btn" id="bookBtn" data-act="book">📒 Get booking code</button>
      <button class="btn ghost" data-act="clear">Clear slip</button>
      <div class="disclaimer" style="text-align:center;margin-top:10px">Odds shown are from when you added each pick. Estimates, not guarantees. 18+.</div>`;
  }

  async function book() {
    const items = slip.get(); if (!items.length) return;
    const btn = document.getElementById("bookBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Creating code…"; }
    try {
      const r = await api("/api/book", { selections: items.map((p) => ({ event_id: p.event_id, market_id: p.market_id, specifier: p.specifier || "", outcome_id: p.outcome_id })) });
      haptic("heavy");
      state.stack.push({ name: "code", arg: { code: r.code, url: r.url || LOAD_URL, n: items.length, total: slip.total(items) } });
      render();
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = "📒 Get booking code"; }
      toast(e.message);
      if (tg && tg.showAlert) tg.showAlert(e.message);
    }
  }

  function renderCode(a) {
    $view.innerHTML = `${backLink()}<div class="card code-box"><div class="sub">SportyBet booking code</div>
      <div class="code" id="codeText">${esc(a.code)}</div>
      <div class="sub">${a.n} selection${a.n > 1 ? "s" : ""} · total odds ≈ ${a.total.toFixed(2)}</div>
      <button class="btn" data-act="copy">Copy code</button>
      <button class="btn secondary" data-act="open">Open SportyBet code hub</button>
      <div class="disclaimer" style="margin-top:12px;padding:0">${esc(a.url)}</div></div>
      <div class="disclaimer" style="text-align:center">Load the code on SportyBet and check every selection before staking. Bet only what you can afford to lose. 18+.</div>`;
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) {}
    try {
      const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return ok;
    } catch (e) { return false; }
  }

  // ---------------------------------------------------------------- events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-act]"); if (!t) return;
    const act = t.dataset.act;
    if (act === "league") go("league", t.dataset.id);
    else if (act === "match") go("match", t.dataset.id);
    else if (act === "rematch") renderMatch(t.dataset.id);
    else if (act === "back") back();
    else if (act === "retry") renderHome();
    else if (act === "add") addPick(t.dataset.m, t.dataset.o);
    else if (act === "remove") { slip.remove(t.dataset.id); renderSlip(); renderSlipBtn(); }
    else if (act === "clear") {
      const doClear = () => { slip.clear(); renderSlip(); toast("Slip cleared"); };
      if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm("Clear all picks?", (ok) => ok && doClear());
      else if (confirm("Clear all picks?")) doClear();
    }
    else if (act === "book") book();
    else if (act === "copy") {
      const code = document.getElementById("codeText").textContent;
      toast((await copy(code)) ? "Code copied" : "Couldn't copy: press and hold the code to copy it");
      haptic("light");
    }
    else if (act === "open") {
      const cur = state.stack[state.stack.length - 1];
      const url = (cur && cur.arg && cur.arg.url) || LOAD_URL;
      if (tg && tg.openLink) tg.openLink(url); else window.open(url, "_blank");
    }
  });
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    state.range = b.dataset.range; state.stack = []; render();
  }));
  let searchTimer;
  $search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.query = $search.value; renderHome(); }, 180); });
  $slipBtn.addEventListener("click", () => go("slip"));

  if (!initData) {
    $view.innerHTML = `<div class="empty">Open this app from your picks bot in Telegram (menu button or /start).</div>`;
    renderSlipBtn();
    return;
  }
  render();
})();
