// Dev-only browser check (Chromium via Playwright), run by tests/e2e/run.sh:
//   node tests/e2e/check.mjs <port> <tokens.json> <shots-dir>
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const [port, tokensFile, shots] = process.argv.slice(2);
const { child: childJwt, parent: parentJwt } = JSON.parse(readFileSync(tokensFile, "utf8"));
const base = `http://127.0.0.1:${port}`;
const results = [];
const ok = (name, cond, extra = "") => results.push({ name, pass: !!cond, extra });

const browser = await chromium.launch();
async function page(jwt, width = 360, scale = 1) {
  const ctx = await browser.newContext({ viewport: { width, height: 760 }, deviceScaleFactor: scale,
    extraHTTPHeaders: { "Cf-Access-Jwt-Assertion": jwt } });
  const p = await ctx.newPage();
  p.cspErrors = []; p.dialogs = 0;
  p.on("console", (m) => { if (/Content Security Policy|Refused to/.test(m.text())) p.cspErrors.push(m.text()); });
  p.on("dialog", async (d) => { p.dialogs++; await d.dismiss(); });
  return p;
}
const noHScroll = (p) => p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
const smallTargets = (p, sel) => p.evaluate((sel) => [...document.querySelectorAll(sel)]
  .filter((e) => e.offsetParent !== null)
  .map((e) => [e.textContent.trim().slice(0, 20) || e.getAttribute("aria-label"), e.getBoundingClientRect()])
  .filter(([, r]) => r.width < 44 || r.height < 44).map(([n]) => n), sel);

const p = await page(childJwt, 360, 2);
const t0 = Date.now();
await p.goto(base + "/", { waitUntil: "load" });
await p.waitForSelector(".tile");
const loadMs = Date.now() - t0;
ok("home page loads in under 1 s (local)", loadMs < 1000, `${loadMs} ms`);
ok("12 tiles in a 2-column grid at 360px", (await p.$$(".tile")).length === 12 &&
  await p.evaluate(() => getComputedStyle(document.getElementById("tiles")).gridTemplateColumns.split(" ").length === 2));
ok("home: no horizontal scroll at 360px", await noHScroll(p));
const small = await smallTargets(p, "button:not([hidden]), a.brand, input#q");
ok("home: tap targets >= 44px", small.length === 0, JSON.stringify(small));
await p.screenshot({ path: `${shots}/home-360.png`, fullPage: true });

await p.fill("#q", "moon");
await p.keyboard.press("Enter");
await p.waitForSelector(".result");
ok("results render", (await p.$$(".result")).length >= 5);
ok("results: no horizontal scroll at 360px", await noHScroll(p));
const xss = await p.evaluate(() => [window.__pwned, [...document.querySelectorAll(".result a.title")]
  .map((a) => a.textContent).find((t) => t.includes("<script>"))]);
ok("script tags in crawled text appear as plain text", xss[0] === undefined && !!xss[1], JSON.stringify(xss));
ok("no injected script/img elements", await p.evaluate(() => !document.querySelector(".result script, .result img")));
ok("result links are https:// with rel=noopener noreferrer", await p.evaluate(() =>
  [...document.querySelectorAll(".result a.title")].every((a) => a.href.startsWith("https://") &&
    a.rel.includes("noopener") && a.rel.includes("noreferrer"))));
ok("numbered pagination", (await p.$$("#pager button")).length >= 2);
await p.screenshot({ path: `${shots}/results-360.png` });

await p.goto(base + "/?q=sandcastle");
await p.waitForSelector("#photo-strip .ph img");
ok("All tab shows a compact photo strip when photos match", (await p.$$("#photo-strip .ph")).length === 3);

await p.goto(base + "/?type=photos");
await p.waitForSelector("#photo-grid .ph img");
await p.waitForFunction(() => [...document.querySelectorAll("#photo-grid img")].every((i) => i.complete && i.naturalWidth > 0));
ok("Photos tab: thumbnail grid loads", (await p.$$("#photo-grid .ph")).length === 3);
await p.click("#photo-grid .ph");
await p.waitForSelector("#viewer:not([hidden])");
await p.waitForFunction(() => document.getElementById("viewer-img").naturalWidth > 0);
ok("full-screen viewer opens", await p.isVisible("#viewer"));
await p.screenshot({ path: `${shots}/viewer-360.png` });
await p.keyboard.press("Escape");
ok("viewer closes with Escape", await p.isHidden("#viewer"));

await p.goto(base + "/?q=naked");
await p.waitForSelector(".status.warn");
ok("blocked search shows the calm message", (await p.textContent(".status.warn")).includes("ask a grown-up"));
await p.screenshot({ path: `${shots}/blocked-360.png` });
await p.goto(base + "/?q=zzqxv");
await p.waitForSelector("#status h2");
ok("empty results: no outside results, offers Ask a grown-up",
  (await p.textContent("#status")).includes("approved sites") && (await p.$$(".result")).length === 0);
await p.click("#status button");
await p.waitForFunction(() => document.querySelector("#status button").textContent.startsWith("Sent"));
ok("Ask a grown-up saves the request on tap", true);
await p.screenshot({ path: `${shots}/empty-360.png` });

await p.goto(base + "/");
await p.click(".tile >> text=Space");
await p.waitForSelector("#filter-chip:not([hidden])");
await p.waitForSelector(".result");
ok("Space tile filters to space sites", await p.evaluate(() =>
  [...document.querySelectorAll(".result .site-url")].every((s) => /nasa\.gov/.test(s.textContent))));
ok("child pages: no CSP violations, no dialogs", p.cspErrors.length === 0 && p.dialogs === 0, p.cspErrors.join(" | "));

const d = await page(childJwt, 1280);
await d.goto(base + "/");
await d.waitForSelector(".tile");
ok("4-column tiles on desktop", await d.evaluate(() =>
  getComputedStyle(document.getElementById("tiles")).gridTemplateColumns.split(" ").length === 4));
await d.screenshot({ path: `${shots}/home-desktop.png` });

const pp = await page(parentJwt, 360, 2);
await pp.goto(base + "/parent#sites");
await pp.waitForSelector("#sites .item");
ok("parent page lists the 27 approved sites", (await pp.$$("#sites .item")).length === 27);
ok("parent: no horizontal scroll at 360px", await noHScroll(pp));
await pp.screenshot({ path: `${shots}/parent-360.png` });
await pp.click('#ptabs button[data-sec="requests"]');
await pp.waitForSelector("#requests .item");
ok("parent sees the child's Ask-a-grown-up request", (await pp.textContent("#requests")).includes("zzqxv"));
ok("parent page: no CSP violations", pp.cspErrors.length === 0, pp.cspErrors.join(" | "));

await pp.click('#ptabs button[data-sec="tiles"]');
await pp.waitForSelector("#tiles .item");
const animals = (await pp.$$("#tiles .item"))[3];
const animalsText = await animals.textContent();
ok("tile editor shows sections and a page count", animalsText.includes("Finds about") || animalsText.includes("finds 0 pages"));
ok("tile editor: no horizontal scroll at 360px", await noHScroll(pp));
await animals.scrollIntoViewIfNeeded();
await animals.screenshot({ path: `${shots}/parent-tile-360.png` });

const c2 = await page(childJwt);
const resp = await c2.goto(base + "/parent");
ok("child gets 403 on /parent", resp.status() === 403);

await browser.close();
for (const r of results) console.log(`${r.pass ? "PASS" : "FAIL"}  ${r.name}${r.extra ? "  (" + r.extra + ")" : ""}`);
process.exit(results.every((r) => r.pass) ? 0 : 1);
