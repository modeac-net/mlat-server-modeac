# -*- mode: python; indent-tabs-mode: nil -*-

# modeac.net: checks every feeder against what it actually hears.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

"""
The hub's readsb already keeps, for every feeder, the box its aircraft positions fall in (only
reliable airborne ADS-B positions count, so the box can't be faked by typing in coordinates) and
times out feeders that send bad positions. This job reads that every few minutes and applies the
network's own rules:

  outside   the feeder's reception box doesn't touch the accepted area at all
            -> its IP and UUID go on the automatic ban list (unless ACTION=report)
  mismatch  the feeder also runs mlat-client from the same IP, but hears a different place from
            the location it states -> reported only (a shared IP, e.g. carrier-grade NAT, could
            belong to more than one person, so this is never banned automatically)
  ok / new  fine / not enough positions yet to judge

Inputs (all already produced by the stack, nothing extra to run):
  HUB_URL/data/receivers.json   per-feeder reception boxes   (readsb --write-receiver-id-json)
  HUB_URL/data/clients.json     per-connection UUID and IP   (readsb --devel=enableClientsJson)
  MLAT_CLIENTS                  mlat-server's clients.json   (stated locations and IPs)

Outputs, in OUT_DIR:
  autoban.txt   IPs and UUIDs, read by mlat-server's --banlist (and later by the relay firewall)
  status.json   every feeder and its verdict
Auto-bans expire AUTOBAN_DAYS after the feeder was last seen outside the area.

Cost: one pass over a few hundred feeders every few minutes - a few milliseconds.
"""

import datetime
import json
import logging
import math
import os
import re
import time
import urllib.request

from mlat import geofence

log = logging.getLogger('feedercheck')

HUB_URL = os.environ.get('HUB_URL', 'http://hub').rstrip('/')
MLAT_CLIENTS = os.environ.get('MLAT_CLIENTS', '/run/mlat/clients.json')
OUT_DIR = os.environ.get('OUT_DIR', '/run/feedercheck')
AREA = os.environ.get('MODEAC_RECEIVER_AREA', 'us')
INTERVAL_S = float(os.environ.get('INTERVAL_S', '300'))
MIN_POSITIONS = int(os.environ.get('MIN_POSITIONS', '300'))
MISMATCH_KM = float(os.environ.get('MISMATCH_KM', '150'))
AUTOBAN_DAYS = float(os.environ.get('AUTOBAN_DAYS', '7'))
ACTION = os.environ.get('ACTION', 'ban').lower()          # ban | report


def area_path(area):
    if area.lower() in ('us', 'ohio'):
        return os.path.join(os.path.dirname(geofence.__file__), 'data', area.lower() + '_outline.json')
    return area


_IP_RE = re.compile(r'^(?:TCP[46] )?(\S+)')


def ip_of(proxy_string):
    """readsb's connection description: '1.2.3.4 port 5678', or after a PROXY header
    'TCP4 <client ip> <proxy ip> <client port> <proxy port>'."""
    s = (proxy_string or '').strip()
    m = _IP_RE.match(s)
    if not m:
        return None
    ip = m.group(1)
    return ip[7:] if ip.startswith('::ffff:') else ip


def box_distance_km(lat, lon, box):
    """Rough distance from a point to a lat/lon box (0 if inside)."""
    lat_min, lat_max, lon_min, lon_max = box
    clat = min(max(lat, lat_min), lat_max)
    dlon = min(0.0 if lon_min <= l <= lon_max else min(abs(l - lon_min), abs(l - lon_max))
               for l in (lon, lon - 360.0, lon + 360.0))  # readsb boxes can extend past +-180
    dy = (lat - clat) * 111.2
    dx = dlon * 111.2 * math.cos(math.radians((lat + clat) / 2.0))
    return math.hypot(dx, dy)


