#!/usr/bin/env python3
"""Generate the Hachimi card portrait (668x508, the size the game uses for
packed card portraits) with a small procedural illustration.

The art is an original flat-style "singing hachimi cat" so the mod ships no
third-party artwork. Run:  python3 tools/make_card_art.py assets/hachimi_card.png
"""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter

W, H = 668, 508
SS = 3  # supersampling factor


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def grad_bg(w, h):
    top = (255, 236, 205)
    bottom = (255, 176, 138)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        c = lerp(top, bottom, y / (h - 1))
        for x in range(w):
            px[x, y] = c
    return img


def draw_note(d, cx, cy, scale, color):
    r = 14 * scale
    d.ellipse([cx - r, cy - r * 0.8, cx + r, cy + r * 0.8], fill=color)
    d.rectangle([cx + r * 0.55, cy - r * 3.0, cx + r * 0.95, cy], fill=color)
    d.polygon(
        [
            (cx + r * 0.55, cy - r * 3.0),
            (cx + r * 1.9, cy - r * 2.1),
            (cx + r * 1.9, cy - r * 1.2),
            (cx + r * 0.55, cy - r * 2.1),
        ],
        fill=color,
    )


def main(out):
    w, h = W * SS, H * SS
    img = grad_bg(w, h).convert("RGBA")

    # soft glow behind the cat
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w * 0.14, h * 0.10, w * 0.86, h * 0.98], fill=(255, 245, 220, 150))
    glow = glow.filter(ImageFilter.GaussianBlur(40 * SS))
    img = Image.alpha_composite(img, glow)

    d = ImageDraw.Draw(img, "RGBA")

    # background music notes
    for (nx, ny, s, a) in [
        (0.10, 0.16, 1.6, 60),
        (0.86, 0.22, 1.3, 55),
        (0.20, 0.78, 1.2, 50),
        (0.80, 0.80, 1.7, 55),
        (0.29, 0.30, 0.9, 40),
        (0.70, 0.10, 0.9, 40),
    ]:
        draw_note(d, w * nx, h * ny, s * SS * 0.9, (226, 118, 60, a))

    cx, cy = w * 0.5, h * 0.60

    # ears
    ear_l = [(cx - 108 * SS, cy - 78 * SS), (cx - 150 * SS, cy - 205 * SS), (cx - 34 * SS, cy - 128 * SS)]
    ear_r = [(cx + 108 * SS, cy - 78 * SS), (cx + 150 * SS, cy - 205 * SS), (cx + 34 * SS, cy - 128 * SS)]
    for ear in (ear_l, ear_r):
        d.polygon(ear, fill=(240, 158, 84, 255))
    # inner ears
    d.polygon([(cx - 100 * SS, cy - 96 * SS), (cx - 131 * SS, cy - 182 * SS), (cx - 52 * SS, cy - 128 * SS)], fill=(247, 193, 166, 255))
    d.polygon([(cx + 100 * SS, cy - 96 * SS), (cx + 131 * SS, cy - 182 * SS), (cx + 52 * SS, cy - 128 * SS)], fill=(247, 193, 166, 255))

    # head
    head = [cx - 152 * SS, cy - 135 * SS, cx + 152 * SS, cy + 135 * SS]
    d.ellipse(head, fill=(244, 162, 89, 255))

    # forehead stripes
    stripe = (222, 132, 52, 255)
    for dx in (-46, 0, 46):
        d.arc(
            [cx + dx * SS - 20 * SS, cy - 128 * SS, cx + dx * SS + 20 * SS, cy - 88 * SS],
            200,
            340,
            fill=stripe,
            width=int(9 * SS),
        )

    # muzzle
    for dx in (-40, 40):
        d.ellipse(
            [cx + dx * SS - 56 * SS, cy + 20 * SS - 44 * SS, cx + dx * SS + 56 * SS, cy + 20 * SS + 44 * SS],
            fill=(255, 246, 236, 255),
        )

    # eyes
    for dx in (-52, 52):
        ex, ey = cx + dx * SS, cy - 28 * SS
        d.ellipse([ex - 31 * SS, ey - 36 * SS, ex + 31 * SS, ey + 36 * SS], fill=(58, 38, 30, 255))
        d.ellipse([ex - 31 * SS, ey - 36 * SS, ex + 31 * SS, ey + 36 * SS], outline=(138, 82, 45, 255), width=int(3 * SS))
        d.ellipse([ex - 20 * SS, ey - 26 * SS, ex - 2 * SS, ey - 8 * SS], fill=(255, 255, 255, 235))
        d.ellipse([ex + 6 * SS, ey + 12 * SS, ex + 17 * SS, ey + 23 * SS], fill=(255, 255, 255, 150))

    # cheeks
    for dx in (-92, 92):
        d.ellipse(
            [cx + dx * SS - 26 * SS, cy + 16 * SS, cx + dx * SS + 26 * SS, cy + 46 * SS],
            fill=(246, 150, 140, 110),
        )

    # nose
    d.polygon(
        [(cx - 13 * SS, cy + 8 * SS), (cx + 13 * SS, cy + 8 * SS), (cx, cy + 22 * SS)],
        fill=(226, 109, 122, 255),
    )

    # open singing mouth
    d.ellipse([cx - 42 * SS, cy + 30 * SS, cx + 42 * SS, cy + 96 * SS], fill=(110, 44, 56, 255))
    d.ellipse([cx - 27 * SS, cy + 62 * SS, cx + 27 * SS, cy + 93 * SS], fill=(245, 143, 163, 255))

    # whiskers
    for side in (-1, 1):
        for i, yoff in enumerate((-6, 10, 26)):
            x0 = cx + side * 128 * SS
            y0 = cy + (18 + yoff) * SS
            x1 = cx + side * (196 + i * 6) * SS
            y1 = cy + (8 + yoff * 1.6) * SS
            d.line([x0, y0, x1, y1], fill=(255, 250, 242, 210), width=int(4 * SS))

    # notes floating out of the mouth
    for (nx, ny, s) in [(cx + 96 * SS, cy + 46 * SS, 1.0), (cx + 150 * SS, cy - 6 * SS, 1.3), (cx - 120 * SS, cy - 60 * SS, 0.9)]:
        draw_note(d, nx, ny, s * SS, (122, 62, 34, 220))

    img = img.convert("RGB").resize((W, H), Image.LANCZOS)
    img.save(out)
    print("wrote", out, img.size)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/art/hachimi_card.png")
