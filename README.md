# modeac.net server

The multilateration server behind **[modeac.net](https://modeac.net)**, a volunteer network that locates
aircraft whose transponders only answer in the old **Mode A/C** format (no ADS-B, no Mode S), such as
many military aircraft.

It's a fork of [wiedehopf/mlat-server](https://github.com/wiedehopf/mlat-server) that adds
multilateration of **Mode A/C replies**. Ordinary Mode S MLAT is unchanged and keeps working.

> **Want to feed?** You don't need anything from this repository: see the
> [receiver client](https://github.com/modeac-net/mlat-client-modeac#join-with-ultrafeeder-about-5-minutes),
> or the guide at [modeac.net/feed](https://modeac.net/feed/). Receivers anywhere in the US are welcome.

```
volunteers' receivers (mlat-client-modeac)
      │  ADS-B feed + Mode S / Mode A/C timing
      ▼
feed.modeac.net (relay VPS: HAProxy, nftables) ══ WireGuard ══▶ server: mlat-server-modeac ──fixes──▶ ac-filter ──▶ readsb / tar1090 map
                                                                                                                           │
                                                                         visitors ◀── Cloudflare (cache) ◀── web front ◀──┘
```

- **This repository:** the server, plus everything needed to run the network in [`deploy/`](deploy/README.md):
  one Docker Compose file for the server, the [relay](deploy/relay/README.md) that keeps the server's
  address private, a firewall lockdown, a private admin page, and the public web front.
- **[mlat-client-modeac](https://github.com/modeac-net/mlat-client-modeac):** the client volunteers run; it
  forwards Mode A/C replies alongside the usual MLAT data.
- **modeac-filter** (not public yet): turns fixes into map-ready aircraft: tracking, pairing each squawk
  with its altitude, and hiding aircraft that are already known from ADS-B.

## Running it

See **[deploy/README.md](deploy/README.md)**: quick start, who may connect, the client pre-filter,
checks, ports, the public site and the relay.

## How the Mode A/C path works (`mlat/modeac.py`)

Mode A/C replies have no aircraft address, so they can't use the Mode S path. That path keys
everything on the ICAO address, starts each solve from that aircraft's last position, and
throttles per aircraft. Instead, for each code:

1. Collect every copy of the code for `MLAT_DELAY` (0.9 s).
2. Put each receiver's timestamps on common clocks, using the same ADS-B-based clock sync as Mode S.
3. Split the group into clusters, one per individual transmission. Two aircraft on the same code become separate clusters.
4. **Solve every cluster** with ≥ 4 distinct receivers (no altitude in the solve), starting from the middle of those receivers.
5. Reject bad solutions (mirror solutions below ground, large horizontal error) and send each fix to ac-filter as one JSON line.

Squawk 1200 (configurable) is dropped on arrival.

## Changes from upstream

| File | Change |
|---|---|
| `mlat/coordinator.py` | A failing state-file write (full disk, permissions) no longer skips clearing clock-sync points, which used to silently degrade sync for every receiver. |
| `mlat/modeac.py` | new: the Mode A/C path above, the ac-filter connection, the pairs log |
| `mlat/jsonclient.py` | UDP record type 7 (`>i2s`: timestamp delta + 2-byte A/C word) |
| `mlat/mlattrack.py` | raw 2-byte messages (UDP type 7, or TCP `mlat` with 4 hex chars) are routed to the Mode A/C path |
| `mlat/main.py` | `--modeac*` options |
| `mlat/config.py` | AGPL source URL points at this repository |
| `test/modeac_test.py` | fast unit checks |
| `mlat/admission.py`, `mlat/geofence.py`, `mlat/data/` | who may connect: accepted area (US / Ohio outline), ban lists with live reload |
| `mlat/feedercheck.py`, `tools/feedercheck_test.py` | checks what each feeder actually hears against the area and its stated location |
| `mlat/clocktrack.pyx` | `normalize2` takes a minimum receiver count, so Mode A/C timing pairs work from 2 receivers (Mode S MLAT still needs 3) |
| `deploy/` | the complete modeac.net stack: compose file, relay, lockdown, admin page, public web front |
| `tools/ac_timing.py` | timing check from the pairs log (works with 2 receivers) |

## Options

| Option | Default | Meaning |
|---|---|---|
| `--modeac` | off | enable the Mode A/C path |
| `--modeac-fix-connect host:port` | none | send fixes to ac-filter (repeatable) |
| `--modeac-exclude` | `1200` | squawks dropped on arrival |
| `--modeac-min-receivers` | `4` | distinct receivers per fix (4 is the minimum without altitude) |
| `--modeac-max-error` | `5000` | discard fixes with a larger 1-sigma horizontal error (m) |
| `--modeac-max-group` | `200` | copies of one code per 0.9 s window |
| `--modeac-pairs-log FILE` | none | log every reply heard by 2+ receivers, clock-corrected |
| `--modeac-client-filter FILE` | built-in defaults | JSON pre-filter settings pushed to clients (exclude / Mode A match / Mode C match), re-read when the file changes; see [`deploy/config/client-filter.json`](deploy/config/client-filter.json) |
| `--modeac-stats-interval` | `300` | seconds between `Mode A/C:` statistics log lines |

## Client pre-filter protocol

A client whose handshake has `"modeac_forward": true` gets the current settings in the handshake
response as `"modeac_filter": {...}`, and again as a `{"modeac_filter": {...}}` line whenever the
`--modeac-client-filter` file changes. The client applies them before sending (see
mlat-client-modeac's `mlat/client/acmatch.py`). No settings = the client filters nothing. Server-side
`--modeac-exclude` and ac-filter still filter too, so a client running old code is harmless.

Clients with reply pairing send `{"modeac_pairs": [["4000", "3020", 12], ...]}` every few seconds: squawk/altitude
codes that receiver linked by reply timing. The server validates them and passes them to ac-filter as
`{"pairs": [...], "rx": "<receiver>", "t": <time>}` on the same connection as the fixes.

## Fix output (to ac-filter)

One JSON object per line:

```json
{"t": 1760366820.123, "word": 16384, "lat": 40.12, "lon": -83.47, "err": 180, "n": 5}
```

`word` is the hex-octal A/C word (squawk 4000 = `0x4000`; bit `0x80` = SPI). `err` is the 1-sigma
horizontal error in metres. `n` is the number of distinct receivers. The solved altitude is not
sent: with ground-based receivers it's too poor to use.

## Timing check with two receivers

With only two sites no fixes are possible, but the pairs log still fills up. Compare it with
readsb's ADS-B picture:

```sh
python3 tools/ac_timing.py --pairs /run/mlat/pairs.jsonl \
    --aircraft-json http://localhost:8080/data/aircraft.json --follow
```

It matches replies to ADS-B aircraft by **discrete squawk**, predicts the arrival-time difference
from their known positions, and reports per receiver pair:
- **median:** a fixed bias. Usually a wrong antenna position or height.
- **spread:** your real Mode A/C timing error. 100 ns ≈ 30 m.

Clients drop replies from aircraft they already know from ADS-B, which are exactly the ones this check
needs, so turn `mode_a_match` off in the client filter settings while it runs (see [deploy/README.md](deploy/README.md#what-to-expect-with-two-receivers)).

## Building

```sh
docker build -t mlat-server-modeac .
```

The image's default command listens for clients on 31090/tcp (+31091/udp), enables Mode A/C,
sends fixes to `acfilter:39100`, writes the pairs log to `/run/mlat/pairs.jsonl`, sends Mode S
MLAT results to the hub's MLAT input (`hub:32007`), accepts receivers inside the US only
(`MODEAC_RECEIVER_AREA`, see `mlat/admission.py`) and applies the ban lists
(`/etc/modeac/banlist.txt`, `/run/feedercheck/autoban.txt`).

## Credits and license

This is a fork of **[wiedehopf/mlat-server](https://github.com/wiedehopf/mlat-server)** (wiedehopf and
TanerH), which continues **[mutability/mlat-server](https://github.com/mutability/mlat-server)** by Oliver
Jowett. Clock synchronisation and Mode S multilateration, the heart of this server, are their work. The
original README, including its important licence notes, is kept in [README-upstream.md](README-upstream.md).
Please report problems with this fork here (receiver setup questions go to the
[client's issues](https://github.com/modeac-net/mlat-client-modeac/issues)), not upstream. Private matters: contact@modeac.net.

Licensed under the **GNU Affero General Public License v3** ([COPYING](COPYING)), like upstream. If others
connect to a server you run, you must offer them your modified source: this repository is modeac.net's,
and the server tells every connecting client where to find it (`AGPL_SERVER_CODE_URL` in `mlat/config.py`).
Third-party parts keep their own licences ([Leaflet](deploy/admin/leaflet/LICENSE), BSD-2-Clause).
