"""The verification suite (FR7) - the reason this app exists.

Runs automatically after every regeneration; "job ready" is blocked on
failures. Every check is here because its absence bit us on board 1.

Report shape:
    {"ran": ts, "ready": bool, "checks": [
        {"id": 1, "name": ..., "status": "PASS|WARN|FAIL|SKIP", "details": [..]},
        ...]}
"""

import os
import time

from . import sliver as sliver_mod
from .excellon import DrillFile
from .gcodeio import GCodeAnalysis, lint_comments
from .kicadpcb import Board
from .probegen import ProbeMap

MM_PER_IN = 25.4

# checks that only report and never block readiness
NON_BLOCKING = {10}


def _check(cid, name, status, details):
    return {"id": cid, "name": name, "status": status,
            "details": details if isinstance(details, list) else [details]}


def run(job, deep=True, log=lambda *a: None):
    """Run the whole suite. deep=False skips the sliver render (slow-ish)."""
    checks = []
    m = job.params["machine"]
    cam = job.params["cam"]

    drill_in = job.input_path("drill")
    dfile = DrillFile(drill_in) if drill_in else None
    pm = None
    if job.probe_map_path and os.path.exists(job.probe_map_path):
        try:
            pm = ProbeMap(job.probe_map_path)
        except ValueError:
            pm = None

    analyses = {}
    for name in ("back", "drillfile", "back_comp", "drill_comp",
                 "outline", "outline_comp"):
        p = job.artifact_path(name)
        state = job.artifact_state(name)
        if state in ("missing", "skipped") or not os.path.exists(p):
            continue
        analyses[name] = GCodeAnalysis(p)

    checks.append(_inputs_check(job, dfile))
    checks.append(_extents_check(job, analyses, pm))
    checks.append(_mirror_check(job, dfile, analyses))
    checks.append(_hole_count_check(dfile, analyses))
    checks.append(_tool_audit(job, analyses))
    checks.append(_z_audit(job, analyses, pm))
    checks.append(_clamp_check(analyses))
    checks.append(_lint_check(job, analyses))
    checks.append(_staleness_check(job))
    if deep:
        checks.append(_sliver_check(job, dfile, log))
    else:
        checks.append(_check(9, "Sliver prediction", "SKIP",
                             "skipped (quick verify) - run full verify"))
    checks.append(_intake_lint(job))
    checks.append(_frame_check(job, deep, log))

    ready = all(c["status"] in ("PASS", "WARN") or c["id"] in NON_BLOCKING
                for c in checks)
    # a job with no compensated files is never ready
    if "back_comp" not in analyses or "drill_comp" not in analyses:
        ready = False
        checks.append(_check(11, "Compensated files present", "FAIL",
                             "back_comp / drill_comp missing - import a probe "
                             "map and regenerate"))
    report = {"ran": time.strftime("%Y-%m-%dT%H:%M:%S"), "ready": ready,
              "checks": checks}
    job.data["verification"] = report
    job.save()
    return report


# ------------------------------------------------------------------ 0

def _inputs_check(job, dfile):
    """FR2: back Gerber present; ONE merged MixedPlating Excellon."""
    details, status = [], "PASS"
    if not job.input_path("gerber_back"):
        details.append("no back-copper Gerber set")
        status = "FAIL"
    else:
        details.append("gerber_back: %s" % job.data["inputs"]["gerber_back"]["file"])
    if dfile is None:
        details.append("no drill file set")
        status = "FAIL"
    else:
        if dfile.is_mixed_plating:
            details.append("drill: MixedPlating merged file, %d tools, %d holes"
                           % (len(dfile.tools), dfile.hole_count()))
        else:
            details.append("DRILL FILE IS NOT A MERGED MixedPlating EXPORT - "
                           "this looks like a split PTH/NPTH pair; drilling "
                           "from a split file loses half the holes. Export "
                           "ONE merged Excellon from KiCad.")
            status = "FAIL"
        if dfile.warnings:
            details.extend("drill parse: %s" % w for w in dfile.warnings)
            if status == "PASS":
                status = "WARN"
    return _check(0, "Inputs (back Gerber + merged Excellon)", status, details)


