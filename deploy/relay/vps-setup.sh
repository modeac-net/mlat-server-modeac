#!/bin/bash
# modeac.net relay: one-time setup of a small VPS (Ubuntu 22.04+ / Debian 12+, run as root).
#
# The VPS is the network's public front door (feed.modeac.net). Feeders connect to it; it passes
# their connections to the home server through a WireGuard tunnel that the HOME server dials out
# to, so the home IP is never published and nothing is opened on the home router. The VPS holds
# no feeder data and no keys to anything at home: if it is ever lost or compromised, rebuild it.
#
# Everything security-relevant here is standard, heavily reviewed software from the distribution:
#   WireGuard (in the Linux kernel)  the tunnel to home
#   HAProxy                          feeders' TCP connections -> home, with a PROXY header carrying
#                                    each feeder's real IP (what adsb.lol runs for the same job)
#   nftables (in the Linux kernel)   ban list, connection-rate limits, UDP forwarding
#   OpenSSH                          the home server replaces the ban list (one fixed command, only
#                                    from inside the tunnel)
#
#   bash vps-setup.sh install                        install + configure; prints what home needs
#   bash vps-setup.sh add-home '<wg key>' '<ssh key>'  link the home server (both keys are in its log)
#   bash vps-setup.sh status                         tunnel, ports, ban count
#
# Public ports: 30004/tcp (ADS-B feed), 31090/tcp + 31091/udp (mlat-client), 51820/udp (tunnel).
# Leaves the provider's own firewall rules alone (own nftables table). Keep SSH for yourself.
set -euo pipefail

CONF=/etc/modeac
STATE=/var/lib/modeac
WG_PORT=51820
VPS_TUN=10.77.0.1
HOME_TUN=10.77.0.2
FEED_TCP="30004, 31090"
FEED_UDP=31091

need_root() { [ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }; }

# ---------------------------------------------------------------------------------------------
# config files (also used by tools/relay_test.sh: render <dir>)

render_nft() {
    cat <<EOF
table inet modeac
delete table inet modeac
table inet modeac {
    set banned4 { type ipv4_addr; flags interval; auto-merge; }
    set banned6 { type ipv6_addr; flags interval; auto-merge; }
    set flood4 { type ipv4_addr; flags dynamic; timeout 10m; }

    # after connection tracking, before forwarding decisions
    chain early {
        type filter hook prerouting priority -150; policy accept;
        # banned feeders: dropped on the feed ports only (never the tunnel or SSH)
        meta l4proto { tcp, udp } th dport { $FEED_TCP, $FEED_UDP } ip saddr @banned4 drop
        meta l4proto { tcp, udp } th dport { $FEED_TCP, $FEED_UDP } ip6 saddr @banned6 drop
        # reconnect storms / floods: at most ~20 new connections a minute per address
        tcp dport { $FEED_TCP } ct state new add @flood4 { ip saddr limit rate over 20/minute burst 30 packets } drop
    }

    # mlat-client's UDP timing data goes straight down the tunnel (it is keyed per session,
    # the feeder's address is not needed); TCP is handled by HAProxy instead
    chain nat_pre {
        type nat hook prerouting priority dstnat; policy accept;
        iifname != "wg0" udp dport $FEED_UDP dnat ip to $HOME_TUN:$FEED_UDP
    }
    chain nat_post {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "wg0" udp dport $FEED_UDP masquerade
    }

    # the VPS forwards nothing else, in either direction
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        oifname "wg0" udp dport $FEED_UDP ct status dnat accept
    }

    # from the tunnel: only the home server's ban-list push (SSH) and replies
    chain input {
        type filter hook input priority 0; policy accept;
        iifname "wg0" ct state established,related accept
        iifname "wg0" ip saddr $HOME_TUN tcp dport 22 accept
        iifname "wg0" drop
    }
}
EOF
}

