#!/usr/bin/env python3
"""What copper actually sits inside each rule area of a .kicad_pcb.

Tests footprint origins, track segment endpoints and vias against each
rule-area polygon. Rotation-free: uses absolute coordinates only.

Usage:  python3 kicad_whatsinside.py path/to/board.kicad_pcb
"""
import re, sys, glob

path = sys.argv[1] if len(sys.argv) > 1 else None
if not path:
    c = glob.glob('*.kicad_pcb') + glob.glob('../*.kicad_pcb')
    if not c:
        sys.exit('give me the .kicad_pcb path')
    path = c[0]
s = open(path).read()

def block(txt, start):
    d = 0
    for j in range(start, len(txt)):
        if txt[j] == '(':
            d += 1
        elif txt[j] == ')':
            d -= 1
            if d == 0:
                return txt[start:j + 1]
    return txt[start:]

def inside(pt, poly):
    x, y = pt; c = False; n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]; x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xin = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x < xin:
                c = not c
    return c

# rule areas
areas = []
for m in re.finditer(r'\(zone\b', s):
    blk = block(s, m.start())
    if '(keepout' not in blk:
        continue
    pts = [(float(a), float(b))
           for a, b in re.findall(r'\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)', blk)]
    areas.append(pts)

# footprints
fps = []
for m in re.finditer(r'\(footprint\b', s):
    blk = block(s, m.start())
    at = re.search(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)', blk)
    ref = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', blk)
    lay = re.search(r'\(layer\s+"([^"]*)"', blk)
    if at:
        fps.append((float(at.group(1)), float(at.group(2)),
                    ref.group(1) if ref else '?',
                    lay.group(1) if lay else '?'))

# tracks and vias on B.Cu
trk, vias = [], []
for m in re.finditer(r'\(segment\b', s):
    blk = block(s, m.start())
    if '"B.Cu"' not in blk:
        continue
    a = re.search(r'\(start\s+(-?[\d.]+)\s+(-?[\d.]+)', blk)
    b = re.search(r'\(end\s+(-?[\d.]+)\s+(-?[\d.]+)', blk)
    if a and b:
        trk.append(((float(a.group(1)), float(a.group(2))),
                    (float(b.group(1)), float(b.group(2)))))
for m in re.finditer(r'\(via\b', s):
    blk = block(s, m.start())
    a = re.search(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)', blk)
    if a:
        vias.append((float(a.group(1)), float(a.group(2))))

print('%d rule areas, %d footprints, %d B.Cu track segments, %d vias\n'
      % (len(areas), len(fps), len(trk), len(vias)))

for i, poly in enumerate(areas, 1):
    f = [(r, l) for x, y, r, l in fps if inside((x, y), poly)]
    t = sum(1 for a, b in trk if inside(a, poly) or inside(b, poly))
    v = sum(1 for p in vias if inside(p, poly))
    print('RULE AREA %d' % i)
    print('   footprints inside : %s' % (', '.join('%s [%s]' % (r, l)
          for r, l in f) if f else 'NONE'))
    print('   B.Cu track ends   : %d' % t)
    print('   vias              : %d' % v)
    print('   verdict           : %s\n'
          % ('EMPTY - safe to raster' if not f and not t and not v
             else 'HAS COPPER - do NOT raster'))
