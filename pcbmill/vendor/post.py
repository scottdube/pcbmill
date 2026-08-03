#!/usr/bin/env python3
import re, sys
N = 600
p = sys.argv[1] if len(sys.argv) > 1 else 'out/geoaux_back.ngc'
s = open(p).read()
s = re.sub(r'^T\d+\s*$', 'T%d' % N, s, count=1, flags=re.M)
if not re.search(r'^G43\b', s, flags=re.M):
    s = re.sub(r'^(M6.*)$', r'\1\nG43 H%d (apply V-bit tool length)' % N,
               s, count=1, flags=re.M)
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
    if re.match(r'^(T\d|M6|G43|G30|G64|G54|F[\d.])', l):
        print('   ', l.rstrip())
