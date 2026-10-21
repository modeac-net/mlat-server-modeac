# Relay VPS (feed.modeac.net)

The VPS is the network's public front door. Volunteers connect to it; it passes their connections
to the home server through a WireGuard tunnel that the home server dials **out** to, so:

- the home IP address is never published and nothing is opened on the home router;
- each feeder's real IP address still reaches readsb and mlat-server (HAProxy adds a PROXY header
  to every connection), so bans and the feeder check work;
- only the three feed ports reach home, and only the hub and mlat-server: a compromised VPS can't
  reach the hub's web pages, other home devices, the home router or the relay container itself;
- the VPS holds no feeder data and no keys to anything at home. If it is lost or compromised:
  rebuild it, run `vps-setup.sh install` again and link the home server again (`add-home`).

```
volunteers ──30004/tcp, 31090/tcp──▶ VPS: HAProxy (adds PROXY header) ─┐
volunteers ──31091/udp─────────────▶ VPS: nftables (forwards) ─────────┤
                                                                         ╞══ WireGuard ══ home: relay-link ─▶ hub / mlat-server
             home relay-link ──ssh, inside the tunnel, one fixed command──▶ VPS ban list (nftables)
```

## What it's built from

Every security-relevant part is standard, widely reviewed software; our own code is only
configuration and two short shell scripts.

| Part | Where | Job |
|---|---|---|
| WireGuard | both ends (in the Linux kernel) | the encrypted tunnel; each end only accepts the other's key |
| HAProxy | VPS | feeders' TCP connections → home, with the PROXY header (as adsb.lol runs it) |
| nftables | both ends (in the Linux kernel) | VPS: ban list, ~20 new connections/min per address, UDP forwarding, nothing else forwarded. Home: only the feed ports pass, to the hub and mlat-server only |
| OpenSSH | VPS | the home server replaces the ban list; its key only works from inside the tunnel and only runs that one command |

## Files

| File | Where it runs | What it does |
|---|---|---|
| `vps-setup.sh` | VPS, once, as root | installs and configures all of the above; `add-home` links the home server; `status` |
| `relay.sh` | home, `relay-link` service | brings up the tunnel, the forwarding rules, and pushes the ban list (`config/banlist.txt` + `data/feedercheck/autoban.txt`) every 30 s when it changes |

`tools/relay_test.sh` runs the whole thing (VPS, home router, relay, feeders, a "compromised VPS")
in network namespaces with these exact configs: 20 checks, including what a compromised VPS can reach.

## Setup (when the VPS exists)

1. Any small Ubuntu 22.04+ / Debian 12+ VPS with a public IPv4 address. DNS: an **A record**
   `feed.modeac.net` → the VPS IP, *not* proxied through Cloudflare (Cloudflare's proxy only carries
   web traffic). If the provider has its own firewall, allow 30004/tcp, 31090/tcp, 31091/udp and 51820/udp.
2. On the VPS: `bash vps-setup.sh install`. It prints `RELAY_PUBKEY=...`.
3. At home, in `.env`: `RELAY_HOST=feed.modeac.net` and that `RELAY_PUBKEY`, then
   `docker compose --profile relay up -d`.
4. `docker compose logs relay-link` prints two keys (WireGuard and SSH); on the VPS:
   `bash vps-setup.sh add-home '<wireguard key>' '<ssh key>'`.
5. Check: `docker compose logs relay-link` shows `tunnel to feed.modeac.net is up`;
   on the VPS `bash vps-setup.sh status`.

Volunteers then use `feed.modeac.net` for both the map feed (30004) and mlat-client (31090).

The home server's Linux needs WireGuard (built into Ubuntu, Debian and Docker Desktop's kernel). If
a kernel ever lacks it, relay-link falls back to the official userspace version (wireguard-go)
by itself.
