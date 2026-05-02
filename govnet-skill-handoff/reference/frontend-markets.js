/* ===================================================================
   markets.js — list + detail view for /markets/
   ===================================================================
   No innerHTML on data paths. Uses tiny `h(tag, attrs, …children)`
   factory + `clear()` for DOM construction.
*/

import {
  api, getWS,
  fmtDateRel, fmtDateAbs, fmtAmount, statusLabel,
} from "/assets/api.js";
import { renderKline } from "/assets/kline.js";
import { fleuron }    from "/assets/ornaments.js";

/* ------------------------------------------------------------------
   DOM helpers
   ------------------------------------------------------------------ */
function h(tag, attrs, ...children){
  const e = document.createElement(tag);
  if (attrs){
    for (const k in attrs){
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class")        e.className = v;
      else if (k === "dataset") for (const d in v) e.dataset[d] = v[d];
      else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else                       e.setAttribute(k, v);
    }
  }
  for (const c of children.flat()){
    if (c == null || c === false) continue;
    if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
    else                                                e.appendChild(c);
  }
  return e;
}
function clear(el){ el.replaceChildren(); }

/* ------------------------------------------------------------------
   12-color palette — newspaper ink rotation, no rainbow.
   Extends c0–c5 to c0–c11 so 7+-worknet markets don't wrap.
   The CSS class .cN is set via stylesheets keyed by the index.
   ------------------------------------------------------------------ */
const COLOR_CLASSES = ["c0","c1","c2","c3","c4","c5","c6","c7","c8","c9","c10","c11"];

/* ------------------------------------------------------------------
   Router
   ------------------------------------------------------------------ */
const params   = new URLSearchParams(location.search);
const detailId = params.get("id");
const outlet   = document.getElementById("outlet");

/* The shared WS state pill in the topbar. Lives here so both the
   list view (which uses no WS) and the detail view light it up the
   same way once a WS connection actually exists. */
const wsMini = document.getElementById("ws-state-mini");
let topbarWsBound = false;
function bindTopbarWs(ws){
  // Idempotent — multiple calls would otherwise stack onState
  // listeners. Only the first call wins.
  if (topbarWsBound) return;
  topbarWsBound = true;
  ws.onState(s => {
    wsMini.textContent = s === "open" ? "live" : s;
    wsMini.style.color = s === "open" ? "var(--vermillion)" : "var(--ink-faded)";
  });
}

if (detailId)  renderDetail(detailId).catch(showFatal);
else           renderList().catch(showFatal);

function showFatal(err){
  console.error(err);
  clear(outlet);
  outlet.appendChild(emptyBlock("The wire is silent.", err.message || "Unable to reach api.gov.works."));
}

/* ===================================================================
   LIST VIEW
   =================================================================== */
async function renderList(){
  // List view doesn't need WS. Don't connect — empty pill it is.
  wsMini.textContent = "REST only";
  wsMini.style.color = "var(--ink-faded)";

  clear(outlet);

  const headRight = h("div", { class: "meta" });
  const counts = h("span", null, "—");
  const reloadBtn = h("button", {
      class: "chip",
      style: { marginTop:"6px" },
      onclick: () => location.reload(),
      title: "Refresh markets list",
    }, "↻ refresh");
  headRight.append("Pulled from ", h("b", null, "api.gov.works/v1/markets"),
                   h("br"), counts, h("br"), reloadBtn);

  const head = h("div", { class: "list-head rise d1" },
    h("div", null,
      h("span", { class: "kicker" }, "Section II · The Markets"),
      h("h1", { class: "h-section", style: { marginTop: "8px" } },
        "A Catalogue of ",
        h("em", null, "Open"), " & ", h("em", null, "Settled"), " Markets."
      ),
    ),
    headRight,
  );

  const deck = h("p", { class: "deck deck-row rise d2" },
    "Each row is a weekly emission market. The status pip indicates the present phase; ",
    "schedule cells are absolute UTC. Click any row to inspect its order book, K-line, ",
    "and prints in real time."
  );

  const tabs    = h("div", { class: "tabs", role: "tablist" });
  const listBody = h("div", { "aria-live": "polite" });
  const wrap = h("div", { class: "rise d2" }, tabs, listBody);

  outlet.append(head, deck, wrap);

  // loading state
  const loading = h("div", { style: { padding:"48px 0", textAlign:"center", color:"var(--ink-faded)" } },
    h("span", { class: "spinner" }), " loading markets…");
  listBody.appendChild(loading);

  let markets;
  try {
    const r = await api.markets();
    markets = (r && r.items) || [];
  } catch (e){
    clear(listBody);
    listBody.appendChild(emptyBlock("The wire is silent.",
      `${e.message || "Unreachable"} — try again in a moment.`));
    return;
  }

  // ----- buckets + tabs
  const buckets = {
    all:                markets,
    voting_and_trading: markets.filter(m => m.status === "voting_and_trading"),
    trading_only:       markets.filter(m => m.status === "trading_only"),
    settling:           markets.filter(m => m.status === "settling"),
    pending:            markets.filter(m => m.status === "pending"),
    completed:          markets.filter(m => m.status === "completed"),
  };
  const tabDef = [
    ["all",                "All"],
    ["voting_and_trading", "Voting + Trading"],
    ["trading_only",       "Trading Only"],
    ["settling",           "Settling"],
    ["pending",            "Pending"],
    ["completed",          "Completed"],
  ];
  const initialKey = sessionStorage.getItem("markets-tab") || "all";
  let activeKey = buckets[initialKey] ? initialKey : "all";

  // Build tab buttons ONCE. Clicks toggle is-active/aria-selected on
  // the existing buttons and repaint the table; identity survives,
  // so keyboard focus on the just-clicked tab is preserved.
  const tabBtns = new Map();
  for (const [k, lbl] of tabDef){
    const btn = h("button", {
      class: activeKey === k ? "is-active" : "",
      role: "tab",
      "aria-selected": activeKey === k ? "true" : "false",
      dataset: { k },
      onclick: () => {
        if (activeKey === k) return;
        activeKey = k;
        sessionStorage.setItem("markets-tab", k);
        for (const [kk, b] of tabBtns){
          const on = activeKey === kk;
          b.classList.toggle("is-active", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        }
        paintTable();
      }
    }, lbl, h("span", { class: "count" }, String(buckets[k].length)));
    tabs.appendChild(btn);
    tabBtns.set(k, btn);
  }

  function paintTable(){
    clear(listBody);
    const rows = buckets[activeKey];
    counts.textContent = `${rows.length} ${rows.length === 1 ? "market" : "markets"} · ${markets.length} total`;

    if (!rows.length){
      const heading = activeKey === "all"
        ? "No markets at present."
        : `No ${statusLabel(activeKey).toLowerCase()} markets at present.`;
      listBody.appendChild(emptyBlock(
        heading,
        "The first edition awaits its admin's signature. Once a market is created, it will appear here automatically."
      ));
      return;
    }

    const thead = h("thead", null,
      h("tr", null,
        h("th", { style: { width: "30%" } }, "Market"),
        h("th", { style: { width: "14%" } }, "Status"),
        h("th", { class: "num", style: { width: "11%" } }, "Σ Emission"),
        h("th", null, "Voting closes"),
        h("th", null, "Trading closes"),
        h("th", { class: "num" }, "Worknets"),
        h("th", { class: "num" }, "Strategy"),
      )
    );
    const tbody = h("tbody");
    for (const m of rows) tbody.appendChild(marketRow(m));

    listBody.appendChild(h("table", { class: "ledger" }, thead, tbody));
  }

  paintTable();
}

function marketRow(m){
  const wn = (m.worknets || []).length;
  const strat = ({
    per_market_clean_slate: "Clean Slate",
    global_ema:             "Global EMA",
    most_recent_market:     "Most-Recent",
  })[m.epistemic_prior_strategy] ?? m.epistemic_prior_strategy ?? "—";

  const descSpan = m.description
    ? h("span", { class: "desc", title: m.description }, m.description)
    : null;

  const nameCell = h("td", null,
    h("div", { class: "market-name" },
      h("span", { class: "name" }, m.name ?? "(untitled)", " ",
        h("em", null, "№ ", String(m.id))),
      descSpan
    )
  );

  // Make the row reachable + activatable from the keyboard.
  const activate = () => { location.search = "?id=" + encodeURIComponent(m.id); };
  const tr = h("tr",
    {
      class: "row-link",
      tabindex: "0",
      role: "link",
      "aria-label": `Open market ${m.name ?? "untitled"} (id ${m.id})`,
      dataset: { id: String(m.id) },
      onclick: activate,
      onkeydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " "){
          ev.preventDefault();
          activate();
        }
      },
    },
    nameCell,
    h("td", null, h("span", { class: "status-pip s-" + m.status }, statusLabel(m.status))),
    h("td", { class: "num", title: m.total_gov_emission ?? "" }, fmtAmount(m.total_gov_emission)),
    h("td", null, h("div", { class: "schedule-cell" },
      h("span", { class: "lab" }, "UTC"),
      fmtDateAbs(m.voting_close_at),
      h("br"),
      h("span", { style: { color: "var(--ink-faded)" } }, fmtDateRel(m.voting_close_at)),
    )),
    h("td", null, h("div", { class: "schedule-cell" },
      h("span", { class: "lab" }, "UTC"),
      fmtDateAbs(m.trading_close_at),
      h("br"),
      h("span", { style: { color: "var(--ink-faded)" } }, fmtDateRel(m.trading_close_at)),
    )),
    h("td", { class: "num" }, String(wn)),
    h("td", { class: "num" }, strat),
  );
  return tr;
}

