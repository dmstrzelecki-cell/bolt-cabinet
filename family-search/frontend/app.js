/* Strz Search front end. No frameworks, no third-party code.
 *
 * Safety rule for this file: text from the server (titles, snippets, album
 * names, notes) is ONLY ever inserted with textContent / createTextNode.
 * Highlights arrive as private-use marker characters and are turned into
 * <mark> elements here — never by parsing HTML. Links are only followed if
 * they are https:// (or our own /library/ and /api/library/ paths).
 */
"use strict";

const MARK_PRE = "";
const MARK_POST = "";
const $ = (id) => document.getElementById(id);

let ME = null;
let CFG = { tiles: [] };
const state = { q: "", type: "all", page: 1, tile: "" };
let currentPhotos = [];
let viewerIndex = 0;
let searchSeq = 0;

/* ------------------------------------------------------------- helpers */
function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k in n && typeof v !== "string") n[k] = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

function marked(text) {
  const frag = document.createDocumentFragment();
  const s = String(text || "");
  let i = 0;
  while (i < s.length) {
    const a = s.indexOf(MARK_PRE, i);
    if (a < 0) { frag.append(document.createTextNode(s.slice(i))); break; }
    if (a > i) frag.append(document.createTextNode(s.slice(i, a)));
    const b = s.indexOf(MARK_POST, a + 1);
    const end = b < 0 ? s.length : b;
    frag.append(el("mark", { text: s.slice(a + 1, end) }));
    i = end + 1;
  }
  return frag;
}

function plain(text) {
  return String(text || "").split(MARK_PRE).join("").split(MARK_POST).join("");
}

function safeHref(url) {
  if (typeof url !== "string") return null;
  if (/^\/(library|api\/library)\/[A-Za-z0-9_-]+(\/file)?$/.test(url)) return url;
  try {
    const u = new URL(url);
    if (u.protocol === "https:" && !u.username && !u.password) return u.href;
  } catch (_) { /* fall through */ }
  return null;
}

class ApiError extends Error {
  constructor(kind, info = {}) { super(kind); this.kind = kind; this.info = info; }
}

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", redirect: "manual", cache: "no-store", ...opts };
  init.headers = { Accept: "application/json", ...(opts.headers || {}) };
  if (init.method && init.method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-CSRF-Token"] = ME ? ME.csrf : "";
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch (_) {
    throw new ApiError(navigator.onLine === false ? "offline" : "network");
  }
  // Cloudflare Access answers an expired session with a redirect to its login page.
  if (r.type === "opaqueredirect" || r.status === 0) throw new ApiError("signin");
  let body = {};
  try { body = await r.json(); } catch (_) { body = {}; }
  if (r.status === 401 || (r.status === 403 && body.signin)) throw new ApiError("signin", body);
  if (!r.ok) throw new ApiError(r.status === 429 ? "slow" : r.status === 503 ? "unavailable" : "error", body);
  return body;
}

/* ------------------------------------------------------------- startup */
document.addEventListener("DOMContentLoaded", init);

async function init() {
  wire();
  try {
    [ME, CFG] = await Promise.all([api("/api/me"), api("/api/config")]);
  } catch (e) {
    showResultsShell();
    showProblem(e);
    return;
  }
  applyBrand();
  renderTiles();
  readUrl();
  if (state.q || state.tile || state.type !== "all") runSearch({ push: false });
  else showHome();
}

/* Tell the server which page she opened (only kept if a parent turned on
 * activity monitoring). keepalive lets it finish after the page changes, and
 * it never delays or blocks the tap. */
function logOpen(url, q, tile) {
  if (!ME || !url) return;
  try {
    fetch("/api/activity", { method: "POST", keepalive: true, credentials: "same-origin",
      redirect: "manual", headers: { "Content-Type": "application/json", "X-CSRF-Token": ME.csrf },
      body: JSON.stringify({ url, q: q || "", tile: tile || "" }) }).catch(() => {});
  } catch (_) { /* never block navigation */ }
}

function applyBrand() {
  const [a, b] = CFG.wordmark && CFG.wordmark.length ? CFG.wordmark : ["family", "search"];
  $("wm-a").textContent = a || "";
  $("wm-b").textContent = b || "";
  $("tagline").textContent = CFG.tagline || "";
  $("monitor-note").hidden = !CFG.monitoring_notice;
  document.title = CFG.site_name || "Search";
}

