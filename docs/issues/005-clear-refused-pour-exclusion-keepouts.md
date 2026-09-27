# 005 — `clear` refused every pour-exclusion keepout

**Found:** 2026-09-26, eyes-carrier-v2. `pcbmill regions` listed seven B.Cu
rule areas, three of them `EMPTY - safe to raster`, and `pcbmill clear`
refused all seven: "not a full copper keepout".

**Cause:** the refusal demanded tracks, vias, pads AND copperpour all
`not_allowed`. KiCad's normal way to keep the ground pour off an antenna or
a flash LED is a rule area with only `copperpour=not_allowed`. That is the
region the clearing cycle exists for — the isolation cut leaves it as a
floating copper island — so the rule refused exactly its own use case.

**Fix:** `auxgen.keepout_refusal(restrictions, empty)` is the one policy for
the CLI and the web page. It refuses a region holding copper (whatever the
rules say) and a region that admits the pour (nothing to clear; rastering
it cuts ground copper). A pour-only area that is empty is clearable.
Tests: `tests/test_clear_policy.py`.

**Rejected:** keeping the strict rule and telling the user to change the
KiCad rule area to a full keepout. That edits the design to satisfy the
tool, and a full keepout is wrong for these areas — the tracks to the LED
and antenna pads have to enter them.

**Trap that remains:** `clear` defaults the tool number to
`tools.outline_t_number`. Once the outline moves to another cutter (T615
fishtail here) the 1/8" clearing needs `--tool-number 610` explicitly.
