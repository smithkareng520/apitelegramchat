#!/usr/bin/env python3
"""minichart -- zero-dependency (stdlib only) chart-to-PNG generator.

No matplotlib / PIL required. Draws simple charts (bar, hbar, line, donut)
directly into a raw pixel buffer and encodes PNG with zlib + struct.
Labels use a built-in 5x7 bitmap font, so use ENGLISH labels (no CJK glyphs).

Library use:
    import minichart as mc
    mc.bar_chart("out.png", ["A","B","C"], [1,2,3], title="T", ylabel="V")

CLI use:
    python3 minichart.py bar out.png --labels A,B,C --values 1,2,3 --title "T"
"""
import struct, zlib, sys, math

def new_canvas(w, h, bg=(255, 255, 255)):
    return [[bg for _ in range(w)] for _ in range(h)]

def save_png(canvas, w, h, path):
    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        c += struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        return c
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            r, g, b = canvas[y][x]
            raw += bytes((max(0,min(255,r)), max(0,min(255,g)), max(0,min(255,b))))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

def _px(c, x, y, col):
    if 0 <= y < len(c) and 0 <= x < len(c[y]):
        c[y][x] = col

def fill_rect(c, x0, y0, x1, y1, col):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            _px(c, x, y, col)

def hline(c, x0, x1, y, col, thick=1):
    for t in range(thick):
        for x in range(x0, x1 + 1):
            _px(c, x, y + t, col)

def vline(c, x, y0, y1, col, thick=1):
    for t in range(thick):
        for y in range(y0, y1 + 1):
            _px(c, x + t, y, col)

def line(c, x0, y0, x1, y1, col, thick=1):
    if thick <= 1:
        dx = x1 - x0; dy = y1 - y0
        steps = max(abs(dx), abs(dy), 1)
        for i in range(steps + 1):
            _px(c, int(x0 + dx * i / steps), int(y0 + dy * i / steps), col)
    else:
        dx = x1 - x0; dy = y1 - y0
        steps = max(abs(dx), abs(dy), 1)
        for i in range(steps + 1):
            cx = int(x0 + dx * i / steps); cy = int(y0 + dy * i / steps)
            for a in range(thick):
                for b in range(thick):
                    _px(c, cx + a, cy + b, col)

def rect(c, x0, y0, x1, y1, col, thick=1):
    hline(c, x0, x1, y0, col, thick); hline(c, x0, x1, y1, col, thick)
    vline(c, x0, y0, y1, col, thick); vline(c, x1, y0, y1, col, thick)

# ---------------------------------------------------------------------------
# 5x7 bitmap font (English + digits + basic symbols). 7 rows x 5 bits.
# ---------------------------------------------------------------------------
_FONT = {
 "A":[14,17,17,31,17,17,17], "B":[30,17,17,30,17,17,30], "C":[14,17,16,16,16,17,14],
 "D":[30,17,17,17,17,17,30], "E":[31,16,16,30,16,16,31], "F":[31,16,16,30,16,16,16],
 "G":[14,17,16,23,17,17,31], "H":[17,17,17,31,17,17,17], "I":[31,4,4,4,4,4,31],
 "J":[7,2,2,2,2,18,12], "K":[17,18,20,24,20,18,17], "L":[16,16,16,16,16,16,31],
 "M":[17,27,21,21,17,17,17], "N":[17,25,21,19,17,17,17], "O":[14,17,17,17,17,17,14],
 "P":[30,17,17,30,16,16,16], "Q":[14,17,17,17,21,18,13], "R":[30,17,17,30,20,18,17],
 "S":[15,17,16,14,1,17,30], "T":[31,4,4,4,4,4,4], "U":[17,17,17,17,17,17,14],
 "V":[17,17,17,17,17,10,4], "W":[17,17,17,21,21,27,17], "X":[17,17,10,4,10,17,17],
 "Y":[17,17,10,4,4,4,4], "Z":[31,1,2,4,8,16,31],
 "0":[14,17,19,21,25,17,14], "1":[4,12,4,4,4,4,14], "2":[14,17,1,2,4,8,31],
 "3":[14,17,1,6,1,17,14], "4":[2,6,10,18,31,2,2], "5":[31,16,30,1,1,17,14],
 "6":[14,16,16,30,17,17,14], "7":[31,1,2,4,8,8,8], "8":[14,17,17,14,17,17,14],
 "9":[14,17,17,15,1,1,14],
 " ":[0,0,0,0,0,0,0], ".":[0,0,0,0,4,4,4], ",":[0,0,0,4,4,8,0],
 ":":[0,4,4,0,4,4,0], "%":[16,9,2,4,8,25,2],
 "(":
 [0,0,2,4,4,2,0], ")":[0,0,4,2,2,4,0], "-":[0,0,0,31,0,0,0], "/":[1,2,4,8,16,0,0],
 "+":[0,0,4,4,31,4,4], "$":[4,31,18,14,4,31,4], "=":[0,0,31,0,31,0,0],
 "'":[4,4,0,0,0,0,0], "!":[4,4,4,4,4,0,4], "?":[14,17,1,2,4,0,4],
}

