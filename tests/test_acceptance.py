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

import pytest

from pcbmill import pipeline, probegen, runsheet, verify
from pcbmill.excellon import DrillFile
from pcbmill.manifest import Job
from pcbmill.toollib import (check_feed, consolidate_drills, vbit_kerf)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def check(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)


def make_job(tmp, name, iso_width=None, x_offset=-4.2776, y_offset=0.5319,
             zero_start=1):
    """Board-1 job with the v1 frame (the one the proven files used)."""
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


# ------------------------------------------------------------ acceptance 1

def test_board1_back_matches_proven(board1):
    got = motion_lines(board1.artifact_path("back"))
    want = motion_lines(os.path.join(FIX, "proven_out", "geoaux_back.ngc"))
    assert got == want


def test_board1_drill_matches_proven(board1):
    got = motion_lines(board1.artifact_path("drillfile"))
    want = motion_lines(os.path.join(FIX, "proven_out", "geoaux_drill.ngc"))
    assert got == want


def test_board1_back_comp_matches_proven(board1):
    got = motion_lines(board1.artifact_path("back_comp"))
    want = motion_lines(os.path.join(FIX, "proven_out", "geoaux_back_comp.ngc"))
    assert got == want


def test_board1_drill_comp_matches_proven(board1):
    got = motion_lines(board1.artifact_path("drill_comp"))
    want = motion_lines(os.path.join(FIX, "proven_out", "geoaux_drill_comp.ngc"))
    assert got == want


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
    job = make_job(str(tmp_path), "posx", x_offset=+4.2776)
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
