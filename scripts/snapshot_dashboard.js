/**
 * Render the real dashboard in jsdom and dump the markup of a few tabs.
 *
 *   node scripts/snapshot_dashboard.js /tmp/dash_inner.html /tmp/dash_snap.json
 *
 * The output is used by scripts/make_dashboard_preview.py to show the phone
 * layout without a second browser: the markup is genuinely produced by the
 * dashboard code, only frozen.
 */
const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");

const [, , innerPath, outPath] = process.argv;
const html = fs.readFileSync(innerPath, "utf8");

const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => {
  if (!/navigation|Not implemented/i.test(e.message)) console.error("page error:", e.message);
});

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole,
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  await sleep(400);
  const { document } = dom.window;
  const tabs = ["overview", "requests", "users", "channels", "pool", "forcesub", "operators"];
  const out = {};
  for (const tab of tabs) {
    const btn = document.querySelector(`#nav button[data-tab="${tab}"]`);
    if (!btn) { console.error("missing tab", tab); continue; }
    btn.click();
    await sleep(220);
    out[tab] = {
      nav: document.getElementById("nav").innerHTML,
      view: document.getElementById("view").innerHTML,
    };
  }
  const leaked = Object.entries(out).filter(([, v]) =>
    /<(span|button|div|code)\b|&lt;/.test(v.view.replace(/<[^>]+>/g, "")));
  fs.writeFileSync(outPath, JSON.stringify(out), "utf8");
  if (leaked.length) {
    console.error("✘ escaped markup found in:", leaked.map(([k]) => k).join(", "));
    process.exit(1);
  }
  console.log(`✔ snapshotted ${Object.keys(out).length} tabs with no escaped markup`);
  dom.window.close();   // the dashboard keeps a 12 s timer alive — stop it
  process.exit(0);
})();
