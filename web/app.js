// Homelab terminal dashboard — renders the panel from status.json and refreshes itself.
// Everything is data-driven: a new container or device gets a card automatically,
// and disappears when it goes away. No need to edit this file per setup.

const POLL_MS = 3000;          // how often to fetch status.json
const STALE_S = 30;            // warn after this many seconds without data

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ── Formatting ─────────────────────────────────────────────────
function fmtUptime(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function fmtRate(kbps) {
  if (!kbps) return "0B/s";
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)}M/s` : `${Math.round(kbps)}K/s`;
}
function fmtMem(mb) {
  if (!mb) return "—";
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}G` : `${mb.toFixed(1)}M`;
}
// How many columns and how much detail, depending on the number of cards.
function serviceDensity(n) {
  if (n <= 4) return { cols: Math.max(n, 1), mode: "" };
  if (n <= 10) return { cols: 5, mode: "" };
  if (n <= 18) return { cols: 6, mode: "compact" };
  return { cols: 8, mode: "mini" };
}
function applyDensity(el, { cols, mode }) {
  el.style.setProperty("--cols", cols);
  el.classList.remove("compact", "mini", "chips");
  if (mode) el.classList.add(mode);
}

const pct = (a, b) => (b ? Math.min(100, (a / b) * 100) : 0);
const level = (p) => (p >= 90 ? "crit" : p >= 75 ? "warn" : "");

// Sparkline: polyline + area, scaled to the box height.
function spark(values, height = 26, floor = 0) {
  const v = (values || []).filter((x) => x != null);
  if (v.length < 2) return `<svg class="spark" viewBox="0 0 100 ${height}" preserveAspectRatio="none"></svg>`;
  const max = Math.max(...v, floor, 1), min = Math.min(...v, 0);
  const span = max - min || 1;
  const pts = v.map((y, i) => {
    const x = (i / (v.length - 1)) * 100;
    const yy = height - 2 - ((y - min) / span) * (height - 4);
    return `${x.toFixed(2)},${yy.toFixed(2)}`;
  });
  const area = `0,${height} ${pts.join(" ")} 100,${height}`;
  return `<svg class="spark" viewBox="0 0 100 ${height}" preserveAspectRatio="none">
    <polygon class="area" points="${area}"></polygon>
    <polyline points="${pts.join(" ")}" vector-effect="non-scaling-stroke"></polyline></svg>`;
}

// ── Clock (browser time = kiosk time) ─────────
function tickClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("en-US", { hour12: false });
  $("date").textContent = now
    .toLocaleDateString("en-US", { weekday: "short", day: "numeric", month: "short", year: "numeric" })
    .replace(/\./g, "").toUpperCase();
}

// ── Sections ────────────────────────────────────────────────
function renderHead(d) {
  const h = d.host || {};
  if (d.site) {
    $("brand").textContent = d.site.brand || "HOME";
    $("name").textContent = d.site.name || "LAB";
    $("role").textContent = d.site.role || "";
  }
  $("h-host").textContent = h.name || "—";
  $("h-lan").textContent = h.ip || "—";
  $("h-os").textContent = h.os || "—";
  $("h-load").textContent = (h.load || []).map((x) => Number(x).toFixed(2)).join(" ") || "— — —";
  $("h-procs").textContent = `LOAD · PROCS ${h.procs ?? "—"}`;
  const w = d.weather;
  $("w-temp").textContent = w ? `${Math.round(w.temp_c)}°` : "--°";
  $("w-cond").textContent = w ? `${w.cond} - ${w.place}` : "—";
}