# ------------------------------------------------------------------ 1

def _extents_check(job, analyses, pm):
    blank = job.params["blank"]
    bx0, by0 = blank["x0"], blank["y0"]
    bx1, by1 = bx0 + blank["width"], by0 + blank["height"]
    details, status = [], "PASS"
    if not analyses:
        return _check(1, "Extents", "SKIP", "no generated files yet")
    for name, ana in analyses.items():
        ext = ana.xy_extents()
        if ext is None:
            details.append("%s: no motion found" % name)
            status = "FAIL"
            continue
        x0, y0, x1, y1 = ext
        line = "%s: X %.4f..%.4f  Y %.4f..%.4f" % (name, x0, x1, y0, y1)
        if x0 < bx0 - 1e-4 or x1 > bx1 + 1e-4 or y0 < by0 - 1e-4 or y1 > by1 + 1e-4:
            line += "  OUTSIDE BLANK (X %.3f..%.3f Y %.3f..%.3f)" % (bx0, bx1, by0, by1)
            status = "FAIL"
        if pm and name in ("back", "drillfile", "back_comp", "drill_comp"):
            mx0, my0, mx1, my1 = pm.extents
            if x0 < mx0 - 1e-4 or x1 > mx1 + 1e-4 or y0 < my0 - 1e-4 or y1 > my1 + 1e-4:
                line += "  OUTSIDE PROBE MAP (X %.3f..%.3f Y %.3f..%.3f)" % (
                    mx0, mx1, my0, my1)
                status = "FAIL"
        details.append(line)
    if pm is None:
        details.append("no probe map imported - map coverage not checked")
        if status == "PASS":
            status = "WARN"
    return _check(1, "Extents vs blank and probe map", status, details)


# ------------------------------------------------------------------ 2

def _mirror_check(job, dfile, analyses):
    if dfile is None:
        return _check(2, "Mirror proof", "SKIP", "no drill input")
    ana = analyses.get("drillfile") or analyses.get("drill_comp")
    if ana is None:
        return _check(2, "Mirror proof", "SKIP", "no drill G-code yet")
    details, status = [], "PASS"

    # span equality to 4 decimals
    ext_e = dfile.extents()
    ge = ana.xy_extents()
    span_e = round(ext_e[2] - ext_e[0], 4)
    span_g = round(ge[2] - ge[0], 4)
    details.append("X span: Excellon %.4f, G-code %.4f" % (span_e, span_g))
    if abs(span_e - span_g) > 1e-4 + 1e-9:
        details.append("SPAN MISMATCH - offsets or scaling are wrong")
        status = "FAIL"

    # histogram profile: G-code columns ascending must match Excellon
    # columns DESCENDING (the board is mirrored)
    he = dfile.x_histogram()
    hg = ana.plunge_x_histogram()
    prof_e = [c for _, c in sorted(he.items())]
    prof_g = [c for _, c in sorted(hg.items())]
    details.append("column profile: Excellon %s" % _short(prof_e))
    details.append("column profile: G-code   %s" % _short(prof_g))
    mirrored = prof_g == prof_e[::-1]
    unmirrored = prof_g == prof_e
    if mirrored and (not unmirrored):
        details.append("largest column group lands on the mirrored side - OK")
    elif mirrored and unmirrored:
        details.append("profile is symmetric; falling back to offset math")
    else:
        status = "FAIL"
        details.append("COLUMN PROFILE DOES NOT MATCH THE MIRRORED EXCELLON - "
                       "was the board generated with front= instead of back=?")

    # absolute positions of the highest-count column group, when the frame
    # is known (e.g. the 2x19 ESP rows - there can be several top columns)
    cam = job.params["cam"]
    if not cam.get("zero_start") and cam.get("x_offset") is not None and status != "FAIL":
        mx = -cam["x_offset"]
        top = max(he.values())
        exp_cols = sorted(round(mx - x, 3) for x, c in he.items() if c == top)
        got_cols = sorted(round(x, 3) for x, c in hg.items()
                          if c == max(hg.values()))
        details.append("top column group (count %d): expect G-code X %s, "
                       "found %s" % (top, exp_cols, got_cols))
        matched = (len(exp_cols) == len(got_cols) and
                   all(abs(a - b) <= 0.002 for a, b in zip(exp_cols, got_cols)))
        if not matched:
            status = "FAIL"
            details.append("MIRRORED POSITION MISMATCH")
    return _check(2, "Mirror proof (drill histogram)", status, details)