function wire() {
  $("search-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    hideSuggest();
    const q = $("q").value.trim();
    if (!q && !state.tile) { $("q").focus(); return; }
    state.q = q; state.page = 1;
    if (!q) state.type = state.type || "all";
    runSearch({ push: true });
  });
  $("q").addEventListener("input", () => { $("clear").hidden = !$("q").value; scheduleSuggest(); });
  $("q").addEventListener("keydown", suggestKeys);
  $("q").addEventListener("blur", () => setTimeout(hideSuggest, 150));
  $("clear").addEventListener("click", () => {
    $("q").value = ""; $("clear").hidden = true; hideSuggest(); $("q").focus();
  });
  $("curious").addEventListener("click", () => curious(""));
  $("brand").addEventListener("click", (ev) => {
    ev.preventDefault();
    state.q = ""; state.tile = ""; state.type = "all"; state.page = 1;
    $("q").value = ""; $("clear").hidden = true;
    history.pushState(null, "", "/");
    showHome();
  });
  for (const b of $("tabs").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      state.type = b.dataset.type; state.page = 1;
      runSearch({ push: true });
    });
  }
  $("filter-clear").addEventListener("click", () => {
    state.tile = ""; state.page = 1;
    if (!state.q) { history.pushState(null, "", "/"); showHome(); return; }
    runSearch({ push: true });
  });
  window.addEventListener("popstate", () => {
    readUrl();
    if (state.q || state.tile || state.type !== "all") runSearch({ push: false });
    else showHome();
  });
  window.addEventListener("offline", () => {
    if (document.body.classList.contains("is-results")) showProblem(new ApiError("offline"));
  });
  window.addEventListener("online", () => {
    if (document.body.classList.contains("is-results")) runSearch({ push: false });
  });

  // Ask a grown-up
  $("ask-open").addEventListener("click", () => openAsk(state.q));
  $("ask-cancel").addEventListener("click", () => $("ask").close());
  $("ask-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = $("ask-text").value.trim();
    if (!text) return;
    $("ask-send").disabled = true;
    try {
      await api("/api/requests", { method: "POST",
        body: JSON.stringify({ kind: $("ask-site").checked ? "site" : "query", text }) });
      $("ask-done").hidden = false;
      setTimeout(() => $("ask").close(), 1400);
    } catch (e) {
      $("ask-done").hidden = false;
      $("ask-done").textContent = e.kind === "slow" ? (e.info.error || "Please wait a bit.")
        : "That didn't send. Try again in a moment.";
    } finally {
      $("ask-send").disabled = false;
    }
  });

  // Viewer
  $("viewer-close").addEventListener("click", closeViewer);
  $("viewer-prev").addEventListener("click", () => stepViewer(-1));
  $("viewer-next").addEventListener("click", () => stepViewer(1));
  document.addEventListener("keydown", (ev) => {
    if ($("viewer").hidden) return;
    if (ev.key === "Escape") closeViewer();
    else if (ev.key === "ArrowLeft") stepViewer(-1);
    else if (ev.key === "ArrowRight") stepViewer(1);
  });
  let touchX = null;
  $("viewer").addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  $("viewer").addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 50) stepViewer(dx < 0 ? 1 : -1);
    touchX = null;
  });
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").slice(0, 200);
  state.type = ["all", "web", "photos", "library"].includes(p.get("type")) ? p.get("type") : "all";
  state.page = Math.max(1, Math.min(50, parseInt(p.get("page") || "1", 10) || 1));
  state.tile = /^[a-z0-9-]{1,32}$/.test(p.get("tile") || "") ? p.get("tile") : "";
  $("q").value = state.q;
  $("clear").hidden = !state.q;
  const m = location.pathname.match(/^\/library\/([A-Za-z0-9_-]+)$/);
  if (m) openLibraryItem(m[1]);
}

function writeUrl(push) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.type !== "all") p.set("type", state.type);
  if (state.page > 1) p.set("page", String(state.page));
  if (state.tile) p.set("tile", state.tile);
  const url = "/?" + p.toString();
  if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
}

/* ------------------------------------------------------------- home */
function showHome() {
  document.body.classList.add("is-home");
  document.body.classList.remove("is-results");
  document.title = CFG.site_name || "Search";
}

function showResultsShell() {
  document.body.classList.remove("is-home");
  document.body.classList.add("is-results");
}

function renderTiles() {
  const box = $("tiles");
  box.replaceChildren();
  for (const t of CFG.tiles || []) {
    const b = el("button", { type: "button", class: "tile", on: { click: () => tileTap(t) } },
      el("span", { class: `ti i-${t.icon}`, "aria-hidden": "true" }),
      el("span", { text: t.label }));
    box.append(b);
  }
}

