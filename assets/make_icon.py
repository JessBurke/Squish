"""Draw the Squish icon and write the icon files next to this script.

Developer tool only - Squish itself never runs this, and the finished icon
files are kept in the repository. Pillow (8.2 or newer) is only needed to
regenerate the icons:

    python -m pip install pillow
    python assets/make_icon.py

squish.svg is the hand-drawn vector master on a 256 x 256 grid. Pillow cannot
read SVG, so this script draws the same picture from the same numbers (keep the
two in step if you change one). Every size is drawn 4x larger and then scaled
down, which gives smooth edges.

128 and 256 use the full MASTER drawing; 64 drops the little side lines. The
smaller sizes are not simply shrunk: 32-48 have their own simplified,
pixel-aligned layouts (MID_LAYOUTS: no shadow, no squish curves, relatively
thicker strokes) and 16-24 are hand-placed pixel by pixel (PIXEL_ART) so the
"envelope being squeezed" still reads on the taskbar and in Explorer.

Writes: squish.png (256), squish-64.png and squish-32.png (window icon and
header) and squish.ico (16, 20, 24, 32, 40, 48, 64, 128, 256; for Windows
shortcuts and the taskbar).
"""

import io
import os
import struct

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4  # supersampling factor

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# Colours
TILE_TOP = (91, 63, 217)       # #5B3FD9
TILE_BOTTOM = (59, 42, 140)    # #3B2A8C
ENVELOPE = (255, 255, 255)
FLAP = (75, 52, 184)           # #4B34B8, the V of the envelope flap
CHEVRON = (198, 244, 94)       # #C6F45E lime
SHADOW = (24, 12, 72, 110)     # under the envelope (large sizes only)
SIDE_LINE = (255, 255, 255, 150)

# Master drawing on the 256 grid (same numbers as squish.svg). Only the top
# chevron and the left side lines are listed; the bottom chevron and the right
# side lines are mirror images.
MASTER = {
    "grid": 256,
    "tile": (8, 8, 248, 248), "tile_radius": 44,
    "envelope": (52, 96, 204, 160), "envelope_radius": 10,
    "bulge": 5, "pinch": 3,          # sides push out, top/bottom press in
    "flap": [(66, 106), (128, 138), (190, 106)], "flap_width": 9,
    "chevron": [(88, 52), (128, 78), (168, 52)], "chevron_width": 20,
    "side_lines": [[(24, 110), (37, 114)], [(22, 128), (37, 128)], [(24, 146), (37, 142)]],
    "side_line_width": 5,
    "shadow": True,
}

# Middle sizes, in real pixels. Edges sit on whole pixels so they stay sharp.
# Anything not listed (bulge, pinch, side lines, shadow) is simply left out.
MID_LAYOUTS = {
    32: {"grid": 32, "tile": (1, 1, 31, 31), "tile_radius": 5.5,
         "envelope": (5, 11, 27, 21), "envelope_radius": 1.5,
         "flap": [(7.5, 13.5), (16, 17.5), (24.5, 13.5)], "flap_width": 2,
         "chevron": [(11, 5.5), (16, 8.5), (21, 5.5)], "chevron_width": 3},
    40: {"grid": 40, "tile": (1, 1, 39, 39), "tile_radius": 7,
         "envelope": (7, 14, 33, 26), "envelope_radius": 1.75,
         "flap": [(9.5, 16.5), (20, 21.5), (30.5, 16.5)], "flap_width": 2.25,
         "chevron": [(13.5, 7), (20, 11), (26.5, 7)], "chevron_width": 3.75},
    48: {"grid": 48, "tile": (1, 1, 47, 47), "tile_radius": 8.5,
         "envelope": (8, 17, 40, 31), "envelope_radius": 2,
         "flap": [(11, 20), (24, 25.5), (37, 20)], "flap_width": 2.5,
         "chevron": [(16, 8.5), (24, 13), (32, 8.5)], "chevron_width": 4.25},
}