/* ===================================================================
   DETAIL VIEW
   =================================================================== */
async function renderDetail(id){
  clear(outlet);

  // ---------- header shell ----------
  const crumb = h("div", { class: "crumb" }, h("a", { href: "/markets/" }, "← Markets"));
  const h1 = h("h1", null, "№ ", String(id));
  const idTag = h("div", { class: "id-tag" },
    "ID · ", h("span", { style: { fontWeight: "600", color: "var(--vermillion-d)" } }, String(id)));
  // The desc div is appended later only when the market has a
  // description, so an empty market doesn't leave a blank row gap.
  const headLeft = h("div", null, crumb, h1);
  outlet.appendChild(h("div", { class: "detail-head rise d1" }, headLeft, idTag));

  const metaGrid = h("div", { class: "meta-grid rise d2" });
  outlet.appendChild(metaGrid);

  // status-specific content goes here
  const mainHost = h("div");
  outlet.appendChild(mainHost);

  // ---------- load metadata + worknet directory in parallel ----------
  let market, worknetIndex;
  try {
    const [m, wnList] = await Promise.all([api.market(id), api.worknets().catch(() => null)]);
    market = m;
    worknetIndex = new Map();
    if (wnList && Array.isArray(wnList.items)) {
      for (const w of wnList.items) worknetIndex.set(w.id, w);
    }
  } catch (e){
    clear(metaGrid);
    metaGrid.classList.remove("meta-grid"); metaGrid.classList.add("rise");
    metaGrid.appendChild(emptyBlock(
      e.status === 404 ? "No such market." : "The wire is silent.",
      `${e.message || "Unreachable"} — see all markets.`,
      "/markets/", "all markets",
    ));
    return;
  }

  document.title = `${market.name} · Markets · The Governance Broadsheet`;
  clear(h1); h1.append(market.name ?? "(untitled)", " ", h("em", null, "№ ", String(market.id)));
  if (market.description){
    headLeft.appendChild(h("div", { class: "desc" }, market.description));
  }

  const stratLabel = ({
    per_market_clean_slate: "Clean Slate",
    global_ema:             "Global EMA",
    most_recent_market:     "Most-Recent",
  })[market.epistemic_prior_strategy] ?? market.epistemic_prior_strategy ?? "—";

  metaGrid.append(
    metaCell("Status",         h("span", { class: "status-pip s-" + market.status }, statusLabel(market.status))),
    metaCell("Voting closes",  fmtDateAbs(market.voting_close_at), fmtDateRel(market.voting_close_at)),
    metaCell("Trading closes", fmtDateAbs(market.trading_close_at), fmtDateRel(market.trading_close_at)),
    metaCell("Σ Emission",     fmtAmount(market.total_gov_emission), "govₜ"),
    metaCell("Strategy",       stratLabel),
  );

  // ---------- phase ribbon (status-aware narration) ----------
  const ribbon = buildPhaseRibbon(market);
  mainHost.appendChild(ribbon);

  // 30-second tick keeps the countdown current. Stops on
  // visibilitychange-hidden to spare CPU on background tabs.
  startCountdownTick(ribbon, market);

  // ---------- worknet set ----------
  const worknets = (market.worknets || []).slice().sort((a,b) => a.position - b.position);
  if (!worknets.length){
    mainHost.appendChild(h("div", { class: "pending-note" },
      h("h4", null, "No worknets associated."),
      h("p", null, "This market was created without a worknet set; admin handlers should have rejected such input."),
    ));
    return;
  }
  worknets.forEach((w, i) => {
    w._cls = COLOR_CLASSES[i % COLOR_CLASSES.length];
    const directory = worknetIndex && worknetIndex.get(w.worknet_id);
    w._name  = directory && directory.name ? directory.name : ("№ " + w.worknet_id);
    w._sub   = "№ " + w.worknet_id;
  });

  // ---------- watch for phase transitions (live + transitional only) ----------
  const status = market.status;
  if (status !== "completed"){
    // Lazy WS for everything from here. List view never connects.
    const ws = getWS();
    bindTopbarWs(ws);
    // Subscribe to phase channel — if THIS market changes phase, reload.
    // The full reload below tears the page down, so the unsubscribe
    // handle here is mainly a hook for future soft-navigation paths.
    const unsubPhase = ws.on("phase", payload => {
      if (Number(payload.market_id) === Number(market.id)){
        try { unsubPhase(); } catch {}
        // small debounce to let the new state settle DB-side
        setTimeout(() => location.reload(), 800);
      }
    });
  }

  // ---------- dispatch by status ----------
  if (status === "completed"){
    return renderCompletedView(mainHost, market, worknets);
  }
  if (status === "pending"){
    return renderPendingView(mainHost, market, worknets);
  }
  return renderLiveView(mainHost, market, worknets);
}

/* ===================================================================
   PHASE RIBBON
   =================================================================== */
