"""Copy runnable G-code to the machine (FR7.11).

The share is the last point at which pcbmill can tell the difference between
a verified job and a broken one. Once a file is on /Volumes/Tormach/gcode it
is indistinguishable from any other file, and the next thing that happens to
it is cycle start. So deploy is gated on the persisted verification result
rather than being a plain copy.

Naming on the share is flat: <basename>_<op>.ngc. The out/ artifacts already
carry that shape ({basename}_back_comp.ngc etc); only the probe program needs
renaming, since it lives at probe/smartprobe_job.ngc inside the job.

Overwriting a file PathPilot currently has loaded is safe: it detects the
change and prompts

    "File changed on disk.  Reload?"   [CANCEL] [OK]

leaving the operator in control. Confirmed on the machine 2026-08-13. So
there is deliberately no overwrite guard here - adding one would trade a
real prompt at the console for a false obstacle at the laptop.
"""

import os
import shutil

# Artifacts that are actually run on the machine. The uncompensated back /
# drillfile / outline are intermediates - deploying them just invites loading
# the wrong file at the console.
RUNNABLE = ["back_comp", "drill_comp", "outline_comp"]


class DeployError(Exception):
    pass


def plan(job):
    """Return [(source_abs, dest_name), ...] without touching the filesystem.

    Raises DeployError if nothing runnable exists yet.
    """
    items = []

    probe = job.artifact_path("probe_program")
    if os.path.exists(probe):
        items.append((probe, "%s_probe.ngc" % job.basename))

    for name in RUNNABLE:
        path = job.artifact_path(name)
        if os.path.exists(path):
            items.append((path, os.path.basename(path)))

    if not items:
        raise DeployError(
            "nothing to deploy: no probe program and no compensated output.\n"
            "  run 'pcbmill probe-gen' first, or 'pcbmill regen' after "
            "importing a probe map")
    return items


def check_ready(job):
    """None if the job verified clean, else the reason it didn't."""
    v = job.data.get("verification") or {}
    if not v:
        return "job has never been verified - run 'pcbmill verify'"
    if not v.get("ready"):
        fails = [c["id"] for c in v.get("checks", [])
                 if c.get("status") == "FAIL"]
        return ("verification FAILED (checks: %s) - run 'pcbmill verify'"
                % (", ".join(str(f) for f in fails) or "see report"))
    return None


def deploy(job, force=False, dry_run=False, log=print):
    """Copy the runnable set to params["machine"]["gcode_dir"]."""
    dest_dir = job.params["machine"].get("gcode_dir")
    if not dest_dir:
        raise DeployError("machine.gcode_dir is not set")

    items = plan(job)

    reason = check_ready(job)
    if reason:
        if not force:
            raise DeployError(
                "%s\n  refusing to put unverified G-code on the machine.\n"
                "  override with --force if you know what you are doing"
                % reason)
        log("WARNING: %s" % reason)
        log("WARNING: deploying anyway because --force was given")

    if dry_run:
        log("would copy to %s:" % dest_dir)
        for src, name in items:
            log("  cp %s %s" % (src, os.path.join(dest_dir, name)))
        return [name for _, name in items]

    # Fail loudly on an unmounted share rather than silently creating a plain
    # local directory at the mountpoint, which looks identical afterwards and
    # means the machine never sees the files.
    if not os.path.isdir(dest_dir):
        raise DeployError(
            "%s is not a mounted directory.\n"
            "  mount the Tormach SMB share, or set a different destination:\n"
            "    pcbmill set machine.gcode_dir <path>" % dest_dir)

    written = []
    for src, name in items:
        dest = os.path.join(dest_dir, name)
        existed = os.path.exists(dest)
        shutil.copyfile(src, dest)
        log("  %s %s" % ("overwrote" if existed else "wrote    ", dest))
        written.append(name)

    log("")
    log("deployed %d file(s) to %s" % (len(written), dest_dir))
    log("PathPilot will prompt to reload any of these it already has open.")
    return written