# The tiniest sizes are drawn pixel by pixel on top of the smooth tile.
#   .  tile (left as drawn)    W  envelope white    w  white, half blended (soft corner)
#   v  envelope flap V         L  lime chevron
PIXEL_ART = {
    16: dict(margin=0, radius=3, rows=[
        "................",
        "....LL....LL....",
        ".....LL..LL.....",
        "......LLLL......",
        "................",
        "..wWWWWWWWWWWw..",
        "..WvvWWWWWWvvW..",
        "..WWWvvWWvvWWW..",
        "..WWWWWvvWWWWW..",
        "..WWWWWWWWWWWW..",
        "..wWWWWWWWWWWw..",
        "................",
        "......LLLL......",
        ".....LL..LL.....",
        "....LL....LL....",
        "................",
    ]),
    20: dict(margin=0, radius=3.5, rows=[
        "....................",
        ".....LL......LL.....",
        "......LL....LL......",
        ".......LL..LL.......",
        "........LLLL........",
        "....................",
        "..wWWWWWWWWWWWWWWw..",
        "..WvvWWWWWWWWWWvvW..",
        "..WWWvvWWWWWWvvWWW..",
        "..WWWWWvvWWvvWWWWW..",
        "..WWWWWWWvvWWWWWWW..",
        "..WWWWWWWWWWWWWWWW..",
        "..WWWWWWWWWWWWWWWW..",
        "..wWWWWWWWWWWWWWWw..",
        "....................",
        "........LLLL........",
        ".......LL..LL.......",
        "......LL....LL......",
        ".....LL......LL.....",
        "....................",
    ]),
    24: dict(margin=1, radius=4, rows=[
        "........................",
        "........................",
        "........................",
        ".......LL......LL.......",
        "........LL....LL........",
        ".........LL..LL.........",
        "..........LLLL..........",
        "........................",
        "...wWWWWWWWWWWWWWWWWw...",
        "...WWvvWWWWWWWWWWvvWW...",
        "...WWWWvvWWWWWWvvWWWW...",
        "...WWWWWWvvWWvvWWWWWW...",
        "...WWWWWWWWvvWWWWWWWW...",
        "...WWWWWWWWWWWWWWWWWW...",
        "...WWWWWWWWWWWWWWWWWW...",
        "...wWWWWWWWWWWWWWWWWw...",
        "........................",
        "..........LLLL..........",
        ".........LL..LL.........",
        "........LL....LL........",
        ".......LL......LL.......",
        "........................",
        "........................",
        "........................",
    ]),
}


def quad_points(p0, ctrl, p1, steps=16):
    """Points along a quadratic Bezier curve (excluding the start point)."""
    pts = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t ** 2
        pts.append((a * p0[0] + b * ctrl[0] + c * p1[0], a * p0[1] + b * ctrl[1] + c * p1[1]))
    return pts


