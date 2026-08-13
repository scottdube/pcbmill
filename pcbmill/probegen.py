"""Probe integration (FR4).

- Generates the smartprobe_pcb.ngc job program: the vendored motion section
  is FROZEN (it contains the four required workarounds #12 #13 #14 #15);
  only the configuration block and its describing comments are generated,
  and the comments are derived from the same numbers so they can never go
  stale. No nested parens (PathPilot rejects them).
- Imports probe-results.ngc and computes the QA numbers: span, plane fit,
  residual RMS, and LOCAL ROUGHNESS (the void/bond-failure metric).
- Touch-off assistant: highest map node (fail-safe direction) + validation
  nodes with expected DRO readings.
"""

import math
import os
import re

VENDOR = os.path.join(os.path.dirname(__file__), "vendor")
FROZEN_SOURCE = os.path.join(VENDOR, "smartprobe_pcb.ngc")

CONFIG_START = "(Configuration section)"
CONFIG_END = "(End configuration section)"


def _floor01(v):
    """Floor to the 0.01 grid.

    FLOORING, not rounding. A step rounded UP overshoots the usable area,
    the count guard below then drops a whole row to compensate, and the
    board loses far more coverage than the fraction of a hundredth that
    rounding up was worth. Measured on deskv1 (70 mm blank): 0.2656 rounded
    to 0.27, 11 rows spanned 2.70 > 2.656 usable, the guard cut it to 10
    rows spanning 2.43, and 0.086 in / 2.2 mm of copper ended up outside
    the probe map. Flooring to 0.26 keeps 11 rows spanning 2.60 - fits,
    covers the copper, and hits the target point count exactly.
    """
    return math.floor(v * 100 + 1e-9) / 100


def plan_grid(x0, y0, width, height, inset, target_points, copper=None):
    """Choose a grid (start/step/count in X and Y) covering the inset blank
    with roughly target_points, aspect-matched, steps floored to 0.01.

    copper: optional (x0, y0, x1, y1) copper extents in G54 inches, as
    measured by solve_offsets. When given, the grid is grown to cover the
    copper and it is an ERROR for it not to - a grid sized only against the
    blank has no idea where the board actually is, which is how the top edge
    went uncovered while a row was wasted below the copper.
    """
    ux = width - 2 * inset
    uy = height - 2 * inset
    if ux <= 0 or uy <= 0:
        raise ValueError("inset leaves no probe area")
    aspect = ux / uy
    ny = max(2, int(round(math.sqrt(target_points / aspect))))
    nx = max(2, int(round(target_points / ny)))
    sx = _floor01(ux / (nx - 1))
    sy = _floor01(uy / (ny - 1))
    if sx <= 0 or sy <= 0:
        raise ValueError("blank too small for a 0.01 in probe grid")
    # Flooring means the span can only be <= usable, so these are backstops
    # for degenerate inputs, no longer the normal path.
    while nx > 2 and sx * (nx - 1) > ux + 1e-9:
        nx -= 1
    while ny > 2 and sy * (ny - 1) > uy + 1e-9:
        ny -= 1

    gx0, gy0 = x0 + inset, y0 + inset
    if copper:
        cx0, cy0, cx1, cy1 = copper
        while (gx0 + sx * nx <= x0 + width - inset + 1e-9
               and gx0 + sx * (nx - 1) < cx1 - 1e-9):
            nx += 1
        while (gy0 + sy * ny <= y0 + height - inset + 1e-9
               and gy0 + sy * (ny - 1) < cy1 - 1e-9):
            ny += 1
        short = []
        if gx0 > cx0 + 1e-9:
            short.append("X low (grid starts %.4f, copper %.4f)" % (gx0, cx0))
        if gy0 > cy0 + 1e-9:
            short.append("Y low (grid starts %.4f, copper %.4f)" % (gy0, cy0))
        if gx0 + sx * (nx - 1) < cx1 - 1e-9:
            short.append("X high (grid ends %.4f, copper %.4f)"
                         % (gx0 + sx * (nx - 1), cx1))
        if gy0 + sy * (ny - 1) < cy1 - 1e-9:
            short.append("Y high (grid ends %.4f, copper %.4f)"
                         % (gy0 + sy * (ny - 1), cy1))
        if short:
            raise ValueError(
                "probe grid cannot cover the copper: %s\n"
                "  the blank dimensions or probe.inset are wrong - the map "
                "must span every cut, or verify check 1 fails on OUTSIDE "
                "PROBE MAP" % "; ".join(short))

    return {
        "x_start": round(gx0, 3), "x_step": sx, "x_count": nx,
        "y_start": round(gy0, 3), "y_step": sy, "y_count": ny,
    }


