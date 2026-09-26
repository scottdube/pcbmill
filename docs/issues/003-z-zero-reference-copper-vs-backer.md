# DESIGN: Z0 is defined at the backer, but the backer is never touchable when probing happens

**Status:** open, 2026-09-26 (workaround in use)
**Severity:** medium — no bad output; the reference the design asks for is one the operator cannot reach
**Found by:** Scott, eyes-carrier-v2, blank already waxed down when it came time to zero

---

## Symptom

The probe program header says "ZERO Z ON THE FIXTURE the work is bonded to -
NOT on the work", and `verify.py` check 5 audits drill bottoms and outline
depth as absolute numbers below Z0 (breakthrough <= -0.003, backer damage
>= -0.030). But the blank is always waxed to the MDF before probing, wax
squeeze-out covers the MDF beside it, and MDF is compressible - there is no
clean backer to touch at the moment the touch-off happens.

Scott: "I don't think there's any reason to do it off the back, or really
should be off the copper in the first place."

## Evidence

- deskv1 (2026-08-13) map: copper 0.0646..0.0727, mean 0.0685 on 0.062
  stock. Frame Z0 was ~68 thou under the copper. Nothing on disk records
  whether that came from touching MDF or from touching copper and typing
  the thickness.
- eyes-carrier-v2: with Z0 on the copper and 0 typed in, every drill
  bottom would read -0.045 and check 5 would FAIL a physically correct job.

## Workaround (in use)

Touch the probe on the copper and type the calipered blank thickness into
TOUCH Z. The map then reads +thickness, and touchoff/regen/verify behave as
designed. The audit margins (0.0105 breakthrough, 0.0165 backer) absorb a
few thou of caliper error and the unknown wax film.

## What a copper-referenced frame would need

1. `probe_gen` header + runsheet: "touch off on the copper", Z0 = copper.
2. `verify` check 5: derive the backer from `blank.thickness_in` plus a
   wax-film allowance instead of assuming it is at Z0. The wax film is
   the unknown - deskv1's map implies ~6 thou if its backer touch was true.
3. `cam.zdrill` / `cam.zcut_outline` already act relative to local copper
   (applymap absolute: bottom = copper height + zdrill), so they do not
   change meaning; only the audit limits do.
4. `probe.z_limit` (#8): currently "below the fixture"; becomes
   "below the copper by more than any plausible bow".

## Why this is not just a doc fix

The absolute audit is the one check that does not need the blank thickness
to be right. Moving to a copper reference makes the backer-damage check
depend on a typed thickness and an assumed wax film. That is acceptable
(the margins are 3-5x the plausible error) but it must be said in the
check's output, not hidden.
