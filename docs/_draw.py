"""Shared drawing helpers using the Aura control-panel's own dark palette
(the values in web/public/index.html's :root block) so the diagrams look
like the app rather than like generic clip art."""
from PIL import Image, ImageDraw, ImageFont

# --- palette, lifted verbatim from index.html ---------------------------
BG = "#0b0c0f"
PANEL = "#15171c"
BORDER = "#262930"
TEXT = "#eef0f2"
MUTED = "#8b909c"
ACCENT = "#98a4ff"
ACCENT_DIM = "#262c52"
DANGER = "#ff6b6b"
GREEN = "#6fef3a"
GREEN_DIM = "#1c3312"

_WIN = "C:/Windows/Fonts/"


def font(kind: str, size: int):
    """kind: mono | sans | bold. Falls back to PIL's default if a Windows
    font is missing, so this never hard-fails on another machine."""
    names = {
        "mono": ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"],
        "sans": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
        "bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
        "monob": ["consolab.ttf", "courbd.ttf", "DejaVuSansMono-Bold.ttf"],
    }[kind]
    for n in names:
        try:
            return ImageFont.truetype(_WIN + n, size)
        except OSError:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                continue
    return ImageFont.load_default()


def canvas(w: int, h: int):
    img = Image.new("RGB", (w, h), BG)
    return img, ImageDraw.Draw(img)


def box(d, xy, fill=PANEL, outline=BORDER, width=2, radius=10):
    d.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline,
                        width=width)


def text_w(d, s, f):
    return d.textlength(s, font=f)


def centered(d, s, f, cx, y, fill=TEXT):
    d.text((cx - text_w(d, s, f) / 2, y), s, font=f, fill=fill)


def node(d, x, y, w, h, title, sub=None, accent=ACCENT, bg=PANEL,
         tf=None, sf=None):
    """A box with a title and optional subtitle, left border accent."""
    box(d, (x, y, x + w, y + h), fill=bg, outline=accent, width=2)
    # accent spine on the left edge, same idea as the answer block in the UI
    d.rounded_rectangle((x, y, x + 5, y + h), radius=2, fill=accent)
    cx = x + w / 2
    if sub:
        centered(d, title, tf, cx, y + h / 2 - 27, TEXT)
        centered(d, sub, sf, cx, y + h / 2 + 5, MUTED)
    else:
        centered(d, title, tf, cx, y + h / 2 - 11, TEXT)


def arrow(d, x1, y1, x2, y2, color=BORDER, width=2, head=7):
    d.line((x1, y1, x2, y2), fill=color, width=width)
    if y2 != y1 and x1 == x2:                       # vertical
        dy = 1 if y2 > y1 else -1
        d.polygon([(x2, y2), (x2 - head, y2 - head * dy),
                   (x2 + head, y2 - head * dy)], fill=color)
    elif y1 == y2:                                  # horizontal
        dx = 1 if x2 > x1 else -1
        d.polygon([(x2, y2), (x2 - head * dx, y2 - head),
                   (x2 - head * dx, y2 + head)], fill=color)


def save_jpeg(img, path, quality=94):
    img.convert("RGB").save(path, "JPEG", quality=quality, subsampling=0)
    return path
