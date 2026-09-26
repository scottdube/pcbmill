"""Machine-specific constants (§6 of the requirements).

Everything here is a default; jobs may override any of it in their
manifest under params["machine"].
"""

MACHINE_DEFAULTS = {
    # G54 convention (Scott, 2026-09-26): Z0 on the COPPER by electrical
    # touch; the probe map carries the copper's deviation from it and the
    # board bottom is blank.thickness_in below. applymap runs in absolute
    # mode. A used MDF backer carries wax and cannot give a true zero.
    "applymap_mode": "absolute",

    # Spindle
    "spindle_rpm_max": 10000,

    # Probing
    "probe_tool": 99,          # probe is T99 / H99
    "probe_h": 99,

    # Tool numbering convention: rows >= 600 are unused in the ATC table so
    # M6 prompts manually and never fires the ATC.
    "manual_tool_min": 600,

    # Known open issue: ETS-measured lengths read +4.1 thou high vs
    # work-frame electrical touch-off. Depth-critical tools are touched
    # off electrically until resolved (see equipment/tormach-ets-frame-offset.md).
    "ets_frame_offset_in": 0.0041,

    # Drill breakthrough: hole bottom must land at least this far below
    # the BOARD BOTTOM (local copper - blank thickness) on every hole ...
    "drill_breakthrough_min_in": 0.003,
    # ... and never further than this past it (backer assumed to start
    # at the board bottom; a wax film only adds margin).
    "backer_damage_limit_in": -0.030,

    # Probe map QA thresholds (FR4.2). Roughness is the void/bond-failure
    # metric; span is not.
    "roughness_rms_limit_in": 0.0005,
    "roughness_point_limit_in": 0.0015,

    # Feeds sanity (FR3.3): chipload window in inches/tooth.
    "chipload_min": 0.0002,
    "chipload_max": 0.001,

    # Drill consolidation (FR3.4)
    "drill_undersize_limit_in": 0.002,   # flag bits smaller than nominal by more
    "annulus_min_mm": 0.25,              # (pad - drill)/2, no plating

    # Sliver prediction (FR7.9)
    "sliver_fail_radius_mm": 1.0,        # sliver within this of a drilled pad = FAIL
    "sliver_min_area_mm2": 0.002,        # ignore raster dust below this

    # Run sheet runtime estimate: path length / feed * corner factor
    "runtime_corner_factor": 1.4,

    # Design intake lint (FR7.10)
    "zone_clearance_warn_mm": 0.6,
    "th_row_pitch_warn_mm": 2.54,

    # Where `pcbmill deploy` copies runnable G-code. This is the SMB share
    # PathPilot browses. Note ~/gcode also exists on the Mac, is empty, and
    # has been untouched since 2024 - it is a decoy, not this.
    "gcode_dir": "/Volumes/Tormach/gcode",
}

# PathPilot quirks (informational; enforced by the linter where checkable):
#  - LOGOPEN takes a bare filename only; absolute paths silently fail
#  - #5061-#5070 are read-only
#  - no nested parens in comments
#  - warns on missing G5x / tool-before-feed; generated files must load clean