def envelope_outline(box, r, bulge, pinch):
    """Rounded rectangle whose sides bulge out and top/bottom dip in (the squish)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    pts = [(x0 + r, y0)]
    segments = [
        ((cx, y0 + 2 * pinch), (x1 - r, y0)),   # top edge, pressed down
        ((x1, y0), (x1, y0 + r)),               # top-right corner
        ((x1 + 2 * bulge, cy), (x1, y1 - r)),   # right edge, pushed out
        ((x1, y1), (x1 - r, y1)),               # bottom-right corner
        ((cx, y1 - 2 * pinch), (x0 + r, y1)),   # bottom edge, pressed up
        ((x0, y1), (x0, y1 - r)),               # bottom-left corner
        ((x0 - 2 * bulge, cy), (x0, y0 + r)),   # left edge, pushed out
        ((x0, y0), (x0 + r, y0)),               # top-left corner
    ]
    for ctrl, end in segments:
        pts.extend(quad_points(pts[-1], ctrl, end))
    return pts


def stroke(draw, points, width, colour):
    """Polyline with round joins and round ends."""
    draw.line(points, fill=colour, width=int(round(width)), joint="curve")
    r = width / 2.0
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=colour)


def draw_tile(big, box, radius):
    """Rounded square filled with the top-to-bottom gradient, on a transparent canvas."""
    x0, y0, x1, y1 = box
    gradient = Image.new("RGBA", (big, big))
    gd = ImageDraw.Draw(gradient)
    for y in range(big):
        t = min(1.0, max(0.0, (y - y0) / float(y1 - y0)))
        col = tuple(int(round(a + (b - a) * t)) for a, b in zip(TILE_TOP, TILE_BOTTOM))
        gd.line([(0, y), (big, y)], fill=col + (255,))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=radius, fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)
    return img, mask


def render(geo, size):
    """Draw one icon size from a geometry dict, 4x larger, then scale down."""
    grid = float(geo["grid"])
    big = size * SS
    k = big / grid  # geometry units -> supersampled pixels

    def sc(points):
        return [(x * k, y * k) for x, y in points]

    def mirror_x(points):
        return [(grid - x, y) for x, y in points]

    def mirror_y(points):
        return [(x, grid - y) for x, y in points]

    img, mask = draw_tile(big, [v * k for v in geo["tile"]], geo["tile_radius"] * k)
    # Pillow fills a shape's last row and column too, so pull the envelope's
    # right and bottom edges in by one supersampled pixel (as for the tile).
    ex0, ey0, ex1, ey1 = [v * k for v in geo["envelope"]]
    outline = envelope_outline((ex0, ey0, ex1 - 1, ey1 - 1), geo["envelope_radius"] * k,
                               geo.get("bulge", 0) * k, geo.get("pinch", 0) * k)

    if geo.get("shadow"):
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon([(x, y + 6 * k) for x, y in outline], fill=SHADOW)
        layer = layer.filter(ImageFilter.GaussianBlur(5 * k))
        img = Image.alpha_composite(img, layer)

    if geo.get("side_lines"):
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for line in geo["side_lines"]:
            for points in (line, mirror_x(line)):
                stroke(ld, sc(points), geo["side_line_width"] * k, SIDE_LINE)
        img = Image.alpha_composite(img, layer)

    draw = ImageDraw.Draw(img)
    draw.polygon(outline, fill=ENVELOPE)
    stroke(draw, sc(geo["flap"]), geo["flap_width"] * k, FLAP)
    for chevron in (geo["chevron"], mirror_y(geo["chevron"])):
        stroke(draw, sc(chevron), geo["chevron_width"] * k, CHEVRON)

    # Nothing may spill outside the tile; then average each 4x4 block down to one pixel.
    img.putalpha(ImageChops.darker(img.getchannel("A"), mask))
    return img.resize((size, size), Image.BOX)


def blend(base, colour, amount):
    """Mix an RGBA pixel towards an RGB colour (amount 0..1), keeping its alpha."""
    return tuple(int(round(b + (c - b) * amount)) for b, c in zip(base[:3], colour)) + (base[3],)


def render_pixel_art(size):
    """Smooth tile (4x then scaled down) with the PIXEL_ART rows painted on top."""
    art = PIXEL_ART[size]
    m = art["margin"] * SS
    tile, _ = draw_tile(size * SS, (m, m, size * SS - m, size * SS - m), art["radius"] * SS)
    img = tile.resize((size, size), Image.BOX)
    px = img.load()
    for y, row in enumerate(art["rows"]):
        assert len(row) == size, "row %d of the %d px art has %d pixels" % (y, size, len(row))
        for x, ch in enumerate(row):
            if ch == "W":
                px[x, y] = ENVELOPE + (255,)
            elif ch == "w":
                px[x, y] = blend(px[x, y], ENVELOPE, 0.55)
            elif ch == "v":
                px[x, y] = FLAP + (255,)
            elif ch == "L":
                px[x, y] = CHEVRON + (255,)
    return img


def make_icon(size):
    """The finished RGBA image for one icon size."""
    if size in PIXEL_ART:
        return render_pixel_art(size)
    if size in MID_LAYOUTS:
        return render(MID_LAYOUTS[size], size)
    if size < 128:
        return render(dict(MASTER, side_lines=[]), size)
    return render(MASTER, size)


def bmp_entry(img):
    """32-bit BMP icon entry (BITMAPINFOHEADER + BGRA rows bottom-up + AND mask)."""
    n = img.size[0]
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    r, g, b, a = img.split()
    pixels = Image.merge("RGBA", (b, g, r, a)).transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    row_bytes = ((n + 31) // 32) * 4
    alpha = a.transpose(Image.FLIP_TOP_BOTTOM).load()
    mask = bytearray()
    for y in range(n):
        row = bytearray(row_bytes)
        for x in range(n):
            if alpha[x, y] == 0:  # fully transparent -> masked out
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row
    return header + pixels + bytes(mask)


def write_ico(path, images):
    """Classic multi-size .ico: BMP entries below 256 px, PNG for 256 px."""
    blobs = []
    for img in images:
        if img.size[0] >= 256:
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            blobs.append(buf.getvalue())
        else:
            blobs.append(bmp_entry(img))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for img, blob in zip(images, blobs):
        n = img.size[0] % 256  # 0 means 256
        out += struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(out + b"".join(blobs))


def main():
    icons = dict((size, make_icon(size)) for size in ICO_SIZES)
    icons[256].save(os.path.join(HERE, "squish.png"), optimize=True)
    icons[64].save(os.path.join(HERE, "squish-64.png"), optimize=True)
    icons[32].save(os.path.join(HERE, "squish-32.png"), optimize=True)
    write_ico(os.path.join(HERE, "squish.ico"), [icons[s] for s in ICO_SIZES])
    print("Wrote squish.png, squish-64.png, squish-32.png, squish.ico to " + HERE)


if __name__ == "__main__":
    main()
