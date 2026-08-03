"""pcbmill CLI - every UI action has a CLI entry point (§7)."""

import argparse
import json
import os
import sys

from . import camconfig, pipeline, probegen, runsheet, verify
from .excellon import DrillFile
from .kicadpcb import Board
from .manifest import ARTIFACTS, Job
from .toollib import (ToolLibrary, check_feed, consolidate_drills,
                      mill_diameter_for, annulus_check)

MM_PER_IN = 25.4


def _job(args):
    root = getattr(args, "job", None) or os.getcwd()
    try:
        return Job.load(root)
    except FileNotFoundError:
        sys.exit("no job.json in %s - run 'pcbmill new' first or pass --job" % root)


def cmd_new(args):
    job = Job.create(args.dir, name=args.name)
    print("created job at", job.root)
    if args.back:
        job.set_input("gerber_back", args.back)
        print("  gerber_back =", args.back)
    if args.drill:
        job.set_input("drill", args.drill)
        _warn_drill(job)
    if args.pcb:
        job.set_input("kicad_pcb", args.pcb)
        print("  kicad_pcb =", args.pcb)
    if args.edge:
        job.set_input("edge_cuts", args.edge)
        print("  edge_cuts =", args.edge)


def _warn_drill(job):
    d = DrillFile(job.input_path("drill"))
    if not d.is_mixed_plating:
        print("  *** DRILL FILE REJECTED CHECK: header does not declare "
              "MixedPlating - this looks like a SPLIT PTH/NPTH file. Export "
              "ONE merged Excellon from KiCad. ***")
    print("  drill = %d tools, %d holes" % (len(d.tools), d.hole_count()))


def cmd_input(args):
    job = _job(args)
    job.set_input(args.key, args.file)
    print("set", args.key)
    if args.key == "drill":
        _warn_drill(job)


def cmd_status(args):
    job = _job(args)
    print("job:", job.data["name"], " (", job.root, ")")
    print("inputs:")
    for k in ("gerber_back", "drill", "kicad_pcb", "edge_cuts"):
        rec = job.data["inputs"].get(k)
        print("  %-12s %s" % (k, rec["file"] if rec else "-"))
    pm = job.data.get("probe_map")
    print("probe map:", pm["file"] if pm else "NOT IMPORTED")
    print("artifacts:")
    for name, state in job.states().items():
        mark = {"fresh": "ok    ", "stale": "STALE ", "missing": "missing",
                "tampered": "TAMPERED", "skipped": "-     "}[state]
        print("  %-14s %s" % (name, mark))
    v = job.data.get("verification")
    if v:
        print("verification: %s (ran %s)"
              % ("READY" if v["ready"] else "NOT READY", v["ran"]))
    else:
        print("verification: never run")


def cmd_drill_info(args):
    job = _job(args)
    d = DrillFile(job.input_path("drill"))
    s = d.summary()
    print(json.dumps(s, indent=2))
    if not s["mixed_plating"]:
        sys.exit("*** not a merged MixedPlating drill file ***")


def cmd_consolidate(args):
    job = _job(args)
    d = DrillFile(job.input_path("drill"))
    lib = ToolLibrary()
    avail = job.params["tools"]["drills_available"]
    mapping = consolidate_drills(
        d.tools, avail,
        undersize_limit=job.params["machine"]["drill_undersize_limit_in"])
    print("drill consolidation (pcb2gcode rounds to NEAREST, not up):")
    for m in mapping:
        line = ("  T%-3d nominal %.4f\" -> bit %.4f\"  error %+.4f\""
                % (m["excellon_tool"], m["nominal_in"],
                   m["chosen_in"] or 0, m["error_in"] or 0))
        if m["flag"]:
            line += "   *** " + m["flag"]
        print(line)
    pcb = job.input_path("kicad_pcb")
    if pcb:
        pads = [{"pad_size_mm": p["size_mm"], "drill_mm": p["drill_mm"],
                 "ref": "%s.%s" % (p["ref"], p["pad"])}
                for p in Board(pcb).drilled_pads()]
        problems = annulus_check(
            pads, mapping,
            annulus_min_mm=job.params["machine"]["annulus_min_mm"])
        if problems:
            print("ANNULUS FAILURES ((pad-drill)/2 < %.2f mm, no plating!):"
                  % job.params["machine"]["annulus_min_mm"])
            for pr in problems:
                print("  %s: pad %s mm, bit %.3f mm -> ring %.3f mm"
                      % (pr["ref"], pr["pad_mm"], pr["drill_actual_mm"],
                         pr["annulus_mm"]))
        else:
            print("annulus check: all rings >= %.2f mm"
                  % job.params["machine"]["annulus_min_mm"])


