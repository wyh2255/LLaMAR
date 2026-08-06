#!/usr/bin/env python3
"""Generate fig_architecture.svg/.png — LLaMAR-SAR module architecture figure.

Layout mirrors the LLaMAR paper architecture illustration:
  top zone    : LM modules with symbolic input stacks (chips + brace)
  bottom zone : colored legend panels defining each symbol with SAR examples

Mapping to this project:
  Planner LM   -> Coordinator LLM (RouterAgent)
  Actor LM     -> Worker Agent LM (ReAct tool loop, xN)
  Memory       -> Semantic Map Store
  Environment  -> SAR Grid World (GridEngine + SARBarrier)
  Verifier LM  -> TaskWatchdog (rule-based)

Run:  uv run --with cairosvg python fig_architecture_gen.py
"""

from html import escape

W, H = 1600, 1000
OUT_SVG = "fig_architecture.svg"
OUT_PNG = "fig_architecture.png"

INK = "#1F2933"
NEUT = "#4D5E6E"
RED = "#C0392B"

# symbol colors (chips == legend panels, as in the reference figure)
C_I = ("#D6EAF8", "#2E86C1")    # mission instruction
C_S = ("#D5F5E3", "#1E8449")    # semantic map
C_T = ("#E8DAEF", "#7D3C98")    # team status
C_K = ("#FDEBD0", "#D68910")    # subtask states
C_E = ("#FADBD8", "#C0392B")    # alerts
C_SUB = ("#F5B7B1", "#922B21")  # subtask dispatch
C_O = ("#D1F2EB", "#148F77")    # observation / report
C_Q = ("#FCF3CF", "#B7950B")    # agent state
C_A = ("#FCF3CF", "#B7950B")    # action
C_D = ("#E9F7EF", "#52BE80")    # primitive feedback

CELL_GRID_ST = "#C5CBCE"

parts: list[str] = []


def add(s: str) -> None:
    parts.append(s)


def rect(x, y, w, h, fill="none", stroke=NEUT, sw=1.2, rx=0, dash=None):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {d}/>')


def circle(cx, cy, r, fill="none", stroke=NEUT, sw=1.2):
    add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def line(x1, y1, x2, y2, stroke=NEUT, sw=1.2, dash=None):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}" {d}/>')


def text(x, y, s, size=11, anchor="start", weight="normal", fill=INK, style=None):
    st = f'font-style="{style}"' if style else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'font-weight="{weight}" fill="{fill}" {st}>{escape(s)}</text>')


def rich(x, y, segs, size=11, anchor="start", weight="normal", fill=INK):
    """Text with subscripts: segs = [(txt, is_sub), ...].

    cairosvg mishandles baseline-shift across chained tspans, so each segment
    is placed manually with an estimated advance width.
    """
    factor = 0.63 if weight == "bold" else 0.60

    def seg_w(txt, sub):
        return len(txt) * size * (0.68 if sub else 1.0) * factor

    total = sum(seg_w(t, s) for t, s in segs)
    cursor = x - total / 2 if anchor == "middle" else x
    for txt, sub in segs:
        fs = size * 0.68 if sub else size
        yy = y + size * 0.16 if sub else y
        add(f'<text x="{cursor:.1f}" y="{yy:.1f}" font-size="{fs:.1f}" '
            f'font-weight="{weight}" fill="{fill}">{escape(txt)}</text>')
        cursor += seg_w(txt, sub)


def mtext(x, y, lines, size=11, anchor="middle", weight="normal", fill=INK, lh=None):
    lh = lh or size + 3
    y0 = y - (len(lines) - 1) * lh / 2 + size * 0.35
    for i, ln in enumerate(lines):
        text(x, y0 + i * lh, ln, size=size, anchor=anchor, weight=weight, fill=fill)


def arrow(points, color=NEUT, sw=1.8, dash=None):
    pts = " ".join(f"{x},{y}" for x, y in points)
    mid = {"#4D5E6E": "mg", "#C0392B": "mr"}[color]
    d = f'stroke-dasharray="{dash}"' if dash else ""
    add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{sw}" {d} '
        f'marker-end="url(#a{mid})"/>')


def chip(x, y, w, h, segs, colors, size=12):
    f, s = colors
    rect(x, y, w, h, fill=f, stroke=s, sw=1.3, rx=5)
    rich(x + w / 2, y + h / 2 + size * 0.35, segs, size=size, anchor="middle", weight="bold")


