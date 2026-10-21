# -*- mode: python; indent-tabs-mode: nil -*-

# Part of mlat-server: a Mode S multilateration server
# Mode A/C extension for the modeac.net network.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Mode A/C multilateration.

Mode A/C replies are 2 bytes with no address, so they can't use the Mode S path (which keys
everything on the ICAO address, seeds each solve from that aircraft's last position and
throttles per aircraft). Instead, per code:

  1. collect every copy of that code for MLAT_DELAY seconds (one group per code)
  2. convert each receiver's timestamps onto common clocks (the same clock model the
     Mode S path uses, built from ADS-B sync traffic)
  3. split the group into clusters = individual transmissions heard by several receivers
     (two aircraft on the same code, or repeated replies, become separate clusters)
  4. solve EVERY cluster with >= min_receivers distinct receivers, starting from the
     centre of those receivers (never from a previous fix: that might be another aircraft)
  5. send each fix to ac-filter as one JSON line; ac-filter does tracking, squawk/altitude
     pairing and suppression of known traffic

No altitude is used in the solve (which code is the altitude isn't known yet), so a fix
needs 4 distinct receivers. Clusters heard by 2+ receivers can also be written to a
"pairs" log for measuring the network's timing accuracy (tools/ac_timing.py).
"""

import asyncio
import collections
import functools
import json
import logging
import math
import os
import socket

import numpy

from mlat import clocktrack, geodesy, net, solver, config, util
from mlat.mlattrack import _cluster_timestamps

glogger = logging.getLogger("modeac")

CODE_MASK = 0x7777
SPI_BIT = 0x0080


def code_str(code):
    return "%04x" % (code & CODE_MASK)


# ---------------------------------------------------------------------------------------------
# Client pre-filter settings ("modeac_filter"), sent to every modeac.net client in the handshake
# response and again whenever the settings file changes. The client applies them before sending,
# see mlat-client-modeac's mlat/client/acmatch.py.

DEFAULT_CLIENT_FILTER = {
    # codes always dropped
    "exclude": {"enabled": True, "codes": ["1200"]},
    # drop a code that is the squawk of a Mode S aircraft the receiver heard in the last window_s
    "mode_a_match": {"enabled": True, "window_s": 60,
                     "never_drop": ["0000", "1200", "1255", "1277", "4000", "7000",
                                    "7400", "7500", "7600", "7700", "7777"]},
    # drop a code that is the Mode C altitude (+/- tolerance_ft) of a Mode S aircraft heard in window_s
    "mode_c_match": {"enabled": False, "window_s": 30, "tolerance_ft": 100},
    # pair squawk / altitude codes of one aircraft from one receiver's reply timing (X Y X at the
    # radar's interrogation spacing, same signal level). drop_paired drops a code whose every
    # partner is dropped (altitude replies of known traffic / 1200); send_hints reports links
    # between kept codes (an unknown's squawk and altitude) so ac-filter pairs them at once.
    # Two linkers: sandwich (reply timing, fast) and correlation (same beam dwells at the same mean
    # signal level, dwell after dwell; slower, but works for staggered-PRI radars too).
    # Correlation is off: on real traffic (one receiver, 15 min, checked against aircraft.json) sandwich
    # links were 23/23 right but correlation links 41 right / 108 wrong - airliners at different
    # levels in the same beam at similar signal levels look alike dwell after dwell.
    "reply_pairing": {"enabled": True, "drop_paired": False, "send_hints": True, "hint_interval_s": 5,
                      "sandwich": True, "min_hits": 3, "window_s": 30, "min_gap_us": 1500, "max_gap_us": 6000,
                      "spacing_tol_us": 5, "signal_tol_db": 3, "stagger_test": False,
                      "correlation": False, "burst_gap_ms": 25, "burst_signal_tol_db": 2, "min_bursts": 4,
                      "min_ratio": 0.4, "corr_window_s": 120, "min_replies_per_burst": 2},
}


def _codes(value, what):
    if not isinstance(value, (list, tuple)):
        raise ValueError("{0} must be a list of squawks".format(what))
    out = []
    for c in value:
        c = str(c).strip()
        if len(c) != 4 or any(ch not in "01234567" for ch in c):
            raise ValueError("{0}: {1!r} is not a 4-digit octal squawk".format(what, c))
        out.append(c)
    return sorted(set(out))


def _number(value, lo, hi, what):
    v = float(value)
    if not lo < v <= hi:
        raise ValueError("{0} must be > {1} and <= {2}".format(what, lo, hi))
    return v


def normalize_client_filter(cfg):
    """Validate settings (missing keys take the defaults) and return the canonical dict."""
    if not isinstance(cfg, dict):
        raise ValueError("settings must be a JSON object")
    unknown = set(cfg) - set(DEFAULT_CLIENT_FILTER) - {"_comment"}
    if unknown:
        raise ValueError("unknown setting(s): " + ", ".join(sorted(unknown)))
    out = {}
    for tier, default in DEFAULT_CLIENT_FILTER.items():
        given = cfg.get(tier, {})
        if not isinstance(given, dict):
            raise ValueError("{0} must be an object".format(tier))
        bad = set(given) - set(default) - {"_comment"}
        if bad:
            raise ValueError("{0}: unknown key(s) {1}".format(tier, ", ".join(sorted(bad))))
        merged = dict(default)
        merged.update({k: v for k, v in given.items() if k != "_comment"})
        if not isinstance(merged["enabled"], bool):
            raise ValueError("{0}.enabled must be true or false".format(tier))
        out[tier] = merged
    out["exclude"]["codes"] = _codes(out["exclude"]["codes"], "exclude.codes")
    out["mode_a_match"]["never_drop"] = _codes(out["mode_a_match"]["never_drop"], "mode_a_match.never_drop")
    out["mode_a_match"]["window_s"] = _number(out["mode_a_match"]["window_s"], 0, 3600, "mode_a_match.window_s")
    out["mode_c_match"]["window_s"] = _number(out["mode_c_match"]["window_s"], 0, 3600, "mode_c_match.window_s")
    out["mode_c_match"]["tolerance_ft"] = _number(out["mode_c_match"]["tolerance_ft"], -1, 1000,
                                                  "mode_c_match.tolerance_ft")
    rp = out["reply_pairing"]
    for k in ("drop_paired", "send_hints", "stagger_test", "sandwich", "correlation"):
        if not isinstance(rp[k], bool):
            raise ValueError("reply_pairing.{0} must be true or false".format(k))
    rp["hint_interval_s"] = _number(rp["hint_interval_s"], 0.999, 3600, "reply_pairing.hint_interval_s")
    rp["min_hits"] = int(_number(rp["min_hits"], 0, 1000, "reply_pairing.min_hits"))
    rp["window_s"] = _number(rp["window_s"], 0, 3600, "reply_pairing.window_s")
    rp["min_gap_us"] = _number(rp["min_gap_us"], 0, 20000, "reply_pairing.min_gap_us")
    rp["max_gap_us"] = _number(rp["max_gap_us"], 0, 20000, "reply_pairing.max_gap_us")
    if rp["min_gap_us"] >= rp["max_gap_us"]:
        raise ValueError("reply_pairing: min_gap_us must be below max_gap_us")
    rp["spacing_tol_us"] = _number(rp["spacing_tol_us"], 0, 1000, "reply_pairing.spacing_tol_us")
    rp["signal_tol_db"] = _number(rp["signal_tol_db"], -1, 60, "reply_pairing.signal_tol_db")
    rp["burst_gap_ms"] = _number(rp["burst_gap_ms"], 0.999, 1000, "reply_pairing.burst_gap_ms")
    rp["burst_signal_tol_db"] = _number(rp["burst_signal_tol_db"], -1, 60, "reply_pairing.burst_signal_tol_db")
    rp["min_bursts"] = int(_number(rp["min_bursts"], 0, 1000, "reply_pairing.min_bursts"))
    rp["min_ratio"] = _number(rp["min_ratio"], 0, 1, "reply_pairing.min_ratio")
    rp["corr_window_s"] = _number(rp["corr_window_s"], 0, 3600, "reply_pairing.corr_window_s")
    rp["min_replies_per_burst"] = int(_number(rp["min_replies_per_burst"], 0, 1000,
                                              "reply_pairing.min_replies_per_burst"))
    return out


def describe_client_filter(f):
    ex, ma, mc = f["exclude"], f["mode_a_match"], f["mode_c_match"]
    return "; ".join([
        "exclude " + (",".join(ex["codes"]) or "nothing") if ex["enabled"] else "exclude off",
        "Mode A match on ({0:.0f} s)".format(ma["window_s"]) if ma["enabled"] else "Mode A match off",
        "Mode C match on ({0:.0f} s, +/-{1:.0f} ft)".format(mc["window_s"], mc["tolerance_ft"])
        if mc["enabled"] else "Mode C match off",
        "reply pairing on ({0}; drop paired {1}, hints {2})".format(
            " + ".join(m for m in ("sandwich", "correlation") if f["reply_pairing"][m]) or "no linker",
            "on" if f["reply_pairing"]["drop_paired"] else "off",
            "on" if f["reply_pairing"]["send_hints"] else "off")
        if f["reply_pairing"]["enabled"] else "reply pairing off"])


class ACGroup:
    __slots__ = ("code", "first_seen", "copies", "spi")

    def __init__(self, code, first_seen):
        self.code = code
        self.first_seen = first_seen
        self.copies = []
        self.spi = False


def horizontal_error(ecef, ecef_cov, lat, lon):
    """1-sigma horizontal error (m) from an ECEF covariance, rotated into east/north."""
    la, lo = math.radians(lat), math.radians(lon)
    east = numpy.array([-math.sin(lo), math.cos(lo), 0.0])
    north = numpy.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    var = east @ ecef_cov @ east + north @ ecef_cov @ north
    return math.sqrt(abs(var))


class ModeACTracker:
    def __init__(self, coordinator, *, exclude=frozenset({0x1200}), min_receivers=4, max_group=200,
                 max_error=5000.0, guess_alt=6000.0, pairs_filename=None, stats_interval=300.0,
                 client_filter_path=None, client_filter_check_interval=15.0):
        self.coordinator = coordinator
        self.loop = coordinator.loop
        self.clock_tracker = coordinator.clock_tracker
        self.exclude = frozenset(exclude)
        self.min_receivers = max(4, int(min_receivers))     # no altitude in the solve: 4 minimum
        self.max_group = max_group
        self.max_error = max_error
        self.guess_alt = guess_alt
        self.pending = {}
        self.fix_handlers = []
        self.stats = collections.Counter()
        self.pairs_file = open(pairs_filename, "a", buffering=1) if pairs_filename else None
        self.stats_interval = stats_interval
        if stats_interval > 0:
            self.loop.call_later(stats_interval, self._log_stats)

        # client pre-filter settings, live-reloaded from a JSON file
        self.clients = set()
        self.client_filter = normalize_client_filter({})
        self.client_filter_path = client_filter_path
        self.client_filter_check_interval = client_filter_check_interval
        self._client_filter_mtime = None
        if client_filter_path:
            self._check_client_filter_file(initial=True)
        glogger.warning("Mode A/C client pre-filter: %s", describe_client_filter(self.client_filter))

    # ------------------------------------------------------ client pre-filter
    def register_client(self, client):
        """A modeac.net client connected; returns the settings to put in its handshake response."""
        self.clients.add(client)
        return self.client_filter

    def unregister_client(self, client):
        self.clients.discard(client)

    def set_client_filter(self, cfg):
        """Validate, store and push new settings to every connected client. Raises ValueError."""
        new = normalize_client_filter(cfg)
        if new == self.client_filter:
            return False
        self.client_filter = new
        for client in list(self.clients):
            try:
                client.send(modeac_filter=new)
            except Exception:
                glogger.exception("failed to send pre-filter settings to a client")
        glogger.warning("Mode A/C client pre-filter changed, sent to %d client(s): %s",
                        len(self.clients), describe_client_filter(new))
        return True

    def _check_client_filter_file(self, initial=False):
        try:
            try:
                mtime = os.stat(self.client_filter_path).st_mtime
            except FileNotFoundError:
                if initial:
                    glogger.warning("Mode A/C client pre-filter file %s not found, using defaults",
                                    self.client_filter_path)
                mtime = None
            if mtime is not None and mtime != self._client_filter_mtime:
                self._client_filter_mtime = mtime
                try:
                    with open(self.client_filter_path) as f:
                        cfg = json.load(f)
                    self.set_client_filter(cfg)
                except (OSError, ValueError, TypeError) as e:
                    # keep the current settings: a typo must not switch filtering off for everyone
                    glogger.warning("Mode A/C client pre-filter file %s rejected, keeping current settings: %s",
                                    self.client_filter_path, e)
        finally:
            if self.client_filter_check_interval > 0:
                self.loop.call_later(self.client_filter_check_interval, self._check_client_filter_file)

    # ---------------------------------------------------------------- input
    def receiver_mlat(self, receiver, timestamp, message, utc):
        """A 2-byte Mode A/C reply from a client (UDP type 7 or TCP 4-hex-char mlat message)."""
        word = (message[0] << 8) | message[1]
        code = word & CODE_MASK
        self.stats["received"] += 1
        if receiver.bad_syncs > 0 or utc - receiver.last_sync > 20:
            # no usable clock sync: the reply can't be placed, drop it on arrival (as the TCP path does)
            self.stats["unsynced"] += 1
            return
        if code in self.exclude:
            self.stats["excluded"] += 1
            return
        group = self.pending.get(code)
        if group is None:
            group = self.pending[code] = ACGroup(code, utc)
            self.loop.call_later(config.MLAT_DELAY, self._resolve, group)
        if len(group.copies) >= self.max_group:
            self.stats["dropped_group_full"] += 1
            return
        group.copies.append((receiver, timestamp, utc))
        if word & SPI_BIT:
            group.spi = True

    def receiver_pairs(self, receiver, pairs, now):
        """Pair hints from one client: [[code, code, hits], ...] linked by reply timing at that
        receiver. Validated and passed on to ac-filter as one line."""
        out = []
        try:
            for item in pairs[:200]:
                a, b = str(item[0]), str(item[1])
                if (len(a) != 4 or len(b) != 4 or a == b
                        or any(ch not in "01234567" for ch in a + b)):
                    continue
                out.append([a, b, int(item[2]) if len(item) > 2 else 1])
        except (TypeError, ValueError, IndexError):
            self.stats["bad_pair_hints"] += 1
            return
        if not out:
            return
        self.stats["pair_hints"] += len(out)
        line = {"pairs": out, "rx": receiver.user, "t": round(now, 3)}
        for handler in list(self.fix_handlers):
            handler(line)

    # -------------------------------------------------------------- resolve
    def _resolve(self, group):
        if self.pending.get(group.code) is group:
            del self.pending[group.code]
        self.stats["groups"] += 1

        timestamp_map = {}
        for receiver, timestamp, utc in group.copies:
            if receiver.dead:
                continue
            timestamp_map.setdefault(receiver, []).append((timestamp, utc))
        if len(timestamp_map) < 2:
            return
        # diagnostics: where do codes heard by 2+ receivers stop?
        self.stats["groups_multi"] += 1

        try:
            # 2, not upstream's 3: two-site groups are what the pairs log (timing check) needs
            components = clocktrack.normalize2(clocktracker=self.clock_tracker, timestamp_map=timestamp_map,
                                               min_receivers=2)
        except Exception:
            glogger.exception("normalize failed")
            return

        joined = False
        clustered = False
        for component in components:
            if len(component) < 2:
                continue
            joined = True
            for distinct, first_seen, cluster in _cluster_timestamps(component, 2):
                clustered = True
                self.stats["clusters_2plus"] += 1
                if self.pairs_file:
                    self._log_pair(group, first_seen, cluster)
                if distinct >= self.min_receivers:
                    self._solve(group, distinct, first_seen, cluster)
        if not joined:
            self.stats["multi_no_clock_link"] += 1     # receivers' clocks couldn't be related for these times
        elif not clustered:
            self.stats["multi_no_match"] += 1          # related, but no two copies close enough in time

    def _initial_guess(self, cluster):
        x = sum(r.position[0] for r, _, _ in cluster) / len(cluster)
        y = sum(r.position[1] for r, _, _ in cluster) / len(cluster)
        z = sum(r.position[2] for r, _, _ in cluster) / len(cluster)
        lat, lon, _ = geodesy.ecef2llh((x, y, z))
        return geodesy.llh2ecef((lat, lon, self.guess_alt))

    def _solve(self, group, distinct, first_seen, cluster):
        self.stats["solve_attempts"] += 1
        try:
            r = solver.solve(cluster, None, None, self._initial_guess(cluster))
        except ValueError:
            return
        if not r:
            self.stats["solve_failed"] += 1
            return
        ecef, ecef_cov = r
        if ecef_cov is None:
            self.stats["solve_no_cov"] += 1
            return
        lat, lon, alt = geodesy.ecef2llh(ecef)
        if alt < config.MIN_ALT or alt > config.MAX_ALT:
            self.stats["solve_bad_alt"] += 1      # usually the mirror solution below ground
            return
        err = horizontal_error(ecef, ecef_cov, lat, lon)
        if not math.isfinite(err) or err > self.max_error:
            self.stats["solve_poor"] += 1
            return
        self.stats["fixes"] += 1
        fix = {
            "t": round(first_seen, 3),
            "word": group.code | (SPI_BIT if group.spi else 0),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "err": round(err),
            "n": distinct,
        }
        for handler in list(self.fix_handlers):
            handler(fix)

    # ------------------------------------------------------------- pairs log
    def _log_pair(self, group, first_seen, cluster):
        t0 = cluster[0][1]
        rec = {
            "t": round(first_seen, 3),
            "code": code_str(group.code),
            "rx": [[r.user, round(r.position[0]), round(r.position[1]), round(r.position[2]),
                    round((ts - t0) * 1e6, 3), round(math.sqrt(var) * 1e6, 3)]
                   for r, ts, var in cluster],
        }
        try:
            self.pairs_file.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            glogger.exception("pairs log write failed")

    def _log_stats(self):
        s = self.stats
        glogger.warning("Mode A/C: clients {n} received {r} unsynced {u} excluded {e} groups {g} "
                        "(2+rx {gm}: no clock link {nl}, no time match {nm}) 2+rx clusters {c} "
                        "solves {a} fixes {f} (failed {sf}, poor {sp}, bad alt {sb}) pair hints {ph} "
                        "outputs {o}".format(
                            ph=s["pair_hints"], gm=s["groups_multi"], nl=s["multi_no_clock_link"],
                            nm=s["multi_no_match"],
                            n=len(self.clients), u=s["unsynced"],
                            r=s["received"], e=s["excluded"], g=s["groups"], c=s["clusters_2plus"],
                            a=s["solve_attempts"], f=s["fixes"], sf=s["solve_failed"], sp=s["solve_poor"],
                            sb=s["solve_bad_alt"], o=len(self.fix_handlers)))
        self.loop.call_later(self.stats_interval, self._log_stats)


class ACFixClient:
    """Connection to ac-filter: one JSON object per line, one line per Mode A/C fix."""

    def __init__(self, reader, writer, *, modeac):
        peer = writer.get_extra_info("peername")
        self.logger = util.TaggingLogger(logging.getLogger("modeac"),
                                         {"tag": "acfilter {0}:{1}".format(peer[0], peer[1])})
        self.reader = reader
        self.writer = writer
        self.modeac = modeac
        self.reader_task = asyncio.ensure_future(self.read_until_eof())
        self.modeac.fix_handlers.append(self.write_fix)
        self.logger.warning("Connection established")

    def write_fix(self, fix):
        if not self.writer:
            return
        try:
            self.writer.write((json.dumps(fix, separators=(",", ":")) + "\n").encode("ascii"))
        except Exception:
            self.logger.exception("Failed to write fix")
            self.close()

    def close(self):
        if not self.writer:
            return
        self.logger.info("Connection lost")
        if self.write_fix in self.modeac.fix_handlers:
            self.modeac.fix_handlers.remove(self.write_fix)
        self.writer.close()
        self.writer = None

    async def wait_closed(self):
        await util.safe_wait([self.reader_task])

    async def read_until_eof(self):
        try:
            while True:
                r = await self.reader.read(1024)
                if len(r) == 0:
                    self.close()
                    return
        except socket.error:
            self.close()


def make_fix_connector(host, port, modeac):
    return net.MonitoringConnector(host, port, 5.0, functools.partial(ACFixClient, modeac=modeac))
