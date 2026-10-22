"""
Brutalist web admin panel for the Telegram Panel.

One page, zero build step, served by the same aiohttp process that already runs
``/health`` — so on Railway (single ``$PORT``) or a VPS nothing extra is needed:

    https://<your-host>/admin          → the dashboard (login protected)
    https://<your-host>/admin/api/...  → JSON API used by the page

Why it is "live": the page talks to the very same service objects the bot uses
(users, payments, channels, orders, settings, accounts, plan requests) inside the
same event loop and the same MongoDB — a change made here is visible in the bot
immediately and the other way round. The UI also refreshes itself on a timer.

Security
--------
* Password login (``ADMIN_PANEL_PASSWORD`` env, or a generated one stored in
  settings and printed in the log), signed session cookie (HMAC, http-only).
* Only users registered as **owner/admin** in the DB (or the env OWNER_ID /
  ADMIN_IDS) may log in — see ``_is_operator``.
* Multiple owners/admins are supported: the *Operators* tab manages the list
  (roles owner/admin), and every operator can be added or removed live.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COOKIE_NAME = "tp_admin"
SESSION_TTL = 60 * 60 * 12          # 12 hours
MAX_LOGIN_FAILS = 8


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Brutalist theme — raw CSS, hard edges, no framework, no external requests
# ---------------------------------------------------------------------------
"""
Brutalist theme — mobile first.

Rules of the house:
* one inline stylesheet, no external fonts/CDN (works behind any proxy and in
  countries where Google Fonts is blocked),
* every table lives in a scroll box, so a long list can never stretch the page,
* below 760px the tables become stacked label/value cards and the nav turns into
  a swipeable strip with 44px touch targets — this is what makes the dashboard
  usable on an Android/iPhone browser.
"""
BRUTAL_CSS = r"""
:root{
  --ink:#0b0b0b; --paper:#f4f1e8; --white:#ffffff; --acid:#eaff00; --hot:#ff2d55;
  --blue:#2b5bff; --green:#00c853; --shadow:6px 6px 0 var(--ink);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--paper);color:var(--ink);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  font-size:15px;line-height:1.45;-webkit-text-size-adjust:100%}
body{overflow-x:hidden}
a{color:var(--ink)}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;
  padding:14px 18px;background:var(--ink);color:var(--paper);border-bottom:6px solid var(--acid)}
header h1{margin:0;font-size:20px;letter-spacing:1.5px;text-transform:uppercase;line-height:1.1}
header .meta{display:flex;gap:8px;align-items:center;font-size:12px;flex-wrap:wrap}
.badge{border:3px solid var(--acid);padding:2px 8px;text-transform:uppercase;letter-spacing:1px;
  white-space:nowrap}
.badge.live{background:var(--acid);color:var(--ink)}
.badge.edit{border-color:var(--hot);color:var(--hot)}
nav{display:flex;gap:8px;padding:14px 18px 0;overflow-x:auto;scrollbar-width:none;
  -webkit-overflow-scrolling:touch}
nav::-webkit-scrollbar{display:none}
nav button{font:inherit;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  background:var(--white);border:3px solid var(--ink);padding:9px 12px;cursor:pointer;
  box-shadow:4px 4px 0 var(--ink);flex:0 0 auto;white-space:nowrap;min-height:42px}
nav button:hover{background:var(--acid)}
nav button.active{background:var(--ink);color:var(--paper);box-shadow:4px 4px 0 var(--hot)}
main{padding:16px 18px 90px;max-width:1400px;margin:0 auto}
.card{background:var(--white);border:4px solid var(--ink);box-shadow:var(--shadow);
  padding:14px 16px;margin:14px 0;min-width:0}
.card h2{margin:0 0 10px;font-size:16px;text-transform:uppercase;letter-spacing:1px;
  border-bottom:4px solid var(--ink);padding-bottom:7px;display:flex;gap:8px;
  align-items:center;justify-content:space-between;flex-wrap:wrap}
.card h2 small{font-size:11px;letter-spacing:0;opacity:.65;text-transform:none}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.kpi{background:var(--acid);border:4px solid var(--ink);box-shadow:var(--shadow);padding:12px;
  min-width:0}
.kpi .n{font-size:28px;font-weight:800;letter-spacing:-1px;word-break:break-word}
.kpi .l{font-size:11px;text-transform:uppercase;letter-spacing:1.5px}
/* --- tables: always inside a box, never stretch the page --- */
.table-scroll{max-height:330px;overflow:auto;border:3px solid var(--ink);background:var(--white);
  -webkit-overflow-scrolling:touch}
.table-scroll.tall{max-height:min(62vh,620px)}
.table-scroll table{border:0}
.table-scroll thead th{position:sticky;top:0;z-index:2}
.table-x{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--white)}
th,td{border:3px solid var(--ink);padding:7px 9px;text-align:left;vertical-align:top}
th{background:var(--ink);color:var(--paper);text-transform:uppercase;letter-spacing:1px;font-size:11px;
  white-space:nowrap}