def brace(x, ymid, h, w=13, color=NEUT):
    """Right-facing curly brace '}' : arms at x+w, waist at x."""
    y0, y1 = ymid - h / 2, ymid + h / 2
    k = 0.36
    add(f'<path d="M {x + w} {y0} '
        f'C {x + w * 0.25} {y0 + h * 0.02}, {x + w * 0.25} {y0 + h * k * 0.55}, {x + w * 0.25} {ymid - h * 0.10} '
        f'C {x + w * 0.25} {ymid - h * 0.03}, {x + w * 0.45} {ymid}, {x} {ymid} '
        f'C {x + w * 0.45} {ymid}, {x + w * 0.25} {ymid + h * 0.03}, {x + w * 0.25} {ymid + h * 0.10} '
        f'C {x + w * 0.25} {y1 - h * k * 0.55}, {x + w * 0.25} {y1 - h * 0.02}, {x + w} {y1}" '
        f'fill="none" stroke="{color}" stroke-width="2"/>')


def mini_grid(x, y, cols, rows, cell, paints=None, agents=None, persons=None):
    for r in range(rows):
        for c in range(cols):
            fill = (paints or {}).get((c, r), "#FFFFFF")
            rect(x + c * cell, y + r * cell, cell, cell, fill=fill, stroke=CELL_GRID_ST, sw=0.8)
    for (c, r), col in (agents or {}).items():
        circle(x + (c + 0.5) * cell, y + (r + 0.5) * cell, cell * 0.3, fill=col, stroke="#FFFFFF", sw=1)
    for (c, r), col in (persons or {}).items():
        cxp, cyp = x + (c + 0.5) * cell, y + (r + 0.5) * cell
        s = cell * 0.28
        add(f'<rect x="{cxp - s}" y="{cyp - s}" width="{2 * s}" height="{2 * s}" '
            f'transform="rotate(45 {cxp} {cyp})" fill="{col}" stroke="#FFFFFF" stroke-width="1"/>')


def nn_glyph_white(cx, y, w=52, h=40):
    cols = [cx - w / 2, cx, cx + w / 2]
    ns = [3, 4, 3]
    pts = [[(px, y + h * (i + 1) / (n + 1)) for i in range(n)] for px, n in zip(cols, ns)]
    for a, b in ((0, 1), (1, 2)):
        for (x1, y1) in pts[a]:
            for (x2, y2) in pts[b]:
                line(x1, y1, x2, y2, stroke="#9FC5E8", sw=0.7)
    for col in pts:
        for (px, py) in col:
            circle(px, py, 3, fill="#0F4D92", stroke="#FFFFFF", sw=1)


