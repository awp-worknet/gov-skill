/* ===================================================================
   api.js — REST + WS client for the EMG protocol
   ===================================================================
   Vanilla, no deps. The API speaks NUMERIC(28,18) decimals encoded as
   strings; this layer keeps them as strings on the wire and only
   coerces (`+x` / `parseFloat`) at the display path, where double
   precision is sufficient for the price / quantity ranges in play.
*/

export const API_BASE = "https://api.gov.works/v1";
export const WS_URL   = "wss://api.gov.works/v1/ws";

/* ---------- REST ---------------------------------------------------- */

async function get(path, opts = {}){
  const url = path.startsWith("http") ? path : API_BASE + path;
  const ctl = new AbortController();
  const t   = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 8000);
  try{
    const r = await fetch(url, { signal: ctl.signal, headers: { "accept":"application/json" } });
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("application/json") || ct.includes("application/problem")
      ? await r.json() : await r.text();
    if (!r.ok){
      // Surface the most informative message Problem+JSON gives us.
      const detail =
        (body && (body.detail || body.title)) ||
        (typeof body === "string" && body) ||
        `HTTP ${r.status}`;
      const err = new Error(detail);
      err.status = r.status; err.body = body; err.code = body && body.code;
      throw err;
    }
    return body;
  } finally { clearTimeout(t); }
}

export const api = {
  health:        () => get("/health"),
  authInfo:      () => get("/auth/info"),
  markets:       () => get("/markets"),
  market:        (id) => get(`/markets/${encodeURIComponent(id)}`),
  worknets:      () => get("/worknets"),
  book:          (mid, wn, depth=20) =>
    get(`/markets/${mid}/worknets/${wn}/book?depth=${depth}`),
  klines:        (mid, wn, { interval="1h", from, to, limit=200 } = {}) => {
    const q = new URLSearchParams({ interval });
    if (from) q.set("from", from);
    if (to)   q.set("to",   to);
    if (limit) q.set("limit", String(limit));
    return get(`/markets/${mid}/worknets/${wn}/klines?${q}`);
  },
  leaderboard:   (epoch_id, limit=20) =>
    get(`/leaderboard/epistemic?epoch_id=${epoch_id}&limit=${limit}`),
  epochResults:  (epoch_id) =>
    get(`/epochs/${encodeURIComponent(epoch_id)}/results`),
};

/* ---------- WS subscriptions --------------------------------------- *
   Public channels work without auth.hello: subscribe straight away.
   Pattern: WS.connect() returns a manager object with on(channel, cb)
   and unsubscribe(channel). Auto-reconnect with exponential backoff
   plus ±20% jitter so a thundering-herd reconnect doesn't pile up on
   the same instant.

   Wire shape (server: emg-broadcast / emg-api-ws):
   - Subscribe RPC: { method: "subscribe", params: { channels: [...] } }
   - Server-pushed notification: { method:"book.update",
       params:{ channel:"book.6.10", ... } }
   so we dispatch by `params.channel`.
   ------------------------------------------------------------------ */

