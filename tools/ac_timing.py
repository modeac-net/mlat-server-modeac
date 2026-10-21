#!/usr/bin/env python3
"""
Mode A/C timing check: how well do your receivers agree on Mode A/C reply times?

Works with as few as TWO receivers. Reads the server's --modeac-pairs-log (every Mode A/C
reply heard by 2+ receivers, already clock-corrected) and the ADS-B picture from readsb's
aircraft.json. When a reply's code is the *discrete* squawk of exactly one ADS-B aircraft,
we know where it came from, so we can predict the arrival-time difference between each pair
of receivers and compare with what was measured:

    residual = measured TDOA - predicted TDOA

The spread of the residuals (per receiver pair) is your real Mode A/C timing error: it
combines both receivers' timestamp noise and the clock-sync error. 100 ns ~ 30 m of range
difference. The median shows a fixed bias (e.g. a wrong antenna position).

Live (tail the log, poll readsb):
    ac_timing.py --pairs work/pairs.jsonl --aircraft-json http://localhost:8080/data/aircraft.json --follow

Offline against the simulator's truth log:
    ac_timing.py --pairs pairs.jsonl --truth truth.jsonl

Only needs the Python 3 standard library.
"""
import argparse
import bisect
import collections
import json
import math
import statistics
import sys
import time
import urllib.request

C_AIR = 299792458 / 1.0003          # same as mlat-server's constants.Cair
FT = 0.3048
SHARED = {"1200", "4000", "7000", "1255", "1277", "0000", "7777", "7500", "7600", "7700"}

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def llh2ecef(lat, lon, alt_m):
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return ((n + alt_m) * math.cos(la) * math.cos(lo),
            (n + alt_m) * math.cos(la) * math.sin(lo),
            (n * (1 - _E2) + alt_m) * math.sin(la))


def dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class KnownTracks:
    """Recent positions of known aircraft, keyed by id; lookup by discrete squawk."""

    def __init__(self, keep_s=120):
        self.keep_s = keep_s
        self.tracks = collections.defaultdict(list)    # id -> [(t, lat, lon, alt_ft)]
        self.squawk = {}                                # id -> squawk

    def add(self, key, squawk, t, lat, lon, alt_ft):
        tr = self.tracks[key]
        if tr and t <= tr[-1][0]:
            return
        tr.append((t, lat, lon, alt_ft))
        while tr and t - tr[0][0] > self.keep_s:
            tr.pop(0)
        if squawk:
            self.squawk[key] = squawk

    def position(self, squawk, t):
        """ECEF of the one aircraft with this discrete squawk at time t, or None."""
        if squawk in SHARED:
            return None
        keys = [k for k, s in self.squawk.items() if s == squawk and self.tracks[k]]
        if len(keys) != 1:
            return None
        tr = self.tracks[keys[0]]
        times = [p[0] for p in tr]
        i = bisect.bisect_left(times, t)
        if i == 0 or i >= len(tr):
            if tr and abs(tr[min(i, len(tr) - 1)][0] - t) < 1.5:
                p = tr[min(i, len(tr) - 1)]
                return llh2ecef(p[1], p[2], p[3] * FT)
            return None
        (t0, la0, lo0, a0), (t1, la1, lo1, a1) = tr[i - 1], tr[i]
        if t1 - t0 > 10:
            return None
        f = (t - t0) / (t1 - t0)
        return llh2ecef(la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0), (a0 + f * (a1 - a0)) * FT)


def load_aircraft_json(src):
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=5) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


class Stats:
    def __init__(self):
        self.res = collections.defaultdict(list)          # (rxA, rxB) -> [residual ns]
        self.matched = self.records = 0

    def add(self, rec, pos):
        self.records += 1
        if pos is None:
            return
        self.matched += 1
        rx = rec["rx"]
        user0, x0, y0, z0, ts0, _ = rx[0]
        d0 = dist(pos, (x0, y0, z0))
        for user, x, y, z, ts_us, _ in rx[1:]:
            predicted_us = (dist(pos, (x, y, z)) - d0) / C_AIR * 1e6
            residual_ns = (ts_us - ts0 - predicted_us) * 1000
            key = tuple(sorted((user0, user)))
            sign = 1 if key[0] == user0 else -1
            self.res[key].append(sign * residual_ns)

    def report(self):
        lines = ["%d logged replies, %d matched to an ADS-B aircraft by discrete squawk" % (self.records, self.matched)]
        if self.records >= 50 and self.matched == 0:
            lines.append("(none matched: the clients' mode_a_match filter drops squawks of ADS-B aircraft before\n"
                         " sending, so known traffic never reaches the server. For a timing check, set\n"
                         " mode_a_match \"enabled\": false in client-filter.json for a while, then set it back.)")
        lines.append("%-30s %6s %12s %14s %10s" % ("receiver pair", "n", "median (ns)", "spread (ns)", "~ metres"))
        for key, v in sorted(self.res.items()):
            if len(v) < 5:
                continue
            med = statistics.median(v)
            mad = statistics.median(abs(x - med) for x in v) * 1.4826       # robust std
            lines.append("%-30s %6d %12.0f %14.0f %10.0f" % (" - ".join(key), len(v), med, mad, mad * 1e-9 * C_AIR))
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", required=True, help="the server's --modeac-pairs-log file")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--aircraft-json", help="readsb aircraft.json (path or URL), polled live")
    src.add_argument("--truth", help="simulator truth log (testing)")
    ap.add_argument("--follow", action="store_true", help="keep reading the pairs log as it grows")
    ap.add_argument("--report-every", type=float, default=30)
    args = ap.parse_args()

    known, stats = KnownTracks(), Stats()

    if args.truth:
        with open(args.truth) as f:
            for line in f:
                d = json.loads(line)
                if "name" in d:
                    known.add(d["name"], d["squawk"], d["t"], d["lat"], d["lon"], d["alt"])
        known.keep_s = 1e12

    def poll():
        try:
            doc = load_aircraft_json(args.aircraft_json)
        except Exception as e:                                    # noqa: BLE001
            print("aircraft.json poll failed:", e, file=sys.stderr)
            return
        now = float(doc.get("now", time.time()))
        for a in doc.get("aircraft", []):
            if a.get("lat") is None or a.get("seen_pos") is None or not a.get("squawk"):
                continue
            alt = a.get("alt_geom", a.get("alt_baro"))
            if not isinstance(alt, (int, float)):
                continue
            known.add(a["hex"], str(a["squawk"]), now - float(a["seen_pos"]), a["lat"], a["lon"], alt)

    pending = collections.deque()          # live mode: wait until positions around t exist
    last_poll = last_report = 0.0
    with open(args.pairs) as f:
        if args.follow:
            f.seek(0, 2)
        while True:
            line = f.readline()
            now = time.time()
            if args.aircraft_json and now - last_poll >= 1.0:
                poll()
                last_poll = now
            if line:
                rec = json.loads(line)
                if args.aircraft_json:
                    pending.append(rec)
                else:
                    stats.add(rec, known.position(rec["code"], rec["t"]))
            while pending and now - pending[0]["t"] > 2.0:
                rec = pending.popleft()
                stats.add(rec, known.position(rec["code"], rec["t"]))
            if not line:
                if not args.follow:
                    break
                if now - last_report >= args.report_every:
                    last_report = now
                    print(time.strftime("%H:%M:%S"), stats.report(), sep="\n", flush=True)
                time.sleep(0.2)
    while pending:
        rec = pending.popleft()
        stats.add(rec, known.position(rec["code"], rec["t"]))
    print(stats.report())


if __name__ == "__main__":
    main()
