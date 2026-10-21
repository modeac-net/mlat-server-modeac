# -*- mode: python; indent-tabs-mode: nil -*-

# modeac.net: which receiver locations the network accepts.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

"""
Point-in-outline test against mlat/data/us_outline.json (the 50 states widened by 25 km; see
tools/build_us_outline.py). Run once per receiver connection, so it costs nothing in steady state:
a bounding-box test throws out almost everything outside, and the even-odd ray test over ~2000
points takes well under a millisecond.

Also answers whether a rectangle (a feeder's reception box) touches the outline, for the
feeder check.
"""

import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'us_outline.json')


def _point_in_rings(x, y, rings):
    """Even-odd rule over every ring of a region (outer boundaries and holes alike)."""
    inside = False
    for ring in rings:
        n = len(ring)
        x0, y0 = ring[n - 1]
        for i in range(n):
            x1, y1 = ring[i]
            if (y1 > y) != (y0 > y):
                if x < (x0 - x1) * (y - y1) / (y0 - y1) + x1:
                    inside = not inside
            x0, y0 = x1, y1
    return inside


def _segments_cross(ax, ay, bx, by, cx, cy, dx, dy):
    def orient(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        return (v > 0) - (v < 0)
    o1 = orient(ax, ay, bx, by, cx, cy)
    o2 = orient(ax, ay, bx, by, dx, dy)
    o3 = orient(cx, cy, dx, dy, ax, ay)
    o4 = orient(cx, cy, dx, dy, bx, by)
    return o1 != o2 and o3 != o4


class Region:
    def __init__(self, d):
        self.name = d['name']
        self.lon_mode = int(d.get('lon_mode', 180))
        self.bbox = tuple(d['bbox'])
        self.rings = [[(float(p[0]), float(p[1])) for p in r] for r in d['rings']]

    def lon(self, lon):
        lon = ((lon + 180.0) % 360.0) - 180.0            # -180 .. 180
        if self.lon_mode == 360 and lon < 0:
            lon += 360.0
        return lon

    def contains(self, lat, lon):
        x = self.lon(lon)
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= lat <= y1):
            return False
        return _point_in_rings(x, lat, self.rings)

    def touches_box(self, lat_min, lat_max, lon_min, lon_max):
        a, b = self.lon(lon_min), self.lon(lon_max)
        if b < a:                                         # box straddles this region's longitude seam
            return False
        x0, y0, x1, y1 = self.bbox
        if b < x0 or a > x1 or lat_max < y0 or lat_min > y1:
            return False
        # a box corner inside the outline, an outline point inside the box, or crossing edges
        for cx, cy in ((a, lat_min), (a, lat_max), (b, lat_min), (b, lat_max)):
            if _point_in_rings(cx, cy, self.rings):
                return True
        edges = (((a, lat_min), (b, lat_min)), ((b, lat_min), (b, lat_max)),
                 ((b, lat_max), (a, lat_max)), ((a, lat_max), (a, lat_min)))
        for ring in self.rings:
            px, py = ring[-1]
            for qx, qy in ring:
                if a <= qx <= b and lat_min <= qy <= lat_max:
                    return True
                for (ex0, ey0), (ex1, ey1) in edges:
                    if _segments_cross(px, py, qx, qy, ex0, ey0, ex1, ey1):
                        return True
                px, py = qx, qy
        return False


class Outline:
    def __init__(self, path=DEFAULT_PATH):
        with open(path) as f:
            doc = json.load(f)
        self.path = path
        self.regions = [Region(r) for r in doc['regions']]

    def contains(self, lat, lon):
        """True if (lat, lon) in degrees is inside the accepted area."""
        return any(r.contains(lat, lon) for r in self.regions)

    def touches_box(self, lat_min, lat_max, lon_min, lon_max):
        """True if the lat/lon rectangle overlaps the accepted area at all."""
        return any(r.touches_box(lat_min, lat_max, lon_min, lon_max) for r in self.regions)
