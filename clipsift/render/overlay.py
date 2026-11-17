"""Draw captions, hook text and the branding panel as transparent PNGs."""
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

from ..config import FONTS_DIR

_font_cache: dict = {}


def font(size: int, bold: str = "Bold"):
    key = (size, bold)
    if key not in _font_cache:
        path = FONTS_DIR / f"Poppins-{bold}.ttf"
        try:
            _font_cache[key] = ImageFont.truetype(str(path), size)
        except OSError:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def wrap(draw, text: str, fnt, max_w: int, max_lines: int = 2) -> list[str] | None:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines or any(draw.textlength(ln, font=fnt) > max_w for ln in lines):
        return None
    return lines


def fit_text(draw, text: str, size: int, max_w: int, max_lines: int, bold: str = "Bold"):
    s = size
    while s >= max(12, size // 2):
        f = font(s, bold)
        lines = wrap(draw, text, f, max_w, max_lines)
        if lines:
            return f, lines
        s -= max(1, size // 16)
    f = font(max(12, size // 2), bold)
    return f, wrap(draw, text, f, max_w, 99) or [text]


def draw_caption(img: Image.Image, text: str, y_center: float, *, highlight_word: int | None = None,
                 color="#FFFFFF", hl_color="#FFD84D") -> None:
    W, H = img.size
    d = ImageDraw.Draw(img)
    size = round(W * 0.064)
    f, lines = fit_text(d, text, size, int(W * 0.86), 2)
    stroke = max(2, round(W * 0.0065))
    lh = int(f.size * 1.18)
    y = int(y_center - lh * len(lines) / 2)
    word_i = 0
    for ln in lines:
        lw = d.textlength(ln, font=f)
        x = (W - lw) / 2
        if highlight_word is None:
            d.text((x, y), ln, font=f, fill=color, stroke_width=stroke, stroke_fill="#000000")
        else:
            for w in ln.split(" "):
                fill = hl_color if word_i == highlight_word else color
                d.text((x, y), w, font=f, fill=fill, stroke_width=stroke, stroke_fill="#000000")
                x += d.textlength(w + " ", font=f)
                word_i += 1
        y += lh


def draw_hook(img: Image.Image, text: str, y_center: float) -> None:
    W, _H = img.size
    d = ImageDraw.Draw(img)
    f, lines = fit_text(d, text, round(W * 0.058), int(W * 0.84), 2, bold="ExtraBold")
    pad_x, pad_y = round(W * 0.03), round(W * 0.016)
    lh = int(f.size * 1.2)
    widths = [d.textlength(ln, font=f) for ln in lines]
    box_w = max(widths) + 2 * pad_x
    box_h = lh * len(lines) + 2 * pad_y
    x0 = (W - box_w) / 2
    y0 = y_center - box_h / 2
    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=round(W * 0.02), fill=(0, 0, 0, 175))
    y = y0 + pad_y
    for ln, lw in zip(lines, widths, strict=True):
        d.text(((W - lw) / 2, y), ln, font=f, fill="#FFFFFF")
        y += lh


def base_canvas(size: tuple[int, int], brand: dict | None, brand_h: int, out: Path) -> Path:
    """Background plus the bottom branding panel (horizontal layouts only)."""
    W, H = size
    img = Image.new("RGB", (W, H), "#0B0C0F")
    if brand and brand_h > 0:
        d = ImageDraw.Draw(img)
        bg = _color(brand.get("color"), "#111318")
        d.rectangle([0, H - brand_h, W, H], fill=bg)
        logo_w = 0
        logo = brand.get("logo")
        if logo and Path(logo).is_file():
            try:
                lg = Image.open(logo).convert("RGBA")
                lh = int(brand_h * 0.7)
                lg = lg.resize((max(1, int(lg.width * lh / lg.height)), lh))
                logo_w = lg.width + int(W * 0.02)
                text_w = 0
                if brand.get("text"):
                    f = font(int(brand_h * 0.38), "ExtraBold")
                    text_w = d.textlength(brand["text"], font=f)
                x = int((W - (logo_w + text_w)) / 2)
                img.paste(lg, (x, H - brand_h + (brand_h - lh) // 2), lg)
            except OSError:
                logo_w = 0
        text = brand.get("text") or ""
        if text:
            f = font(int(brand_h * 0.38), "ExtraBold")
            tw = d.textlength(text, font=f)
            x = (W - tw - logo_w) / 2 + logo_w
            d.text((x, H - brand_h + (brand_h - f.size * 1.25) / 2), text, font=f,
                   fill=_color(brand.get("text_color"), "#FFFFFF"))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def _color(value, default):
    try:
        return ImageColor.getrgb(value or default)
    except ValueError:
        return ImageColor.getrgb(default)


def text_timeline(size, lines: list[dict], hook: str, hook_y: float, cap_y: float, duration: float, out_dir: Path,
                  *, hook_seconds: float = 0.0, highlight: bool = False) -> Path:
    """Write PNG states plus an ffconcat list. Returns the list path."""
    W, H = size
    out_dir.mkdir(parents=True, exist_ok=True)
    hook_end = duration if not hook_seconds else min(duration, hook_seconds)
    marks = {0.0, duration}
    if hook:
        marks.add(hook_end)
    spans = []
    for i, ln in enumerate(lines):
        s, e = max(0.0, ln["start"]), min(duration, ln["end"])
        if e - s < 0.02:
            continue
        if highlight and ln.get("word_times"):
            wt = ln["word_times"]
            for j, w in enumerate(wt):
                ws = max(s, w["start"])
                we = min(e, wt[j + 1]["start"]) if j + 1 < len(wt) else e
                if we - ws >= 0.02:
                    spans.append((ws, we, i, j))
        else:
            spans.append((s, e, i, None))
    for s, e, _i, _j in spans:
        marks.update((s, e))
    pts = sorted(m for m in marks if 0 <= m <= duration)
    entries = []
    cache: dict = {}
    for a, b in pairwise(pts):
        if b - a < 1e-3:
            continue
        mid = (a + b) / 2
        active = next(((i, j) for s, e, i, j in spans if s <= mid < e), None)
        show_hook = bool(hook) and mid < hook_end
        key = (show_hook, active)
        if key not in cache:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            if show_hook:
                draw_hook(img, hook, hook_y * H)
            if active is not None:
                i, j = active
                draw_caption(img, lines[i]["display"], cap_y * H, highlight_word=j)
            name = f"t{len(cache):04d}.png"
            img.save(out_dir / name, compress_level=1)
            cache[key] = name
        if entries and entries[-1][0] == cache[key]:
            entries[-1][1] += b - a
        else:
            entries.append([cache[key], b - a])
    if not entries:
        Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(out_dir / "t0000.png")
        entries = [["t0000.png", duration]]
    lst = out_dir / "timeline.ffconcat"
    body = ["ffconcat version 1.0"]
    for name, dur in entries:
        body += [f"file '{name}'", f"duration {dur:.4f}"]
    body.append(f"file '{entries[-1][0]}'")  # concat ignores the last duration without this
    lst.write_text("\n".join(body) + "\n", encoding="utf-8")
    return lst
