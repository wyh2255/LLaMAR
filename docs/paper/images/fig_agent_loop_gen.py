#!/usr/bin/env python3
"""Generate fig_agent_loop.svg/.png — LLaMAR-SAR agentic tool-loop figure.

Emphasis (per review feedback):
  * agent != static workflow: the LLM selects the next tool every turn
  * tools read/write shared state: Mission Plan (K_t) and Semantic Map (S_t)
  * coordinator agent and worker agent both run closed observe-act loops

Run:  uv run --with cairosvg python fig_agent_loop_gen.py
"""

from html import escape

W, H = 1600, 800
OUT_SVG = "fig_agent_loop.svg"
OUT_PNG = "fig_agent_loop.png"

INK = "#1F2933"
NEUT = "#4D5E6E"
GRAY = "#7A8794"
ORANGE = "#E67E22"
BLUE = "#2874A6"

C_CTOOL = ("#EAF2FB", "#0F4D92")   # coordinator tools
C_WTOOL = ("#F4ECF7", "#7D3C98")   # worker tools
C_MP = ("#FDEBD0", "#D68910")      # mission plan
C_MISSION = ("#D5F5E3", "#1E8449")  # mission instruction
C_SM = ("#AED6F1", "#2874A6")      # semantic map store
C_ENV = ("#E5E8E8", "#566573")     # environment

parts: list[str] = []


def add(s: str) -> None:
    parts.append(s)


def rect(x, y, w, h, fill="none", stroke=NEUT, sw=1.2, rx=0, dash=None):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" {d}/>')


def circle(cx, cy, r, fill="none", stroke=NEUT, sw=1.2):
    add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def text(x, y, s, size=11, anchor="start", weight="normal", fill=INK, style=None, rotate=None):
    st = f'font-style="{style}"' if style else ""
    tr = f'transform="rotate({rotate} {x} {y})"' if rotate else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'font-weight="{weight}" fill="{fill}" {st} {tr}>{escape(s)}</text>')


def arrow(points, color=NEUT, sw=1.6, dash=None, both=False):
    pts = " ".join(f"{x},{y}" for x, y in points)
    mid = {"#4D5E6E": "mg", "#E67E22": "mo", "#7A8794": "ml"}[color]
    d = f'stroke-dasharray="{dash}"' if dash else ""
    ms = f'marker-start="url(#a{mid})"' if both else ""
    add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{sw}" {d} '
        f'marker-end="url(#a{mid})" {ms}/>')


def tool_chip(cx, cy, label, colors, w=130, h=40, size=10.5, tag=None):
    f, s = colors
    rect(cx - w / 2, cy - h / 2, w, h, fill=f, stroke=s, sw=1.3, rx=6)
    text(cx, cy + size * 0.35, label, size=size, anchor="middle", weight="bold")
    if tag:
        text(cx, cy + h / 2 + 13, tag, size=8.5, anchor="middle", fill=GRAY, style="italic")


