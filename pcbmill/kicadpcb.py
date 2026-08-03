"""Read-only .kicad_pcb parsing: pads + drills (for the annulus check),
rule areas (region browser, FR8), and the design intake lint (FR7.10).

Uses the same balanced-paren block scanning as the proven
kicad_regions.py / kicad_whatsinside.py scripts.
"""

import math
import re

MM_PER_IN = 25.4


def _block(txt, start):
    d = 0
    for j in range(start, len(txt)):
        if txt[j] == "(":
            d += 1
        elif txt[j] == ")":
            d -= 1
            if d == 0:
                return txt[start:j + 1]
    return txt[start:]


def _inside(pt, poly):
    x, y = pt
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


class Board:
    def __init__(self, path):
        self.path = path
        with open(path, errors="replace") as fh:
            self.text = fh.read()

    # ------------------------------------------------------------ footprints

    def pads(self):
        """All pads with absolute positions (KiCad mm). Includes drill size
        and shape. Rotation handled the same way as isocheck.py."""
        out = []
        for m in re.finditer(r"\(footprint\b", self.text):
            blk = _block(self.text, m.start())
            fat = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?", blk)
            ref = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', blk)
            if not fat:
                continue
            fx, fy = float(fat.group(1)), float(fat.group(2))
            rot = float(fat.group(3) or 0)
            a = math.radians(rot)
            refname = ref.group(1) if ref else "?"
            for pm in re.finditer(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)', blk):
                pb = _block(blk, pm.start())
                at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)", pb)
                sz = re.search(r"\(size\s+([\d.]+)\s+([\d.]+)\)", pb)
                dr = re.search(r"\(drill\s+(?:oval\s+)?([\d.]+)", pb)
                zc = re.search(r"\(zone_connect\s+(\d+)\)", pb)
                if not (at and sz):
                    continue
                px, py = float(at.group(1)), float(at.group(2))
                gx = fx + px * math.cos(a) + py * math.sin(a)
                gy = fy - px * math.sin(a) + py * math.cos(a)
                w, h = float(sz.group(1)), float(sz.group(2))
                if abs(rot) % 180 > 45:
                    w, h = h, w
                out.append({
                    "ref": refname,
                    "pad": pm.group(1),
                    "type": pm.group(2),          # thru_hole / smd / np_thru_hole
                    "shape": pm.group(3),          # circle / oval / rect / roundrect
                    "x_mm": gx, "y_mm": gy,
                    "size_mm": (w, h),
                    "drill_mm": float(dr.group(1)) if dr else None,
                    "zone_connect": int(zc.group(1)) if zc else None,
                })
        return out

    def drilled_pads(self):
        return [p for p in self.pads() if p["drill_mm"]]

    # ------------------------------------------------------------ rule areas

    def rule_areas(self):
        out = []
        n = 0
        for m in re.finditer(r"\(zone\b", self.text):
            blk = _block(self.text, m.start())
            if "(keepout" not in blk:
                continue
            n += 1
            layer = re.search(r'\(layers?\s+"?([^")\s]+)', blk)
            pts = [(float(a), float(b)) for a, b in
                   re.findall(r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)", blk)]
            ko = re.search(r"\(keepout(.*?)\n\s*\)", blk, re.S)
            restrictions = dict(re.findall(r"\((\w+)\s+(\w+)\)", ko.group(1))) if ko else {}
            area = 0.0
            for i in range(len(pts)):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % len(pts)]
                area += x0 * y1 - x1 * y0
            out.append({
                "index": n,
                "layer": layer.group(1) if layer else "?",
                "polygon_mm": pts,
                "restrictions": restrictions,
                "area_mm2": abs(area) / 2.0,
            })
        return out

    def region_contents(self, polygon_mm):
        """kicad_whatsinside logic: what copper sits inside a polygon."""
        fps = []
        for m in re.finditer(r"\(footprint\b", self.text):
            blk = _block(self.text, m.start())
            at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)", blk)
            ref = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', blk)
            if at and _inside((float(at.group(1)), float(at.group(2))), polygon_mm):
                fps.append(ref.group(1) if ref else "?")
        trk = 0
        for m in re.finditer(r"\(segment\b", self.text):
            blk = _block(self.text, m.start())
            if '"B.Cu"' not in blk:
                continue
            a = re.search(r"\(start\s+(-?[\d.]+)\s+(-?[\d.]+)", blk)
            b = re.search(r"\(end\s+(-?[\d.]+)\s+(-?[\d.]+)", blk)
            if a and b and (_inside((float(a.group(1)), float(a.group(2))), polygon_mm)
                            or _inside((float(b.group(1)), float(b.group(2))), polygon_mm)):
                trk += 1
        vias = 0
        for m in re.finditer(r"\(via\b", self.text):
            blk = _block(self.text, m.start())
            a = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)", blk)
            if a and _inside((float(a.group(1)), float(a.group(2))), polygon_mm):
                vias += 1
        return {"footprints": fps, "track_ends": trk, "vias": vias,
                "empty": not fps and not trk and not vias}

    # ------------------------------------------------------------ zones

    def copper_zones(self):
        """Non-keepout zones (pours) with clearance / pad-connect settings."""
        out = []
        for m in re.finditer(r"\(zone\b", self.text):
            blk = _block(self.text, m.start())
            if "(keepout" in blk:
                continue
            layer = re.search(r'\(layers?\s+"?([^")\s]+)', blk)
            net = re.search(r'\(net_name\s+"([^"]*)"', blk)
            clearance = re.search(r"\(clearance\s+([\d.]+)\)", blk)
            # (connect_pads (clearance x))          -> thermal relief (default)
            # (connect_pads yes (clearance x))      -> solid
            # (connect_pads thru_hole_only ...)     -> thermal for SMD
            cp = re.search(r"\(connect_pads(\s+\w+)?", blk)
            mode = (cp.group(1) or "").strip() if cp else ""
            spoke = re.search(r"\(thermal_bridge_width\s+([\d.]+)\)", blk)
            out.append({
                "layer": layer.group(1) if layer else "?",
                "net": net.group(1) if net else "?",
                "clearance_mm": float(clearance.group(1)) if clearance else None,
                "pad_connection": {"": "thermal", "yes": "solid",
                                   "thru_hole_only": "thru_hole_only",
                                   "no": "none"}.get(mode, mode),
                "thermal_bridge_width_mm": float(spoke.group(1)) if spoke else None,
            })
        return out

    # ------------------------------------------------------------ lint FR7.10

    def intake_lint(self, zone_clearance_warn_mm=0.6, th_pitch_warn_mm=2.54):
        """Report-only warnings whose fix belongs in KiCad, not the app."""
        warnings = []

        for z in self.copper_zones():
            where = "zone net=%s layer=%s" % (z["net"], z["layer"])
            if z["clearance_mm"] is not None and z["clearance_mm"] < zone_clearance_warn_mm:
                warnings.append({
                    "kind": "zone-clearance",
                    "msg": "%s: clearance %.2f mm is tight for hand soldering on "
                           "a milled board (board 1 was 0.4 mm and needed rework "
                           "space; %.1f mm is comfortable)"
                           % (where, z["clearance_mm"], zone_clearance_warn_mm),
                })
            if z["pad_connection"] == "solid":
                warnings.append({
                    "kind": "zone-solid-pads",
                    "msg": "%s: pads connect SOLID to the pour - no solder mask "
                           "plus pour heat-sinking makes these miserable to "
                           "hand-solder; use thermal relief" % where,
                })

        # thermal spoke count lives on pads in newer KiCad; a pad-level
        # zone_connect of 1 with < 2 spokes isn't directly readable, so warn
        # only on what is: explicit solid overrides on thru-hole pads.
        for p in self.pads():
            if p["type"] == "thru_hole" and p.get("zone_connect") == 2:
                warnings.append({
                    "kind": "pad-solid-override",
                    "msg": "%s pad %s: explicit solid zone connection on a "
                           "thru-hole pad - hard to hand-solder without mask"
                           % (p["ref"], p["pad"]),
                })

        warnings.extend(self._round_pad_row_lint(th_pitch_warn_mm))

        for ra in self.rule_areas():
            r = ra["restrictions"]
            if (r.get("copperpour") == "not_allowed"
                    and r.get("tracks") == "allowed"
                    and r.get("pads") == "allowed"):
                warnings.append({
                    "kind": "pour-exclusion",
                    "msg": "rule area %d (%s): pour-exclusion only - this mills "
                           "as an OUTLINED FLOATING ISLAND of copper; accept it, "
                           "widen isolation, or plan an end-mill clear (FR8)"
                           % (ra["index"], ra["layer"]),
                })
        return warnings

    def _round_pad_row_lint(self, pitch_mm):
        """Round pads on <= pitch through-hole rows -> suggest oval pads."""
        warnings = []
        by_ref = {}
        for p in self.pads():
            if p["type"] == "thru_hole":
                by_ref.setdefault(p["ref"], []).append(p)
        for ref, pads in by_ref.items():
            round_pads = [p for p in pads if p["shape"] == "circle"]
            if len(round_pads) < 3:
                continue
            # detect a row: >= 3 collinear pads at pitch <= threshold
            for axis in (0, 1):   # 0 = row along X, 1 = along Y
                key = "y_mm" if axis == 0 else "x_mm"
                pos = "x_mm" if axis == 0 else "y_mm"
                lanes = {}
                for p in round_pads:
                    lanes.setdefault(round(p[key], 2), []).append(p[pos])
                for lane, xs in lanes.items():
                    xs = sorted(xs)
                    if len(xs) < 3:
                        continue
                    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
                    if gaps and max(gaps) <= pitch_mm + 0.01:
                        warnings.append({
                            "kind": "round-pads-on-row",
                            "msg": "%s: %d round pads in a %.2f mm-pitch row - "
                                   "oval pads (long axis perpendicular to the "
                                   "row) open the inter-pad gap without losing "
                                   "annulus" % (ref, len(xs), min(gaps)),
                        })
                        break
                else:
                    continue
                break
        return warnings
