# G37 on the isolation cutter overwrote the electrical touch-off

**Found by:** Scott, 2026-09-26, reading the eyes-carrier-v2 run sheet:
"are you having it check tool length on all but 600?"
**Status:** fixed 2026-09-26, 46 tests green, fixtures re-blessed.

## What was wrong

Issue 002 (2026-08-13) put `G37` + `G43` after the `M0` in **every** program
for **every** tool, and made verify check 4 demand it. The motivating case
was the outline cutter — a different physical tool that had been applying
the previous tool's length. The rule was applied to the isolation V-bit too.

But the run sheet, README and constants all say the isolation cutter is
touched off **electrically, mandatory**, because the ETS reads
**+4.1 thou** off the work frame (`ets_frame_offset_in`) and the isolation
cut is **2 thou** deep. The operator touches the V-bit on the copper with a
meter, types the map's high-node value into TOUCH Z — and the program then
drove to the ETS, re-measured H600 and applied that instead. Whether that
moved the cut depends on what H600 held at touch-off time: if it was a
previous ETS reading of the same tool, ~nothing; if it was anything else,
up to the full 4.1 thou, i.e. air or a 6 thou cut. deskv1 (Aug 13) was cut
with `G43 H600` only and came out right; every program generated since
issue 002 has had the G37. Nobody caught the contradiction between the
program and its own run sheet until Scott read the sheet.

## Fix

- `post.py --electrical`: after the `M0`, emit only
  `G43 H600 (apply the ELECTRICALLY touched-off length - no G37, ...)`.
  No G30/G00 guard either — the spindle never left the tool-change position.
- `pipeline.regenerate` passes `--electrical` for the **back** program only.
  Drill programs (`postdrill.py`) and the outline program keep G37: the
  4.1 thou bias is nothing on a through-hole or a 50 thou profile cut.
  Scott's rule, verbatim: G37 "on all but 600".
- verify check 4 now **inverts** for the isolation tool: a G37 on it is a
  FAIL ("overwrites the ELECTRICAL touch-off"); every other tool still needs
  G37 before G43.
- Run sheet: the outline op now says G37/ETS in-program, matching the file.

## Fixtures

`geoaux_back.ngc` / `geoaux_back_comp.ngc` re-blessed. The diff is exactly
the four issue-002 lines out and one G43 in — motion lines back to **7043**,
the count of the bytes that physically cut board 1. So this returns the
fixtures to board 1's real tool block, which issue 002 had moved away from.

## Rule

A tool that is touched off by hand must never be re-measured by the program.
When a verify rule is generalised from one tool to all, check it against the
run sheet's per-tool touch-off column — the two documents describe the same
machine and cannot disagree.
