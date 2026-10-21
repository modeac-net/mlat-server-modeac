#!/usr/bin/env python3
"""
End-to-end check of the feeder check against a real readsb hub.

Starts readsb (path in $READSB, built from wiedehopf/readsb) with the hub's feeder-tracking options,
connects two fake feeders that send UUIDs and ADS-B positions - one hearing Ohio, one hearing
southern England - and runs mlat.feedercheck.evaluate on what readsb reports.

    READSB=/path/to/readsb python3 tools/feedercheck_test.py
"""

import json
import math
import os
import random
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

from adsb_encode import df17_airborne_position      # noqa: E402
from mlat import feedercheck, geofence               # noqa: E402

READSB = os.environ.get('READSB', 'readsb')
PORT = 41004
HUB_FLAGS = ['--net-receiver-id', '--write-receiver-id-json', '--devel=enableClientsJson']


def esc(b):
    return b.replace(b'\x1a', b'\x1a\x1a')


def beast_long(ticks, msg):
    return b'\x1a3' + esc(ticks.to_bytes(6, 'big') + b'\x80' + msg)


class Feeder:
    def __init__(self, uuid, lat0, lon0, seed):
        self.s = socket.create_connection(('127.0.0.1', PORT))
        self.s.sendall(b'\x1a\xe4' + uuid.encode('ascii'))
        rnd = random.Random(seed)
        self.ac = []
        for i in range(25):
            brg = rnd.uniform(0, 2 * math.pi)
            dist = rnd.uniform(20, 250) / 111.0
            self.ac.append({'icao': (0xA00000 if lat0 > 45 else 0xA10000) + seed * 0x1000 + i,
                            'lat': lat0 + dist * math.cos(brg),
                            'lon': lon0 + dist * math.sin(brg) / math.cos(math.radians(lat0)),
                            'hdg': rnd.uniform(0, 2 * math.pi), 'alt': rnd.randrange(10000, 38000, 1000)})
        self.odd = 0

    def tick(self, t, dt):
        out = b''
        for a in self.ac:
            v = 230.0 / 111000.0 * dt            # ~450 kt
            a['lat'] += v * math.cos(a['hdg'])
            a['lon'] += v * math.sin(a['hdg']) / math.cos(math.radians(a['lat']))
            msg = df17_airborne_position(a['icao'], a['lat'], a['lon'], a['alt'], self.odd)
            out += beast_long(int(t * 12e6) & 0xFFFFFFFFFFFF, msg)
        self.odd ^= 1
        self.s.sendall(out)


def main():
    work = tempfile.mkdtemp()
    hub = subprocess.Popen([READSB, '--net-only', '--net-bi-port', str(PORT), '--write-json', work,
                            '--write-json-every', '1', '--quiet'] + HUB_FLAGS,
                           stdout=subprocess.DEVNULL, stderr=open(os.path.join(work, 'readsb.log'), 'w'))
    try:
        time.sleep(1.5)
        ohio = Feeder('11111111-2222-3333-4444-555555555555', 40.40, -82.90, 1)
        uk = Feeder('99999999-8888-7777-6666-555555555555', 51.50, -0.12, 2)
        t0 = time.time()
        while time.time() - t0 < 40:
            t = time.time()
            ohio.tick(t, 0.5)
            uk.tick(t, 0.5)
            time.sleep(0.5)
        time.sleep(12)                            # receivers.json / clients.json written every 10 s
        receivers = json.load(open(os.path.join(work, 'receivers.json')))
        clients = json.load(open(os.path.join(work, 'clients.json')))
        print(json.dumps(receivers, indent=1)[:900])
        print(json.dumps(clients, indent=1)[:900])
        # the Ohio feeder also runs mlat-client, honestly; a second mlat site on the same IP lies
        mlat = {'site-a': {'user': 'site-a', 'lat': 40.40, 'lon': -82.90, 'source_ip': '127.0.0.1'}}
        outline = geofence.Outline(feedercheck.area_path('us'))
        feedercheck.MIN_POSITIONS = 100
        verdicts = {f['uuid']: f for f in feedercheck.evaluate(receivers, clients, mlat, outline)}
        for f in verdicts.values():
            print(f)
        ok = (verdicts['11111111-2222-3333-4444-555555555555']['verdict'] == 'ok'
              and verdicts['99999999-8888-7777-6666-555555555555']['verdict'] == 'outside')
        # stated location far from what is heard
        mlat_liar = {'liar': {'user': 'liar', 'lat': 33.75, 'lon': -84.39, 'source_ip': '127.0.0.1'}}
        v2 = {f['uuid']: f for f in feedercheck.evaluate(receivers, clients, mlat_liar, outline)}
        ok = ok and v2['11111111-2222-3333-4444-555555555555']['verdict'] == 'mismatch'
        print('PASS' if ok else 'FAIL')
        return 0 if ok else 1
    finally:
        hub.terminate()


if __name__ == '__main__':
    sys.exit(main())
