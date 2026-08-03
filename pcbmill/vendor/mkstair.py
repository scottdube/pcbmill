# keepout box (G54):  X 3.22 - 3.80   Y 0.540 - 0.761
# map starts at Y 0.540, so keep clear of it
X0, Y0 = 3.245, 0.570
W, H, GAP = 0.090, 0.170, 0.020
DEPTHS = [-0.0010, -0.0015, -0.0020, -0.0025, -0.0030]
L = ['(depth staircase - 5 closed rectangles, one per depth)',
     'G94','G20','G90','G54','G00 S10000','G64 P0.00020',
     'G30','T600','M5','G43 H600','M0','M3','G04 P1.0']
for i, d in enumerate(DEPTHS):
    x0 = X0 + i*(W+GAP); x1 = x0 + W; y0 = Y0; y1 = Y0 + H
    L += ['(rect %d  commanded depth %.4f  X%.4f-%.4f)' % (i+1, d, x0, x1),
          'G00 Z0.10000', 'G00 X%.4f Y%.4f' % (x0, y0),
          'G01 Z%.5f F5' % d,
          'G01 X%.4f Y%.4f F9' % (x1, y0),
          'G01 X%.4f Y%.4f' % (x1, y1),
          'G01 X%.4f Y%.4f' % (x0, y1),
          'G01 X%.4f Y%.4f' % (x0, y0),
          'G00 Z0.10000']
L += ['G30','M5','M2','']
open('stair.ngc','w').write('\n'.join(L))
for i in range(5):
    print('rect %d  X %.4f-%.4f  depth %+.4f'
          % (i+1, X0+i*(W+GAP), X0+i*(W+GAP)+W, DEPTHS[i]))
print('Y %.4f to %.4f' % (Y0, Y0+H))
