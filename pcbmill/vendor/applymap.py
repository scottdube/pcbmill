#!/usr/bin/env python3
"""
applymap.py - apply a probed height map to a G-code file.

Reads a probe-results file (G1 X.. Y.. Z.. per line, as written by
smartprobe's LOG command) and a G-code file, and emits a new G-code file
whose cutting moves follow the measured surface.

WHAT IT DOES
  - Builds a regular grid from the probe points and interpolates it
    bilinearly.
  - Subdivides long G1 moves so each piece is short compared to the grid
    pitch, and gives every piece its own Z.
  - Linearises G2/G3 arcs (I/J and R forms, including helical) into short
    G1 segments so they get compensated too.
  - Leaves G0 rapids as rapids, but in absolute mode shifts their Z by the
    map's mean so clearance heights stay clearance heights.
  - Clamps queries that fall outside the map to the nearest edge, and
    counts them so silent extrapolation is impossible.

TWO MODES
  absolute  (default)  The map's Z values are real WCS heights - i.e. you
                       did NOT re-zero Z on the work surface. Every cutting
                       Z becomes  Z + map(x,y).  Rapids get Z + mean(map).

  relative             You HAVE zeroed Z on the work surface. Only the
                       deviation from the map's reference is applied:
                       Z + (map(x,y) - ref).  ref defaults to the map mean;
                       override with --z-ref.

USAGE
  applymap.py -m probe-results.ngc -i in.ngc -o out.ngc
  applymap.py -m probe-results.ngc -i in.ngc -o out.ngc --mode relative
  applymap.py -m probe-results.ngc -i in.ngc -o out.ngc \
              --offset-x 0.587 --offset-y 0.62

  --offset-x/--offset-y shift the G-CODE into the MAP's frame. Use them when
  the G-code is board-relative but the map is in machine coordinates. If both
  already share the same WCS, leave them at 0.

Python 3, standard library only.
"""

import argparse
import math
import re
import sys

MM_PER_IN = 25.4

# ---------------------------------------------------------------- map


