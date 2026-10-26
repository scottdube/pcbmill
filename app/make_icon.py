"""Draw PCBMill.icns: an isolation-milled copper board with a V-bit cutting a trace.

Run with the repo venv (Pillow comes with it):  .venv/bin/python app/make_icon.py
Writes app/PCBMill.icns, which build.sh copies into the bundle. Drawn in code
rather than an SVG because this Mac has no SVG rasteriser (no rsvg/magick/inkscape).
"""
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter

S = 2048                      # draw at 2x, downsample to 1024 for anti-aliasing
HERE = os.path.dirname(os.path.abspath(__file__))


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vgradient(size, top, bottom):
    w, h = size
    g = Image.new("RGBA", size)
    d = ImageDraw.Draw(g)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / (h - 1)))
    return g


def hgradient(size, stops):
    w, h = size
    g = Image.new("RGBA", size)
    d = ImageDraw.Draw(g)
    for x in range(w):
        t = x / (w - 1) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        d.line([(x, 0), (x, h)], fill=lerp(stops[i], stops[i + 1], t - i))
    return g


def mask_rrect(box, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, r, fill=255)
    return m


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # macOS icon grid: 824/1024 body, ~185 corner radius. macOS 26 puts any
    # plain .icns in a gray tile (full-bleed too: measured); build.sh also sets
    # this as a custom Finder icon, which macOS shows exactly as drawn.
    b0, b1, rad = 200, S - 200, 370
    body = (b0, b0, b1, b1)

    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (b0, b0 + 24, b1, b1 + 24), rad, fill=(0, 0, 0, 110))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))

    bg = vgradient((S, S), (44, 52, 64, 255), (22, 26, 34, 255))
    img.paste(bg, (0, 0), mask_rrect(body, rad))

    # copper-clad blank
    c0, c1 = b0 + 150, b1 - 150
    copper = vgradient((S, S), (236, 160, 104, 255), (184, 98, 52, 255))
    plate = mask_rrect((c0, c0, c1, c1), 70)
    img.paste(copper, (0, 0), plate)

    # isolation channels: draw every net wide in dark, then narrower in copper
    groove = (58, 40, 30, 255)
    gap, tw, pr = 34, 64, 78          # channel width, trace width, pad radius
    hole = 28
    nets = [
        [(500, 760), (900, 760), (1100, 960), (1100, 1500)],
        [(500, 1100), (760, 1100), (760, 1500)],
        [(500, 1500), (500, 1290)],
        [(1380, 1500), (1380, 1180), (1560, 1000)],
    ]
    pads = [p for n in nets for p in (n[0], n[-1])]

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for n in nets:
        d.line(n, fill=groove, width=tw + 2 * gap, joint="curve")
        for p in n:
            d.ellipse((p[0] - (tw / 2 + gap), p[1] - (tw / 2 + gap),
                       p[0] + (tw / 2 + gap), p[1] + (tw / 2 + gap)), fill=groove)
    for x, y in pads:
        d.ellipse((x - pr - gap, y - pr - gap, x + pr + gap, y + pr + gap), fill=groove)
    img.paste(layer, (0, 0), Image.composite(layer, Image.new("RGBA", (S, S)), plate))

    shine = vgradient((S, S), (255, 196, 140, 255), (206, 120, 66, 255))
    tmask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(tmask)
    for n in nets:
        d.line(n, fill=255, width=tw, joint="curve")
        for p in n:
            d.ellipse((p[0] - tw / 2, p[1] - tw / 2, p[0] + tw / 2, p[1] + tw / 2), fill=255)
    for x, y in pads:
        d.ellipse((x - pr, y - pr, x + pr, y + pr), fill=255)
    img.paste(shine, (0, 0), tmask)

    d = ImageDraw.Draw(img)
    for x, y in pads:
        d.ellipse((x - hole, y - hole, x + hole, y + hole), fill=(30, 22, 18, 255))

    # V-bit, tip on the last net's open end where the channel is being cut
    tx, ty = 1560, 1000
    sw = 150                                   # shank width
    shank_top = b0 - 40                        # runs off the top of the body, clipped below
    cone_top = ty - 300
    steel = [(120, 128, 140, 255), (238, 242, 248, 255), (170, 178, 190, 255), (96, 104, 116, 255)]
    tool = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = hgradient((sw, S), steel)
    tool.paste(grad.crop((0, 0, sw, cone_top - shank_top)), (tx - sw // 2, shank_top))
    cone = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cone).polygon(
        [(tx - sw // 2, cone_top), (tx + sw // 2, cone_top), (tx, ty)], fill=255)
    full = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    full.paste(grad, (tx - sw // 2, 0))
    tool.paste(full, (0, 0), cone)
    # flute edge across the cone
    ImageDraw.Draw(tool).line([(tx - sw // 2 + 10, cone_top + 6), (tx + 6, ty - 40)],
                              fill=(80, 88, 100, 255), width=10)
    # collar line where the cone meets the shank
    ImageDraw.Draw(tool).line([(tx - sw // 2, cone_top), (tx + sw // 2, cone_top)],
                              fill=(70, 76, 88, 255), width=8)

    tshadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tshadow.paste((0, 0, 0, 120), (0, 0), tool.split()[3])
    tshadow = tshadow.filter(ImageFilter.GaussianBlur(18))
    clip = mask_rrect(body, rad)
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh.paste(tshadow, (30, 24), None)
    img.paste(sh, (0, 0), Image.composite(sh.split()[3], Image.new("L", (S, S), 0), clip))
    img.paste(tool, (0, 0), Image.composite(tool.split()[3], Image.new("L", (S, S), 0), clip))

    # chip spark at the tip
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((tx - 60, ty - 60, tx + 60, ty + 60), fill=(255, 230, 170, 200))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(24)))

    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    master = draw()
    out = os.path.join(HERE, "PCBMill.icns")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "PCBMill.iconset")
        os.mkdir(iconset)
        for size in (16, 32, 128, 256, 512):
            master.resize((size, size), Image.LANCZOS).save(
                os.path.join(iconset, "icon_%dx%d.png" % (size, size)))
            master.resize((size * 2, size * 2), Image.LANCZOS).save(
                os.path.join(iconset, "icon_%dx%d@2x.png" % (size, size)))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
        if os.environ.get("PCBMILL_ICON_PREVIEW"):
            shutil.copy(os.path.join(iconset, "icon_512x512@2x.png"),
                        os.environ["PCBMILL_ICON_PREVIEW"])
    print("wrote", out)


if __name__ == "__main__":
    main()
