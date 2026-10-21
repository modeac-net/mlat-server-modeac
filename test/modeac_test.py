"""Fast checks for the Mode A/C additions (the full chain is tested end to end with a simulated receiver network).
Run from the repo root after `python3 setup.py build_ext --inplace`:
    python3 -m unittest discover -s test -p "modeac_test.py" -t test
"""
import os
import sys
import math
import json
import struct
import tempfile
import unittest

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mlat import geodesy, jsonclient, mlattrack, modeac


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_later(self, delay, fn, *args):
        self.calls.append((delay, fn, args))


class FakeCoordinator:
    def __init__(self):
        self.loop = FakeLoop()
        self.clock_tracker = None


class FakeReceiver:
    def __init__(self, name, synced=True):
        self.user = name
        self.dead = False
        self.bad_syncs = 0
        self.last_sync = 1e12 if synced else 0.0


class UdpParseTest(unittest.TestCase):
    def test_type7_reaches_mlat_handler_with_2_bytes(self):
        proto = jsonclient.PackedMlatServerProtocol()
        got = []
        key = proto.add_client(sync_handler=lambda *a: None, mlat_handler=lambda t, m, utc: got.append((t, m)))
        hdr = jsonclient.PackedMlatServerProtocol.STRUCT_HEADER.pack(key, 0, 1_000_000)
        body = bytes([7]) + struct.pack(">i2s", 1234, b"\x40\x00")
        body += bytes([2]) + struct.pack(">i7s", 5, b"\x5d" * 7)            # a Mode S short too
        proto.datagram_received(hdr + body, ("127.0.0.1", 1))
        self.assertEqual(got[0], (1_001_234, b"\x40\x00"))
        self.assertEqual(len(got[1][1]), 7)


class RoutingTest(unittest.TestCase):
    def test_two_byte_messages_go_to_modeac(self):
        tr = mlattrack.MlatTracker.__new__(mlattrack.MlatTracker)
        seen = []

        class Stub:
            def receiver_mlat(self, *a):
                seen.append(a)
        tr.modeac = Stub()
        tr.receiver_mlat("rx", 1.0, b"\x40\x00", 2.0)
        self.assertEqual(len(seen), 1)


class TrackerTest(unittest.TestCase):
    def test_exclude_grouping_spi(self):
        coord = FakeCoordinator()
        t = modeac.ModeACTracker(coord, exclude={0x1200}, stats_interval=0)
        a, b = FakeReceiver("a"), FakeReceiver("b")
        t.receiver_mlat(a, 100, b"\x12\x00", 1.0)                 # excluded
        t.receiver_mlat(a, 101, b"\x40\x00", 1.0)
        t.receiver_mlat(b, 102, b"\x40\x80", 1.0)                 # same code with SPI
        self.assertEqual(t.stats["excluded"], 1)
        self.assertEqual(list(t.pending), [0x4000])
        g = t.pending[0x4000]
        self.assertEqual(len(g.copies), 2)
        self.assertTrue(g.spi)
        self.assertEqual(len(coord.loop.calls), 1)                # one resolve timer per group

    def test_min_receivers_floor(self):
        t = modeac.ModeACTracker(FakeCoordinator(), min_receivers=2, stats_interval=0)
        self.assertEqual(t.min_receivers, 4)

    def test_horizontal_error_ignores_vertical(self):
        lat, lon = 39.8, -83.0
        la, lo = math.radians(lat), math.radians(lon)
        up = numpy.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        east = numpy.array([-math.sin(lo), math.cos(lo), 0.0])
        cov = 1000.0 ** 2 * numpy.outer(up, up) + 100.0 ** 2 * numpy.outer(east, east)
        ecef = geodesy.llh2ecef((lat, lon, 5000))
        self.assertAlmostEqual(modeac.horizontal_error(ecef, cov, lat, lon), 100.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, **kw):
        self.sent.append(kw)