class HeightMap:
    """A regular XY grid of probed Z values with bilinear lookup."""

    def __init__(self, xs, ys, z, units):
        self.xs = xs                      # sorted unique X, ascending
        self.ys = ys                      # sorted unique Y, ascending
        self.z = z                        # z[iy][ix]
        self.units = units
        self.clamped = 0                  # count of out-of-map queries
        flat = [v for row in z for v in row]
        self.zmin = min(flat)
        self.zmax = max(flat)
        self.zmean = sum(flat) / len(flat)

    LOGLINE = re.compile(
        r"^\s*G0*1\s+X\s*(-?[\d.]+(?:[eE][-+]?\d+)?)"
        r"\s+Y\s*(-?[\d.]+(?:[eE][-+]?\d+)?)"
        r"\s+Z\s*(-?[\d.]+(?:[eE][-+]?\d+)?)",
        re.IGNORECASE,
    )

    @classmethod
    def from_file(cls, path, units, tol=1e-4):
        pts = []
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                m = cls.LOGLINE.match(line)
                if m:
                    pts.append(tuple(float(g) for g in m.groups()))
        if len(pts) < 4:
            die("map %s: found %d probe points, need at least 4" % (path, len(pts)))

        xs = uniq([p[0] for p in pts], tol)
        ys = uniq([p[1] for p in pts], tol)
        if len(xs) < 2 or len(ys) < 2:
            die("map %s: needs at least 2 distinct X and 2 distinct Y values "
                "(got %d x %d)" % (path, len(xs), len(ys)))

        grid = [[None] * len(xs) for _ in ys]
        for x, y, z in pts:
            ix = nearest_index(xs, x)
            iy = nearest_index(ys, y)
            grid[iy][ix] = z

        missing = [(ix, iy) for iy in range(len(ys))
                   for ix in range(len(xs)) if grid[iy][ix] is None]
        if missing:
            warn("map has %d empty grid cells out of %d; filling from nearest "
                 "neighbours" % (len(missing), len(xs) * len(ys)))
            for ix, iy in missing:
                grid[iy][ix] = nearest_value(grid, xs, ys, ix, iy)

        return cls(xs, ys, grid, units)

    def to_units(self, units):
        if units == self.units:
            return self
        k = MM_PER_IN if units == "mm" else 1.0 / MM_PER_IN
        return HeightMap([x * k for x in self.xs],
                         [y * k for y in self.ys],
                         [[v * k for v in row] for row in self.z],
                         units)

    def at(self, x, y):
        """Bilinear Z at (x, y), clamped to the map's extents."""
        xs, ys = self.xs, self.ys
        cx = min(max(x, xs[0]), xs[-1])
        cy = min(max(y, ys[0]), ys[-1])
        if cx != x or cy != y:
            self.clamped += 1

        ix = bracket(xs, cx)
        iy = bracket(ys, cy)
        x0, x1 = xs[ix], xs[ix + 1]
        y0, y1 = ys[iy], ys[iy + 1]
        tx = 0.0 if x1 == x0 else (cx - x0) / (x1 - x0)
        ty = 0.0 if y1 == y0 else (cy - y0) / (y1 - y0)

        z00 = self.z[iy][ix]
        z10 = self.z[iy][ix + 1]
        z01 = self.z[iy + 1][ix]
        z11 = self.z[iy + 1][ix + 1]
        return ((z00 * (1 - tx) + z10 * tx) * (1 - ty) +
                (z01 * (1 - tx) + z11 * tx) * ty)

    @property
    def pitch(self):
        dx = min(self.xs[i + 1] - self.xs[i] for i in range(len(self.xs) - 1))
        dy = min(self.ys[i + 1] - self.ys[i] for i in range(len(self.ys) - 1))
        return min(dx, dy)


def uniq(vals, tol):
    out = []
    for v in sorted(vals):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def nearest_index(arr, v):
    best, bi = None, 0
    for i, a in enumerate(arr):
        d = abs(a - v)
        if best is None or d < best:
            best, bi = d, i
    return bi


def nearest_value(grid, xs, ys, ix, iy):
    best, bv = None, None
    for jy in range(len(ys)):
        for jx in range(len(xs)):
            if grid[jy][jx] is None:
                continue
            d = (xs[jx] - xs[ix]) ** 2 + (ys[jy] - ys[iy]) ** 2
            if best is None or d < best:
                best, bv = d, grid[jy][jx]
    return bv


def bracket(arr, v):
    lo, hi = 0, len(arr) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if arr[mid] <= v:
            lo = mid
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------- gcode

WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
COMMENT = re.compile(r"\([^)]*\)|;.*$")


def strip_comments(line):
    return COMMENT.sub(" ", line)


def sanitize_comment(text):
    """PathPilot forbids nested parens in comments."""
    return text.replace("(", "[").replace(")", "]")


