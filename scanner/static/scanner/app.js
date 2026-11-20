// Sentinel page behaviour: scan-page preview, results filters and downloads.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

// Plain line icon for software without a logo (globe for PostGIS, database otherwise).
function plainIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("aria-hidden", "true");
  const d = name === "postgis"
    ? ["M3 12h18", "M12 3a14 14 0 0 1 0 18", "M12 3a14 14 0 0 0 0 18", "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0"]
    : ["M4 5c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z", "M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5", "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"];
  for (const p of d) { const path = document.createElementNS(svg.namespaceURI, "path"); path.setAttribute("d", p); svg.append(path); }
  return el("span", { class: "logo" }, svg);
}

// ---------------------------------------------------------------- theme switch (light by default)
const toggle = $("#theme-toggle");
if (toggle) {
  const sync = () => toggle.setAttribute("aria-pressed", String(document.documentElement.dataset.theme === "dark"));
  sync();
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("sentinel-theme", next); } catch (e) {}
    sync();
  });
}

// ---------------------------------------------------------------- scan page
const form = $("#scan-form");
if (form) {
  const input = $("#files"), drop = $("#drop");
  let timer;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(preview, 350); };

  function renderFileList() {
    const list = $("#file-list");
    list.replaceChildren(...[...input.files].map((f) => el("li", {}, `${f.name} · ${(f.size / 1024).toFixed(1)} KB`)));
  }

  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => {
    e.preventDefault();
    drop.classList.add("is-over");
  }));
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("is-over");
    if (e.dataTransfer?.files?.length) {
      const dt = new DataTransfer();
      [...e.dataTransfer.files].forEach((f) => dt.items.add(f));
      input.files = dt.files;
      renderFileList();
      preview();
    }
  });
  input.addEventListener("change", () => {
    renderFileList();
    preview();
  });
  $("#pasted").addEventListener("input", schedule);
  $("#other").addEventListener("input", schedule);

  async function preview() {
    const data = new FormData(form);
    let res;
    try {
      res = await fetch(form.dataset.preview, { method: "POST", body: data,
        headers: { "X-CSRFToken": data.get("csrfmiddlewaretoken") } });
    } catch { return; }
    const json = await res.json();
    const box = $("#detected"), list = $("#detected-list"), bad = $("#detected-bad");
    if (json.error) { bad.textContent = json.error; bad.hidden = false; box.hidden = false; return; }
    list.replaceChildren(...json.packages.map((p) => el("li", {},
      p.logo ? el("span", { class: "logo" }, el("img", { src: p.logo, alt: "", width: "18", height: "18" }))
             : plainIcon(p.name),
      el("span", {}, p.name),
      el("code", {}, p.version || p.spec || "any version"))));
    $("#detected-count").textContent = `${json.packages.length} package${json.packages.length === 1 ? "" : "s"} from ${json.files.map((f) => f.name).join(", ") || "—"}`;
    bad.hidden = !json.unreadable.length;
    bad.textContent = json.unreadable.length ? `Can't read: ${json.unreadable.map((u) => u.line).join(" · ")}` : "";
    box.hidden = !json.packages.length && !json.unreadable.length;
    $("#ready").textContent = json.packages.length ? `${json.packages.length} packages ready to scan` : "Add a file, paste a list or fill in Other software to begin.";
  }

  form.addEventListener("submit", () => {
    $("#progress").hidden = false;
    $("#scan-btn").disabled = true;
    $("#scan-btn").textContent = "Scanning…";
  });
}

