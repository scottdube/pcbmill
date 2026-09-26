#!/usr/bin/env python3
"""Post-process pcb2gcode isolation/outline output for the 1100MX.

Renumbers the tool to an unused row so PathPilot prompts for a manual
change, and inserts G37 + G43 after the M0 pause: the program drives
itself to the ETS, measures the tool it is about to cut with, and applies
that result.

This file previously emitted a bare G43 with no G37, so Z rode on
whatever the tool table happened to hold. It survived only because the
isolation pass usually followed something that had just measured the same
tool. The OUTLINE is the dangerous case: the profile cutter is a
different physical tool from every other op in the job - V-bit for
isolation, drills, then a fishtail for the profile - so an unmeasured
G43 there applies the previous tool's length and drives into the fixture
or cuts air.

REQUIRES: the G37 ETS position must be set, and the spindle nose must
have been referenced to the ETS since the last Z-axis reference, or G37
fails.
"""
import re, sys

N = 600
args = [a for a in sys.argv[1:] if not a.startswith('--')]
# --electrical: the operator touches this tool off on the copper with a meter
# (the isolation cutter). Apply the H register after the M0 and NEVER G37 it:
# the ETS reads +4.1 thou off the work frame, which on a 2 thou cut is the
# whole cut. Scott, 2026-09-26: G37 "on all but 600".
ELECTRICAL = '--electrical' in sys.argv[1:]
p = args[0] if args else 'out/geoaux_back.ngc'
s = open(p).read()
s = re.sub(r'^T\d+\s*$', 'T%d' % N, s, count=1, flags=re.M)

if not re.search(r'^G43\b', s, flags=re.M):
    # After the M0 operator pause, NOT after M6: the length must be measured
    # once the new tool is actually in the spindle. Same order as
    # postdrill.py. The comment stays tool-agnostic - this same code path
    # post-processes the V-bit isolation pass and the fishtail profile pass,
    # and a hardcoded "V-bit" label misnames the tool on one of them at
    # cycle start.
    out, made = [], 0
    for l in s.split('\n'):
        out.append(l)
        if made == 0 and re.match(r'^M0\b', l):
            if ELECTRICAL:
                out.append('G43 H%d (apply the ELECTRICALLY touched-off length'
                           ' - no G37, the ETS reads high vs the work frame)' % N)
            else:
                out.append('G37 H%d (measure this tool on the ETS)' % N)
                out.append('G43 H%d (apply the length G37 just stored)' % N)
            made += 1
    if made != 1:
        sys.exit('post: expected 1 G37/G43 insert, made %d - check the file'
                 % made)
    s = '\n'.join(out)

    # After G37 the spindle is parked at the ETS, where the program's own
    # "retract" to Z0.1 can be a DESCENT. Clear the ETS and get over the work
    # before any Z move. Same guard as postdrill.py. (Not needed without a
    # G37: the spindle never left the tool-change position.)
    lines, out, i = s.split('\n'), [], 0
    while i < len(lines):
        l = lines[i]
        out.append(l)
        if not ELECTRICAL and re.match(r'^G43 H\d+', l):
            nxt = None
            for j in range(i + 1, min(i + 40, len(lines))):
                q = re.match(r'^G0*0\s+(X-?[\d.]+\s+Y-?[\d.]+)', lines[j])
                if q:
                    nxt = q.group(1)
                    break
            if nxt:
                out.append('G30 (clear of the ETS before descending)')
                out.append('G00 %s (over the work first)' % nxt)
        i += 1
    s = '\n'.join(out)

if not re.search(r'^G5[4-9]\b', s, flags=re.M):
    s = re.sub(r'^(G90\b.*)$', r'\1\nG54 (work offset)', s, count=1, flags=re.M)
s = re.sub(r'^G0*1\s+(F[\d.]+)', r'\1', s, count=1, flags=re.M)
s = re.sub(r'^G00 Z1\.0+.*$', 'G30 (retract to tool change position)',
           s, flags=re.M)
s = re.sub(r'^G64 P0\.00040.*$', 'G64 P0.00020 (tightened for 2 thou cut)',
           s, flags=re.M)
open(p, 'w').write(s)
print('post-processed', p)
for l in open(p):
    if re.match(r'^(T\d|M6|M0|G37|G43|G30|G64|G54|F[\d.])', l):
        print('   ', l.rstrip())
