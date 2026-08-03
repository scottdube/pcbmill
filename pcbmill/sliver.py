"""Sliver prediction (FR7.9) - the board-1 solderability problem.

Geometric, from the Gerber, before anything is cut:

    blank copper sheet - the mill only clears a band of width
    isolation-width around every copper outline. Designed gap regions
    farther than isolation-width from any copper stay COPPER on the
    physical board: floating slivers.

Implementation: render the copper layer with gerbv (exact, handles
aperture macros), then a distance transform. A gap pixel whose distance
to copper exceeds the effective isolation width is predicted sliver.
Bias is conservative: the band is shrunk by one pixel so we never miss a
sliver by rounding.

Severity: sliver within sliver_fail_radius_mm of a DRILLED pad = FAIL
(solder-bridge trap between hand-soldered pads); elsewhere WARN.
Also computes the minimum isolation-width that zeroes the FAILs.
"""

import os
import re
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image
from scipy import ndimage

MM_PER_IN = 25.4
GERBV = shutil.which("gerbv") or "gerbv"

Image.MAX_IMAGE_PIXELS = None


# ------------------------------------------------------------ gerber bbox

def gerber_bbox_mm(path):
    """Covering bbox of a KiCad mm Gerber: coordinate extents padded by the
    largest aperture dimension. Only needs to COVER; exactness irrelevant."""
    xs, ys = [], []
    scale = None
    max_ap = 0.0
    with open(path, errors="replace") as fh:
        for line in fh:
            m = re.match(r"%FSLAX(\d)(\d)Y", line)
            if m:
                scale = 10 ** int(m.group(2))
            if line.startswith("%AD"):
                for tok in re.findall(r"[\d.]+", line.split(",", 1)[-1]):
                    try:
                        max_ap = max(max_ap, abs(float(tok)))
                    except ValueError:
                        pass
                continue
            if line.startswith("%"):
                continue
            for m in re.finditer(r"X(-?\d+)", line):
                xs.append(int(m.group(1)))
            for m in re.finditer(r"Y(-?\d+)", line):
                ys.append(int(m.group(1)))
    if not xs or not ys or not scale:
        raise ValueError("could not read coordinates from %s" % path)
    pad = max_ap + 1.0
    return (min(xs) / scale - pad, min(ys) / scale - pad,
            max(xs) / scale + pad, max(ys) / scale + pad)


# ------------------------------------------------------------ rasterise

