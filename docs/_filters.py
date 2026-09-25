"""Every hard filter mezmo-proxy injects, drawn in the app's palette.

Content is taken from the constants in services/mezmo-proxy/server.py so
the picture cannot drift from the code.
"""
import re
import sys

sys.path.insert(0, ".")
from draw import (ACCENT, ACCENT_DIM, BORDER, DANGER, GREEN, GREEN_DIM,
                  MUTED, PANEL, TEXT, box, canvas, font, save_jpeg, text_w)

SRC = sys.argv[1]
OUT = sys.argv[2]
src = open(SRC, encoding="utf-8").read()


def tuple_of(name):
    m = re.search(rf"^{name}\s*=\s*\((.*?)\)", src, re.S | re.M)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


GATED = tuple_of("LEVEL_GATED_APPS")
KEEP = tuple_of("KEEP_LEVELS")
FULL = tuple_of("FULLY_EXCLUDED_APPS")
m = re.search(r"^APP_LEVEL_EXCLUSIONS\s*=\s*\{(.*?)\}", src, re.S | re.M)
APP_LEVEL = re.findall(r'"([^"]+)":\s*\("([^"]+)"', m.group(1)) if m else []

f_h1 = font("bold", 34)
f_h2 = font("sans", 19)
f_num = font("monob", 30)
f_title = font("bold", 23)
f_body = font("sans", 18)
f_mono = font("mono", 16)
f_note = font("mono", 15)

W = 1600
PADX = 70
CW = W - 2 * PADX


def wrap(d, s, f, maxw):
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if text_w(d, t, f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


CARDS = [
    ("1", "Production robots only", GREEN,
     "host:gen1-prod          host:gen1-prod2  ->  host:==gen1-prod2",
     "Scoped to production, and a bare robot name is rewritten to an exact "
     "match so it cannot silently sweep in its siblings -- prod2 would "
     "otherwise also return prod20-prod29. Asking for gen1-dev*, "
     "gen1-proto* or any non-production host returns an explicit "
     "out-of-scope error rather than empty results."),

    ("2", "DEBUG dropped, for every app", GREEN,
     "-level:debug",
     "About 80% of all fleet volume. Applied globally rather than per app."),

    ("3", f"{', '.join(FULL)} removed entirely", GREEN,
     " ".join(f'-app:"{a}"' for a in FULL),
     "2,839,922 lines/day. Foxglove / ROS diagnostics and LIDAR drivers -- "
     "a bridging layer, not robot behaviour. Its 466 errors and 6 fatals a "
     "day go too, which is the deliberate difference from rule 5."),

    ("4", "INFO dropped for three high-rate loops", GREEN,
     "  ".join(f'-(app:{a} level:{lv})' for a, lv in APP_LEVEL),
     "Loop telemetry, not events. Consequence: fastloop is nearly absent "
     "from the agent's view, and the prompt tells it to say so rather than "
     "conclude fastloop was idle."),

    ("5", f"{len(GATED)} housekeeping apps gated to problems only", GREEN,
     ", ".join(GATED),
     f"Kept only at {', '.join(KEEP)}. Note audit, kernel, kern.log and "
     "auth.log carry no level at all, so in practice they are removed "
     "entirely -- about 2.83M lines/day."),

    ("6", "One known-benign error suppressed", GREEN,
     '-(app:"user@1000.service" "temperature-probe" "Is sensor connected")',
     "The TEMPerX232 USB probe is deliberately not fitted, so the reader "
     "logs ENOENT on every poll. 3,133 lines/day; expected, not a fault."),

    ("7", "Read-only tool allowlist", GREEN,
     "32 tools upstream  ->  11 served   (8 scoped + 2 time-only + describe_scope)",
     "The ~20 pipeline mutators are not refused, they are absent from the "
     "list the agent sees. A tool marked scoped that turns out to have no "
     "query parameter is rejected at startup, never registered unscoped."),
]

# --- measure -----------------------------------------------------------
_, dm = canvas(10, 10)
heights = []
for _, _, _, mono, note in CARDS:
    ml = wrap(dm, mono, f_mono, CW - 150)
    nl = wrap(dm, note, f_body, CW - 150)
    heights.append(46 + len(ml) * 24 + 10 + len(nl) * 26 + 26)

H = 250 + sum(h + 22 for h in heights) + 190
img, d = canvas(W, H)

d.text((PADX, 52), "mezmo-proxy", font=f_h1, fill=TEXT)
d.text((PADX, 96), "hard filters, injected into every query before it leaves "
       "the network", font=f_h2, fill=MUTED)
d.text((PADX, 132), "enforced in Python -- neither the system prompt nor the "
       "agent can relax them", font=f_h2, fill=GREEN)

# scope-clause strip
box(d, (PADX, 176, W - PADX, 226), fill=PANEL, outline=BORDER, width=2)
d.text((PADX + 18, 192),
       "caller's query  ->  ( caller's query )  AND  scope clause below",
       font=f_mono, fill=MUTED)

y = 254
for (num, title, acc, mono, note), ch in zip(CARDS, heights):
    box(d, (PADX, y, W - PADX, y + ch), fill=PANEL, outline=BORDER, width=2)
    d.rounded_rectangle((PADX, y, PADX + 5, y + ch), radius=2, fill=acc)
    d.text((PADX + 30, y + 18), num, font=f_num, fill=acc)
    d.text((PADX + 90, y + 20), title, font=f_title, fill=TEXT)
    ty = y + 56
    for line in wrap(d, mono, f_mono, CW - 150):
        d.text((PADX + 90, ty), line, font=f_mono, fill=ACCENT)
        ty += 24
    ty += 10
    for line in wrap(d, note, f_body, CW - 150):
        d.text((PADX + 90, ty), line, font=f_body, fill=MUTED)
        ty += 26
    y += ch + 22

# --- footer -------------------------------------------------------------
box(d, (PADX, y + 8, W - PADX, y + 116), fill=GREEN_DIM, outline=GREEN,
    width=2)
d.text((PADX + 30, y + 28), "Net effect on one measured fleet day",
       font=f_title, fill=TEXT)
d.text((PADX + 30, y + 66),
       "120,767,854 raw  ->  3,256,497 reaching the agent   (2.7%)"
       "        Mezmo rejects any query matching over 1,000,000 lines",
       font=f_mono, fill=TEXT)

d.text((PADX, y + 140),
       "Not filtered, deliberately: dill-user (operator bug reports). It is "
       "a FATAL-level false lead, handled in the prompt instead so that "
       '"show me the bug reports" still works.',
       font=f_note, fill=MUTED)

save_jpeg(img, OUT)
print("wrote", OUT, img.size)
