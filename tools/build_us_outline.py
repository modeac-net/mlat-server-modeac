#!/usr/bin/env python3
"""
Build mlat/data/us_outline.json: the area modeac.net accepts receivers in.

The 50 states (Natural Earth 1:50m "United States of America", which leaves out Puerto Rico and
the other territories), widened by BUFFER_KM so simplification and coastline detail never reject a
real receiver on a coast, an island or a border, then simplified to keep the file small.

Run once, at development time (needs shapely + pyproj; the server itself needs neither):

    curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson
    python3 tools/build_us_outline.py ne_50m_admin_0_countries.geojson mlat/data/us_outline.json

A smaller area made of whole states (e.g. for an Ohio-only test phase), from the states file:

    curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson
    python3 tools/build_us_outline.py ne_50m_admin_1_states_provinces.geojson mlat/data/ohio_outline.json --states Ohio

Natural Earth is public domain.
"""

import json
import warnings

warnings.filterwarnings('ignore', category=DeprecationWarning)

from pyproj import Transformer
from shapely.geometry import shape, Polygon, MultiPolygon
from shapely.ops import transform, unary_union

BUFFER_KM = 25.0
SIMPLIFY_KM = 1.0

# region -> (projection for metric buffering, longitude convention of the stored outline)
# Alaska's Aleutians cross 180 degrees, so its outline is stored with longitudes 0..360.
REGIONS = {
    'lower48': ('EPSG:5070', 180),     # CONUS Albers
    'alaska': ('EPSG:3338', 360),      # Alaska Albers
    'hawaii': ('+proj=aeqd +lat_0=20.5 +lon_0=-157 +units=m', 180),
}


def region_of(poly):
    c = poly.representative_point()
    if c.x > 0 or (c.x < -129 and c.y > 50):
        return 'alaska'
    if c.y < 30 and c.x < -150:
        return 'hawaii'
    return 'lower48'


def build_states(doc, names):
    want = {n.strip().lower() for n in names.split(',') if n.strip()}
    feats = [f for f in doc['features']
             if f['properties'].get('adm0_a3') == 'USA' and f['properties']['name'].lower() in want]
    missing = want - {f['properties']['name'].lower() for f in feats}
    if missing:
        raise SystemExit('unknown state(s): ' + ', '.join(sorted(missing)))
    merged_ll = unary_union([shape(f['geometry']) for f in feats])
    c = merged_ll.centroid
    proj = '+proj=aeqd +lat_0={0:.3f} +lon_0={1:.3f} +units=m'.format(c.y, c.x)
    return {', '.join(sorted(f['properties']['name'] for f in feats)): (proj, 180, merged_ll)}


def main(src, dst, states=None):
    doc = json.load(open(src))
    if states:
        regions = build_states(doc, states)
        write(regions, dst, 'Natural Earth 1:50m admin-1 states: ' + states)
        return

    usa = next(f for f in doc['features'] if f['properties']['ADM0_A3'] == 'USA')
    geom = shape(usa['geometry'])
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]

    by_region = {}
    for p in parts:
        by_region.setdefault(region_of(p), []).append(p)
    regions = {name: (proj, lon_mode, unary_union(by_region[name]))
               for name, (proj, lon_mode) in REGIONS.items()}
    write(regions, dst, 'Natural Earth 1:50m admin-0 (USA, 50 states; no territories)')


def write(regions, dst, source):
    out = {'source': source, 'buffer_km': BUFFER_KM, 'simplify_km': SIMPLIFY_KM, 'regions': []}

    for name, (proj, lon_mode, geom_ll) in regions.items():
        fwd = Transformer.from_crs('EPSG:4326', proj, always_xy=True).transform
        back = Transformer.from_crs(proj, 'EPSG:4326', always_xy=True).transform
        merged = transform(fwd, geom_ll)
        grown = merged.buffer(BUFFER_KM * 1000.0, resolution=4).simplify(SIMPLIFY_KM * 1000.0)
        ll = transform(back, grown)
        polys = list(ll.geoms) if isinstance(ll, MultiPolygon) else [ll]

        rings = []
        for poly in polys:
            for ring in [poly.exterior] + list(poly.interiors):
                pts = []
                for x, y in ring.coords:
                    if lon_mode == 360 and x < 0:
                        x += 360.0
                    pts.append([round(x, 4), round(y, 4)])
                rings.append(pts)

        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        out['regions'].append({
            'name': name,
            'lon_mode': lon_mode,
            'bbox': [min(xs), min(ys), max(xs), max(ys)],
            'rings': rings,
        })
        print('{0}: {1} rings, {2} points, bbox {3}'.format(
            name, len(rings), sum(len(r) for r in rings), out['regions'][-1]['bbox']))

    with open(dst, 'w') as f:
        json.dump(out, f, separators=(',', ':'))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('source')
    ap.add_argument('output')
    ap.add_argument('--states', help='comma-separated state names (use the admin-1 states file)')
    a = ap.parse_args()
    main(a.source, a.output, a.states)