// ---------------------------------------------------------------- results page
const dataTag = $("#scan-data");
if (dataTag) {
  const show = { vuln: true, clean: true, unchecked: true };
  const search = $("#pkg-search");

  function applyFilters() {
    const q = (search?.value || "").trim().toLowerCase();
    let visible = 0;
    $$("[data-state][data-name]").forEach((card) => {
      const ok = show[card.dataset.state] && card.dataset.name.includes(q);
      card.hidden = !ok;
      if (ok) visible += 1;
    });
    $$("h3[data-state]").forEach((h) => { h.hidden = !show[h.dataset.state]; });
    $("#no-match").hidden = visible > 0;
  }
  $$("md-filter-chip[data-filter]").forEach((chip) => chip.addEventListener("click", () =>
    requestAnimationFrame(() => { show[chip.dataset.filter] = chip.selected; applyFilters(); })));
  search?.addEventListener("input", applyFilters);

  $("#dl-json")?.addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(JSON.parse(dataTag.textContent), null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: `sentinel-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("#dl-pdf")?.addEventListener("click", () => window.print());
  window.addEventListener("beforeprint", () => $$("details.pkg").forEach((d) => { d.dataset.wasOpen = d.open; d.open = true; }));
  window.addEventListener("afterprint", () => $$("details.pkg").forEach((d) => { d.open = d.dataset.wasOpen === "true"; }));
}


// ---------------------------------------------------------------- navigation and session state
// State lives in sessionStorage: it belongs to this tab, survives Back/Forward and reloads,
// and is gone when the tab closes. Pages themselves are never cached (the server sends no-store).
const store = {
  get(k) { try { return JSON.parse(sessionStorage.getItem("sentinel:" + k)); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem("sentinel:" + k, JSON.stringify(v)); } catch { /* storage full or blocked: carry on without it */ } },
  del(k) { try { sessionStorage.removeItem("sentinel:" + k); } catch { /* ignore */ } },
};
const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// Back links: go back in history when we came from this site (keeps scroll and state), else follow the link.
$$("a.back[data-back]").forEach((a) => a.addEventListener("click", (e) => {
  try {
    if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) { e.preventDefault(); history.back(); }
  } catch { /* fall through to the normal link */ }
}));

// Scan page: remember what was entered (text and file contents) for this session.
const scanForm = $("#scan-form");
if (scanForm) {
  const KEY = "form", pasted = $("#pasted"), other = $("#other"), files = $("#files");
  const note = $("#restored");

  async function snapshot() {
    const kept = []; let total = 0;
    for (const f of files.files) {            // file contents are kept only while the tab is open, up to ~1 MB
      if (total + f.size > 900000) break;
      total += f.size; kept.push({ name: f.name, text: await f.text() });
    }
    store.set(KEY, { pasted: pasted.value, other: other.value, files: kept });
  }
  const save = debounce(snapshot, 250);
  [pasted, other].forEach((t) => t.addEventListener("input", save));
  files.addEventListener("change", snapshot);

  function fire(node, type) { node.dispatchEvent(new Event(type, { bubbles: true })); }
  function restore() {
    const s = store.get(KEY); if (!s) return;
    let did = false;
    if (!pasted.value && s.pasted) { pasted.value = s.pasted; fire(pasted, "input"); did = true; }
    if (!other.value && s.other) { other.value = s.other; fire(other, "input"); did = true; }
    if (!files.files.length && s.files?.length) {
      const dt = new DataTransfer();
      s.files.forEach((f) => dt.items.add(new File([f.text], f.name, { type: "text/plain" })));
      files.files = dt.files; fire(files, "change"); did = true;
    }
    if (did && note) note.hidden = false;
  }
  function clearAll() {
    store.del(KEY);
    pasted.value = ""; other.value = ""; files.value = "";
    $("#file-list").replaceChildren(); $("#detected").hidden = true; $("#detected-list").replaceChildren();
    $("#ready").textContent = "Add a file, paste a list or fill in Other software to begin.";
    if (note) note.hidden = true;
  }
  $("#clear-form")?.addEventListener("click", clearAll);

  if (new URLSearchParams(location.search).get("new") === "1") {   // "New scan" starts clean
    store.del(KEY); history.replaceState(null, "", location.pathname);
  } else {
    restore();
  }
  // Coming back (Back button, bfcache) must not show a stuck "Scanning…" button.
  window.addEventListener("pageshow", () => {
    $("#progress").hidden = true; const b = $("#scan-btn"); b.disabled = false; b.textContent = "Scan dependencies";
    restore();
  });
}

// Results page: remember filters and which packages were open, so Back returns you to the same view.
const pageState = $("#page-state");
if (pageState && $("#scan-data")) {
  const key = pageState.dataset.key, search = $("#pkg-search");
  const chips = $$("md-filter-chip[data-filter]");
  const snap = () => store.set(key, {
    q: search?.value || "",
    show: Object.fromEntries(chips.map((c) => [c.dataset.filter, c.selected])),
    open: $$("details.pkg[open]").map((d) => d.dataset.name),
  });
  const saved = store.get(key);
  if (saved) {
    if (search) { search.value = saved.q || ""; search.dispatchEvent(new Event("input")); }
    $$("details.pkg").forEach((d) => { d.open = (saved.open || []).includes(d.dataset.name); });
    customElements.whenDefined("md-filter-chip").then(() => chips.forEach((c) => {
      const want = saved.show?.[c.dataset.filter];
      if (want === false && c.selected) { c.selected = false; c.dispatchEvent(new Event("click")); c.selected = false; }
    }));
  }
  search?.addEventListener("input", debounce(snap, 200));
  chips.forEach((c) => c.addEventListener("click", () => setTimeout(snap, 50)));
  $$("details.pkg").forEach((d) => d.addEventListener("toggle", snap));
}

// Run detail: severity filter, remembered for the session.
const sev = $("#sev-filter");
if (sev && pageState) {
  const key = pageState.dataset.key + ":sev";
  const apply = () => {
    let n = 0;
    $$("[data-severity]").forEach((a) => { const ok = !sev.value || a.dataset.severity === sev.value; a.hidden = !ok; n += ok; });
    $("#no-match").hidden = n > 0;
    store.set(key, sev.value);
  };
  const saved = store.get(key); if (saved) sev.value = saved;
  sev.addEventListener("change", apply); apply();
}

// Runs list: remember which runs were open and the chosen target.
if (pageState?.dataset.key === "runs") {
  const open = store.get("runs:open"), target = $("#target-select");
  if (open) $$("details[data-run]").forEach((d) => { d.open = open.includes(d.dataset.run); });
  $$("details[data-run]").forEach((d) => d.addEventListener("toggle", () =>
    store.set("runs:open", $$("details[data-run][open]").map((x) => x.dataset.run))));
  const chosen = store.get("runs:target"); if (chosen && target) target.value = chosen;
  target?.addEventListener("change", () => store.set("runs:target", target.value));
}

// A run in progress: check its status every few seconds and reload once when it finishes.
const watched = $("[data-status-url]");
if (watched) {
  let tries = 0;
  const poll = async () => {
    if (++tries > 200) return;
    try {
      const r = await fetch(watched.dataset.statusUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (r.ok && (await r.json()).done) { location.reload(); return; }
    } catch { /* offline: try again */ }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
}