function buildPhaseRibbon(market){
  const ribbon = h("div", { class: "phase-ribbon s-" + market.status });
  const tag    = h("span", { class: "phase-tag" }, statusLabel(market.status));
  const copy   = h("div", { class: "copy" });
  const cnt    = h("div", { class: "countdown" });
  ribbon._target = null;     // ISO string the countdown reads from
  ribbon._cntV   = null;     // <div class="v"> reference

  switch (market.status){
    case "voting_and_trading": {
      copy.append(
        "Markets are ", h("b", null, "open"), " — votes private, prices public. ",
        "The vote window closes first; trading runs on for five days more."
      );
      const v = h("div", { class: "v" }, fmtDateRel(market.voting_close_at));
      cnt.append(v, h("div", { class: "lbl" }, "voting closes"));
      ribbon._target = market.voting_close_at; ribbon._cntV = v;
      break;
    }
    case "trading_only": {
      copy.append(
        "Voting is ", h("b", null, "sealed"), "; the order book remains. ",
        "What the price does next is the verdict the votes already cast."
      );
      const v = h("div", { class: "v" }, fmtDateRel(market.trading_close_at));
      cnt.append(v, h("div", { class: "lbl" }, "trading closes"));
      ribbon._target = market.trading_close_at; ribbon._cntV = v;
      break;
    }
    case "settling": {
      copy.append(
        "Reconciliation in progress — the engine is computing ",
        h("b", null, "Wⱼ"), " from V and the closing P."
      );
      cnt.append(
        h("div", { class: "v" }, "—"),
        h("div", { class: "lbl" }, "settling"),
      );
      break;
    }
    case "completed": {
      copy.append(
        "Settled — the emission ", h("b", null, "Wⱼ"),
        " is final. What follows is the historical record."
      );
      cnt.append(
        h("div", { class: "v" }, market.settled_at ? fmtDateAbs(market.settled_at) : "—"),
        h("div", { class: "lbl" }, "settled"),
      );
      break;
    }
    case "pending": {
      copy.append(
        "Awaiting the bell — admin-defined schedule, ",
        h("b", null, "no orders accepted"), " until the voting window opens."
      );
      const v = h("div", { class: "v" }, fmtDateRel(market.voting_open_at));
      cnt.append(v, h("div", { class: "lbl" }, "voting opens"));
      ribbon._target = market.voting_open_at; ribbon._cntV = v;
      break;
    }
    default: {
      copy.append("Status: ", h("b", null, statusLabel(market.status)));
      cnt.textContent = "—";
    }
  }
  ribbon.append(tag, copy, cnt);
  return ribbon;
}

function startCountdownTick(ribbon, market){
  if (!ribbon._target || !ribbon._cntV) return;
  let timer = null;
  function cadenceFor(remainingMs){
    // Base 30s for distant targets; speed up to 1s in the final
    // 90 seconds so the countdown actually ticks through zero
    // instead of jumping "15s → 15s ago". Past the deadline, drop
    // back to 60s — the displayed string ("Xs ago" / "Xm ago")
    // only updates once per minute, so any faster cadence is a
    // wasted CPU-burn that lasts until the user navigates.
    if (remainingMs > 0 && remainingMs < 90_000) return 1_000;
    if (remainingMs <= 0) return 60_000;
    return 30_000;
  }
  function tick(){
    if (!ribbon.isConnected){ teardown(); return; }
    ribbon._cntV.textContent = fmtDateRel(ribbon._target);
    // Re-arm with a cadence appropriate to the (now updated)
    // remaining time. A fixed setInterval set at start() time
    // would lock the cadence for the lifetime of the ribbon.
    const remainingMs = ribbon._target ? +new Date(ribbon._target) - Date.now() : Infinity;
    timer = setTimeout(tick, cadenceFor(remainingMs));
  }
  function start(){
    stop();
    const remainingMs = ribbon._target ? +new Date(ribbon._target) - Date.now() : Infinity;
    timer = setTimeout(tick, cadenceFor(remainingMs));
  }
  function stop(){
    if (timer != null){ clearTimeout(timer); timer = null; }
  }
  // AbortController-scoped listener so teardown() drops it cleanly
  // on ribbon detach (avoids a permanent listener accumulating
  // every renderDetail call).
  const ac = new AbortController();
  function teardown(){
    stop();
    try { ac.abort(); } catch {}
  }
  document.addEventListener(
    "visibilitychange",
    () => {
      if (!ribbon.isConnected) { teardown(); return; }
      // On hide, stop the timer; on show, just start() — which
      // calls stop() then re-arms exactly one setTimeout. The
      // earlier shape was `tick(); start()` which armed two
      // pending timers (tick() self-rearms internally), giving a
      // double-tick burst on every tab restore.
      if (document.hidden) stop();
      else start();
    },
    { signal: ac.signal },
  );
  start();
}

/* ===================================================================
   LIVE VIEW — voting_and_trading / trading_only / settling
   =================================================================== */
