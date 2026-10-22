# modeac.net server stack

One `docker-compose.yml` runs the whole back end:

| Service | Image | Job |
|---|---|---|
| `hub` | ultrafeeder (no SDR) | readsb + tar1090 map; takes ADS-B / Mode S / UAT in on 30004, and on its MLAT input (32007) Mode A/C targets from `acfilter` and Mode S MLAT results from `mlat-server` |
| `mlat-server` | `ghcr.io/modeac-net/mlat-server-modeac` | clock sync, ordinary Mode S multilateration (tankers and other Mode S-only aircraft, sent to the hub) and Mode A/C multilateration (sent to acfilter); clients connect on 31090/tcp + 31091/udp |
| `acfilter` | `ghcr.io/modeac-net/modeac-filter` | tracks fixes, pairs squawk with altitude by position, hides traffic already known from ADS-B/Mode S, drops 1200 |
| `feedercheck` | `ghcr.io/modeac-net/mlat-server-modeac` | every 5 min: checks each feeder's reception area (from readsb) against the accepted area and its stated location; auto-bans feeders that only hear outside it |
| `web`, `cloudflared` | nginx, Cloudflare tunnel | public site (`--profile public`): the home page and feeding guide at modeac.net (`site/`), the live map at map.modeac.net; `web` keeps the feeder lists and admin pages private |
| `admin` | nginx | private admin page on port 8090: feeder map (sync lines, coverage) and a diagnostics table in plain English |
| `relay-link` | alpine + WireGuard | tunnel to the VPS relay and ban pushes (`--profile relay`) |

## Quick start

On a Linux machine with Docker (a small PC or old laptop is plenty to start):

1. The `modeac-filter` image isn't public yet. Log in to GitHub's registry once, with a classic
   token (GitHub → Settings → Developer settings → Personal access tokens (classic), only
   `read:packages`) from an account that can read it:
   ```sh
   docker login ghcr.io -u <your-github-username>     # paste the token as the password
   ```
2. Copy this `deploy` folder to the server, then in it:
   ```sh
   cp .env.example .env
   docker compose up -d
   ```
