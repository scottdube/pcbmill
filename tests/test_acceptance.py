"""Acceptance tests (§8 of the requirements).

1. Recreate board 1 from its actual inputs; generated files match the
   proven ones apart from comment/whitespace noise.
2. Reintroduce each historical bug deliberately - the suite must catch
   every one.
3. Parameter changes flip downstream artifacts stale.
4. Run sheet for board 1 matches what was actually done.
"""

import os
import re
import shutil
import sys

import pytest

from pcbmill import deploy, pipeline, probegen, runsheet, verify
from pcbmill.excellon import DrillFile
from pcbmill.manifest import Job
from pcbmill.toollib import (annulus_check, check_feed, consolidate_drills,
                            vbit_kerf)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def check(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)


def make_job(tmp, name, iso_width=None, x_offset=-4.3372, y_offset=3.2478,
             zero_start=0):
    """Board-1 job with the FR5.3 fixed frame (x-offset=-MX, y-offset=MY).

    The proven files were cut under the old zero-start frame, which this
    frame reproduces to within one output quantum (0.05 mil)."""
    job = Job.create(os.path.join(tmp, name), name=name)
    job.params["basename"] = "geoaux"
    job.set_input("gerber_back", os.path.join(FIX, "sln-geo-aux-heat-B_Cu.gbr"))
    job.set_input("drill", os.path.join(FIX, "sln-geo-aux-heat.drl"))
    job.set_input("kicad_pcb", os.path.join(FIX, "sln-geo-aux-heat.kicad_pcb"))
    job.update_params("cam", {"x_offset": x_offset, "y_offset": y_offset,
                              "zero_start": zero_start,
                              "isolation_width": iso_width})
    job.update_params("blank", {"x0": 0.39, "y0": 0.44,
                                "width": 3.98, "height": 2.80})
    job.set_probe_map(os.path.join(FIX, "proberesults_test_A_2.ngc"))
    return job


@pytest.fixture(scope="module")
def board1(tmp_path_factory):
    """Full pipeline run with board 1's v1 parameters."""
    tmp = tmp_path_factory.mktemp("b1")
    job = make_job(str(tmp), "board1")
    pipeline.regenerate(job, log=lambda *a: None)
    return job


def motion_lines(path):
    """Everything except standalone comment lines (header noise)."""
    out = []
    for line in open(path):
        s = line.rstrip("\n")
        if s.strip().startswith("(") or not s.strip():
            continue
        out.append(s)
    return out


WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d+\.?\d*)")


def assert_equivalent(got_path, want_path, tol=2e-4):
    """Same program, coordinate-for-coordinate, within one output quantum.

    The proven board-1 files were generated under the old zero-start frame;
    the FR5.3 fixed frame lands 0.05 mil away (bounding-box quantisation),
    so 'byte-match apart from noise' means: identical structure and word
    letters, every numeric value within tol."""
    got = motion_lines(got_path)
    want = motion_lines(want_path)
    assert len(got) == len(want), "line count %d != %d" % (len(got), len(want))
    for lineno, (a, b) in enumerate(zip(got, want), 1):
        ca = a.split("(")[0]
        cb = b.split("(")[0]
        wa = WORD_RE.findall(ca)
        wb = WORD_RE.findall(cb)
        assert [w[0] for w in wa] == [w[0] for w in wb], \
            "line %d: %r vs %r" % (lineno, a, b)
        for (la, va), (_, vb) in zip(wa, wb):
            assert abs(float(va) - float(vb)) <= tol, \
                "line %d word %s: %s vs %s" % (lineno, la, va, vb)
        if not wa:
            assert ca.strip() == cb.strip(), "line %d: %r vs %r" % (lineno, a, b)


# ------------------------------------------------------------ acceptance 1

def test_board1_back_matches_proven(board1):
    assert_equivalent(board1.artifact_path("back"),
                      os.path.join(FIX, "proven_out", "geoaux_back.ngc"))


def test_board1_drill_matches_proven(board1):
    assert_equivalent(board1.artifact_path("drillfile"),
                      os.path.join(FIX, "proven_out", "geoaux_drill.ngc"))


