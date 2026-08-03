#!/usr/bin/env python3
"""Post-process pcb2gcode drill output for the 1100MX.

Renumbers tools to unused rows so PathPilot prompts for a manual change,
and inserts G37 + G43 after the M0 pause: the program drives itself to
the ETS, measures the new tool, and applies the result. No operator
measurement mid-program. One tool holder, three drills.

REQUIRES: the G37 ETS position must be set, and the spindle nose must
have been referenced to the ETS since the last Z-axis reference, or G37
fails.
"""
import re, sys
BASE = 605
p = sys.argv[1] if len(sys.argv) > 1 else 'out/geoaux_drill.ngc'
src = open(p).read()
add_g43 = not re.search(r'^G43\b', src, flags=re.M)
out, seen, pending, made = [], {}, None, 0
for l in src.split('\n'):
    m = re.match(r'^T(\d+)\s*$', l)
    if m:
        old = int(m.group(1))
        if old not in seen:
            seen[old] = BASE + len(seen)
        pending = seen[old]
        out.append('T%d' % pending)
        continue
    out.append(l)
    if add_g43 and pending is not None and re.match(r'^M0\b', l):
        out.append('G37 H%d (measure this tool on the ETS)' % pending)
        out.append('G43 H%d (apply the length G37 just stored)' % pending)
        pending = None
        made += 1
if add_g43 and made != len(seen):
    sys.exit('postdrill: expected %d G43 inserts, made %d - check the file'
             % (len(seen), made))
# After each G37/G43, retract to G30 and traverse to the first hole's XY
# BEFORE the program's Z descent - otherwise that descent happens while the
# spindle is still parked over the ETS, which on a low-profile vise can be
# above the work Z. Only then is it safe to come down.
fixed, i = [], 0
while i < len(out):
    l = out[i]
    fixed.append(l)
    m = re.match(r'^G43 H(\d+)', l)
    if m:
        nxt = None
        for j in range(i + 1, min(i + 40, len(out))):
            q = re.match(r'^G0*0\s+(X-?[\d.]+\s+Y-?[\d.]+)', out[j])
            if q:
                nxt = q.group(1)
                break
        if nxt:
            fixed.append('G30 (clear of the ETS before descending)')
            fixed.append('G00 %s (over the work first)' % nxt)
    i += 1
out = fixed
s = '\n'.join(out)
if not re.search(r'^G5[4-9]\b', s, flags=re.M):
    s = re.sub(r'^(G90\b.*)$', r'\1\nG54 (work offset)', s, count=1, flags=re.M)
s = re.sub(r'^G0*1\s+(F[\d.]+)', r'\1', s, count=1, flags=re.M)
s = re.sub(r'^G00 Z1\.0+.*$', 'G30 (retract to tool change position)',
           s, flags=re.M)
open(p, 'w').write(s)
print('post-processed', p)
for o, n in sorted(seen.items()):
    print('   T%-3d -> T%d' % (o, n))
for l in open(p):
    if re.match(r'^(T\d|M6|M0|G37|G43|G30|G54|F[\d.])', l):
        print('   ', l.rstrip())