def _short(profile):
    s = ",".join(str(c) for c in profile)
    return s if len(s) <= 80 else s[:77] + "..."


# ------------------------------------------------------------------ 3

def _hole_count_check(dfile, analyses):
    if dfile is None:
        return _check(3, "Hole count", "SKIP", "no drill input")
    details, status = [], "PASS"
    expected = dfile.hole_count()
    any_file = False
    for name in ("drillfile", "drill_comp"):
        ana = analyses.get(name)
        if ana is None:
            continue
        any_file = True
        got = len(ana.deep_plunges(below=0.0))
        details.append("%s: %d plunges below Z0 vs %d Excellon holes"
                       % (name, got, expected))
        if got != expected:
            status = "FAIL"
    if not any_file:
        return _check(3, "Hole count", "SKIP", "no drill G-code yet")
    return _check(3, "Hole count (plunges == Excellon)", status, details)


# ------------------------------------------------------------------ 4

def _tool_audit(job, analyses):
    tools = job.params["tools"]
    manual_min = job.params["machine"]["manual_tool_min"]
    iso_t = tools["isolation_t_number"]
    base = tools["drill_t_base"]
    assigned = {iso_t, tools.get("outline_t_number")} | set(range(base, base + 8))
    details, status = [], "PASS"
    if not analyses:
        return _check(4, "Tool block audit", "SKIP", "no generated files yet")
    for name, ana in analyses.items():
        probs = []
        for t in ana.tools:
            if t not in assigned:
                probs.append("T%d not in the job's assigned set" % t)
            if t < manual_min:
                probs.append("T%d is BELOW %d - M6 will fire the ATC" % (t, manual_min))
        if not ana.has_g54:
            probs.append("no G54")
        if not ana.has_g30:
            probs.append("no G30 retract")
        for t in ana.tools:
            if t not in ana.g43_h:
                probs.append("T%d has no matching G43 H%d" % (t, t))
        # EVERY tool the program changes to must be MEASURED before its length
        # is applied, and the G37 must PRECEDE the G43 - presence alone is not
        # the invariant. This was previously required of drill programs only,
        # so an isolation or outline program that applied an unmeasured length
        # passed cleanly. The outline is the worst case: the profile cutter is
        # a different physical tool from every other op in the job, so a stale
        # length there is the previous tool's.
        # EXCEPT the isolation cutter: it is touched off ELECTRICALLY on the
        # copper (the ETS reads +4.1 thou off the work frame, and the cut is
        # 2 thou), so a G37 there OVERWRITES the touch-off with the wrong
        # number. G43 only, and any G37 for it is a FAIL.
        for t in ana.tools:
            if t == iso_t:
                if t in ana.g37_h:
                    probs.append("T%d has G37 H%d - overwrites the ELECTRICAL "
                                 "touch-off of the isolation cutter with an "
                                 "ETS reading" % (t, t))
                continue
            if t not in ana.g37_h:
                probs.append("T%d has no G37 H%d - applies an UNMEASURED "
                             "tool length" % (t, t))
                continue
            order = [op for op, h in ana.tool_len_ops if h == t]
            if order and order[0] != "G37":
                probs.append("T%d applies G43 H%d BEFORE measuring with "
                             "G37 H%d" % (t, t, t))
        if ana.bare_g01_feed:
            probs.append("bare 'G01 F..' at line(s) %s" %
                         ana.bare_g01_feed[:5])
        if probs:
            status = "FAIL"
            details.append("%s: %s" % (name, "; ".join(probs)))
        else:
            details.append("%s: T%s ok, G37-before-G43 (electrical for T%d) / G54 / G30 present"
                           % (name, ",".join(str(t) for t in ana.tools) or "-", iso_t))
    return _check(4, "Tool block audit", status, details)


# ------------------------------------------------------------------ 5