class ClientFilterTest(unittest.TestCase):
    def test_defaults(self):
        f = modeac.normalize_client_filter({})
        self.assertTrue(f["exclude"]["enabled"])
        self.assertEqual(f["exclude"]["codes"], ["1200"])
        self.assertTrue(f["mode_a_match"]["enabled"])
        self.assertIn("4000", f["mode_a_match"]["never_drop"])
        self.assertFalse(f["mode_c_match"]["enabled"])

    def test_partial_override_and_validation(self):
        f = modeac.normalize_client_filter({"mode_c_match": {"enabled": True}})
        self.assertTrue(f["mode_c_match"]["enabled"])
        self.assertEqual(f["mode_c_match"]["tolerance_ft"], 100)
        for bad in ({"exclude": {"codes": ["1289"]}}, {"mode_a_match": {"enabled": "yes"}},
                    {"mode_a_match": {"window_s": 0}}, {"modec": {}}, {"exclude": {"code": []}}, []):
            with self.assertRaises(ValueError, msg=repr(bad)):
                modeac.normalize_client_filter(bad)

    def test_file_reload_pushes_and_bad_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "client-filter.json")
            with open(path, "w") as f:
                json.dump({"mode_a_match": {"enabled": False}}, f)
            t = modeac.ModeACTracker(FakeCoordinator(), stats_interval=0, client_filter_path=path)
            self.assertFalse(t.client_filter["mode_a_match"]["enabled"])
            c = FakeClient()
            self.assertIs(t.register_client(c), t.client_filter)

            # change -> pushed to the connected client
            with open(path, "w") as f:
                json.dump({"mode_a_match": {"enabled": True}, "mode_c_match": {"enabled": True}}, f)
            os.utime(path, (1, 1))
            t._check_client_filter_file()
            self.assertEqual(len(c.sent), 1)
            self.assertTrue(c.sent[0]["modeac_filter"]["mode_c_match"]["enabled"])

            # typo -> rejected, settings kept, nothing pushed
            with open(path, "w") as f:
                f.write("{not json")
            os.utime(path, (2, 2))
            t._check_client_filter_file()
            self.assertEqual(len(c.sent), 1)
            self.assertTrue(t.client_filter["mode_c_match"]["enabled"])

            # same settings again -> nothing pushed; disconnected clients get nothing
            t.unregister_client(c)
            with open(path, "w") as f:
                json.dump({"mode_c_match": {"enabled": False}}, f)
            os.utime(path, (3, 3))
            t._check_client_filter_file()
            self.assertEqual(len(c.sent), 1)


