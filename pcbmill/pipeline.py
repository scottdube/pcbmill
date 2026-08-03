"""The regeneration chain (FR1.3, FR6):

    millproject -> pcb2gcode -> post.py / postdrill.py -> applymap.py x n

One action, always the full chain, always in order. The vendored scripts
are run exactly as proven; the only post-step the app adds is tool
RENUMBERING when the job's T-numbers differ from the scripts' defaults
(post.py writes T600, postdrill.py writes T605+).
"""

import os
import re
import subprocess
import sys
import time

from . import camconfig
from .manifest import ARTIFACTS

VENDOR = os.path.join(os.path.dirname(__file__), "vendor")
PY = sys.executable


class PipelineError(RuntimeError):
    pass


def _run(cmd, cwd, log_lines, timeout=600):
    log_lines.append("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)
    if proc.stdout:
        log_lines.extend(proc.stdout.rstrip().split("\n"))
    if proc.stderr:
        log_lines.extend(proc.stderr.rstrip().split("\n"))
    if proc.returncode != 0:
        raise PipelineError("%s failed (exit %d)\n%s\n%s"
                            % (cmd[0], proc.returncode,
                               proc.stdout[-1000:], proc.stderr[-1000:]))
    return proc


def _renumber(path, mapping):
    """Renumber T/H words (T, G43 H, G37 H) per {old: new}."""
    if not mapping or all(k == v for k, v in mapping.items()):
        return
    text = open(path).read()

    def sub_word(word, m_):
        n = int(m_.group(1))
        return "%s%d" % (word, mapping.get(n, n))

    text = re.sub(r"\bT(\d+)", lambda m_: sub_word("T", m_), text)
    text = re.sub(r"\bH(\d+)", lambda m_: sub_word("H", m_), text)
    with open(path, "w") as fh:
        fh.write(text)


def regenerate(job, log=print):
    """Run the full chain. Requires inputs; the applymap stages also
    require an imported probe map (skipped with a loud note otherwise)."""
    lines = []
    t0 = time.time()
    out_dir = job.path("out")
    os.makedirs(out_dir, exist_ok=True)

    # 1. millproject ------------------------------------------------------
    mp_text = camconfig.build_millproject(job)
    mp_path = job.artifact_path("millproject")
    with open(mp_path, "w") as fh:
        fh.write(mp_text)
    job.record_artifact("millproject")
    log("millproject written")

    # 2. pcb2gcode --------------------------------------------------------
    _run([camconfig.PCB2GCODE, "--config", os.path.relpath(mp_path, job.root)],
         job.root, lines)
    back = job.artifact_path("back")
    drill = job.artifact_path("drillfile")
    for f, name in ((back, "back"), (drill, "drill")):
        if not os.path.exists(f):
            raise PipelineError("pcb2gcode did not produce %s" % f)
    log("pcb2gcode ok")

    # 3. post.py / postdrill.py (vendored, run as proven) -----------------
    _run([PY, os.path.join(VENDOR, "post.py"),
          os.path.relpath(back, job.root)], job.root, lines)
    _run([PY, os.path.join(VENDOR, "postdrill.py"),
          os.path.relpath(drill, job.root)], job.root, lines)

    # renumber if the job wants different T-numbers than the scripts emit
    iso_t = job.params["tools"]["isolation_t_number"]
    _renumber(back, {600: iso_t})
    base = job.params["tools"]["drill_t_base"]
    if base != 605:
        drill_tools = sorted(set(
            int(m) for m in re.findall(r"^T(6\d\d)\s*$",
                                       open(drill).read(), re.M)))
        _renumber(drill, {t: base + (t - 605) for t in drill_tools})

    outline = None
    if job.params["cam"].get("outline_enabled"):
        outline = job.artifact_path("outline")
        if os.path.exists(outline):
            _run([PY, os.path.join(VENDOR, "post.py"),
                  os.path.relpath(outline, job.root)], job.root, lines)
            _renumber(outline, {600: job.params["tools"]["outline_t_number"]})
        else:
            outline = None
            lines.append("NOTE: outline enabled but pcb2gcode produced none")

    job.record_artifact("back")
    job.record_artifact("drillfile")
    if outline:
        job.record_artifact("outline")
    log("post-processing ok")

    # 4. applymap.py per file --------------------------------------------
    pm = job.probe_map_path
    if not pm:
        lines.append("NO PROBE MAP IMPORTED - compensation SKIPPED. The job "
                     "cannot become ready without _comp files.")
        log("WARNING: no probe map; skipped applymap")
    else:
        mode = job.params["machine"].get("applymap_mode", "absolute")
        pairs = [(back, job.artifact_path("back_comp"), "back_comp"),
                 (drill, job.artifact_path("drill_comp"), "drill_comp")]
        if outline:
            pairs.append((outline, job.artifact_path("outline_comp"),
                          "outline_comp"))
        for src, dst, name in pairs:
            _run([PY, os.path.join(VENDOR, "applymap.py"),
                  "-m", os.path.relpath(pm, job.root),
                  "-i", os.path.relpath(src, job.root),
                  "-o", os.path.relpath(dst, job.root),
                  "--mode", mode], job.root, lines)
            job.record_artifact(name)
        log("applymap ok (%s mode)" % mode)

    lines.append("regeneration finished in %.1f s" % (time.time() - t0))
    with open(job.path("out/regen.log"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return lines