def _z_audit(job, analyses, pm):
    if pm is None:
        return _check(5, "Z audit", "SKIP", "no probe map imported")
    cam = job.params["cam"]
    m = job.params["machine"]
    details, status = [], "PASS"
    tol = 0.002

    ana = analyses.get("back_comp")
    if ana:
        lo_exp = pm.zmin + cam["zwork"]
        hi_exp = pm.zmax + cam["zwork"]
        if ana.cut_zs:
            lo, hi = min(ana.cut_zs), max(ana.cut_zs)
            details.append("back_comp cutting Z %.4f..%.4f (expect %.4f..%.4f)"
                           % (lo, hi, lo_exp, hi_exp))
            if lo < lo_exp - tol or hi > hi_exp + tol:
                status = "FAIL"
                details.append("CUTTING Z OUTSIDE map+zwork - wrong map or "
                               "mode? (relative-mode-by-accident cuts 70 thou "
                               "deep; uncompensated cuts air or full depth)")
        clearance_rapids = [z for z in ana.rapid_zs if z > pm.zmax]
        if clearance_rapids:
            exp = cam["zsafe"] + pm.zmean
            worst = max(abs(z - exp) for z in clearance_rapids)
            details.append("back_comp rapid Z vs zsafe+map mean %.4f: worst "
                           "deviation %.4f" % (exp, worst))
            if worst > tol:
                status = "FAIL"
        low_retract = [z for z in ana.retract_zs if z <= pm.zmax]
        if low_retract:
            status = "FAIL"
            details.append("back_comp: %d retracts do NOT clear the copper "
                           "(max map Z %.4f)" % (len(low_retract), pm.zmax))

    ana = analyses.get("drill_comp")
    if ana:
        # Z0 is the COPPER (electrical touch). The board bottom under each
        # hole is the local map height minus the blank thickness; depth is
        # audited from there. The backer is assumed to start at the board
        # bottom - any wax film puts it lower, so this errs safe.
        thick = job.params["blank"].get("thickness_in")
        bottoms = [(x, y, z) for x, y, z in ana.plunges if z < pm.zmin]
        if thick is None:
            status = "FAIL"
            details.append("blank.thickness_in is not set - the board bottom "
                           "is unknown, so drill depth cannot be audited")
        elif bottoms:
            below = [(pm.z_at(x, y) - thick) - z for x, y, z in bottoms]
            need_min = m["drill_breakthrough_min_in"]
            need_max = -m["backer_damage_limit_in"]
            shallow = [d for d in below if d < need_min]
            deep = [d for d in below if d > need_max]
            details.append("drill_comp %d holes bottom %.4f..%.4f below board "
                           "bottom (local copper - %.4f blank); need >= %.4f "
                           "and <= %.4f (backer assumed at board bottom)"
                           % (len(bottoms), min(below), max(below), thick,
                              need_min, need_max))
            if shallow:
                status = "FAIL"
                details.append("%d hole(s) do NOT break through the board by "
                               ">= %g in" % (len(shallow), need_min))
            if deep:
                status = "FAIL"
                details.append("%d hole(s) plunge into backer-damage territory "
                               "(> %g in below board bottom)"
                               % (len(deep), need_max))
        low_retract = [z for z in ana.retract_zs if z <= pm.zmax]
        if low_retract:
            status = "FAIL"
            details.append("drill_comp: %d retracts below copper" % len(low_retract))

    # probe safety height (#7) must clear the copper: probing a board whose
    # copper sits above #7 crashes the stylus on the first traverse
    z_safe = job.params["probe"]["z_safe"]
    if z_safe <= pm.zmax + 0.05:
        status = "FAIL"
        details.append("probe z_safe (#7) %.3f does NOT clear the copper "
                       "(map max %.4f) by 0.050 - fix probe.z_safe"
                       % (z_safe, pm.zmax))
    else:
        details.append("probe z_safe (#7) %.3f clears copper max %.4f"
                       % (z_safe, pm.zmax))

    if not details:
        return _check(5, "Z audit", "SKIP", "no compensated files yet")
    return _check(5, "Z audit (cut depth, rapids, breakthrough)", status, details)


# ------------------------------------------------------------------ 6

