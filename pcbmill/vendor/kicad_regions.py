#!/usr/bin/env python3
"""Pull rule-area (keepout) polygons out of a .kicad_pcb and convert to G54.

Usage:  python3 kicad_regions.py path/to/board.kicad_pcb
"""
import re, sys, glob

MX, MY = 4.3372, 3.2478          # G54 = M - kicad_mm/25.4   (X reversed: mirror)

path = sys.argv[1] if len(sys.argv) > 1 else None
if not path:
    c = glob.glob('*.kicad_pcb') + glob.glob('../*.kicad_pcb')
    if not c:
        sys.exit('give me the .kicad_pcb path')
    path = c[0]
s = open(path).read()
print('file:', path, '\n')

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

def area(p):
    a = 0.0
    for i in range(len(p)):
        x0, y0 = p[i]; x1, y1 = p[(i + 1) % len(p)]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0

n = 0
for m in re.finditer(r'\(zone\b', s):
    blk = block(s, m.start())
    if '(keepout' not in blk:
        continue
    n += 1
    layer = re.search(r'\(layers?\s+"?([^")\s]+)', blk)
    pts = [(float(a), float(b))
           for a, b in re.findall(r'\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)', blk)]
    ko = re.search(r'\(keepout(.*?)\n\s*\)', blk, re.S)
    rest = ' '.join('%s=%s' % (a, b) for a, b in
                    re.findall(r'\((\w+)\s+(\w+)\)', ko.group(1))) if ko else '?'
    print('RULE AREA %d   layer %s   %d corners   %.3f mm2'
          % (n, layer.group(1) if layer else '?', len(pts), area(pts)))
    print('   restrictions: %s' % rest)
    print('   %-22s %s' % ('KiCad mm', 'G54 inch'))
    for x, y in pts:
        print('   %8.3f %8.3f       X %7.4f  Y %7.4f'
              % (x, y, MX - x / 25.4, MY - y / 25.4))
    gx = [MX - x / 25.4 for x, y in pts]
    gy = [MY - y / 25.4 for x, y in pts]
    inmap = (min(gx) >= 0.490 and max(gx) <= 4.270
             and min(gy) >= 0.540 and max(gy) <= 3.140)
    print('   bounds  X %.4f to %.4f   Y %.4f to %.4f   %s\n'
          % (min(gx), max(gx), min(gy), max(gy),
             'inside probe map' if inmap else 'EXTENDS OUTSIDE THE PROBE MAP'))
if not n:
    print('no rule areas found')
