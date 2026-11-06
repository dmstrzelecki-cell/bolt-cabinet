/* Parent settings page. Same rule as app.js: server text goes in via
 * textContent only. Every POST carries the CSRF token from /api/me. */
"use strict";

const $ = (id) => document.getElementById(id);
let ME = null;
let S = null;              // last /api/parent/state
let tilesDraft = [];
const ICONS = ["book", "pencil", "flask", "rocket", "paw", "column", "calculator", "letters", "news",
  "ballet", "lightbulb", "library", "camera", "globe", "star", "music"];
const ACTIONS = { filter: "Filter results", search: "Run a search", photos: "Open Photos",
  random: "Random page", open_host: "Open a site" };

function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (k in n && typeof v !== "string") n[k] = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}
const btn = (text, onclick, cls = "btn btn-ghost") =>
  el("button", { type: "button", class: cls, text, on: { click: onclick } });

async function api(path, body, raw = false) {
  const init = { credentials: "same-origin", redirect: "manual", cache: "no-store",
    headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["X-CSRF-Token"] = ME ? ME.csrf : "";
    if (raw) init.body = body;
    else { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  }
  let r;
  try { r = await fetch(path, init); } catch (_) { throw new Error("You're offline."); }
  if (r.type === "opaqueredirect" || r.status === 0) { location.reload(); throw new Error("Signing in…"); }
  let data = {};
  try { data = await r.json(); } catch (_) { data = {}; }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2500);
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    await load();
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function when(ts) {
  if (!ts) return "never";
  const d = new Date(ts.replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
  return isNaN(d) ? ts : d.toLocaleString();
}

/* ------------------------------------------------------------- startup */
document.addEventListener("DOMContentLoaded", async () => {
  for (const b of $("ptabs").querySelectorAll("button"))
    b.addEventListener("click", () => showSection(b.dataset.sec, true));
  wireForms();
  try {
    ME = await api("/api/me");
  } catch (e) { toast(e.message, true); return; }
  $("who").textContent = ME.email ? `Signed in as ${ME.email}` : "";
  await load();
  showSection((location.hash || "#requests").slice(1), false);
});

function showSection(sec, push) {
  if (!$("sec-" + sec)) sec = "requests";
  for (const s of document.querySelectorAll(".sec")) s.hidden = s.id !== "sec-" + sec;
  for (const b of $("ptabs").querySelectorAll("button"))
    b.setAttribute("aria-pressed", String(b.dataset.sec === sec));
  if (push) history.replaceState(null, "", "#" + sec);
  if (sec === "audit") loadAudit();
  if (sec === "settings") loadHistory();
}

async function load() {
  try { S = await api("/api/parent/state"); } catch (e) { toast(e.message, true); return; }
  renderRequests(); renderSites(); renderFamilyLink(); renderLibrary(); renderAlbums();
  renderFilters(); tilesDraft = JSON.parse(JSON.stringify(S.tiles.tiles)); renderTiles();
  $("store-history").checked = !!S.settings.store_history;
}

/* ------------------------------------------------------------- forms */
function formObj(form) { return Object.fromEntries(new FormData(form).entries()); }

function wireForms() {
  $("site-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = formObj(ev.target);
    act(() => api("/api/parent/sites", { action: "add", host: f.host, category: f.category,
      name: f.name, page_cap: Number(f.page_cap), boost: Number(f.boost),
      crawl: ev.target.crawl.checked }), "Site added. It will be crawled shortly.")
      .then((r) => { if (r) ev.target.reset(); });
  });
  $("crawl-all").addEventListener("click", () =>
    act(() => api("/api/parent/crawl", { hosts: null }), "Crawl requested."));
  $("fl-copy-btn").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("fl-copy").value); toast("Copied."); }
    catch (_) { $("fl-copy").select(); toast("Select-all done — copy with your keyboard or long-press."); }
  });
  $("lib-url").addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(() => api("/api/parent/library", { action: "add_url", ...formObj(ev.target) }), "Link added.")
      .then((r) => { if (r) ev.target.reset(); });
  });
  $("lib-note").addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(() => api("/api/parent/library", { action: "add_note", ...formObj(ev.target) }), "Note added.")
      .then((r) => { if (r) ev.target.reset(); });
  });
  $("lib-pdf").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const file = fd.get("file");
    if (file && file.size > 25 * 1024 * 1024) { toast("PDFs can be up to 25 MB.", true); return; }
    act(() => api("/api/parent/library/pdf", fd, true), "PDF uploaded.")
      .then((r) => { if (r) ev.target.reset(); });
  });
  $("album-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(() => api("/api/parent/albums", { action: "add", ...formObj(ev.target) }),
      "Album allowed. Press Sync to load its photos.").then((r) => { if (r) ev.target.reset(); });
  });
  $("sync").addEventListener("click", async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "Syncing…";
    const r = await act(() => api("/api/parent/albums/sync", {}));
    if (r) toast(`Synced ${r.albums} album(s): ${r.photos} photos (${r.added} new, ${r.removed} removed)` +
      (r.errors.length ? ` — ${r.errors.length} album error(s)` : ""), !!r.errors.length);
    ev.target.disabled = false; ev.target.textContent = "Sync albums now";
  });
  $("filters-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const blocked = ev.target.blocked.value.split("\n").map((s) => s.trim()).filter(Boolean);
    const synonyms = {};
    for (const line of ev.target.synonyms.value.split("\n")) {
      const [k, v] = line.split(":");
      if (!k || !v) continue;
      synonyms[k.trim()] = v.split(",").map((s) => s.trim()).filter(Boolean);
    }
    act(() => api("/api/parent/filters", { blocked, synonyms }), "Filters saved.");
  });
  $("tile-add").addEventListener("click", () => {
    tilesDraft.push({ id: `tile-${tilesDraft.length + 1}`, label: "New tile", icon: "star",
      action: "filter", categories: [], hosts: [], query: "", hidden: false, include_library: false });
    renderTiles();
  });
  $("tiles-save").addEventListener("click", () =>
    act(() => api("/api/parent/tiles", { tiles: tilesDraft }), "Tiles saved."));
  $("store-history").addEventListener("change", (ev) => {
    const on = ev.target.checked;
    if (!on && !confirm("Turn off history and delete what is saved?")) { ev.target.checked = true; return; }
    act(() => api("/api/parent/settings", { store_history: on }),
      on ? "Search history is on." : "Search history is off and cleared.").then(loadHistory);
  });
  $("audit-kind").addEventListener("change", loadAudit);
}