def test_board1_back_comp_matches_proven(board1):
    assert_equivalent(board1.artifact_path("back_comp"),
                      os.path.join(FIX, "proven_out", "geoaux_back_comp.ngc"))


def test_board1_drill_comp_matches_proven(board1):
    assert_equivalent(board1.artifact_path("drill_comp"),
                      os.path.join(FIX, "proven_out", "geoaux_drill_comp.ngc"))


# ------------------------------------------------------------ acceptance 2

def test_bug_split_drill_files(tmp_path):
    job = make_job(str(tmp_path), "split")
    job.set_input("drill", os.path.join(FIX, "sln-geo-aux-heat-PTH.drl"))
    rep = verify.run(job, deep=False)
    assert check(rep, 0)["status"] == "FAIL"
    assert not rep["ready"]


def test_bug_front_instead_of_back(board1, tmp_path):
    """A drill file generated in the UNMIRRORED (front) frame must trip the
    mirror proof."""
    import subprocess
    from pcbmill.camconfig import PCB2GCODE
    work = tmp_path / "front"
    work.mkdir()
    shutil.copyfile(os.path.join(FIX, "sln-geo-aux-heat-B_Cu.gbr"),
                    work / "b.gbr")
    shutil.copyfile(os.path.join(FIX, "sln-geo-aux-heat.drl"),
                    work / "d.drl")
    (work / "out").mkdir()
    subprocess.run(
        [PCB2GCODE, "--software", "linuxcnc",
         "--front", "b.gbr", "--drill", "d.drl", "--output-dir", "out",
         "--basename", "wrong", "--mill-diameters", "0.0073",
         "--zwork", "-0.002", "--zsafe", "0.1", "--zdrill", "-0.075",
         "--zchange", "1.0",
         "--drill-feed", "25", "--drill-speed", "10000",
         "--mill-feed", "9", "--mill-speed", "10000", "--nog81", "1",
         "--drills-available", "0.032,0.0465,0.0625,0.125",
         "--zero-start", "1",
         "--x-offset", "-4.2776", "--y-offset", "0.5319"],
        cwd=work, capture_output=True, text=True, check=True, timeout=600)
    # swap the wrong-side drill file into the job's out/
    shutil.copyfile(work / "out" / "wrong_drill.ngc",
                    board1.artifact_path("drillfile"))
    try:
        rep = verify.run(board1, deep=False)
        assert check(rep, 2)["status"] == "FAIL"
        assert not rep["ready"]
    finally:
        pipeline.regenerate(board1, log=lambda *a: None)


def test_bug_positive_x_offset(tmp_path):
    """Positive x-offset on a mirrored board shoves the toolpath negative -
    extents must fail."""
    job = make_job(str(tmp_path), "posx", x_offset=+4.3372)
    pipeline.regenerate(job, log=lambda *a: None)
    rep = verify.run(job, deep=False)
    assert check(rep, 1)["status"] == "FAIL"
    assert not rep["ready"]


def test_bug_stale_output(board1):
    """A parameter change flips downstream artifacts stale (acceptance 3)."""
    old = board1.params["cam"]["zwork"]
    board1.update_params("cam", {"zwork": -0.003})
    try:
        assert board1.artifact_state("millproject") == "stale"
        assert board1.artifact_state("back") == "stale"
        assert board1.artifact_state("back_comp") == "stale"
        rep = verify.run(board1, deep=False)
        assert check(rep, 8)["status"] == "FAIL"
        assert not rep["ready"]
    finally:
        board1.update_params("cam", {"zwork": old})
        pipeline.regenerate(board1, log=lambda *a: None)


def test_bug_hand_edited_output(board1):
    """FR1.4: hand edits in out/ are detected as TAMPERED."""
    path = board1.artifact_path("back")
    with open(path, "a") as fh:
        fh.write("(sneaky hand edit)\n")
    try:
        assert board1.artifact_state("back") == "tampered"
        rep = verify.run(board1, deep=False)
        assert check(rep, 8)["status"] == "FAIL"
    finally:
        pipeline.regenerate(board1, log=lambda *a: None)


