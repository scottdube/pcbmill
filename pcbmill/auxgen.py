"""Auxiliary generators (FR8): depth staircase + keepout clearing.

The algorithms are lifted from the proven mkstair.py / mkclear.py and
parameterized; the originals are vendored unchanged for reference. All
output Z is RELATIVE to the copper surface - these files MUST go through
applymap before running (same contract as mkclear.py).
"""

import math

EPS = 1e-9


# ---------------------------------------------------------------- staircase

def stair_gcode(x0, y0, depths=None, width=0.090, height=0.170, gap=0.020,
                t_number=600, rpm=10000, feed=9.0, plunge=5.0, safe_z=0.1):
    """5-rectangle depth staircase - closed shapes, one per commanded depth
    (open lines can't be ohmmeter-tested on a continuous sheet)."""
    depths = depths or [-0.0010, -0.0015, -0.0020, -0.0025, -0.0030]
    L = ["(depth staircase - %d closed rectangles, one per depth)" % len(depths),
         "(Z is RELATIVE to the copper surface - run applymap before use)",
         "G94", "G20", "G90", "G54", "G00 S%d" % rpm, "G64 P0.00020",
         # G37 before G43, and only after the M0 pause: apply a length that
         # was actually measured, not whatever the tool table happens to hold.
         # Same invariant as postdrill.py / post.py / clear_gcode.
         "G30", "T%d" % t_number, "M5", "M0",
         "G37 H%d (measure this tool on the ETS)" % t_number,
         "G43 H%d (apply the length G37 just stored)" % t_number,
         "G30 (clear of the ETS before descending)", "M3",
         "G04 P1.0"]
    for i, d in enumerate(depths):
        rx0 = x0 + i * (width + gap)
        rx1 = rx0 + width
        ry0, ry1 = y0, y0 + height
        L += ["(rect %d  commanded depth %.4f  X%.4f-%.4f)" % (i + 1, d, rx0, rx1),
              "G00 Z%.5f" % safe_z,
              "G00 X%.4f Y%.4f" % (rx0, ry0),
              "G01 Z%.5f F%g" % (d, plunge),
              "G01 X%.4f Y%.4f F%g" % (rx1, ry0, feed),
              "G01 X%.4f Y%.4f" % (rx1, ry1),
              "G01 X%.4f Y%.4f" % (rx0, ry1),
              "G01 X%.4f Y%.4f" % (rx0, ry0),
              "G00 Z%.5f" % safe_z]
    L += ["G30", "M5", "M2", ""]
    extent = (x0, y0, x0 + len(depths) * (width + gap) - gap, y0 + height)
    return "\n".join(L), extent


# ------------------------------------------------------- polygon machinery
# (from mkclear.py - half-plane inset, NOT naive vertex offset)

def _edges(poly):
    for i in range(len(poly)):
        yield poly[i], poly[(i + 1) % len(poly)]


def _signed_area(poly):
    a = 0.0
    for (ax, ay), (bx, by) in _edges(poly):
        a += ax * by - bx * ay
    return a / 2.0


def _inward_halfplanes(poly, r):
    cw = _signed_area(poly) < 0
    hp = []
    for (ax, ay), (bx, by) in _edges(poly):
        vx, vy = bx - ax, by - ay
        L = math.hypot(vx, vy)
        if L < EPS:
            continue
        nx, ny = (vy / L, -vx / L) if cw else (-vy / L, vx / L)
        hp.append((nx, ny, nx * ax + ny * ay + r))
    return hp


def _clip(poly, nx, ny, c):
    if not poly:
        return []
    out = []
    for i in range(len(poly)):
        cur, prv = poly[i], poly[i - 1]
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


def _dedupe(poly, tol=1e-7):
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < tol \
            and abs(out[0][1] - out[-1][1]) < tol:
        out.pop()
    return out