/* ------------------------------------------------------------- requests */
function renderRequests() {
  const box = $("requests");
  const fresh = S.requests.filter((r) => r.status === "new").length;
  $("req-badge").hidden = !fresh;
  $("req-badge").textContent = String(fresh);
  if (!S.requests.length) { box.replaceChildren(el("p", { class: "muted", text: "No requests yet." })); return; }
  const ul = el("ul", { class: "items" });
  for (const r of S.requests) {
    const send = (action) => act(() => api("/api/parent/requests", { action, id: r.id }));
    ul.append(el("li", { class: "item" + (r.status === "new" ? "" : " off") },
      el("div", { class: "grow" },
        el("div", { class: "title", text: r.text }),
        el("div", { class: "meta", text: `${r.kind === "site" ? "Website" : "Search"} · ${when(r.ts)} · ${r.status}` +
          (r.handled_by ? ` by ${r.handled_by}` : "") })),
      el("div", { class: "btns" },
        r.kind === "site" ? btn("Add as site", () => {
          showSection("sites", true);
          const host = r.text.replace(/^https?:\/\//, "").split("/")[0].toLowerCase();
          $("site-form").host.value = host;
          $("site-form").category.focus();
        }) : null,
        r.status === "new" ? btn("Approve", () => send("approve")) : null,
        r.status === "new" ? btn("Dismiss", () => send("dismiss")) : null,
        btn("Delete", () => send("delete")))));
  }
  box.replaceChildren(ul);
}

/* ------------------------------------------------------------- sites */
function statusPill(st) {
  const s = (st && st.status) || "not crawled";
  const cls = s === "ok" ? "pill ok" : ["blocked", "challenged", "needs_review", "error"].includes(s) ? "pill bad" : "pill";
  return el("span", { class: cls, text: s.replace("_", " ") });
}

function renderSites() {
  const dl = $("categories");
  dl.replaceChildren(...S.categories.map((c) => el("option", { value: c })));
  $("crawl-info").textContent = S.crawl_pending ? "A crawl is queued." :
    S.crawl.updated ? `Crawler last reported ${when(S.crawl.updated)}.` : "The crawler has not run yet.";
  const ul = el("ul", { class: "items" });
  for (const s of S.sites) {
    const st = s.crawl_status || {};
    const send = (body, msg) => act(() => api("/api/parent/sites", { host: s.host, ...body }), msg);
    const meta = s.crawl
      ? `${st.pages ?? 0} pages · ${st.skipped ?? 0} skipped · ${st.errors ?? 0} errors · last success ${when(st.last_success)}`
      : "Not crawled (links and Family Link only)";
    const edit = el("div", { class: "tile-edit", hidden: true },
      el("label", {}, "Category", el("input", { value: s.category, list: "categories", name: "category" })),
      el("label", {}, "Name", el("input", { value: s.name || "", name: "name" })),
      el("label", {}, "Page cap", el("input", { type: "number", min: "1", max: "100000", value: String(s.page_cap), name: "page_cap" })),
      el("label", {}, "Boost", el("input", { type: "number", min: "0", max: "5", value: String(s.boost), name: "boost" })),
      el("label", {}, "Crawl day", (() => {
        const sel = el("select", { name: "schedule_day" }, el("option", { value: "", text: "automatic" }),
          ...["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => el("option", { value: String(i), text: d })));
        sel.value = s.schedule_day ?? "";
        return sel;
      })()),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "crawl", checked: !!s.crawl }), "Crawl"),
      btn("Save", () => {
        const v = (n) => edit.querySelector(`[name=${n}]`);
        send({ action: "update", category: v("category").value, name: v("name").value,
          page_cap: Number(v("page_cap").value), boost: Number(v("boost").value),
          schedule_day: v("schedule_day").value === "" ? null : Number(v("schedule_day").value),
          crawl: v("crawl").checked }, "Saved.");
      }, "btn btn-primary"));
    ul.append(el("li", { class: "item" + (s.enabled ? "" : " off") },
      el("div", { class: "grow" },
        el("div", { class: "title" }, document.createTextNode(s.host + " "), statusPill(s.enabled ? st : { status: "disabled" })),
        el("div", { class: "meta", text: `${s.name ? s.name + " · " : ""}${s.category} · cap ${s.page_cap} · boost ${s.boost}` }),
        el("div", { class: "meta", text: meta }),
        st.last_error ? el("div", { class: "meta", text: `Last problem: ${st.last_error}` }) : null),
      el("div", { class: "btns" },
        btn("Edit", () => { edit.hidden = !edit.hidden; }),
        s.crawl && s.enabled ? btn("Crawl now", () => act(() => api("/api/parent/crawl", { hosts: [s.host] }), "Crawl requested.")) : null,
        btn(s.enabled ? "Disable" : "Enable", () => send({ action: s.enabled ? "disable" : "enable" },
          s.enabled ? "Disabled — results hidden now." : "Enabled.")),
        btn("Remove", () => {
          if (confirm(`Remove ${s.host}? Its results disappear immediately.`))
            send({ action: "remove" }, "Removed.");
        })),
      edit));
  }
  $("sites").replaceChildren(ul);
}