def draw_text(c, x, y, s, col=(40, 40, 40), scale=1):
    cx = x
    s = str(s)
    for ch in s:
        g = _FONT.get(ch.upper())
        for row in range(7):
            bits = g[row] if g else 0
            for colbit in range(5):
                if (bits >> (4 - colbit)) & 1:
                    fill_rect(c, cx + colbit * scale, y + row * scale,
                              cx + colbit * scale + scale - 1, y + row * scale + scale - 1, col)
        cx += 6 * scale

def text_width(s, scale=1):
    return len(str(s)) * 6 * scale - scale

# ---------------------------------------------------------------------------
# High-level charts
# ---------------------------------------------------------------------------
DEFAULTS = dict(width=1040, height=680, title_color=(21, 34, 56),
                text=(40, 40, 40), grid=(214, 222, 232),
                bar=(59, 130, 196), hi=(192, 80, 77), axis=(120, 134, 150))

def _hex(v):
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)) if isinstance(v, str) else tuple(v)

def _num(v):
    s = f"{v:g}" if isinstance(v, float) else str(v)
    return s

def bar_chart(path, labels, values, title="", xlabel="", ylabel="", colors=None, width=None, height=None, scale=2):
    W = width or DEFAULTS["width"]; H = height or DEFAULTS["height"]
    c = new_canvas(W, H)
    D = DEFAULTS
    mx = max(max(values), 0) or 1
    padL, padR, padT, padB = 90*scale, 30*scale, 70*scale, 90*scale
    plotW = W - padL - padR; plotH = H - padT - padB
    hline(c, 0, W-1, 24*scale, D["title_color"], 2)
    draw_text(c, padL, 34*scale, title.upper(), D["title_color"], scale)
    # y gridlines + labels
    for i in range(5):
        gv = mx * i / 4
        gy = padT + plotH - int(plotH * i / 4)
        hline(c, padL, padL + plotW, gy, D["grid"], 1)
        draw_text(c, padL - 50*scale, gy - 7*scale, _num(round(gv, 2)), D["text"], scale-1)
    vline(c, padL, padT, padT + plotH, D["axis"], 1)
    n = len(values)
    gap = plotW * 0.18 / (n + 1)
    bw = (plotW - gap * (n + 1)) / n
    for i, (lb, v) in enumerate(zip(labels, values)):
        x0 = int(padL + gap + i * (bw + gap)); x1 = int(x0 + bw)
        hgt = int(plotH * (v / mx))
        y1 = padT + plotH; y0 = y1 - hgt
        col = D["hi"] if (colors and i < len(colors) and colors[i] is not None and colors[i]) else D["bar"]
        fill_rect(c, x0, y0, x1, y1, col)
        draw_text(c, (x0 + x1) // 2 - text_width(_num(v), scale) // 2, y0 - 16*scale, _num(v), D["text"], scale)
        tl = lb.upper()
        tx = max(padL, (x0 + x1) // 2 - text_width(tl, scale-1) // 2)
        draw_text(c, tx, y1 + 10*scale, tl, D["text"], scale-1)
    if ylabel:
        for k, ch in enumerate(ylabel.upper()):
            _px(c, 26*scale, 60*scale + k * 12*scale, D["text"])
    save_png(c, W, H, path); return path

def hbar_chart(path, labels, values, title="", colors=None, width=None, height=None, scale=2):
    W = width or DEFAULTS["width"]; H = height or DEFAULTS["height"]
    c = new_canvas(W, H); D = DEFAULTS
    mx = max(max(values), 0) or 1
    padL, padR, padT, padB = 150*scale, 40*scale, 70*scale, 30*scale
    plotW = W - padL - padR; rowH = (H - padT - padB) / len(values)
    hline(c, 0, W-1, 24*scale, D["title_color"], 2)
    draw_text(c, padL, 34*scale, title.upper(), D["title_color"], scale)
    for i, (lb, v) in enumerate(zip(labels, values)):
        cy = int(padT + i * rowH + rowH / 2)
        hgt = int(rowH * 0.55)
        bl = int(plotW * (v / mx))
        col = D["hi"] if (colors and i < len(colors) and colors[i] is not None and colors[i]) else D["bar"]
        fill_rect(c, padL, cy - hgt // 2, padL + bl, cy + hgt // 2, col)
        tl = lb.upper()
        tx = padL - 10*scale - text_width(tl, scale-1)
        draw_text(c, max(0, tx), cy - 7*(scale-1)*3//2, tl, D["text"], scale-1)
        draw_text(c, padL + bl + 8*scale, cy - 7*(scale-1)*3//2, _num(v), D["text"], scale-1)
    vline(c, padL, padT, padT + int(rowH*len(values)), D["axis"], 1)
    save_png(c, W, H, path); return path

def line_chart(path, labels, values, title="", ylabel="", width=None, height=None, scale=2):
    W = width or DEFAULTS["width"]; H = height or DEFAULTS["height"]
    c = new_canvas(W, H); D = DEFAULTS
    lo = min(min(values), 0); hi = max(max(values), 0)
    if hi == lo: hi = lo + 1
    padL, padR, padT, padB = 90*scale, 30*scale, 70*scale, 90*scale
    plotW = W - padL - padR; plotH = H - padT - padB
    hline(c, 0, W-1, 24*scale, D["title_color"], 2)
    draw_text(c, padL, 34*scale, title.upper(), D["title_color"], scale)
    for i in range(5):
        gv = lo + (hi - lo) * i / 4
        gy = padT + plotH - int(plotH * i / 4)
        hline(c, padL, padL + plotW, gy, D["grid"], 1)
        draw_text(c, padL - 55*scale, gy - 7*scale, _num(round(gv, 2)), D["text"], scale-1)
    n = len(values); xs = [int(padL + plotW * i / (n - 1)) if n > 1 else W//2 for i in range(n)]
    ys = [int(padT + plotH - plotH * ((v - lo) / (hi - lo))) for v in values]
    for i in range(n - 1):
        line(c, xs[i], ys[i], xs[i+1], ys[i+1], D["bar"], 2)
        fill_rect(c, xs[i]-4, ys[i]-4, xs[i]+4, ys[i]+4, D["bar"])
    for i, lb in enumerate(labels):
        tx = max(padL, xs[i] - text_width(lb.upper(), scale-1)//2)
        draw_text(c, tx, padT + plotH + 10*scale, lb.upper(), D["text"], scale-1)
    save_png(c, W, H, path); return path

def donut_chart(path, items, title="", width=None, height=None, legend=True):
    """items: list of (label, value, color). color = hex '#RRGGBB' or None (auto)."""
    W = width or 720; H = height or 520
    c = new_canvas(W, H); D = DEFAULTS
    auto = [D["bar"], D["teal"], D["amber"], D["hi"], D["axis"]] if "teal" in D else [D["bar"], (79,179,169), (230,165,54), D["hi"], D["axis"]]
    total = sum(v for _, v, _ in items) or 1
    cx = W // 2 - (60 if legend else 0); cy = H // 2 + 10
    R = min(cx, cy - 40) - 6; r = R * 0.55
    # segments
    import math as _m
    ang = 0; segs = []
    for i, (lb, v, col) in enumerate(items):
        frac = v / total; a0 = ang; a1 = ang + frac * 2 * _m.pi
        segs.append((a0, a1, _hex(col) if col else auto[i % len(auto)], frac, lb))
        ang = a1
    for y in range(cy - R - 1, cy + R + 1):
        for x in range(cx - R - 1, cx + R + 1):
            dx = x - cx; dy = y - cy; d = _m.sqrt(dx * dx + dy * dy)
            if d > R or d < r:
                continue
            a = _m.atan2(dy, dx) + _m.pi  # shift so 0 = top
            a = a % (2 * _m.pi)
            for a0, a1, col, frac, lb in segs:
                s0 = (a0 + _m.pi) % (2 * _m.pi); s1 = (a1 + _m.pi) % (2 * _m.pi)
                in_seg = (a >= s0 and a < s1) if s0 <= s1 else (a >= s0 or a < s1)
                if in_seg:
                    _px(c, x, y, col); break
    hline(c, 0, W - 1, 24, D["title_color"], 2)
    draw_text(c, 40, 34, title.upper(), D["title_color"], 2)
    # center label
    cv = f"{total:g}"
    draw_text(c, cx - text_width(cv, 3) // 2, cy - 12, cv, D["title_color"], 3)
    if legend:
        ly = cy - len(segs) * 16
        lx = W - 150
        for a0, a1, col, frac, lb in segs:
            fill_rect(c, lx, ly, lx + 12, ly + 12, col)
            draw_text(c, lx + 18, ly - 1, f"{lb.upper()} {int(frac*100)}%", D["text"], 1)
            ly += 32
    save_png(c, W, H, path); return path

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _csv(vals):
    vals = vals.strip()
    try:
        return [float(x) for x in vals.split(",") if x != ""]
    except ValueError:
        return []

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Zero-dependency chart -> PNG")
    p.add_argument("kind", choices=["bar", "hbar", "line", "donut"])
    p.add_argument("out")
    p.add_argument("--labels", default="")
    p.add_argument("--values", default="")
    p.add_argument("--title", default="")
    p.add_argument("--ylabel", default="")
    args = p.parse_args()
    labels = [x for x in args.labels.split(",") if x != ""] if args.labels else []
    values = _csv(args.values)
    if args.kind == "bar":
        bar_chart(args.out, labels, values, title=args.title, ylabel=args.ylabel)
    elif args.kind == "hbar":
        hbar_chart(args.out, labels, values, title=args.title)
    elif args.kind == "line":
        line_chart(args.out, labels, values, title=args.title, ylabel=args.ylabel)
    else:
        # donut: pairs label:value:color,#...
        items = []
        for part in args.values.split("#"):
            parts = part.split(":")
            lb = parts[0]; v = float(parts[1]) if len(parts) > 1 else 0.0
            col = parts[2] if len(parts) > 2 else None
            items.append((lb, v, col))
        donut_chart(args.out, items, title=args.title)
    print("WROTE", args.out)
