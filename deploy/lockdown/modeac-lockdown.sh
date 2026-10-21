#!/bin/bash
# modeac.net server lockdown: keeps the containers from reaching anything else you own.
#
# The services that face the internet (readsb, mlat-server, nginx) run in containers. If one were
# ever broken into, these rules stop it from starting connections to:
#   - your home network (router, PCs, printers... any private address), and
#   - your Tailscale network (your other devices).
# Containers can still talk to each other, reach the internet (Cloudflare, the relay VPS, updates),
# look up names (DNS), and answer connections that come IN (your receivers, your browser).
# It also stops the server's published ports from answering over public IPv6 (only Tailscale and
# this machine may use them over IPv6).
#
#   sudo bash modeac-lockdown.sh apply     apply the rules (the installed service does this at boot)
#   sudo bash modeac-lockdown.sh check     show the rules and test them from a throwaway container
#   sudo bash modeac-lockdown.sh remove    take the rules away again
set -euo pipefail

PORTS_TCP="8080,8090,30004,31090"
PORTS_UDP="31091"
PRIVATE="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16"
TAILNET6="fd7a:115c:a1e0::/48"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

apply() {
    # --- IPv4: containers -> your networks ----------------------------------------------------
    iptables -N MODEAC-EGRESS 2>/dev/null || iptables -F MODEAC-EGRESS
    iptables -A MODEAC-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN   # answers to incoming
    iptables -A MODEAC-EGRESS -o br+ -j RETURN                                       # container <-> container
    iptables -A MODEAC-EGRESS -o docker0 -j RETURN
    iptables -A MODEAC-EGRESS -o tailscale0 -j DROP                                  # never into the tailnet
    iptables -A MODEAC-EGRESS -p udp --dport 53 -j RETURN                            # DNS
    iptables -A MODEAC-EGRESS -p tcp --dport 53 -j RETURN
    for net in $PRIVATE; do
        iptables -A MODEAC-EGRESS -d "$net" -j DROP                                  # home network
    done
    iptables -A MODEAC-EGRESS -j RETURN                                              # the internet
    iptables -N DOCKER-USER 2>/dev/null || true
    for ifc in br+ docker0; do
        iptables -C DOCKER-USER -i "$ifc" -j MODEAC-EGRESS 2>/dev/null \
            || iptables -I DOCKER-USER 1 -i "$ifc" -j MODEAC-EGRESS
    done

    # --- IPv6: published ports only for this machine and Tailscale -----------------------------
    ip6tables -N MODEAC-IN6 2>/dev/null || ip6tables -F MODEAC-IN6
    ip6tables -A MODEAC-IN6 -s ::1 -j RETURN
    ip6tables -A MODEAC-IN6 -s "$TAILNET6" -j RETURN
    ip6tables -A MODEAC-IN6 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    ip6tables -A MODEAC-IN6 -p tcp -m multiport --dports "$PORTS_TCP" -j DROP
    ip6tables -A MODEAC-IN6 -p udp -m multiport --dports "$PORTS_UDP" -j DROP
    ip6tables -C INPUT -j MODEAC-IN6 2>/dev/null || ip6tables -I INPUT 1 -j MODEAC-IN6
    echo "modeac lockdown: on"
}

remove() {
    for ifc in br+ docker0; do
        while iptables -D DOCKER-USER -i "$ifc" -j MODEAC-EGRESS 2>/dev/null; do :; done
    done
    iptables -F MODEAC-EGRESS 2>/dev/null && iptables -X MODEAC-EGRESS 2>/dev/null || true
    while ip6tables -D INPUT -j MODEAC-IN6 2>/dev/null; do :; done
    ip6tables -F MODEAC-IN6 2>/dev/null && ip6tables -X MODEAC-IN6 2>/dev/null || true
    echo "modeac lockdown: off"
}

check() {
    echo "--- rules"
    iptables -S DOCKER-USER | grep MODEAC || echo "(not hooked into DOCKER-USER)"
    iptables -S MODEAC-EGRESS 2>/dev/null | sed 1d || echo "(no MODEAC-EGRESS chain)"
    ip6tables -S INPUT | grep MODEAC || echo "(not hooked into IPv6 INPUT)"
    echo "--- test from a throwaway container on the server's network"
    gw=$(ip -4 route show default | awk '{print $3; exit}')
    net=$(docker network ls --format '{{.Name}}' | grep -m1 '_default$' || echo bridge)
    ts=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin); p=[x for x in (d.get("Peer") or {}).values() if x.get("Online") and x.get("TailscaleIPs")]
print(p[0]["TailscaleIPs"][0] if p else "")' 2>/dev/null || true)
    docker run --rm --network "$net" alpine:3.20 sh -c "
        t() { if nc -z -w 3 \$1 \$2 2>/dev/null; then echo \"  reachable: \$3\"; else echo \"  blocked:   \$3\"; fi; }
        t $gw 80 'your router ($gw)          <- should be blocked'
        ${ts:+t $ts 22 'a Tailscale device ($ts) <- should be blocked'}
        t 1.1.1.1 443 'the internet (1.1.1.1)     <- should be reachable'
        t hub 80 'the hub container          <- should be reachable'
        if nslookup modeac.net >/dev/null 2>&1; then echo '  DNS works                   <- good'; else echo '  DNS FAILED'; fi
    "
}

case "${1:-}" in
    apply) apply ;;
    remove) remove ;;
    check) check ;;
    *) sed -n '2,17p' "$0"; exit 1 ;;
esac