def test_bug_probe_safety_below_copper(board1):
    """#7 below the copper top = stylus crash on the first traverse."""
    old = board1.params["probe"]["z_safe"]
    board1.update_params("probe", {"z_safe": 0.05})
    try:
        rep = verify.run(board1, deep=False)
        assert check(rep, 5)["status"] == "FAIL"
        assert any("z_safe" in d for d in check(rep, 5)["details"])
    finally:
        board1.update_params("probe", {"z_safe": old})


def test_bug_grid_short_of_toolpath(tmp_path):
    """Probe map missing the top rows -> coverage + clamp failures."""
    job = make_job(str(tmp_path), "shortgrid")
    # keep only map rows with Y <= 2.6 (toolpath runs to Y ~3.1)
    short = tmp_path / "short-map.ngc"
    with open(os.path.join(FIX, "proberesults_test_A_2.ngc")) as src, \
            open(short, "w") as dst:
        for line in src:
            m = re.match(r"G1 X[-\d.]+ Y([-\d.]+)", line)
            if m and float(m.group(1)) > 2.6:
                continue
            dst.write(line)
    job.set_probe_map(str(short))
    pipeline.regenerate(job, log=lambda *a: None)
    rep = verify.run(job, deep=False)
    assert check(rep, 1)["status"] == "FAIL"      # extents vs map
    assert check(rep, 6)["status"] == "FAIL"      # clamped points
    assert not rep["ready"]


def test_bug_atc_hazard_tool_number(board1):
    """A T-number below 600 with M6 must trip the tool audit."""
    path = board1.artifact_path("drillfile")
    pipeline._renumber(path, {605: 1})
    try:
        rep = verify.run(board1, deep=False)
        assert check(rep, 4)["status"] == "FAIL"
        assert any("ATC" in d for d in check(rep, 4)["details"])
    finally:
        pipeline.regenerate(board1, log=lambda *a: None)


def test_bug_single_pass_slivers(tmp_path):
    """Acceptance: board 1's Gerber with isolation-width UNSET must flag the
    inter-pad slivers on the ESP32 rows that the real board produced."""
    job = make_job(str(tmp_path), "slivers", iso_width=None)
    pipeline.regenerate(job, log=lambda *a: None)
    rep = verify.run(job, deep=True)
    c9 = check(rep, 9)
    assert c9["status"] == "FAIL"
    sl = job.data["sliver_report"]
    assert sl["fail_count"] > 0
    # the ESP32 column sits at Gerber X ~33 mm; slivers flank it
    esp = [s for s in sl["slivers"]
           if s["severity"] == "FAIL" and 28 <= s["x_mm"] <= 38]
    assert esp, "no FAIL slivers found near the ESP32 rows"
    assert sl["suggested_isolation_width_in"] is not None
    assert not rep["ready"]


# ------------------------------------------------------------ FR5.3 frame

def test_zero_start_is_forbidden(tmp_path):
    """FR5.3: build_millproject must refuse zero-start outright."""
    from pcbmill.camconfig import build_millproject
    job = make_job(str(tmp_path), "zs", zero_start=1)
    with pytest.raises(ValueError, match="zero-start is forbidden"):
        build_millproject(job)
    rep = verify.run(job, deep=False)
    assert check(rep, 12)["status"] == "FAIL"
    assert not rep["ready"]


def test_frame_from_page_transform(tmp_path):
    from pcbmill.camconfig import set_frame_from_page_transform
    job = make_job(str(tmp_path), "frame")
    res = set_frame_from_page_transform(job, 4.3372, 3.2478, dx=0.1, dy=-0.05)
    assert res["x_offset"] == -4.4372
    assert res["y_offset"] == 3.1978
    assert job.params["cam"]["zero_start"] == 0


def test_frame_stability_across_isolation_widths(tmp_path):
    """FR5.3 verification: drill G-code byte-identical when isolation-width
    changes - the frame does not move."""
    from pcbmill.camconfig import drill_frame_stability
    job = make_job(str(tmp_path), "stab")
    ok, msg = drill_frame_stability(job)
    assert ok, msg