def cylinder(cx, y, w, h, fill, stroke, sw=1.6):
    ry = 10
    add(f'<path d="M {cx - w / 2} {y + ry} A {w / 2} {ry} 0 0 1 {cx + w / 2} {y + ry} '
        f'L {cx + w / 2} {y + h} A {w / 2} {ry} 0 0 1 {cx - w / 2} {y + h} Z" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    add(f'<ellipse cx="{cx}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


# ================================================================ canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, \'DejaVu Sans\', sans-serif">')
add('<defs>'
    '<marker id="amg" markerWidth="10" markerHeight="10" refX="7.5" refY="5" orient="auto">'
    '<path d="M0,0 L10,5 L0,10 Z" fill="#4D5E6E"/></marker>'
    '<marker id="amr" markerWidth="10" markerHeight="10" refX="7.5" refY="5" orient="auto">'
    '<path d="M0,0 L10,5 L0,10 Z" fill="#C0392B"/></marker>'
    '</defs>')
rect(0, 0, W, H, fill="#FFFFFF", stroke="none")

# ================================================================ TOP zone
rect(20, 20, 1560, 550, fill="#F5F8FA", stroke="#D7E1EA", sw=1.4, rx=14, dash="7 4")

# ---------------- Coordinator LLM
coord_chips = [
    ([("I", False)], C_I),
    ([("S", False), ("t", True)], C_S),
    ([("T", False), ("t", True)], C_T),
    ([("K", False), ("t", True)], C_K),
    ([("e", False), ("t", True)], C_E),
]
for i, (segs, col) in enumerate(coord_chips):
    chip(60, 143 + i * 40, 110, 34, segs, col)
brace(180, 240, 194)
rect(200, 140, 220, 200, fill="#0F4D92", stroke="#0B3A6E", sw=1.6, rx=12)
nn_glyph_white(310, 162)
text(310, 245, "Coordinator LLM", size=16, anchor="middle", weight="bold", fill="#FFFFFF")
text(310, 267, "(RouterAgent · agentic loop)", size=10, anchor="middle", fill="#D6E4F5")
text(310, 322, "dispatch · cancel · re-plan", size=9.5, anchor="middle", fill="#9FC5E8", style="italic")

# ---------------- Worker Agent LM (xN)
worker_chips = [
    ([("s", False), ("i,t", True)], C_SUB),
    ([("o", False), ("i,t", True)], C_O),
    ([("q", False), ("i,t", True)], C_Q),
]
for i, (segs, col) in enumerate(worker_chips):
    chip(520, 193 + i * 40, 110, 34, segs, col)
brace(640, 250, 114)
rect(672, 182, 170, 160, fill="#6C3483", stroke="#AF7AC5", sw=1.3, rx=12)
rect(666, 176, 170, 160, fill="#76448A", stroke="#AF7AC5", sw=1.3, rx=12)
rect(660, 170, 170, 160, fill="#7D3C98", stroke="#5B2C6F", sw=1.6, rx=12)
rect(786, 182, 34, 20, fill="#FFFFFF", stroke="#5B2C6F", sw=1.2, rx=6)
text(803, 196.5, "×N", size=11, anchor="middle", weight="bold", fill="#7D3C98")
text(745, 240, "Worker Agent LM", size=15, anchor="middle", weight="bold", fill="#FFFFFF")
text(745, 261, "(ReAct tool loop)", size=10, anchor="middle", fill="#E8DAEF")
text(745, 312, "navigate · supply · rescue · report", size=9.5, anchor="middle", fill="#D2B4DE", style="italic")

# ---------------- SAR Environment
rect(910, 150, 230, 200, fill="#E5E8E8", stroke="#566573", sw=1.6, rx=12)
text(1025, 184, "SAR Environment", size=15, anchor="middle", weight="bold", fill=INK)
mini_grid(975, 200, 6, 4, 15,
          paints={(1, 1): "#F5B041", (2, 1): "#E74C3C", (0, 3): "#2E86C1", (5, 0): "#212121"},
          agents={(3, 2): "#F06292"}, persons={(4, 0): "#8E44AD"})
text(1025, 300, "GridEngine · SARBarrier", size=10, anchor="middle", fill=NEUT)
text(1025, 318, "turn-based sync · fire spread", size=9, anchor="middle", fill=NEUT, style="italic")

# ---------------- Semantic Map Store
rect(1210, 150, 190, 180, fill="#148F77", stroke="#0E6655", sw=1.6, rx=12)
cylinder(1305, 170, 64, 42, fill="#148F77", stroke="#FFFFFF", sw=1.4)
text(1305, 250, "Semantic", size=15, anchor="middle", weight="bold", fill="#FFFFFF")
text(1305, 270, "Map Store", size=15, anchor="middle", weight="bold", fill="#FFFFFF")
text(1305, 306, "ingest · diff · summarize", size=9.5, anchor="middle", fill="#D1F2EB", style="italic")

# ---------------- TaskWatchdog
rect(1450, 90, 120, 90, fill="#C2185B", stroke="#8A1245", sw=1.6, rx=10)
text(1510, 122, "Task", size=13, anchor="middle", weight="bold", fill="#FFFFFF")
text(1510, 140, "Watchdog", size=13, anchor="middle", weight="bold", fill="#FFFFFF")
# watchdog monitors task store & heartbeats (no LLM)
text(1510, 164, "rule-based", size=9, anchor="middle", fill="#F5B7D4", style="italic")

# ---------------- flow arrows
# dispatch: coordinator -> worker input stack
arrow([(420, 210), (518, 210)])
rich(469, 196, [("dispatch ", False), ("s", False), ("i,t", True)],
     size=10, anchor="middle", fill=NEUT)
text(469, 226, "(A2A)", size=9, anchor="middle", fill=NEUT)
# actions: worker -> environment
arrow([(830, 250), (908, 250)])
rich(869, 238, [("a", False), ("i,t", True)], size=10.5, anchor="middle", weight="bold", fill=NEUT)
# observations: environment -> worker
arrow([(1025, 350), (1025, 400), (745, 400), (745, 344)])
rich(895, 392, [("o", False), ("i,t+1", True), ("  (step N+1)", False)],
     size=10, anchor="middle", fill=NEUT)
# reports: worker -> store
arrow([(745, 170), (745, 100), (1300, 100), (1300, 148)])
rich(1030, 92, [("report ", False), ("r", False), ("i,t", True), ("  ([DATA] push)", False)],
     size=10, anchor="middle", fill=NEUT)
# context injection: store -> coordinator
arrow([(1300, 330), (1300, 440), (310, 440), (310, 342)])
rich(810, 432, [("inject ", False), ("S", False), ("t+1", True), (", ", False),
                ("T", False), ("t+1", True), (", ", False), ("K", False), ("t+1", True),
                ("  into context", False)], size=10, anchor="middle", fill=NEUT)
# alerts: watchdog -> coordinator
arrow([(1510, 90), (1510, 50), (310, 50), (310, 138)], color=RED, dash="5 4")
rich(915, 42, [("alerts ", False), ("e", False), ("t", True),
               ("  (stale · deadline)", False)], size=10, anchor="middle", fill=RED)
# (watchdog monitors TaskStore/heartbeats internally; no extra edge needed)

# ================================================================ LEGEND zone
def legend_panel(x, y, w, h, title_segs, colors, body_lines, grid_glyph=False):
    f, s = colors
    rect(x, y, w, h, fill=f, stroke=s, sw=1.4, rx=10)
    rich(x + 16, y + 30, title_segs, size=13, weight="bold")
    tx = x + 16
    if grid_glyph:
        mini_grid(x + 16, y + 48, 5, 4, 16,
                  paints={(1, 1): "#E74C3C", (0, 3): "#2E86C1"},
                  agents={(3, 2): "#F06292"}, persons={(4, 0): "#8E44AD"})
        tx = x + 116
    for i, ln in enumerate(body_lines):
        text(tx, y + 60 + i * 16, ln, size=10, fill=INK)


LW, LH1, LH2 = 375, 180, 160
LX = [20, 415, 810, 1205]
Y1, Y2 = 610, 810

legend_panel(LX[0], Y1, LW, LH1, [("Mission I", False)], C_I,
             ["Extinguish all fires and rescue",
              "every person in Scene 1 within",
              "the 50-step budget.",
              "",
              "Agents: 3 rescue robots, seed 42."])
legend_panel(LX[1], Y1, LW, LH1, [("Semantic Map ", False), ("S", False), ("t", True)], C_S,
             ["fire (high)  (13, 7)",
              "person  (8, 2)",
              "reservoir  (0, 9)",
              "deposit  (19, 0)"], grid_glyph=True)
legend_panel(LX[2], Y1, LW, LH1, [("Team Status ", False), ("T", False), ("t", True)], C_T,
             ["A1  (12, 7)   supply×2   RUNNING",
              "A2  (5, 3)    carrying person",
              "A3  (20, 15)  —   IDLE",
              "",
              "coverage 38% · transport 1/4"])
legend_panel(LX[3], Y1, LW, LH1, [("Subtask States ", False), ("K", False), ("t", True)], C_K,
             ["S1 suppress-fire → A1  RUNNING",
              "S2 rescue-person → A2  RUNNING",
              "S3 transport → A1/A2  PENDING",
              "S4 explore → A3  COMPLETED"])

legend_panel(LX[0], Y2, LW, LH2, [("Observation ", False), ("o", False), ("i,t", True)], C_O,
             ["“Fire (high) at (13, 7); person at",
              "(8, 2); reservoir at (0, 9).”",
              "",
              "→ pushed as report r_i,t ([DATA])"])
legend_panel(LX[1], Y2, LW, LH2, [("Action ", False), ("a", False), ("i,t", True)], C_A,
             ["[NavigateTo(13, 7),",
              " UseSupply,",
              " ReportObservation]"])
legend_panel(LX[2], Y2, LW, LH2, [("Primitive Feedback ", False), ("d", False), ("t", True)], C_D,
             ["UseSupply failed: no supply",
              "in inventory;",
              "barrier timeout → auto-NoOp",
              "(A2, step 17)"])
legend_panel(LX[3], Y2, LW, LH2, [("Alert ", False), ("e", False), ("t", True)], C_E,
             ["TASK_STALE: S1 no progress",
              "for 6 steps;",
              "deadline warning: 10 steps",
              "left"])

add("</svg>")

svg = "\n".join(parts)
with open(OUT_SVG, "w", encoding="utf-8") as f:
    f.write(svg)
print(f"wrote {OUT_SVG} ({len(svg)} bytes)")

try:
    import cairosvg
    cairosvg.svg2png(url=OUT_SVG, write_to=OUT_PNG, scale=1.6)
    print(f"wrote {OUT_PNG}")
except ImportError:
    print("cairosvg not available; SVG only")