def cylinder(cx, y, w, h, fill, stroke, sw=1.5):
    ry = 12
    add(f'<path d="M {cx - w / 2} {y + ry} A {w / 2} {ry} 0 0 1 {cx + w / 2} {y + ry} '
        f'L {cx + w / 2} {y + h} A {w / 2} {ry} 0 0 1 {cx - w / 2} {y + h} Z" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
    add(f'<ellipse cx="{cx}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def dag_glyph(x, y, w=36, h=34):
    n = [(x, y + h / 2), (x + w * 0.45, y + 2), (x + w * 0.45, y + h - 2), (x + w, y + h / 2)]
    for a, b in ((0, 1), (0, 2), (1, 3), (2, 3)):
        add(f'<line x1="{n[a][0]}" y1="{n[a][1]}" x2="{n[b][0]}" y2="{n[b][1]}" '
            f'stroke="#B9770E" stroke-width="1"/>')
    for i, (cx, cy) in enumerate(n):
        circle(cx, cy, 3.4, fill="#FDEBD0" if i else "#FDF2E0", stroke="#B9770E", sw=1.1)


def mini_grid(x, y, cols, rows, cell, paints=None, agents=None, persons=None):
    for r in range(rows):
        for c in range(cols):
            fill = (paints or {}).get((c, r), "#FFFFFF")
            rect(x + c * cell, y + r * cell, cell, cell, fill=fill, stroke="#C5CBCE", sw=0.8)
    for (c, r), col in (agents or {}).items():
        circle(x + (c + 0.5) * cell, y + (r + 0.5) * cell, cell * 0.3, fill=col, stroke="#FFFFFF", sw=1)
    for (c, r), col in (persons or {}).items():
        cxp, cyp = x + (c + 0.5) * cell, y + (r + 0.5) * cell
        s = cell * 0.28
        add(f'<rect x="{cxp - s}" y="{cyp - s}" width="{2 * s}" height="{2 * s}" '
            f'transform="rotate(45 {cxp} {cyp})" fill="{col}" stroke="#FFFFFF" stroke-width="1"/>')


# ================================================================ canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
    f'viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, \'DejaVu Sans\', sans-serif">')
add('<defs>'
    '<marker id="amg" markerWidth="10" markerHeight="10" refX="7.5" refY="5" orient="auto">'
    '<path d="M0,0 L10,5 L0,10 Z" fill="#4D5E6E"/></marker>'
    '<marker id="amo" markerWidth="12" markerHeight="12" refX="9" refY="6" orient="auto">'
    '<path d="M0,0 L12,6 L0,12 Z" fill="#E67E22"/></marker>'
    '<marker id="aml" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">'
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#7A8794"/></marker>'
    '</defs>')
rect(0, 0, W, H, fill="#FFFFFF", stroke="none")

# ================================================================ Coordinator panel
rect(30, 30, 690, 530, fill="#F5F9FD", stroke="#B9C7D2", sw=1.4, rx=14, dash="7 4")
text(48, 62, "Coordinator Agent", size=14, weight="bold", fill=INK)

# mission instruction
rect(250, 65, 130, 50, fill=C_MISSION[0], stroke=C_MISSION[1], sw=1.3, rx=6)
text(315, 86, "Mission I", size=11.5, anchor="middle", weight="bold")
text(315, 104, "SAR task + budget", size=8.5, anchor="middle", fill=GRAY)
arrow([(315, 115), (315, 253)])

# mission plan node (shared state, lives next to coordinator)
rect(470, 60, 170, 60, fill=C_MP[0], stroke=C_MP[1], sw=1.3, rx=6)
dag_glyph(482, 73)
text(524, 86, "Mission Plan", size=11, weight="bold")
text(524, 104, "MissionGraph · K_t", size=8.5, fill=GRAY)
arrow([(470, 90), (410, 90), (410, 253)], color=GRAY, sw=1.3, dash="4 3")
text(438, 82, "K_t", size=9, anchor="middle", fill=GRAY, style="italic")

# LLM core
rect(285, 255, 200, 90, fill="#0F4D92", stroke="#0B3A6E", sw=1.6, rx=12)
text(385, 297, "Coordinator LLM", size=15, anchor="middle", weight="bold", fill="#FFFFFF")
text(385, 317, "RouterAgent · agentic loop", size=9.5, anchor="middle", fill="#D6E4F5")

# tools around the LLM (symmetric ring: left/right columns at x=170 / x=600)
tool_chip(600, 160, "UpdatePlan", C_CTOOL)
tool_chip(600, 320, "DispatchSubtask", C_CTOOL)
tool_chip(600, 440, "CancelTask", C_CTOOL)
tool_chip(170, 440, "FinishTask", C_CTOOL)
tool_chip(170, 160, "QuerySARState", C_CTOOL, tag="(oracle mode)")

# plan read/write between UpdatePlan tool and Mission Plan
arrow([(580, 140), (580, 122)], sw=1.4, both=True)

# LLM <-> tools (selection / result)
arrow([(460, 262), (556, 184)], color=GRAY, sw=1.2, both=True)
arrow([(310, 262), (214, 184)], color=GRAY, sw=1.2, both=True)
arrow([(450, 343), (545, 418)], color=GRAY, sw=1.2, both=True)
arrow([(320, 343), (225, 418)], color=GRAY, sw=1.2, both=True)
arrow([(485, 320), (533, 320)], color=ORANGE, sw=2.4, both=True)

text(385, 532, "ReAct loop: reason → select tool → act", size=9.5, anchor="middle", fill=GRAY, style="italic")

# ================================================================ Worker panel
rect(880, 30, 690, 530, fill="#FAF5FC", stroke="#D2B4DE", sw=1.4, rx=14, dash="7 4")
text(898, 62, "Worker Agent ×N", size=14, weight="bold", fill=INK)
text(898, 80, "one ReAct loop per rescue robot", size=9.5, fill=GRAY, style="italic")

# stacked rect behind the LLM box hints at N replicated workers
rect(1121, 249, 200, 90, fill="#7D3C98", stroke="#5B2C6F", sw=1.4, rx=12)
rect(1115, 255, 200, 90, fill="#7D3C98", stroke="#5B2C6F", sw=1.6, rx=12)
text(1215, 297, "Worker Agent LM", size=15, anchor="middle", weight="bold", fill="#FFFFFF")
text(1215, 317, "ReAct tool loop", size=9.5, anchor="middle", fill="#E8DAEF")

tool_chip(1215, 115, "NavigateTo", C_WTOOL)
tool_chip(1435, 160, "GetSupply", C_WTOOL)
tool_chip(1490, 305, "UseSupply", C_WTOOL)
tool_chip(1400, 455, "CarryPerson", C_WTOOL)
tool_chip(1215, 495, "DropOffPerson", C_WTOOL)
tool_chip(1030, 455, "ReportObservation", C_WTOOL, w=150)
tool_chip(960, 290, "Explore", C_WTOOL, w=130)

# LLM <-> tools
arrow([(1290, 262), (1405, 182)], color=GRAY, sw=1.2, both=True)
arrow([(1315, 305), (1423, 305)], color=GRAY, sw=1.2, both=True)
arrow([(1280, 343), (1395, 437)], color=GRAY, sw=1.2, both=True)
arrow([(1215, 345), (1215, 473)], color=GRAY, sw=1.2, both=True)
arrow([(1150, 343), (1075, 437)], color=GRAY, sw=1.2, both=True)
arrow([(1115, 290), (1027, 290)], color=GRAY, sw=1.2, both=True)
# highlighted dynamic selection
arrow([(1215, 247), (1215, 137)], color=ORANGE, sw=2.4, both=True)
text(1295, 118, "← step t: the LLM picks", size=9.5, fill=ORANGE, weight="bold")
text(1295, 132, "this tool (not hard-coded)", size=9.5, fill=ORANGE, weight="bold")

text(1225, 535, "ReAct loop: observe → reason → select → act", size=9.5, anchor="middle", fill=GRAY, style="italic")

# ================================================================ shared state in the strip
cylinder(800, 425, 80, 70, C_SM[0], C_SM[1])
text(745, 460, "Semantic Map Store", size=10, anchor="middle", weight="bold", fill=BLUE, rotate=-90)

# report_observation -> semantic map
arrow([(1030, 477), (1030, 545), (800, 545), (800, 497)], sw=1.6)
text(915, 537, "ingest obs.", size=8.5, anchor="middle", fill=GRAY, style="italic")
# semantic map -> coordinator context
arrow([(760, 470), (435, 470), (435, 347)], sw=1.6)
text(597, 486, "inject S,T,K", size=9, anchor="middle", fill=GRAY, style="italic")

# ================================================================ inter-agent channels
# dispatch: coordinator -> worker LLM (collinear with the LLM<->DispatchSubtask link)
arrow([(665, 320), (1113, 320)], sw=2)
text(800, 336, "dispatch s_i,t (A2A)", size=9.5, anchor="middle", fill=NEUT)
# cancel: coordinator -> worker LLM (dashed, single lane above the semantic map)
arrow([(600, 420), (600, 400), (1160, 400), (1160, 347)], color=GRAY, sw=1.4, dash="5 4")
text(930, 390, "cancel", size=8.5, anchor="middle", fill=GRAY, style="italic")

# ================================================================ bottom band
# note box: agent vs workflow
rect(30, 600, 560, 155, fill="#FFFFFF", stroke=NEUT, sw=1.3, rx=10)
text(48, 628, "Agent loop ≠ static workflow", size=12.5, weight="bold", fill=INK)
note = [
    "• The LLM selects the next tool every turn —",
    "   no hard-coded action chain.",
    "• Tool results re-enter the LLM context,",
    "   closing the observe–act loop.",
    "• Tools read & write shared state: mission plan",
    "   (K_t), semantic map (S_t), team status (T_t).",
]
for i, ln in enumerate(note):
    text(48, 654 + i * 16, ln, size=10)

# environment (aligned with the note box: same top/bottom)
rect(640, 600, 880, 155, fill=C_ENV[0], stroke=C_ENV[1], sw=1.5, rx=10)
text(1080, 640, "SAR Environment", size=13, anchor="middle", weight="bold", fill=INK)
mini_grid(700, 655, 6, 4, 13,
          paints={(1, 1): "#F5B041", (2, 1): "#E74C3C", (0, 3): "#2E86C1", (5, 0): "#212121"},
          agents={(3, 2): "#F06292"}, persons={(4, 0): "#8E44AD"})
text(805, 682, "GridEngine · SARBarrier", size=10.5, fill=INK)
text(805, 702, "lock-step turns · fire spreads · rescue & transport", size=9.5, fill=NEUT)

# worker <-> environment (act / observe)
arrow([(1300, 562), (1300, 598)], sw=2, both=True)
text(1312, 574, "a_i,t ↓ · o_i,t ↑", size=9.5, fill=NEUT)
text(1312, 589, "(1 action / agent / step)", size=8.5, fill=GRAY, style="italic")

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
