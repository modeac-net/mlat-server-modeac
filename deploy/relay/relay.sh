#!/bin/sh
# Home end of the relay (runs as the relay-link service in docker-compose.yml).
#
# Dials OUT to the VPS over WireGuard, so nothing is ever opened on the home router. Inside this
# container only, the tunnel's three feed ports are forwarded to the hub and mlat-server; nothing
# else at home can be reached from the tunnel (no route to anything else, everything else dropped).
# Feeders' real IP addresses arrive in the PROXY header HAProxy adds on the VPS (readsb and
# mlat-server both read it), so bans and the feeder check see real addresses.
#
# Also pushes banned IPs (config/banlist.txt + the feeder check's autoban.txt) to the VPS firewall,
# over the tunnel, with an SSH key the VPS only accepts for that one command.
#
# Settings from .env: RELAY_HOST (e.g. feed.modeac.net) and RELAY_PUBKEY (printed by vps-setup.sh).
set -eu

STATE="${RELAY_STATE:-/state}"
BANLISTS="${RELAY_BANLISTS:-/banlists}"
WG_KEY=$STATE/wg.key
SSH_KEY=$STATE/id_ed25519
VPS_TUN=10.77.0.1
HOME_TUN=10.77.0.2
WG_PORT="${RELAY_WG_PORT:-51820}"
HUB="${RELAY_HUB:-hub}"
MLAT="${RELAY_MLAT:-mlat-server}"
CHECK_S="${RELAY_CHECK_S:-30}"

log() { echo "relay: $*"; }

if [ -z "${RELAY_SKIP_APK:-}" ]; then
    apk add --no-cache -q wireguard-tools-wg iproute2 nftables openssh-client
fi

umask 077
mkdir -p "$STATE"
[ -s "$WG_KEY" ] || wg genkey > "$WG_KEY"
[ -s "$SSH_KEY" ] || ssh-keygen -q -t ed25519 -N '' -C modeac-relay -f "$SSH_KEY"
umask 022
log "keys for the VPS (run there: bash vps-setup.sh add-home '<wireguard key>' '<ssh key>'):"
log "  wireguard key: $(wg pubkey < "$WG_KEY")"
log "  ssh key:       $(cat "$SSH_KEY.pub")"

if [ -z "${RELAY_HOST:-}" ] || [ -z "${RELAY_PUBKEY:-}" ]; then
    log "RELAY_HOST / RELAY_PUBKEY not set in .env; nothing to do"
    exec sleep infinity
fi

if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" != 1 ]; then
    log "ERROR: IP forwarding is off in this container (docker-compose.yml sets net.ipv4.ip_forward=1); feeds can't pass"
fi

# --- tunnel ----------------------------------------------------------------------------------
ip link del wg0 2>/dev/null || true
if ! ip link add wg0 type wireguard 2>/dev/null; then
    # no WireGuard in this kernel: the official userspace version does the same job
    log "kernel WireGuard not available; using wireguard-go"
    [ -n "${RELAY_SKIP_APK:-}" ] || apk add --no-cache -q wireguard-go
    WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go wg0
fi
wg set wg0 private-key "$WG_KEY" \
    peer "$RELAY_PUBKEY" endpoint "$RELAY_HOST:$WG_PORT" allowed-ips "$VPS_TUN/32" persistent-keepalive 25
ip addr add "$HOME_TUN/32" peer "$VPS_TUN/32" dev wg0
ip link set wg0 mtu 1420 up

# --- forwarding: the three feed ports only ---------------------------------------------------
resolve() { getent hosts "$1" | awk '{print $1; exit}'; }

apply_rules() {   # $1 = hub address, $2 = mlat-server address
    nft -f - <<EOF
table ip relay
delete table ip relay
table ip relay {
    chain pre {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "wg0" tcp dport 30004 dnat to $1:30004
        iifname "wg0" tcp dport 31090 dnat to $2:31090
        iifname "wg0" udp dport 31091 dnat to $2:31091
    }
    chain post {
        type nat hook postrouting priority srcnat; policy accept;
        oifname != "wg0" ct status dnat masquerade      # so replies come back through here
    }
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        iifname "wg0" ct status dnat accept
    }
    chain input {
        type filter hook input priority 0; policy accept;
        iifname "wg0" ct state established,related accept
        iifname "wg0" drop
    }
}
EOF
}

# --- ban list ----------------------------------------------------------------------------------
SENT=$STATE/last-sent.txt
collect() {
    cat "$BANLISTS/banlist.txt" "$BANLISTS/feedercheck/autoban.txt" 2>/dev/null \
        | sed 's/#.*//' | tr -s ' \t' '\n\n' \
        | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$|^[0-9a-fA-F:]*:[0-9a-fA-F:.]*(/[0-9]{1,3})?$' \
        | sort -u || true
}
push_bans() {
    collect > "$STATE/bans.now"
    cmp -s "$STATE/bans.now" "$SENT" 2>/dev/null && return 0
    if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 \
           -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE/known_hosts" \
           "modeacban@$VPS_TUN" < "$STATE/bans.now" >/dev/null 2>&1; then
        cp "$STATE/bans.now" "$SENT"
        log "VPS now blocks $(grep -c . "$STATE/bans.now" || true) address(es)"
    fi
}

# --- keep it all current ------------------------------------------------------------------
cur=""; up=0
while true; do
    hub_ip=$(resolve "$HUB" || true); mlat_ip=$(resolve "$MLAT" || true)
    if [ -n "$hub_ip" ] && [ -n "$mlat_ip" ] && [ "$hub_ip $mlat_ip" != "$cur" ]; then
        apply_rules "$hub_ip" "$mlat_ip" && cur="$hub_ip $mlat_ip" && log "forwarding feed ports to $HUB ($hub_ip) and $MLAT ($mlat_ip)"
    fi
    hs=$(wg show wg0 latest-handshakes | awk '{print $2; exit}')
    age=$(( $(date +%s) - ${hs:-0} ))
    if [ "${hs:-0}" -gt 0 ] && [ "$age" -lt 180 ]; then
        [ "$up" = 1 ] || log "tunnel to $RELAY_HOST is up"
        up=1
        push_bans
    else
        [ "$up" = 0 ] || log "tunnel to $RELAY_HOST is down; retrying"
        up=0
        # the VPS may have a new address: look the name up again
        wg set wg0 peer "$RELAY_PUBKEY" endpoint "$RELAY_HOST:$WG_PORT" 2>/dev/null || true
    fi
    sleep "$CHECK_S"
done