render_haproxy() {
    cat <<EOF
# modeac.net relay (written by vps-setup.sh)
global
    log /dev/log local0 notice
    maxconn 4000
    user haproxy
    group haproxy
    daemon

defaults
    mode tcp
    log global
    option dontlognull
    timeout connect 5s
    timeout client 1h
    timeout server 1h
    timeout tunnel 1h

# ADS-B feed (beast_reduce_plus_out) -> home readsb
frontend adsb_feed
    bind 0.0.0.0:30004
    maxconn 2000
    default_backend home_adsb
backend home_adsb
    server home $HOME_TUN:30004 send-proxy

# mlat-client control connection -> home mlat-server
frontend mlat_tcp
    bind 0.0.0.0:31090
    maxconn 2000
    default_backend home_mlat
backend home_mlat
    server home $HOME_TUN:31090 send-proxy
EOF
}

render_ban_apply() {
    cat <<'EOF'
#!/bin/bash
# Load /var/lib/modeac/banned.txt (one IP or network per line) into the firewall sets.
set -euo pipefail
f=/var/lib/modeac/banned.txt
v4=(); v6=()
if [ -f "$f" ]; then
    while read -r a; do
        [ -z "$a" ] && continue
        if [[ "$a" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]]; then v4+=("$a")
        elif [[ "$a" =~ ^[0-9a-fA-F:]*:[0-9a-fA-F:.]*(/[0-9]{1,3})?$ ]]; then v6+=("$a")
        fi
    done < "$f"
fi
{
    echo "flush set inet modeac banned4"
    echo "flush set inet modeac banned6"
    [ ${#v4[@]} -gt 0 ] && echo "add element inet modeac banned4 { $(IFS=,; echo "${v4[*]}") }"
    [ ${#v6[@]} -gt 0 ] && echo "add element inet modeac banned6 { $(IFS=,; echo "${v6[*]}") }"
    true
} | nft -f -
echo "relay: blocking ${#v4[@]} IPv4 and ${#v6[@]} IPv6 entr(y/ies)"
EOF
}

render_ban_receive() {
    cat <<'EOF'
#!/bin/bash
# Forced command for the home server's key: stdin is the full list of banned addresses.
set -euo pipefail
head -c 1048576 > /var/lib/modeac/banned.new
mv /var/lib/modeac/banned.new /var/lib/modeac/banned.txt
sudo -n /usr/local/sbin/modeac-ban-apply
EOF
}

render_wg() {   # $1 = private key
    cat <<EOF
# modeac.net relay tunnel (written by vps-setup.sh; the home peer is added by add-home)
[Interface]
Address = $VPS_TUN/30
ListenPort = $WG_PORT
PrivateKey = $1
EOF
}

# ---------------------------------------------------------------------------------------------

install_all() {
    need_root
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q wireguard-tools haproxy nftables
    mkdir -p "$CONF" "$STATE"

    # --- tunnel ----------------------------------------------------------------------------------
    umask 077
    [ -s /etc/wireguard/vps.key ] || wg genkey > /etc/wireguard/vps.key
    wg pubkey < /etc/wireguard/vps.key > /etc/wireguard/vps.pub
    if [ ! -s /etc/wireguard/wg0.conf ]; then
        render_wg "$(cat /etc/wireguard/vps.key)" > /etc/wireguard/wg0.conf
    fi
    umask 022
    echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/90-modeac.conf
    sysctl -q -p /etc/sysctl.d/90-modeac.conf

    # --- firewall + forwarding ---------------------------------------------------------------
    render_nft > "$CONF/modeac.nft"
    render_ban_apply > /usr/local/sbin/modeac-ban-apply
    render_ban_receive > /usr/local/sbin/modeac-ban-receive
    chmod 755 /usr/local/sbin/modeac-ban-apply /usr/local/sbin/modeac-ban-receive
    cat > /etc/systemd/system/modeac-firewall.service <<EOF
[Unit]
Description=modeac.net relay firewall (ban list, rate limits, UDP forwarding)
After=nftables.service network-pre.target
Before=haproxy.service wg-quick@wg0.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $CONF/modeac.nft
ExecStart=/usr/local/sbin/modeac-ban-apply

[Install]
WantedBy=multi-user.target
EOF

    # --- feeders' TCP connections ----------------------------------------------------------------
    render_haproxy > /etc/haproxy/haproxy.cfg
    haproxy -c -q -f /etc/haproxy/haproxy.cfg

    # --- the restricted account the home server pushes bans to -------------------------------
    id modeacban >/dev/null 2>&1 || useradd --system --create-home --home-dir "$STATE/ban-home" --shell /bin/bash modeacban
    usermod -p '*' modeacban      # no password at all (a 'locked' account can't log in with a key either)
    chown modeacban "$STATE"
    touch "$STATE/banned.txt"; chown modeacban "$STATE/banned.txt"
    echo "modeacban ALL=(root) NOPASSWD: /usr/local/sbin/modeac-ban-apply" > /etc/sudoers.d/modeacban
    chmod 440 /etc/sudoers.d/modeacban
    mkdir -p "$STATE/ban-home/.ssh"; touch "$STATE/ban-home/.ssh/authorized_keys"
    chown -R modeacban "$STATE/ban-home/.ssh"; chmod 700 "$STATE/ban-home/.ssh"; chmod 600 "$STATE/ban-home/.ssh/authorized_keys"

    systemctl daemon-reload
    systemctl enable --now modeac-firewall.service
    systemctl enable --now wg-quick@wg0.service
    systemctl enable haproxy.service
    systemctl restart haproxy.service

    echo
    echo "Relay installed. Put these two lines in the home server's .env:"
    echo "  RELAY_HOST=<this VPS's DNS name, e.g. feed.modeac.net>"
    echo "  RELAY_PUBKEY=$(cat /etc/wireguard/vps.pub)"
    echo "Then start the home side (docker compose --profile relay up -d); its log prints two keys."
    echo "Link it with: bash vps-setup.sh add-home '<wireguard key>' '<ssh key>'"
}

add_home() {
    need_root
    wgkey="$1"; sshkey="$2"
    [[ "$wgkey" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]] || { echo "the first key doesn't look like a WireGuard public key"; exit 1; }
    [[ "$sshkey" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+( .*)?$ ]] || { echo "the second key doesn't look like an ssh-ed25519 public key"; exit 1; }

    # tunnel peer: replace any earlier home key
    conf=/etc/wireguard/wg0.conf
    sed -i '/^\[Peer\]/,$d' "$conf"
    cat >> "$conf" <<EOF
[Peer]
# home server
PublicKey = $wgkey
AllowedIPs = $HOME_TUN/32
EOF
    wg syncconf wg0 <(wg-quick strip wg0)

    # ban-list push: only this key, only from inside the tunnel, only the one command
    line="from=\"$HOME_TUN\",command=\"/usr/local/sbin/modeac-ban-receive\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $sshkey"
    echo "$line" > "$STATE/ban-home/.ssh/authorized_keys"
    echo "Home server linked. The tunnel comes up within half a minute; check with: bash vps-setup.sh status"
}

status() {
    systemctl --no-pager --lines=0 status wg-quick@wg0 haproxy modeac-firewall || true
    echo
    wg show wg0 2>/dev/null | sed -n '1,20p' || echo "tunnel not up"
    echo
    ss -ltnup | grep -E ':(30004|31090|31091|51820)\b' || echo "relay ports not listening"
    echo
    n=$(grep -c . "$STATE/banned.txt" 2>/dev/null || true)
    echo "banned entries: ${n:-0}"
}

case "${1:-}" in
    install) install_all ;;
    add-home) add_home "${2:?usage: add-home '<wireguard key>' '<ssh key>'}" "${3:?usage: add-home '<wireguard key>' '<ssh key>'}" ;;
    status) status ;;
    render)   # for tests: write the generated configs into a directory
        d="${2:?usage: render <dir>}"; mkdir -p "$d"
        render_nft > "$d/modeac.nft"; render_haproxy > "$d/haproxy.cfg"
        render_ban_apply > "$d/modeac-ban-apply"; render_ban_receive > "$d/modeac-ban-receive" ;;
    *) sed -n '2,24p' "$0"; exit 1 ;;
esac
