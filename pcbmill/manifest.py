"""Job manifest and staleness tracking (FR1).

A job is one directory containing:
    job.json          - the manifest (this module owns it)
    inputs/           - copies of the KiCad exports (the job is self-contained)
    out/              - generated files; NOTHING in here is ever hand-edited
    probe/            - generated probe program + imported probe results

Staleness model (FR1.2): every generated artifact records, at generation
time, a fingerprint = sha256 over (the parameters that influence it +
the content hashes of its upstream files). An artifact is STALE when its
recorded fingerprint no longer matches the recomputed one, MISSING when
the file is absent, and TAMPERED when the file on disk no longer matches
the content hash recorded at generation (FR1.4 - nothing in out/ is
edited by hand).
"""

import copy
import hashlib
import json
import os
import re
import time

from .constants import MACHINE_DEFAULTS

MANIFEST = "job.json"

# Artifact dependency graph. Each artifact lists the parameter groups and
# upstream artifacts/inputs that feed it. Order = regeneration order.
def _safe_basename(name):
    """Job name -> a basename safe for G-code filenames on the share."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_")
    return s or "board"


ARTIFACTS = {
    "millproject": {
        "params": ["cam", "tools"],
        "inputs": ["gerber_back", "drill", "edge_cuts"],
        "upstream": [],
        "path": "out/millproject",
    },
    "probe_program": {
        "params": ["probe"],
        "inputs": [],
        "upstream": [],
        "path": "probe/smartprobe_job.ngc",
    },
    "back": {
        "params": ["cam", "tools"],
        "inputs": ["gerber_back", "drill"],
        "upstream": ["millproject"],
        "path": "out/{basename}_back.ngc",
    },
    "drillfile": {
        "params": ["cam", "tools"],
        "inputs": ["gerber_back", "drill"],
        "upstream": ["millproject"],
        "path": "out/{basename}_drill.ngc",
    },
    "outline": {
        "params": ["cam", "tools"],
        "inputs": ["gerber_back", "drill", "edge_cuts"],
        "upstream": ["millproject"],
        "path": "out/{basename}_outline.ngc",
        "optional": True,
    },
    "back_comp": {
        "params": ["cam", "machine"],
        "inputs": ["probe_map"],
        "upstream": ["back"],
        "path": "out/{basename}_back_comp.ngc",
    },
    "drill_comp": {
        "params": ["cam", "machine"],
        "inputs": ["probe_map"],
        "upstream": ["drillfile"],
        "path": "out/{basename}_drill_comp.ngc",
    },
    "outline_comp": {
        "params": ["cam", "machine"],
        "inputs": ["probe_map"],
        "upstream": ["outline"],
        "path": "out/{basename}_outline_comp.ngc",
        "optional": True,
    },
}

DEFAULT_PARAMS = {
    "basename": "board",
    "blank": {
        # Lower-left corner of the blank in G54 (DRO-read), inches.
        "x0": 0.0, "y0": 0.0,
        "width": 2.756, "height": 3.937,      # 70 x 100 mm default blank
        "thickness_in": 0.062,
    },
    "cam": {
        "zwork": -0.002,
        "zsafe": 0.1,
        "zchange": 1.0,
        "zdrill": -0.075,
        # None = single pass at the tool kerf. Board 1 proved a single pass
        # leaves floating slivers between pads - the sliver predictor (FR7.9)
        # will flag it; 0.030 was the fix on board 1.
        "isolation_width": None,
        "milling_overlap": "60%",
        "mill_feed": 9.0,
        "mill_vertfeed": 5.0,
        "mill_speed": 10000,
        "drill_feed": 25.0,
        "drill_speed": 10000,
        "zero_start": 0,           # fixed frame; zero-start=1 silently shifts
        "x_offset": None,          # None = not solved yet (use offset solver)
        "y_offset": None,
        "outline_enabled": False,
        "zcut_outline": -0.080,
        "outline_tool_diameter": 0.125,
        "outline_feed": 15.0,
    },
    "tools": {
        # ids reference the tool library (toollib.py)
        "isolation_tool": "vbit-60-005",
        "isolation_t_number": 600,
        "drill_t_base": 605,       # postdrill.py assigns 605, 606, ...
        "drills_available": [0.032, 0.0465, 0.0625, 0.125],
        "outline_t_number": 610,
    },
    "probe": {
        "x_start": None, "x_step": None, "x_count": None,
        "y_start": None, "y_step": None, "y_count": None,
        "z_safe": 0.25,            # #7 - above WCS Z0
        "z_limit": -0.05,          # #8 - probe depth limit
        "inset": 0.05,             # from copper extents when auto-gridding
        "target_points": 165,
    },
    "machine": dict(MACHINE_DEFAULTS),
}

INPUT_KEYS = ("gerber_back", "drill", "kicad_pcb", "edge_cuts")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Job:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.data = None

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, root, name=None):
        job = cls(root)
        if os.path.exists(job.manifest_path):
            raise FileExistsError("job already exists: %s" % job.manifest_path)
        os.makedirs(job.root, exist_ok=True)
        for sub in ("inputs", "out", "probe", "verify"):
            os.makedirs(os.path.join(job.root, sub), exist_ok=True)
        job_name = name or os.path.basename(job.root)
        job.data = {
            "name": job_name,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "params": copy.deepcopy(DEFAULT_PARAMS),
            "inputs": {},          # key -> {"file": rel path, "sha256": ...}
            "probe_map": None,     # {"file": rel, "sha256":, "imported": ts, "qa": {...}}
            "artifacts": {},       # name -> {"file":, "sha256":, "fingerprint":, "generated": ts}
            "verification": None,  # last verify report
            "notes": [],
        }
        # Default the output basename to the job name, not "board". The gcode
        # share is FLAT and shared across jobs, so two jobs left at the default
        # both deploy as board_back_comp.ngc and silently overwrite each other.
        job.data["params"]["basename"] = _safe_basename(job_name)
        job.save()
        return job

    @classmethod
    def load(cls, root):
        job = cls(root)
        with open(job.manifest_path) as fh:
            job.data = json.load(fh)
        # forward-fill any params added since the job was created
        job.data["params"] = _deep_merge(DEFAULT_PARAMS, job.data.get("params"))
        return job

    @property
    def manifest_path(self):
        return os.path.join(self.root, MANIFEST)

    def save(self):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.manifest_path)

    def path(self, rel):
        return os.path.join(self.root, rel)

    @property
    def params(self):
        return self.data["params"]

    @property
    def basename(self):
        return self.params.get("basename", "board")

    # ------------------------------------------------------------ inputs

    def set_input(self, key, source_path):
        """Copy an input file into the job (self-contained) and record it."""
        if key not in INPUT_KEYS:
            raise KeyError("unknown input key %r" % key)
        import shutil
        dest_rel = os.path.join("inputs", os.path.basename(source_path))
        dest = self.path(dest_rel)
        if os.path.abspath(source_path) != os.path.abspath(dest):
            shutil.copyfile(source_path, dest)
        self.data["inputs"][key] = {"file": dest_rel, "sha256": _sha256(dest)}
        self.save()
        return dest

    def input_path(self, key):
        rec = self.data["inputs"].get(key)
        return self.path(rec["file"]) if rec else None

    def set_probe_map(self, source_path, qa=None):
        import shutil
        dest_rel = os.path.join("probe", "probe-results.ngc")
        dest = self.path(dest_rel)
        if os.path.abspath(source_path) != os.path.abspath(dest):
            shutil.copyfile(source_path, dest)
        self.data["probe_map"] = {
            "file": dest_rel,
            "sha256": _sha256(dest),
            "imported": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "qa": qa,
        }
        self.save()
        return dest

    @property
    def probe_map_path(self):
        pm = self.data.get("probe_map")
        return self.path(pm["file"]) if pm else None

    # ------------------------------------------------------------ staleness

    def artifact_path(self, name):
        spec = ARTIFACTS[name]
        return self.path(spec["path"].format(basename=self.basename))

    def _current_hash_of(self, ref):
        """Content hash of an input key, 'probe_map', or artifact name."""
        if ref == "probe_map":
            pm = self.data.get("probe_map")
            return pm["sha256"] if pm else None
        if ref in INPUT_KEYS:
            rec = self.data["inputs"].get(ref)
            return rec["sha256"] if rec else None
        rec = self.data["artifacts"].get(ref)
        return rec["sha256"] if rec else None

    def fingerprint(self, name):
        """Recompute the fingerprint an up-to-date artifact would have."""
        spec = ARTIFACTS[name]
        parts = {"artifact": name}
        for group in spec["params"]:
            parts["param:" + group] = self.params.get(group)
        parts["param:basename"] = self.basename
        for ref in spec["inputs"] + spec["upstream"]:
            parts["dep:" + ref] = self._current_hash_of(ref)
        blob = json.dumps(parts, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def record_artifact(self, name):
        path = self.artifact_path(name)
        self.data["artifacts"][name] = {
            "file": os.path.relpath(path, self.root),
            "sha256": _sha256(path),
            "fingerprint": self.fingerprint(name),
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.save()

    def artifact_state(self, name):
        """missing | stale | tampered | fresh  (+ 'skipped' for optional)."""
        spec = ARTIFACTS[name]
        rec = self.data["artifacts"].get(name)
        path = self.artifact_path(name)
        if spec.get("optional") and not self._optional_enabled(name):
            return "skipped"
        if rec is None or not os.path.exists(path):
            return "missing"
        if _sha256(path) != rec["sha256"]:
            return "tampered"
        if rec["fingerprint"] != self.fingerprint(name):
            return "stale"
        # upstream freshness is transitive: a fresh fingerprint over a stale
        # upstream hash is impossible because the upstream hash changes when
        # it regenerates; but a *stale upstream* with an unchanged file means
        # our fingerprint still matches - so check upstream states too.
        for up in spec["upstream"]:
            if self.artifact_state(up) in ("stale", "missing", "tampered"):
                return "stale"
        return "fresh"

    def _optional_enabled(self, name):
        if name in ("outline", "outline_comp"):
            return bool(self.params["cam"].get("outline_enabled"))
        return True

    def states(self):
        return {n: self.artifact_state(n) for n in ARTIFACTS}

    def all_fresh(self):
        return all(s in ("fresh", "skipped") for s in self.states().values())

    # ------------------------------------------------------------ params

    def update_params(self, group, values):
        """Update one parameter group. Downstream artifacts go stale
        automatically because fingerprints include the parameters."""
        if group not in self.params:
            raise KeyError("unknown parameter group %r" % group)
        if not isinstance(self.params[group], dict):
            self.params[group] = values
        else:
            self.params[group] = _deep_merge(self.params[group], values)
        self.save()