class Compensator:
    def __init__(self, hmap, mode, z_ref, max_seg, offset_x, offset_y,
                 decimals, arc_tol):
        self.map = hmap
        self.mode = mode
        self.z_ref = z_ref
        self.max_seg = max_seg
        self.ox = offset_x
        self.oy = offset_y
        self.dec = decimals
        self.arc_tol = arc_tol

        self.x = self.y = self.z = 0.0
        self.motion = None
        self.absolute = True
        self.plane = 17
        self.stats = dict(moves=0, segments=0, arcs=0, rapids=0)

    def z_cut(self, x, y, z_nom):
        m = self.map.at(x + self.ox, y + self.oy)
        if self.mode == "absolute":
            return z_nom + m
        return z_nom + (m - self.z_ref)

    def z_rapid(self, z_nom):
        if self.mode == "absolute":
            return z_nom + self.map.zmean
        return z_nom

    def fmt(self, v):
        s = "%.*f" % (self.dec, v)
        if s.startswith("-") and float(s) == 0.0:
            s = s[1:]
        return s

    def run(self, lines):
        out = []
        for lineno, raw in enumerate(lines, 1):
            out.extend(self.line(raw.rstrip("\n"), lineno))
        return out

    def line(self, raw, lineno):
        code = strip_comments(raw)
        words = [(w.group(1).upper(), float(w.group(2)))
                 for w in WORD.finditer(code)]
        if not words:
            return [raw]

        gs = []
        for k, v in words:
            if k != "G":
                continue
            if abs(v - round(v)) > 1e-6:
                # G90.1 / G91.1 are ARC DISTANCE modes, not positioning modes.
                # G91.1 (incremental I/J) is the default and is what the arc
                # code assumes. G90.1 would need absolute arc centres.
                if abs(v - 90.1) < 1e-6:
                    die("line %d: G90.1 absolute arc centres are not supported"
                        % lineno)
                continue
            gs.append(int(round(v)))
        for g in gs:
            if g == 90:
                self.absolute = True
            elif g == 91:
                self.absolute = False
            elif g in (17, 18, 19):
                self.plane = g
            elif g in (0, 1, 2, 3):
                self.motion = g

        if not self.absolute:
            die("line %d: G91 incremental mode is not supported. "
                "Height compensation needs absolute coordinates." % lineno)
        if self.motion in (2, 3) and self.plane != 17:
            die("line %d: arcs are only supported in the G17 XY plane." % lineno)

        vals = dict(words)
        has_axis = any(k in vals for k in ("X", "Y", "Z"))
        is_arc = self.motion in (2, 3) and any(
            k in vals for k in ("I", "J", "R"))

        if not has_axis and not is_arc:
            return [raw]
        if self.motion is None:
            return [raw]

        tx = vals.get("X", self.x)
        ty = vals.get("Y", self.y)
        tz = vals.get("Z", self.z)

        if self.motion == 0:
            return self.emit_rapid(raw, vals, tx, ty, tz)
        if self.motion == 1:
            return self.emit_line(raw, vals, tx, ty, tz)
        return self.emit_arc(raw, vals, tx, ty, tz)

    def passthrough_words(self, vals):
        skip = set("GXYZIJKR")
        keep = []
        for k in ("F", "S", "P", "Q", "L", "H", "D", "T", "M"):
            if k in vals:
                keep.append("%s%s" % (k, trim(vals[k])))
        for k, v in vals.items():
            if k not in skip and k not in "FSPQLHDTM":
                keep.append("%s%s" % (k, trim(v)))
        return keep

    def emit_rapid(self, raw, vals, tx, ty, tz):
        self.stats["rapids"] += 1
        parts = ["G0"]
        if "X" in vals:
            parts.append("X" + self.fmt(tx))
        if "Y" in vals:
            parts.append("Y" + self.fmt(ty))
        if "Z" in vals:
            parts.append("Z" + self.fmt(self.z_rapid(tz)))
        parts.extend(self.passthrough_words(vals))
        self.x, self.y, self.z = tx, ty, tz
        return [" ".join(parts)]

    def emit_line(self, raw, vals, tx, ty, tz):
        self.stats["moves"] += 1
        x0, y0, z0 = self.x, self.y, self.z
        dist = math.hypot(tx - x0, ty - y0)
        n = max(1, int(math.ceil(dist / self.max_seg)))
        extra = self.passthrough_words(vals)

        out = []
        for i in range(1, n + 1):
            t = i / n
            xi = x0 + (tx - x0) * t
            yi = y0 + (ty - y0) * t
            zi = z0 + (tz - z0) * t
            parts = ["G1",
                     "X" + self.fmt(xi),
                     "Y" + self.fmt(yi),
                     "Z" + self.fmt(self.z_cut(xi, yi, zi))]
            if i == 1 and extra:
                parts.extend(extra)
            out.append(" ".join(parts))
        self.stats["segments"] += n
        self.x, self.y, self.z = tx, ty, tz
        return out

    def emit_arc(self, raw, vals, tx, ty, tz):
        self.stats["arcs"] += 1
        x0, y0, z0 = self.x, self.y, self.z
        cw = (self.motion == 2)

        if "R" in vals:
            cx, cy = center_from_r(x0, y0, tx, ty, vals["R"], cw)
        else:
            cx = x0 + vals.get("I", 0.0)
            cy = y0 + vals.get("J", 0.0)

        r = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(ty - cy, tx - cx)
        sweep = a1 - a0
        if cw:
            while sweep >= 0:
                sweep -= 2 * math.pi
            if abs(sweep + 2 * math.pi) < 1e-12:
                sweep = -2 * math.pi
        else:
            while sweep <= 0:
                sweep += 2 * math.pi
            if abs(sweep - 2 * math.pi) < 1e-12:
                sweep = 2 * math.pi
        if "R" not in vals and abs(tx - x0) < 1e-9 and abs(ty - y0) < 1e-9:
            sweep = -2 * math.pi if cw else 2 * math.pi

        if r > 1e-9 and self.arc_tol < r:
            dtheta = 2 * math.acos(max(-1.0, min(1.0, 1 - self.arc_tol / r)))
        else:
            dtheta = math.pi / 8
        n = max(2,
                int(math.ceil(abs(sweep) / max(dtheta, 1e-6))),
                int(math.ceil(abs(sweep) * r / self.max_seg)))

        extra = self.passthrough_words(vals)
        out = []
        for i in range(1, n + 1):
            t = i / n
            a = a0 + sweep * t
            xi = cx + r * math.cos(a)
            yi = cy + r * math.sin(a)
            zi = z0 + (tz - z0) * t
            parts = ["G1",
                     "X" + self.fmt(xi),
                     "Y" + self.fmt(yi),
                     "Z" + self.fmt(self.z_cut(xi, yi, zi))]
            if i == 1 and extra:
                parts.extend(extra)
            out.append(" ".join(parts))
        self.stats["segments"] += n
        self.x, self.y, self.z = tx, ty, tz
        return out


