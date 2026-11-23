"""Draw the About page diagrams as SVG, in the Uhambo cut-paper palette used by the scenes.

Usage (from the Sentinel repo root):  python scripts/build_about_diagrams.py
Writes scanner/static/scanner/diagrams/:
  flow.svg                     how a scan travels, and what is kept (moving dashes on the arrows)
  site-map-public.svg          the routes the public site serves (SENTINEL_PUBLIC_ONLY=1)
  site-map-full.svg            every route, with the admin, sign-in and Security runs parts
  architecture-public.svg      the code that runs the public site
  architecture-full.svg        the whole Django project
Transparent background, so they sit on light and dark pages. Shown through <img>; any animation is
CSS inside the file, stopped by prefers-reduced-motion or by loading name.svg#still.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "scanner" / "static" / "scanner" / "diagrams"

NAVY, NAVY_B, TERRA, OCHRE = "#16344D", "#2F5E8C", "#B1643E", "#C99754"
SAGE, SAGE_D, CARD, CREAM, MUTED = "#8EA38E", "#5C7A6B", "#FBF8F1", "#F4F0E4", "#546E83"
LINE = "#8A93A6"
SANS = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, 'Cascadia Mono', Consolas, monospace"
ACCESS = {"public": SAGE_D, "role": NAVY_B, "staff": OCHRE, "api": TERRA}
ACCESS_LABEL = {"public": "Public: anyone", "role": "Role: signed in with GitHub", "staff": "Staff only", "api": "API: called by a page"}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def doc(w: int, h: int, title: str, desc: str, body: str, css: str = "") -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="t d">
<g id="still"/>
<title id="t">{esc(title)}</title>
<desc id="d">{esc(desc)}</desc>
<style>
  text {{ font-family: {SANS}; }}
  .mono {{ font-family: {MONO}; }}
  .b {{ font-weight: 800; }}
{css}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} }}
  #still:target ~ *, #still:target ~ * * {{ animation: none !important; }}
</style>
<defs>
  <marker id="ah" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="{LINE}"/></marker>
  <marker id="ah-navy" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="{NAVY_B}"/></marker>
  <marker id="ah-sage" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="{SAGE_D}"/></marker>
</defs>
{body}
</svg>
"""


def lines(s: str, width: int) -> list[str]:
    return textwrap.wrap(s, width) or [""]


def card(x, y, w, h, fill=CARD, stroke=None, dash=False, r=16) -> str:
    shadow = f'<rect x="{x + 3}" y="{y + 5}" width="{w}" height="{h}" rx="{r}" fill="#000" opacity=".10"/>' if fill != "none" else ""
    st = f' stroke="{stroke}" stroke-width="2.5"' + (' stroke-dasharray="8 6"' if dash else "") if stroke else ""
    return f'{shadow}<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"{st}/>'


def text_block(x, y, rows, size=13, fill=MUTED, cls="", anchor="start", lh=None) -> str:
    lh = lh or round(size * 1.35)
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    c = f' class="{cls}"' if cls else ""
    return "".join(f'<text x="{x}" y="{y + i * lh}" font-size="{size}" fill="{fill}"{a}{c}>{esc(r)}</text>' for i, r in enumerate(rows))


def pill(cx, cy, label, fill=CARD, ink=NAVY, size=13) -> str:
    w = len(label) * size * 0.58 + 22
    return (f'<rect x="{cx - w / 2:.0f}" y="{cy - 13}" width="{w:.0f}" height="26" rx="13" fill="{fill}" stroke="{LINE}" stroke-width="1.5"/>'
            f'<text x="{cx}" y="{cy + 4.5}" font-size="{size}" fill="{ink}" text-anchor="middle" class="b">{esc(label)}</text>')


def cylinder(x, y, w, h, fill, title, sub, ink=CREAM) -> str:
    ry = 14
    body = (f'<rect x="{x + 3}" y="{y + 5}" width="{w}" height="{h}" rx="14" fill="#000" opacity=".10"/>'
            f'<path d="M{x} {y + ry} V{y + h - ry} A{w / 2} {ry} 0 0 0 {x + w} {y + h - ry} V{y + ry} Z" fill="{fill}"/>'
            f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" fill="{fill}"/>'
            f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2 - 6}" ry="{ry - 5}" fill="#fff" opacity=".18"/>')
    return body + text_block(x + w / 2, y + 52, [title], 17, ink, "b", "middle") + text_block(x + w / 2, y + 74, lines(sub, int(w / 7.2)), 13, ink, "", "middle")