tr:nth-child(even) td{background:#fffdf2}
td{overflow-wrap:anywhere}
td code{word-break:break-all;white-space:normal}
input,select,textarea{font:inherit;background:var(--white);border:3px solid var(--ink);
  padding:9px 10px;width:100%;border-radius:0;min-height:42px}
textarea{min-height:96px;resize:vertical}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
button.b{font:inherit;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  background:var(--white);border:3px solid var(--ink);padding:8px 12px;cursor:pointer;
  box-shadow:4px 4px 0 var(--ink);min-height:40px}
button.b:hover{background:var(--acid)}
button.b:active{transform:translate(2px,2px);box-shadow:2px 2px 0 var(--ink)}
button.b.p{background:var(--blue);color:var(--white)}
button.b.g{background:var(--green);color:var(--ink)}
button.b.d{background:var(--hot);color:var(--white)}
button.b.sm{padding:5px 9px;box-shadow:3px 3px 0 var(--ink);font-size:12px;min-height:34px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row > *{flex:1 1 160px;min-width:0}
.row.tight > *{flex:0 0 auto}
.actions{display:flex;gap:6px;flex-wrap:wrap}
.toast{position:fixed;right:14px;bottom:14px;left:14px;background:var(--ink);color:var(--paper);
  border:4px solid var(--acid);box-shadow:var(--shadow);padding:12px 14px;max-width:420px;
  margin-left:auto;display:none;z-index:80;font-size:13px}
.toast.show{display:block}
.tag{display:inline-block;border:2px solid var(--ink);padding:1px 6px;font-size:10px;
  text-transform:uppercase;letter-spacing:1px;background:var(--acid);white-space:nowrap}
.tag.red{background:var(--hot);color:var(--white)}
.tag.green{background:var(--green)}
.tag.blue{background:var(--blue);color:var(--white)}
.hint{font-size:12px;opacity:.75;margin:6px 0}
code{background:var(--acid);border:2px solid var(--ink);padding:0 3px;word-break:break-all}
.split{display:grid;gap:14px;grid-template-columns:1.2fr .8fr}
.login{max-width:520px;margin:6vh auto;background:var(--white);border:5px solid var(--ink);
  box-shadow:10px 10px 0 var(--ink);padding:22px}
.login h1{margin:0 0 6px;font-size:22px;letter-spacing:1.5px;text-transform:uppercase}
.sticky-save{position:sticky;bottom:0;background:var(--paper);padding:10px 0;z-index:5}
.form-grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.form-grid .wide{grid-column:1/-1}
.pill{display:inline-block;border:2px solid var(--ink);padding:1px 8px;font-size:11px;background:var(--paper)}

/* ---------- tablets ---------- */
@media (max-width:1024px){
  .split{grid-template-columns:1fr}
}

/* ---------- phones (Android / iPhone) ---------- */
@media (max-width:760px){
  header{position:sticky;top:0;z-index:60;padding:10px 12px;gap:6px;border-bottom-width:4px}
  header h1{font-size:15px;letter-spacing:1px}
  header .meta{font-size:11px;gap:6px}
  .hide-m{display:none !important}
  nav{position:sticky;top:var(--hdr,52px);z-index:55;background:var(--paper);
    padding:10px 12px 8px;border-bottom:3px solid var(--ink);gap:6px}
  nav button{padding:8px 10px;font-size:12px;box-shadow:3px 3px 0 var(--ink);min-height:40px}
  main{padding:12px 10px 96px}
  .card{padding:12px;margin:10px 0;border-width:3px;box-shadow:4px 4px 0 var(--ink)}
  .card h2{font-size:14px}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .kpi{padding:9px;box-shadow:3px 3px 0 var(--ink)}
  .kpi .n{font-size:20px}
  .kpi .l{font-size:9.5px;letter-spacing:.5px}
  .row > *{flex:1 1 100%}
  .form-grid{grid-template-columns:1fr}
  input,select,textarea{font-size:16px;padding:10px;min-height:46px} /* 16px stops iOS zoom */
  button.b{min-height:44px;padding:10px 12px}
  button.b.sm{min-height:38px}

  /* tables become stacked cards: no horizontal scrolling, nothing clipped */
  .table-scroll{max-height:none;border:0;background:transparent}
  table.mob{font-size:13px}
  table.mob thead{display:none}
  table.mob tbody,table.mob tr,table.mob td{display:block;width:100%}
  table.mob tr{border:3px solid var(--ink);background:var(--white);margin:0 0 10px;
    box-shadow:3px 3px 0 var(--ink);padding:2px}
  table.mob tr:nth-child(even) td{background:transparent}
  table.mob td{border:0;border-bottom:1px dashed #cfcbbd;padding:6px 8px;
    display:flex;gap:10px;justify-content:space-between;align-items:flex-start;
    text-align:right;max-width:none;overflow-wrap:anywhere}
  table.mob td > *:not(.actions){min-width:0}
  table.mob td:last-child{border-bottom:0}
  table.mob td::before{content:attr(data-label);font-weight:800;text-transform:uppercase;
    letter-spacing:.5px;font-size:9.5px;opacity:.6;text-align:left;flex:0 0 38%}
  table.mob td:empty{display:none}
  table.mob td .actions{justify-content:flex-end;flex:1 1 auto;min-width:0}
  table.mob td .actions button{flex:0 1 auto}
  .table-scroll table.mob thead{position:static}
  .sticky-save{bottom:0}
}

/* ---------- very small phones ---------- */
@media (max-width:380px){
  .grid{grid-template-columns:1fr}
  header h1{font-size:13px}
}
"""


BRUTAL_JS = r"""
/* ------------------------------------------------------------------
   state + tiny helpers
   ------------------------------------------------------------------ */
const S = {
  tab: "overview", auto: true, user: null, dirty: false,
  timer: null, query: "", updated: 0, busy: false,
};
const $  = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const esc = (t) => String(t ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
/* Pre-built markup (buttons, tags). fmtCell inserts it exactly as built instead
   of escaping it; every dynamic value inside is escaped where it is written. */
const RAW = Symbol("raw");
const html = (markup) => ({[RAW]: String(markup ?? "")});
const money = (v) => "₹" + Number(v || 0).toLocaleString("en-IN", {maximumFractionDigits: 2});
const hash = (text) => {
  let h = 0;
  for (let i = 0; i < text.length; i++) { h = ((h << 5) - h + text.charCodeAt(i)) | 0; }
  return text.length + ":" + h;
};

function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.borderColor = bad ? "var(--hot)" : "var(--acid)";
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 4200);
}

async function api(path, opts = {}) {
  const res = await fetch("/admin/api/" + path, {
    credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.href = "/admin/login"; throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({ok: false, error: "bad json"}));
  if (data && data.ok === false) {
    toast(data.error || "request failed", true);
    throw new Error(data.error || "failed");
  }
  return data;
}

/* ------------------------------------------------------------------
   live state indicator + dirty tracking
   ------------------------------------------------------------------ */
function isEditing() {
  if (S.dirty) return true;
  const el = document.activeElement;
  return !!el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

function setLiveState() {
  const el = $("#live-state");
  if (!el) return;
  if (!S.auto) { el.textContent = "⏸ live off"; el.className = "badge"; }
  else if (isEditing()) { el.textContent = "✎ editing — live paused"; el.className = "badge edit"; }
  else { el.textContent = "● live"; el.className = "badge live"; }
  const stamp = $("#updated");
  if (stamp && S.updated) {
    stamp.textContent = "updated " + new Date(S.updated).toLocaleTimeString();
  }
}

function markDirty() {
  if (!S.dirty) { S.dirty = true; setLiveState(); }
}

/* ------------------------------------------------------------------
   regions: patch one box in place instead of re-rendering the page
   ------------------------------------------------------------------ */
/* The initial render already carries the fingerprint, so the first timer tick
   compares equal and writes nothing at all. */
const region = (id, html) => `<div id="${id}" data-h="${hash(html)}">${html}</div>`;
function setRegion(id, html) {
  const el = document.getElementById(id);
  if (!el) return false;
  const fingerprint = hash(html);
  if (el.dataset.h === fingerprint) return false;      // nothing changed → no DOM touch
  const scrolls = $$(".table-scroll", el).map((box) => box.scrollTop);
  el.innerHTML = html;
  el.dataset.h = fingerprint;
  $$(".table-scroll", el).forEach((box, i) => { box.scrollTop = scrolls[i] || 0; });
  wire(el);
  return true;
}

/* ------------------------------------------------------------------
   tables (desktop table / mobile stacked cards)
   ------------------------------------------------------------------ */
function table(rows, cols, opts = {}) {
  rows = rows || [];
  if (!rows.length) return `<p class="hint">Nothing here yet.</p>`;
  const body = rows.map((row) => `<tr>${cols.map((col) => {
    const value = fmtCell(row[col[0]], col[0]);
    return `<td data-label="${esc(col[1])}">${value || "—"}</td>`;
  }).join("")}</tr>`).join("");
  const head = cols.map((col) => `<th>${esc(col[1])}</th>`).join("");
  const cls = opts.mob === false ? "" : ` class="mob"`;
  const html = `<table${cls}><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  if (!opts.scroll) return `<div class="table-x">${html}</div>`;
  return `<div class="table-scroll${opts.tall ? " tall" : ""}">${html}</div>`;
}

function fmtCell(v, key) {
  if (v && typeof v === "object" && v[RAW] !== undefined) return v[RAW];  // already-built HTML
  if (key === "status") return statusTag(v);
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (key === "price" || key === "balance" || key === "amount") return money(v);
  if (typeof v === "object" && v !== null) {
    return `<code>${esc(JSON.stringify(v)).slice(0, 80)}</code>`;
  }
  if (v === null || v === undefined || v === "") return "—";
  return esc(v);
}

const statusTag = (s) => `<span class="tag ${s === "completed" ? "green" :
  (s === "failed" || s === "cancelled" ? "red" : "blue")}">${esc(s)}</span>`;

/* ------------------------------------------------------------------
   navigation
   ------------------------------------------------------------------ */
const TABS = [
  ["overview", "Dashboard"], ["requests", "Requests"], ["users", "Users"],
  ["plans", "Plans"], ["pricing", "Pricing"], ["channels", "Channels"],
  ["orders", "Orders"], ["pool", "Accounts"], ["forcesub", "Force Sub", "owner"],
  ["operators", "Operators", "owner"], ["settings", "Settings"],
];
const isOwner = () => (S.user || {}).role === "owner";
const visibleTabs = () => TABS.filter((t) => t[2] !== "owner" || isOwner());

function renderNav() {
  const nav = $("#nav");
  const keep = nav.scrollLeft;
  nav.innerHTML = visibleTabs().map(([id, label]) =>
    `<button data-tab="${id}" class="${S.tab === id ? "active" : ""}">${label}</button>`).join("");
  nav.scrollLeft = keep;
  $$("button", nav).forEach((b) => {
    b.onclick = () => {
      if (S.tab === b.dataset.tab) return;
      S.tab = b.dataset.tab;
      S.dirty = false;
      draw();
    };
  });
}

/* ------------------------------------------------------------------
   region builders (shared by the full view and the live patcher)
   ------------------------------------------------------------------ */
const R = {
  kpis: (d) => `<div class="grid">
    <div class="kpi"><div class="n">${d.users}</div><div class="l">Users</div></div>
    <div class="kpi"><div class="n">${d.orders}</div><div class="l">Orders</div></div>
    <div class="kpi"><div class="n">${d.active_orders}</div><div class="l">Active orders</div></div>
    <div class="kpi"><div class="n">${money(d.revenue)}</div><div class="l">Revenue</div></div>
    <div class="kpi"><div class="n">${d.accounts_live}/${d.accounts_total}</div><div class="l">Accounts live</div></div>
    <div class="kpi"><div class="n">${d.channels}</div><div class="l">Channels</div></div>
    <div class="kpi"><div class="n">${d.requests}</div><div class="l">Plan requests</div></div>
    <div class="kpi"><div class="n">${d.topups}</div><div class="l">Pending top-ups</div></div>
  </div>`,
  recentOrders: (d) => table(d.recent_orders, [
    ["_id", "ID"], ["user_id", "User"], ["kind", "Kind"],
    ["status", "Status"], ["price", "Price"], ["created_at", "Created"],
  ], {scroll: true}),
  recentUsers: (d) => table(d.recent_users, [
    ["_id", "User"], ["first_name", "Name"], ["balance", "Wallet"], ["created_at", "Joined"],
  ], {scroll: true}),
  requests: (d) => `<div class="card"><h2>Plan requests <small>${d.requests.length}</small></h2>
    <p class="hint">Approve = the plan is granted exactly as requested and the user is notified.</p>
    ${table(d.requests.map((r) => ({...r, actions: html(r.status === "pending" ? `
      <div class="actions"><button class="b sm g" data-act="req_ok" data-id="${esc(r._id)}"
        data-extra="${r.days || 30}">Approve</button>
      <button class="b sm d" data-act="req_no" data-id="${esc(r._id)}">Reject</button></div>` : "—"),
    })), [
      ["_id", "ID"], ["user_id", "User"], ["title", "Plan"], ["price", "Price"],
      ["days", "Days"], ["channels", "+Ch"], ["accounts", "Acc"],
      ["status", "Status"], ["created_at", "Created"], ["actions", "Action"],
    ], {scroll: true, tall: true})}</div>`,
  users: (d) => `<div class="card"><h2>Users <small>${d.users.length}</small></h2>
    <div class="row"><input data-search placeholder="search id / name / username"
      value="${esc(S.query || "")}"></div>
    ${table(d.users.map((u) => {
      const plan = u.plan || {};
      return {
        ...u,
        plan_tag: html(plan.type
          ? `<span class="tag blue">${esc(plan.type)}</span> ${esc((plan.kinds || []).join("+"))}`
          : `<span class="tag">free</span>`),
        expires: plan.expires_at ? esc(String(plan.expires_at).slice(0, 10)) : "—",
        ban_tag: html(u.banned ? `<span class="tag red">banned</span>` : "—"),
        actions: html(`<div class="actions">
          <button class="b sm" data-act="wallet" data-id="${esc(u._id)}">Wallet</button>
          <button class="b sm p" data-act="grant" data-id="${esc(u._id)}">Plan</button>
          <button class="b sm" data-act="revoke" data-id="${esc(u._id)}">Revoke</button>
          <button class="b sm d" data-act="ban" data-id="${esc(u._id)}"
            data-on="${u.banned ? 1 : 0}">${u.banned ? "Unban" : "Ban"}</button>
          <button class="b sm" data-act="msg" data-id="${esc(u._id)}">Message</button></div>`),
      };
    }), [
      ["_id", "User"], ["first_name", "Name"], ["balance", "Wallet"],
      ["plan_tag", "Plan"], ["expires", "Valid till"], ["ban_tag", "Status"], ["actions", "Actions"],
    ], {scroll: true, tall: true})}</div>`,
  channels: (d) => `<div class="card"><h2>Channels <small>${d.channels.length}</small></h2>
    <p class="hint">Locked = the user may only disable automation; only an operator can delete
    or re-assign it.</p>
    ${table(d.channels.map((c) => {
      const on = (bucket) => bucket && bucket.enabled;
      return {
        ...c,
        reactions: html(`${on(c.auto_reactions) ? `<span class="tag green">on</span>` : `<span class="tag">off</span>`}
          <div class="actions"><button class="b sm" data-act="chan_auto" data-id="${esc(c._id)}"
            data-extra="auto_reactions" data-on="${on(c.auto_reactions) ? 1 : 0}">toggle</button></div>`),
        views: html(`${on(c.auto_views) ? `<span class="tag green">on</span>` : `<span class="tag">off</span>`}
          <div class="actions"><button class="b sm" data-act="chan_auto" data-id="${esc(c._id)}"
            data-extra="auto_views" data-on="${on(c.auto_views) ? 1 : 0}">toggle</button></div>`),
        lock: html(`${c.locked ? `<span class="tag red">locked</span>` : `<span class="tag">open</span>`}
          <div class="actions"><button class="b sm" data-act="chan_lock" data-id="${esc(c._id)}"
            data-on="${c.locked ? 1 : 0}">${c.locked ? "unlock" : "lock"}</button></div>`),
        actions: html(`<div class="actions">
          <button class="b sm p" data-act="chan_owner" data-id="${esc(c._id)}"
            data-on="${esc(c.owner_id || "")}">owner</button>
          <button class="b sm d" data-act="chan_del" data-id="${esc(c._id)}">delete</button></div>`),
      };
    }), [
      ["_id", "ID"], ["title", "Title"], ["owner_id", "Owner"], ["reactions", "Reactions"],
      ["views", "Views"], ["lock", "Lock"], ["actions", "Actions"],
    ], {scroll: true, tall: true})}</div>`,
  orders: (d) => `<div class="card"><h2>Orders <small>${d.orders.length}</small></h2>
    ${table(d.orders.map((o) => ({
      ...o,
      actions: html(["completed", "failed", "cancelled", "refunded"].includes(o.status) ? "—" :
        `<div class="actions">
           <button class="b sm d" data-act="order_cancel" data-id="${esc(o._id)}">Cancel</button>
           <button class="b sm" data-act="order_retry" data-id="${esc(o._id)}">Retry</button></div>`),
    })), [
      ["_id", "ID"], ["user_id", "User"], ["kind", "Kind"], ["status", "Status"],
      ["price", "Price"], ["paid_with", "Paid with"], ["created_at", "Created"],
      ["actions", "Actions"],
    ], {scroll: true, tall: true})}</div>`,
  pool: (d) => `<div class="card"><h2>Account pool <small>${d.live} live / ${d.total}</small></h2>
    <div class="row"><button class="b p" data-act="pool_revive">Run pool supervisor now</button></div>
    ${table(d.accounts.map((a) => ({
      ...a,
      status_tag: html(a.status === "active" ? `<span class="tag green">active</span>` :
        `<span class="tag red">${esc(a.status)}</span>`),
      seen: a.last_seen ? String(a.last_seen).slice(0, 10) : "—",
      actions: html(`<button class="b sm d" data-act="pool_remove" data-id="${esc(a._id)}">remove</button>`),
    })), [
      ["_id", "ID"], ["phone", "Phone"], ["name", "Name"], ["status_tag", "Status"],
      ["reactions_done", "Reactions"], ["views_done", "Views"], ["seen", "Last seen"],
      ["actions", "Actions"],
    ], {scroll: true, tall: true})}</div>`,
  forceSub: (d) => table(d.channels.map((c) => ({
    ...c,
    link: html(c.username ? `<code>@${esc(c.username)}</code>` :
      (c.invite ? `<code>${esc(String(c.invite).slice(0, 42))}</code>` : "—")),
    on: html(c.enabled === false ? `<span class="tag">off</span>` : `<span class="tag green">on</span>`),
    actions: html(`<button class="b sm d" data-act="fs_del" data-id="${esc(c.id)}">remove</button>`),
  })), [["id", "ID"], ["title", "Title"], ["link", "Link"], ["on", "State"],
       ["actions", "Actions"]], {scroll: true}),
  operators: (d) => `<div class="card"><h2>Operators <small>${d.operators.length}</small></h2>
    <p class="hint">Several owners are supported. Owners have every power; admins manage users,
    plans and channels. <code>OWNER_ID</code> is always an owner.</p>
    <div class="row"><button class="b g" data-act="op_add">Add operator</button></div>
    ${table(d.operators.map((o) => ({
      ...o,
      actions: html(`<button class="b sm d" data-act="op_del" data-id="${esc(o.user_id)}">remove</button>`),
    })), [
      ["user_id", "User id"], ["role", "Role"], ["added_at", "Added"], ["actions", "Actions"],
    ], {scroll: true})}</div>`,
};

/* ------------------------------------------------------------------
   views (full render of the current tab)
   ------------------------------------------------------------------ */
async function viewOverview() {
  const d = await api("overview");
  return `<div class="card"><h2>Live dashboard</h2>
      ${region("kpis", R.kpis(d))}</div>
    <div class="split">
      <div class="card"><h2>Recent orders</h2>
        ${region("recent-orders", R.recentOrders(d))}</div>
      <div class="card"><h2>Latest users</h2>
        ${region("recent-users", R.recentUsers(d))}</div>
    </div>
    <div class="card"><h2>Environment</h2>
      <div class="table-x"><table><tbody>
        <tr><th>Version</th><td>${esc(d.version)}</td></tr>
        <tr><th>Storage</th><td>${esc(d.storage)}</td></tr>
        <tr><th>Uptime</th><td>${Math.round(d.uptime / 60)} min</td></tr>
        <tr><th>Owners</th><td>${(d.operators || []).map(
          (o) => `<code>${o.user_id}</code> (${o.role})`).join(", ") || "—"}</td></tr>
      </tbody></table></div></div>`;
}

async function viewRequests() { return region("req-region", R.requests(await api("requests"))); }
async function viewUsers() { return region("users-region", R.users(await api("users?q=" + encodeURIComponent(S.query || "")))); }
async function viewChannels() { return region("chan-region", R.channels(await api("channels"))); }
async function viewOrders() { return region("orders-region", R.orders(await api("orders"))); }
async function viewPool() { return region("pool-region", R.pool(await api("pool"))); }
async function viewOperators() { return region("op-region", R.operators(await api("operators"))); }

async function viewPlans() {
  const d = await api("plans");
  const pause = `<p class="hint">✎ Auto-refresh is paused on this tab so your edits are never lost.</p>`;
  const card = (p) => {
    const fid = "plan_" + p.id;
    return `<div class="card"><h2>${esc(p.title || p.id)} <small>${esc(p.id)}</small></h2>
      <div id="${fid}" class="form-grid">
        <label>Title<input name="title" value="${esc(p.title || "")}"></label>
        <label>Days<input name="days" type="number" value="${esc(p.days || 0)}"></label>
        <label>Price<input name="price" type="number" step="1" value="${esc(p.price || 0)}"></label>
        <label>Accounts<input name="accounts" type="number" value="${esc(p.accounts || 0)}"></label>
        <label>Extra channels<input name="channels" type="number" value="${esc(p.channels || 0)}"></label>
        <label>Features<input name="kinds" value="${esc((p.kinds || []).join(","))}"></label>
        <label>Badge<input name="badge" value="${esc(p.badge || "")}"></label>
        <label class="wide">Description<textarea name="description">${esc(p.description || "")}</textarea></label>
      </div>
      <div class="row tight" style="margin-top:10px">
        <button class="b g" data-save="plan" data-id="${esc(p.id)}" data-form="${fid}">Save</button>
        <button class="b d" data-save="plan_del" data-id="${esc(p.id)}">Delete</button></div></div>`;
  };
  return `<div class="card"><h2>Premium plans <small>${d.plans.length}</small></h2>${pause}
      <p class="hint">These are the plans users see in <b>Buy Premium</b>. Features: all,
      reactions, views, comments, members, channels.</p></div>
    ${d.plans.map(card).join("")}
    <div class="card"><h2>New plan</h2><div id="plan_new" class="form-grid">
      <label>ID<input name="id" placeholder="premium_365"></label>
      <label>Title<input name="title" placeholder="Premium • 1 year"></label>
      <label>Days<input name="days" type="number" value="365"></label>
      <label>Price<input name="price" type="number" value="999"></label>
      <label>Accounts<input name="accounts" type="number" value="25"></label>
      <label>Extra channels<input name="channels" type="number" value="10"></label>
      <label>Features<input name="kinds" value="all"></label>
      <label>Badge<input name="badge" value=""></label>
      <label class="wide">Description<textarea name="description"></textarea></label></div>
      <div class="row tight" style="margin-top:10px">
        <button class="b g" data-save="plan_new" data-form="plan_new">Create plan</button></div>
    </div>`;
}

async function viewPricing() {
  const d = await api("pricing");
  const pause = `<p class="hint">✎ Auto-refresh is paused on this tab so your edits are never lost.</p>`;
  return pause + Object.entries(d.sections).map(([name, cfg]) => {
    const fid = "sec_" + name;
    return `<div class="card"><h2>${esc(name)}</h2>
      <div id="${fid}"><label>Section JSON
        <textarea name="__json" data-json="1">${esc(JSON.stringify(cfg, null, 2))}</textarea>
      </label></div>
      <div class="row tight"><button class="b g" data-save="section" data-id="${esc(name)}"
        data-form="${fid}">Save ${esc(name)}</button></div></div>`;
  }).join("");
}

async function viewForceSub() {
  const d = await api("force_sub");
  return `<div class="card"><h2>Force subscribe <small>${d.enabled ? "ON" : "OFF"}</small></h2>
    <p class="hint">A user must join every channel below before the bot answers. Owners and
    admins are never blocked. Add the bot to the channel first (admin rights are not
    required, membership is enough).</p>
    <div class="row tight">
      <button class="b ${d.enabled ? "d" : "g"}" data-act="fs_toggle">
        ${d.enabled ? "Turn OFF" : "Turn ON"}</button>
    </div>
    <div id="fs_add" class="row" style="margin-top:10px">
      <label>Add channel <input name="link" placeholder="@username or t.me/+invite or -100…"></label>
      <label>Title (optional) <input name="title" placeholder="Updates channel"></label>
      <button class="b g" data-save="fs_add" data-form="fs_add">Add channel</button>
    </div>
    ${region("fs-region", R.forceSub(d))}
  </div>`;
}

async function viewSettings() {
  const d = await api("settings");
  const pause = `<p class="hint">✎ Auto-refresh is paused on this tab so your edits are never lost.</p>`;
  return `<div class="card"><h2>All settings</h2>${pause}
      <p class="hint">Every threshold, price, toggle and list the bot uses.</p>
      <div id="all_settings"><textarea name="__all" data-json="1"
        style="min-height:44vh">${esc(JSON.stringify(d.settings, null, 2))}</textarea></div>
      <div class="row tight sticky-save"><button class="b g" data-save="settings"
        data-form="all_settings">Save all settings</button></div></div>
    <div class="card"><h2>Broadcast</h2>
      <div id="bc"><label>Message<textarea name="text"></textarea></label></div>
      <div class="row tight"><button class="b p" data-save="broadcast"
        data-form="bc">Send to every user</button></div></div>`;
}

const VIEWS = {
  overview: viewOverview, requests: viewRequests, users: viewUsers, plans: viewPlans,
  pricing: viewPricing, channels: viewChannels, orders: viewOrders, pool: viewPool,
  forcesub: viewForceSub, operators: viewOperators, settings: viewSettings,
};

/* full render — only on tab switch / manual refresh */
function syncHeaderHeight() {
  const bar = $("header");
  if (bar) document.documentElement.style.setProperty("--hdr", bar.offsetHeight + "px");
}
window.addEventListener("resize", syncHeaderHeight);

async function draw() {
  renderNav();
  setLiveState();
  syncHeaderHeight();
  const view = $("#view");
  view.innerHTML = `<div class="card"><h2>Loading ${esc(S.tab)}…</h2></div>`;
  try {
    view.innerHTML = await VIEWS[S.tab]();
    wire(view);
    S.updated = Date.now();
    setLiveState();
  } catch (e) {
    view.innerHTML = `<div class="card"><h2>Error</h2><p>${esc(e.message)}</p>
      <button class="b" onclick="draw()">retry</button></div>`;
  }
}

/* ------------------------------------------------------------------
   soft refresh — patches the data boxes that exist on this tab, never the
   whole page; skipped entirely while a form field is focused or dirty
   ------------------------------------------------------------------ */
const LIVE = {
  overview: async () => {
    const d = await api("overview");
    setRegion("kpis", R.kpis(d));
    setRegion("recent-orders", R.recentOrders(d));
    setRegion("recent-users", R.recentUsers(d));
  },
  requests: async () => setRegion("req-region", R.requests(await api("requests"))),
  users: async () => setRegion("users-region",
    R.users(await api("users?q=" + encodeURIComponent(S.query || "")))),
  channels: async () => setRegion("chan-region", R.channels(await api("channels"))),
  orders: async () => setRegion("orders-region", R.orders(await api("orders"))),
  pool: async () => setRegion("pool-region", R.pool(await api("pool"))),
  operators: async () => setRegion("op-region", R.operators(await api("operators"))),
  forcesub: async () => setRegion("fs-region", R.forceSub(await api("force_sub"))),
};

async function liveTick() {
  if (!S.auto || S.busy) return setLiveState();
  if (isEditing()) return setLiveState();              // ← the fix: never wipe an edit
  if (document.hidden) return;
  const fn = LIVE[S.tab];
  if (!fn) return setLiveState();                      // forms: no auto refresh at all
  S.busy = true;
  try {
    await fn();
    S.updated = Date.now();
  } catch (e) { /* transient — the next tick retries */ }
  finally { S.busy = false; setLiveState(); }
}

/* ------------------------------------------------------------------
   actions
   ------------------------------------------------------------------ */
function readForm(formId) {
  const form = document.getElementById(formId);
  const out = {};
  $$("input,select,textarea", form).forEach((i) => {
    let v = i.value;
    if (i.dataset.json) { v = JSON.parse(v); }
    else if (i.type === "number") { v = Number(v); }
    else if (i.dataset.bool) { v = (v === "true"); }
    out[i.name] = v;
  });
  return out;
}

function wire(root = document) {
  $$("[data-act]", root).forEach((el) => {
    el.onclick = async () => {
      const act = el.dataset.act, id = el.dataset.id || "", extra = el.dataset.extra || "";
      try {
        if (act === "ban") { await api(`users/${id}/ban`, {method: "POST", body: {banned: el.dataset.on !== "1"}}); toast("Ban state updated"); }
        if (act === "wallet") { const amount = prompt("Amount (₹) — negative to debit:", "100"); if (!amount) return;
          await api(`users/${id}/wallet`, {method: "POST", body: {amount: Number(amount)}}); toast("Wallet updated"); }
        if (act === "grant") { const days = prompt("Days:", "30"); if (!days) return;
          const kinds = prompt("Features (comma list or 'all'):", "all"); if (!kinds) return;
          const accounts = prompt("Accounts cap (0 = plan default):", "0");
          const channels = prompt("Extra channels (0 = plan default):", "0");
          await api(`users/${id}/plan`, {method: "POST", body: {kinds, days: Number(days), accounts: Number(accounts || 0), channels: Number(channels || 0)}});
          toast("Plan granted — user notified"); }
        if (act === "revoke") { if (!confirm("Revoke this user's plan?")) return;
          await api(`users/${id}/plan`, {method: "DELETE"}); toast("Plan revoked"); }
        if (act === "msg") { const text = prompt("Message to send (Telegram DM):"); if (!text) return;
          await api(`users/${id}/message`, {method: "POST", body: {text}}); toast("Message sent"); }
        if (act === "req_ok") { const days = prompt("Days for this plan:", extra || "30"); if (!days) return;
          await api(`requests/${id}/approve`, {method: "POST", body: {days: Number(days)}}); toast("Approved & activated"); }
        if (act === "req_no") { const reason = prompt("Reason (optional):", "");
          await api(`requests/${id}/reject`, {method: "POST", body: {reason: reason || ""}}); toast("Rejected"); }
        if (act === "chan_lock") { await api(`channels/${id}/lock`, {method: "POST", body: {locked: el.dataset.on !== "1"}}); toast("Lock updated"); }
        if (act === "chan_owner") { const owner = prompt("New owner user id:", el.dataset.on || ""); if (!owner) return;
          await api(`channels/${id}/owner`, {method: "POST", body: {owner_id: Number(owner)}}); toast("Owner set"); }
        if (act === "chan_del") { if (!confirm("Delete this channel record?")) return;
          await api(`channels/${id}`, {method: "DELETE"}); toast("Channel deleted"); }
        if (act === "chan_auto") { await api(`channels/${id}/auto`, {method: "POST", body: {kind: el.dataset.extra, enabled: el.dataset.on !== "1"}}); toast("Automation updated"); }
        if (act === "order_cancel") { const reason = prompt("Cancel reason:", "cancelled by admin");
          await api(`orders/${id}/cancel`, {method: "POST", body: {reason: reason || ""}}); toast("Order cancelled"); }
        if (act === "order_retry") { await api(`orders/${id}/retry`, {method: "POST", body: {}}); toast("Order queued again"); }
        if (act === "pool_revive") { await api("pool/revive", {method: "POST", body: {}}); toast("Pool supervise ran"); }
        if (act === "pool_remove") { if (!confirm("Remove this account from the pool?")) return;
          await api(`pool/${id}`, {method: "DELETE"}); toast("Account removed"); }
        if (act === "op_add") { const uid = prompt("Telegram user id:"); if (!uid) return;
          const role = (prompt("Role (owner/admin):", "admin") || "admin").trim();
          await api("operators", {method: "POST", body: {user_id: Number(uid), role}}); toast("Operator added"); }
        if (act === "op_del") { if (!confirm("Remove this operator?")) return;
          await api(`operators/${id}`, {method: "DELETE"}); toast("Operator removed"); }
        if (act === "fs_toggle") { await api("force_sub/toggle", {method: "POST", body: {}}); toast("Force subscribe updated"); }
        if (act === "fs_del") { if (!confirm("Remove this channel from force subscribe?")) return;
          await api(`force_sub/${id}`, {method: "DELETE"}); toast("Channel removed"); }
        S.dirty = false;
        await draw();
      } catch (e) { /* toast already shown */ }
    };
  });

  $$("[data-save]", root).forEach((el) => {
    el.onclick = async () => {
      const kind = el.dataset.save, id = el.dataset.id || "";
      try {
        if (kind === "plan") { await api(`plans/${id}`, {method: "POST", body: readForm(el.dataset.form)}); toast("Plan saved"); }
        if (kind === "plan_new") { await api("plans", {method: "POST", body: readForm(el.dataset.form)}); toast("Plan created"); }
        if (kind === "plan_del") { if (!confirm("Delete this plan?")) return; await api(`plans/${id}`, {method: "DELETE"}); toast("Plan deleted"); }
        if (kind === "section") { await api(`pricing/${id}`, {method: "POST", body: readForm(el.dataset.form)}); toast("Saved"); }
        if (kind === "settings") { await api("settings", {method: "POST", body: readForm(el.dataset.form)}); toast("Settings saved"); }
        if (kind === "broadcast") { const text = readForm(el.dataset.form).text; if (!text) return;
          const out = await api("broadcast", {method: "POST", body: {text}}); toast(`Broadcast: ${out.sent} sent, ${out.failed} failed`); }
        if (kind === "fs_add") {
          const body = readForm(el.dataset.form);
          if (!body.link) return toast("Channel link is required", true);
          await api("force_sub", {method: "POST", body});
          toast("Channel added");
        }
        S.dirty = false;
        await draw();
      } catch (e) { toast(e.message || "failed", true); }
    };
  });

  $$("[data-search]", root).forEach((el) => {
    el.onkeydown = (ev) => {
      if (ev.key === "Enter") { S.query = el.value; S.dirty = false; draw(); }
    };
    el.oninput = markDirty;
  });
}

/* ------------------------------------------------------------------
   boot
   ------------------------------------------------------------------ */
async function boot() {
  try {
    S.user = await api("me");
    $("#who").textContent = `operator ${S.user.user_id} · ${S.user.role}`;
  } catch (e) { location.href = "/admin/login"; return; }

  // any typing in the workspace pauses the live refresh
  ["input", "change", "keyup"].forEach((evt) => {
    $("#view").addEventListener(evt, (e) => {
      if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) markDirty();
    }, true);
  });
  document.addEventListener("focusout", () => setTimeout(() => {
    if (!isEditing() && !S.dirty) setLiveState();
  }, 0));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) liveTick(); });

  renderNav();
  await draw();
  S.timer = setInterval(liveTick, 12000);
  $("#auto").onclick = () => {
    S.auto = !S.auto;
    $("#auto").textContent = S.auto ? "⏸ live" : "▶ live";
    setLiveState();
  };
  $("#refresh").onclick = () => { S.dirty = false; draw(); };
}
boot();
"""


class WebAdmin:
    """The aiohttp routes + JSON API behind the dashboard."""

    def __init__(self, bot):
        self.bot = bot
        self.fails = 0

    # -- dependencies ------------------------------------------------------
    @property
    def store(self):
        return getattr(self.bot, "store", None)

    def _services(self) -> Dict[str, Any]:
        """The live service objects (same instances the bot uses)."""
        from src.services.channels import get_channel_service
        from src.services.orders import get_order_service
        from src.services.payments import get_payment_service
        from src.services.requests import get_plan_request_service
        from src.services.settings import get_settings_service
        from src.services.users import get_user_service

        settings = get_settings_service()
        users = get_user_service()
        return {
            "settings": settings,
            "users": users,
            "orders": get_order_service(),
            "payments": get_payment_service(),
            "channels": get_channel_service(),
            "requests": get_plan_request_service(users=users),
            "executor": getattr(self.bot, "executor", None),
            "registry": getattr(self.bot, "account_registry", None),
        }

    # -- auth --------------------------------------------------------------
    async def _password(self) -> str:
        from src import config as app_config

        env_pw = (os.getenv("ADMIN_PANEL_PASSWORD") or "").strip().strip("'\"")
        if env_pw:
            return env_pw
        try:
            settings = self._services()["settings"]
            web = await settings.section("web")
            if web.get("password"):
                return str(web["password"])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"web password lookup failed: {exc}")
        try:
            settings = self._services()["settings"]
            web = await settings.section("web")
            generated = web.get("generated_password")
            if not generated:
                generated = secrets.token_urlsafe(9)
                await settings.set_path("web.generated_password", generated)
                logger.warning(
                    "Web admin panel password generated: %s — change it with the "
                    "ADMIN_PANEL_PASSWORD env variable or in Settings ▸ web.password",
                    generated,
                )
            return str(generated)
        except Exception:  # pragma: no cover - last resort
            return "panel-admin"

    def _secret(self) -> bytes:
        from src import config as app_config

        raw = (os.getenv("ADMIN_PANEL_SECRET") or "").strip().strip("'\"")
        if not raw:
            raw = f"tp::{getattr(app_config, 'BOT_TOKEN', '')}::{getattr(app_config, 'OWNER_ID', 0)}"
        return hashlib.sha256(raw.encode()).digest()

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._secret(), payload, hashlib.sha256).digest())

    def _session(self, user_id: int, role: str) -> str:
        body = json.dumps({"u": user_id, "r": role, "t": int(time.time())}).encode()
        return f"{_b64(body)}.{self._sign(body)}"

    def _read_session(self, token: str) -> Optional[Dict[str, Any]]:
        try:
            raw, sig = token.split(".", 1)
            payload = _unb64(raw)
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            data = json.loads(payload)
            if int(time.time()) - int(data.get("t", 0)) > SESSION_TTL:
                return None
            return data
        except Exception:
            return None

    async def _operator(self, request) -> Optional[Dict[str, Any]]:
        token = (request.cookies.get(COOKIE_NAME) or "").strip()
        if not token:
            return None
        data = self._read_session(token)
        if not data:
            return None
        user_id = int(data.get("u") or 0)
        if not await self._is_operator(user_id):
            return None
        data["user_id"] = user_id
        return data

    async def _is_operator(self, user_id: int) -> bool:
        try:
            users = self._services()["users"]
            return bool(await users.is_admin(int(user_id)))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"operator check failed: {exc}")
            return False

    # -- routes ------------------------------------------------------------
    def install(self, app) -> None:
        """Register every route on an aiohttp app."""
        app.router.add_get("/admin", self.page)
        app.router.add_get("/admin/", self.page)
        app.router.add_get("/admin/login", self.login_page)
        app.router.add_post("/admin/api/login", self.api_login)
        app.router.add_post("/admin/api/logout", self.api_logout)
        app.router.add_get("/admin/api/me", self.api_me)

        for path, handler in (
            ("overview", self.api_overview),
            ("requests", self.api_requests),
            ("users", self.api_users),
            ("plans", self.api_plans),
            ("pricing", self.api_pricing),
            ("channels", self.api_channels),
            ("orders", self.api_orders),
            ("pool", self.api_pool),
            ("operators", self.api_operators),
            ("settings", self.api_settings),
            ("force_sub", self.api_force_sub),
        ):
            app.router.add_get(f"/admin/api/{path}", handler)
            app.router.add_post(f"/admin/api/{path}", handler)

        app.router.add_post("/admin/api/users/{user_id}/wallet", self.api_wallet)
        app.router.add_post("/admin/api/users/{user_id}/plan", self.api_grant_plan)
        app.router.add_delete("/admin/api/users/{user_id}/plan", self.api_revoke_plan)
        app.router.add_post("/admin/api/users/{user_id}/ban", self.api_ban)
        app.router.add_post("/admin/api/users/{user_id}/message", self.api_message)
        app.router.add_post("/admin/api/requests/{request_id}/approve", self.api_approve_request)
        app.router.add_post("/admin/api/requests/{request_id}/reject", self.api_reject_request)
        app.router.add_post("/admin/api/channels/{channel_id}/lock", self.api_channel_lock)
        app.router.add_post("/admin/api/channels/{channel_id}/owner", self.api_channel_owner)
        app.router.add_post("/admin/api/channels/{channel_id}/auto", self.api_channel_auto)
        app.router.add_delete("/admin/api/channels/{channel_id}", self.api_channel_delete)
        app.router.add_post("/admin/api/orders/{order_id}/cancel", self.api_order_cancel)
        app.router.add_post("/admin/api/orders/{order_id}/retry", self.api_order_retry)
        app.router.add_post("/admin/api/plans/{plan_id}", self.api_plan_save)
        app.router.add_delete("/admin/api/plans/{plan_id}", self.api_plan_delete)
        app.router.add_post("/admin/api/pricing/{section}", self.api_section_save)
        app.router.add_post("/admin/api/pool/revive", self.api_pool_revive)
        app.router.add_delete("/admin/api/pool/{doc_id}", self.api_pool_remove)
        app.router.add_delete("/admin/api/operators/{user_id}", self.api_operator_delete)
        app.router.add_post("/admin/api/broadcast", self.api_broadcast)
        app.router.add_post("/admin/api/force_sub/toggle", self.api_force_sub_toggle)
        app.router.add_delete("/admin/api/force_sub/{channel_id}", self.api_force_sub_delete)
        logger.info("Web admin panel mounted at /admin")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    async def _body(request) -> Dict[str, Any]:
        """JSON body, or an HTML form post (the login page uses a plain form)."""
        try:
            data = await request.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        try:
            post = await request.post()
            return {k: v for k, v in post.items()}
        except Exception:
            return {}

    @staticmethod
    def _ok(payload: Optional[Dict[str, Any]] = None):
        from aiohttp import web

        return web.json_response({"ok": True, **(payload or {})}, dumps=lambda v, *a, **k: json.dumps(v, default=str))

    @staticmethod
    def _err(message: str, status: int = 400):
        from aiohttp import web

        return web.json_response({"ok": False, "error": message}, status=status,
                                 dumps=lambda v, *a, **k: json.dumps(v, default=str))

    async def _guard(self, request):
        return await self._operator(request)

    async def _json_api(self, request, handler):
        operator = await self._guard(request)
        if operator is None:
            return self._err("unauthorized", 401)
        try:
            payload = await handler(request, operator)
            return self._ok(payload or {})
        except Exception as exc:  # noqa: BLE001 - the UI shows the message
            logger.error(f"web admin API {request.path} failed: {exc}", exc_info=True)
            return self._err(str(exc)[:300], 500)

    # -- pages -------------------------------------------------------------
    async def page(self, request):
        from aiohttp import web

        if await self._guard(request) is None:
            raise web.HTTPFound("/admin/login")
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0b0b">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>TELEGRAM PANEL · ADMIN</title><style>{BRUTAL_CSS}</style></head><body>
<header>
  <h1>⛓ Telegram Panel</h1>
  <div class="meta"><span id="who" class="hide-m">…</span>
    <span id="live-state" class="badge live">● live</span>
    <span id="updated" class="hide-m" style="opacity:.7"></span>
    <button class="b sm" id="auto">⏸ live</button>
    <button class="b sm" id="refresh">↻</button>
    <button class="b sm" onclick="logout()">exit</button></div>
</header>
<nav id="nav"></nav>
<main id="view"></main>
<div class="toast" id="toast"></div>
<script>
async function logout(){{ await fetch("/admin/api/logout", {{method:"POST", credentials:"same-origin"}}); location.href="/admin/login"; }}
{BRUTAL_JS}
</script></body></html>"""
        return web.Response(text=html, content_type="text/html")

    async def login_page(self, request):
        from aiohttp import web

        error = request.query.get("e", "")
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ADMIN LOGIN</title><style>{BRUTAL_CSS}</style></head><body>
<div class="login"><h1>⛓ Telegram Panel</h1>
<p class="hint">Brutalist admin console — live synced with the bot.</p>
<form method="post" action="/admin/api/login">
  <label>Operator user id<input name="user_id" placeholder="123456789" autocomplete="off"></label>
  <p></p>
  <label>Password<input name="password" type="password" autocomplete="current-password"></label>
  <p class="hint">Password: <code>ADMIN_PANEL_PASSWORD</code> env var. If it is not set, the
  generated one is printed in the bot log at startup.</p>
  {f'<p class="tag red">{error}</p>' if error else ''}
  <p><button class="b p" style="width:100%">LOGIN</button></p>