def _clamp_check(analyses):
    details, status = [], "PASS"
    seen = False
    for name, ana in analyses.items():
        if not name.endswith("_comp"):
            continue
        seen = True
        if ana.clamped is None:
            details.append("%s: no applymap clamp record found" % name)
            status = "FAIL"
        elif ana.clamped == 0:
            details.append("%s: 0 clamped points" % name)
        elif name.startswith("outline"):
            details.append("%s: %d clamped points (allowed for edge ops, "
                           "check them)" % (name, ana.clamped))
            if status == "PASS":
                status = "WARN"
        else:
            details.append("%s: %d CLAMPED POINTS - probe grid does not cover "
                           "the toolpath" % (name, ana.clamped))
            status = "FAIL"
    if not seen:
        return _check(6, "Clamp count", "SKIP", "no compensated files yet")
    return _check(6, "Clamp count (map covers toolpath)", status, details)


# ------------------------------------------------------------------ 7

def _lint_check(job, analyses):
    details, status = [], "PASS"
    targets = {name: ana.path for name, ana in analyses.items()}
    pp = job.artifact_path("probe_program")
    if os.path.exists(pp):
        targets["probe_program"] = pp
    if not targets:
        return _check(7, "Comment/paren lint", "SKIP", "nothing generated yet")
    for name, path in targets.items():
        probs = lint_comments(path)
        if probs:
            status = "FAIL"
            details.append("%s: %s" % (name, "; ".join(probs[:5])))
        else:
            details.append("%s: clean" % name)
    return _check(7, "Comment/paren lint (PathPilot-safe)", status, details)


# ------------------------------------------------------------------ 8

def _staleness_check(job):
    states = job.states()
    details, status = [], "PASS"
    for name, st in states.items():
        if st in ("fresh", "skipped"):
            continue
        status = "FAIL"
        label = {"stale": "STALE (inputs/params changed since generation)",
                 "missing": "MISSING",
                 "tampered": "TAMPERED (edited outside the app - FR1.4)"}[st]
        details.append("%s: %s" % (name, label))
    if status == "PASS":
        details.append("all artifacts fresh")
    return _check(8, "Staleness (hashes current)", status, details)


# ------------------------------------------------------------------ 9

def _sliver_check(job, dfile, log):
    gerber = job.input_path("gerber_back")
    if not gerber:
        return _check(9, "Sliver prediction", "SKIP", "no Gerber input")
    cam = job.params["cam"]
    m = job.params["machine"]
    # pad disks: exact sizes from the .kicad_pcb when present, else the
    # Excellon hole plus a typical annulus
    pads = None
    exclusions = None
    pcb = job.input_path("kicad_pcb")
    if pcb:
        # .kicad_pcb has +Y down; the Gerber/Excellon frame negates Y.
        # np_thru_hole (mounting holes) are not solderable pads.
        board = Board(pcb)
        pads = [(p["x_mm"], -p["y_mm"], min(p["size_mm"]) / 2.0)
                for p in board.drilled_pads() if p["type"] == "thru_hole"]
        exclusions = [
            [(x, -y) for x, y in ra["polygon_mm"]]
            for ra in board.rule_areas()
            if ra["restrictions"].get("copperpour") == "not_allowed"]
    elif dfile:
        pads = [(x * MM_PER_IN, y * MM_PER_IN,
                 dfile.tools[t] * MM_PER_IN / 2.0 + 0.6)
                for t, x, y in dfile.holes
                if dfile.tool_plating.get(t) != "NPTH"]
    g54 = None
    if not cam.get("zero_start") and cam.get("x_offset") is not None:
        g54 = (-cam["x_offset"], cam["y_offset"])
    log("sliver: rendering %s ..." % os.path.basename(gerber))
    # isolation_width unset = single pass at the tool kerf
    iso = cam.get("isolation_width")
    if not iso:
        from .toollib import ToolLibrary, mill_diameter_for
        t = ToolLibrary().get(job.params["tools"]["isolation_tool"])
        iso = mill_diameter_for(t, cam["zwork"]) if t else 0.0073
    rep = sliver_mod.predict(
        gerber, iso, drilled_pads_mm=pads,
        fail_radius_mm=m["sliver_fail_radius_mm"],
        min_area_mm2=m["sliver_min_area_mm2"],
        kicad_to_g54=g54, exclusion_polys_mm=exclusions)
    job.data["sliver_report"] = rep
    job.save()
    details = ["isolation-width %.4f in (effective %.4f): %d slivers, "
               "%d FAIL / %d WARN / %d planned (in pour-exclusion areas)"
               % (rep["isolation_width_in"], rep["effective_width_in"],
                  rep["sliver_count"], rep["fail_count"], rep["warn_count"],
                  rep["planned_count"])]
    for s in rep["slivers"][:10]:
        loc = "KiCad %.2f,%.2f mm" % (s["x_mm"], s["y_mm"])
        if "x_g54" in s:
            loc += " = G54 X%.4f Y%.4f" % (s["x_g54"], s["y_g54"])
        details.append("%s: %s  area %.3f mm2  width %.3f mm  needs iso "
                       ">= %.4f in%s"
                       % (s["severity"], loc, s["area_mm2"], s["min_width_mm"],
                          s["needs_isolation_in"],
                          ("  (%.2f mm from a drilled pad)" % s["dist_to_drilled_pad_mm"]
                           if s["dist_to_drilled_pad_mm"] is not None else "")))
    if rep["fail_count"]:
        details.append("SUGGESTED isolation-width to zero the FAILs: %.4f in"
                       % rep["suggested_isolation_width_in"])
        return _check(9, "Sliver prediction", "FAIL", details)
    if rep["warn_count"]:
        return _check(9, "Sliver prediction", "WARN", details)
    return _check(9, "Sliver prediction", "PASS", details)