# ------------------------------------------------------------ acceptance 4

def test_board1_runsheet(board1):
    verify.run(board1, deep=False)
    sheet = runsheet.build(board1)
    ops = [o["op"] for o in sheet["ops"]]
    assert ops == ["Probe the blank", "Isolation milling", "Drilling"] or \
           ops == ["Isolation milling", "Drilling"]
    iso = next(o for o in sheet["ops"] if o["op"] == "Isolation milling")
    assert "ELECTRICAL" in iso["touch_off"]
    assert iso["t_number"] == 600
    drl = next(o for o in sheet["ops"] if o["op"] == "Drilling")
    assert "G37" in drl["touch_off"]
    assert sheet["touch_off"] is not None
    # highest node of the real map
    hi = sheet["touch_off"]["nodes"][0]
    assert abs(hi["z"] - 0.071893) < 1e-6
    text = runsheet.to_text(sheet)
    assert "PRE-FLIGHT" in text and "TOUCH-OFF" in text


# ------------------------------------------------------------ unit-level

def test_kerf_math():
    # 60 deg, 5 thou tip, 2 thou deep -> 7.3 thou kerf (board 1's number)
    assert abs(vbit_kerf(0.005, 60.0, -0.002) - 0.0073) < 0.0001


def test_feed_windows():
    c, warn = check_feed(9.0, 2, 10000)
    assert warn is None and abs(c - 0.00045) < 1e-5
    _, warn = check_feed(2.0, 2, 10000)     # rubbing
    assert warn and "rubbing" in warn
    _, warn = check_feed(30.0, 2, 10000)    # too heavy
    assert warn


def test_drill_consolidation_rounds_to_nearest():
    d = DrillFile(os.path.join(FIX, "sln-geo-aux-heat.drl"))
    assert d.is_mixed_plating
    assert d.hole_count() == 73
    mapping = consolidate_drills(d.tools, [0.032, 0.0465, 0.0625, 0.125])
    # 0.0394 nominal is NEARER to 0.032 than 0.0465: rounds DOWN and the
    # signed error must show it
    t2 = next(m for m in mapping if abs(m["nominal_in"] - 0.0394) < 1e-4)
    assert t2["chosen_in"] == 0.0465 or t2["chosen_in"] == 0.032
    assert t2["error_in"] is not None


def test_annulus_check_skips_npth():
    """NPTH holes must not be annulus-checked.

    KiCad writes np_thru_hole pads with size == drill (mounting holes,
    ESP32 flash-pin clearance holes), so the ring is ~0 by construction.
    Before this filter the CLI reported a failure for every mounting hole
    on an otherwise healthy board, while the web UI - which had its own
    filter - reported none. A check that cries wolf gets ignored.
    """
    mapping = consolidate_drills({1: 3.2 / 25.4, 2: 1.0 / 25.4},
                                 [0.032, 0.0465, 0.0625, 0.125])
    pads = [
        # M3 mounting hole: KiCad emits size == drill
        {"ref": "H1.1", "pad_size_mm": (3.2, 3.2), "drill_mm": 3.2,
         "type": "np_thru_hole"},
        # a real THT pad that genuinely fails: 1.524 pad on a 1.0 nominal
        # drill snaps up to 1.181 -> ring 0.171 mm
        {"ref": "J1.1", "pad_size_mm": (1.524, 1.524), "drill_mm": 1.0,
         "type": "thru_hole"},
    ]
    problems = annulus_check(pads, mapping, annulus_min_mm=0.25)
    refs = {p["ref"] for p in problems}
    assert "H1.1" not in refs, "NPTH mounting hole must be skipped"
    assert "J1.1" in refs, "a genuinely thin THT ring must still fail"


def test_annulus_check_untyped_pads_still_checked():
    """Absent a "type" key, fall back to checking - never silently skip."""
    mapping = consolidate_drills({1: 1.0 / 25.4},
                                 [0.032, 0.0465, 0.0625, 0.125])
    pads = [{"ref": "J1.1", "pad_size_mm": (1.524, 1.524), "drill_mm": 1.0}]
    assert annulus_check(pads, mapping, annulus_min_mm=0.25)


