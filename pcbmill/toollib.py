"""Tool library, V-bit kerf math, chipload feeds, drill consolidation (FR3)."""

import json
import math
import os

MM_PER_IN = 25.4

DEFAULT_LIBRARY_PATH = os.path.expanduser("~/.pcbmill/tools.json")

# Shipped defaults - the tools that cut board 1.
DEFAULT_TOOLS = [
    {
        "id": "vbit-60-005",
        "kind": "vbit",
        "label": "60 deg V-bit, 0.005 tip",
        "tip_diameter": 0.005,
        "included_angle": 60.0,
        "flutes": 2,
        "t_number": 600,
        "chipload": 0.000455,     # in/tooth -> 9 IPM at 10k RPM x 2 flutes
        "touch_off": "electrical",
    },
    {"id": "drill-032", "kind": "drill", "label": '#66 drill 0.032"',
     "diameter": 0.032, "flutes": 2, "t_number": 605, "touch_off": "ets"},
    {"id": "drill-0465", "kind": "drill", "label": '#56 drill 0.0465"',
     "diameter": 0.0465, "flutes": 2, "t_number": 606, "touch_off": "ets"},
    {"id": "drill-0625", "kind": "drill", "label": '1/16" drill',
     "diameter": 0.0625, "flutes": 2, "t_number": 607, "touch_off": "ets"},
    {"id": "drill-125", "kind": "drill", "label": '1/8" drill',
     "diameter": 0.125, "flutes": 2, "t_number": 608, "touch_off": "ets"},
    {"id": "endmill-125", "kind": "endmill", "label": '1/8" flat end mill',
     "diameter": 0.125, "flutes": 2, "t_number": 610, "touch_off": "electrical"},
]


class ToolLibrary:
    def __init__(self, path=DEFAULT_LIBRARY_PATH):
        self.path = path
        if os.path.exists(path):
            with open(path) as fh:
                self.tools = json.load(fh)
        else:
            self.tools = [dict(t) for t in DEFAULT_TOOLS]
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.tools, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)

    def get(self, tool_id):
        for t in self.tools:
            if t["id"] == tool_id:
                return t
        return None

    def upsert(self, tool):
        for i, t in enumerate(self.tools):
            if t["id"] == tool["id"]:
                self.tools[i] = tool
                break
        else:
            self.tools.append(tool)
        self.save()

    def delete(self, tool_id):
        self.tools = [t for t in self.tools if t["id"] != tool_id]
        self.save()

    def drills(self):
        return [t for t in self.tools if t["kind"] == "drill"]

    def atc_hazards(self, manual_min=600):
        """Tools whose T-number would fire the ATC instead of prompting."""
        return [t for t in self.tools if t.get("t_number", 999) < manual_min]


# ---------------------------------------------------------------- kerf math

def vbit_kerf(tip_diameter, included_angle_deg, depth):
    """Cut width of a V-bit at |depth|:  width = tip + 2*tan(angle/2)*depth."""
    d = abs(depth)
    return tip_diameter + 2.0 * math.tan(math.radians(included_angle_deg / 2.0)) * d


def mill_diameter_for(tool, zwork):
    """The mill-diameters value pcb2gcode should be given for this tool at
    this zwork. Keeping these two consistent is FR3.2 - they were once
    inconsistent and would have eaten every trace."""
    if tool["kind"] == "vbit":
        return round(vbit_kerf(tool["tip_diameter"], tool["included_angle"], zwork), 4)
    return tool["diameter"]


# ---------------------------------------------------------------- feeds

def feed_from_chipload(chipload, flutes, rpm):
    """IPM = chipload (in/tooth) x flutes x RPM."""
    return chipload * flutes * rpm


def chipload_from_feed(feed_ipm, flutes, rpm):
    if flutes <= 0 or rpm <= 0:
        return None
    return feed_ipm / (flutes * rpm)


def check_feed(feed_ipm, flutes, rpm, chipload_min=0.0002, chipload_max=0.001):
    """Returns (chipload, warning|None) per FR3.3."""
    c = chipload_from_feed(feed_ipm, flutes, rpm)
    if c is None:
        return None, "cannot compute chipload (flutes/rpm zero)"
    if c < chipload_min:
        return c, ("chipload %.6f in/tooth < %.4f - rubbing, will dull the tool"
                   % (c, chipload_min))
    if c > chipload_max:
        return c, ("chipload %.6f in/tooth > %.4f - heavy for PCB work"
                   % (c, chipload_max))
    return c, None


# ------------------------------------------------------- drill consolidation

def consolidate_drills(drill_tools, available, undersize_limit=0.002):
    """FR3.4. drill_tools: {excellon_tool: nominal_dia_in}. available: list of
    owned bit diameters. pcb2gcode's drills-available rounds to NEAREST, not
    up - so we mirror that and report the signed error.

    Returns a list of dicts, one per Excellon tool.
    """
    out = []
    for t in sorted(drill_tools):
        nominal = drill_tools[t]
        best = min(available, key=lambda a: abs(a - nominal)) if available else None
        err = (best - nominal) if best is not None else None
        flag = None
        if best is None:
            flag = "no drills available"
        elif err < -undersize_limit:
            flag = ("bit is %.4f\" SMALLER than nominal - hole will be "
                    "undersized (pcb2gcode rounds to nearest, not up)" % -err)
        out.append({
            "excellon_tool": t,
            "nominal_in": nominal,
            "chosen_in": best,
            "error_in": err,
            "flag": flag,
        })
    return out


def annulus_check(pads_with_drills, mapping, annulus_min_mm=0.25):
    """pads_with_drills: list of
    {"pad_size_mm": (w,h), "drill_mm": d, "ref": .., "type": ..}
    mapping: consolidate_drills output used to translate nominal->actual bit.
    With no plating the annular ring is all there is: (pad - drill)/2 >= min.

    NPTH pads are SKIPPED. KiCad writes np_thru_hole pads with size == drill
    (mounting holes, flash-pin clearance holes), so the computed ring is ~0 by
    construction and every such hole would report a failure on an otherwise
    healthy board. Filtering here rather than in each caller: the CLI and the
    web UI had already drifted apart on this, and a check that cries wolf
    trains people to ignore it.
    """
    by_nominal = {round(m["nominal_in"] * MM_PER_IN, 3): m for m in mapping}
    problems = []
    for p in pads_with_drills:
        if p.get("type") == "np_thru_hole":
            continue
        d_nom = round(p["drill_mm"], 3)
        m = by_nominal.get(d_nom)
        actual_mm = (m["chosen_in"] * MM_PER_IN) if m and m["chosen_in"] else p["drill_mm"]
        pad_min = min(p["pad_size_mm"])
        ring = (pad_min - actual_mm) / 2.0
        if ring < annulus_min_mm:
            problems.append({
                "ref": p.get("ref"),
                "pad_mm": p["pad_size_mm"],
                "drill_nominal_mm": p["drill_mm"],
                "drill_actual_mm": actual_mm,
                "annulus_mm": round(ring, 3),
                "required_mm": annulus_min_mm,
            })
    return problems
