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

  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, () => drop.classList.add("is-over")));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("is-over")));
  input.addEventListener("change", () => {
    const list = $("#file-list");
    list.replaceChildren(...[...input.files].map((f) => el("li", {}, `${f.name} · ${(f.size / 1024).toFixed(1)} KB`)));
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
    const a = el("a", { href: URL.createObjectURL(blob), download: `sentinel-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.append(a); a.click(); a.remove();
  });
  $("#dl-pdf")?.addEventListener("click", () => window.print());
  window.addEventListener("beforeprint", () => $$("details.pkg").forEach((d) => { d.dataset.wasOpen = d.open; d.open = true; }));
  window.addEventListener("afterprint", () => $$("details.pkg").forEach((d) => { d.open = d.dataset.wasOpen === "true"; }));
}
