# pcbmill

Cockpit for milling PCBs on the Tormach 1100MX. Wraps the proven pipeline —
`pcb2gcode → post.py / postdrill.py → applymap.py` plus the smartprobe
grid-probing program — behind one job directory, one **Regenerate all**
button, a 10-point verification suite, and a printable run sheet.

It runs the existing tools (vendored unchanged in `pcbmill/vendor/`); it does
not reimplement CAM and never talks to the machine.

## Install

```bash
cd pcbmill
python3 -m venv .venv
.venv/bin/pip install -e .
```

External requirements (Homebrew): `pcb2gcode` (3.0.4) and `gerbv`
(used to rasterise the Gerber for sliver prediction).

## Web UI

```bash
.venv/bin/pcbmill serve --jobs-root ~/PCBMill
```

Then open http://127.0.0.1:8337. Left rail = pipeline stages with status
badges; the verification report is the landing page for a job. The command
is run from the repo root (`.venv` is relative).

**PCBMill.app** does both from Finder/Dock/Spotlight: `app/build.sh` builds it
into `~/Applications`. It starts the server only if nothing answers on 8337,
then opens the browser. The server is detached and keeps running after the app
exits; log in `~/Library/Logs/pcbmill.log`, stop with `pkill -f 'pcbmill serve'`.
The repo path is baked in at build time, so rebuild if the repo moves.

## CLI (every UI action, scriptable)

```bash
pcbmill new jobdir --back B_Cu.gbr --drill merged.drl --pcb board.kicad_pcb
cd jobdir
pcbmill status                 # inputs, artifact staleness, readiness
pcbmill drill-info             # parsed drill table (before anything generates)
pcbmill consolidate            # drill->bit mapping + annulus check
pcbmill set cam.isolation_width 0.030
pcbmill frame 4.3372 3.2478 --verify   # fixed frame from the KiCad-page
                               # transform (FR5.3): x-offset=-MX, y-offset=MY;
                               # --verify proves drill byte-stability
pcbmill solve-offsets          # measurement-based placement solver
pcbmill probe-gen              # emit smartprobe program (frozen motion + config)
pcbmill probe-import probe-results.ngc   # QA: span, plane, roughness
pcbmill touchoff               # highest-node touch-off + validation nodes
pcbmill regen                  # the WHOLE chain, then verify
pcbmill verify                 # 10-check suite; blocks "job ready" on FAIL
pcbmill runsheet               # printable operator sheet
```

## Job model

A job is one self-contained directory:

```
job/
  job.json      manifest: params, input hashes, artifact fingerprints
  inputs/       copies of the KiCad exports
  probe/        generated probe program + imported probe-results.ngc
  out/          generated G-code - NEVER edited by hand (edits are detected)
```

Any parameter or input change flips downstream artifacts **stale**
(fingerprint mismatch); hand edits in `out/` show as **TAMPERED**. Both
block "job ready" until you Regenerate all.

## The verification suite (why this exists)

0. Inputs — back Gerber present; ONE merged MixedPlating Excellon (split
   PTH/NPTH pairs rejected)
1. Extents — every file vs blank and probe map
2. Mirror proof — drill X histogram must match the Excellon reversed;
   spans equal to 4 decimals; top column group lands on the mirrored side
3. Hole count — G-code plunges == Excellon holes
4. Tool block audit — T-numbers in the assigned set (≥600 so M6 never
   fires the ATC), G43 H match, G54, G30, no bare feed-before-tool
5. Z audit — cutting Z == map ± zwork; rapids == zsafe + map mean; drill
   breakthrough ≥ 3 thou into backer but above the damage limit; retracts
   clear copper; probe #7 clears copper
6. Clamp count — 0 for cutting ops (map must cover the toolpath)
7. Comment/paren lint — PathPilot-safe: balanced, no nesting, O-words paired
8. Staleness — all hashes current
9. Sliver prediction — gerbv render + distance transform; gap regions the
   isolation band won't consume = floating copper. FAIL within 1 mm of a
   solderable pad, PLANNED inside declared pour-exclusion rule areas,
   WARN elsewhere. Suggests the minimum isolation-width that zeroes FAILs.
10. Design intake lint (report-only) — zone clearance, solid pours, round
    pads on fine-pitch rows, pour-exclusion areas
12. Fixed frame (FR5.3) — zero-start is forbidden (the bounding-box origin
    silently shifts every output when isolation-width changes); explicit
    offsets required; full verify regenerates at a second isolation-width
    and proves the drill G-code is byte-identical

## Machine conventions baked in (configurable per job)

- G54 Z0 on the **backer**; the map carries absolute copper height;
  applymap runs in absolute mode (relative mode requires an explicit
  confirmation — its failure mode is a 70-thou crash)
- Isolation cutter touch-off is **electrical**, mandatory (ETS reads
  +4.1 thou high vs the work frame), and its program applies H600 with
  **no G37**; every other tool (drills, outline cutter) is measured by
  G37/ETS in-program
- Probe is T99/H99; spindle max 10k RPM; manual-prompt tools ≥ T600
- The pcb2gcode autoleveller is never emitted (`al-*` produces Mach3 G31)
- The coordinate frame is always `zero-start=0` with explicit offsets from
  the KiCad-page transform (`x-offset = −MX`, `y-offset = MY`, optionally a
  DRO-read placement delta); on this mirrored back-side board x-offset acts
  in mirrored X — frames are verified by measurement, never assumption

## Tests

```bash
.venv/bin/python -m pytest tests/
```

19 tests: byte-level recreation of board 1 (sln-geo-aux-heat) from its
real inputs, plus one test per historical bug (front=, split drills,
positive x-offset on the mirrored frame, stale/tampered outputs, probe #7
below copper, short probe grid, ATC tool numbers, single-pass slivers on
the ESP32 rows).
