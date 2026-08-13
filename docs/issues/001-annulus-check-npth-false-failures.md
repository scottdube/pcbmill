# BUG: annulus check reports a failure for every NPTH hole (CLI path only)

**Status:** fixed, 2026-08-13
**Severity:** medium — no bad output, but it makes a safety check untrustworthy
**Found by:** Scott, while adding mounting holes per the shop guide's new §0b

---

## Symptom

`pcbmill consolidate` reports an ANNULUS FAILURE for every mounting hole on an
otherwise healthy board:

```
ANNULUS FAILURES ((pad-drill)/2 < 0.25 mm, no plating!):
  H1.1: pad (3.2, 3.2) mm, bit 3.175 mm -> ring 0.013 mm
```

## Cause

KiCad writes `np_thru_hole` pads with **pad size equal to drill size** — that's
what NPTH means. So `(pad − drill) / 2` is ~0 **by construction**, and the check
fires on every one.

The 0.013 mm above isn't even zero only because 3.2 mm snaps down to the 3.175 mm
(1/8″) bit. The ring is an artifact of bit rounding, not a real ring.

**The bug is a divergence, not a plain omission.** `server.py` had the filter:

```python
for p in Board(job.input_path("kicad_pcb")).drilled_pads()
if p["type"] == "thru_hole"]                      # <- server.py had this
```

`cli.py` did not. It also **discarded the `type` field** when building the pad
dicts, so `annulus_check()` had no way to filter even in principle — despite
`kicadpcb.py:79` parsing `type` correctly all along.

Result: the web UI reported the board clean while the CLI reported N failures on
the same job. **The documented path (`pcbmill consolidate`, per the shop runbook)
is the broken one.**

## Why it matters more than a cosmetic warning

The shop guide *requires* NPTH holes in two places — mounting holes (§0b) and the
ESP32 GPIO6–11 flash-pin clearance holes (§3). So following the documented design
rules **guarantees** a wall of annulus failures on every board.

A gate that always fails is a gate people learn to scroll past — and this one
exists to catch the genuine case (a 1.0 mm nominal drill snapping *up* to 1.181
and collapsing a real pad's ring to 0.171 mm). Losing that to alarm fatigue is
the actual cost.

## Fix

Filter in `annulus_check()` rather than in each caller, so a third caller can't
reintroduce the divergence:

**`pcbmill/toollib.py`**
```python
for p in pads_with_drills:
    if p.get("type") == "np_thru_hole":
        continue
```

**`pcbmill/cli.py`** — pass `type` through (was dropped):
```python
"ref": "%s.%s" % (p["ref"], p["pad"]), "type": p["type"]}
```

**`pcbmill/server.py`** — pass `type` through, drop the now-redundant local filter
so both callers are identical.

`.get()` is deliberate: a pad dict with no `type` key is still **checked**, never
silently skipped.

## Verified

```
AFTER FIX  -> [('J1.1', 0.171)]
BEFORE FIX -> [('H1.1', 0.013), ('J1.1', 0.171)]
```

`H1.1` = M3 mounting hole (3.2 mm NPTH → 3.175 mm bit). `J1.1` = genuine thin
ring, 1.524 mm pad on a 1.0 mm nominal drill snapping up to 1.181 mm. The real
failure still fires.

## Regression tests

There were **no tests for `annulus_check` at all** — which is why this shipped.
Added to `tests/test_acceptance.py`:

- `test_annulus_check_skips_npth` — NPTH skipped, genuine thin THT ring still caught
- `test_annulus_check_untyped_pads_still_checked` — missing `type` falls back to checking

Both pass. The 14 other failures in the suite are `FileNotFoundError: 'pcb2gcode'`
in the sandbox used to verify this, unrelated to the change — **run the full suite
on the Mac before committing.**

## Follow-up worth considering

Should NPTH holes get their own check? There is a real constraint the current code
doesn't cover: **copper clearance around an NPTH hole.** The pour fills to the drill
edge unless a keepout is drawn (0.00 mm clearance, DRC errors, bit shearing copper
with no isolation). That's a different measurement from an annular ring — pour edge
to hole edge, not pad edge to hole edge — and it's currently caught only by KiCad
DRC, not by `pcbmill`.