3. Map: `http://<server>:8080`. Admin page: `http://<server>:8090` (keep it private).
4. On Linux, lock it down: `sudo bash lockdown/install.sh` (see [Ports](#ports)).

Then point receivers at it. During testing that can be the server's LAN or VPN address; for
volunteers it's the relay (`feed.modeac.net`, see [relay/README.md](relay/README.md)). Receivers
follow the [client's README](https://github.com/modeac-net/mlat-client-modeac#join-with-ultrafeeder-about-5-minutes),
with `feed.modeac.net` replaced by your server's address for a private test.

## What to expect with two receivers

A Mode A/C fix needs **4** receivers (no altitude in the solve), so with two sites the map
shows ADS-B traffic only. What two sites *do* give you is the important number: how
well the receivers agree on Mode A/C reply times. Every reply both sites hear goes into
`data/mlat/pairs.jsonl`; this compares it against ADS-B aircraft whose positions are known:

```sh
docker compose exec mlat-server python3 /app/tools/ac_timing.py \
    --pairs /run/mlat/pairs.jsonl --aircraft-json http://hub/data/aircraft.json --follow
```

The client drops replies from aircraft it already knows from ADS-B, so for this check set
`mode_a_match` to `"enabled": false` in `config/client-filter.json` for a while (it's pushed to the
clients within 15 s), then set it back. Per-pair spread under ~300 ns is good; a large median offset
usually means a wrong antenna position or height.

## Client pre-filter (save upload and server CPU)

Most Mode A/C replies come from ordinary Mode S / ADS-B traffic. Each volunteer's client can drop
those before sending, the same matching readsb does for its console display. You control it
from **`config/client-filter.json`**: edit, save, and within 15 s mlat-server pushes the new
settings to every connected client. Nobody else touches anything.

| Tier | Default | Drops |
|---|---|---|
| `exclude` | on (`1200`) | listed codes, always |
| `mode_a_match` | on | a code that is the squawk of a Mode S aircraft that receiver heard in the last 60 s, except shared codes in `never_drop` (4000, 7000, emergencies, …) |
| `mode_c_match` | off | a code that is the Mode C altitude (±100 ft) of a Mode S aircraft heard in the last 30 s. Leave off: airliners fill most flight levels, so this also drops unknowns' altitude replies |
| `reply_pairing` | on | links one aircraft's squawk and altitude codes at each receiver: `sandwich` (squawk, altitude, squawk at the radar's exact spacing and same signal level) (on). `correlation` (same beam dwells at the same mean signal level) is off: on 15 min of real traffic it was right 41 times and wrong 108, against sandwich 23 / 0. `drop_paired` (off until checked on a real capture): drop altitude replies linked only to dropped codes, i.e. known traffic and 1200 VFR. `send_hints` (on): unknowns' squawk/altitude links go to ac-filter, which pairs them at once |

Before turning on `drop_paired`, check it on 15 minutes of real traffic. On one of your Pis:

```sh
docker exec -it mlat-modeac python3 /app/tools/ac_capture.py --connect ultrafeeder:30005 --seconds 900
```

It lists the radars' interrogation intervals, the code links found, and what share of replies each
tier would drop. The recording stays in the container at `/tmp/ac-capture.bin`
(`docker cp mlat-modeac:/tmp/ac-capture.bin .` to keep it).

Set `"enabled": false` on a tier to switch it off everywhere. If the file has a mistake, the old
settings stay and `docker compose logs mlat-server` says why. Each client's `Mode A/C:` stats line
shows what each tier dropped and what share was sent.

## Who may connect

Three layers, all automatic:

| | Checked | Cost |
|---|---|---|
| **Area** | When an mlat-client connects, its stated location must be inside the accepted area: `RECEIVER_AREA` in `.env`, `us` (50 states, no territories, 25 km margin) or `ohio` for a state-only test phase, or `off`. Refused volunteers are told why and wait 6 h before retrying. | once per connection |
| **What feeders hear** | `feedercheck` reads the hub's per-feeder reception boxes (readsb builds them from trusted ADS-B positions; typed-in coordinates can't fake them). A box that doesn't touch the area is banned automatically by IP and UUID for 7 days. A box far from the location the same IP's mlat-client states is only reported. readsb itself also times out feeders that send bad positions. `data/feedercheck/status.json` lists every feeder and its verdict. | every 5 min |
| **Ban list** | `config/banlist.txt` (by hand: UUIDs, IPs, networks, user names) plus `data/feedercheck/autoban.txt`. Saved changes apply within 15 s; connected matches are disconnected and told to stay away for a day. With the relay, banned IPs are also dropped at the VPS firewall. | once per connection |

`FEEDERCHECK_ACTION=report` in `.env` turns automatic bans into log lines only.

## Checks

```sh
docker compose logs -f mlat-server     # receivers connecting; "Mode A/C: received ..." every 5 min
docker compose logs -f acfilter        # fix source connected, connected to readsb
```

- On the receivers: `docker logs -f mlat-modeac` → `Handshake complete`, and the `Mode A/C:` stats line shows
  replies being sent. 0 received means `READSB_MODEAC=true` isn't active.
- `mlat-server` logs show whether the two receivers sync. If they never do, one dongle is
  probably not a TCXO (the server allows 5 ppm).
- `data/acfilter/status.json` lists every track and why it is shown or hidden.
- `docker compose logs -f feedercheck` → a `feeders:` count every 5 min; `data/feedercheck/status.json` for details.

## Ports

| Port | Who needs it | Exposure |
|---|---|---|
| 8080/tcp | you (map) | local only; public via the Cloudflare tunnel, never port-forwarded |
| 8090/tcp | you (admin page) | local / VPN only, never public |
| 30004/tcp | receivers (ADS-B feed) | via the VPS relay; LAN / VPN only for private tests |
| 31090/tcp, 31091/udp | receivers (mlat-client) | via the VPS relay at `feed.modeac.net`; LAN / VPN only for private tests |

With the relay, nothing at home is reachable from the internet: the `relay-link` service dials a
WireGuard tunnel out to the VPS, which takes feeders' connections on 30004 / 31090 / 31091 and passes
them down the tunnel with their real IP addresses attached (HAProxy's PROXY header). Only those three
ports reach home, and only the hub and mlat-server. See [relay/README.md](relay/README.md).

**Lockdown (Linux server):** `sudo bash lockdown/install.sh` keeps the containers from starting
connections to your home network or your Tailscale network, if you use one (if one were ever broken into, it can't
reach your other devices), and keeps the published ports off public IPv6. Containers still reach
the internet, DNS and each other, and receivers still connect in. It runs at every boot;
`sudo modeac-lockdown check` shows the rules and tests them from a throwaway container.

## Public site

In the Cloudflare tunnel, route **both** public hostnames, `modeac.net` and `map.modeac.net`, to `http://web:80`;
`web` serves the home page (`site/`) on the first and the map on the second. The home page's live
numbers come from `data/feedercheck/public.json` (totals only), refreshed every few minutes, and its
"Your feed" box asks the feeder check (port 8099, reachable only from `web`) which feeds are connected
from the visitor's own address, using the address Cloudflare reports.

With `TUNNEL_TOKEN` in `.env`, `docker compose --profile public up -d` (or `COMPOSE_PROFILES=public,relay` in `.env`) adds `web` (in front of the hub) and the Cloudflare tunnel.
In the Cloudflare dashboard, point the tunnel's hostname at `http://web:80`, and add one **Cache Rule**:
*when URI path starts with `/data/`* → *Eligible for cache*, *Edge TTL: use cache-control header if
present*. Cloudflare doesn't cache JSON unless told to; with the rule, `web`'s 1-second
`Cache-Control` lets each Cloudflare location serve all its visitors from one copy a second, so
your home upload no longer grows with the number of people watching. Check it from outside with
`curl -sI https://map.modeac.net/data/aircraft.json`: `cf-cache-status: HIT` on repeats means it works.

## Update

```sh
docker compose pull && docker compose up -d
```