def rasterise(gerber_path, bbox_mm, dpi=1000):
    """Render copper as a boolean array [row, col]; row 0 = TOP of the
    image = max Y. Returns (mask, meta) where meta maps pixels to mm."""
    x0, y0, x1, y1 = bbox_mm
    w_in = (x1 - x0) / MM_PER_IN
    h_in = (y1 - y0) / MM_PER_IN
    with tempfile.TemporaryDirectory(prefix="pcbmill-gerbv-") as td:
        out = os.path.join(td, "copper.png")
        cmd = [GERBV, "-x", "png", "-o", out,
               "--dpi=%d" % dpi, "--border=0",
               "--origin=%.6fx%.6f" % (x0 / MM_PER_IN, y0 / MM_PER_IN),
               "--window_inch=%.6fx%.6f" % (w_in, h_in),
               "-b", "#000000", "-f", "#FFFFFF",
               gerber_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("gerbv failed:\n%s" % proc.stderr[-1000:])
        img = Image.open(out).convert("L")
        arr = np.asarray(img)
    mask = arr > 127
    meta = {"x0_mm": x0, "y0_mm": y0, "x1_mm": x1, "y1_mm": y1,
            "dpi": dpi, "px_mm": MM_PER_IN / dpi,
            "rows": mask.shape[0], "cols": mask.shape[1]}
    return mask, meta


def _px_to_mm(meta, row, col):
    x = meta["x0_mm"] + (col + 0.5) * meta["px_mm"]
    y = meta["y1_mm"] - (row + 0.5) * meta["px_mm"]
    return x, y


# ------------------------------------------------------------ prediction

def _point_in_poly(x, y, poly):
    c = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xin = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x < xin:
                c = not c
    return c


def predict(gerber_path, isolation_width_in, drilled_pads_mm=None,
            fail_radius_mm=1.0, min_area_mm2=0.002, dpi=1000,
            kicad_to_g54=None, exclusion_polys_mm=None):
    """Returns the sliver report dict.

    drilled_pads_mm: [(x_mm, y_mm) or (x_mm, y_mm, pad_radius_mm), ...] in
        the Gerber/KiCad frame; radius defaults to 0.8 mm when omitted.
    kicad_to_g54: optional (MX, MY) so locations can be reported in G54 too
                  (G54x = MX - x_mm/25.4, G54y = MY - y_mm/25.4).
    exclusion_polys_mm: polygons (Gerber frame) of DECLARED pour-exclusion
        rule areas. Slivers inside are intentional outlined islands: severity
        PLANNED, excluded from the suggested-width computation (they are
        end-mill-clearing candidates, FR8).
    """
    bbox = gerber_bbox_mm(gerber_path)
    copper, meta = rasterise(gerber_path, bbox, dpi)
    px = meta["px_mm"]

    # distance (mm) from every non-copper pixel to the nearest copper pixel
    dist = ndimage.distance_transform_edt(~copper) * px

    iso_mm = isolation_width_in * MM_PER_IN
    # conservative: shrink the cleared band by one pixel
    eff = max(iso_mm - px, 0.0)
    sliver = (~copper) & (dist > eff)

    # the un-milled outer sheet is connected to the image border - not a
    # floating sliver (it is the rest of the blank)
    labels, nlab = ndimage.label(sliver)
    if nlab:
        border = np.unique(np.concatenate([
            labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
        border = border[border != 0]
        if border.size:
            sliver &= ~np.isin(labels, border)
        labels, nlab = ndimage.label(sliver)

    # distance to the nearest drilled-pad COPPER (not centre), for severity:
    # each pad is drawn as a disk of its actual radius, so "within 1 mm of a
    # pad" means 1 mm from the solderable copper edge
    pad_dist = None
    if drilled_pads_mm:
        pads = np.zeros(copper.shape, dtype=bool)
        yy_grid = None
        for pad in drilled_pads_mm:
            x_mm, y_mm = pad[0], pad[1]
            rad_mm = pad[2] if len(pad) > 2 and pad[2] else 0.8
            c = (x_mm - meta["x0_mm"]) / px
            r = (meta["y1_mm"] - y_mm) / px
            rp = max(1, int(round(rad_mm / px)))
            r0, r1 = int(r - rp), int(r + rp) + 1
            c0, c1 = int(c - rp), int(c + rp) + 1
            r0c, c0c = max(r0, 0), max(c0, 0)
            r1c = min(r1, pads.shape[0])
            c1c = min(c1, pads.shape[1])
            if r1c <= r0c or c1c <= c0c:
                continue
            ys = np.arange(r0c, r1c) - r
            xs = np.arange(c0c, c1c) - c
            disk = (ys[:, None] ** 2 + xs[None, :] ** 2) <= rp * rp
            pads[r0c:r1c, c0c:c1c] |= disk
        if pads.any():
            pad_dist = ndimage.distance_transform_edt(~pads) * px

    slivers = []
    if nlab:
        objects = ndimage.find_objects(labels)
        for i, sl in enumerate(objects, 1):
            comp = labels[sl] == i
            area_mm2 = comp.sum() * px * px
            if area_mm2 < min_area_mm2:
                continue
            # centroid + representative location
            rr, cc = np.nonzero(comp)
            r0, c0 = sl[0].start, sl[1].start
            cy = r0 + rr.mean()
            cx = c0 + cc.mean()
            x_mm, y_mm = _px_to_mm(meta, cy, cx)
            # width estimate: twice the deepest interior point
            inner = ndimage.distance_transform_edt(comp) * px
            width_mm = 2.0 * float(inner.max())
            # clearing need: how wide must isolation be to consume it all
            need_mm = float(dist[sl][comp].max()) + px
            d_pad = (float(pad_dist[sl][comp].min())
                     if pad_dist is not None else None)
            planned = any(_point_in_poly(x_mm, y_mm, poly)
                          for poly in (exclusion_polys_mm or []))
            if planned:
                severity = "PLANNED"
            elif d_pad is not None and d_pad <= fail_radius_mm:
                severity = "FAIL"
            else:
                severity = "WARN"
            # x_mm/y_mm are Gerber-frame; KiCad's Y axis is the negation
            rec = {
                "x_mm": round(float(x_mm), 3),
                "y_mm": round(float(-y_mm), 3),
                "area_mm2": round(float(area_mm2), 4),
                "min_width_mm": round(width_mm, 3),
                "needs_isolation_in": round(need_mm / MM_PER_IN, 4),
                "dist_to_drilled_pad_mm": (round(d_pad, 3)
                                           if d_pad is not None else None),
                "severity": severity,
            }
            if kicad_to_g54:
                mx, my = kicad_to_g54
                rec["x_g54"] = round(mx - x_mm / MM_PER_IN, 4)
                rec["y_g54"] = round(my - (-y_mm) / MM_PER_IN, 4)
            slivers.append(rec)

    order = {"FAIL": 0, "WARN": 1, "PLANNED": 2}
    slivers.sort(key=lambda s: (order[s["severity"]], -s["area_mm2"]))
    fails = [s for s in slivers if s["severity"] == "FAIL"]
    planned = [s for s in slivers if s["severity"] == "PLANNED"]
    suggestion = None
    if fails:
        suggestion = round(max(s["needs_isolation_in"] for s in fails) + 0.001, 4)

    return {
        "isolation_width_in": isolation_width_in,
        "effective_width_in": round(eff / MM_PER_IN, 4),
        "dpi": dpi,
        "sliver_count": len(slivers),
        "fail_count": len(fails),
        "planned_count": len(planned),
        "warn_count": len(slivers) - len(fails) - len(planned),
        "slivers": slivers[:200],
        "truncated": len(slivers) > 200,
        "suggested_isolation_width_in": suggestion,
        "note": ("suggested width zeroes the FAILs (near drilled pads); "
                 "WARN slivers elsewhere are cosmetic or end-mill-clearing "
                 "candidates; PLANNED slivers sit inside declared "
                 "pour-exclusion rule areas" if suggestion else None),
    }