def inset_region(poly, tool_radius, map_bounds=None):
    """Erode a convex polygon by the tool radius, clip to the probe map.
    Returns [] if the tool is too big for the region."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    pad = 1.0
    region = [(min(xs) - pad, min(ys) - pad), (max(xs) + pad, min(ys) - pad),
              (max(xs) + pad, max(ys) + pad), (min(xs) - pad, max(ys) + pad)]
    for nx, ny, c in _inward_halfplanes(poly, tool_radius):
        region = _clip(region, nx, ny, c)
    if map_bounds:
        mx0, my0, mx1, my1 = map_bounds
        region = _clip(region, 1.0, 0.0, mx0)
        region = _clip(region, -1.0, 0.0, -mx1)
        region = _clip(region, 0.0, 1.0, my0)
        region = _clip(region, 0.0, -1.0, -my1)
    region = _dedupe(region)
    return region if len(region) >= 3 else []


def _span_at_y(region, y):
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


def clear_gcode(poly, tool_diameter, depth, t_number, map_bounds=None,
                stepover_frac=0.5, rpm=10000, feed=15.0, plunge=5.0,
                safe_z=0.1):
    """Serpentine raster + perimeter pass clearing of a convex keepout.
    Same tool-change/ETS-safe block shape as postdrill.py output."""
    r = tool_diameter / 2.0
    region = inset_region(poly, r, map_bounds)
    if not region:
        raise ValueError("tool %.3f\" is too wide for this region" % tool_diameter)
    stepover = tool_diameter * stepover_frac
    ys = [p[1] for p in region]
    ymin, ymax = min(ys), max(ys)
    levels = []
    y = ymin
    while y < ymax - 1e-9:
        levels.append(y)
        y += stepover
    levels.append(ymax)

    L = ["(keepout clearing - generated by pcbmill from a rule area)",
         "(inset %.4f for the %.3f cutter; half-plane erosion)" % (r, tool_diameter),
         "(Z is RELATIVE to the copper surface - run applymap before use)",
         "(commanded depth %.4f  stepover %.4f  %d raster passes)"
         % (depth, stepover, len(levels)),
         "G94", "G20", "G91.1", "G90", "G54", "G00 S%d" % rpm,
         "G64 P0.00020"]
    first = region[0]
    L += ["G30 (retract to tool change position)",
          "T%d" % t_number,
          "M5      (Spindle stop.)",
          "G04 P1.00000",
          "(MSG, Change tool to the %.3f inch end mill)" % tool_diameter,
          "M6      (Tool change.)",
          "M0      (Temporary machine stop.)",
          "G37 H%d (measure this tool on the ETS)" % t_number,
          "G43 H%d (apply the length G37 just stored)" % t_number,
          "G30 (clear of the ETS before descending)",
          "G00 X%.4f Y%.4f (over the work first)" % (first[0], first[1]),
          "M3      (Spindle on clockwise.)",
          "G04 P1.0"]

    def rapid_to(x, y):
        L.append("G00 Z%.5f" % safe_z)
        L.append("G00 X%.4f Y%.4f" % (x, y))
        L.append("G01 Z%.5f F%g" % (depth, plunge))

    L.append("(perimeter pass on the inset boundary)")
    rapid_to(*region[0])
    L.append("G01 X%.4f Y%.4f F%g" % (region[1][0], region[1][1], feed))
    for p in region[2:] + [region[0]]:
        L.append("G01 X%.4f Y%.4f" % p)
    L.append("G00 Z%.5f" % safe_z)

    L.append("(raster interior)")
    flip = False
    for i, yl in enumerate(levels):
        s = _span_at_y(region, yl)
        if not s:
            continue
        a, b = (s[1], s[0]) if flip else s
        L.append("(pass %d  Y%.4f)" % (i + 1, yl))
        rapid_to(a, yl)
        L.append("G01 X%.4f Y%.4f F%g" % (b, yl, feed))
        L.append("G00 Z%.5f" % safe_z)
        flip = not flip

    L += ["G30", "M5", "M2", ""]
    return "\n".join(L), region