/* ------------------------------------------------------------- family link */
function renderFamilyLink() {
  const missing = S.familylink.filter((h) => !h.confirmed);
  $("fl-badge").hidden = !missing.length;
  $("fl-badge").textContent = String(missing.length);
  $("fl-copy").value = S.familylink.map((h) => h.host).join("\n");
  const ul = $("familylink");
  ul.replaceChildren();
  for (const h of S.familylink) {
    const cb = el("input", { type: "checkbox", checked: h.confirmed, id: `fl-${h.host}` });
    cb.addEventListener("change", () =>
      act(() => api("/api/parent/familylink", { host: h.host, confirmed: cb.checked })));
    ul.append(el("li", {}, cb, el("label", { for: `fl-${h.host}` }, el("code", { text: h.host })),
      h.confirmed ? null : el("span", { class: "pill bad", text: "still needs approval in Family Link" })));
  }
}

/* ------------------------------------------------------------- library */
function renderLibrary() {
  if (!S.library.length) { $("library").replaceChildren(el("p", { class: "muted", text: "Nothing in the library yet." })); return; }
  const ul = el("ul", { class: "items" });
  for (const it of S.library) {
    const kind = { url: "Link", note: "Note", pdf: "PDF" }[it.kind];
    ul.append(el("li", { class: "item" },
      el("div", { class: "grow" },
        el("div", { class: "title", text: it.title }),
        el("div", { class: "meta", text: `${kind}${it.host ? " · " + it.host : ""}${it.file_size ? " · " + Math.round(it.file_size / 1024) + " KB" : ""} · added ${when(it.created)} by ${it.created_by}` }),
        it.note ? el("div", { class: "meta", text: it.note.slice(0, 200) }) : null),
      el("div", { class: "btns" }, btn("Remove", () => {
        if (confirm(`Remove “${it.title}”?`))
          act(() => api("/api/parent/library", { action: "remove", id: it.id }), "Removed.");
      }))));
  }
  $("library").replaceChildren(ul);
}

