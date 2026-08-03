#!/usr/bin/env python3
"""Generate the area-1 copper clearing toolpath for sln-geo-aux-heat.

Rule area 1 is the ESP32 antenna keepout on B.Cu.  Isolation routing leaves
the background copper in place, so that copper has to be removed or it
detunes the antenna.  This is the removal pass.

GEOMETRY
  The area-1 polygon comes from kicad_regions.py (KiCad mm -> G54 inch,
  X mirrored).  It is convex and wound clockwise.

  The polygon boundary is a COPPER boundary: everything outside it is
  legitimate copper that must not be touched.  So the toolpath centreline
  is inset by one tool radius, computed as the intersection of the offset
  half-planes.  Half-planes, not a naive vertex offset - edge P4->P5 is
  only 0.016 in long, far shorter than the 0.0625 in offset, and a vertex
  offset self-intersects there.

  The bottom of the polygon (Y 0.4525) hangs 0.079 in BELOW the Edge_Cuts
  line at Y 0.5313, and below the probe map floor at Y 0.540.  The
  centreline is clipped to the map floor; the cutter still reaches down to
  Y 0.4775, well past the board edge, so coverage is complete without
  asking applymap to extrapolate.

OUTPUT
  Z values are relative to the copper surface (negative = into copper),
  and safe Z is relative to WCS Z0.  applymap.py turns both into absolute
  WCS heights.  Do not run this file on the machine uncompensated.
"""

# ----------------------------------------------------------------- inputs

# Rule area 1, G54 inch, clockwise, from kicad_regions.py
POLY = [
    (3.8451, 0.7872),
    (3.8451, 0.4525),
    (3.1561, 0.4525),
    (3.1561, 0.7675),
    (3.1674, 0.7791),
]

TOOL_D   = 0.125          # 1/8 in flat end mill, T610
STEPOVER = 0.0625         # 50 percent of tool diameter
DEPTH    = 0.00133        # ETS bias +0.00383 (mean of the two clean continuity touches)
SAFE_Z   = 0.10           # relative to WCS Z0, same as mkstair.py
TOOL_N   = 610
RPM      = 10000
FEED     = 15.0           # in/min, cutting
PLUNGE   = 5.0            # in/min, matches millproject mill-vertfeed

MAP_YMIN = 0.540          # probe map floor - do not put a centreline below
MAP_YMAX = 3.140
MAP_XMIN = 0.490
MAP_XMAX = 4.270

OUT = 'clear.ngc'

R = TOOL_D / 2.0
EPS = 1e-9

# ------------------------------------------------------------- half-planes


def edges(poly):
    for i in range(len(poly)):
        yield poly[i], poly[(i + 1) % len(poly)]


def signed_area(poly):
    a = 0.0
    for (x0, y0), (x1, y1) in edges(poly):
        a += x0 * y1 - x1 * y0
    return a / 2.0


def inward_halfplanes(poly, r):
    """(nx, ny, c) with nx*x + ny*y >= c for points at least r inside."""
    cw = signed_area(poly) < 0
    hp = []
    for (x0, y0), (x1, y1) in edges(poly):
        vx, vy = x1 - x0, y1 - y0
        L = (vx * vx + vy * vy) ** 0.5
        if L < EPS:
            continue
        # interior lies to the right of travel for a clockwise winding
        nx, ny = (vy / L, -vx / L) if cw else (-vy / L, vx / L)
        hp.append((nx, ny, nx * x0 + ny * y0 + r))
    return hp


def clip(poly, nx, ny, c):
    """Sutherland-Hodgman against nx*x + ny*y >= c."""
    if not poly:
        return []
    out = []
    for i in range(len(poly)):
        cur = poly[i]
        prv = poly[i - 1]
        dc = nx * cur[0] + ny * cur[1] - c
        dp = nx * prv[0] + ny * prv[1] - c
        if dc >= -EPS:
            if dp < -EPS:
                t = dp / (dp - dc)
                out.append((prv[0] + t * (cur[0] - prv[0]),
                            prv[1] + t * (cur[1] - prv[1])))
            out.append(cur)
        elif dp >= -EPS:
            t = dp / (dp - dc)
            out.append((prv[0] + t * (cur[0] - prv[0]),
                        prv[1] + t * (cur[1] - prv[1])))
    return out


def dedupe(poly, tol=1e-7):
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < tol \
            and abs(out[0][1] - out[-1][1]) < tol:
        out.pop()
    return out


def build_inset():
    xs = [p[0] for p in POLY]
    ys = [p[1] for p in POLY]
    pad = 1.0
    box = [(min(xs) - pad, min(ys) - pad), (max(xs) + pad, min(ys) - pad),
           (max(xs) + pad, max(ys) + pad), (min(xs) - pad, max(ys) + pad)]
    region = box
    for nx, ny, c in inward_halfplanes(POLY, R):
        region = clip(region, nx, ny, c)
    # clip the centreline to the probe map so applymap never extrapolates
    region = clip(region, 0.0, 1.0, MAP_YMIN)
    region = clip(region, 0.0, -1.0, -MAP_YMAX)
    region = clip(region, 1.0, 0.0, MAP_XMIN)
    region = clip(region, -1.0, 0.0, -MAP_XMAX)
    return dedupe(region)


