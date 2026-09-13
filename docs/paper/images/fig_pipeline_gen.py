#!/usr/bin/env python3
"""Generate fig_pipeline.svg/.png — LLaMAR SAR four-step pipeline figure.

Layout mirrors a 4-step method pipeline:
  Step 1  Global Mission Decomposition (Coordinator LLM; context & memory
          assembly bar: pre_llm refresh → prune → assemble)
  Step 2  Subtask Allocation via A2A
  Step 3  Barrier-Synchronized Execution (worker agents; per-round assemble/observe note)
  Step 4  Dynamic Semantic Memory Updating (SemanticMapStore)

Run:  uv run --with cairosvg python fig_pipeline_gen.py
"""

from html import escape

W, H = 1600, 950
OUT_SVG = "fig_pipeline.svg"
OUT_PNG = "fig_pipeline.png"

# ---------------------------------------------------------------- palette
INK = "#1F2933"       # main text
NEUT = "#4D5E6E"      # neutral stroke
ORANGE = "#E67E22"    # inter-module flow
RED = "#C0392B"       # warning / retry
GREEN = "#1E8449"     # success
BLUE = "#2874A6"      # store / hero
HDR = "#922B21"       # step headers (dark red)

C_STEP1_FILL, C_STEP1_ST = "#FEF9E7", "#B7950B"
C_STEP2_FILL, C_STEP2_ST = "#FADBD8", "#922B21"
C_STEP4_FILL, C_STEP4_ST = "#F4F8FD", "#5D6D7E"
C_TEMP_FILL, C_TEMP_ST = "#EBF5FB", "#5DADE2"
C_SPAT_FILL, C_SPAT_ST = "#E9F7EF", "#52BE80"
C_STAT_FILL, C_STAT_ST = "#F4ECF7", "#7D3C98"
C_MISSION_FILL, C_MISSION_ST = "#D5F5E3", "#1E8449"
C_MEM_FILL, C_MEM_ST = "#FCF3CF", "#B7950B"
C_HERO_FILL, C_HERO_ST = "#EAF2FB", "#0F4D92"
C_CARD_FILL, C_CARD_ST = "#F9EBEA", "#B03A2E"
C_COLL_FILL, C_COLL_ST = "#FDEBD0", "#D68910"
C_TOOL_FILL, C_TOOL_ST = "#D6EAF8", "#2E86C1"
C_STORE_FILL, C_STORE_ST = "#AED6F1", "#2874A6"

# map-cell colors (same semantics as SAR map UI)
CELL_BASE = "#FFFFFF"
CELL_FIRE_HI = "#E74C3C"
CELL_FIRE_MID = "#F5B041"
CELL_FIRE_LOW = "#FDEBD0"
CELL_AGENT = "#F06292"
CELL_PERSON = "#8E44AD"
CELL_RESERVOIR = "#2E86C1"
CELL_DEPOSIT = "#212121"
CELL_GRID_ST = "#C5CBCE"

parts: list[str] = []


def add(s: str) -> None:
    parts.append(s)


def rect(x, y, w, h, fill="none", stroke=NEUT, sw=1.2, rx=0, dash=None, opacity=None):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    op = f'opacity="{opacity}"' if opacity else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {d} {op}/>')


def circle(cx, cy, r, fill="none", stroke=NEUT, sw=1.2):
    add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def line(x1, y1, x2, y2, stroke=NEUT, sw=1.2, dash=None):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}" {d}/>')


def text(x, y, s, size=11, anchor="start", weight="normal", fill=INK, style=None, spacing=None):
    st = f'font-style="{style}"' if style else ""
    ls = f'letter-spacing="{spacing}"' if spacing else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'font-weight="{weight}" fill="{fill}" {st} {ls}>{escape(s)}</text>')


def mtext(x, y, lines, size=11, anchor="middle", weight="normal", fill=INK, lh=None):
    """Multi-line centered text; y is the vertical center of the block."""
    lh = lh or size + 3
    y0 = y - (len(lines) - 1) * lh / 2 + size * 0.35
    for i, ln in enumerate(lines):
        text(x, y0 + i * lh, ln, size=size, anchor=anchor, weight=weight, fill=fill)


def arrow(points, color=NEUT, sw=1.6, dash=None, both=False):
    """Polyline arrow through points [(x,y),...]; head at last point."""
    pts = " ".join(f"{x},{y}" for x, y in points)
    mid = {"#4D5E6E": "mg", "#E67E22": "mo", "#C0392B": "mr", "#2874A6": "mb"}[color]
    d = f'stroke-dasharray="{dash}"' if dash else ""
    ms = f'marker-start="url(#a{mid})"' if both else ""
    add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{sw}" {d} '
        f'marker-end="url(#a{mid})" {ms}/>')