def center_from_r(x0, y0, x1, y1, r, cw):
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy)
    if d < 1e-12:
        die("arc with R form needs distinct start and end points")
    h2 = r * r - (d / 2) ** 2
    if h2 < 0:
        if h2 < -1e-9:
            die("arc radius %g is too small to span the endpoints" % r)
        h2 = 0.0
    h = math.sqrt(h2)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    ux, uy = -dy / d, dx / d
    sign = 1.0 if (r > 0) == (not cw) else -1.0
    return mx + sign * h * ux, my + sign * h * uy


def trim(v):
    s = ("%.6f" % v).rstrip("0").rstrip(".")
    return s if s else "0"


def detect_units(lines):
    for raw in lines:
        code = strip_comments(raw).upper()
        if re.search(r"\bG0*20\b", code):
            return "in"
        if re.search(r"\bG0*21\b", code):
            return "mm"
    return None


def warn(msg):
    sys.stderr.write("applymap: warning: %s\n" % msg)


def die(msg):
    sys.stderr.write("applymap: error: %s\n" % msg)
    sys.exit(1)


# ---------------------------------------------------------------- cli


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Apply a probed height map to a G-code file.")
    p.add_argument("-m", "--map", required=True,
                   help="probe results file (G1 X.. Y.. Z.. per line)")
    p.add_argument("-i", "--input", required=True, help="G-code to compensate")
    p.add_argument("-o", "--output", required=True, help="output G-code")
    p.add_argument("--mode", choices=("absolute", "relative"),
                   default="absolute",
                   help="absolute: map Z values are real WCS heights and are "
                        "added outright. relative: only deviation from --z-ref "
                        "is applied. Default: absolute")
    p.add_argument("--z-ref", type=float, default=None,
                   help="reference height for relative mode "
                        "(default: the map's mean)")
    p.add_argument("--map-units", choices=("in", "mm"), default="in",
                   help="units the probe file is in (default: in)")
    p.add_argument("--gcode-units", choices=("in", "mm"), default=None,
                   help="override units of the input G-code "
                        "(default: detect from G20/G21)")
    p.add_argument("--max-seg", type=float, default=None,
                   help="maximum segment length (G-code units). "
                        "Default: 1/6 of the map pitch")
    p.add_argument("--arc-tol", type=float, default=None,
                   help="max chord sag when linearising arcs. "
                        "Default: 0.0005 in / 0.012 mm")
    p.add_argument("--offset-x", type=float, default=0.0,
                   help="shift the G-code into the map's frame in X")
    p.add_argument("--offset-y", type=float, default=0.0,
                   help="shift the G-code into the map's frame in Y")
    p.add_argument("--decimals", type=int, default=None,
                   help="output decimal places (default: 4 in / 3 mm)")
    args = p.parse_args(argv)

    with open(args.input, "r", errors="replace") as fh:
        lines = fh.readlines()

    units = args.gcode_units or detect_units(lines)
    if units is None:
        die("could not find G20 or G21 in %s - pass --gcode-units"
            % args.input)

    hmap = HeightMap.from_file(args.map, args.map_units).to_units(units)

    decimals = args.decimals if args.decimals is not None else (
        4 if units == "in" else 3)
    max_seg = args.max_seg or (hmap.pitch / 6.0)
    arc_tol = args.arc_tol or (0.0005 if units == "in" else 0.012)
    z_ref = args.z_ref if args.z_ref is not None else hmap.zmean

    comp = Compensator(hmap, args.mode, z_ref, max_seg,
                       args.offset_x, args.offset_y, decimals, arc_tol)
    body = comp.run(lines)

    span = hmap.zmax - hmap.zmin
    header = [
        "(Height-compensated by applymap.py - DO NOT EDIT BY HAND)",
        "(  map file    : %s)" % sanitize_comment(args.map),
        "(  source      : %s)" % sanitize_comment(args.input),
        "(  mode        : %s)" % args.mode,
        "(  map grid    : %d x %d = %d points, pitch %.4f %s)"
        % (len(hmap.xs), len(hmap.ys), len(hmap.xs) * len(hmap.ys),
           hmap.pitch, units),
        "(  map extents : X %.4f to %.4f, Y %.4f to %.4f)"
        % (hmap.xs[0], hmap.xs[-1], hmap.ys[0], hmap.ys[-1]),
        "(  map Z       : min %.4f  max %.4f  mean %.4f  span %.4f)"
        % (hmap.zmin, hmap.zmax, hmap.zmean, span),
        "(  z reference : %.4f)" % (hmap.zmean if args.mode == "absolute"
                                    else z_ref),
        "(  xy offset   : %.4f, %.4f)" % (args.offset_x, args.offset_y),
        "(  max segment : %.4f %s)" % (max_seg, units),
        "(  clamped     : %d queries fell outside the map)" % hmap.clamped,
    ]

    with open(args.output, "w") as fh:
        fh.write("\n".join(header) + "\n")
        fh.write("\n".join(body) + "\n")

    sys.stderr.write(
        "applymap: %d moves -> %d segments, %d arcs linearised, %d rapids\n"
        % (comp.stats["moves"], comp.stats["segments"],
           comp.stats["arcs"], comp.stats["rapids"]))
    sys.stderr.write(
        "applymap: map span %.4f %s over %d points\n"
        % (span, units, len(hmap.xs) * len(hmap.ys)))
    if hmap.clamped:
        warn("%d point(s) fell outside the map and were clamped to its edge. "
             "Check that the probe grid covers the whole toolpath."
             % hmap.clamped)


if __name__ == "__main__":
    main()