def span_at_y(region, y):
    """X interval of the convex region at height y, or None."""
    hits = []
    n = len(region)
    for i in range(n):
        x0, y0 = region[i]
        x1, y1 = region[(i + 1) % n]
        if abs(y1 - y0) < EPS:
            if abs(y0 - y) < 1e-7:
                hits += [x0, x1]
            continue
        if min(y0, y1) - 1e-9 <= y <= max(y0, y1) + 1e-9:
            t = (y - y0) / (y1 - y0)
            if -1e-9 <= t <= 1 + 1e-9:
                hits.append(x0 + t * (x1 - x0))
    if len(hits) < 2:
        return None
    lo, hi = min(hits), max(hits)
    return None if hi - lo < 1e-6 else (lo, hi)


# ------------------------------------------------------------------ emit

def main():
    region = build_inset()
    if len(region) < 3:
        raise SystemExit('inset region collapsed - tool too big for the area')

    ys = [p[1] for p in region]
    ymin, ymax = min(ys), max(ys)

    levels = []
    y = ymin
    while y < ymax - 1e-9:
        levels.append(y)
        y += STEPOVER
    levels.append(ymax)

    L = ['(area 1 copper clearing - ESP32 antenna keepout)',
         '(source: kicad_regions.py RULE AREA 1, inset %.4f for %.3f cutter)'
         % (R, TOOL_D),
         '(centreline clipped to probe map floor Y%.3f)' % MAP_YMIN,
         '(Z is RELATIVE to the copper surface - run applymap.py before use)',
         '(commanded depth %.4f  stepover %.4f  %d raster passes)'
         % (DEPTH, STEPOVER, len(levels)),
         'G94', 'G20', 'G91.1', 'G90', 'G54', 'G00 S%d' % RPM,
         'G64 P0.00020']

    def rapid_to(x, y):
        L.append('G00 Z%.5f' % SAFE_Z)
        L.append('G00 X%.4f Y%.4f' % (x, y))
        L.append('G01 Z%.5f F%.1f' % (DEPTH, PLUNGE))

    # Tool-change block, same shape as postdrill.py produces for the drills:
    # measure on the ETS, apply, then G30 and traverse to the first XY BEFORE
    # any Z descent - otherwise the descent happens over the ETS.
    first = region[0]
    L += ['G30 (retract to tool change position)',
          'T%d' % TOOL_N,
          'M5      (Spindle stop.)',
          'G04 P1.00000',
          '(MSG, Change tool to the 1/8 inch flat end mill)',
          'M6      (Tool change.)',
          'M0      (Temporary machine stop.)',
          'G37 H%d (measure this tool on the ETS)' % TOOL_N,
          'G43 H%d (apply the length G37 just stored)' % TOOL_N,
          'G30 (clear of the ETS before descending)',
          'G00 X%.4f Y%.4f (over the work first)' % (first[0], first[1]),
          'M3      (Spindle on clockwise.)',
          'G04 P1.0']

    # perimeter first - catches the thin wedge under the sloped top edge
    # that a horizontal raster cannot reach
    L.append('(perimeter pass on the inset boundary)')
    rapid_to(*region[0])
    L.append('G01 X%.4f Y%.4f F%.1f' % (region[1][0], region[1][1], FEED))
    for p in region[2:] + [region[0]]:
        L.append('G01 X%.4f Y%.4f' % p)
    L.append('G00 Z%.5f' % SAFE_Z)

    # raster the interior, serpentine along X
    L.append('(raster interior)')
    flip = False
    cut = 0
    for i, yl in enumerate(levels):
        s = span_at_y(region, yl)
        if not s:
            continue
        x0, x1 = (s[1], s[0]) if flip else s
        L.append('(pass %d  Y%.4f  X%.4f to %.4f)' % (i + 1, yl, x0, x1))
        rapid_to(x0, yl)
        L.append('G01 X%.4f Y%.4f F%.1f' % (x1, yl, FEED))
        L.append('G00 Z%.5f' % SAFE_Z)
        flip = not flip
        cut += abs(x1 - x0)

    L += ['G30', 'M5', 'M2', '']
    open(OUT, 'w').write('\n'.join(L))

    per = sum(((region[i][0] - region[i - 1][0]) ** 2
               + (region[i][1] - region[i - 1][1]) ** 2) ** 0.5
              for i in range(len(region)))
    print('wrote', OUT)
    print('inset region, %d corners:' % len(region))
    for x, y in region:
        print('   X %7.4f  Y %7.4f' % (x, y))
    print('Y span   %.4f to %.4f   (%d raster passes at %.4f)'
          % (ymin, ymax, len(levels), STEPOVER))
    print('cut length  perimeter %.3f in + raster %.3f in = %.3f in'
          % (per, cut, per + cut))
    print('cut time at F%.0f  %.1f min (plus rapids)' % (FEED, (per + cut) / FEED))


if __name__ == '__main__':
    main()
