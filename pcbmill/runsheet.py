"""Run sheet (FR9): printable per-job operator document."""

import os
import time

from .gcodeio import GCodeAnalysis
from .probegen import ProbeMap


def build(job):
    """Returns the run sheet as a dict; render with to_text()/to_html()."""
    p = job.params
    cam = p["cam"]
    tools = p["tools"]
    m = p["machine"]
    corner = m["runtime_corner_factor"]

    ops = []
    probe_prog = job.artifact_path("probe_program")
    if os.path.exists(probe_prog):
        ops.append({
            "op": "Probe the blank",
            "file": os.path.basename(probe_prog),
            "sha256": (job.data["artifacts"].get("probe_program") or {}).get("sha256", "-"),
            "tool": "Probe", "t_number": m["probe_tool"],
            "touch_off": "probe loaded as T%d/H%d" % (m["probe_tool"], m["probe_h"]),
            "runtime_min": None,
            "note": "run smartprobe, then IMPORT probe-results.ngc into the job",
        })

    def add_gcode_op(art, label, tool_label, t_number, touch_off, feed):
        path = job.artifact_path(art)
        if not os.path.exists(path):
            return
        rec = job.data["artifacts"].get(art) or {}
        ana = GCodeAnalysis(path)
        runtime = (ana.path_length_cut / feed * corner * 60.0) if feed else None
        ops.append({
            "op": label,
            "file": os.path.basename(path),
            "sha256": rec.get("sha256", "UNRECORDED"),
            "tool": tool_label, "t_number": t_number,
            "touch_off": touch_off,
            "runtime_min": round(runtime / 60.0, 1) if runtime else None,
            "note": "",
        })

    add_gcode_op("back_comp", "Isolation milling",
                 tools["isolation_tool"], tools["isolation_t_number"],
                 "ELECTRICAL touch-off - MANDATORY for the isolation cutter "
                 "(ETS reads +%.1f thou high vs work frame)"
                 % (m["ets_frame_offset_in"] * 1000),
                 cam["mill_feed"])
    add_gcode_op("drill_comp", "Drilling",
                 "drills T%d+" % tools["drill_t_base"], tools["drill_t_base"],
                 "G37/ETS in-program (acceptable for drills)",
                 cam["drill_feed"])
    if cam.get("outline_enabled"):
        add_gcode_op("outline_comp", "Outline / edge cuts",
                     "end mill", tools["outline_t_number"],
                     "electrical touch-off", cam["outline_feed"])

    touch = None
    if job.probe_map_path and os.path.exists(job.probe_map_path):
        try:
            touch = ProbeMap(job.probe_map_path).touch_off()
        except ValueError:
            touch = None

    preflight = [
        "Tool length is NOT 0.0000 for every tool in the job",
        "G54 unchanged since probing (do not re-touch-off between ops)",
        "#7 (probe Z safety, %.3f) clears the work and clamps" % p["probe"]["z_safe"],
        "Probe loaded as T%d for probing ops" % m["probe_tool"],
        "ETS position set + spindle nose referenced since last Z reference "
        "(required by G37 in the drill program)",
        "Verification suite is GREEN (job ready)",
    ]

    return {
        "job": job.data["name"],
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "ready": bool((job.data.get("verification") or {}).get("ready")),
        "ops": ops,
        "touch_off": touch,
        "preflight": preflight,
    }


def to_text(sheet):
    L = []
    L.append("RUN SHEET - %s        (generated %s)" % (sheet["job"], sheet["generated"]))
    L.append("job ready: %s" % ("YES" if sheet["ready"] else "NO - DO NOT CUT"))
    L.append("")
    L.append("OPERATIONS (in order)")
    for i, op in enumerate(sheet["ops"], 1):
        L.append("%d. %s" % (i, op["op"]))
        L.append("   file      : %s" % op["file"])
        L.append("   sha256    : %s" % op["sha256"][:16])
        L.append("   tool      : %s   T%s" % (op["tool"], op["t_number"]))
        L.append("   touch-off : %s" % op["touch_off"])
        if op["runtime_min"]:
            L.append("   runtime   : ~%.0f min (path/feed x %s corner factor)"
                     % (op["runtime_min"], 1.4))
        if op["note"]:
            L.append("   note      : %s" % op["note"])
        L.append("")
    if sheet["touch_off"]:
        L.append("TOUCH-OFF (from the probe map - fail-safe highest node)")
        for line in sheet["touch_off"]["instructions"]:
            L.append("   " + line)
        L.append("")
    L.append("PRE-FLIGHT")
    for item in sheet["preflight"]:
        L.append("   [ ] " + item)
    L.append("")
    return "\n".join(L)