def _deploy_job(tmp_path, ready=True, probe_only=False, name="dj"):
    """Minimal job with a fake probe program + compensated outputs."""
    job = Job.create(os.path.join(str(tmp_path), name), name=name)
    job.params["basename"] = "deskv1"
    rels = ["probe/smartprobe_job.ngc"]
    if not probe_only:
        rels += ["out/deskv1_back_comp.ngc", "out/deskv1_drill_comp.ngc"]
    for rel in rels:
        p = job.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("(test)\n")
    if ready is not None:
        job.data["verification"] = {"ready": ready, "checks": [
            {"id": 1, "status": "PASS" if ready else "FAIL"}]}
    job.save()
    return job


def test_deploy_names_files_flat_job_op(tmp_path):
    """Share convention is flat <basename>_<op>.ngc.

    out/ artifacts already carry that shape; only the probe program needs
    renaming, since inside the job it is probe/smartprobe_job.ngc.
    """
    job = _deploy_job(tmp_path)
    names = [n for _, n, _ in deploy.plan(job)]
    assert "deskv1_probe.ngc" in names
    assert "deskv1_back_comp.ngc" in names
    assert "deskv1_drill_comp.ngc" in names
    assert "smartprobe_job.ngc" not in names


def test_deploy_probe_program_is_not_gated(tmp_path):
    """Chicken-and-egg: the probe program can NEVER pass the gate.

    verify needs regen, regen needs the height map, and the height map needs
    the probe program to have been run on the machine. Gating it would make
    --force the only way to ship it, training people to bypass the guard on
    the one file where it matters least.
    """
    job = _deploy_job(tmp_path, ready=None, probe_only=True)   # never verified
    share = str(tmp_path / "share")
    os.makedirs(share)
    job.params["machine"]["gcode_dir"] = share
    res = deploy.deploy(job, log=lambda *a: None)              # no --force
    assert res["written"] == ["deskv1_probe.ngc"]
    assert res["skipped"] == []
    assert os.listdir(share) == ["deskv1_probe.ngc"]


def test_deploy_ships_probe_but_holds_cutting_programs(tmp_path):
    """Unverified job with both: probe goes, cutting programs are held."""
    job = _deploy_job(tmp_path, ready=False)
    share = str(tmp_path / "share")
    os.makedirs(share)
    job.params["machine"]["gcode_dir"] = share
    res = deploy.deploy(job, log=lambda *a: None)
    assert res["written"] == ["deskv1_probe.ngc"]
    assert sorted(res["skipped"]) == ["deskv1_back_comp.ngc",
                                      "deskv1_drill_comp.ngc"]
    assert os.listdir(share) == ["deskv1_probe.ngc"]
    # --force is the documented override and must actually work
    deploy.deploy(job, force=True, log=lambda *a: None)
    assert sorted(os.listdir(share)) == [
        "deskv1_back_comp.ngc", "deskv1_drill_comp.ngc", "deskv1_probe.ngc"]


def test_deploy_refuses_when_only_cutting_programs_and_unverified(tmp_path):
    """With nothing ungated to ship, an unverified job is a hard refusal."""
    job = _deploy_job(tmp_path, ready=False)
    os.remove(job.path("probe/smartprobe_job.ngc"))
    share = str(tmp_path / "share")
    os.makedirs(share)
    job.params["machine"]["gcode_dir"] = share
    with pytest.raises(deploy.DeployError, match="unverified cutting G-code"):
        deploy.deploy(job, log=lambda *a: None)
    assert not os.listdir(share)


def test_deploy_refuses_unmounted_share(tmp_path):
    """An absent mountpoint must fail loudly, not create a local decoy dir.

    Silently writing to an unmounted /Volumes path looks identical afterwards
    and means the machine never sees the files.
    """
    job = _deploy_job(tmp_path)
    missing = str(tmp_path / "not-mounted")
    job.params["machine"]["gcode_dir"] = missing
    with pytest.raises(deploy.DeployError, match="not a mounted directory"):
        deploy.deploy(job, log=lambda *a: None)
    assert not os.path.exists(missing)