def cmd_set(args):
    job = _job(args)
    if "." not in args.param:
        job.params[args.param] = args.value
        job.save()
        print("set %s = %r (downstream artifacts are now stale)"
              % (args.param, args.value))
        return
    group, key = args.param.split(".", 1)
    val = args.value
    for cast in (int, float):
        try:
            val = cast(args.value)
            break
        except ValueError:
            continue
    if args.value in ("true", "false"):
        val = args.value == "true"
    if group == "machine" and key == "applymap_mode" and val == "relative":
        if not args.yes_i_know:
            sys.exit("relative applymap mode's failure mode is a 70-thou "
                     "crash. Re-run with --yes-i-know if you really zeroed "
                     "Z on the work surface.")
    job.update_params(group, {key: val})
    print("set %s.%s = %r (downstream artifacts are now stale)" % (group, key, val))


def cmd_probe_gen(args):
    job = _job(args)
    p = job.params["probe"]
    if p["x_start"] is None or args.replan:
        blank = job.params["blank"]
        grid = probegen.plan_grid(blank["x0"], blank["y0"], blank["width"],
                                  blank["height"], p["inset"],
                                  p["target_points"])
        job.update_params("probe", grid)
        p = job.params["probe"]
        print("planned grid:", json.dumps(grid))
    out = job.artifact_path("probe_program")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    info = probegen.generate_program(p, out)
    job.record_artifact("probe_program")
    print("wrote %s (%d points, X to %.3f, Y to %.3f)"
          % (out, info["points"], info["x_end"], info["y_end"]))


def cmd_probe_import(args):
    job = _job(args)
    pmap = probegen.ProbeMap(args.file)
    m = job.params["machine"]
    qa = pmap.qa(roughness_rms_limit=m["roughness_rms_limit_in"],
                 roughness_point_limit=m["roughness_point_limit_in"])
    job.set_probe_map(args.file, qa=qa)
    print(json.dumps(qa, indent=2))
    if not qa["ok"]:
        print("*** RED FLAGS above - do not cut before investigating ***")


def cmd_touchoff(args):
    job = _job(args)
    if not job.probe_map_path:
        sys.exit("no probe map imported")
    t = probegen.ProbeMap(job.probe_map_path).touch_off()
    for line in t["instructions"]:
        print(line)


def cmd_solve_offsets(args):
    job = _job(args)
    res = camconfig.solve_offsets(job)
    print(json.dumps(res, indent=2))


def cmd_regen(args):
    job = _job(args)
    pipeline.regenerate(job)
    print("regenerated; running verification...")
    rep = verify.run(job, deep=not args.quick)
    _print_report(rep)


def cmd_verify(args):
    job = _job(args)
    rep = verify.run(job, deep=not args.quick, log=print)
    _print_report(rep)


def _print_report(rep):
    for c in rep["checks"]:
        print("[%s] %2s. %s" % (c["status"].ljust(4), c["id"], c["name"]))
        for d in c["details"]:
            print("        " + d)
    print()
    print("JOB READY" if rep["ready"] else "JOB NOT READY - fix the FAILs above")


def cmd_runsheet(args):
    job = _job(args)
    sheet = runsheet.build(job)
    text = runsheet.to_text(sheet)
    out = job.path("out/runsheet.txt")
    with open(out, "w") as fh:
        fh.write(text)
    print(text)
    print("(saved to %s)" % out)


def cmd_tools(args):
    lib = ToolLibrary()
    print("tool library (%s):" % lib.path)
    for t in lib.tools:
        extra = ""
        if t["kind"] == "vbit":
            extra = "  tip %.4f\" %g deg" % (t["tip_diameter"], t["included_angle"])
        else:
            extra = "  dia %.4f\"" % t["diameter"]
        print("  %-14s T%-4d %-28s%s" % (t["id"], t.get("t_number", 0),
                                         t["label"], extra))
    hazards = lib.atc_hazards()
    if hazards:
        print("*** ATC HAZARD: tools below T600 will fire the changer: %s"
              % ", ".join(t["id"] for t in hazards))


def cmd_kerf(args):
    lib = ToolLibrary()
    t = lib.get(args.tool)
    if not t or t["kind"] != "vbit":
        sys.exit("%s is not a V-bit in the library" % args.tool)
    d = mill_diameter_for(t, args.zwork)
    print("kerf at zwork %g: %.4f in  (tip %.4f + 2*tan(%g/2)*%g)"
          % (args.zwork, d, t["tip_diameter"], t["included_angle"],
             abs(args.zwork)))


def cmd_feedcheck(args):
    c, warning = check_feed(args.feed, args.flutes, args.rpm)
    print("chipload: %.6f in/tooth" % c)
    print(warning or "within the %.4f-%.4f window" % (0.0002, 0.001))