function tileTap(t) {
  if (t.action === "photos") {
    state.tile = ""; state.type = "photos"; state.page = 1; state.q = $("q").value.trim();
    runSearch({ push: true });
  } else if (t.action === "random") {
    curious(t.id);
  } else if (t.action === "open_url") {
    const href = safeHref(t.url);
    if (href) { logOpen(href, "", t.id); window.location.assign(href); }
  } else {
    state.tile = t.id; state.type = "all"; state.page = 1;
    state.q = t.action === "filter" ? $("q").value.trim() : "";
    runSearch({ push: true });
  }
}

async function curious(tileId) {
  const btn = $("curious");
  btn.disabled = true;
  try {
    const r = await api("/api/curious" + (tileId ? `?tile=${encodeURIComponent(tileId)}` : ""));
    const href = safeHref(r.url);
    if (href) { logOpen(href, "", tileId); window.location.assign(href); }
  } catch (e) {
    showResultsShell();
    showProblem(e, "Nothing to explore yet — check back after tonight's update!");
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------- search */
async function runSearch({ push }) {
  showResultsShell();
  writeUrl(push);
  $("q").value = state.q;
  $("clear").hidden = !state.q;
  for (const b of $("tabs").querySelectorAll("button"))
    b.setAttribute("aria-pressed", String(b.dataset.type === state.type));
  const tile = (CFG.tiles || []).find((t) => t.id === state.tile);
  $("filter-chip").hidden = !tile;
  if (tile) $("filter-label").textContent = `Showing: ${tile.label}`;
  document.title = `${state.q || (tile ? tile.label : "Photos")} – ${CFG.site_name || "Search"}`;

  clearResults();
  const status = $("status");
  status.hidden = false;
  status.className = "status loading";
  status.replaceChildren(el("div", { class: "bar" }), el("div", { class: "bar" }), el("div", { class: "bar" }));
  status.setAttribute("aria-label", "Loading");

  const seq = ++searchSeq;
  const p = new URLSearchParams({ q: state.q, type: state.type, page: String(state.page) });
  if (state.tile) p.set("tile", state.tile);
  let data;
  try {
    data = await api("/api/search?" + p.toString());
  } catch (e) {
    if (seq === searchSeq) showProblem(e);
    return;
  }
  if (seq !== searchSeq) return;
  status.hidden = true;
  status.removeAttribute("aria-label");
  render(data);
  window.scrollTo(0, 0);
}

function clearResults() {
  $("ask-banner").replaceChildren(); $("ask-banner").hidden = true;
  $("list").replaceChildren();
  $("photo-grid").replaceChildren(); $("photo-grid").hidden = true;
  $("photo-strip").replaceChildren(); $("photo-strip").hidden = true;
  $("pager").replaceChildren(); $("pager").hidden = true;
}

function render(data) {
  if (data.blocked) {
    statusBox("warn", "Let's ask a grown-up about that one.",
      "That search isn't something we look up here. A grown-up can help you find what you need.",
      askButton(data.q, "query"));
    return;
  }
  if (data.suggest_ask) {
    // Gentle, not blocking: results still show below.
    const box = $("ask-banner");
    box.replaceChildren(
      el("div", { class: "ask-text" },
        el("b", { text: "This might be a good one to talk about." }),
        el("span", { text: " A grown-up can help you find the right answer." })),
      askButton(data.q, "query"));
    box.hidden = false;
  }
  const isPhotos = state.type === "photos";
  const web = data.results || [];
  const lib = data.library || [];
  const photos = isPhotos ? web : data.photos || [];

  if (isPhotos) {
    renderPhotoGrid(photos);
  } else {
    if (photos.length) renderPhotoStrip(photos);
    const list = $("list");
    if (state.type === "all" && lib.length) {
      list.append(el("li", { class: "section-label", text: "From our family library" }));
      for (const r of lib) list.append(resultItem(r));
      if (web.length) list.append(el("li", { class: "section-label", text: "From approved websites" }));
    }
    for (const r of web) list.append(resultItem(r));
  }
  const nothing = !web.length && !lib.length && !(photos && photos.length);
  if (nothing) {
    const what = state.q ? `“${state.q}”` : "this";
    statusBox("", `We didn't find ${what} on our approved sites.`,
      "Try different words, check the spelling, or ask a grown-up to help find it.",
      state.q ? askButton(state.q, "query") : null);
  }
  renderPager(data.page || 1, data.total_pages || 0);
}

function resultItem(r) {
  const href = safeHref(r.url);
  if (!href) return document.createTextNode("");
  const isLib = r.kind === "library";
  const title = el("a", { class: "title", href, target: isLib ? undefined : "_blank",
    rel: "noopener noreferrer", referrerpolicy: "no-referrer" }, marked(r.title || r.display_url));
  if (isLib && r.item_kind === "note") {
    title.addEventListener("click", (ev) => { ev.preventDefault(); openLibraryItem(r.id); });
  }
  if (isLib && r.item_kind === "pdf") {
    title.href = `/api/library/${encodeURIComponent(r.id)}/file`;
    title.target = "_blank";
  }
  title.addEventListener("click", () => logOpen(title.getAttribute("href"), state.q, state.tile));
  const siteName = isLib ? "Family library" : r.site;
  const initial = isLib ? "★" : r.initial || (siteName || "?").slice(0, 1).toUpperCase();
  return el("li", { class: "result" + (isLib ? " lib" : "") },
    el("div", { class: "site" },
      el("span", { class: "initial", "aria-hidden": "true", text: initial }),
      el("span", { class: "site-text" },
        el("span", { text: siteName }),
        el("span", { class: "site-url", text: r.display_url || "" }))),
    title,
    r.snippet ? el("p", { class: "snippet" }, marked(r.snippet)) : null);
}

function thumbButton(p, i, list) {
  const img = el("img", { src: `/api/photos/${encodeURIComponent(p.id)}/thumb`, alt: photoLabel(p),
    loading: "lazy", decoding: "async", width: 200, height: 200 });
  img.addEventListener("error", () => { img.alt = "Photo unavailable"; img.removeAttribute("src"); }, { once: true });
  return el("button", { type: "button", class: "ph", "aria-label": `Open photo: ${photoLabel(p)}`,
    on: { click: () => openViewer(list, i) } }, img);
}

function photoLabel(p) {
  return [p.description, p.album, p.date].filter(Boolean).join(" · ") || "Family photo";
}

function renderPhotoStrip(photos) {
  const strip = $("photo-strip");
  strip.hidden = false;
  photos.forEach((p, i) => strip.append(thumbButton(p, i, photos)));
  strip.append(el("button", { type: "button", class: "strip-more", text: "More photos",
    on: { click: () => { state.type = "photos"; state.page = 1; runSearch({ push: true }); } } }));
}

function renderPhotoGrid(photos) {
  const grid = $("photo-grid");
  grid.hidden = !photos.length;
  photos.forEach((p, i) => grid.append(thumbButton(p, i, photos)));
}

function renderPager(page, total) {
  const nav = $("pager");
  if (total <= 1) return;
  nav.hidden = false;
  const add = (label, n, current = false, aria = undefined) => nav.append(
    el("button", { type: "button", text: label, "aria-label": aria || `Page ${n}`,
      "aria-current": current ? "page" : undefined,
      on: { click: () => { state.page = n; runSearch({ push: true }); } } }));
  if (page > 1) add("‹", page - 1, false, "Previous page");
  const start = Math.max(1, Math.min(page - 3, total - 6));
  const end = Math.min(total, start + 6);
  for (let n = start; n <= end; n++) add(String(n), n, n === page);
  if (page < total) add("›", page + 1, false, "Next page");
}

function statusBox(kind, title, text, action) {
  const s = $("status");
  s.hidden = false;
  s.className = "status" + (kind ? " " + kind : "");
  s.replaceChildren(el("h2", { text: title }), el("p", { text }), action || "");
}

function askButton(text, kind) {
  const b = el("button", { type: "button", class: "btn btn-primary", text: "Ask a grown-up" });
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await api("/api/requests", { method: "POST", body: JSON.stringify({ kind, text }) });
      b.textContent = "Sent! A grown-up will look soon.";
    } catch (e) {
      b.disabled = false;
      b.textContent = e.kind === "slow" ? "Please wait a bit, then try again" : "Didn't send — try again";
    }
  });
  return b;
}