function renderLiveView(mainHost, market, worknets){
  const ws = getWS();   // lazy: no-op if already up

  // ---------- DISTRIBUTION plate ----------
  const sigmaSpan = h("span", { class: "sigma" }, "Σ Pⱼ", h("b", null, "—"));
  const distStacked = h("div", { class: "dist-stacked" });
  const distRows    = h("div", { class: "dist-rows" });
  const distPlate = h("div", { class: "plate dist-plate rise d2" },
    h("div", { class: "plate-cap" },
      h("span", null,
        "The Distribution · live mids · ",
        h("span", { style: { color:"var(--vermillion-d)" } },
          "click any worknet for its order book + activity ↓"),
      ),
      sigmaSpan,
    ),
    distStacked, distRows,
  );
  mainHost.appendChild(distPlate);

  // ---------- HISTORY plate (small multiples K-line) ----------
  const intervalRow = h("span", { class: "interval-row" });
  const smallMults  = h("div", { class: "small-mults" });
  const intDisplay  = h("b", null, "1h");
  const historyPlate = h("div", { class: "plate history-plate rise d3" },
    h("div", { class: "plate-cap" },
      h("span", null, "Price History · ", intDisplay, " buckets"),
      intervalRow,
    ),
    smallMults,
  );
  mainHost.appendChild(historyPlate);

  // ---------- INSPECTOR shell ----------
  const insSwatch    = h("span", { class: "swatch" });
  const insTitleName = h("span", null, "—");
  const insTitle = h("div", { class: "ins-title" },
    insSwatch,
    h("span", null, "Order Book "),
    h("em", null, "+"),
    h("span", null, " Activity"),
    h("span", { style: { color:"var(--ink-faded)", fontSize:"0.55em",
                         marginLeft:"12px", letterSpacing:".04em" } },
      "—  "), insTitleName,
  );

  // Two ws-state pills — one over the order-book plate-cap, one over
  // the activity tape's. Both follow the same WS connection state; a
  // single onState callback drives both via this helper.
  function makeWsPill(openLabel){
    const lbl  = h("span", null, "…");
    const pill = h("span", { class: "ws-state s-" + ws.state },
      h("span", { class: "dot" }), lbl);
    return {
      pill, lbl,
      apply(s){
        pill.className = "ws-state s-" + s;
        lbl.textContent =
          s === "open"       ? openLabel
        : s === "connecting" ? "connecting…"
        :                      "offline";
      },
    };
  }
  const wsPillBook = makeWsPill("live");
  const wsPillTape = makeWsPill("live tape");
  ws.onState(s => { wsPillBook.apply(s); wsPillTape.apply(s); });

  const insHeadRight = h("div", { class: "chip-row" },
    h("span", { style: { fontFamily:"var(--mono)", fontSize:"10.5px",
                         letterSpacing:".2em", textTransform:"uppercase",
                         color:"var(--ink-faded)" } },
      "showing the selected worknet"),
  );
  const insHead = h("div", { class: "ins-head" },
    insTitle,
    h("div", { style: { display:"flex", gap:"14px", alignItems:"baseline" } },
      insHeadRight, wsPillBook.pill,
    ),
  );
  const bookGrid = h("div", { class: "book-grid", "aria-live": "polite" });
  bookGrid.appendChild(h("div", { class: "book-empty", style: { gridColumn: "1/-1" } },
    h("span", { class: "spinner" }), " loading order book…"));
  const bookPlate = h("div", { class: "plate book-plate" },
    h("div", { class: "plate-cap" }, h("span", null, "Order book")),
    bookGrid,
  );
  const tradesList = h("div", { class: "trades-list", "aria-live": "polite", "aria-relevant": "additions" });
  tradesList.appendChild(h("div", { class: "trades-empty" }, "No activity observed yet."));
  const tradesPlate = h("div", { class: "plate trades-plate" },
    h("div", { class: "plate-cap" },
      h("span", null, "Activity · 1m buckets"),
      wsPillTape.pill,
    ),
    tradesList,
  );
  const insPlates = h("div", { class: "ins-plates" }, bookPlate, tradesPlate);
  mainHost.appendChild(h("div", { class: "inspector rise d4" }, insHead, insPlates));

  // mids[wn_id] = best-mid OR fallback to initial_price
  const mids = {};
  for (const w of worknets) mids[w.worknet_id] = +w.initial_price;
  let activeWn = worknets[0].worknet_id;

  // Request tokens guard against stale-response paints — both worknet
  // switches and interval switches can race in-flight REST awaits.
  // Each switch bumps the matching token; every await completion
  // bails if the token has moved on since.
  let inspectorReqToken = 0;
  let klineReqToken     = 0;

  // ---------- DISTRIBUTION render ----------
  // The segs and rows are built ONCE; subsequent calls update text,
  // width, and is-active toggle in place — no clear+rebuild thrash on
  // every WS book push. Per-worknet last-written caches additionally
  // skip writes whose value has not changed since the prior paint.
  const distSegRefs = new Map();   // wn_id → { el, lbl, vSpan, last }
  const distRowRefs = new Map();   // wn_id → { el, bar, pxNow, delta, last }
  let distBuilt = false;
  let lastSigmaOff = null;
  let lastSigmaTxt = null;

  function paintDistribution(){
    const total = worknets.reduce((s,w) => s + (mids[w.worknet_id] || 0), 0);

    if (!distBuilt){
      clear(distStacked);
      for (const w of worknets){
        const lbl   = h("span", { class: "lbl" }, w._name);
        const vSpan = h("span", { class: "v" }, "—");
        const seg = h("div", {
          class: "seg " + w._cls,
          dataset: { wn: String(w.worknet_id) },
          onclick: () => selectWn(w.worknet_id),
        }, lbl, vSpan);
        distStacked.appendChild(seg);
        distSegRefs.set(w.worknet_id, { el: seg, lbl, vSpan, last: {} });
      }
      clear(distRows);
      for (const w of worknets){
        const cells = distRowScaffold(w);
        const pxNow = h("span", { class: "px-now", title: "Current mid price" }, "—");
        const delta = h("span", { class: "delta flat", title: "Δ vs P₀ (initial price)" }, "—");
        const row = h("div", {
          class: "dist-row",
          dataset: { wn: String(w.worknet_id) },
          onclick: () => selectWn(w.worknet_id),
        }, cells.swatch, cells.name, cells.barWrap, pxNow, cells.pxInit, delta);
        distRows.appendChild(row);
        distRowRefs.set(w.worknet_id, { el: row, bar: cells.bar, pxNow, delta, last: {} });
      }
      distBuilt = true;
    }

    for (const w of worknets){
      const v   = mids[w.worknet_id] || 0;
      const pct = total > 0 ? (v/total)*100 : (100/worknets.length);
      const isActive = w.worknet_id === activeWn;
      const widthStr = pct.toFixed(2) + "%";
      const vStr     = v.toFixed(4);

      const s = distSegRefs.get(w.worknet_id);
      const sLast = s.last;
      if (sLast.width !== widthStr){ s.el.style.width = widthStr; sLast.width = widthStr; }
      if (sLast.active !== isActive){ s.el.classList.toggle("is-active", isActive); sLast.active = isActive; }
      if (sLast.v !== vStr){
        s.el.setAttribute("title", `${w._name} (${w._sub}) — ${vStr}`);
        s.vSpan.textContent = vStr;
        sLast.v = vStr;
      }

      const r = distRowRefs.get(w.worknet_id);
      const rLast = r.last;
      if (rLast.active !== isActive){ r.el.classList.toggle("is-active", isActive); rLast.active = isActive; }
      if (rLast.width !== widthStr){ r.bar.style.width = widthStr; rLast.width = widthStr; }
      if (rLast.v !== vStr){ r.pxNow.textContent = vStr; rLast.v = vStr; }

      const init = +w.initial_price;
      const dlt  = init ? ((v - init)/init)*100 : 0;
      const dCls = Math.abs(dlt) < 0.001 ? "flat" : (dlt >= 0 ? "gain" : "loss");
      const dStr = (dlt > 0 ? "+" : (dlt < 0 ? "" : "±")) + dlt.toFixed(2) + "%";
      if (rLast.dCls !== dCls){ r.delta.className = "delta " + dCls; rLast.dCls = dCls; }
      if (rLast.dStr !== dStr){ r.delta.textContent = dStr; rLast.dStr = dStr; }
    }

    const sigOff = Math.abs(total - 1) > 0.02;
    if (sigOff !== lastSigmaOff){
      sigmaSpan.classList.toggle("off", sigOff);
      lastSigmaOff = sigOff;
    }
    const sigStr = "= " + total.toFixed(4);
    if (sigStr !== lastSigmaTxt){
      const sigB = sigmaSpan.querySelector("b");
      if (sigB) sigB.textContent = sigStr;
      lastSigmaTxt = sigStr;
    }
  }

  paintDistribution();

  // ---------- intervals + small multiples ----------
  const INTERVALS = ["1m","5m","1h","4h"];
  let activeInterval = sessionStorage.getItem("kline-interval") || "1h";
  if (!INTERVALS.includes(activeInterval)) activeInterval = "1h";
  function paintIntervals(){
    clear(intervalRow);
    for (const i of INTERVALS){
      intervalRow.appendChild(h("button",
        { class: "chip" + (i === activeInterval ? " is-active" : ""),
          onclick: () => {
            if (i === activeInterval) return;
            activeInterval = i;
            // Invalidate any in-flight loadOneKline calls so a slower
            // 1m response cannot overwrite a freshly painted 5m chart.
            klineReqToken++;
            sessionStorage.setItem("kline-interval", i);
            intDisplay.textContent = i;
            paintIntervals();
            loadAllKlines();
            // The book/tape subs are interval-independent; only the
            // inspector kline channel needs to follow the new bucket.
            wireInspectorKlineSub();
          }
        },
        i));
    }
  }
  paintIntervals();
  intDisplay.textContent = activeInterval;

  const multCards = new Map();
  function buildMults(){
    clear(smallMults);
    multCards.clear();
    for (const w of worknets){
      const svg = document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox", "0 0 360 120");
      svg.setAttribute("preserveAspectRatio","none");
      const pxLabel = h("span", { class: "px" }, "—");
      const statLine = h("span", { class: "stat" }, "—");
      const card = h("div",
        { class: "mult" + (w.worknet_id === activeWn ? " is-active" : ""),
          dataset: { wn: String(w.worknet_id) },
          onclick: () => selectWn(w.worknet_id) },
        h("div", { class: "mult-head" },
          h("div", { class: "name" },
            h("span", { class: "swatch " + w._cls }),
            w._name,
            h("span", { class: "wn-id" }, w._sub),
            w.govnet ? h("span", { style: { color:"var(--gilt)", marginLeft:"4px" } }, "★") : null),
          pxLabel,
        ),
        svg,
        h("div", { class: "mult-foot" },
          statLine,
          h("span", { class: "stat" }, h("b", null, w.initial_price ? (+w.initial_price).toFixed(4) : "—"), " · P₀"),
        ),
      );
      smallMults.appendChild(card);
      multCards.set(w.worknet_id, { card, svg, pxLabel, statLine });
    }
  }
  buildMults();

  async function loadOneKline(w){
    const myToken = klineReqToken;
    const fromMs = ({ "1m": 60*60_000, "5m": 6*60*60_000,
                       "1h": 7*24*3600_000, "4h": 30*24*3600_000 })[activeInterval] || 24*3600_000;
    const fromIso = new Date(Date.now() - fromMs).toISOString();
    const toIso   = new Date().toISOString();
    const card = multCards.get(w.worknet_id);
    if (!card) return;
    // visual fade during fetch — keeps the user from reading stale
    // numbers while a new request is in flight.
    card.card.classList.add("loading");
    let rows = [];
    try {
      rows = await api.klines(market.id, w.worknet_id, { interval: activeInterval, from: fromIso, to: toIso });
    } catch {
      if (myToken !== klineReqToken) return;   // interval changed
      renderKline(card.svg, [], { emptyText: "no candles yet" });
      card.pxLabel.textContent = "—"; card.pxLabel.className = "px";
      card.statLine.textContent = "no data";
      card.card.classList.remove("loading");
      return;
    }
    if (myToken !== klineReqToken) return;     // stale, do not paint
    renderKline(card.svg, rows || []);
    if (rows && rows.length){
      const last = +rows[rows.length-1].close;
      const first = +rows[0].open;
      const delta = first ? ((last - first)/first)*100 : 0;
      card.pxLabel.textContent = (delta>=0?"+":"") + delta.toFixed(2) + "%  ·  " + last.toFixed(4);
      card.pxLabel.className = "px " + (delta >= 0 ? "gain" : "loss");
      const vol = rows.reduce((s,r)=>s+(+r.volume||0), 0);
      const n   = rows.reduce((s,r)=>s+(+r.trade_count||0), 0);
      clear(card.statLine);
      card.statLine.append(
        "vol ", h("b", null, vol >= 1000 ? (vol/1000).toFixed(1) + "K" : vol.toFixed(2)),
        " · ", h("b", null, String(n)), " trades",
      );
    } else {
      card.pxLabel.textContent = "no candles"; card.pxLabel.className = "px";
      card.statLine.textContent = "—";
    }
    card.card.classList.remove("loading");
  }
  function loadAllKlines(){ return Promise.all(worknets.map(loadOneKline)); }
  loadAllKlines();

  // ---------- per-worknet book state — applied from BookDelta ----------
  // Server pushes BookDelta with `changes: [{side, price, new_quantity}]`
  // where `new_quantity` is the absolute resting quantity at that
  // price level after the change (0 = level removed). So we maintain
  // per-worknet bid/ask price→quantity Maps, apply each delta in
  // place, and derive both the mid (for the distribution) and the
  // depth-20 inspector view from the same state. No REST refetch
  // per WS push. Sequence numbers are monotonic per channel; we
  // ignore any out-of-order arrivals.
  const bookState = new Map();   // wn_id → { bids, asks, lastSeq }
  function getBook(wn_id){
    let st = bookState.get(wn_id);
    if (!st){
      st = { bids: new Map(), asks: new Map(), lastSeq: 0 };
      bookState.set(wn_id, st);
    }
    return st;
  }
  function applyBookDelta(wn_id, payload){
    const st = getBook(wn_id);
    const seq = +payload.sequence;
    // Server's BroadcastEvent::BookDelta always carries `sequence`
    // (per emg-broadcast/src/lib.rs:480-518), so a missing or
    // non-finite seq is a malformed push — drop it rather than
    // applying without dedup (a future replay could double-apply).
    if (!Number.isFinite(seq)) return false;
    if (seq <= st.lastSeq) return false;   // out of order / replay
    st.lastSeq = seq;
    for (const c of payload.changes || []){
      const side = c.side === "bid" ? st.bids : c.side === "ask" ? st.asks : null;
      if (!side) continue;
      const key = String(c.price);
      const q = +c.new_quantity;
      if (!Number.isFinite(q) || q <= 0) side.delete(key);
      else side.set(key, q);
    }
    return true;
  }
  function bestPrice(map, dir){
    let best = null;
    for (const k of map.keys()){
      const p = +k;
      if (!Number.isFinite(p)) continue;
      if (best == null || (dir === "desc" ? p > best : p < best)) best = p;
    }
    return best;
  }
  function midOfWorknet(wn_id){
    const st = bookState.get(wn_id);
    if (!st) return null;
    const bb = bestPrice(st.bids, "desc");
    const ba = bestPrice(st.asks, "asc");
    if (bb != null && ba != null) return (bb + ba) / 2;
    if (ba != null) return ba;
    if (bb != null) return bb;
    return null;
  }
  function bookSnapshotFor(wn_id, depth = 12){
    const st = bookState.get(wn_id);
    if (!st) return { bids: [], asks: [] };
    const collect = (map, dir) => {
      const arr = [];
      for (const [k, q] of map) arr.push({ price: +k, total_quantity: q });
      arr.sort((a, b) => dir === "desc" ? b.price - a.price : a.price - b.price);
      return arr.slice(0, depth);
    };
    return { bids: collect(st.bids, "desc"), asks: collect(st.asks, "asc") };
  }

  // Single book subscription per worknet — drives both the
  // distribution mid AND (when wn === activeWn) the inspector view.
  // Splitting these into two subscriptions would double-handle every
  // delta and complicate ordering; one path serves both.
  const wnBookUnsubs = [];
  function subscribeWorknetBooks(){
    for (const u of wnBookUnsubs) try { u(); } catch {}
    wnBookUnsubs.length = 0;
    for (const w of worknets){
      // REST seed — currently a Phase-2B stub returning empty
      // bids/asks so it's effectively a no-op, but keep the call
      // so once the REST handler is implemented we get warm-start
      // levels without another client rewrite.
      api.book(market.id, w.worknet_id, 20).then(snap => {
        const st = getBook(w.worknet_id);
        for (const b of (snap && snap.bids) || []){
          const q = +b.total_quantity;
          if (Number.isFinite(q) && q > 0) st.bids.set(String(b.price), q);
        }
        for (const a of (snap && snap.asks) || []){
          const q = +a.total_quantity;
          if (Number.isFinite(q) && q > 0) st.asks.set(String(a.price), q);
        }
        const m = midOfWorknet(w.worknet_id);
        if (m != null){ mids[w.worknet_id] = m; paintDistribution(); }
        if (w.worknet_id === activeWn) renderInspectorBook();
      }).catch(()=>{});
      const channel = `book.${market.id}.${w.worknet_id}`;
      const unsub = ws.on(channel, payload => {
        if (!applyBookDelta(w.worknet_id, payload)) return;
        const m = midOfWorknet(w.worknet_id);
        if (m != null){ mids[w.worknet_id] = m; paintDistribution(); }
        if (w.worknet_id === activeWn) renderInspectorBook();
      });
      wnBookUnsubs.push(unsub);
    }
  }
  subscribeWorknetBooks();

  // ---------- inspector book + activity tape ----------
  // Activity tape is sourced from the public `klines.{m}.{wn}.1m`
  // channel + a REST seed. Each push represents the latest 1m bucket
  // (the matcher publishes aggregates after every fill). This is the
  // best public-only signal of "what just traded" — `fills.me` is
  // authenticated and per-principal, so it cannot drive a public tape.
  let unsubInspectorKline = null, unsubTapeKline = null;
  let lastTapeBucket = null;        // dedup tape entries by bucket
  let lastTapeRow    = null;        // top tape row, for in-place mutation
  let lastTradePrice = null;        // baseline for up/dn coloring

  // Render the inspector book grid from the shared client-side
  // bookState for the currently active worknet. Called from
  // subscribeWorknetBooks's WS callback whenever a delta touches
  // activeWn, and once on worknet switch.
  function renderInspectorBook(){
    renderBook(bookSnapshotFor(activeWn, 20));
  }

  async function subscribeInspector(){
    // Bump the inspector token; any in-flight REST awaits from a
    // prior call will bail at their completion check.
    const myToken = ++inspectorReqToken;

    if (unsubInspectorKline) { try { unsubInspectorKline(); } catch {} unsubInspectorKline = null; }
    if (unsubTapeKline) { try { unsubTapeKline(); } catch {} unsubTapeKline = null; }

    // Reset per-worknet tape state.
    lastTapeBucket = null;
    lastTapeRow    = null;
    lastTradePrice = null;

    // Tape spinner; book is driven by the worknet-wide subscription
    // already wired in subscribeWorknetBooks, so render whatever has
    // accumulated immediately and let subsequent deltas update it.
    clear(tradesList);
    tradesList.appendChild(h("div", { class: "trades-empty" },
      h("span", { class: "spinner" }), "  seeding activity…"));
    renderInspectorBook();

    await seedActivityTape(myToken).catch(()=>{});
    if (myToken !== inspectorReqToken) return;   // user moved on

    // active-worknet kline (drives the inspector small-mult refresh)
    wireInspectorKlineSub();
    // 1m kline channel — the activity tape's data source
    wireTapeKlineSub();
  }

  async function seedActivityTape(myToken){
    // pull the last 30 1m buckets.
    const fromIso = new Date(Date.now() - 30 * 60_000).toISOString();
    let rows = [];
    try {
      rows = await api.klines(market.id, activeWn, { interval: "1m", from: fromIso, to: new Date().toISOString(), limit: 60 });
    } catch (e){
      if (myToken !== inspectorReqToken) return;
      // 404 means engine not spawned yet; tape just stays empty.
      clear(tradesList);
      tradesList.appendChild(h("div", { class: "trades-empty", style: { fontStyle: "italic" } },
        e && e.status === 404
          ? "No engine for this worknet — no activity."
          : "Could not seed activity. The wire is silent."));
      return;
    }
    if (myToken !== inspectorReqToken) return;
    clear(tradesList);
    if (!rows || !rows.length){
      tradesList.appendChild(h("div", { class: "trades-empty" }, "No activity yet."));
      return;
    }
    // Sort newest-first to take the 30 most recent, then iterate
    // OLDEST-FIRST so each insertBefore-at-top yields newest-at-top in
    // display order, AND so lastTradePrice progresses chronologically
    // for correct up/dn coloring.
    // Server spec: REST klines + WS klines.update both use `bucket`.
    // The `timestamp` fallback is a vestige of an older field name —
    // keep it as a defensive read but lead with the canonical key.
    rows.sort((a,b) => +new Date(b.bucket || b.timestamp) - +new Date(a.bucket || a.timestamp));
    const recent = rows.slice(0, 30);
    for (let i = recent.length - 1; i >= 0; i--) pushBucket(recent[i], true);
  }

  function wireInspectorKlineSub(){
    if (unsubInspectorKline) { try { unsubInspectorKline(); } catch {} unsubInspectorKline = null; }
    const klineChan = `klines.${market.id}.${activeWn}.${activeInterval}`;
    unsubInspectorKline = ws.on(klineChan, () => {
      const w = worknets.find(x => x.worknet_id === activeWn);
      if (w) loadOneKline(w);
    });
  }

  function wireTapeKlineSub(){
    if (unsubTapeKline) { try { unsubTapeKline(); } catch {} unsubTapeKline = null; }
    const tapeChan = `klines.${market.id}.${activeWn}.1m`;
    unsubTapeKline = ws.on(tapeChan, payload => {
      pushBucket(payload, false);
    });
  }

  function pushBucket(b, fromSeed){
    // REST kline rows carry `timestamp`; WS pushes carry `bucket`.
    const ts = b.bucket || b.timestamp;
    if (!ts) return;
    // Empty buckets are noise; skip on EVERY path (seed and live).
    const n = +b.trade_count || 0;
    if (n === 0) return;

    const close  = +b.close;
    const t      = new Date(ts);
    const pad    = z => String(z).padStart(2,"0");
    // Append Z so users can't read the time as local.
    const tStr   = `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}Z`;
    const vol    = +b.volume || 0;
    const volStr = vol >= 1000 ? (vol/1000).toFixed(2)+"K" : vol.toFixed(2);

    // Same bucket as visible top row → mutate in place. The matcher
    // republishes the same 1m bucket as fills accrue; each push
    // carries the bucket's running close/volume/n-trades. Returning
    // early would freeze the row at the first fill and lose every
    // subsequent print. lastTradePrice is intentionally NOT updated
    // here — the bucket has not closed, so the up/dn baseline is
    // still the prior bucket's close.
    if (!fromSeed && lastTapeBucket === ts && lastTapeRow){
      const cls = lastTradePrice == null ? "" :
        (close > lastTradePrice ? "up" : (close < lastTradePrice ? "dn" : ""));
      const cells = lastTapeRow.children;
      if (cells[1]){
        cells[1].textContent = close.toFixed(4);
        cells[1].className   = "price" + (cls ? " " + cls : "");
      }
      if (cells[2]) cells[2].textContent = volStr;
      if (cells[3]) cells[3].textContent = n + "×";
      return;
    }

    // New bucket
    tradesList.querySelectorAll(".trades-empty").forEach(e => e.remove());
    const cls = lastTradePrice == null ? "" :
      (close > lastTradePrice ? "up" : (close < lastTradePrice ? "dn" : ""));
    lastTradePrice = close;
    lastTapeBucket = ts;
    const row = h("div", { class: "row" },
      h("span", { class: "t" }, tStr),
      h("span", { class: "price" + (cls ? " " + cls : "") }, close.toFixed(4)),
      h("span", null, volStr),
      h("span", { class: "side" }, n + "×"),
    );
    tradesList.insertBefore(row, tradesList.firstChild);
    lastTapeRow = row;
    while (tradesList.children.length > 80) tradesList.lastChild.remove();
  }

  function renderBook(s){
    const bids = (s && s.bids || []).slice(0, 12);
    const asks = (s && s.asks || []).slice(0, 12);
    clear(bookGrid);

    if (!bids.length && !asks.length){
      bookGrid.appendChild(h("div",
        { class: "book-empty", style: { gridColumn: "1/-1", fontStyle: "italic" } },
        "Book is empty — no resting orders."));
      return;
    }

    const maxQ = Math.max(
      ...bids.map(b => +b.total_quantity || 0),
      ...asks.map(a => +a.total_quantity || 0),
      1,
    );

    const bidSide = h("div", { class: "book-side bid" },
      h("h5", null, h("span", null, "Bids"), h("span", null, "price ↑"))
    );
    for (const b of bids){
      const q = +b.total_quantity || 0;
      const p = +b.price;
      const w = (q / maxQ * 100).toFixed(1) + "%";
      bidSide.appendChild(h("div", { class: "row", style: { "--w": w } },
        h("span", { class: "qty" }, q.toFixed(2)),
        h("span", { class: "price" }, p.toFixed(4)),
      ));
    }
    if (!bids.length) bidSide.appendChild(h("div", { class: "book-empty" }, "no bids"));

    const askSide = h("div", { class: "book-side ask" },
      h("h5", null, h("span", null, "price ↓"), h("span", null, "Asks"))
    );
    for (const a of asks){
      const q = +a.total_quantity || 0;
      const p = +a.price;
      const w = (q / maxQ * 100).toFixed(1) + "%";
      askSide.appendChild(h("div", { class: "row", style: { "--w": w } },
        h("span", { class: "qty" }, q.toFixed(2)),
        h("span", { class: "price" }, p.toFixed(4)),
      ));
    }
    if (!asks.length) askSide.appendChild(h("div", { class: "book-empty" }, "no asks"));

    const bestBid = bids[0] ? +bids[0].price : null;
    const bestAsk = asks[0] ? +asks[0].price : null;
    const mid     = (bestBid != null && bestAsk != null) ? (bestBid + bestAsk) / 2 : null;
    const spread  = (bestBid != null && bestAsk != null) ? (bestAsk - bestBid) : null;

    bookGrid.append(bidSide, askSide,
      h("div", { class: "book-spread" },
        h("span", null, "spread " + (spread != null ? spread.toFixed(4) : "—")),
        h("span", { class: "mid" }, "mid " + (mid != null ? mid.toFixed(4) : "—")),
        h("span", null, "depth " + maxQ.toFixed(2)),
      ),
    );
  }

  // ---------- selection ----------
  function selectWn(wnId){
    const same = activeWn === wnId;
    if (!same){
      activeWn = wnId;
      paintDistribution();
      multCards.forEach((c, wn) => c.card.classList.toggle("is-active", wn === wnId));
      const w = worknets.find(x => x.worknet_id === wnId);
      if (w){
        insSwatch.className = "swatch " + w._cls;
        insTitleName.textContent = w._name + " · " + w._sub + (w.govnet ? "  ★" : "");
      }
      subscribeInspector();
    }
    document.querySelector(".inspector")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  // initial inspector title
  {
    const w = worknets.find(x => x.worknet_id === activeWn);
    if (w){
      insSwatch.className = "swatch " + w._cls;
      insTitleName.textContent = w._name + " · " + w._sub + (w.govnet ? "  ★" : "");
    }
  }

  // initial subscription
  subscribeInspector();
}

/* ===================================================================
   COMPLETED VIEW
   =================================================================== */
async function renderCompletedView(mainHost, market, worknets){
  // ---------- Settled-results plate ----------
  const settledRows = h("div", { class: "settled-rows" });
  const totalsRow = h("div", { class: "settled-totals" });
  const settledPlate = h("div", { class: "plate dist-plate rise d2" },
    h("div", { class: "plate-cap" },
      h("span", null, "Settlement Record · final emission Wⱼ"),
      h("span", { style: {
        fontFamily:"var(--mono)", fontSize:"10.5px", letterSpacing:".24em",
        textTransform:"uppercase", color:"var(--ink-faded)" } }, "archived"),
    ),
    settledRows,
    totalsRow,
  );
  mainHost.appendChild(settledPlate);

  const intervalRow = h("span", { class: "interval-row" });
  const smallMults  = h("div", { class: "small-mults" });
  const intDisplay  = h("b", null, "1h");
  const historyPlate = h("div", { class: "plate history-plate rise d3" },
    h("div", { class: "plate-cap" },
      h("span", null, "Historical Price · ", intDisplay, " buckets · the trading window"),
      intervalRow,
    ),
    smallMults,
  );
  mainHost.appendChild(historyPlate);

  let results = null;
  try {
    results = await api.epochResults(market.id);
  } catch (e){
    settledRows.appendChild(h("div",
      { class: "book-empty", style: { fontStyle: "italic", padding: "24px 12px" } },
      e.status === 404
        ? "Settlement aggregate not yet available."
        : (e.message || "results unreachable")));
  }

  const v = (results && results.v_vector) || [];
  const w = (results && results.w_vector) || [];
  const p = (results && results.p_open_vector) || [];

  // The server-side contract is "ordered by position ASC" — the same
  // sort the client applies in renderDetail. If the lengths don't
  // match the worknet count we have no way to repair the alignment;
  // surface that so it shows up in the console rather than displaying
  // mis-attributed numbers silently.
  if (results){
    for (const [name, vec] of [["v_vector", v], ["w_vector", w], ["p_open_vector", p]]){
      if (vec.length && vec.length !== worknets.length){
        console.warn(`[markets] settlement ${name} length ${vec.length} ≠ worknets ${worknets.length} — display alignment may be wrong`);
      }
    }
  }

  const sumV = v.reduce((s,x) => s + (+x || 0), 0);
  const sumW = w.reduce((s,x) => s + (+x || 0), 0);
  const sumP = p.reduce((s,x) => s + (+x || 0), 0);

  for (let i = 0; i < worknets.length; i++){
    const wn = worknets[i];
    const vi = +(v[i] ?? 0);
    const wi = +(w[i] ?? 0);
    const pi = +(p[i] ?? +wn.initial_price);

    const triple = h("div", { class: "triple" },
      h("div", { class: "col" },
        h("div", { class: "lbl" }, "P · open"),
        h("div", { class: "v" }, pi.toFixed(4)),
        h("div", { class: "bar", style: { "--w": (sumP > 0 ? (pi / sumP * 100) : 0).toFixed(1) + "%" } }),
      ),
      h("div", { class: "col v" },
        h("div", { class: "lbl" }, "V · vote mean"),
        h("div", { class: "v" }, results ? vi.toFixed(4) : "—"),
        h("div", { class: "bar", style: { "--w": (sumV > 0 ? (vi / sumV * 100) : 0).toFixed(1) + "%" } }),
      ),
      h("div", { class: "col w" },
        h("div", { class: "lbl" }, "W · emission"),
        h("div", { class: "v" }, results ? wi.toFixed(4) : "—"),
        h("div", { class: "bar", style: { "--w": (sumW > 0 ? (wi / sumW * 100) : 0).toFixed(1) + "%" } }),
      ),
    );
    settledRows.appendChild(h("div", { class: "settled-row" },
      h("span", { class: "swatch " + wn._cls }),
      h("span", { class: "name" },
        wn._name,
        h("span", { class: "wn-id" }, wn._sub),
        wn.govnet ? h("span", { style: { color:"var(--gilt)", marginLeft:"4px" }, title: "GovNet" }, "★") : null),
      triple,
    ));
  }

  if (results){
    totalsRow.append(
      h("span", null, "Σ V = ", h("b", null, sumV.toFixed(4))),
      h("span", null, "Σ W = ", h("b", null, sumW.toFixed(4))),
      h("span", null, "total emission ", h("b", null, results.total_gov_tokens != null
        ? fmtAmount(results.total_gov_tokens) + " govₜ"
        : "—")),
      market.settled_at ? h("span", null, "settled ",
        h("b", null, fmtDateAbs(market.settled_at)), " · ",
        fmtDateRel(market.settled_at)) : null,
    );
  }

  const INTERVALS = ["1m","5m","1h","4h"];
  let activeInterval = sessionStorage.getItem("kline-interval") || "1h";
  if (!INTERVALS.includes(activeInterval)) activeInterval = "1h";
  // Bumped on every interval switch so a slow earlier response can
  // not paint over a freshly loaded chart.
  let klineReqToken = 0;

  const multCards = new Map();
  function buildMults(){
    clear(smallMults);
    multCards.clear();
    for (const wn of worknets){
      const svg = document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox", "0 0 360 120");
      svg.setAttribute("preserveAspectRatio","none");
      const pxLabel = h("span", { class: "px" }, "—");
      const statLine = h("span", { class: "stat" }, "—");
      const card = h("div", { class: "mult", dataset: { wn: String(wn.worknet_id) } },
        h("div", { class: "mult-head" },
          h("div", { class: "name" },
            h("span", { class: "swatch " + wn._cls }),
            wn._name,
            h("span", { class: "wn-id" }, wn._sub),
            wn.govnet ? h("span", { style: { color:"var(--gilt)", marginLeft:"4px" } }, "★") : null),
          pxLabel,
        ),
        svg,
        h("div", { class: "mult-foot" },
          statLine,
          h("span", { class: "stat" }, h("b", null, wn.initial_price ? (+wn.initial_price).toFixed(4) : "—"), " · P₀"),
        ),
      );
      smallMults.appendChild(card);
      multCards.set(wn.worknet_id, { card, svg, pxLabel, statLine });
    }
  }

  function paintIntervals(){
    clear(intervalRow);
    for (const i of INTERVALS){
      intervalRow.appendChild(h("button",
        { class: "chip" + (i === activeInterval ? " is-active" : ""),
          onclick: () => {
            if (i === activeInterval) return;
            activeInterval = i;
            klineReqToken++;
            sessionStorage.setItem("kline-interval", i);
            intDisplay.textContent = i;
            paintIntervals();
            loadAllKlines();
          }
        },
        i));
    }
  }
  buildMults();
  paintIntervals();
  intDisplay.textContent = activeInterval;

  async function loadOneKline(wn){
    const myToken = klineReqToken;
    const fromIso = market.voting_open_at;
    const toIso   = market.settled_at || market.trading_close_at;
    const card = multCards.get(wn.worknet_id);
    if (!card) return;
    card.card.classList.add("loading");
    let rows = [];
    try {
      rows = await api.klines(market.id, wn.worknet_id, { interval: activeInterval, from: fromIso, to: toIso });
    } catch {
      if (myToken !== klineReqToken) return;
      renderKline(card.svg, [], { emptyText: "no candles archived" });
      card.pxLabel.textContent = "—"; card.pxLabel.className = "px";
      card.statLine.textContent = "no data";
      card.card.classList.remove("loading");
      return;
    }
    if (myToken !== klineReqToken) return;
    renderKline(card.svg, rows || []);
    if (rows && rows.length){
      const last = +rows[rows.length-1].close;
      const first = +rows[0].open;
      const delta = first ? ((last - first)/first)*100 : 0;
      card.pxLabel.textContent = (delta>=0?"+":"") + delta.toFixed(2) + "%  ·  " + last.toFixed(4);
      card.pxLabel.className = "px " + (delta >= 0 ? "gain" : "loss");
      const vol = rows.reduce((s,r)=>s+(+r.volume||0), 0);
      const n   = rows.reduce((s,r)=>s+(+r.trade_count||0), 0);
      clear(card.statLine);
      card.statLine.append(
        "vol ", h("b", null, vol >= 1000 ? (vol/1000).toFixed(1) + "K" : vol.toFixed(2)),
        " · ", h("b", null, String(n)), " trades",
      );
    } else {
      card.pxLabel.textContent = "no candles"; card.pxLabel.className = "px";
      card.statLine.textContent = "—";
    }
    card.card.classList.remove("loading");
  }
  function loadAllKlines(){ return Promise.all(worknets.map(loadOneKline)); }
  loadAllKlines();
}

/* ===================================================================
   PENDING VIEW
   =================================================================== */
function renderPendingView(mainHost, market, worknets){
  const sigmaSpan = h("span", { class: "sigma" }, "Σ P₀", h("b", null, "—"));
  const distStacked = h("div", { class: "dist-stacked" });
  const distRows    = h("div", { class: "dist-rows" });
  const distPlate = h("div", { class: "plate dist-plate rise d2" },
    h("div", { class: "plate-cap" },
      h("span", null, "Planned Distribution · initial prices P₀"),
      sigmaSpan,
    ),
    distStacked, distRows,
  );
  mainHost.appendChild(distPlate);

  const total = worknets.reduce((s,w) => s + (+w.initial_price || 0), 0);
  for (const w of worknets){
    const v = +w.initial_price || 0;
    const pct = total > 0 ? (v / total) * 100 : (100 / worknets.length);
    distStacked.appendChild(h("div",
      { class: "seg " + w._cls,
        style: { width: pct.toFixed(2) + "%" },
        title: `${w._name} (${w._sub}) — P₀ ${v.toFixed(4)}` },
      h("span", { class: "lbl" }, w._name),
      h("span", { class: "v" }, v.toFixed(3)),
    ));
  }
  sigmaSpan.classList.toggle("off", Math.abs(total - 1) > 0.02);
  const sigB = sigmaSpan.querySelector("b");
  if (sigB) sigB.textContent = "= " + total.toFixed(4);

  for (const w of worknets){
    const v = +w.initial_price || 0;
    const pct = total > 0 ? (v / total) * 100 : (100 / worknets.length);
    const cells = distRowScaffold(w);
    cells.bar.style.width = pct.toFixed(2) + "%";
    // No live mid yet — px-now is intentionally dashed and dimmed
    // so it reads as "to come" rather than "zero".
    distRows.appendChild(h("div", { class: "dist-row" },
      cells.swatch, cells.name, cells.barWrap,
      h("span", { class: "px-now", style: { color: "var(--ink-faded)" } }, "—"),
      cells.pxInit,
      h("span", { class: "delta flat" }, "—"),
    ));
  }

  mainHost.appendChild(h("div", { class: "pending-note rise d3" },
    h("h4", null, "Awaiting the bell."),
    h("p", null,
      "This market is scheduled but not yet trading. ",
      "Engines will spawn at ", fmtDateAbs(market.voting_open_at),
      "; until then the order book is sealed and no order will be accepted."),
  ));
}

/* ===================================================================
   small reusable widgets
   =================================================================== */
/* Distribution-row scaffold shared by the live and pending views.
   Returns the static cells (swatch, name+id+star, bar-wrap+bar,
   P₀ column) so callers can compose them with their own pxNow +
   delta + interactivity. */
function distRowScaffold(w){
  const init = +w.initial_price;
  const swatch = h("span", { class: "swatch " + w._cls });
  const name = h("span", { class: "name" },
    w._name,
    h("span", { class: "wn-id", title: "Worknet id" }, w._sub),
    w.govnet ? h("span", { class: "gov-star", title: "GovNet" }, "★") : null);
  const bar = h("div", { class: "bar " + w._cls });
  const barWrap = h("div", { class: "bar-wrap" }, bar);
  const pxInit = h("span", { class: "px-init" },
    h("span", { class: "lab" }, "P₀"), init.toFixed(4));
  return { swatch, name, barWrap, bar, pxInit };
}

function metaCell(label, primary, sub){
  return h("div", { class: "meta-cell" },
    h("div", { class: "lbl" }, label),
    h("div", { class: "v" },
      primary,
      sub != null ? h("small", null, sub) : null,
    ),
  );
}

function emptyBlock(title, body, linkHref, linkText){
  const linkSuffix = linkHref
    ? [" — see ",
       h("a",
         { href: linkHref, style: { color:"var(--vermillion-d)", borderBottom:"1px solid currentColor" } },
         linkText || "more")]
    : [];
  return h("div", { class: "empty" },
    fleuron(),
    h("h4", null, title),
    h("p", null, body, ...linkSuffix),
  );
}