function renderServices(list, tasks = []) {
  const up = list.filter((s) => s.state === "running").length;
  $("svc-up").textContent = `UP ${up}/${list.length}`;
  $("svc-up").style.color = up === list.length ? "" : "var(--amber)";
  applyDensity($("services"), serviceDensity(list.length + tasks.length));
  $("services").innerHTML = list.map((s) => {
    const ok = s.state === "running";
    return `<article class="card ${ok ? "" : "down"}">
      <div class="row"><span class="name"><i class="dot ${ok ? "ok" : "bad"}"></i>${esc(s.name)}</span>
        <span class="up">${ok ? `UP ${esc(s.uptime)}` : esc(String(s.state).toUpperCase())}</span></div>
      ${spark(s.hist, 26, 5)}
      <div class="metrics">
        <span><span class="k">cpu</span><span class="v">${ok ? Number(s.cpu_pct || 0).toFixed(1) + "%" : "—"}</span></span>
        <span><span class="k">mem</span><span class="v">${ok ? fmtMem(s.mem_mb) : "—"}</span></span>
        <span class="net">↓${fmtRate(s.rx_kbps)}</span>
        <span class="net">↑${fmtRate(s.tx_kbps)}</span>
      </div>
      <div class="foot"><span>lat ${s.ms != null ? s.ms + "ms" : "—"}</span>
        <span class="links">${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url.replace(/^https?:\/\//, ""))}</a> · ` : ""}${esc(s.image)}</span></div>
    </article>`;
  }).join("") + tasks.map(renderTask).join("");
}

// Scheduled tasks (e.g. LG webOS Developer Mode renewal)
// Countdown dial: 60 clock ticks + an arc with the time left.
function dial(fraction, level) {
  const R = 44, C = 2 * Math.PI * R;
  const ticks = Array.from({ length: 60 }, (_, i) => {
    const a = (i / 60) * 2 * Math.PI - Math.PI / 2;
    const big = i % 5 === 0;
    const r1 = big ? 50 : 52, r2 = 56;
    const on = i / 60 < fraction;
    return `<line class="tick ${big ? "big" : ""} ${on ? "on" : ""}" x1="${(60 + r1 * Math.cos(a)).toFixed(2)}" y1="${(60 + r1 * Math.sin(a)).toFixed(2)}" x2="${(60 + r2 * Math.cos(a)).toFixed(2)}" y2="${(60 + r2 * Math.sin(a)).toFixed(2)}"/>`;
  }).join("");
  return `<svg class="dial ${level}" viewBox="0 0 120 120">
    ${ticks}
    <circle class="track" cx="60" cy="60" r="${R}"/>
    <circle class="arc" cx="60" cy="60" r="${R}" stroke-dasharray="${(C * fraction).toFixed(2)} ${C.toFixed(2)}" transform="rotate(-90 60 60)"/>
  </svg>`;
}

const CHECK_EVERY_H = 4;   // the renewal timer runs at 00, 04, 08, 12, 16 and 20 h

function nextCheckDate() {
  const n = new Date();
  const h = Math.floor(n.getHours() / CHECK_EVERY_H) * CHECK_EVERY_H + CHECK_EVERY_H;
  const d = new Date(n);
  d.setHours(h % 24, 0, 0, 0);
  if (h >= 24) d.setDate(d.getDate() + 1);
  return d;
}

const hhmm = (d) => d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });

function renderTask(t) {
  const ok = t.status === "ok";
  // Dial = countdown to the next scheduled check (the part that actually moves)
  const next = nextCheckDate();
  const left = Math.max(0, next - new Date()) / 1000;
  const frac = Math.min(1, left / (CHECK_EVERY_H * 3600));
  const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60);
  // Session time left drives the colour: amber under 30 %, red under 10 % or on failure
  const sess = t.remaining_s != null ? t.remaining_s / t.full_s : 0;
  const level = !ok || sess < 0.1 ? "crit" : sess < 0.3 ? "warn" : "";
  const sd = t.remaining_s != null ? Math.floor(t.remaining_s / 86400) : null;
  const sh = t.remaining_s != null ? Math.floor((t.remaining_s % 86400) / 3600) : null;
  const last = t.checked_at ? hhmm(new Date(t.checked_at * 1000)) : "—";
  return `<article class="card task ${ok ? "" : "down"}">
    <div class="row"><span class="name"><i class="dot ${ok ? "ok" : "bad"}"></i>${esc(t.name)}</span>
      <span class="up">${ok ? "ACTIVE" : "FAIL"}</span></div>
    <div class="task-body">
      <div class="dial-wrap">
        ${dial(frac, level)}
        <div class="dial-center ${level}">
          <div class="big">${h}<small>h</small> ${String(m).padStart(2, "0")}<small>m</small></div>
          <div class="k">next check</div>
        </div>
      </div>
      <div class="task-info">
        <div><span class="k">session</span><span class="v">${sd != null ? `${sd}d ${sh}h` : "—"}</span></div>
        <div><span class="k">last check</span><span class="v">${last}</span></div>
        <div><span class="k">next at</span><span class="v">${hhmm(next)}</span></div>
        <div><span class="k">renews at</span><span class="v">&lt; 30d</span></div>
      </div>
    </div>
    <div class="foot"><span>${esc(t.msg)}</span><span>LG webOS · TV</span></div>
  </article>`;
}

function renderDevices(list) {
  const on = list.filter((x) => x.state === "online").length;
  $("net-up").textContent = `ONLINE ${on}/${list.length}`;
  const chips = list.length > 6;
  applyDensity($("devices"), chips ? { cols: 8, mode: "chips" } : { cols: 6, mode: "" });
  // Chips mode fits 2 rows. On overflow: offline first and a "+N more" chip.
  const MAX = 16;
  let shown = list, hidden = 0;
  if (chips && list.length > MAX) {
    shown = [...list].sort((a, b) => (a.state === "online") - (b.state === "online")).slice(0, MAX - 1);
    hidden = list.length - shown.length;
  }
  $("devices").innerHTML = shown.map((x) => {
    const ok = x.state === "online";
    return `<article class="card ${ok ? "" : "down"}">
      <div class="row"><span class="name"><i class="dot ${ok ? "ok" : "bad"}"></i>${esc(x.name)}</span>
        <span class="up">${ok ? (x.ms != null ? x.ms + "ms" : "UP") : "OFFLINE"}</span></div>
      ${spark(x.hist, 18)}
      <div class="foot"><span>${esc(x.ip)}</span><span>${esc(x.kind)}</span></div>
    </article>`;
  }).join("") + (hidden ? `<article class="card more"><div class="row"><span class="name">+${hidden} more</span></div></article>` : "");
}

function renderSystem(h, d) {
  const disk = h.disk || {};
  const rows = [
    ["CPU", h.cpu?.pct ?? 0, `${(h.cpu?.pct ?? 0).toFixed(1)}%`,
      h.cpu?.model ? `${h.cpu.model}${h.cpu.threads ? ` · ${h.cpu.threads} threads` : ""}` : ""],
    ["MEM", pct(h.mem?.used_gb, h.mem?.total_gb), `${h.mem?.used_gb ?? "—"}/${h.mem?.total_gb ?? "—"}G`, ""],
    ["DISK", pct(disk.used_gb, disk.total_gb), `${Math.round(pct(disk.used_gb, disk.total_gb))}%`,
      disk.total_gb ? `${disk.free_gb} GB free of ${disk.total_gb} GB` : ""],
    ...(h.battery ? [["BAT", h.battery.pct ?? 0, `${h.battery.pct ?? "—"}%${h.battery.ac ? " AC" : ""}`, ""]] : []),
    ["NET", Math.min(100, ((h.net?.rx_kbps ?? 0) / 1000) * 100), fmtRate(h.net?.rx_kbps), ""],
  ];
  const gpuRows = (h.gpus || []).map((g, i) => {
    const on = g.state !== "suspended";
    const val = g.pct != null ? `${g.pct.toFixed(1)}%` : (on ? "ON" : "OFF");
    const mem = g.vram === "shared" ? "shared memory" : g.vram ? `${g.vram} VRAM` : "";
    return `<span class="lbl">GPU${i ? i + 1 : ""}</span>
      <span class="meter ${g.pct == null ? "nodata" : ""}"><i class="${level(g.pct ?? 0)}" style="width:${Math.max(g.pct == null ? 0 : 1, g.pct ?? 0).toFixed(1)}%"></i></span>
      <span class="val ${on ? "" : "off"}">${val}</span>
      <span></span><span class="model">${esc(g.name)}${mem ? " · " + mem : ""}</span>`;
  }).join("");
  $("system").innerHTML = rows.map(([l, p, v, sub]) =>
    `<span class="lbl">${l}</span>
     <span class="meter"><i class="${l === "BAT" ? "" : level(p)}" style="width:${Math.max(1, p).toFixed(1)}%"></i></span>
     <span class="val">${v}</span>
     ${sub ? `<span></span><span class="model">${esc(sub)}</span>` : ""}
     ${l === "CPU" ? gpuRows : ""}`).join("");
  $("sys-up").textContent = `UP ${fmtUptime(h.uptime_s)}`;
  $("sys-foot").innerHTML =
    `<span>↓<b>${fmtRate(h.net?.rx_kbps)}</b></span><span>↑<b>${fmtRate(h.net?.tx_kbps)}</b></span>` +
    `<span>SW <b>${h.swap_pct ?? 0}%</b></span><span>CPU <b>${h.temp_c ?? "—"}°C</b></span>`;
}

function renderDns(n) {
  if (!n) return;
  const num = (v, f = (x) => x) => (v == null ? "—" : f(v));
  const blockedPct = n.queries ? pct(n.blocked, n.queries) : null;
  const v6 = n.ipv6;   // only present when DNS_IPV6 is set
  $("dns-state").innerHTML = !(n.resolve_ok && n.block_ok)
    ? `<span style="color:var(--red)">DEGRADED</span>`
    : v6 && !v6.ok ? `<span style="color:var(--red)">${v6.present ? "IPV6 DOWN" : "IPV6 CHANGED"}</span>`
    : `<span style="color:var(--green)">NOMINAL</span>`;
  const v6alert = !v6 || v6.ok ? "" : `<div class="v6alert">${
    v6.present ? "IPv6 DNS not answering"
    : v6.current ? `IPv6 changed → set router DNS to <b>${v6.current}</b>`
    : "no global IPv6 on this host"}</div>`;
  $("dns").innerHTML = `
    <div class="stat"><div class="k">queries 24h</div><div class="v c">${num(n.queries, (v) => v.toLocaleString("en-US"))}</div></div>
    <div class="stat"><div class="k">blocked</div><div class="v r">${num(blockedPct, (v) => v.toFixed(1) + "%")}</div></div>
    <div class="stat"><div class="k">latency</div><div class="v">${num(n.avg_ms, (v) => v + "ms")}</div></div>
    <div class="stat"><div class="k">clients</div><div class="v">${num(n.clients)}</div></div>
    ${n.hist && n.hist.length > 1 ? spark(n.hist, 40) : `<div class="spark k" style="display:flex;align-items:center;justify-content:center">no AdGuard stats (missing credentials)</div>`}
    <div class="checks">
      <span><i class="dot ${n.resolve_ok ? "ok" : "bad"}"></i>resolves</span>
      <span><i class="dot ${n.block_ok ? "ok" : "bad"}"></i>blocks</span>
      ${v6 ? `<span title="${v6.expected}"><i class="dot ${v6.ok ? "ok" : "bad"}"></i>IPv6</span>` : ""}
    </div>${v6alert}`;
}

function renderLog(events) {
  const tag = { info: "INFO", ok: "OK", warn: "WARN", err: "ERR" };
  $("log").innerHTML = (events || []).map((e) =>
    `<li><span class="t">${esc(e.t)}</span><span class="lv ${esc(e.level)}">[${tag[e.level] || "INFO"}]</span>${esc(e.msg)}</li>`).join("");
}

// ── Data loop ───────────────────────────────────────────
let lastOk = 0;
async function poll() {
  try {
    const r = await fetch(`status.json?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    renderHead(d);
    renderServices(d.services || [], d.tasks || []);
    renderDevices(d.devices || []);
    renderSystem(d.host || {}, d);
    renderDns(d.dns);
    renderLog(d.events);
    lastOk = Date.now();
  } catch (e) {
    // Keep showing the last good data; the indicator below says so.
  }
  const age = lastOk ? Math.round((Date.now() - lastOk) / 1000) : null;
  $("stale").textContent = age == null ? "NO DATA" : age > STALE_S ? `NO DATA ${age}s` : "";
}

tickClock();
setInterval(tickClock, 1000);
poll();
setInterval(poll, POLL_MS);
