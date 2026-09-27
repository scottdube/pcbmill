"""Which rule areas `pcbmill clear` may raster.

A KiCad pour-exclusion area (copperpour=not_allowed, everything else
allowed) is the normal way to keep the ground pour away from an antenna or
a flash LED. The isolation cut leaves it as a floating copper island, so it
is exactly the region the clearing cycle exists for. eyes-carrier-v2
(2026-09-26) had seven of them and `clear` refused every one as "not a
full copper keepout", although three held nothing at all.
"""

from pcbmill.auxgen import keepout_refusal

POUR_ONLY = {"tracks": "allowed", "vias": "allowed", "pads": "allowed",
             "copperpour": "not_allowed", "footprints": "allowed"}
FULL = {k: "not_allowed" for k in ("tracks", "vias", "pads", "copperpour")}
NOT_A_KEEPOUT = dict(POUR_ONLY, copperpour="allowed")


def test_empty_pour_exclusion_is_clearable():
    assert keepout_refusal(POUR_ONLY, empty=True) is None


def test_full_keepout_is_clearable():
    assert keepout_refusal(FULL, empty=True) is None


def test_region_holding_copper_is_refused_whatever_the_rules_say():
    assert "HAS COPPER" in keepout_refusal(FULL, empty=False)
    assert "HAS COPPER" in keepout_refusal(POUR_ONLY, empty=False)


def test_area_that_lets_the_pour_in_is_refused():
    # the pour fills it, so there is no island to clear - rastering it
    # would cut ground copper
    assert "not a copper keepout" in keepout_refusal(NOT_A_KEEPOUT, empty=True)