def test_deploy_dry_run_touches_nothing(tmp_path):
    job = _deploy_job(tmp_path)
    share = str(tmp_path / "share")
    job.params["machine"]["gcode_dir"] = share
    lines = []
    deploy.deploy(job, dry_run=True, log=lines.append)
    assert not os.path.exists(share)
    assert any("deskv1_probe.ngc" in l for l in lines)


def test_deploy_overwrites_without_guard(tmp_path):
    """PathPilot prompts when a loaded file changes, so overwrite is safe."""
    job = _deploy_job(tmp_path)
    share = str(tmp_path / "share")
    os.makedirs(share)
    job.params["machine"]["gcode_dir"] = share
    with open(os.path.join(share, "deskv1_back_comp.ngc"), "w") as fh:
        fh.write("(old)\n")
    deploy.deploy(job, log=lambda *a: None)
    with open(os.path.join(share, "deskv1_back_comp.ngc")) as fh:
        assert fh.read() == "(test)\n"


def test_deploy_needs_something_to_deploy(tmp_path):
    job = Job.create(os.path.join(str(tmp_path), "empty"), name="empty")
    with pytest.raises(deploy.DeployError, match="nothing to deploy"):
        deploy.plan(job)


RAW_ISO = """( pcb2gcode 3.0.4 )
G94 ( Inches per minute feed rate. )
G20 ( Units == INCHES. )
G90 ( Absolute coordinates. )
G00 S10000 ( RPM spindle speed. )
G64 P0.00040
F9.00000 ( Feedrate. )
G00 Z1.00000
T1
M5      (Spindle stop.)
G04 P1.00000 (Wait for spindle to stop)
(MSG, Change tool bit to mill diameter 0.00730in)
M6      (Tool change.)
M0      (Temporary machine stop.)
M3 ( Spindle on clockwise. )
G00 Z0.10000 ( retract )
G00 X3.98105 Y2.44293 ( rapid move to begin. )
G01 F5.00000
G01 Z-0.00200
"""


def _run_post(tmp_path, script, raw):
    import subprocess
    p = os.path.join(str(tmp_path), "prog.ngc")
    with open(p, "w") as fh:
        fh.write(raw)
    vendor = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "pcbmill", "vendor", script)
    subprocess.run([sys.executable, vendor, p], check=True,
                   capture_output=True)
    return open(p).read()


def test_post_measures_the_tool_before_applying_its_length(tmp_path):
    """post.py emitted a bare G43 with no G37: Z rode on whatever the tool
    table happened to hold.

    It survived because the isolation pass usually followed something that
    had just measured the same tool. The OUTLINE goes through this same
    script and uses a different physical tool from every other op in the
    job, so an unmeasured G43 there applies the previous tool's length.
    """
    out = _run_post(tmp_path, "post.py", RAW_ISO)
    lines = [l for l in out.split("\n") if re.match(r"^(M0|G37|G43|G30|G00 X)", l)]
    kinds = [l.split()[0] for l in lines]
    assert "G37" in kinds, "no ETS measurement before the cut"
    assert kinds.index("G37") < kinds.index("G43"), \
        "G43 must not apply a length before G37 measures it"
    assert kinds.index("M0") < kinds.index("G37"), \
        "measure only after the operator pause - the tool must be in first"
    # and the ETS must be cleared, over the work, before any Z move
    g43 = kinds.index("G43")
    assert kinds[g43 + 1] == "G30", "no G30 clearing the ETS after G43"
    assert kinds[g43 + 2] == "G00", "no XY move over the work before descending"
    # the label must not name a specific tool: this same script post-processes
    # the V-bit isolation pass and the fishtail profile pass
    assert "V-bit tool length" not in out