function showProblem(e, emptyMsg) {
  clearResults();
  const kind = e && e.kind;
  const reload = el("button", { type: "button", class: "btn btn-primary", text: "Try again",
    on: { click: () => location.reload() } });
  if (kind === "signin") {
    statusBox("", "Time to sign in again", "Your sign-in timed out. Tap below to sign back in.",
      el("button", { type: "button", class: "btn btn-primary", text: "Sign in",
        on: { click: () => location.reload() } }));
  } else if (kind === "offline" || kind === "network") {
    statusBox("", "You're offline", "Check the Wi-Fi or data connection, then try again.", reload);
  } else if (kind === "slow") {
    statusBox("", "Whoa, speedy!", (e.info && e.info.error) || "Take a breath and try again in a minute.", null);
  } else if (kind === "unavailable") {
    statusBox("", "Search is taking a nap", "Try again in a minute.", reload);
  } else if (e && e.info && e.info.error && emptyMsg && e.info.error.startsWith("Nothing")) {
    statusBox("", "Nothing to explore yet", emptyMsg, null);
  } else {
    statusBox("", "Something went wrong", (e && e.info && e.info.error) || "Please try again.", reload);
  }
}

/* ------------------------------------------------------------- suggestions */
let suggestTimer = null;
let suggestSeq = 0;
function scheduleSuggest() {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(loadSuggest, 160);
}
async function loadSuggest() {
  const q = $("q").value.trim();
  if (q.length < 2 || !ME) { hideSuggest(); return; }
  const seq = ++suggestSeq;
  let r;
  try { r = await api("/api/suggest?q=" + encodeURIComponent(q)); } catch (_) { return; }
  if (seq !== suggestSeq || document.activeElement !== $("q")) return;
  const items = r.suggestions || [];
  const ul = $("suggest");
  ul.replaceChildren();
  items.forEach((s, i) => {
    ul.append(el("li", { role: "option", id: `sg-${i}`, "aria-selected": "false",
      on: { mousedown: (ev) => { ev.preventDefault(); pickSuggest(s); } } },
      document.createTextNode(s)));
  });
  ul.hidden = !items.length;
  $("q").setAttribute("aria-expanded", String(!!items.length));
}
function hideSuggest() {
  $("suggest").hidden = true;
  $("q").setAttribute("aria-expanded", "false");
  $("q").removeAttribute("aria-activedescendant");
}
function pickSuggest(s) {
  $("q").value = s;
  hideSuggest();
  state.q = s; state.page = 1;
  runSearch({ push: true });
}
function suggestKeys(ev) {
  const ul = $("suggest");
  if (ul.hidden) return;
  const opts = [...ul.querySelectorAll("li")];
  let i = opts.findIndex((o) => o.getAttribute("aria-selected") === "true");
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    i = ev.key === "ArrowDown" ? (i + 1) % opts.length : (i <= 0 ? opts.length - 1 : i - 1);
    opts.forEach((o, j) => o.setAttribute("aria-selected", String(j === i)));
    $("q").setAttribute("aria-activedescendant", opts[i].id);
  } else if (ev.key === "Enter" && i >= 0) {
    ev.preventDefault();
    pickSuggest(opts[i].textContent);
  } else if (ev.key === "Escape") {
    hideSuggest();
  }
}

