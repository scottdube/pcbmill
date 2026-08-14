# BUG BATCH 2 — unmeasured tool length, and four smaller findings

**Found by:** Scott, cutting deskv1, 2026-08-13
**Status:** all five fixed. **One decision outstanding — see "Proven fixtures" at the end.**

---

## 1. HIGH — `post.py` applied a tool length it never measured

`pcbmill/vendor/post.py` inserted a bare `G43` after `M6`:

```python
s = re.sub(r'^(M6.*)$', r'\1\nG43 H%d (apply V-bit tool length)' % N, ...)
```

No `G37`. Z rode on whatever was last stored in that H register. `postdrill.py` had it right all along, and its own docstring states the ETS precondition.

**Two things made this worse than it looks.**

It applied the length *before* the `M0` operator pause, so the length was applied before the operator had necessarily finished changing the tool.

And **the outline is the dangerous case.** `post.py` post-processes both the isolation pass and the profile pass. The profile cutter is a **different physical tool from every other op in the job** — V-bit (T600) for isolation, drills (T605–607), fishtail (T615) for the profile. The one program with the greatest need for a fresh measurement was the only one omitting it. Run it after swapping to the fishtail and it applies the previous tool's length: into the fixture, or cutting air.

**It hadn't bitten yet by accident.** On deskv1 the outline followed the FR8 clearing program, which `auxgen.clear_gcode` generates *with* `G37 H610` — so the table happened to hold a fresh measurement of the exact tool the outline then used. Correctness by coincidence.

**Fix:** mirror `postdrill.py` exactly — `G37` then `G43`, inserted after `M0`, followed by `G30` to clear the ETS and a `G00 X Y` over the work before any Z move (after `G37` the spindle is parked at the ETS, where the program's own "retract" to Z0.1 can be a descent).

Verified on a real isolation file:

```
T600
M6      (Tool change.)
M0      (Temporary machine stop.)
G37 H600 (measure this tool on the ETS)
G43 H600 (apply the length G37 just stored)
G30 (clear of the ETS before descending)
G00 X3.98105 Y2.44293 (over the work first)
M3
```

Byte-identical in shape to what `postdrill.py` produces.

**`auxgen.stair_gcode` had the same defect** and was not in the report — `G43 H600` with no `G37`, and before the `M0`. Fixed the same way. All four generators (`post.py`, `postdrill.py`, `clear_gcode`, `stair_gcode`) now emit the identical block.

## 2. MEDIUM — verify check 4 could not catch it

The check *knew* the invariant but scoped it to drill files:

```python
if name.startswith("drill"):
    for t in ana.tools:
        if t not in ana.g37_h: ...
```

So an isolation or outline program applying an unmeasured length passed clean:

```
outline_comp: T610 ok, G43/G54/G30 present     [PASS]
```

**Fix:** the requirement now applies to **every** tool in **every** program, and checks **order**, not just presence — a `G43` preceding its `G37` still applies a stale length. `GCodeAnalysis` gained `tool_len_ops`, a `("G37"|"G43", H)` list in file order, because two independent presence lists can't answer an ordering question.

Confirmed against real files:

| file | tools | order | verdict |
|---|---|---|---|
| `geoaux_back.ngc` (as cut on board 1) | 600 | `['G43']` | **FAIL — unmeasured length** |
| `geoaux_drill.ngc` | 605–607 | `['G37','G43']` each | PASS |

The new check fails the exact file that shipped.

## 3. LOW — hardcoded "V-bit" label

`post.py` labelled every `G43` `(apply V-bit tool length)` regardless of tool, so the profile program called a 1/8″ fishtail a V-bit, at cycle start. Now reads `(apply the length G37 just stored)` — tool-agnostic, and identical to `postdrill.py`. **Third stale machine-facing string this session**, after the FlatCAM reference and two Z-zero lines.

## 4. LOW — FR8 clearing had no CLI path

`auxgen.clear_gcode` was reachable only through `pcbmill serve`. Clearing a keepout meant importing `auxgen` in a Python shell and calling `applymap.py` by hand — putting the one operation with **hard safety refusals** on the least scripted path.

**Added `pcbmill clear`:**

```
pcbmill clear <region> --tool 0.125 --depth -0.01 [--tool-number 610]
```

It generates **and** compensates (an uncompensated aux program isn't runnable), and carries over both refusals from the web path: region must contain no copper, and must be a *full* copper keepout. It also refuses if `solve-offsets` hasn't run, since the region has to be placed in the same frame as the toolpaths.

## 5. LOW — `basename` defaulted to `"board"`

The gcode share is flat and shared, so two fresh jobs both deploy `board_back_comp.ngc` and silently overwrite each other. `Job.create` now defaults `basename` to the job name, sanitised for filenames.

**Note for the report's own caveat:** changing `basename` marks the probe program stale even though the map is unaffected, because `basename` is in the `probe_program` fingerprint. Defaulting it at creation avoids the re-run entirely. Left the fingerprint alone — the staleness is technically correct, since the deployed *filename* does change.

---

## The proven fixtures — RESOLVED: re-blessed

`tests/fixtures/proven_out/geoaux_back.ngc` and `geoaux_back_comp.ngc` were cut **without** the `G37`. The fix adds three lines, so:

- `test_board1_back_matches_proven` and `test_board1_back_comp_matches_proven` **will now fail on line count**
- running `verify` against board 1's proven output now correctly reports **FAIL — applies an UNMEASURED tool length**

That second one is a true statement about how board 1 was actually cut. The question is what "proven" should mean:

- **Re-bless** — regenerate the fixtures with the fixed `post.py` and treat them as "what pcbmill produces now, verified correct." Loses the historical record of what physically ran.
- **Keep and annotate** — leave them as the historical artifact and exempt those two tests, documenting that board 1 predates the fix.

**Verified before re-blessing.** Rather than eyeball a 6000-line diff, post.py's edits were inverted on the proven file to recover the raw pcb2gcode input, and the new post.py run against that — which isolates this change from the ~0.00005 in frame quantum that already separates the FR5.3 fixed frame from the zero-start frame the proven files were cut under (`assert_equivalent` carries `tol=2e-4` for exactly that reason):

```
proven motion lines : 7043
new post.py         : 7046   (delta +3)

  -G43 H600 (apply V-bit tool length)
  +G37 H600 (measure this tool on the ETS)
  +G43 H600 (apply the length G37 just stored)
  +G30 (clear of the ETS before descending)
  +G00 X3.98105 Y2.44293 (over the work first)
```

One line out, four in. **No motion lines added and no coordinate altered by post.py.** The drill fixtures are untouched — `postdrill.py` was not modified, and the other changed files either add a manifest key or are read-only analysis.

`geoaux_back.ngc` and `geoaux_back_comp.ngc` were re-blessed on that basis. **Suite: 38 passed.**

**What the re-blessed fixtures now mean:** they are no longer the bytes that cut board 1 — board 1 was cut *without* the G37. That history lives in this document, which is a better home for it than a test fixture.

## Test status

38 passing.