def evaluate(receivers_doc, clients_doc, mlat_doc, outline, now=None):
    """Pure function: the verdict for every feeder connection. Returns a list of dicts."""
    now = time.time() if now is None else now
    boxes = {}
    for row in receivers_doc.get('receivers', []):
        try:
            rid, _rate, timeouts_h, la0, la1, lo0, lo1, bad = row[:8]
        except (TypeError, ValueError):
            continue
        boxes[str(rid).lower()] = ((float(la0), float(la1), float(lo0), float(lo1)), bool(bad), float(timeouts_h))

    mlat_by_ip = {}
    for c in (mlat_doc or {}).values():
        ip = c.get('source_ip')
        if ip and c.get('lat') is not None:
            ip = ip[7:] if ip.startswith('::ffff:') else ip
            mlat_by_ip.setdefault(ip, []).append(c)

    out = []
    for row in clients_doc.get('clients', []):
        try:
            uuid, conn, _kbit, conn_s, _mps, _pps, _red, _rtt, positions = row[:9]
        except (TypeError, ValueError):
            continue
        uuid = str(uuid).strip().lower()
        ip = ip_of(conn)
        f = {'uuid': uuid, 'ip': ip, 'positions': int(positions), 'connected_s': round(float(conn_s))}
        b = boxes.get(uuid[:18])
        if b is None or f['positions'] < MIN_POSITIONS:
            f['verdict'] = 'new'
            out.append(f)
            continue
        box, bad_extent, timeouts_h = b
        f.update(box=[round(v, 2) for v in box], bad_extent=bad_extent, timeouts_per_hour=round(timeouts_h, 2))
        if bad_extent:
            # readsb saw positions much too far apart for one receiver: its box isn't trustworthy
            f['verdict'] = 'unclear'
        elif not outline.touches_box(*box):
            f['verdict'] = 'outside'
        else:
            f['verdict'] = 'ok'
            sites = mlat_by_ip.get(ip, [])
            if sites:
                d = min(box_distance_km(c['lat'], c['lon'], box) for c in sites)
                f['mlat_sites'] = [c.get('user') for c in sites]
                f['stated_km_from_box'] = round(d)
                if d > MISMATCH_KM:
                    f['verdict'] = 'mismatch'
        out.append(f)
    return out


def update_autoban(state, feeders, now):
    """state: {key: {'reason':..., 'first':..., 'last':...}} keyed by IP or UUID."""
    for f in feeders:
        if f['verdict'] != 'outside':
            continue
        for key in (f['ip'], f['uuid']):
            if not key:
                continue
            e = state.setdefault(key, {'first': now, 'reason': 'reception box {0} outside the area'.format(f['box'])})
            e['last'] = now
    expire = now - AUTOBAN_DAYS * 86400
    for key in [k for k, e in state.items() if e['last'] < expire]:
        del state[key]
    return state


def _fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'modeac-feedercheck'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _write_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def _stamp(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def run_once(outline, state):
    now = time.time()
    receivers = _fetch(HUB_URL + '/data/receivers.json')
    clients = _fetch(HUB_URL + '/data/clients.json')
    try:
        with open(MLAT_CLIENTS) as f:
            mlat = json.load(f)
    except (OSError, ValueError):
        mlat = {}
    feeders = evaluate(receivers, clients, mlat, outline, now)

    counts = {}
    for f in feeders:
        counts[f['verdict']] = counts.get(f['verdict'], 0) + 1
        if f['verdict'] in ('outside', 'mismatch', 'unclear'):
            log.warning('feeder %s (%s): %s %s', f['uuid'], f['ip'], f['verdict'],
                        f.get('box') if f['verdict'] != 'mismatch' else
                        '(stated location %s km from what it hears)' % f.get('stated_km_from_box'))

    if ACTION == 'ban':
        update_autoban(state, feeders, now)
    _write_atomic(os.path.join(OUT_DIR, 'autoban.json'), json.dumps(state, indent=1))
    lines = ['# written by feedercheck - do not edit; it is rewritten every few minutes.',
             '# Entries expire {0:g} days after last seen. Hand-kept bans go in banlist.txt.'.format(AUTOBAN_DAYS)]
    for key, e in sorted(state.items()):
        lines.append('{0}  # {1}; first {2}, last {3}'.format(key, e['reason'], _stamp(e['first']), _stamp(e['last'])))
    _write_atomic(os.path.join(OUT_DIR, 'autoban.txt'), '\n'.join(lines) + '\n')
    _write_atomic(os.path.join(OUT_DIR, 'status.json'),
                  json.dumps({'now': round(now), 'area': AREA, 'action': ACTION, 'counts': counts,
                              'feeders': feeders}, indent=1))
    return counts


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    os.makedirs(OUT_DIR, exist_ok=True)
    outline = geofence.Outline(area_path(AREA))
    try:
        with open(os.path.join(OUT_DIR, 'autoban.json')) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    log.info('checking feeders every %.0f s against %s (action: %s)', INTERVAL_S, AREA, ACTION)
    failing = False
    while True:
        try:
            counts = run_once(outline, state)
            if failing:
                log.info('hub reachable again')
            failing = False
            log.info('feeders: %s', ', '.join('%d %s' % (n, v) for v, n in sorted(counts.items())) or 'none')
        except Exception as e:                       # noqa: BLE001 - keep running; the hub may be restarting
            if not failing:
                log.warning('cannot check feeders: %s', e)
            failing = True
        time.sleep(INTERVAL_S)


if __name__ == '__main__':
    main()
