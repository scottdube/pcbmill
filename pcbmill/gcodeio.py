"""G-code reading and analysis used by the verification suite (FR7).

This is a *reader*: it never modifies files. It understands the subset of
RS-274 that pcb2gcode + post.py/postdrill.py/applymap.py emit.
"""

import math
import re

WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
COMMENT = re.compile(r"\([^()]*\)|;.*$")


def strip_comments(line):
    return COMMENT.sub(" ", line)


class GCodeAnalysis:
    """Single pass over a G-code file collecting everything verify needs."""

    def __init__(self, path):
        self.path = path
        self.units = None
        self.tools = []             # T numbers in order of appearance
        self.g43_h = []             # H numbers from G43 lines
        self.g37_h = []             # H numbers from G37 lines
        self.has_g54 = False
        self.has_g30 = False
        self.g30_count = 0
        self.m6_lines = []          # (lineno, preceding T number)
        self.bare_g01_feed = []     # lines matching ^G0?1 F.. with no axis
        self.cut_segments = []      # ((x0,y0,z0),(x1,y1,z1)) for G1 moves
        self.rapids = []            # same for G0
        self.plunges = []           # (x, y, zmin) - G1 Z-down at fixed XY
        self.min_z = None
        self.max_z = None
        self.cut_zs = []            # z values of cutting (G1) endpoints
        self.rapid_zs = []          # z of G0 endpoints that carry a Z word
        self.retract_zs = []        # z of G0/G1 upward Z-only moves
        self.header_comments = []
        self.clamped = None         # from applymap header, if present
        self.path_length_cut = 0.0
        self.path_length_rapid = 0.0
        self.feeds = []
        self._parse()

    def _parse(self):
        x = y = z = None
        motion = None
        last_t = None
        with open(self.path, errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.rstrip("\n")
                if line.strip().startswith("("):
                    self.header_comments.append(line.strip())
                    m = re.search(r"clamped\s*:\s*(\d+)", line)
                    if m:
                        self.clamped = int(m.group(1))
                    continue
                code = strip_comments(line)
                if not code.strip():
                    continue

                m = re.match(r"^\s*T(\d+)\s*$", code)
                if m:
                    last_t = int(m.group(1))
                    self.tools.append(last_t)
                    continue

                words = [(w.group(1).upper(), float(w.group(2)))
                         for w in WORD.finditer(code)]
                if not words:
                    continue
                d = {}
                gs, ms = [], []
                for k, v in words:
                    if k == "G":
                        gs.append(v)
                    elif k == "M":
                        ms.append(int(v))
                    else:
                        d[k] = v

                for g in gs:
                    gi = int(round(g))
                    if abs(g - gi) > 1e-9:
                        continue
                    if gi == 20:
                        self.units = "in"
                    elif gi == 21:
                        self.units = "mm"
                    elif gi == 54:
                        self.has_g54 = True
                    elif gi == 30:
                        self.has_g30 = True
                        self.g30_count += 1
                    elif gi == 43:
                        if "H" in d:
                            self.g43_h.append(int(d["H"]))
                    elif gi == 37:
                        if "H" in d:
                            self.g37_h.append(int(d["H"]))
                    elif gi in (0, 1, 2, 3):
                        motion = gi

                if 6 in ms:
                    self.m6_lines.append((lineno, last_t))

                # bare "G01 F.." is only the PathPilot tool-before-feed hazard
                # when it appears BEFORE the first tool block
                if ("F" in d and not any(k in d for k in "XYZ")
                        and last_t is None):
                    if any(int(round(g)) == 1 for g in gs if abs(g - round(g)) < 1e-9):
                        self.bare_g01_feed.append(lineno)
                if "F" in d:
                    self.feeds.append(d["F"])

                has_axis = any(k in d for k in "XYZ")
                if not has_axis:
                    continue
                is_move = any(int(round(g)) in (0, 1, 2, 3) for g in gs
                              if abs(g - round(g)) < 1e-9) or (gs == [] and motion is not None)
                if not is_move:
                    continue

                nx = d.get("X", x)
                ny = d.get("Y", y)
                nz = d.get("Z", z)

                if nz is not None:
                    self.min_z = nz if self.min_z is None else min(self.min_z, nz)
                    self.max_z = nz if self.max_z is None else max(self.max_z, nz)

                seg = None
                if x is not None and nx is not None:
                    seg = ((x, y, z), (nx, ny, nz))

                xy_same = (x is not None and nx is not None
                           and y is not None and ny is not None
                           and abs(nx - x) < 1e-6 and abs(ny - y) < 1e-6)
                if motion == 1:
                    if nz is not None:
                        self.cut_zs.append(nz)
                    if seg and None not in (seg[0][1], seg[1][1]):
                        self.cut_segments.append(seg)
                        self.path_length_cut += _seg_len(seg)
                    # plunge: Z decreased, XY numerically unchanged (the XY
                    # words may still be present - applymap re-emits them)
                    if (z is not None and nz is not None and nz < z - 1e-9
                            and (xy_same or ("X" not in d and "Y" not in d))):
                        self.plunges.append((nx, ny, nz))
                    if (z is not None and nz is not None and nz > z + 1e-9
                            and (xy_same or ("X" not in d and "Y" not in d))):
                        self.retract_zs.append(nz)
                elif motion == 0:
                    if "Z" in d:
                        self.rapid_zs.append(nz)
                        if (z is not None and nz > z + 1e-9
                                and (xy_same or ("X" not in d and "Y" not in d))):
                            self.retract_zs.append(nz)
                    if seg and None not in (seg[0][1], seg[1][1]):
                        self.rapids.append(seg)
                        self.path_length_rapid += _seg_len(seg)

                x, y, z = nx, ny, nz

    # ------------------------------------------------------------ queries

    def xy_extents(self, cutting_only=True):
        """Extents of endpoints of (cutting) moves plus plunge points."""
        pts = []
        for a, b in (self.cut_segments if cutting_only
                     else self.cut_segments + self.rapids):
            for p in (a, b):
                if p[0] is not None and p[1] is not None:
                    pts.append((p[0], p[1]))
        for px, py, _ in self.plunges:
            if px is not None and py is not None:
                pts.append((px, py))
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def deep_plunges(self, below=0.0):
        return [p for p in self.plunges if p[2] is not None and p[2] < below]

    def plunge_x_histogram(self, bin_size=0.001):
        hist = {}
        for px, _, _ in self.plunges:
            key = round(round(px / bin_size) * bin_size, 6)
            hist[key] = hist.get(key, 0) + 1
        return hist

    def cutting_z_range(self):
        if not self.cut_zs:
            return None
        neg = [v for v in self.cut_zs]
        return (min(neg), max(neg))


def _seg_len(seg):
    (x0, y0, z0), (x1, y1, z1) = seg
    dx = (x1 - x0) if None not in (x0, x1) else 0.0
    dy = (y1 - y0) if None not in (y0, y1) else 0.0
    dz = (z1 - z0) if None not in (z0, z1) else 0.0
    return math.hypot(dx, dy, dz)


# ------------------------------------------------------------ comment lint

def lint_comments(path):
    """FR7.7: balanced parens, no nesting, O-word pairing.

    Returns a list of problem strings (empty = clean).
    """
    problems = []
    o_stack = []
    with open(path, errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            depth = 0
            for ch in line:
                if ch == "(":
                    depth += 1
                    if depth > 1:
                        problems.append(
                            "line %d: nested parens (PathPilot rejects them)" % lineno)
                        break
                elif ch == ")":
                    depth -= 1
                    if depth < 0:
                        problems.append("line %d: unmatched ')'" % lineno)
                        break
            if depth > 0:
                problems.append("line %d: unclosed '('" % lineno)

            code = strip_comments(line).strip()
            m = re.match(r"^[oO](\d+)\s+(\w+)", code)
            if m:
                num, kw = m.group(1), m.group(2).lower()
                if kw in ("while", "if", "sub", "do", "repeat"):
                    o_stack.append((num, kw, lineno))
                elif kw.startswith("end"):
                    if not o_stack:
                        problems.append("line %d: O%s %s with no opener"
                                        % (lineno, num, kw))
                    else:
                        onum, okw, oline = o_stack.pop()
                        if onum != num:
                            problems.append(
                                "line %d: O%s %s closes O%s opened line %d"
                                % (lineno, num, kw, onum, oline))
                # else/elseif leave the stack alone
    for onum, okw, oline in o_stack:
        problems.append("line %d: O%s %s never closed" % (oline, onum, okw))
    return problems