class PairHintTest(unittest.TestCase):
    def test_reply_pairing_settings(self):
        f = modeac.normalize_client_filter({})
        self.assertTrue(f["reply_pairing"]["enabled"])
        self.assertFalse(f["reply_pairing"]["drop_paired"])
        f = modeac.normalize_client_filter({"reply_pairing": {"drop_paired": True}})
        self.assertTrue(f["reply_pairing"]["drop_paired"])
        self.assertFalse(f["reply_pairing"]["correlation"])     # off: unreliable on real traffic
        self.assertEqual(f["reply_pairing"]["min_ratio"], 0.4)
        for bad in ({"reply_pairing": {"drop_paired": 1}}, {"reply_pairing": {"min_gap_us": 7000}},
                    {"reply_pairing": {"spacing": 5}}, {"reply_pairing": {"correlation": "yes"}},
                    {"reply_pairing": {"min_ratio": 1.5}}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                modeac.normalize_client_filter(bad)

    def test_hints_validated_and_forwarded(self):
        t = modeac.ModeACTracker(FakeCoordinator(), stats_interval=0)
        got = []
        t.fix_handlers.append(got.append)
        t.receiver_pairs(FakeReceiver("site-a"), [["4000", "3020", 7], ["4000", "4000", 3], ["1289", "3020", 2]], 100.0)
        self.assertEqual(got, [{"pairs": [["4000", "3020", 7]], "rx": "site-a", "t": 100.0}])
        t.receiver_pairs(FakeReceiver("site-a"), "garbage", 101.0)
        t.receiver_pairs(FakeReceiver("site-a"), [], 101.0)
        self.assertEqual(len(got), 1)


class UnsyncedTest(unittest.TestCase):
    def test_unsynced_receiver_dropped_on_arrival(self):
        t = modeac.ModeACTracker(FakeCoordinator(), stats_interval=0)
        t.receiver_mlat(FakeReceiver("cold", synced=False), 100, b"\x40\x00", 1000.0)
        bad = FakeReceiver("bad")
        bad.bad_syncs = 2
        t.receiver_mlat(bad, 101, b"\x40\x00", 1000.0)
        self.assertEqual(t.stats["unsynced"], 2)
        self.assertEqual(t.pending, {})
        t.receiver_mlat(FakeReceiver("ok"), 102, b"\x40\x00", 1000.0)
        self.assertEqual(list(t.pending), [0x4000])


class AdmissionTest(unittest.TestCase):
    """modeac.net: receiver area and ban list."""

    def test_us_outline(self):
        from mlat import geofence
        o = geofence.Outline()
        inside = {'Columbus OH': (39.96, -83.00), 'Key West': (24.555, -81.78), 'Detroit': (42.33, -83.05),
                  'San Diego': (32.72, -117.16), 'Adak AK': (51.88, -176.66), 'Attu AK': (52.93, 173.2),
                  'Honolulu': (21.31, -157.86), 'Utqiagvik': (71.29, -156.79)}
        outside = {'San Juan PR': (18.47, -66.11), 'London': (51.5, -0.12), 'Toronto': (43.65, -79.38),
                   'Mexico City': (19.43, -99.13), 'swapped lat/lon': (-83.11, 40.10),
                   'missing minus': (40.10, 83.11), 'null island': (0.0, 0.0)}
        for name, (lat, lon) in inside.items():
            self.assertTrue(o.contains(lat, lon), name)
        for name, (lat, lon) in outside.items():
            self.assertFalse(o.contains(lat, lon), name)

    def test_box_overlap(self):
        from mlat import geofence
        o = geofence.Outline()
        self.assertFalse(o.touches_box(49, 55, -6, 2))           # UK
        self.assertTrue(o.touches_box(41.5, 46, -82, -76.5))     # Toronto area: hears the US
        self.assertTrue(o.touches_box(23, 28, -83, -77.5))       # Miami
        self.assertFalse(o.touches_box(17, 22, -102, -96))       # Mexico City
        self.assertTrue(o.touches_box(51, 54, 172, 179))         # western Aleutians

    def test_ohio_outline(self):
        from mlat import admission
        import os
        path = os.path.join(os.path.dirname(admission.__file__), 'data', 'ohio_outline.json')
        a = admission.Admission(area_path=path)
        self.assertIsNone(a.check(None, 'site-a', '10.0.0.1', 39.96, -83.00))
        self.assertIsNotNone(a.check(None, 'chicago', '10.0.0.1', 41.88, -87.63))

    def test_banlist(self):
        import os
        import tempfile
        from mlat import admission
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'banlist.txt')
            with open(p, 'w') as f:
                f.write('# comment\n0B7A3C52-1D2E-4F60-9A8B-123456789ABC  # fake\n203.0.113.7\n'
                        '198.51.100.0/24\nbaduser\n')
            a = admission.Admission(banlist_path=p)
            self.assertTrue(a.banned('0b7a3c52-1d2e-4f60-9a8b-123456789abc', 'x', None))
            self.assertTrue(a.banned(None, 'x', '203.0.113.7'))
            self.assertTrue(a.banned(None, 'x', '::ffff:198.51.100.42'))
            self.assertTrue(a.banned(None, 'baduser', None))
            self.assertFalse(a.banned('00000000-0000-0000-0000-000000000000', 'gooduser', '192.0.2.1'))
            reason, holdoff = a.check(None, 'baduser', None, 40.1, -83.1)
            self.assertGreater(holdoff, 3600)
            # unchanged file: no reload
            self.assertFalse(a.reload_banlist())
            os.utime(p, (1, 1))
            with open(p, 'w') as f:
                f.write('')
            self.assertTrue(a.reload_banlist())
            self.assertFalse(a.banned(None, 'baduser', None))


class FeederCheckTest(unittest.TestCase):
    def test_ip_parsing(self):
        from mlat import feedercheck
        self.assertEqual(feedercheck.ip_of('      203.0.113.7 port 5000'), '203.0.113.7')
        self.assertEqual(feedercheck.ip_of('TCP4 198.51.100.9 10.8.0.1 40223 30004'), '198.51.100.9')
        self.assertEqual(feedercheck.ip_of('::ffff:192.0.2.5 port 1'), '192.0.2.5')

    def test_autoban_expiry(self):
        from mlat import feedercheck
        state = {}
        f = {'verdict': 'outside', 'ip': '203.0.113.7', 'uuid': 'u1', 'box': [49, 53, -3, 3]}
        feedercheck.update_autoban(state, [f, {'verdict': 'ok', 'ip': '192.0.2.1', 'uuid': 'u2'}], 1000.0)
        self.assertEqual(set(state), {'203.0.113.7', 'u1'})
        feedercheck.update_autoban(state, [], 1000.0 + feedercheck.AUTOBAN_DAYS * 86400 + 1)
        self.assertEqual(state, {})

    def test_too_few_positions_is_new(self):
        from mlat import feedercheck, geofence
        o = geofence.Outline(feedercheck.area_path('us'))
        rec = {'receivers': [['99999999-8888-7777', 1, 0, 49, 53, -3, 3, 0, 51, 0]]}
        cli = {'clients': [['99999999-8888-7777-6666-555555555555', '1.2.3.4 port 1', 1, 10, 1, 1, 0, 0, 5]]}
        self.assertEqual(feedercheck.evaluate(rec, cli, {}, o)[0]['verdict'], 'new')