def generate_program(probe_params, out_path, log_name="probe-results.ngc"):
    """Emit the full probe program: generated config + frozen motion."""
    p = probe_params
    for k in ("x_start", "x_step", "x_count", "y_start", "y_step", "y_count"):
        if p.get(k) is None:
            raise ValueError("probe grid parameter %s is not set" % k)

    with open(FROZEN_SOURCE) as fh:
        src = fh.read()
    try:
        pre, rest = src.split(CONFIG_START, 1)
        _, frozen = rest.split(CONFIG_END, 1)
    except ValueError:
        raise RuntimeError("vendored smartprobe program lost its config markers")

    x_end = p["x_start"] + p["x_step"] * (p["x_count"] - 1)
    y_end = p["y_start"] + p["y_step"] * (p["y_count"] - 1)
    n = p["x_count"] * p["y_count"]

    # Header comments are DERIVED from the numbers below them - they cannot
    # disagree (stale comments caused real confusion). No nested parens.
    # The Z-zero line is the one piece of prose here and is therefore the one
    # thing that CAN go stale: it said "zero on the HIGHEST point of the area"
    # long after the workflow moved to zeroing on the fixture, contradicting
    # the vendored header a few lines below it in the same emitted file. Keep
    # it in step with vendor/smartprobe_pcb.ngc.
    cfg = [
        CONFIG_START,
        "G20   (Inches)",
        "F10    (probe speed)",
        "#1=%g    (X start)" % p["x_start"],
        "#2=%g    (X increment)" % p["x_step"],
        "#3=%d    (X count)" % p["x_count"],
        "#4=%g    (Y start)" % p["y_start"],
        "#5=%g    (Y increment)" % p["y_step"],
        "#6=%d    (Y count)" % p["y_count"],
        "#7=%g   (Z safety, above WCS Z0)" % p["z_safe"],
        "#8=%g  (Z probe depth limit, below WCS Z0)" % p["z_limit"],
        "(GENERATED by pcbmill - grid derived from the job blank placement)",
        "(  X: %.3f to %.3f in %d steps of %.3f)"
        % (p["x_start"], x_end, p["x_count"] - 1, p["x_step"]),
        "(  Y: %.3f to %.3f in %d steps of %.3f)"
        % (p["y_start"], y_end, p["y_count"] - 1, p["y_step"]),
        "(  %d points total. Log file: %s - bare name, PathPilot ini dir)"
        % (n, log_name),
        "(  Zero Z on the FIXTURE the work is bonded to - NOT on the work.)",
        "(  The map then holds real heights above that zero; #8 sits below.)",
        CONFIG_END,
    ]
    text = pre + "\n".join(cfg) + frozen
    with open(out_path, "w") as fh:
        fh.write(text)
    return {"points": n, "x_end": round(x_end, 4), "y_end": round(y_end, 4)}


# ---------------------------------------------------------------- map QA

LOGLINE = re.compile(
    r"^\s*G0*1\s+X\s*(-?[\d.]+)\s+Y\s*(-?[\d.]+)\s+Z\s*(-?[\d.]+)",
    re.IGNORECASE)