/* ------------------------------------------------------------- albums */
function renderAlbums() {
  $("immich-note").textContent = S.immich_configured ? "" :
    "Immich is not connected yet: IMMICH_URL / IMMICH_API_KEY are still placeholders in the server's env file.";
  $("sync").disabled = !S.immich_configured;
  if (!S.albums.length) { $("albums").replaceChildren(el("p", { class: "muted", text: "No albums allowed yet." })); return; }
  const ul = el("ul", { class: "items" });
  for (const a of S.albums) {
    ul.append(el("li", { class: "item" },
      el("div", { class: "grow" },
        el("div", { class: "title", text: a.name || a.immich_name || "Album" }),
        el("div", { class: "meta", text: `${a.photo_count} photos · last sync ${when(a.last_sync)}` }),
        el("div", { class: "meta" }, el("code", { text: a.id })),
        a.last_error ? el("div", { class: "meta", text: `Problem: ${a.last_error}` }) : null),
      el("div", { class: "btns" }, btn("Remove", () => {
        if (confirm("Remove this album? Its photos disappear immediately."))
          act(() => api("/api/parent/albums", { action: "remove", id: a.id }), "Album removed.");
      }))));
  }
  $("albums").replaceChildren(ul);
}

/* ------------------------------------------------------------- filters */
function renderFilters() {
  const f = $("filters-form");
  f.blocked.value = S.filters.blocked.join("\n");
  f.synonyms.value = Object.entries(S.filters.synonyms).map(([k, v]) => `${k}: ${v.join(", ")}`).join("\n");
}

