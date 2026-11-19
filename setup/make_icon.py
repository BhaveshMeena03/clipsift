"""Draws the ClipSift logo (a blue C around an orange play button) and writes clipsift/web/clipsift.ico.

The .ico is committed, so this only needs to run when the logo changes:
    .venv\\Scripts\\python setup\\make_icon.py
"""
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "clipsift" / "web" / "clipsift.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)  # what Windows asks for at 100% to 300% scaling
S = 1024  # drawn large, then scaled down for each size

# colors from clipsift/web/style.css
TILE = ((28, 33, 41), (14, 16, 19))  # --panel2 to --bg
BLUE = ((79, 179, 255), (45, 127, 214))  # --accent to --accent2
ORANGE = (255, 179, 71)  # --skill


def gradient(top: tuple, bottom: tuple) -> Image.Image:
    y, x = np.mgrid[0:S, 0:S].astype(np.float32)
    t = (x / S * 0.4 + y / S * 0.6)[..., None]
    arr = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    return Image.fromarray(arr.round().clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")


def draw_logo() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    m = round(S * 0.03)
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle([m, m, S - m, S - m], radius=round(S * 0.22), fill=255)
    img.paste(gradient(*TILE), (0, 0), tile)

    # the C: a thick arc with round ends
    c = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(c)
    cx = cy = S / 2
    r, width = S * 0.33, round(S * 0.13)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=48, end=312, fill=255, width=width)
    for deg in (48, 312):
        mid = r - width / 2
        px, py = cx + mid * math.cos(math.radians(deg)), cy + mid * math.sin(math.radians(deg))
        d.ellipse([px - width / 2, py - width / 2, px + width / 2, py + width / 2], fill=255)

    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.putalpha(c.point(lambda v: v * 120 // 255).filter(ImageFilter.GaussianBlur(24)))
    img.alpha_composite(shadow, (0, 14))
    img.paste(gradient(*BLUE), (0, 0), c)

    pr, px = S * 0.13, cx + S * 0.03
    ImageDraw.Draw(img).polygon([(px - pr * 0.5, cy - pr * 0.866), (px - pr * 0.5, cy + pr * 0.866), (px + pr, cy)],
                                fill=ORANGE)
    return img


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    big = draw_logo()
    frames = [big.resize((s, s), Image.Resampling.LANCZOS) for s in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
