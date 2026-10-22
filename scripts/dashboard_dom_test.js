/**
 * Real DOM test of the dashboard's soft refresh (jsdom).
 *
 * Reproduces the operator's bug: while editing a form the 12 s auto refresh used
 * to replace the whole page and wipe the typed values. Now:
 *   * a tick while a field is focused/dirty must not touch the DOM,
 *   * a tick on a read-only tab patches only the changed boxes (same nodes),
 *   * an unchanged tick must not touch the DOM at all,
 *   * KPI/table data still updates live.
 */
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const css = fs.readFileSync("/tmp/admin.css", "utf8");
const js = fs.readFileSync("/tmp/admin.js", "utf8");

const DATA = {
  overview: {
    version: "3.5.0", storage: "MemoryStore", uptime: 125, users: 42, orders: 7,
    active_orders: 2, revenue: 1500, accounts_live: 5, accounts_total: 6,
    channels: 3, requests: 1, topups: 0,
    operators: [{user_id: 9001, role: "owner"}],
    recent_orders: [{_id: "o1", user_id: 1, kind: "views", status: "completed",
      price: 10, created_at: "2026-10-17"}],
    recent_users: [{_id: 1, first_name: "A", balance: 5, created_at: "2026-10-17"}],
  },
  requests: {requests: [
    {_id: "r1", user_id: 7, title: "Premium", price: 199, days: 30, channels: 5,
     accounts: 15, status: "pending", created_at: "2026-10-17"},
  ]},
  users: {users: [{_id: 7, first_name: "A", balance: 5, plan: {}, banned: false}]},
  plans: {plans: [{id: "p1", title: "Premium", days: 30, price: 199, accounts: 15,
    channels: 5, kinds: ["all"], badge: "", description: ""}]},
  pricing: {sections: {reactions: {free_account_limit: 8}}},
  channels: {channels: [{_id: "-1004312382560", title: "Test User Channel #1",
    owner_id: 6974168540, auto_reactions: {enabled: true}, auto_views: {enabled: false},
    locked: false}]},
  orders: {orders: [{_id: "o1", user_id: 7, kind: "views", status: "pending",
    price: 10, paid_with: "wallet", created_at: "2026-10-17"}]},
  pool: {accounts: [{_id: "tg_69741", phone: "+15550000003", name: "Kaka Kaka",
    status: "active", reactions_done: 28, views_done: 15, last_seen: "2026-10-17"}],
    live: 1, total: 1},
  operators: {operators: [{user_id: 9001, role: "owner", added_at: "env"},
    {user_id: 555, role: "admin", added_at: "2026-10-17"}]},
  force_sub: {enabled: true, channels: [{id: "-100123", title: "Updates",
    username: "mychannel", invite: null, enabled: true}]},
  settings: {settings: {reactions: {}, channels: {}}},
};

const requests = [];

function makeDom() {
  const prelude = `
  window.__DATA = ${JSON.stringify(DATA)};
  window.__REQUESTS = [];
  Object.defineProperty(document, "hidden", {get: () => false, configurable: true});
  window.fetch = async (url, opts) => {
    const path = String(url).replace("/admin/api/", "");
    window.__REQUESTS.push(path);
    let data = {ok: true};
    if (path.startsWith("me")) data = {ok: true, user_id: 9001, role: "owner", version: "3.5.0"};
    else if (path.startsWith("users")) data = Object.assign({ok: true}, window.__DATA.users);
    else if (path.startsWith("overview")) data = Object.assign({ok: true}, window.__DATA.overview);
    else if (path.startsWith("requests")) data = Object.assign({ok: true}, window.__DATA.requests);
    else if (path.startsWith("plans")) data = Object.assign({ok: true}, window.__DATA.plans);
    else if (path.startsWith("pricing")) data = Object.assign({ok: true}, window.__DATA.pricing);
    else if (path.startsWith("channels")) data = Object.assign({ok: true}, window.__DATA.channels);
    else if (path.startsWith("orders")) data = Object.assign({ok: true}, window.__DATA.orders);
    else if (path.startsWith("pool")) data = Object.assign({ok: true}, window.__DATA.pool);
    else if (path.startsWith("operators")) data = Object.assign({ok: true}, window.__DATA.operators);
    else if (path.startsWith("force_sub")) data = Object.assign({ok: true}, window.__DATA.force_sub);
    else if (path.startsWith("settings")) data = Object.assign({ok: true}, window.__DATA.settings);
    return {status: 200, ok: true, json: async () => data};
  };
  `;

  const html = `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>
    <header><h1>t</h1><div class="meta"><span id="who">…</span>
      <span id="live-state" class="badge live">● live</span><span id="updated"></span>
      <button id="auto">live</button><button id="refresh">r</button></div></header>
    <nav id="nav"></nav><main id="view"></main><div id="toast"></div>
    <script>${prelude}</script>
    <script>${js}</script></body></html>`;

  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => {
    if (!String(e.message).includes("Not implemented: navigation")) throw e;
  });
  virtualConsole.on("error", (e) => console.error("PAGE ERROR:", e));
  return new JSDOM(html, {runScripts: "dangerously", pretendToBeVisual: true, virtualConsole});
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
function check(ok, label) {
  console.log((ok ? "  ✔ " : "  ✘ ") + label);
  if (!ok) failed++;
}

