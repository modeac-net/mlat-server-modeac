# -*- mode: python; indent-tabs-mode: nil -*-

# modeac.net: who may connect.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

"""
Two checks, both made once when a receiver connects (nothing per message):

  * area:    the receiver's stated location must be inside the accepted outline
             (mlat/data/us_outline.json by default: the 50 states, no territories).
  * banlist: a plain text file of UUIDs, IP addresses and user names, one per line
             ('#' starts a comment). Edits are picked up within 15 s, and receivers that are
             already connected and now match are disconnected.

A refused client is told why and asked to wait hours before trying again (the client honours
the server's reconnect_in), so a misconfigured or banned feeder costs almost nothing.
"""

import ipaddress
import logging
import os

from mlat import geofence, util

glogger = logging.getLogger("admission")

AREA_HOLDOFF_S = 6 * 3600
BAN_HOLDOFF_S = 24 * 3600


def parse_banlist(text):
    uuids, users, ips, nets = set(), set(), set(), []
    for line in text.splitlines():
        item = line.split('#', 1)[0].strip()
        if not item:
            continue
        try:
            if '/' in item:
                nets.append(ipaddress.ip_network(item, strict=False))
            else:
                ips.add(str(ipaddress.ip_address(item)))
            continue
        except ValueError:
            pass
        if len(item) == 36 and item.count('-') == 4:
            uuids.add(item.lower())
        else:
            users.add(item)
    return uuids, users, ips, nets


class Admission:
    def __init__(self, area_path=None, banlist_path=None):
        self.outline = geofence.Outline(area_path) if area_path else None
        self.area_name = os.path.basename(area_path) if area_path else None
        # one or more files (comma-separated): e.g. the hand-kept list plus the feeder check's auto list
        if isinstance(banlist_path, str):
            banlist_path = [p.strip() for p in banlist_path.split(',') if p.strip()]
        self.banlist_paths = list(banlist_path or [])
        self.banlist_path = ', '.join(self.banlist_paths) or None
        self._ban_mtime = None
        self.uuids, self.users, self.ips, self.nets = set(), set(), set(), []
        self.refused_area = 0
        self.refused_ban = 0
        if banlist_path:
            self.reload_banlist(force=True)

    # --- checks ---------------------------------------------------------------

    def banned(self, uuid, user, ip):
        if uuid and str(uuid).lower() in self.uuids:
            return True
        if user and user in self.users:
            return True
        if ip:
            ip = str(ip)
            if ip.startswith('::ffff:'):
                ip = ip[7:]
            if ip in self.ips:
                return True
            if self.nets:
                try:
                    a = ipaddress.ip_address(ip)
                except ValueError:
                    return False
                return any(a in n for n in self.nets)
        return False

    def check(self, uuid, user, ip, lat, lon):
        """None if the receiver may connect, else (reason shown to the volunteer, reconnect_in)."""
        if self.banned(uuid, user, ip):
            self.refused_ban += 1
            return ('This receiver is not accepted by this server. If you think this is a mistake, '
                    'contact the server operator.', util.fuzzy(BAN_HOLDOFF_S))
        if self.outline is not None and not self.outline.contains(lat, lon):
            self.refused_area += 1
            return ('Receiver location {0:.4f},{1:.4f} is outside the area this server covers. '
                    'Check the latitude/longitude in your settings (a swapped pair or a missing minus '
                    'sign on the longitude is the usual cause), then restart the client.'.format(lat, lon),
                    util.fuzzy(AREA_HOLDOFF_S))
        return None

    # --- ban list reload -------------------------------------------------------

    def reload_banlist(self, force=False):
        """Re-read the ban list file(s) if any changed. Returns True if (re)loaded."""
        if not self.banlist_paths:
            return False
        mtimes = []
        for p in self.banlist_paths:
            try:
                mtimes.append(os.stat(p).st_mtime)
            except FileNotFoundError:
                mtimes.append(None)
        mtimes = tuple(mtimes)
        if not force and mtimes == self._ban_mtime:
            return False
        uuids, users, ips, nets = set(), set(), set(), []
        for p, m in zip(self.banlist_paths, mtimes):
            if m is None:
                continue
            try:
                with open(p) as f:
                    u, us, i, n = parse_banlist(f.read())
            except Exception:
                glogger.exception("could not read ban list %s; keeping the previous lists", p)
                return False
            uuids |= u
            users |= us
            ips |= i
            nets += n
        self._ban_mtime = mtimes
        self.uuids, self.users, self.ips, self.nets = uuids, users, ips, nets
        glogger.warning("ban list: %d uuid(s), %d user(s), %d ip(s), %d network(s)",
                        len(self.uuids), len(self.users), len(self.ips), len(self.nets))
        return True

    def enforce(self, receivers):
        """After a ban list change: disconnect connected receivers that now match."""
        for r in list(receivers):
            if self.banned(r.uuid, r.user, getattr(r.connection, 'source_ip', None)):
                glogger.warning("%s: banned, disconnecting", r.user)
                conn = r.connection
                try:
                    # sent through the session's own writer (zlib2 sessions buffer up to 1 s)
                    conn.send(deny=['This receiver is not accepted by this server.'],
                              reconnect_in=util.fuzzy(BAN_HOLDOFF_S))
                    conn.loop.call_later(3.0, conn.close)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        glogger.exception("closing %s", r.user)