export function connectWS(){
  let ws = null, alive = true;
  let backoff = 500;
  const MAX_BACKOFF = 15_000;
  let nextId = 1;
  const subs = new Map(); // channel → Set<callback>
  const pending = new Map();
  const stateListeners = new Set();
  let state = "connecting";
  function setState(s){ state = s; for (const cb of stateListeners) try { cb(s); } catch{} }

  function send(payload){
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(payload));
  }

  function rpc(method, params){
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      send({ jsonrpc:"2.0", id, method, params });
      setTimeout(() => {
        if (pending.has(id)){
          pending.delete(id);
          reject(new Error("rpc timeout: " + method));
        }
      }, 6000);
    });
  }

  function open(){
    setState("connecting");
    ws = new WebSocket(WS_URL);
    ws.addEventListener("open", () => {
      backoff = 500;
      setState("open");
      // resubscribe to every non-wildcard channel on reconnect
      const channels = [...subs.keys()].filter(c => !c.endsWith("*"));
      if (channels.length) rpc("subscribe", { channels }).catch(()=>{});
    });
    ws.addEventListener("message", ev => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      // JSON-RPC response
      if (msg.id != null && pending.has(msg.id)){
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) reject(Object.assign(new Error(msg.error.message || "rpc error"), { code: msg.error.data?.code }));
        else resolve(msg.result);
        return;
      }
      // Server-pushed notification: { method, params:{ channel, ... } }
      // Server's BroadcastEvent::to_notification_value() always emits
      // `params.channel` (per emg-broadcast/src/lib.rs); the prior
      // `params.topic` fallback referenced a field that never existed.
      if (msg.method && msg.params){
        const channel = msg.params.channel;
        if (!channel) return;
        const cbs = subs.get(channel);
        if (cbs) for (const cb of cbs) try { cb(msg.params, msg.method); } catch (e) { console.error(e); }
        // pattern-prefix listeners (e.g. "book.*" matches "book.6.10")
        for (const [k, set] of subs){
          if (k.endsWith("*") && channel.startsWith(k.slice(0,-1))){
            for (const cb of set) try { cb(msg.params, msg.method); } catch (e) { console.error(e); }
          }
        }
      }
    });
    ws.addEventListener("close", () => {
      setState("closed");
      if (!alive) return;
      // exponential backoff with ±20% jitter
      const wait = Math.min(MAX_BACKOFF, backoff);
      const jitter = (Math.random() * 0.4 - 0.2) * wait;
      backoff = Math.min(MAX_BACKOFF, (backoff * 1.7) | 0);
      setTimeout(() => { if (alive) open(); }, Math.max(50, wait + jitter));
    });
    ws.addEventListener("error", () => { try { ws.close(); } catch{} });
  }

  function on(channel, cb){
    if (!subs.has(channel)){
      subs.set(channel, new Set());
      // server-side subscribe (skip wildcard prefixes)
      if (!channel.endsWith("*") && state === "open"){
        rpc("subscribe", { channels: [channel] }).catch(()=>{});
      }
    }
    subs.get(channel).add(cb);
    return () => off(channel, cb);
  }
  function off(channel, cb){
    const s = subs.get(channel); if (!s) return;
    s.delete(cb);
    if (s.size === 0){
      subs.delete(channel);
      if (!channel.endsWith("*") && state === "open"){
        rpc("unsubscribe", { channels: [channel] }).catch(()=>{});
      }
    }
  }

  function close(){
    alive = false;
    try { ws && ws.close(); } catch{}
  }

  function onState(cb){ stateListeners.add(cb); cb(state); return () => stateListeners.delete(cb); }

  open();
  return { on, off, rpc, close, onState, get state(){ return state; } };
}

/* Lazy WS — only one connection per page, established the first
   time something asks for it. The list view (/markets/) doesn't
   need WS at all; only the detail view does. */
let lazyWs = null;
export function getWS(){
  if (lazyWs == null) lazyWs = connectWS();
  return lazyWs;
}

/* ---------- formatters --------------------------------------------- */

export function fmtPrice(s){
  if (s == null || s === "") return "—";
  const n = typeof s === "string" ? parseFloat(s) : s;
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(4);
}
export function fmtQty(s){
  if (s == null || s === "") return "—";
  const n = typeof s === "string" ? parseFloat(s) : s;
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e9) return (n/1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(2) + "K";
  return n.toFixed(2);
}
/* Formats a NUMERIC(28,18)-scaled string ("1000000.000000000000000000")
   into a human-friendly grouped integer when the fractional part is
   all zeros, otherwise drops trailing zeros at scale 4. Used for
   display fields like `total_gov_emission`. */
export function fmtAmount(s){
  if (s == null || s === "") return "—";
  const n = typeof s === "string" ? parseFloat(s) : s;
  if (!Number.isFinite(n)) return "—";
  if (Number.isInteger(n) && Math.abs(n) < 1e15)
    return n.toLocaleString("en-US");
  return fmtQty(n);
}
export function fmtDateRel(iso){
  if (!iso) return "—";
  const d = new Date(iso);
  const now = Date.now();
  const diff = (d.getTime() - now) / 1000;
  const abs = Math.abs(diff);
  const ago = diff < 0;
  let v, unit;
  if (abs < 60)        { v = abs|0;          unit = "s"; }
  else if (abs < 3600) { v = (abs/60)|0;     unit = "m"; }
  else if (abs < 86400){ v = (abs/3600)|0;   unit = "h"; }
  else                 { v = (abs/86400)|0;  unit = "d"; }
  return ago ? `${v}${unit} ago` : `in ${v}${unit}`;
}
export function fmtDateAbs(iso){
  if (!iso) return "—";
  const d = new Date(iso);
  const m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const pad = n => String(n).padStart(2,"0");
  return `${m[d.getUTCMonth()]} ${pad(d.getUTCDate())} · ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}
export function statusLabel(s){
  return ({
    pending:            "Pending",
    voting_and_trading: "Voting + Trading",
    trading_only:       "Trading Only",
    settling:           "Settling",
    completed:          "Completed",
  })[s] ?? s;
}
