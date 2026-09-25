"""The same architecture as before -- no structural change -- redrawn in
the control panel's own palette. Green marks the one component that holds
the Mezmo credential and enforces the scope; purple is our own services;
grey is everything outside the stack."""
import sys

sys.path.insert(0, ".")
from draw import (ACCENT, ACCENT_DIM, BORDER, GREEN, GREEN_DIM, MUTED, PANEL,
                  TEXT, arrow, box, canvas, centered, font, node, save_jpeg,
                  text_w)

W, H = 1600, 1000
img, d = canvas(W, H)

f_title = font("bold", 30)
f_sub = font("sans", 18)
f_node = font("bold", 22)
f_nsub = font("mono", 16)
f_small = font("mono", 15)
f_leg = font("mono", 16)

d.text((60, 44), "aura-mezmo", font=f_title, fill=TEXT)
d.text((60, 84), "software architecture", font=f_sub, fill=MUTED)

# ---- tier 1: browser ---------------------------------------------------
BW, BH = 300, 64
bx = (W - BW) // 2
node(d, bx, 150, BW, BH, "Browser", None, accent=BORDER, bg=PANEL,
     tf=f_node, sf=f_nsub)
arrow(d, W // 2, 214, W // 2, 246)

# ---- tier 2: web -------------------------------------------------------
node(d, (W - 380) // 2, 252, 380, 86, "web", "control panel  :8000",
     accent=ACCENT, bg=ACCENT_DIM, tf=f_node, sf=f_nsub)
arrow(d, W // 2, 338, W // 2, 370)

# ---- tier 3: aura, with config left and the LLM right ------------------
AY, AH = 376, 86
node(d, (W - 440) // 2, AY, 440, AH, "aura", "the agent  ·  LLM loop",
     accent=ACCENT, bg=ACCENT_DIM, tf=f_node, sf=f_nsub)

node(d, 120, AY, 330, AH, "prompt + skills", "aura.toml, SKILL.md",
     accent=BORDER, bg=PANEL, tf=f_node, sf=f_nsub)
arrow(d, 450, AY + AH // 2, 572, AY + AH // 2)

node(d, 1150, AY, 330, AH, "Claude Sonnet 4", "via OpenRouter",
     accent=BORDER, bg=PANEL, tf=f_node, sf=f_nsub)
arrow(d, 1028, AY + AH // 2, 1146, AY + AH // 2)

# ---- bus down to the four tool servers ---------------------------------
BUS_Y = 506
arrow(d, W // 2, AY + AH, W // 2, BUS_Y - 2, head=0)
SERV = [
    ("mezmo-proxy", "live logs  ·  gated", GREEN, GREEN_DIM),
    ("logs-mcp", "uploaded files", ACCENT, ACCENT_DIM),
    ("freshdesk-mcp", "bug tickets", ACCENT, ACCENT_DIM),
    ("fleet-status", "robot online?", ACCENT, ACCENT_DIM),
]
SW, SH, GAP = 320, 92, 40
total = len(SERV) * SW + (len(SERV) - 1) * GAP
sx0 = (W - total) // 2
centers = [sx0 + i * (SW + GAP) + SW // 2 for i in range(len(SERV))]
d.line((centers[0], BUS_Y, centers[-1], BUS_Y), fill=BORDER, width=2)
SY = 556
for cx in centers:
    arrow(d, cx, BUS_Y, cx, SY - 10)
for i, (t, s, acc, bg) in enumerate(SERV):
    node(d, sx0 + i * (SW + GAP), SY, SW, SH, t, s, accent=acc, bg=bg,
         tf=f_node, sf=f_nsub)

# ---- tier 5: what they reach outside the stack -------------------------
EY = 714
EXT = {0: "mcp.mezmo.com", 2: "Freshdesk API", 3: "ROC dashboard"}
for i, label in EXT.items():
    cx = centers[i]
    arrow(d, cx, SY + SH, cx, EY - 10)
    node(d, sx0 + i * (SW + GAP), EY, SW, 62, label, None,
         accent=BORDER, bg=PANEL, tf=font("bold", 19), sf=f_nsub)

# logs-mcp has no external box; say why rather than leave a gap
centered(d, "local volume", f_small, centers[1], EY + 22, MUTED)

# ---- legend ------------------------------------------------------------
LY = 836
box(d, (120, LY, W - 120, LY + 104), fill=PANEL, outline=BORDER, width=2)
d.rounded_rectangle((120, LY, 125, LY + 104), radius=2, fill=GREEN)

sw = 16
d.rectangle((152, LY + 26, 152 + sw, LY + 26 + sw), fill=GREEN_DIM,
            outline=GREEN, width=2)
d.text((152 + sw + 12, LY + 24),
       "holds MEZMO_API_KEY  ·  enforces production-only, read-only, filtered",
       font=f_leg, fill=TEXT)

d.rectangle((152, LY + 60, 152 + sw, LY + 60 + sw), fill=ACCENT_DIM,
            outline=ACCENT, width=2)
d.text((152 + sw + 12, LY + 58),
       "our containers  ·  MEZMO_API_KEY deliberately blanked in all of them",
       font=f_leg, fill=MUTED)

save_jpeg(img, sys.argv[1])
print("wrote", sys.argv[1], img.size)