/* ------------------------------------------------------------- tiles */
function renderTiles() {
  const ul = el("ul", { class: "items" });
  tilesDraft.forEach((t, i) => {
    const input = (name, value, attrs = {}) => {
      const n = el("input", { value: value ?? "", ...attrs });
      n.addEventListener("input", () => {
        if (name === "categories" || name === "hosts")
          t[name] = n.value.split(",").map((s) => s.trim()).filter(Boolean);
        else t[name] = n.value;
      });
      return n;
    };
    const iconSel = el("select", {}, ...ICONS.map((x) => el("option", { value: x, text: x })));
    iconSel.value = t.icon;
    iconSel.addEventListener("change", () => { t.icon = iconSel.value; });
    const actSel = el("select", {}, ...Object.entries(ACTIONS).map(([k, v]) => el("option", { value: k, text: v })));
    actSel.value = t.action;
    actSel.addEventListener("change", () => { t.action = actSel.value; });
    const lib = el("input", { type: "checkbox", checked: !!t.include_library });
    lib.addEventListener("change", () => { t.include_library = lib.checked; });
    const move = (d) => { const j = i + d; if (j < 0 || j >= tilesDraft.length) return;
      [tilesDraft[i], tilesDraft[j]] = [tilesDraft[j], tilesDraft[i]]; renderTiles(); };
    ul.append(el("li", { class: "item" + (t.hidden ? " off" : "") },
      el("div", { class: "grow" },
        el("div", { class: "title", text: `${i + 1}. ${t.label}${t.hidden ? " (hidden)" : ""}` }),
        tileCountLine(t),
        el("div", { class: "tile-edit" },
          el("label", {}, "Id", input("id", t.id)),
          el("label", {}, "Label", input("label", t.label, { maxlength: "24" })),
          el("label", {}, "Icon", iconSel),
          el("label", {}, "Action", actSel),
          el("label", {}, "Categories (comma)", input("categories", (t.categories || []).join(", "))),
          el("label", {}, "Whole sites (comma)", input("hosts", (t.hosts || []).join(", "))),
          el("label", { class: "wide" }, "Sections — one page link per line; every page under it counts (e.g. https://kids.nationalgeographic.com/animals/)",
            (() => {
              const ta = el("textarea", { rows: String(Math.max(2, (t.sections || []).length + 1)), spellcheck: "false" });
              ta.value = (t.sections || []).join("\n");
              ta.addEventListener("input", () => {
                t.sections = ta.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
              });
              return ta;
            })()),
          el("label", {}, "Search words", input("query", t.query)),
          el("label", { class: "check" }, lib, "Include library"))),
      el("div", { class: "btns" },
        btn("↑", () => move(-1)), btn("↓", () => move(1)),
        btn(t.hidden ? "Show" : "Hide", () => { t.hidden = !t.hidden; renderTiles(); }),
        btn("Remove", () => { tilesDraft.splice(i, 1); renderTiles(); }))));
  });
  $("tiles").replaceChildren(ul);
}

function tileCountLine(t) {
  const n = S.tile_counts ? S.tile_counts[t.id] : undefined;
  if (n === undefined || n === null) return null;
  if (n === 0) return el("div", { class: "meta" }, el("span", { class: "pill bad",
    text: "finds 0 pages — check the sections / categories, or wait for the first crawl" }));
  return el("div", { class: "meta", text: `Finds about ${n.toLocaleString()} approved pages (as last saved)` });
}

/* ------------------------------------------------------------- history + audit */
async function loadHistory() {
  const box = $("history");
  if (!$("store-history").checked) { box.replaceChildren(); return; }
  let r;
  try { r = await api("/api/parent/history"); } catch (e) { toast(e.message, true); return; }
  const ul = el("ul", { class: "items" });
  for (const h of r.rows) ul.append(el("li", { class: "item" },
    el("div", { class: "grow" }, el("div", { class: "title", text: h.q }),
      el("div", { class: "meta", text: `${when(h.ts)} · ${h.type}` }))));
  box.replaceChildren(
    el("div", { class: "row-actions" }, btn("Clear history", () => {
      if (confirm("Delete all saved searches?"))
        act(() => api("/api/parent/history/clear", {}), "History cleared.").then(loadHistory);
    })),
    r.rows.length ? ul : el("p", { class: "muted", text: "No searches saved yet." }));
}

async function loadAudit() {
  let r;
  const kind = $("audit-kind").value;
  try { r = await api("/api/parent/audit?limit=300" + (kind ? "&kind=" + encodeURIComponent(kind) : "")); }
  catch (e) { toast(e.message, true); return; }
  const tb = el("tbody");
  for (const row of r.rows) {
    const detail = Object.entries(row.detail || {}).map(([k, v]) =>
      `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
    tb.append(el("tr", {}, el("td", { text: when(row.ts) }), el("td", { text: row.kind }),
      el("td", { text: row.actor }), el("td", { text: row.action }), el("td", { text: detail })));
  }
  $("audit").replaceChildren(el("div", { class: "audit-wrap" }, el("table", { class: "audit" },
    el("thead", {}, el("tr", {}, ...["When", "Kind", "Who", "What", "Details"].map((h) => el("th", { text: h })))), tb)));
}
