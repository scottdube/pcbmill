"""Excellon drill file parsing (FR2).

Handles the KiCad merged-drill dialect: FMAT,2, INCH/METRIC, decimal
coordinates, T<n>C<dia> tool definitions, MixedPlating check.
"""

import re

MM_PER_IN = 25.4


class DrillFile:
    def __init__(self, path):
        self.path = path
        self.units = None                # "in" or "mm"
        self.tools = {}                  # tool number -> diameter (inches)
        self.tool_plating = {}           # tool number -> "PTH"/"NPTH"/None
        self.holes = []                  # (tool, x_in, y_in)
        self.file_functions = []         # TF.FileFunction values
        self.warnings = []
        self._parse()

    # ------------------------------------------------------------ parsing

    def _parse(self):
        header = True
        cur_tool = None
        pending_attr = None
        fmt_decimal = True
        with open(self.path, errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(";"):
                    m = re.search(r"TF\.FileFunction,(.*)", line)
                    if m:
                        self.file_functions.append(m.group(1).strip())
                    m = re.search(r"TA\.AperFunction,(\w+),(\w+)", line)
                    if m:
                        pending_attr = m.group(2)
                    if "decimal" not in line and "FORMAT=" in line:
                        fmt_decimal = "decimal" in line
                    continue
                if line in ("M48",):
                    continue
                if line in ("%", "M95"):
                    header = False
                    continue
                if line in ("INCH", "M72"):
                    self.units = "in"
                    continue
                if line in ("METRIC", "M71"):
                    self.units = "mm"
                    continue
                if line.startswith("FMAT"):
                    continue
                m = re.match(r"^T(\d+)C([\d.]+)", line)
                if m and header:
                    t = int(m.group(1))
                    dia = float(m.group(2))
                    self.tools[t] = self._to_in(dia)
                    self.tool_plating[t] = pending_attr
                    pending_attr = None
                    continue
                m = re.match(r"^T(\d+)\s*$", line)
                if m:
                    t = int(m.group(1))
                    if t == 0:
                        cur_tool = None
                    elif t in self.tools:
                        cur_tool = t
                    else:
                        self.warnings.append("T%d used but never defined" % t)
                    continue
                m = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)", line)
                if m:
                    if cur_tool is None:
                        self.warnings.append("hole before any tool select: %s" % line)
                        continue
                    if not fmt_decimal:
                        self.warnings.append(
                            "non-decimal coordinate format; parse may be wrong")
                    x = self._to_in(float(m.group(1)))
                    y = self._to_in(float(m.group(2)))
                    self.holes.append((cur_tool, x, y))
                    continue
                if re.match(r"^(G0?5|G90|M30|M2\b)", line):
                    continue

        if self.units is None:
            self.warnings.append("no INCH/METRIC directive found; assuming inches")
            self.units = "in"

    def _to_in(self, v):
        return v / MM_PER_IN if self.units == "mm" else v

    # ------------------------------------------------------------ queries

    @property
    def is_mixed_plating(self):
        return any("MixedPlating" in f for f in self.file_functions)

    @property
    def declared_split(self):
        """True if the header says this is a PTH-only or NPTH-only file."""
        joined = " ".join(self.file_functions)
        return ("NonPlated" in joined or ",PTH" in joined) and not self.is_mixed_plating

    def hole_count(self, tool=None):
        if tool is None:
            return len(self.holes)
        return sum(1 for t, _, _ in self.holes if t == tool)

    def extents(self):
        if not self.holes:
            return None
        xs = [x for _, x, _ in self.holes]
        ys = [y for _, _, y in self.holes]
        return (min(xs), min(ys), max(xs), max(ys))

    def summary(self):
        ext = self.extents()
        return {
            "units": self.units,
            "mixed_plating": self.is_mixed_plating,
            "file_functions": self.file_functions,
            "tools": [
                {
                    "tool": t,
                    "diameter_in": self.tools[t],
                    "diameter_mm": self.tools[t] * MM_PER_IN,
                    "plating": self.tool_plating.get(t),
                    "holes": self.hole_count(t),
                }
                for t in sorted(self.tools)
            ],
            "hole_count": len(self.holes),
            "extents_in": ext,
            "span_in": (ext[2] - ext[0], ext[3] - ext[1]) if ext else None,
            "warnings": self.warnings,
        }

    def x_histogram(self, bin_size=0.001):
        """Column histogram of hole X positions: {binned_x: count}."""
        hist = {}
        for _, x, _ in self.holes:
            key = round(x / bin_size) * bin_size
            hist[round(key, 6)] = hist.get(round(key, 6), 0) + 1
        return hist