# ---------------------------------------------------------------- glyphs
def cylinder(cx, y, w, h, fill, stroke):
    """Database cylinder: top ellipse center (cx, y+ry)."""
    ry = 16
    add(f'<path d="M {cx - w / 2} {y + ry} '
        f'A {w / 2} {ry} 0 0 1 {cx + w / 2} {y + ry} '
        f'L {cx + w / 2} {y + h} '
        f'A {w / 2} {ry} 0 0 1 {cx - w / 2} {y + h} Z" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
    add(f'<ellipse cx="{cx}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')


def mini_grid(x, y, cols, rows, cell, paints=None, agents=None, persons=None):
    """Tiled SAR map glyph. paints: {(c,r): color}; agents/persons: {(c,r): color}."""
    for r in range(rows):
        for c in range(cols):
            fill = (paints or {}).get((c, r), CELL_BASE)
            rect(x + c * cell, y + r * cell, cell, cell, fill=fill, stroke=CELL_GRID_ST, sw=0.8)
    for (c, r), col in (agents or {}).items():
        circle(x + (c + 0.5) * cell, y + (r + 0.5) * cell, cell * 0.32, fill=col, stroke="#FFFFFF", sw=1)
    for (c, r), col in (persons or {}).items():
        cxp, cyp = x + (c + 0.5) * cell, y + (r + 0.5) * cell
        s = cell * 0.3
        add(f'<rect x="{cxp - s}" y="{cyp - s}" width="{2 * s}" height="{2 * s}" '
            f'transform="rotate(45 {cxp} {cyp})" fill="{col}" stroke="#FFFFFF" stroke-width="1"/>')


def robot(cx, y, s=1.0):
    """Small rescue-robot glyph; y = top of antenna."""
    g = "#566573"
    line(cx, y, cx, y + 8 * s, stroke=g, sw=1.4 * s)                       # antenna
    circle(cx, y, 2.2 * s, fill=g, stroke=g)
    rect(cx - 13 * s, y + 8 * s, 26 * s, 16 * s, fill="#E5E8E8", stroke=g, sw=1.4 * s, rx=5 * s)   # head
    circle(cx - 5.5 * s, y + 16 * s, 2 * s, fill=g, stroke=g)              # eyes
    circle(cx + 5.5 * s, y + 16 * s, 2 * s, fill=g, stroke=g)
    rect(cx - 16 * s, y + 26 * s, 32 * s, 24 * s, fill="#D5DBDB", stroke=g, sw=1.4 * s, rx=4 * s)  # body
    rect(cx - 8 * s, y + 32 * s, 16 * s, 8 * s, fill="#AED6F1", stroke=g, sw=1 * s, rx=2 * s)      # chest
    line(cx - 16 * s, y + 32 * s, cx - 24 * s, y + 44 * s, stroke=g, sw=2 * s)                     # arms
    line(cx + 16 * s, y + 32 * s, cx + 24 * s, y + 44 * s, stroke=g, sw=2 * s)
    circle(cx - 8 * s, y + 54 * s, 5 * s, fill="#F7F9FC", stroke=g, sw=1.4 * s)                    # wheels
    circle(cx + 8 * s, y + 54 * s, 5 * s, fill="#F7F9FC", stroke=g, sw=1.4 * s)


def nn_glyph(x, y, w=46, h=44):
    """Tiny 3-layer network glyph."""
    cols = [x, x + w / 2, x + w]
    ns = [3, 4, 3]
    pts = []
    for cx, n in zip(cols, ns):
        col = [(cx, y + h * (i + 1) / (n + 1)) for i in range(n)]
        pts.append(col)
    for a, b in ((0, 1), (1, 2)):
        for (x1, y1) in pts[a]:
            for (x2, y2) in pts[b]:
                line(x1, y1, x2, y2, stroke="#9AA7B0", sw=0.7)
    for ci, col in enumerate(pts):
        for (cx, cy) in col:
            fill = C_HERO_FILL if ci == 1 else "#F7F9FC"
            circle(cx, cy, 3.2, fill=fill, stroke=C_HERO_ST, sw=1)


def dag_glyph(x, y, w=52, h=40):
    """Tiny DAG glyph: 4 nodes, 4 edges."""
    n = [(x, y + h / 2), (x + w * 0.45, y + 4), (x + w * 0.45, y + h - 4), (x + w, y + h / 2)]
    for a, b in ((0, 1), (0, 2), (1, 3), (2, 3)):
        line(n[a][0], n[a][1], n[b][0], n[b][1], stroke="#9AA7B0", sw=0.9)
    for i, (cx, cy) in enumerate(n):
        circle(cx, cy, 4, fill="#FADBD8" if i == 3 else "#F7F9FC", stroke=C_STEP2_ST, sw=1.1)


def clock_glyph(cx, cy, r=13):
    circle(cx, cy, r, fill="#F7F9FC", stroke=NEUT, sw=1.4)
    line(cx, cy, cx, cy - r * 0.55, stroke=NEUT, sw=1.4)
    line(cx, cy, cx + r * 0.45, cy + r * 0.2, stroke=NEUT, sw=1.4)


# ================================================================ canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, \'DejaVu Sans\', sans-serif">')
add('<defs>'
    '<marker id="amg" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">'
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#4D5E6E"/></marker>'
    '<marker id="amo" markerWidth="11" markerHeight="11" refX="8" refY="5.5" orient="auto">'
    '<path d="M0,0 L11,5.5 L0,11 Z" fill="#E67E22"/></marker>'
    '<marker id="amr" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">'
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#C0392B"/></marker>'
    '<marker id="amb" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">'
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#2874A6"/></marker>'
    '</defs>')
rect(0, 0, W, H, fill="#FFFFFF", stroke="none")

# ================================================================ LEFT container
rect(20, 20, 540, 908, fill=C_STEP1_FILL, stroke=C_STEP1_ST, sw=1.6, rx=14, dash="7 4")

# ---------------- Step 1
text(40, 50, "Step 1: Global Mission Decomposition", size=15, weight="bold", fill=HDR)

rect(40, 70, 180, 44, fill=C_MISSION_FILL, stroke=C_MISSION_ST, sw=1.4, rx=6)
mtext(130, 92, ["SAR Mission"], size=12, weight="bold")
rect(340, 70, 180, 44, fill=C_MEM_FILL, stroke=C_MEM_ST, sw=1.4, rx=6)
mtext(430, 92, ["Semantic Memory"], size=12, weight="bold")

# context & memory assembly bar (pre_llm hook, every LLM round)
arrow([(130, 114), (130, 124)])
arrow([(430, 114), (430, 124)])
rect(110, 124, 410, 34, fill="#E8F8F5", stroke="#148F77", sw=1.4, rx=6)
text(315, 138, "Context Assembly (pre_llm): refresh → prune → assemble",
     size=10, anchor="middle", weight="bold", fill="#0E6655")
text(315, 151, "system + recent window + memory block (env · state · tasks · alerts)",
     size=8.5, anchor="middle", fill="#148F77")
arrow([(315, 158), (315, 164)], sw=1.4)

nn_glyph(56, 170)
rect(110, 164, 410, 50, fill=C_HERO_FILL, stroke=C_HERO_ST, sw=1.6, rx=6)
mtext(315, 189, ["Coordinator LLM (RouterAgent)"], size=12.5, weight="bold")

arrow([(315, 214), (315, 240)])

dag_glyph(52, 250)
rect(110, 244, 410, 44, fill=C_STEP2_FILL, stroke=C_STEP2_ST, sw=1.4, rx=6)
mtext(315, 266, ["Subtask Plan (DAG)"], size=12, weight="bold")

# ---------------- Step 2
text(40, 330, "Step 2: Subtask Allocation via A2A", size=15, weight="bold", fill=HDR)
arrow([(480, 288), (480, 372)], color=ORANGE, sw=3)

rect(40, 372, 480, 42, fill="#F5B7B1", stroke=C_STEP2_ST, sw=1.4, rx=6)
mtext(280, 393, ["Monitor & Allocator (MissionRuntime)"], size=12, weight="bold")

cards = [
    ("Fire Suppression (S1)", C_CARD_FILL, C_CARD_ST, "Agent-1"),
    ("Search & Rescue (S2)", C_CARD_FILL, C_CARD_ST, "Agent-2"),
    ("Collab Transport (S3)", C_COLL_FILL, C_COLL_ST, "Agent-1/2"),
    ("Exploration (S4)", C_CARD_FILL, C_CARD_ST, "Agent-3"),
]
for i, (label, f, s, ag) in enumerate(cards):
    cx0 = 40 + i * 125
    rect(cx0, 452, 115, 44, fill=f, stroke=s, sw=1.2, rx=5)
    mtext(cx0 + 57.5, 474, [label], size=10, weight="bold")
    arrow([(cx0 + 57.5, 414), (cx0 + 57.5, 452)], sw=1.4)
    robot(cx0 + 57.5, 516, s=0.72)
    text(cx0 + 57.5, 566, ag, size=10.5, anchor="middle", weight="bold", fill=NEUT)
text(160, 438, "Parallel", size=10, anchor="middle", style="italic", fill=NEUT)
text(410, 438, "Sequential", size=10, anchor="middle", style="italic", fill=NEUT)

# ---------------- Step 3
text(40, 636, "Step 3: Barrier-Synchronized Execution", size=15, weight="bold", fill=HDR)
text(40, 654, "worker loop: pre_llm assemble (state · task · episodes · mailbox) → act → post_tool observe",
     size=9.5, style="italic", fill=NEUT)
arrow([(480, 592), (480, 690)], color=ORANGE, sw=3)

# success / failure side box
rect(440, 682, 100, 140, fill=C_MEM_FILL, stroke=C_MEM_ST, sw=1.4, rx=6)
mtext(490, 752, ["Subtask", "Success", "or Failure"], size=10.5, weight="bold")

chain1 = [("NavigateTo", 40), ("GetSupply", 170), ("UseSupply", 300)]
for label, cx0 in chain1:
    rect(cx0, 690, 110, 36, fill=C_TOOL_FILL, stroke=C_TOOL_ST, sw=1.3, rx=5)
    mtext(cx0 + 55, 708, [label], size=10.5, weight="bold")
arrow([(150, 708), (170, 708)], sw=1.4)
arrow([(280, 708), (300, 708)], sw=1.4)
arrow([(410, 708), (440, 708)], sw=1.4)
# retry loop: UseSupply -> GetSupply
arrow([(355, 726), (355, 738), (225, 738), (225, 726)], color=RED, sw=1.2, dash="4 3")
line(147, 747, 157, 757, stroke=RED, sw=1.6)
line(147, 757, 157, 747, stroke=RED, sw=1.6)
text(166, 756, "out of supply → back to reservoir", size=9.5, fill=RED)

chain2 = [("Explore", 40), ("CarryPerson", 170), ("DropOffPerson", 300)]
for label, cx0 in chain2:
    rect(cx0, 780, 110, 36, fill=C_TOOL_FILL, stroke=C_TOOL_ST, sw=1.3, rx=5)
    mtext(cx0 + 55, 798, [label], size=10.5, weight="bold")
arrow([(150, 798), (170, 798)], sw=1.4)
arrow([(280, 798), (300, 798)], sw=1.4)
arrow([(410, 798), (440, 798)], sw=1.4)
# retry loop: CarryPerson -> Explore
arrow([(225, 816), (225, 828), (95, 828), (95, 816)], color=RED, sw=1.2, dash="4 3")
line(37, 837, 47, 847, stroke=RED, sw=1.6)
line(37, 847, 47, 837, stroke=RED, sw=1.6)
text(56, 846, "person not found → re-explore", size=9.5, fill=RED)

# barrier zone
text(40, 872, "SARBarrier: one action per agent per step (timeout → auto-NoOp)", size=10, fill=INK)
line(270, 880, 270, 922, stroke=NEUT, sw=1.6, dash="5 4")
for i, ag in enumerate(("A1", "A2", "A3")):
    yy = 886 + i * 13
    arrow([(110, yy), (264, yy)], sw=1.2)
    text(100, yy + 3.5, ag, size=9, anchor="end", fill=NEUT, weight="bold")
arrow([(276, 899), (336, 899)], sw=1.4)
text(342, 903, "step N+1", size=9.5, fill=NEUT)
clock_glyph(440, 899)

# ================================================================ RIGHT container
rect(580, 20, 1000, 908, fill=C_STEP4_FILL, stroke=C_STEP4_ST, sw=1.6, rx=14, dash="7 4")
text(600, 50, "Step 4: Dynamic Semantic Memory Updating", size=15, weight="bold", fill=HDR)

# store cylinder (top right)
mtext(1370, 40, ["Sub-Task, Tool-Calling,", "Observation, Log, ..."], size=10, fill=NEUT)
cylinder(1370, 64, 140, 150, C_STORE_FILL, C_STORE_ST)
text(1370, 244, "SemanticMapStore", size=11.5, anchor="middle", weight="bold", fill=BLUE)

# subtask-agent tool stack
rect(1470, 90, 90, 140, fill="#FFFFFF", stroke=NEUT, sw=1.3, rx=6)
text(1515, 108, "Subtask Agent", size=10, anchor="middle", weight="bold")
for i, tname in enumerate(("Tool-1", "Tool-2", "Tool-n")):
    rect(1480, 118 + i * 30, 70, 22, fill="#F7F9FC", stroke=NEUT, sw=1, rx=4)
    mtext(1515, 129 + i * 30, [tname], size=9)
arrow([(1440, 150), (1470, 150)], color=BLUE, sw=1.2, dash="4 3")
arrow([(1470, 186), (1440, 186)], color=BLUE, sw=1.2, dash="4 3")

# ---------------- Temporal panel
rect(620, 150, 620, 180, fill=C_TEMP_FILL, stroke=C_TEMP_ST, sw=1.4, rx=10, dash="6 4")
text(930, 174, "Temporal Sequence (Queue)", size=12.5, anchor="middle", weight="bold", fill=INK)

snapshots = [
    (660, "T−1", {(2, 1): CELL_FIRE_HI, (2, 2): CELL_FIRE_MID}, {(1, 3): CELL_AGENT}, {(4, 1): CELL_PERSON}),
    (870, "T", {(2, 1): CELL_FIRE_MID, (2, 2): CELL_FIRE_LOW}, {(2, 3): CELL_AGENT}, {(4, 1): CELL_PERSON}),
    (1080, "T+1", {(2, 1): CELL_FIRE_LOW}, {(3, 3): CELL_AGENT}, {}),
]
for sx, lab, fires, ags, ps in snapshots:
    rect(sx + 10, 198, 150, 118, fill="#E3E9ED", stroke="#B9C2C7", sw=1, rx=4)
    rect(sx + 5, 193, 150, 118, fill="#EFF3F5", stroke="#B9C2C7", sw=1, rx=4)
    rect(sx, 188, 150, 118, fill="#FFFFFF", stroke=NEUT, sw=1.2, rx=4)
    mini_grid(sx + 15, 197, 6, 5, 20, paints=fires, agents=ags, persons=ps)
    text(sx + 75, 184, lab, size=11, anchor="middle", weight="bold", fill=INK)
text(840, 257, "...", size=16, anchor="middle", weight="bold", fill=NEUT)
arrow([(1020, 250), (1070, 250)], color=RED, sw=1.3, both=True)
text(1000, 348, "Synchronize episodic states", size=10, anchor="middle", fill=RED)

# ---------------- Spatial panel
rect(620, 370, 620, 270, fill=C_SPAT_FILL, stroke=C_SPAT_ST, sw=1.4, rx=10, dash="6 4")
text(930, 394, "Spatial Semantic Grid (Map)", size=12.5, anchor="middle", weight="bold", fill=INK)

paints = {
    (2, 1): CELL_FIRE_HI, (2, 2): CELL_FIRE_MID, (3, 1): CELL_FIRE_LOW,
    (6, 2): CELL_FIRE_LOW, (6, 3): CELL_FIRE_LOW,
    (0, 6): CELL_RESERVOIR, (9, 0): CELL_DEPOSIT,
}
mini_grid(660, 410, 10, 7, 30, paints=paints,
          agents={(4, 3): CELL_AGENT, (7, 5): CELL_AGENT},
          persons={(8, 1): CELL_PERSON})

legend = [
    (CELL_FIRE_HI, "Fire (high)"), (CELL_FIRE_MID, "Fire (mid)"),
    (CELL_FIRE_LOW, "Fire (low)"), (CELL_AGENT, "Agent"),
    (CELL_PERSON, "Person"), (CELL_RESERVOIR, "Reservoir"),
    (CELL_DEPOSIT, "Deposit"),
]
for i, (col, lab) in enumerate(legend):
    lx = 995 + (i // 4) * 115
    ly = 420 + (i % 4) * 26
    rect(lx, ly - 10, 13, 13, fill=col, stroke=CELL_GRID_ST, sw=0.8)
    text(lx + 20, ly + 1, lab, size=10, fill=INK)
mtext(1110, 600, ["Update agent states &", "scene topology"], size=10, fill=RED)

# ---------------- Status panel
rect(620, 680, 620, 220, fill=C_STAT_FILL, stroke=C_STAT_ST, sw=1.4, rx=10, dash="6 4")
text(930, 704, "Team & Task Status", size=12.5, anchor="middle", weight="bold", fill=INK)

cols = ["Agent", "Pos", "Inventory", "Status"]
rows = [
    ("Agent-1", "(12, 7)", "Supply×2", "RUNNING"),
    ("Agent-2", "(5, 3)", "Person", "RUNNING"),
    ("Agent-3", "(20, 15)", "—", "IDLE"),
]
tx, tw = 660, 100
rect(tx, 718, tw * 4, 24, fill="#D2B4DE", stroke=C_STAT_ST, sw=1)
for j, cname in enumerate(cols):
    if j:
        line(tx + j * tw, 718, tx + j * tw, 808, stroke=C_STAT_ST, sw=0.8)
    mtext(tx + j * tw + tw / 2, 730, [cname], size=10, weight="bold")
for i, row in enumerate(rows):
    ry = 742 + i * 22
    rect(tx, ry, tw * 4, 22, fill="#FFFFFF", stroke=C_STAT_ST, sw=0.8)
    for j, val in enumerate(row):
        mtext(tx + j * tw + tw / 2, ry + 11, [val], size=9.5)

text(660, 836, "Subtask lifecycle", size=10, weight="bold", fill=INK)
lc = [("PENDING", 660, 90), ("RUNNING", 790, 100), ("COMPLETED", 930, 110), ("FAILED", 1080, 90)]
for label, lx, lw in lc:
    rect(lx, 846, lw, 26, fill="#FFFFFF", stroke=C_STAT_ST, sw=1.1, rx=5)
    mtext(lx + lw / 2, 859, [label], size=9.5, weight="bold")
for (x1, x2) in ((750, 790), (890, 930), (1040, 1080)):
    arrow([(x1, 859), (x2, 859)], sw=1.1)

# ---------------- internal flow arrows (right block)
arrow([(930, 330), (930, 370)], sw=1.6)
arrow([(930, 640), (930, 680)], sw=1.6)
text(1065, 664, "Refresh team & task status", size=10, anchor="middle", fill=RED)
arrow([(1240, 790), (1270, 790), (1270, 210), (1300, 210)], color=ORANGE, sw=2)

# ---------------- Embodiment panel
rect(1300, 270, 260, 370, fill="#FFFFFF", stroke=NEUT, sw=1.4, rx=8)
text(1430, 294, "Embodiment (Worker Agent)", size=12, anchor="middle", weight="bold", fill=INK)
robot(1430, 312, s=1.05)
text(1430, 396, "Rescue Robot", size=10, anchor="middle", fill=NEUT)

checks = [
    ("Navigation Tools", "NavigateTo · Move · Explore"),
    ("Firefighting", "GetSupply · UseSupply"),
    ("Rescue & Transport", "CarryPerson · DropOffPerson"),
    ("Communication", "ReportObservation · AskCoordinator"),
]
for i, (head, sub) in enumerate(checks):
    yy = 428 + i * 52
    text(1320, yy, head, size=11, weight="bold", fill=INK)
    add(f'<polyline points="1448,{yy - 4} 1452,{yy} 1460,{yy - 10}" fill="none" '
        f'stroke="{GREEN}" stroke-width="2"/>')
    text(1320, yy + 16, sub, size=9.5, fill=NEUT)

# ---------------- Environment panel
rect(1300, 670, 260, 230, fill="#FFFFFF", stroke=NEUT, sw=1.4, rx=8)
text(1430, 694, "Environment (SAR World)", size=12, anchor="middle", weight="bold", fill=INK)
mini_grid(1350, 712, 6, 4, 18,
          paints={(1, 1): CELL_FIRE_MID, (0, 3): CELL_RESERVOIR},
          agents={(3, 2): CELL_AGENT}, persons={(4, 0): CELL_PERSON})
for i, ln in enumerate(("GridEngine turn loop", "Fire spread & intensity",
                        "Coverage / transport metrics")):
    circle(1358, 812 + i * 24 - 3.5, 2, fill=NEUT, stroke=NEUT)
    text(1368, 812 + i * 24, ln, size=10, fill=INK)

# ================================================================ inter-module arrows
# Update: step 3 -> step 4
arrow([(560, 790), (620, 790)], color=ORANGE, sw=3)
text(590, 778, "Update", size=11, anchor="middle", weight="bold", fill=ORANGE)
# Retrieve: store -> semantic memory
arrow([(1300, 95), (520, 95)], color=ORANGE, sw=3)
text(910, 85, "Retrieve", size=11, anchor="middle", weight="bold", fill=ORANGE)

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