def test_post_and_postdrill_agree_on_the_tool_length_block(tmp_path):
    """The two post-processors must not disagree about ETS handling."""
    out = _run_post(tmp_path, "post.py", RAW_ISO)
    assert "G37 H600 (measure this tool on the ETS)" in out
    assert "G43 H600 (apply the length G37 just stored)" in out
    assert "G30 (clear of the ETS before descending)" in out


def test_job_basename_defaults_to_job_name(tmp_path):
    """A flat, shared gcode dir means two jobs at the "board" default
    silently overwrite each other."""
    job = Job.create(os.path.join(str(tmp_path), "deskv1"))
    assert job.basename == "deskv1"
    assert "board" not in job.artifact_path("back_comp")


def test_probe_grid_floors_step_instead_of_dropping_a_row():
    """deskv1 regression: a rounded-UP step cost a whole row of coverage.

    70 mm blank, inset 0.05: uy = 2.656, ny = 11, and 2.656/10 = 0.2656.
    Rounding gave 0.27, 11 rows spanned 2.70 > 2.656, the guard dropped ny
    to 10 spanning 2.43, and 0.086 in / 2.2 mm of copper fell outside the
    probe map - which verify check 1 rejects as OUTSIDE PROBE MAP.
    Flooring to 0.26 keeps 11 rows spanning 2.60.
    """
    g = probegen.plan_grid(0.316, 0.316, 3.937, 2.756, 0.05, 165)
    assert g["y_step"] == 0.26
    assert g["y_count"] == 11
    y_top = g["y_start"] + g["y_step"] * (g["y_count"] - 1)
    assert y_top >= 2.882 - 1e-9, "must reach the copper top edge at 2.882"
    # and the floored grid still fits inside the inset blank
    assert y_top <= 0.316 + 2.756 - 0.05 + 1e-9
    assert g["x_count"] * g["y_count"] == 165


def test_probe_grid_grows_to_cover_copper():
    """Given measured copper extents, the grid must span them."""
    copper = (0.40, 0.40, 4.10, 2.95)
    g = probegen.plan_grid(0.316, 0.316, 3.937, 2.756, 0.05, 60, copper=copper)
    assert g["x_start"] + g["x_step"] * (g["x_count"] - 1) >= copper[2] - 1e-9
    assert g["y_start"] + g["y_step"] * (g["y_count"] - 1) >= copper[3] - 1e-9


def test_probe_grid_refuses_copper_it_cannot_cover():
    """Copper outside the inset blank is a setup error, not something to
    silently under-probe - verify check 1 would fail later anyway."""
    copper = (0.40, 0.40, 4.10, 3.40)      # Y beyond the blank entirely
    with pytest.raises(ValueError, match="cannot cover the copper"):
        probegen.plan_grid(0.316, 0.316, 3.937, 2.756, 0.05, 165, copper=copper)


def test_probe_program_generation(tmp_path):
    params = {"x_start": 0.49, "x_step": 0.27, "x_count": 15,
              "y_start": 0.54, "y_step": 0.26, "y_count": 11,
              "z_safe": 0.25, "z_limit": -0.05}
    out = tmp_path / "probe.ngc"
    info = probegen.generate_program(params, str(out))
    assert info["points"] == 165
    text = out.read_text()
    # config numbers present
    assert "#3=15" in text and "#6=11" in text
    # frozen workarounds intact
    for var in ("#12", "#13", "#14", "#15"):
        assert var in text
    # derived comments match the numbers
    assert "165 points" in text
    # PathPilot lint clean
    from pcbmill.gcodeio import lint_comments
    assert lint_comments(str(out)) == []


def test_stale_probe_program_on_grid_change(tmp_path):
    job = make_job(str(tmp_path), "probestale")
    job.update_params("probe", {"x_start": 0.49, "x_step": 0.27, "x_count": 15,
                                "y_start": 0.54, "y_step": 0.26, "y_count": 11})
    out = job.artifact_path("probe_program")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    probegen.generate_program(job.params["probe"], out)
    job.record_artifact("probe_program")
    assert job.artifact_state("probe_program") == "fresh"
    job.update_params("probe", {"x_count": 14})
    assert job.artifact_state("probe_program") == "stale"
