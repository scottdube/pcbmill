"""Local web UI (Flask). Single user, no auth, no cloud (§7).

Left rail = pipeline stages with status badges; the verification report
is the landing page for a job. Every action here is also a CLI command.
"""

import json
import os
import traceback

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

from . import camconfig, pipeline, probegen, runsheet as runsheet_mod, verify
from .excellon import DrillFile
from .kicadpcb import Board
from .manifest import ARTIFACTS, INPUT_KEYS, Job
from .toollib import (ToolLibrary, annulus_check, check_feed,
                      consolidate_drills, mill_diameter_for)
from . import auxgen

WEB = os.path.join(os.path.dirname(__file__), "web")
VENDOR = os.path.join(os.path.dirname(__file__), "vendor")
MM_PER_IN = 25.4


def create_app(jobs_root):
    os.makedirs(jobs_root, exist_ok=True)
    app = Flask(__name__, template_folder=WEB, static_folder=WEB,
                static_url_path="/static")
    app.secret_key = "pcbmill-local"
    app.config["JOBS_ROOT"] = jobs_root

    # ------------------------------------------------------------ helpers

    def load_job(name):
        root = os.path.join(jobs_root, name)
        if not os.path.isdir(root):
            abort(404)
        try:
            return Job.load(root)
        except FileNotFoundError:
            abort(404)

    def stage_rail(job):
        """Compute the left-rail stages + badges."""
        states = job.states()
        inputs_ok = bool(job.input_path("gerber_back") and job.input_path("drill"))
        drill_ok = False
        if job.input_path("drill"):
            try:
                drill_ok = DrillFile(job.input_path("drill")).is_mixed_plating
            except Exception:
                drill_ok = False
        pm = job.data.get("probe_map")
        qa_ok = bool(pm and pm.get("qa") and pm["qa"].get("ok"))
        v = job.data.get("verification")

        def art_badge(*names):
            sts = [states[n] for n in names if states[n] != "skipped"]
            if not sts:
                return "off"
            if any(s == "tampered" for s in sts):
                return "fail"
            if any(s == "missing" for s in sts):
                return "missing"
            if any(s == "stale" for s in sts):
                return "stale"
            return "ready"

        return [
            {"key": "inputs", "label": "Inputs",
             "badge": "ready" if (inputs_ok and drill_ok) else
                      ("fail" if inputs_ok else "missing")},
            {"key": "tools", "label": "Tools & feeds",
             "badge": "ready" if inputs_ok else "off"},
            {"key": "cam", "label": "CAM config",
             "badge": ("ready" if job.params["cam"]["x_offset"] is not None
                       or job.params["cam"]["zero_start"] else "missing")},
            {"key": "probe", "label": "Probe",
             "badge": ("ready" if qa_ok else
                       ("fail" if pm else
                        ("stale" if states["probe_program"] == "fresh" else "missing")))},
            {"key": "generate", "label": "Generate",
             "badge": art_badge("millproject", "back", "drillfile",
                                "back_comp", "drill_comp")},
            {"key": "verify", "label": "Verification",
             "badge": ("verified" if v and v.get("ready") else
                       ("fail" if v else "missing"))},
            {"key": "runsheet", "label": "Run sheet",
             "badge": "ready" if v and v.get("ready") else "off"},
        ]

    def ctx(job, page):
        return dict(job=job, page=page, rail=stage_rail(job),
                    states=job.states(), params=job.params)

    # ------------------------------------------------------------ routes

    @app.route("/")
    def index():
        jobs = []
        for name in sorted(os.listdir(jobs_root)):
            root = os.path.join(jobs_root, name)
            if not os.path.isfile(os.path.join(root, "job.json")):
                continue
            try:
                job = Job.load(root)
            except Exception:
                continue
            v = job.data.get("verification")
            jobs.append({
                "name": name,
                "created": job.data.get("created", ""),
                "ready": bool(v and v.get("ready")),
                "verified": bool(v),
            })
        return render_template("index.html", jobs=jobs, jobs_root=jobs_root)

    @app.post("/create")
    def create():
        name = request.form["name"].strip()
        if not name or "/" in name:
            flash("bad job name")
            return redirect(url_for("index"))
        try:
            Job.create(os.path.join(jobs_root, name), name=name)
        except FileExistsError:
            flash("job already exists")
            return redirect(url_for("index"))
        return redirect(url_for("job_inputs", name=name))

    @app.route("/job/<name>/")
    def job_home(name):
        job = load_job(name)
        return render_template("dashboard.html",
                               report=job.data.get("verification"),
                               sliver=job.data.get("sliver_report"),
                               **ctx(job, "verify"))

    # ---------------------------------------------------------- inputs

    @app.route("/job/<name>/inputs")
    def job_inputs(name):
        job = load_job(name)
        drill = None
        if job.input_path("drill"):
            try:
                drill = DrillFile(job.input_path("drill")).summary()
            except Exception as e:
                drill = {"error": str(e)}
        return render_template("inputs.html", drill=drill,
                               input_keys=INPUT_KEYS, **ctx(job, "inputs"))

    @app.post("/job/<name>/inputs/set")
    def job_inputs_set(name):
        job = load_job(name)
        key = request.form["key"]
        f = request.files.get("file")
        path = request.form.get("path", "").strip()
        try:
            if f and f.filename:
                dest = job.path(os.path.join("inputs", os.path.basename(f.filename)))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                f.save(dest)
                job.set_input(key, dest)
            elif path:
                job.set_input(key, os.path.expanduser(path))
            else:
                flash("choose a file or give a path")
        except Exception as e:
            flash("failed: %s" % e)
        return redirect(url_for("job_inputs", name=name))

    @app.post("/job/<name>/params/<group>")
    def job_params(name, group):
        job = load_job(name)
        values = {}
        for k, v in request.form.items():
            if k.startswith("_"):
                continue
            v = v.strip()
            if v == "":
                values[k] = None
                continue
            if v in ("true", "false", "on"):
                values[k] = v != "false"
                continue
            try:
                values[k] = int(v) if re_int(v) else float(v)
            except ValueError:
                values[k] = v
        if group == "top":
            for k, v in values.items():
                job.params[k] = v
            job.save()
        else:
            # relative applymap mode needs explicit confirmation (§6)
            if (group == "machine" and values.get("applymap_mode") == "relative"
                    and not request.form.get("_confirm_relative")):
                flash("relative applymap mode NOT set: its failure mode is a "
                      "70-thou crash. Tick the confirmation box if you truly "
                      "zeroed Z on the work surface.")
                values.pop("applymap_mode", None)
            job.update_params(group, values)
        flash("saved - downstream artifacts are now stale")
        return redirect(request.referrer or url_for("job_home", name=name))

    # ---------------------------------------------------------- tools

    @app.route("/job/<name>/tools")
    def job_tools(name):
        job = load_job(name)
        lib = ToolLibrary()
        iso = lib.get(job.params["tools"]["isolation_tool"])
        kerf = (mill_diameter_for(iso, job.params["cam"]["zwork"])
                if iso else None)
        feeds = []
        if iso:
            c, warn = check_feed(job.params["cam"]["mill_feed"],
                                 iso.get("flutes", 2),
                                 job.params["cam"]["mill_speed"])
            feeds.append({"label": "isolation", "feed": job.params["cam"]["mill_feed"],
                          "chipload": c, "warn": warn})
        # drilling tolerates roughly twice the milling chipload; 25 IPM at
        # 10k RPM (the house reference) must not warn
        c, warn = check_feed(job.params["cam"]["drill_feed"], 2,
                             job.params["cam"]["drill_speed"],
                             chipload_max=0.002)
        feeds.append({"label": "drills", "feed": job.params["cam"]["drill_feed"],
                      "chipload": c, "warn": warn})

        consolidation, annulus = None, None
        if job.input_path("drill"):
            try:
                d = DrillFile(job.input_path("drill"))
                consolidation = consolidate_drills(
                    d.tools, job.params["tools"]["drills_available"],
                    undersize_limit=job.params["machine"]["drill_undersize_limit_in"])
                if job.input_path("kicad_pcb"):
                    pads = [{"pad_size_mm": p["size_mm"], "drill_mm": p["drill_mm"],
                             "ref": "%s.%s" % (p["ref"], p["pad"]), "type": p["type"]}
                            for p in Board(job.input_path("kicad_pcb")).drilled_pads()]
                    annulus = annulus_check(
                        pads, consolidation,
                        annulus_min_mm=job.params["machine"]["annulus_min_mm"])
            except Exception as e:
                consolidation = [{"flag": "parse failed: %s" % e}]
        return render_template("tools.html", lib=lib, iso=iso, kerf=kerf,
                               feeds=feeds, consolidation=consolidation,
                               annulus=annulus, **ctx(job, "tools"))

    @app.post("/tools/upsert")
    def tools_upsert(name=None):
        lib = ToolLibrary()
        t = {k: v for k, v in request.form.items() if not k.startswith("_")}
        for k in ("tip_diameter", "included_angle", "diameter", "chipload"):
            if t.get(k):
                t[k] = float(t[k])
            elif k in t:
                del t[k]
        for k in ("flutes", "t_number"):
            if t.get(k):
                t[k] = int(t[k])
        lib.upsert(t)
        flash("tool saved")
        return redirect(request.referrer or url_for("index"))

    # ---------------------------------------------------------- cam

    @app.route("/job/<name>/cam")
    def job_cam(name):
        job = load_job(name)
        mp = None
        try:
            mp = camconfig.build_millproject(job)
        except Exception as e:
            mp = "# not buildable yet: %s" % e
        return render_template("cam.html", millproject=mp, **ctx(job, "cam"))

    @app.post("/job/<name>/frame")
    def job_frame(name):
        job = load_job(name)
        try:
            res = camconfig.set_frame_from_page_transform(
                job,
                float(request.form["mx"]), float(request.form["my"]),
                dx=float(request.form.get("dx") or 0),
                dy=float(request.form.get("dy") or 0))
            flash("fixed frame set: x-offset %(x_offset)s, y-offset "
                  "%(y_offset)s (zero-start=0). Check 12 will prove drill "
                  "byte-stability on the next full verify." % res)
        except Exception as e:
            flash("failed: %s" % e)
        return redirect(url_for("job_cam", name=name))

    @app.post("/job/<name>/solve-offsets")
    def job_solve(name):
        job = load_job(name)
        try:
            res = camconfig.solve_offsets(job, log=lambda *a: None)
            flash("solved: x-offset %.4f, y-offset %.4f (centre error %.4f in)"
                  % (res["x_offset"], res["y_offset"], res["centre_error_in"]))
        except Exception as e:
            flash("solver failed: %s" % e)
        return redirect(url_for("job_cam", name=name))

    # ---------------------------------------------------------- probe

    @app.route("/job/<name>/probe")
    def job_probe(name):
        job = load_job(name)
        pm = job.data.get("probe_map")
        touch = None
        if job.probe_map_path and os.path.exists(job.probe_map_path or ""):
            try:
                touch = probegen.ProbeMap(job.probe_map_path).touch_off()
            except ValueError:
                touch = None
        prog_exists = os.path.exists(job.artifact_path("probe_program"))
        return render_template("probe.html", pm=pm, touch=touch,
                               prog_exists=prog_exists, **ctx(job, "probe"))

    @app.post("/job/<name>/probe/plan")
    def job_probe_plan(name):
        job = load_job(name)
        blank = job.params["blank"]
        p = job.params["probe"]
        try:
            ce = job.data.get("copper_extents")
            grid = probegen.plan_grid(blank["x0"], blank["y0"], blank["width"],
                                      blank["height"], p["inset"],
                                      p["target_points"],
                                      copper=((ce["x0"], ce["y0"],
                                               ce["x1"], ce["y1"])
                                              if ce else None))
            job.update_params("probe", grid)
            flash("planned %dx%d grid" % (grid["x_count"], grid["y_count"]))
        except Exception as e:
            flash("planning failed: %s" % e)
        return redirect(url_for("job_probe", name=name))

    @app.post("/job/<name>/probe/generate")
    def job_probe_generate(name):
        job = load_job(name)
        out = job.artifact_path("probe_program")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            info = probegen.generate_program(job.params["probe"], out)
            job.record_artifact("probe_program")
            flash("probe program written: %d points" % info["points"])
        except Exception as e:
            flash("failed: %s" % e)
        return redirect(url_for("job_probe", name=name))

    @app.post("/job/<name>/probe/import")
    def job_probe_import(name):
        job = load_job(name)
        f = request.files.get("file")
        path = request.form.get("path", "").strip()
        try:
            if f and f.filename:
                tmp = job.path("probe/upload.ngc")
                f.save(tmp)
                src = tmp
            elif path:
                src = os.path.expanduser(path)
            else:
                flash("choose a file")
                return redirect(url_for("job_probe", name=name))
            pmap = probegen.ProbeMap(src)
            mach = job.params["machine"]
            qa = pmap.qa(roughness_rms_limit=mach["roughness_rms_limit_in"],
                         roughness_point_limit=mach["roughness_point_limit_in"])
            job.set_probe_map(src, qa=qa)
            flash("map imported: %d points%s"
                  % (qa["points"], "" if qa["ok"] else " - RED FLAGS, see QA"))
        except Exception as e:
            flash("import failed: %s" % e)
        return redirect(url_for("job_probe", name=name))

    @app.route("/viewer")
    def viewer():
        return send_file(os.path.join(VENDOR, "probe-surface-viewer.html"))

    # ---------------------------------------------------------- generate/verify

    @app.post("/job/<name>/regen")
    def job_regen(name):
        job = load_job(name)
        try:
            pipeline.regenerate(job, log=lambda *a: None)
            verify.run(job, deep=True)
            flash("regenerated + verified")
        except Exception as e:
            traceback.print_exc()
            flash("regeneration failed: %s" % e)
        return redirect(url_for("job_home", name=name))

    @app.post("/job/<name>/verify")
    def job_verify(name):
        job = load_job(name)
        try:
            verify.run(job, deep=request.form.get("deep") != "0")
        except Exception as e:
            traceback.print_exc()
            flash("verify failed: %s" % e)
        return redirect(url_for("job_home", name=name))

    # ---------------------------------------------------------- aux (FR8)

    @app.route("/job/<name>/aux")
    def job_aux(name):
        job = load_job(name)
        regions = []
        if job.input_path("kicad_pcb"):
            board = Board(job.input_path("kicad_pcb"))
            cam = job.params["cam"]
            mx = -cam["x_offset"] if cam["x_offset"] is not None else None
            my = cam["y_offset"]
            pmap = None
            if job.probe_map_path and os.path.exists(job.probe_map_path):
                try:
                    pmap = probegen.ProbeMap(job.probe_map_path)
                except ValueError:
                    pmap = None
            for ra in board.rule_areas():
                contents = board.region_contents(ra["polygon_mm"])
                rec = dict(ra)
                rec["contents"] = contents
                # copper-relevant restrictions only: footprints=allowed does
                # not place copper (board 1's antenna keepout has it and was
                # legitimately cleared)
                copper_keys = ("tracks", "pads", "vias", "copperpour")
                rec["clearable"] = (contents["empty"] and ra["restrictions"]
                                    and all(ra["restrictions"].get(k) == "not_allowed"
                                            for k in copper_keys
                                            if k in ra["restrictions"]))
                if mx is not None:
                    gx = [mx - x / MM_PER_IN for x, y in ra["polygon_mm"]]
                    gy = [my - y / MM_PER_IN for x, y in ra["polygon_mm"]]
                    rec["g54_bounds"] = (min(gx), min(gy), max(gx), max(gy))
                    rec["g54_poly"] = list(zip(gx, gy))
                    if pmap:
                        e = pmap.extents
                        rec["in_map"] = (min(gx) >= e[0] and max(gx) <= e[2]
                                         and min(gy) >= e[1] and max(gy) <= e[3])
                regions.append(rec)
        aux_files = sorted(f for f in os.listdir(job.path("out"))
                           if f.startswith(("stair", "clear")))
        return render_template("aux.html", regions=regions,
                               aux_files=aux_files, **ctx(job, "aux"))

    @app.post("/job/<name>/aux/stair")
    def job_aux_stair(name):
        job = load_job(name)
        f = request.form
        try:
            depths = [float(d) for d in f["depths"].replace(",", " ").split()]
            code, extent = auxgen.stair_gcode(
                float(f["x0"]), float(f["y0"]), depths=depths,
                t_number=job.params["tools"]["isolation_t_number"])
            pmap = probegen.ProbeMap(job.probe_map_path) if job.probe_map_path else None
            if pmap:
                e = pmap.extents
                if not (e[0] <= extent[0] and extent[2] <= e[2]
                        and e[1] <= extent[1] and extent[3] <= e[3]):
                    flash("REFUSED: staircase extents X %.3f..%.3f Y %.3f..%.3f "
                          "leave the probe map" % (extent[0], extent[2],
                                                   extent[1], extent[3]))
                    return redirect(url_for("job_aux", name=name))
            with open(job.path("out/stair.ngc"), "w") as fh:
                fh.write(code)
            flash("wrote out/stair.ngc - run applymap via Regenerate? No: "
                  "compensate it with the Compensate button below")
        except Exception as e:
            flash("failed: %s" % e)
        return redirect(url_for("job_aux", name=name))

    @app.post("/job/<name>/aux/clear")
    def job_aux_clear(name):
        job = load_job(name)
        f = request.form
        try:
            board = Board(job.input_path("kicad_pcb"))
            idx = int(f["region"])
            ra = next(r for r in board.rule_areas() if r["index"] == idx)
            contents = board.region_contents(ra["polygon_mm"])
            why = auxgen.keepout_refusal(ra["restrictions"], contents["empty"])
            if why:
                flash("REFUSED: region %d %s" % (idx, why))
                return redirect(url_for("job_aux", name=name))
            cam = job.params["cam"]
            mx, my = -cam["x_offset"], cam["y_offset"]
            poly = [(mx - x / MM_PER_IN, my - y / MM_PER_IN)
                    for x, y in ra["polygon_mm"]]
            map_bounds = None
            if job.probe_map_path:
                e = probegen.ProbeMap(job.probe_map_path).extents
                map_bounds = e
            code, region = auxgen.clear_gcode(
                poly, float(f["tool_diameter"]), float(f["depth"]),
                int(f.get("t_number") or job.params["tools"]["outline_t_number"]),
                map_bounds=map_bounds)
            out = job.path("out/clear_area%d.ngc" % idx)
            with open(out, "w") as fh:
                fh.write(code)
            flash("wrote %s (%d-corner inset region)" % (os.path.basename(out),
                                                         len(region)))
        except ValueError as e:
            flash("REFUSED: %s" % e)
        except Exception as e:
            flash("failed: %s" % e)
        return redirect(url_for("job_aux", name=name))

    @app.post("/job/<name>/aux/compensate")
    def job_aux_comp(name):
        job = load_job(name)
        fname = os.path.basename(request.form["file"])
        src = job.path("out/" + fname)
        dst = job.path("out/" + fname.replace(".ngc", "_comp.ngc"))
        if not job.probe_map_path:
            flash("no probe map imported")
            return redirect(url_for("job_aux", name=name))
        import subprocess, sys as _sys
        proc = subprocess.run(
            [_sys.executable, os.path.join(VENDOR, "applymap.py"),
             "-m", job.probe_map_path, "-i", src, "-o", dst,
             "--mode", job.params["machine"]["applymap_mode"]],
            capture_output=True, text=True)
        if proc.returncode:
            flash("applymap failed: %s" % proc.stderr[-300:])
        else:
            flash("wrote %s  [%s]" % (os.path.basename(dst),
                                      proc.stderr.strip().split("\n")[-1]))
        return redirect(url_for("job_aux", name=name))

    # ---------------------------------------------------------- runsheet

    @app.route("/job/<name>/runsheet")
    def job_runsheet(name):
        job = load_job(name)
        sheet = runsheet_mod.build(job)
        text = runsheet_mod.to_text(sheet)
        with open(job.path("out/runsheet.txt"), "w") as fh:
            fh.write(text)
        return render_template("runsheet.html", sheet=sheet, text=text,
                               **ctx(job, "runsheet"))

    # ---------------------------------------------------------- files

    @app.route("/job/<name>/file/<path:rel>")
    def job_file(name, rel):
        job = load_job(name)
        full = os.path.abspath(job.path(rel))
        if not full.startswith(job.root) or not os.path.isfile(full):
            abort(404)
        return send_file(full, as_attachment="dl" in request.args,
                         mimetype="text/plain")

    return app


def re_int(v):
    try:
        return float(v) == int(float(v)) and "." not in v and "e" not in v.lower()
    except ValueError:
        return False