# ------------------------------------------------------------------ 12

def _frame_check(job, deep, log):
    """FR5.3: always zero-start=0 with explicit offsets, and the frame must
    not move when isolation-width changes (drill G-code byte-identical)."""
    from . import camconfig
    cam = job.params["cam"]
    details = []
    if cam.get("zero_start"):
        return _check(12, "Fixed frame (FR5.3)", "FAIL",
                      "zero-start=1 is FORBIDDEN: the bounding-box origin "
                      "silently shifts every output when isolation-width "
                      "changes. Set x-offset = -MX, y-offset = MY instead.")
    if cam.get("x_offset") is None or cam.get("y_offset") is None:
        return _check(12, "Fixed frame (FR5.3)", "FAIL",
                      "no fixed frame set - set offsets from the KiCad-page "
                      "transform or run the measurement solver")
    details.append("zero-start=0, x-offset %.4f, y-offset %.4f"
                   % (cam["x_offset"], cam["y_offset"]))
    if not deep:
        details.append("byte-stability run skipped (quick verify)")
        return _check(12, "Fixed frame (FR5.3)", "PASS", details)
    if not (job.input_path("gerber_back") and job.input_path("drill")):
        details.append("stability run skipped: inputs missing")
        return _check(12, "Fixed frame (FR5.3)", "WARN", details)
    log("frame: regenerating at two isolation widths ...")
    try:
        ok, msg = camconfig.drill_frame_stability(job)
    except Exception as e:
        return _check(12, "Fixed frame (FR5.3)", "FAIL",
                      details + ["stability run failed: %s" % e])
    details.append(msg)
    return _check(12, "Fixed frame (FR5.3)", "PASS" if ok else "FAIL", details)


# ------------------------------------------------------------------ 10

def _intake_lint(job):
    pcb = job.input_path("kicad_pcb")
    if not pcb:
        return _check(10, "Design intake lint", "SKIP",
                      "no .kicad_pcb input (optional)")
    m = job.params["machine"]
    board = Board(pcb)
    warnings = board.intake_lint(
        zone_clearance_warn_mm=m["zone_clearance_warn_mm"],
        th_pitch_warn_mm=m["th_row_pitch_warn_mm"])
    if not warnings:
        return _check(10, "Design intake lint (KiCad-side)", "PASS",
                      "no design-side flags")
    return _check(10, "Design intake lint (KiCad-side, report-only)", "WARN",
                  [w["msg"] for w in warnings])