def medallions(cx, cy, labels=("OSV", "KEV", "NVD", "CVE", "JS", "RSS"), r=19, gap=48) -> str:
    fills = [OCHRE, TERRA, SAGE, NAVY, OCHRE, SAGE_D]
    out = []
    for i, lab in enumerate(labels):
        x = cx + (i % 3 - 1) * gap
        y = cy + (i // 3) * (gap - 4)
        ink = CREAM if fills[i] in (NAVY, TERRA, SAGE_D) else NAVY
        out.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fills[i]}"/><text x="{x}" y="{y + 4}" font-size="10.5" class="b" fill="{ink}" text-anchor="middle">{lab}</text>')
    return "".join(out)


# ---------------------------------------------------------------------------------------- flow
def flow() -> str:
    W, H = 1200, 520
    top, ch = 70, 190
    nodes = [
        (20, 180, "You", "Upload or paste a dependency file"),
        (245, 180, "Cloudflare", "Encrypts the connection, absorbs attacks"),
        (470, 180, "nginx", "Rate limits scans; only public pages pass"),
        (695, 240, "Sentinel", "Reads the file in memory, asks the sources, ranks findings"),
        (980, 200, "6 public sources", "Asked all at once"),
    ]
    icons = [
        lambda cx, y: (f'<rect x="{cx - 34}" y="{y}" width="68" height="48" rx="7" fill="none" stroke="{NAVY}" stroke-width="4"/>'
                       f'<path d="M{cx - 34} {y + 13} H{cx + 34}" stroke="{NAVY}" stroke-width="4"/>'
                       f'<circle cx="{cx - 25}" cy="{y + 6.5}" r="2.5" fill="{TERRA}"/><circle cx="{cx - 17}" cy="{y + 6.5}" r="2.5" fill="{OCHRE}"/>'
                       f'<rect x="{cx - 9}" y="{y + 20}" width="20" height="22" rx="3" fill="{OCHRE}"/>'),
        lambda cx, y: (f'<g fill="{OCHRE}"><circle cx="{cx - 18}" cy="{y + 30}" r="16"/><circle cx="{cx + 2}" cy="{y + 20}" r="20"/>'
                       f'<circle cx="{cx + 22}" cy="{y + 32}" r="14"/><rect x="{cx - 34}" y="{y + 30}" width="70" height="16" rx="8"/></g>'),
        lambda cx, y: (f'<path d="M{cx - 30} {y + 50} V{y + 22} A30 30 0 0 1 {cx + 30} {y + 22} V{y + 50} Z" fill="{SAGE}"/>'
                       f'<path d="M{cx - 16} {y + 50} V{y + 28} A16 16 0 0 1 {cx + 16} {y + 28} V{y + 50} Z" fill="{NAVY}"/>'
                       + "".join(f'<rect x="{cx - 12 + k * 9}" y="{y + 26}" width="3" height="24" fill="{CREAM}"/>' for k in range(3))),
        lambda cx, y: (f'<path d="M{cx} {y - 2} L{cx + 28} {y + 9} V{y + 26} C{cx + 28} {y + 42} {cx + 15} {y + 50} {cx} {y + 55} C{cx - 15} {y + 50} {cx - 28} {y + 42} {cx - 28} {y + 26} V{y + 9} Z" fill="{NAVY}"/>'
                       f'<path d="M{cx - 11} {y + 26} l8 8 l16 -17" fill="none" stroke="{OCHRE}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'),
        lambda cx, y: medallions(cx, y + 6),
    ]
    out = []
    # the answer goes back to the browser, over the top
    out.append(f'<path class="flowback" d="M1080 {top - 6} V32 H110 V{top - 8}" fill="none" stroke="{SAGE_D}" stroke-width="3" stroke-dasharray="7 9" marker-end="url(#ah-sage)"/>')
    out.append(pill(595, 32, "your results come back to your browser only", CARD, SAGE_D))
    for i, ((x, w, title, sub), icon) in enumerate(zip(nodes, icons)):
        cx = x + w / 2
        hl = i == 3
        out.append(card(x, top, w, ch, CARD, NAVY_B if hl else None))
        out.append(icon(cx, top + 22))
        out.append(text_block(cx, top + 112, [title], 18, NAVY, "b", "middle"))
        out.append(text_block(cx, top + 134, lines(sub, int(w / 7.4)), 13, MUTED, "", "middle"))
        if i < len(nodes) - 1:
            nx = nodes[i + 1][0]
            out.append(f'<path class="flowline" d="M{x + w + 6} {top + ch / 2} H{nx - 8}" stroke="{NAVY_B}" stroke-width="3.5" stroke-dasharray="7 9" marker-end="url(#ah-navy)"/>')
    # what Sentinel keeps
    stores_y, bus_y = 360, 318
    stores = [(40, "file"), (330, "results"), (620, "log"), (910, "cache")]
    out.append(f'<path d="M815 {top + ch} V{bus_y} M165 {bus_y} H1035" stroke="{LINE}" stroke-width="2.5" fill="none"/>')
    out.append(pill(815, bus_y, "what is kept", CARD, NAVY, 12))
    for x, kind in stores:
        cx = x + 125
        out.append(f'<path d="M{cx} {bus_y} V{stores_y - 6}" stroke="{LINE}" stroke-width="2.5" marker-end="url(#ah)"/>')
        if kind == "file":
            out.append(card(x, stores_y, 250, 130, CARD, TERRA, dash=True))
            out.append(f'<path d="M{cx - 18} {stores_y + 18} H{cx + 8} L{cx + 20} {stores_y + 30} V{stores_y + 62} H{cx - 18} Z" fill="{CREAM}" stroke="{MUTED}" stroke-width="2"/>'
                       f'<path d="M{cx - 24} {stores_y + 16} L{cx + 26} {stores_y + 66} M{cx + 26} {stores_y + 16} L{cx - 24} {stores_y + 66}" stroke="{TERRA}" stroke-width="5" stroke-linecap="round"/>')
            out.append(text_block(cx, stores_y + 92, ["Your file"], 16, NAVY, "b", "middle"))
            out.append(text_block(cx, stores_y + 113, ["Never stored: read in memory"], 13, TERRA, "b", "middle"))
        elif kind == "results":
            out.append(card(x, stores_y, 250, 130))
            out.append(f'<rect x="{cx - 30}" y="{stores_y + 18}" width="60" height="44" rx="6" fill="none" stroke="{NAVY}" stroke-width="3.5"/>'
                       f'<path d="M{cx - 30} {stores_y + 30} H{cx + 30}" stroke="{NAVY}" stroke-width="3.5"/>'
                       f'<path d="M{cx - 14} {stores_y + 46} l8 8 l18 -18" fill="none" stroke="{SAGE_D}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
            out.append(text_block(cx, stores_y + 92, ["Your results"], 16, NAVY, "b", "middle"))
            out.append(text_block(cx, stores_y + 113, ["In your browser session only"], 13, MUTED, "", "middle"))
        elif kind == "log":
            out.append(cylinder(x, stores_y, 250, 130, NAVY, "Scan log", "Time, IP, package list · deleted after 30 days"))
        else:
            out.append(cylinder(x, stores_y, 250, 130, SAGE_D, "Public cache", "Advisory records and news feeds"))
    css = f"""
  .flowline {{ animation: flow 1.4s linear infinite; }}
  .flowback {{ animation: flow 1.4s linear infinite; }}
  @keyframes flow {{ to {{ stroke-dashoffset: -32; }} }}
"""
    return doc(W, H, "How a scan travels",
               "You send a dependency file through Cloudflare and nginx to Sentinel, which asks six public sources at once; the results come back to your browser only. "
               "Your file is never stored; results live in your browser session; a scan log is deleted after 30 days; a public cache keeps advisory records and news feeds.",
               "".join(out), css)


# ------------------------------------------------------------------------------------ site map
def site_map(full: bool) -> str:
    kids = [
        ("Scan", "/scan/", "public", [("Detected packages", "/parse/", "api")]),
        ("Results", "/results/", "public", [("Scan result", "/results/<id>/", "public")]),
        ("About", "/about/", "public", []),
        ("Health check", "/health/", "api", []),
    ]
    if full:
        kids += [
            ("Security runs", "/server/", "role", [("Add target", "/server/targets/add/", "role"), ("Start run", "/server/start/", "role"),
                                                  ("Run detail", "/server/runs/<id>/", "role"), ("Run status", "/server/runs/<id>/status/", "api"),
                                                  ("Audit trail", "/server/audit/", "role")]),
            ("Sign-in", "/auth/", "public", [("Sign in", "/auth/github/login/", "public"), ("Callback", "/auth/github/callback/", "api"),
                                             ("Sign out", "/auth/logout/", "role")]),
            ("Admin", "private address", "staff", []),
        ]
    kids.append(("Static files", "/static/", "public", []))
    cw, nw, nh = 218, 198, 70
    n = len(kids)
    W = n * cw + 40
    deepest = max(len(k[3]) for k in kids)
    gy0, gstep = 300, 92
    legend_y = gy0 + max(deepest, 1) * gstep + 30
    H = legend_y + 50
    out = []

    def node(x, y, title, code, access, w=nw):
        c = ACCESS[access]
        return (card(x, y, w, nh, CARD) + f'<rect x="{x + 8}" y="{y + 9}" width="6" height="{nh - 18}" rx="3" fill="{c}"/>'
                + text_block(x + 24, y + 27, [code], 12 if len(code) < 22 else 10.5, MUTED, "mono")
                + text_block(x + 24, y + 52, [title], 16, NAVY, "b"))

    root_x = W / 2 - 110
    out.append(card(root_x, 28, 220, 76, NAVY))
    out.append(text_block(W / 2, 58, ["/"], 13, OCHRE, "mono", "middle") + text_block(W / 2, 86, ["Home"], 20, CREAM, "b", "middle"))
    bus = 150
    xs = [20 + i * cw + (cw - nw) / 2 for i in range(n)]
    out.append(f'<path d="M{W / 2} 104 V{bus} M{xs[0] + nw / 2} {bus} H{xs[-1] + nw / 2}" stroke="{LINE}" stroke-width="2.5" fill="none"/>')
    for (title, code, access, grand), x in zip(kids, xs):
        out.append(f'<path d="M{x + nw / 2} {bus} V{190 - 6}" stroke="{LINE}" stroke-width="2.5" marker-end="url(#ah)"/>')
        out.append(node(x, 190, title, code, access))
        if grand:
            spine = x + 14
            last = gy0 + (len(grand) - 1) * gstep + nh / 2
            out.append(f'<path d="M{spine} {190 + nh} V{last}" stroke="{LINE}" stroke-width="2.5" fill="none"/>')
            for j, (gt, gc, ga) in enumerate(grand):
                gy = gy0 + j * gstep
                out.append(f'<path d="M{spine} {gy + nh / 2} H{x + 26 - 6}" stroke="{LINE}" stroke-width="2.5" marker-end="url(#ah)"/>')
                out.append(node(x + 26, gy, gt, gc, ga, nw - 14))
    used = ["public", "api"] + (["role", "staff"] if full else [])
    legend_w = sum(34 + len(ACCESS_LABEL[a]) * 7.2 for a in used) + 10
    out.append(card(18, legend_y - 24, legend_w, 40, CARD, r=12))   # text never sits on the page itself (dark mode)
    lx = 30
    for a in used:
        label = ACCESS_LABEL[a]
        out.append(f'<rect x="{lx}" y="{legend_y - 12}" width="16" height="16" rx="4" fill="{ACCESS[a]}"/>'
                   + text_block(lx + 24, legend_y + 1, [label], 13, MUTED))
        lx += 34 + len(label) * 7.2
    desc = "Home links to " + ", ".join(f"{t} ({c})" for t, c, _, _ in kids) + ". " + " ".join(
        f"Under {t}: " + ", ".join(f"{gt} ({gc})" for gt, gc, _ in g) + "." for t, _, _, g in kids if g)
    return doc(int(W), int(H), "Site map" + ("" if full else " of the public site"), desc, "".join(out))


# -------------------------------------------------------------------------------- architecture
APPS = {
    "scanner": ("/", "Dependency scanner, news and the public site", NAVY_B, [
        ("parsers", "Reads dependency files and lockfiles"),
        ("scan · sources · versions", "Asks six sources at once, merges duplicates"),
        ("triage", "Confirmed, possible or already fixed"),
        ("news · headlines", "Feeds, CVE matching, the newsroom"),
        ("http_client · store", "Outbound requests and the public cache"),
        ("history", "Results kept in your session only"),
        ("middleware", "Tracing, security headers, rate limits"),
        ("ScanLog", "Scan log, deleted after 30 days"),
    ]),
    "pipeline": ("/server/", "Security runs on registered Git targets", TERRA, [
        ("gitsafe", "Checks repository URLs before cloning"),
        ("runner · jobs · adapters", "Runs each tool, keeps its output"),
        ("bugchecks · osvlocal", "Built-in checks and offline OSV data"),
        ("access", "Permission checks on every page"),
        ("Target · Run · Step · Finding", "What ran, and what it found"),
    ]),
    "accounts": ("/auth/", "GitHub sign-in and roles", SAGE_D, [
        ("github", "The OAuth sign-in flow"),
        ("roles · identity", "Only listed logins get a role"),
        ("middleware", "Removed users are signed out at once"),
    ]),
    "audit": ("", "Tamper-evident record of what happened", OCHRE, [
        ("log", "Hash-chained events, evidence by SHA-256"),
        ("AuditEvent · EvidenceArtifact", "The record and its evidence"),
        ("verify_audit", "Re-checks the whole chain"),
    ]),
}


def architecture(full: bool) -> str:
    cols, chip_w, chip_h, gap = 3, 262, 84, 14
    block_x, block_w = 52, 3 * 262 + 2 * 14 + 40
    names = ["scanner", "pipeline", "accounts", "audit"] if full else ["scanner"]
    out, y, spans = [], 100, {}
    for name in names:
        url, desc, colour, mods = APPS[name]
        rows = -(-len(mods) // cols)
        h = 70 + rows * (chip_h + gap) + 6
        out.append(card(block_x, y, block_w, h))
        out.append(f'<rect x="{block_x}" y="{y}" width="{block_w}" height="10" rx="5" fill="{colour}"/>')
        out.append(text_block(block_x + 20, y + 40, [name], 20, NAVY, "b"))
        tx = block_x + 20 + len(name) * 12.5
        if url:
            out.append(text_block(tx, y + 40, [url], 13, MUTED, "mono"))
            tx += len(url) * 8 + 14
        out.append(text_block(tx, y + 40, [desc], 13, MUTED))
        for i, (mod, what) in enumerate(mods):
            cx = block_x + 20 + (i % cols) * (chip_w + gap)
            cy = y + 62 + (i // cols) * (chip_h + gap)
            out.append(f'<rect x="{cx}" y="{cy}" width="{chip_w}" height="{chip_h}" rx="12" fill="{colour}" opacity=".13"/>'
                       f'<rect x="{cx}" y="{cy}" width="5" height="{chip_h}" rx="2.5" fill="{colour}"/>')
            out.append(text_block(cx + 16, cy + 27, [mod], 13 if len(mod) < 26 else 11.5, NAVY, "mono b"))
            out.append(text_block(cx + 16, cy + 50, lines(what, 34), 12.5, MUTED, "", "start", 17))
        spans[name] = (y, y + h)
        y += h + 22
    cont_bottom = y
    cont = f'<rect x="28" y="40" width="{block_w + 48}" height="{cont_bottom - 40}" rx="26" fill="none" stroke="{LINE}" stroke-width="2.5" stroke-dasharray="10 8"/>'
    head = pill(28 + 150, 40, "config · one Django project", CARD, NAVY, 13)
    ex_x, ex_w = block_x + block_w + 150, 270
    right = block_x + block_w
    externals = []

    def ext(y0, h, title, sub, fill=CARD, ink=NAVY, extra=""):
        externals.append(card(ex_x, y0, ex_w, h, fill) + extra
                         + text_block(ex_x + ex_w / 2, y0 + h - 44, [title], 16, ink, "b", "middle")
                         + text_block(ex_x + ex_w / 2, y0 + h - 24, [sub], 12.5, MUTED if fill == CARD else ink, "", "middle"))
        return y0 + h / 2

    def arrow(y0, label, fromx=right):
        externals.append(f'<path d="M{fromx + 6} {y0} H{ex_x - 8}" stroke="{LINE}" stroke-width="2.5" marker-end="url(#ah)"/>')
        externals.append(pill((fromx + ex_x) / 2, y0 - 16, label, CARD, MUTED, 11.5))

    s0, s1 = spans["scanner"]
    cy = ext(s0 + 6, 160, "6 public sources", "OSV · KEV · NVD · CVE.org · RetireJS · news", extra=medallions(ex_x + ex_w / 2, s0 + 34, r=17, gap=44))
    arrow(cy, "lookups")
    externals.append(cylinder(ex_x, s0 + 196, ex_w, 120, SAGE_D, "Public cache", "SQLite file · advisories, feeds"))
    arrow(s0 + 256, "saves public data")
    if full:
        p0, p1 = spans["pipeline"]
        mx = ex_x + ex_w / 2
        lens = (f'<circle cx="{mx - 6}" cy="{p0 + 38}" r="18" fill="none" stroke="{NAVY}" stroke-width="5"/>'
                f'<path d="M{mx + 7} {p0 + 51} l14 14" stroke="{NAVY}" stroke-width="7" stroke-linecap="round"/>'
                f'<circle cx="{mx - 6}" cy="{p0 + 38}" r="6" fill="{TERRA}"/>')
        cy = ext(p0 + 8, 118, "Security tools", "Gitleaks · Bandit · pip-audit · npm audit", extra=lens)
        arrow(cy, "runs")
        a0, a1 = spans["accounts"]
        g_top = p1 - 36
        branch = (f'<g stroke="{SAGE_D}" stroke-width="5" fill="none" stroke-linecap="round">'
                  f'<path d="M{mx - 14} {g_top + 22} V{g_top + 70}"/><path d="M{mx + 16} {g_top + 30} C{mx + 16} {g_top + 50} {mx - 14} {g_top + 48} {mx - 14} {g_top + 62}"/></g>'
                  f'<circle cx="{mx - 14}" cy="{g_top + 20}" r="7" fill="{SAGE_D}"/><circle cx="{mx + 16}" cy="{g_top + 28}" r="7" fill="{OCHRE}"/>'
                  f'<circle cx="{mx - 14}" cy="{g_top + 72}" r="7" fill="{SAGE_D}"/>')
        gy = ext(g_top, (a0 + 70) - g_top, "GitHub", "Sign-in, and the repos to scan", extra=branch)
        arrow(p1 - 18, "clones")
        arrow(a0 + 52, "sign-in")
        d0, d1 = spans["audit"]
        externals.append(cylinder(ex_x, d0 + 8, ex_w, 130, OCHRE, "Evidence store", "Tool output, filed by SHA-256", NAVY))
        arrow(d0 + 70, "evidence")
    db_y = cont_bottom + 40
    db_sub = "Scan logs, runs, audit events, sessions" if full else "Scan logs (30 days) and sessions"
    db = (f'<path d="M{28 + (block_w + 48) / 2} {cont_bottom} V{db_y - 6}" stroke="{LINE}" stroke-width="2.5" marker-end="url(#ah)"/>'
          + cylinder(28 + (block_w + 48) / 2 - 260, db_y, 520, 120, NAVY, "Database", db_sub))
    W = ex_x + ex_w + 30
    H = db_y + 150
    desc = ("One Django project. " + " ".join(f"{n}: " + ", ".join(m for m, _ in APPS[n][3]) + "." for n in names)
            + " The scanner asks six public sources and saves public data in a cache file"
            + ("; the pipeline runs security tools and clones from GitHub, accounts signs in with GitHub, and audit files evidence by SHA-256" if full else "")
            + ". Everything writes to one database.")
    return doc(int(W), int(H), "Code architecture" + ("" if full else " of the public site"), desc, cont + head + "".join(out) + "".join(externals) + db)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "flow.svg": flow(),
        "site-map-public.svg": site_map(False),
        "site-map-full.svg": site_map(True),
        "architecture-public.svg": architecture(False),
        "architecture-full.svg": architecture(True),
    }
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"{name}: {len(text) / 1024:.1f} KB")