</form></div></body></html>"""
        return web.Response(text=html, content_type="text/html")

    # -- auth API ----------------------------------------------------------
    async def api_login(self, request):
        from aiohttp import web

        data = await self._body(request)
        user_id = int(str(data.get("user_id") or 0).strip() or 0)
        password = str(data.get("password") or "")
        if self.fails >= MAX_LOGIN_FAILS:
            logger.warning("Web admin: too many failed logins — refusing for now")
            raise web.HTTPFound("/admin/login?e=Too+many+attempts%2C+wait+a+minute")
        if not user_id or not password:
            raise web.HTTPFound("/admin/login?e=User+id+and+password+required")
        if not secrets.compare_digest(password, await self._password()):
            self.fails += 1
            raise web.HTTPFound("/admin/login?e=Wrong+password")
        if not await self._is_operator(user_id):
            self.fails += 1
            raise web.HTTPFound("/admin/login?e=That+user+id+is+not+an+owner%2Fadmin")
        role = "owner" if await self._services()["users"].is_owner(user_id) else "admin"
        token = self._session(user_id, role)
        response = web.HTTPFound("/admin")
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax",
                            max_age=SESSION_TTL, path="/")
        logger.info(f"Web admin login: {user_id} ({role})")
        return response

    async def api_logout(self, request):
        from aiohttp import web

        response = self._ok({"logged_out": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def api_me(self, request):
        return await self._json_api(request, lambda req, op: self._me(op))

    async def _me(self, op) -> Dict[str, Any]:
        from src.health import VERSION

        return {"user_id": op["user_id"], "role": op.get("r", "admin"), "version": VERSION}

    # -- read APIs ---------------------------------------------------------
    async def api_overview(self, request):
        async def handler(req, op):
            from src.health import VERSION

            services = self._services()
            users = services["users"]
            orders = services["orders"]
            stats = await orders.stats()
            registry = services["registry"]
            pool = await registry.stats() if registry else {}
            operators = await self._operators_list()
            store = self.store
            return {
                "version": VERSION,
                "storage": type(store).__name__ if store else "none",
                "uptime": time.time() - getattr(self.bot, "started_at", time.time()),
                "users": await users.count_users(),
                "orders": stats.get("total", 0),
                "active_orders": stats.get("pending", 0),
                "revenue": stats.get("revenue", 0),
                "accounts_live": pool.get("live", 0),
                "accounts_total": pool.get("total", 0),
                "channels": await services["channels"].count(),
                "requests": await services["requests"].pending_count(),
                "topups": len(await services["payments"].pending_topups()),
                "operators": operators,
                "recent_orders": await orders.list_all(limit=8),
                "recent_users": await users.list_users(limit=8),
            }

        return await self._json_api(request, handler)

    async def api_users(self, request):
        async def handler(req, op):
            from src.db.store import get_store_sync

            query_text = (req.query.get("q") or "").strip()
            users = self._services()["users"]
            if query_text:
                store = get_store_sync()
                docs = await store.find("users", {}, sort=[("created_at", -1)], limit=2000)
                needle = query_text.lower()
                docs = [d for d in docs
                        if needle in str(d.get("_id", "")).lower()
                        or needle in str(d.get("first_name", "")).lower()
                        or needle in str(d.get("username", "")).lower()]
                return {"users": docs[:200]}
            return {"users": await users.list_users(limit=200)}

        return await self._json_api(request, handler)

    async def api_requests(self, request):
        async def handler(req, op):
            if req.method == "POST":
                data = await self._body(req)
                doc = await self._services()["requests"].request(
                    int(data.get("user_id") or op["user_id"]),
                    kind=str(data.get("kind") or "premium"),
                    title=str(data.get("title") or "Manual request"),
                    price=float(data.get("price") or 0),
                    days=int(data.get("days") or 0),
                    accounts=int(data.get("accounts") or 0),
                    channels=int(data.get("channels") or 0),
                    note=str(data.get("note") or "created in the web panel"),
                )
                return {"requests": await self._services()["requests"].list(limit=100), "created": doc["_id"]}
            return {"requests": await self._services()["requests"].list(limit=100)}

        return await self._json_api(request, handler)

    async def api_plans(self, request):
        async def handler(req, op):
            settings = self._services()["settings"]
            cfg = await settings.section("premium")
            plans = list(cfg.get("plans") or [])
            if req.method == "POST":
                data = await self._body(req)
                pid = str(data.get("id") or "").strip() or f"plan_{int(time.time())}"
                plan = {
                    "id": pid,
                    "title": str(data.get("title") or pid),
                    "days": int(data.get("days") or 30),
                    "price": float(data.get("price") or 0),
                    "accounts": int(data.get("accounts") or 0),
                    "channels": int(data.get("channels") or 0),
                    "description": str(data.get("description") or ""),
                    "badge": str(data.get("badge") or ""),
                    "kinds": [k.strip() for k in str(data.get("kinds") or "all").replace("+", ",").split(",")
                              if k.strip()] or ["all"],
                }
                plans = [p for p in plans if str(p.get("id")) != pid] + [plan]
                await settings.set_path("premium.plans", plans)
                logger.info(f"Web admin: plan {pid} saved")
                return {"plans": plans, "saved": pid}
            return {"plans": plans}

        return await self._json_api(request, handler)

    async def api_plan_save(self, request):
        async def handler(req, op):
            plan_id = req.match_info["plan_id"]
            data = await self._body(req)
            data.setdefault("id", plan_id)
            settings = self._services()["settings"]
            cfg = await settings.section("premium")
            plans = list(cfg.get("plans") or [])
            plan = {
                "id": plan_id,
                "title": str(data.get("title") or plan_id),
                "days": int(data.get("days") or 30),
                "price": float(data.get("price") or 0),
                "accounts": int(data.get("accounts") or 0),
                "channels": int(data.get("channels") or 0),
                "description": str(data.get("description") or ""),
                "badge": str(data.get("badge") or ""),
                "kinds": [k.strip() for k in str(data.get("kinds") or "all").replace("+", ",").split(",")
                          if k.strip()] or ["all"],
            }
            plans = [p for p in plans if str(p.get("id")) != plan_id] + [plan]
            await settings.set_path("premium.plans", plans)
            return {"plans": plans, "saved": plan_id}

        return await self._json_api(request, handler)

    async def api_plan_delete(self, request):
        async def handler(req, op):
            plan_id = req.match_info["plan_id"]
            settings = self._services()["settings"]
            cfg = await settings.section("premium")
            plans = [p for p in (cfg.get("plans") or []) if str(p.get("id")) != plan_id]
            await settings.set_path("premium.plans", plans)
            logger.info(f"Web admin: plan {plan_id} deleted")
            return {"plans": plans}

        return await self._json_api(request, handler)

    async def api_pricing(self, request):
        async def handler(req, op):
            settings = self._services()["settings"]
            data = await settings.get()
            wanted = ("reactions", "views", "members", "comments", "channels", "premium",
                      "accounts", "orders", "payments", "support")
            return {"sections": {k: data.get(k, {}) for k in wanted}}

        return await self._json_api(request, handler)

    async def api_section_save(self, request):
        async def handler(req, op):
            section = req.match_info["section"]
            body = await self._body(req)
            payload = body.get("__json")
            if payload is None:
                return self._err("missing __json")
            settings = self._services()["settings"]
            await settings.update_section(section, payload if isinstance(payload, dict) else {})
            logger.info(f"Web admin: section {section} updated")
            return {"section": section, "value": await settings.section(section)}

        return await self._json_api(request, handler)

    async def api_channels(self, request):
        async def handler(req, op):
            channels = self._services()["channels"]
            docs = await channels.list_all(limit=500)
            slots = {}
            for doc in docs:
                owner = doc.get("owner_id")
                if owner:
                    slots[str(owner)] = await channels.auto_channel_count(int(owner))
            return {"channels": docs, "auto_counts": slots}

        return await self._json_api(request, handler)

    async def api_orders(self, request):
        async def handler(req, op):
            orders = self._services()["orders"]
            status = (req.query.get("status") or "").strip() or None
            if status:
                return {"orders": await orders.list_all(limit=200, statuses=[status])}
            return {"orders": await orders.list_all(limit=200)}

        return await self._json_api(request, handler)

    async def api_pool(self, request):
        async def handler(req, op):
            registry = self._services()["registry"]
            if registry is None:
                return {"accounts": [], "live": 0, "total": 0}
            accounts = await registry.store.all_accounts()
            stats = await registry.stats()
            return {"accounts": accounts, "live": stats.get("live", 0), "total": stats.get("total", 0)}

        return await self._json_api(request, handler)

    async def api_operators(self, request):
        async def handler(req, op):
            if req.method == "POST":
                await self._require_owner(op, self._services()["users"])
                data = await self._body(req)
                user_id = int(data.get("user_id") or 0)
                role = str(data.get("role") or "admin").lower()
                if role not in ("owner", "admin"):
                    return self._err("role must be owner or admin")
                await self._add_operator(user_id, role)
                return {"operators": await self._operators_list()}
            return {"operators": await self._operators_list()}

        return await self._json_api(request, handler)

    async def api_settings(self, request):
        async def handler(req, op):
            settings = self._services()["settings"]
            if req.method == "POST":
                body = await self._body(req)
                payload = body.get("__all")
                if not isinstance(payload, dict):
                    return self._err("expected the whole settings object")
                for section, value in payload.items():
                    if isinstance(value, dict):
                        await settings.update_section(section, value)
                logger.info("Web admin: settings replaced from the dashboard")
                return {"settings": await settings.get()}
            return {"settings": await settings.get()}

        return await self._json_api(request, handler)

    # -- write APIs --------------------------------------------------------
    async def api_wallet(self, request):
        async def handler(req, op):
            user_id = int(req.match_info["user_id"])
            data = await self._body(req)
            amount = float(data.get("amount") or 0)
            reason = str(data.get("reason") or "web admin adjustment")
            users = self._services()["users"]
            if amount >= 0:
                await users.credit(user_id, abs(amount), reason)
            else:
                ok = await users.debit(user_id, abs(amount), reason=reason)
                if not ok:
                    return self._err("balance too low for that debit")
            return {"balance": await users.balance(user_id)}

        return await self._json_api(request, handler)

    async def api_grant_plan(self, request):
        async def handler(req, op):
            user_id = int(req.match_info["user_id"])
            data = await self._body(req)
            users = self._services()["users"]
            kinds = data.get("kinds") or "all"
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.replace("+", ",").split(",") if k.strip()]
            plan = await users.grant_plan(
                user_id,
                kinds=kinds,
                days=int(data.get("days") or 30),
                accounts=int(data.get("accounts") or 0) or None,
                channels=int(data.get("channels") or 0) or None,
                note="granted from the web panel",
                notify=bool(data.get("notify", True)),
            )
            return {"plan": plan}

        return await self._json_api(request, handler)

    async def api_revoke_plan(self, request):
        async def handler(req, op):
            user_id = int(req.match_info["user_id"])
            users = self._services()["users"]
            await users.revoke_premium(user_id)
            return {"revoked": True}

        return await self._json_api(request, handler)

    async def api_ban(self, request):
        async def handler(req, op):
            user_id = int(req.match_info["user_id"])
            data = await self._body(req)
            users = self._services()["users"]
            logger.info(f"Web admin: ban({bool(data.get('banned'))}) for {user_id}"
                        f" — {data.get('reason') or 'web admin'}")
            await users.set_banned(user_id, bool(data.get("banned")))
            return {"banned": bool(data.get("banned"))}

        return await self._json_api(request, handler)

    async def api_message(self, request):
        async def handler(req, op):
            user_id = int(req.match_info["user_id"])
            data = await self._body(req)
            text = str(data.get("text") or "").strip()
            if not text:
                return self._err("empty message")
            from src.services.notify import get_notifier

            sent = await get_notifier().to_user(user_id, text)
            if not sent:
                return self._err("could not deliver (the user must /start the bot once)")
            return {"sent": True}

        return await self._json_api(request, handler)

    async def api_approve_request(self, request):
        async def handler(req, op):
            request_id = req.match_info["request_id"]
            data = await self._body(req)
            services = self._services()
            outcome = await services["requests"].approve(
                request_id, by=op["user_id"],
                days=int(data.get("days") or 0) or None,
                channels=int(data.get("channels") or 0) if data.get("channels") else None,
                accounts=int(data.get("accounts") or 0) if data.get("accounts") else None,
            )
            if outcome is None:
                return self._err("request not found", 404)
            return {"approved": request_id, "plan": outcome.get("plan")}

        return await self._json_api(request, handler)

    async def api_reject_request(self, request):
        async def handler(req, op):
            request_id = req.match_info["request_id"]
            data = await self._body(req)
            doc = await self._services()["requests"].reject(
                request_id, by=op["user_id"], reason=str(data.get("reason") or ""))
            if doc is None:
                return self._err("request not found", 404)
            return {"rejected": request_id}

        return await self._json_api(request, handler)

    async def api_channel_lock(self, request):
        async def handler(req, op):
            channel_id = int(req.match_info["channel_id"])
            data = await self._body(req)
            doc = await self._services()["channels"].set_locked(channel_id, bool(data.get("locked")))
            return {"channel": doc}

        return await self._json_api(request, handler)

    async def api_channel_owner(self, request):
        async def handler(req, op):
            channel_id = int(req.match_info["channel_id"])
            data = await self._body(req)
            owner_id = int(data.get("owner_id") or 0)
            if not owner_id:
                return self._err("owner_id is required")
            doc = await self._services()["channels"].set_owner(channel_id, owner_id,
                                                              source="web admin")
            return {"channel": doc}

        return await self._json_api(request, handler)

    async def api_channel_auto(self, request):
        async def handler(req, op):
            channel_id = int(req.match_info["channel_id"])
            data = await self._body(req)
            kind = str(data.get("kind") or "auto_reactions")
            enabled = bool(data.get("enabled"))
            channels = self._services()["channels"]
            if kind == "auto_views":
                doc = await channels.set_auto_views(channel_id, enabled)
            else:
                doc = await channels.set_auto_reactions(channel_id, enabled, posts_left=-1)
            return {"channel": doc}

        return await self._json_api(request, handler)

    async def api_channel_delete(self, request):
        async def handler(req, op):
            channel_id = int(req.match_info["channel_id"])
            channels = self._services()["channels"]
            try:
                from src.services.autoengage import get_auto_engagement

                await get_auto_engagement().leave_channel(channel_id)
            except Exception as exc:  # noqa: BLE001 - deletion must still work
                logger.debug(f"leave before delete failed: {exc}")
            removed = await channels.remove(channel_id, force=True)
            if not removed and await channels.get(channel_id) is None:
                # the leave-flow already dropped the record — same end result
                removed = 1
            return {"removed": removed}

        return await self._json_api(request, handler)

    async def api_order_cancel(self, request):
        async def handler(req, op):
            order_id = req.match_info["order_id"]
            data = await self._body(req)
            orders = self._services()["orders"]
            logger.info(f"Web admin: cancelling order {order_id} — "
                        f"{data.get('reason') or 'cancelled by admin'}")
            order = await orders.cancel(order_id, by_user=False)
            return {"order": order}

        return await self._json_api(request, handler)

    async def api_order_retry(self, request):
        async def handler(req, op):
            order_id = req.match_info["order_id"]
            orders = self._services()["orders"]
            order = await orders.get(order_id)
            if order is None:
                return self._err("order not found", 404)
            await orders.update(order_id, status="pending", error=None, progress="Queued")
            executor = self._services()["executor"]
            if executor is not None:
                executor.submit(await orders.get(order_id))
            return {"order": await orders.get(order_id)}

        return await self._json_api(request, handler)

    async def api_pool_revive(self, request):
        async def handler(req, op):
            registry = self._services()["registry"]
            if registry is None:
                return self._err("registry not ready")
            return {"outcome": await registry.revive_disconnected()}

        return await self._json_api(request, handler)

    async def api_pool_remove(self, request):
        async def handler(req, op):
            doc_id = req.match_info["doc_id"]
            registry = self._services()["registry"]
            accounts = await registry.store.all_accounts()
            doc = next((a for a in accounts if str(a["_id"]) == str(doc_id)), None)
            if doc is None:
                return self._err("account not found", 404)
            session_name = ""
            try:
                for name, doc_id in (getattr(registry, "_doc_by_session", {}) or {}).items():
                    if str(doc_id) == str(doc["_id"]):
                        session_name = str(name)
                        break
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"session lookup failed: {exc}")
            if session_name:
                await registry.remove_by_session(session_name, "removed from web panel")
            else:
                await registry.store.remove(doc["_id"])
                logger.info(f"Web admin: account record {doc['_id']} removed from the pool")
            return {"removed": True}

        return await self._json_api(request, handler)

    async def api_operator_add(self, request):
        async def handler(req, op):
            await self._require_owner(op, self._services()["users"])
            data = await self._body(req)
            await self._add_operator(int(data.get("user_id") or 0),
                                     str(data.get("role") or "admin").lower())
            return {"operators": await self._operators_list()}

        return await self._json_api(request, handler)

    async def api_operator_delete(self, request):
        async def handler(req, op):
            await self._require_owner(op, self._services()["users"])
            user_id = int(req.match_info["user_id"])
            await self._remove_operator(user_id)
            return {"operators": await self._operators_list()}

        return await self._json_api(request, handler)

    async def api_broadcast(self, request):
        async def handler(req, op):
            data = await self._body(req)
            text = str(data.get("text") or "").strip()
            if not text:
                return self._err("empty broadcast")
            from src.services.notify import get_notifier

            users = self._services()["users"]
            docs = await users.list_users(limit=5000)
            ids = [int(d["_id"]) for d in docs if not d.get("banned")]
            outcome = await get_notifier().broadcast(ids, text, delay=0.06)
            return outcome

        return await self._json_api(request, handler)

    # -- force subscribe ---------------------------------------------------
    async def api_force_sub(self, request):
        async def handler(req, op):
            await self._require_owner(op, self._services()["users"])
            from src.services.forcesub import get_force_sub_service

            service = get_force_sub_service()
            if req.method == "POST":
                data = await self._body(req)
                link = str(data.get("link") or "").strip()
                if not link:
                    return self._err("send @username, a t.me link or a -100… id")
                doc = await service.add_channel(link, title=str(data.get("title") or ""),
                                                added_by=int(op["user_id"]))
                logger.info(f"Web admin: force-subscribe channel {doc['id']} added")
                cfg = await service.config()
                return {"channels": cfg["channels"], "added": doc["id"]}
            cfg = await service.config()
            return {"enabled": cfg["enabled"], "channels": cfg["channels"],
                    "cache_seconds": cfg["cache_seconds"]}

        return await self._json_api(request, handler)

    async def api_force_sub_toggle(self, request):
        async def handler(req, op):
            await self._require_owner(op, self._services()["users"])
            from src.services.forcesub import get_force_sub_service

            service = get_force_sub_service()
            if await service.enabled():
                await service.set_enabled(False)
                return {"enabled": False}
            if not await service.channels():
                return self._err("add at least one channel first")
            await service.set_enabled(True)
            logger.info("Web admin: force subscribe enabled")
            return {"enabled": True}

        return await self._json_api(request, handler)

    async def api_force_sub_delete(self, request):
        async def handler(req, op):
            await self._require_owner(op, self._services()["users"])
            from src.services.forcesub import get_force_sub_service

            service = get_force_sub_service()
            removed = await service.remove_channel(req.match_info["channel_id"])
            if not removed:
                return self._err("channel not found", 404)
            return {"channels": await service.channels()}

        return await self._json_api(request, handler)

    # -- operators (multiple owners) --------------------------------------
    @staticmethod
    async def _require_owner(operator, users) -> None:
        if not await users.is_owner(int(operator.get("user_id") or 0)):
            raise PermissionError("owner only")

    async def _operators_list(self) -> List[Dict[str, Any]]:
        from src import config as app_config

        out: Dict[int, Dict[str, Any]] = {}
        owner = int(getattr(app_config, "OWNER_ID", 0) or 0)
        if owner:
            out[owner] = {"user_id": owner, "role": "owner", "added_at": "env", "source": "env"}
        for admin_id in getattr(app_config, "ADMIN_IDS", []) or []:
            try:
                value = int(admin_id)
            except (TypeError, ValueError):
                continue
            out.setdefault(value, {"user_id": value, "role": "admin", "added_at": "env",
                                   "source": "env"})
        store = self.store
        if store is not None:
            for doc in await store.find("operators", {}, limit=200):
                out[int(doc["_id"])] = {
                    "user_id": int(doc["_id"]),
                    "role": doc.get("role", "admin"),
                    "added_at": doc.get("added_at", ""),
                    "source": "panel",
                }
        return sorted(out.values(), key=lambda o: (o["role"] != "owner", o["user_id"]))

    async def _add_operator(self, user_id: int, role: str) -> None:
        if not user_id:
            raise ValueError("user id is required")
        if role not in ("owner", "admin"):
            raise ValueError("role must be owner or admin")
        store = self.store
        if store is None:
            raise ValueError("storage not ready")
        await store.update_one("operators", {"_id": int(user_id)},
                               {"$set": {"role": role, "added_at": _now()}}, upsert=True)
        users = self._services()["users"]
        try:
            await users.set_role(user_id, role)
        except Exception as exc:  # noqa: BLE001 - the operators collection is enough
            logger.debug(f"set_role failed for {user_id}: {exc}")
        logger.info(f"Web admin: operator {user_id} added as {role}")
        try:
            from src.services.notify import get_notifier

            await get_notifier().to_user(
                user_id,
                f"👑 <b>You are now {'an owner' if role == 'owner' else 'an admin'}</b>\n"
                "Open the bot and press <b>/start</b> to see the admin panel.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"operator notice failed: {exc}")

    async def _remove_operator(self, user_id: int) -> None:
        store = self.store
        if store is None:
            raise ValueError("storage not ready")
        await store.delete_one("operators", {"_id": int(user_id)})
        users = self._services()["users"]
        try:
            await users.set_role(user_id, "user")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"set_role(user) failed for {user_id}: {exc}")
        logger.info(f"Web admin: operator {user_id} removed")


_webadmin: Optional[WebAdmin] = None


def get_web_admin(bot=None) -> WebAdmin:
    global _webadmin
    if _webadmin is None or bot is not None:
        _webadmin = WebAdmin(bot or (_webadmin.bot if _webadmin else None))
    return _webadmin


def reset_web_admin() -> None:
    global _webadmin
    _webadmin = None