class ProbeMap:
    def __init__(self, path, tol=1e-4):
        self.path = path
        pts = []
        with open(path, errors="replace") as fh:
            for line in fh:
                m = LOGLINE.match(line)
                if m:
                    pts.append(tuple(float(g) for g in m.groups()))
        if len(pts) < 4:
            raise ValueError("found %d probe points in %s, need at least 4"
                             % (len(pts), path))
        self.points = pts
        xs = sorted({round(p[0], 4) for p in pts})
        ys = sorted({round(p[1], 4) for p in pts})
        self.xs = _uniq(xs, tol)
        self.ys = _uniq(ys, tol)
        self.grid = {}
        for x, y, z in pts:
            ix = _nearest(self.xs, x)
            iy = _nearest(self.ys, y)
            self.grid[(ix, iy)] = z
        self.missing = [(ix, iy) for iy in range(len(self.ys))
                        for ix in range(len(self.xs))
                        if (ix, iy) not in self.grid]

    @property
    def extents(self):
        return (self.xs[0], self.ys[0], self.xs[-1], self.ys[-1])

    @property
    def zmin(self):
        return min(z for _, _, z in self.points)

    @property
    def zmax(self):
        return max(z for _, _, z in self.points)

    @property
    def zmean(self):
        return sum(z for _, _, z in self.points) / len(self.points)

    def plane_fit(self):
        """Least-squares z = a*x + b*y + c. Returns a, b, c, residual RMS."""
        n = len(self.points)
        sx = sy = sz = sxx = syy = sxy = sxz = syz = 0.0
        for x, y, z in self.points:
            sx += x; sy += y; sz += z
            sxx += x * x; syy += y * y; sxy += x * y
            sxz += x * z; syz += y * z
        # solve the 3x3 normal equations
        A = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
        B = [sxz, syz, sz]
        a, b, c = _solve3(A, B)
        rss = sum((z - (a * x + b * y + c)) ** 2 for x, y, z in self.points)
        return a, b, c, math.sqrt(rss / n)

    def roughness(self):
        """Each point vs the mean of its 4 grid neighbours.
        Returns (rms, worst_value, worst_point)."""
        devs = []
        worst = (0.0, None)
        for (ix, iy), z in self.grid.items():
            nb = [self.grid.get(k) for k in
                  ((ix - 1, iy), (ix + 1, iy), (ix, iy - 1), (ix, iy + 1))]
            nb = [v for v in nb if v is not None]
            if len(nb) < 2:
                continue
            d = z - sum(nb) / len(nb)
            devs.append(d)
            if abs(d) > abs(worst[0]):
                worst = (d, (self.xs[ix], self.ys[iy]))
        if not devs:
            return 0.0, 0.0, None
        rms = math.sqrt(sum(d * d for d in devs) / len(devs))
        return rms, worst[0], worst[1]

    def qa(self, roughness_rms_limit=0.0005, roughness_point_limit=0.0015):
        a, b, c, rms = self.plane_fit()
        rrms, rworst, rwhere = self.roughness()
        flags = []
        if abs(rrms) > roughness_rms_limit:
            flags.append("local roughness RMS %.4f in exceeds %.4f - possible "
                         "voids or bond failure; do not cut"
                         % (rrms, roughness_rms_limit))
        if abs(rworst) > roughness_point_limit:
            flags.append("point at X%.3f Y%.3f is %.4f in off its neighbours "
                         "(limit %.4f) - re-probe or investigate"
                         % (rwhere[0], rwhere[1], rworst, roughness_point_limit))
        if self.missing:
            flags.append("%d grid cells have no probe point" % len(self.missing))
        return {
            "points": len(self.points),
            "grid": [len(self.xs), len(self.ys)],
            "extents": self.extents,
            "z": {"min": self.zmin, "max": self.zmax, "mean": self.zmean,
                  "span": self.zmax - self.zmin},
            "plane": {"tilt_x_in_per_in": a, "tilt_y_in_per_in": b,
                      "residual_rms": rms},
            "roughness": {"rms": rrms, "worst": rworst,
                          "worst_at": rwhere},
            "flags": flags,
            "ok": not flags,
        }

    # ---------------------------------------------------------- touch-off

    def touch_off(self, count_validation=2):
        """FR4.4: highest node (fail-safe), plus validation nodes."""
        nodes = sorted(((z, self.xs[ix], self.ys[iy])
                        for (ix, iy), z in self.grid.items()), reverse=True)
        z, x, y = nodes[0]
        picks = [{"x": x, "y": y, "z": z, "role": "touch-off (highest node)"}]
        # validation: the lowest node and the node nearest the map centre
        lo_z, lo_x, lo_y = nodes[-1]
        picks.append({"x": lo_x, "y": lo_y, "z": lo_z,
                      "role": "validation (lowest node)"})
        cx = (self.xs[0] + self.xs[-1]) / 2
        cy = (self.ys[0] + self.ys[-1]) / 2
        mid = min(nodes, key=lambda n: (n[1] - cx) ** 2 + (n[2] - cy) ** 2)
        if count_validation > 1 and (mid[1], mid[2]) not in ((x, y), (lo_x, lo_y)):
            picks.append({"x": mid[1], "y": mid[2], "z": mid[0],
                          "role": "validation (centre node)"})
        return {
            "instructions": [
                "Jog to X%.4f Y%.4f, electrical touch-off, type %.4f into TOUCH Z"
                % (x, y, z),
            ] + [
                "Validate: at X%.4f Y%.4f the DRO should read Z %.4f when touching"
                % (p["x"], p["y"], p["z"]) for p in picks[1:]
            ],
            "nodes": picks,
        }


def _uniq(vals, tol):
    out = []
    for v in sorted(vals):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def _nearest(arr, v):
    return min(range(len(arr)), key=lambda i: abs(arr[i] - v))


def _solve3(A, B):
    import copy
    a = [row[:] + [b] for row, b in zip(copy.deepcopy(A), B)]
    n = 3
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(a[r][i]))
        a[i], a[piv] = a[piv], a[i]
        if abs(a[i][i]) < 1e-12:
            raise ValueError("degenerate plane fit")
        for r in range(n):
            if r != i:
                f = a[r][i] / a[i][i]
                for c_ in range(i, n + 1):
                    a[r][c_] -= f * a[i][c_]
    return [a[i][3] / a[i][i] for i in range(n)]