def cmd_regions(args):
    job = _job(args)
    pcb = job.input_path("kicad_pcb")
    if not pcb:
        sys.exit("no kicad_pcb input")
    b = Board(pcb)
    cam = job.params["cam"]
    mx = -cam["x_offset"] if cam["x_offset"] is not None else None
    my = cam["y_offset"]
    pmap = probegen.ProbeMap(job.probe_map_path) if job.probe_map_path else None
    for ra in b.rule_areas():
        contents = b.region_contents(ra["polygon_mm"])
        print("RULE AREA %d  layer %s  %.1f mm2" % (ra["index"], ra["layer"],
                                                    ra["area_mm2"]))
        print("  restrictions:", " ".join("%s=%s" % kv
                                          for kv in ra["restrictions"].items()))
        if mx is not None:
            gx = [mx - x / MM_PER_IN for x, y in ra["polygon_mm"]]
            gy = [my - y / MM_PER_IN for x, y in ra["polygon_mm"]]
            line = ("  G54 bounds X %.4f..%.4f Y %.4f..%.4f"
                    % (min(gx), max(gx), min(gy), max(gy)))
            if pmap:
                e = pmap.extents
                inmap = (min(gx) >= e[0] and max(gx) <= e[2]
                         and min(gy) >= e[1] and max(gy) <= e[3])
                line += "  [%s]" % ("inside probe map" if inmap
                                    else "EXTENDS OUTSIDE PROBE MAP")
            print(line)
        print("  contents: %s" % ("EMPTY - safe to raster" if contents["empty"]
                                  else "HAS COPPER - do NOT raster (%s, %d track ends, %d vias)"
                                  % (", ".join(contents["footprints"]) or "no fps",
                                     contents["track_ends"], contents["vias"])))


def cmd_serve(args):
    from .server import create_app
    app = create_app(jobs_root=os.path.abspath(args.jobs_root))
    print("pcbmill UI on http://127.0.0.1:%d  (jobs root: %s)"
          % (args.port, args.jobs_root))
    app.run(host="127.0.0.1", port=args.port, debug=args.debug)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pcbmill",
                                 description="PCB mill pipeline cockpit")
    ap.add_argument("--job", help="job directory (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="create a job directory")
    s.add_argument("dir")
    s.add_argument("--name")
    s.add_argument("--back", help="back-copper Gerber (B.Cu; never front=)")
    s.add_argument("--drill", help="ONE merged Excellon (MixedPlating)")
    s.add_argument("--pcb", help=".kicad_pcb for region tools (optional)")
    s.add_argument("--edge", help="Edge_Cuts Gerber (optional)")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("input", help="set/replace one input file")
    s.add_argument("key", choices=["gerber_back", "drill", "kicad_pcb", "edge_cuts"])
    s.add_argument("file")
    s.set_defaults(fn=cmd_input)

    s = sub.add_parser("status", help="job + artifact staleness overview")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("drill-info", help="parse + display the drill file (FR2.2)")
    s.set_defaults(fn=cmd_drill_info)

    s = sub.add_parser("consolidate", help="drill size mapping + annulus check")
    s.set_defaults(fn=cmd_consolidate)

    s = sub.add_parser("set", help="set a job parameter, e.g. cam.isolation_width 0.03")
    s.add_argument("param", help="group.key")
    s.add_argument("value")
    s.add_argument("--yes-i-know", action="store_true")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("probe-gen", help="generate the probe program")
    s.add_argument("--replan", action="store_true", help="re-derive the grid")
    s.set_defaults(fn=cmd_probe_gen)

    s = sub.add_parser("probe-import", help="import probe-results.ngc + QA")
    s.add_argument("file")
    s.set_defaults(fn=cmd_probe_import)

    s = sub.add_parser("touchoff", help="touch-off assistant (FR4.4)")
    s.set_defaults(fn=cmd_touchoff)

    s = sub.add_parser("solve-offsets", help="measure-based offset solver (FR5.3)")
    s.set_defaults(fn=cmd_solve_offsets)

    s = sub.add_parser("regen", help="regenerate ALL artifacts, then verify")
    s.add_argument("--quick", action="store_true", help="skip sliver render")
    s.set_defaults(fn=cmd_regen)

    s = sub.add_parser("verify", help="run the verification suite")
    s.add_argument("--quick", action="store_true", help="skip sliver render")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("runsheet", help="print + save the run sheet")
    s.set_defaults(fn=cmd_runsheet)

    s = sub.add_parser("tools", help="show the tool library")
    s.set_defaults(fn=cmd_tools)

    s = sub.add_parser("kerf", help="V-bit kerf at a zwork")
    s.add_argument("tool")
    s.add_argument("zwork", type=float)
    s.set_defaults(fn=cmd_kerf)

    s = sub.add_parser("feedcheck", help="chipload sanity for a feed")
    s.add_argument("feed", type=float)
    s.add_argument("--flutes", type=int, default=2)
    s.add_argument("--rpm", type=int, default=10000)
    s.set_defaults(fn=cmd_feedcheck)

    s = sub.add_parser("regions", help="rule-area browser (FR8)")
    s.set_defaults(fn=cmd_regions)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--port", type=int, default=8337)
    s.add_argument("--jobs-root", default=os.path.expanduser("~/PCBMill"))
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