/* ------------------------------------------------------------- viewer */
let lastFocus = null;
function openViewer(list, i) {
  currentPhotos = list;
  viewerIndex = i;
  lastFocus = document.activeElement;
  $("viewer").hidden = false;
  document.body.style.overflow = "hidden";
  showViewerPhoto();
  $("viewer-close").focus();
}
function showViewerPhoto() {
  const p = currentPhotos[viewerIndex];
  if (!p) return;
  const img = $("viewer-img");
  img.src = `/api/photos/${encodeURIComponent(p.id)}/view`;
  img.alt = photoLabel(p);
  $("viewer-cap").textContent = photoLabel(p);
  $("viewer-prev").hidden = viewerIndex <= 0;
  $("viewer-next").hidden = viewerIndex >= currentPhotos.length - 1;
}
function stepViewer(d) {
  const n = viewerIndex + d;
  if (n < 0 || n >= currentPhotos.length) return;
  viewerIndex = n;
  showViewerPhoto();
}
function closeViewer() {
  $("viewer").hidden = true;
  $("viewer-img").removeAttribute("src");
  document.body.style.overflow = "";
  if (lastFocus) lastFocus.focus();
}

/* ------------------------------------------------------------- library + ask */
async function openLibraryItem(id) {
  let item;
  try { item = await api(`/api/library/${encodeURIComponent(id)}`); } catch (_) { return; }
  $("note-title").textContent = item.title || "";
  $("note-body").textContent = item.note || "";
  const link = $("note-link");
  link.replaceChildren();
  const href = safeHref(item.file || item.url);
  link.hidden = !href;
  if (href) link.append(el("a", { href, target: "_blank", rel: "noopener noreferrer",
    text: item.kind === "pdf" ? "Open the PDF" : "Open the page" }));
  $("note").showModal();
}

function openAsk(prefill) {
  $("ask-text").value = prefill || "";
  $("ask-site").checked = false;
  $("ask-done").hidden = true;
  $("ask-done").textContent = "Sent! A grown-up will take a look.";
  $("ask").showModal();
  $("ask-text").focus();
}