(async () => {
  const dom = makeDom();
  const { window } = dom;
  await sleep(300);
  const doc = window.document;
  const view = doc.getElementById("view");

  console.log("\n1) boot renders the dashboard");
  check(/Live dashboard/.test(view.textContent), "dashboard rendered");
  check(/42/.test(view.textContent), "users KPI present");
  check(!/Loading/.test(view.textContent), "no loading placeholder left");

  console.log("\n2) a tick with unchanged data must not touch the DOM");
  const kpisBefore = doc.getElementById("kpis");
  const firstChild = kpisBefore.firstElementChild;
  await window.liveTick();
  const kpisAfter = doc.getElementById("kpis");
  check(kpisAfter === kpisBefore, "same region element (patched in place, not re-rendered)");
  check(kpisAfter.firstElementChild === firstChild, "unchanged data → zero DOM writes");

  console.log("\n3) live data still updates");
  window.__DATA.overview.users = 99;
  await window.liveTick();
  const kpis = doc.getElementById("kpis");
  check(/99/.test(kpis.textContent), "KPI shows the new value");
  check(doc.getElementById("kpis") === kpisBefore, "region element identity preserved");

  console.log("\n4) typing on a form tab survives the auto refresh (the reported bug)");
  const plansTab = doc.querySelector('#nav button[data-tab="plans"]');
  plansTab.click();
  await sleep(150);
  const input = doc.querySelector('#view input[name="title"]');
  check(!!input, "plans form rendered");
  input.focus();
  input.value = "MY EDIT IN PROGRESS";
  input.dispatchEvent(new window.Event("input", {bubbles: true}));
  await window.liveTick();
  await sleep(50);
  const after = doc.querySelector('#view input[name="title"]');
  check(!!after, "form is still there");
  check(after.value === "MY EDIT IN PROGRESS", "typed value survived the tick");
  check(/live paused/.test(doc.getElementById("live-state").textContent),
    "header says live is paused while editing");

  console.log("\n5) leaving the form and refreshing is safe");
  input.blur();
  doc.getElementById("refresh").click();
  await sleep(150);
  const refreshed = doc.querySelector('#view input[name="title"]');
  check(!!refreshed && refreshed.value === "Premium",
    "manual refresh reloads the form from the server (the tab is kept)");

  console.log("\n6) history: the timer runs the soft refresher");
  check(typeof window.liveTick === "function", "liveTick is exposed");
  check(typeof window.draw === "function", "draw still exists for manual refresh");

  // --- 7) mobile contract: tables carry labels, scroll boxes exist
  console.log("\n7) mobile table markup");
  await window.liveTick();
  const reqTab = doc.querySelector('#nav button[data-tab="requests"]');
  reqTab.click();
  await sleep(150);
  const cells = doc.querySelectorAll('#view td[data-label]');
  check(cells.length > 0, "table cells carry data-label for the stacked phone layout");
  check(!!doc.querySelector("#view .table-scroll"), "table sits in a scroll box");

  console.log("\n8) no markup is ever shown as text (all tabs)");
  const tabs = ["overview", "requests", "users", "plans", "pricing", "channels",
                "orders", "pool", "forcesub", "operators", "settings"];
  for (const tab of tabs) {
    const btn = doc.querySelector(`#nav button[data-tab="${tab}"]`);
    if (!btn) { check(false, `tab ${tab} exists`); continue; }
    btn.click();
    await sleep(120);
    const text = doc.getElementById("view").textContent;
    const leaked = /<(span|button|div|code|b)\b|<\/|&lt;/.test(text);
    check(!leaked, `${tab}: renders elements, not code`);
  }

  console.log("\n9) the rows really contain clickable controls");
  doc.querySelector('#nav button[data-tab="users"]').click();
  await sleep(120);
  const userBtns = doc.querySelectorAll('#view td[data-label="Actions"] button');
  check(userBtns.length >= 5, "users row has its 5 action buttons");
  check([...userBtns].some((b) => /wallet/i.test(b.textContent)), "wallets button is a real button");

  doc.querySelector('#nav button[data-tab="channels"]').click();
  await sleep(120);
  check(doc.querySelectorAll('#view td[data-label="Reactions"] .tag').length === 1,
    "channels: reaction state renders as a tag");

  doc.querySelector('#nav button[data-tab="pool"]').click();
  await sleep(120);
  check(doc.querySelectorAll('#view td[data-label="Status"] .tag').length === 1,
    "pool: status renders as a tag");

  doc.querySelector('#nav button[data-tab="operators"]').click();
  await sleep(120);
  const del = [...doc.querySelectorAll('#view button[data-act="op_del"]')][0];
  check(!!del && del.textContent.trim() === "remove", "operators: remove is a button");
  window.__REQUESTS.length = 0;
  window.confirm = () => true;
  del.click();
  await sleep(120);
  check(window.__REQUESTS.some((r) => r.startsWith("operators/")), "clicking remove calls the API");

  console.log("\n" + "=".repeat(50));
  if (failed) {
    console.log(`✘ ${failed} DOM check(s) failed`);
    process.exit(1);
  }
  console.log("✔ DOM test passed — soft refresh preserves edits, live data updates");
  dom.window.close();
})();
