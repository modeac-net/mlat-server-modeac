#!/bin/bash
# End-to-end test of the relay (deploy/relay/) in Linux network namespaces. Run as root on a
# throwaway machine: it creates namespaces, a system user (modeacban) and files under
# /var/lib/modeac and /usr/local/sbin, as the real VPS install does.
#
#   feeder (192.0.2.50) --- internet --- vps (203.0.113.1: HAProxy, nftables, WireGuard, sshd)
#                              |
#                     home router (198.51.100.2, NAT) --- relay (relay.sh) --- hub / mlat-server
#
# Uses the real vps-setup.sh configs (render) and the real relay.sh. Needs: wireguard-tools,
# wireguard-go (or kernel WireGuard), haproxy, nftables, openssh-server, sudo, python3.
set -uo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
RELAY_DIR=$HERE/deploy/relay
W=${1:-$(mktemp -d)}
mkdir -p "$W"
PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL+1)); }
check() { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
inns() { local n=$1; shift; ip netns exec "$n" "$@"; }
export -f inns
# the home end gets its own /var/run/wireguard (one machine plays both ends here)
relay_wg() { nsenter -t "$(cat "$W/relay.pid")" -m -n wg "$@"; }

cleanup() {
    [ -f "$W/services.pid" ] && kill "$(cat "$W/services.pid")" 2>/dev/null
    [ -f "$W/relay.pid" ] && kill "$(cat "$W/relay.pid")" 2>/dev/null
    [ -f "$W/haproxy.pid" ] && kill "$(cat "$W/haproxy.pid")" 2>/dev/null
    [ -f "$W/sshd.pid" ] && kill "$(cat "$W/sshd.pid")" 2>/dev/null
    for n in feeder inet vps homegw relay; do
        ip netns pids "$n" 2>/dev/null | xargs -r kill 2>/dev/null
        ip netns del "$n" 2>/dev/null
    done
    rm -rf /etc/netns/relay
}
trap cleanup EXIT
cleanup

# --- network ---------------------------------------------------------------------------------
for n in feeder inet vps homegw relay; do ip netns add $n; inns $n ip link set lo up; done
link() {   # ns1 if1 addr1 ns2 if2 addr2
    ip link add "$2" netns "$1" type veth peer name "$5" netns "$4"
    inns "$1" ip addr add "$3" dev "$2"; inns "$1" ip link set "$2" up
    inns "$4" ip addr add "$6" dev "$5"; inns "$4" ip link set "$5" up
}
link feeder eth0 192.0.2.50/24     inet f0 192.0.2.254/24
link vps    eth0 203.0.113.1/24    inet v0 203.0.113.254/24
link homegw wan  198.51.100.2/24   inet h0 198.51.100.254/24
link relay  eth0 172.30.0.2/24     homegw lan 172.30.0.1/24
inns inet sysctl -qw net.ipv4.ip_forward=1
inns homegw sysctl -qw net.ipv4.ip_forward=1
inns relay sysctl -qw net.ipv4.ip_forward=1      # docker-compose.yml: sysctls
inns feeder ip route add default via 192.0.2.254
inns vps    ip route add default via 203.0.113.254
inns homegw ip route add default via 198.51.100.254
inns relay  ip route add default via 172.30.0.1
# home router: NAT out, nothing in (like a normal home router: no port forwards)
inns homegw nft -f - <<'EOF'
table ip home {
    chain post { type nat hook postrouting priority srcnat; oifname "wan" masquerade; }
    chain forward_home { type filter hook forward priority 0; policy drop;
                 ct state established,related accept; iifname "lan" accept; }
}
EOF
# "docker" services and other home things live on the home router namespace
for a in 172.30.0.10 172.30.0.11 172.30.0.20; do inns homegw ip addr add $a/32 dev lan; done
mkdir -p /etc/netns/relay
printf '127.0.0.1 localhost\n172.30.0.10 hub\n172.30.0.11 mlat-server\n' > /etc/netns/relay/hosts

cat > "$W/relay_test_services.py" <<'EOF'
import socket, sys, threading, os
out = sys.argv[1]
def tcp(addr, port, name):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((addr, port)); s.listen(50)
    while True:
        c, peer = s.accept()
        def h(c=c, peer=peer):
            data = c.recv(200)
            with open(os.path.join(out, name), "a") as f:
                f.write("%s %s\n" % (peer[0], data.split(b"\r\n")[0].decode(errors="replace")))
            c.sendall(b"hello\n"); c.close()
        threading.Thread(target=h, daemon=True).start()
def udp(addr, port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((addr, port))
    while True:
        d, peer = s.recvfrom(2000)
        with open(os.path.join(out, name), "a") as f:
            f.write("%s %s\n" % (peer[0], d.decode(errors="replace")))
for a in [("172.30.0.10", 30004, "hub_30004"), ("172.30.0.10", 80, "hub_80"),
          ("172.30.0.11", 31090, "mlat_31090"), ("172.30.0.20", 22, "pc_22"), ("172.30.0.1", 445, "router_445")]:
    threading.Thread(target=tcp, args=a, daemon=True).start()
threading.Thread(target=udp, args=("172.30.0.11", 31091, "mlat_31091"), daemon=True).start()
threading.Event().wait()
EOF
mkdir -p "$W/got"
inns homegw python3 "$W/relay_test_services.py" "$W/got" &
echo $! > "$W/services.pid"
sleep 0.5

# --- VPS (as vps-setup.sh install sets it up) ------------------------------------------------
bash "$RELAY_DIR/vps-setup.sh" render "$W/vps"
install -m 755 "$W/vps/modeac-ban-apply" /usr/local/sbin/modeac-ban-apply
install -m 755 "$W/vps/modeac-ban-receive" /usr/local/sbin/modeac-ban-receive
mkdir -p /var/lib/modeac
id modeacban >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/modeac/ban-home --shell /bin/bash modeacban
usermod -p '*' modeacban      # no password at all (a 'locked' account can't log in with a key either)
chown modeacban /var/lib/modeac; : > /var/lib/modeac/banned.txt; chown modeacban /var/lib/modeac/banned.txt
echo "modeacban ALL=(root) NOPASSWD: /usr/local/sbin/modeac-ban-apply" > /etc/sudoers.d/modeacban; chmod 440 /etc/sudoers.d/modeacban
mkdir -p /var/lib/modeac/ban-home/.ssh; chown -R modeacban /var/lib/modeac/ban-home/.ssh; chmod 700 /var/lib/modeac/ban-home/.ssh

inns vps sysctl -qw net.ipv4.ip_forward=1
wg genkey > "$W/vps.key"; VPS_PUB=$(wg pubkey < "$W/vps.key")
if ! inns vps ip link add wg0 type wireguard 2>/dev/null; then inns vps env WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go wg0 2>/dev/null; fi
inns vps wg set wg0 private-key "$W/vps.key" listen-port 51820
inns vps ip addr add 10.77.0.1/30 dev wg0; inns vps ip link set wg0 mtu 1420 up
inns vps nft -f "$W/vps/modeac.nft" || bad "VPS firewall loads"
inns vps /usr/local/sbin/modeac-ban-apply >/dev/null
sed -e "s|^    daemon$|    daemon\n    pidfile $W/haproxy.pid|" -e "s|^    log /dev/log.*|    log stdout format raw local0 notice|" \
    "$W/vps/haproxy.cfg" > "$W/haproxy.cfg"
inns vps haproxy -f "$W/haproxy.cfg" || bad "HAProxy starts"
ssh-keygen -q -t ed25519 -N '' -f "$W/sshd_host_key"
cat > "$W/sshd_config" <<EOF
ListenAddress 0.0.0.0:22
HostKey $W/sshd_host_key
PidFile $W/sshd.pid
AuthorizedKeysFile /var/lib/modeac/ban-home/.ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
EOF
mkdir -p /run/sshd
inns vps /usr/sbin/sshd -f "$W/sshd_config" || bad "sshd starts"

# --- home relay (the real relay.sh) ------------------------------------------------------------
mkdir -p "$W/state" "$W/banlists/feedercheck"
echo "# test bans" > "$W/banlists/banlist.txt"
start_relay() {
    mkdir -p /var/run/wireguard
    inns relay unshare -m sh -c 'mount -t tmpfs tmpfs /var/run/wireguard && exec "$@"' sh \
        env RELAY_SKIP_APK=1 RELAY_STATE="$W/state" RELAY_BANLISTS="$W/banlists" \
        RELAY_HOST=203.0.113.1 RELAY_PUBKEY="$VPS_PUB" RELAY_CHECK_S=2 \
        sh "$RELAY_DIR/relay.sh" >> "$W/relay.log" 2>&1 &
    echo $! > "$W/relay.pid"
}
start_relay
for i in $(seq 1 50); do [ -s "$W/state/id_ed25519.pub" ] && grep -q "wireguard key" "$W/relay.log" && break; sleep 0.2; done
HOME_WG=$(sed -n 's/.*wireguard key: //p' "$W/relay.log" | tail -1)
HOME_SSH=$(sed -n 's/.*ssh key: *//p' "$W/relay.log" | tail -1)
# add-home, as vps-setup.sh does it (peer + forced-command key limited to the tunnel address)
inns vps wg set wg0 peer "$HOME_WG" allowed-ips 10.77.0.2/32
echo "from=\"10.77.0.2\",command=\"/usr/local/sbin/modeac-ban-receive\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $HOME_SSH" \
    > /var/lib/modeac/ban-home/.ssh/authorized_keys

up() { for i in $(seq 1 ${1:-40}); do grep -q "tunnel to .* is up" "$W/relay.log" && return 0; sleep 0.5; done; return 1; }
check "tunnel comes up (home dials out through its NAT)" up
[ -n "${RELAY_TEST_HOLD:-}" ] && sleep "$RELAY_TEST_HOLD"
check "home router has no inbound port forwards (VPS cannot reach 198.51.100.2:22)" \
    bash -c "! inns vps timeout 2 bash -c '</dev/tcp/198.51.100.2/22' 2>/dev/null"

# --- feeders ---------------------------------------------------------------------------------
send_tcp() { inns feeder timeout 3 python3 -c "
import socket,sys; s=socket.create_connection(('203.0.113.1',$1),timeout=2); s.sendall(b'$2\r\n'); print(s.recv(10).decode().strip())" 2>/dev/null; }
probe() { inns vps timeout 2 bash -c "</dev/tcp/$1/$2" 2>/dev/null; }
export -f inns send_tcp probe
check "ADS-B feed (30004) reaches the hub" bash -c '[ "$(send_tcp 30004 beastdata)" = hello ]'
check "mlat-client (31090) reaches mlat-server" bash -c '[ "$(send_tcp 31090 handshake)" = hello ]' 
inns feeder python3 -c "
import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
for i in range(5): s.sendto(b'udp-%d'%i,('203.0.113.1',31091))"
sleep 1
check "hub sees the feeder's real IP in the PROXY header" grep -q "PROXY TCP4 192.0.2.50 .*beastdata\|PROXY TCP4 192.0.2.50" "$W/got/hub_30004"
check "mlat-server sees the feeder's real IP in the PROXY header" grep -q "PROXY TCP4 192.0.2.50" "$W/got/mlat_31090"
check "mlat UDP (31091) arrives at mlat-server" bash -c "[ \$(grep -c udp- '$W/got/mlat_31091' 2>/dev/null) -ge 4 ]"

# --- what a compromised VPS could reach -------------------------------------------------------
inns vps ip route add 172.30.0.0/24 dev wg0
inns vps wg set wg0 peer "$HOME_WG" allowed-ips 10.77.0.2/32,172.30.0.0/24
check "VPS cannot reach the hub's web page (172.30.0.10:80)" bash -c '! probe 172.30.0.10 80'
check "VPS cannot reach another home device (172.30.0.20:22)" bash -c '! probe 172.30.0.20 22'
check "VPS cannot reach the home router (172.30.0.1:445)" bash -c '! probe 172.30.0.1 445'
check "VPS cannot reach the relay container itself (10.77.0.2:22)" bash -c '! probe 10.77.0.2 22' 
check "nothing but the feed ports arrived at home" bash -c "[ ! -s '$W/got/hub_80' ] && [ ! -s '$W/got/pc_22' ] && [ ! -s '$W/got/router_445' ]"
inns vps ip route del 172.30.0.0/24 dev wg0
inns vps wg set wg0 peer "$HOME_WG" allowed-ips 10.77.0.2/32

# --- the tunnel can't be used to reach the internet or other VPS ports -------------------------
# (even if the home side tried: route the feeder's network into the tunnel, give it a return path)
inns feeder python3 -c "
import socket; s=socket.socket(); s.bind(('192.0.2.50',8000)); s.listen(5)
c,a=s.accept(); open('$W/got/feeder_8000','w').write(a[0])" &
FEEDER_LISTENER=$!
inns inet ip route add 10.77.0.0/30 via 203.0.113.1
relay_wg set wg0 peer "$VPS_PUB" allowed-ips 10.77.0.1/32,192.0.2.0/24
inns relay ip route add 192.0.2.0/24 dev wg0
inns relay timeout 2 bash -c '</dev/tcp/192.0.2.50/8000' 2>/dev/null
check "home side cannot use the VPS to reach the internet" bash -c "[ ! -s '$W/got/feeder_8000' ]"
inns relay ip route del 192.0.2.0/24 dev wg0
relay_wg set wg0 peer "$VPS_PUB" allowed-ips 10.77.0.1/32
kill $FEEDER_LISTENER 2>/dev/null

# --- bans --------------------------------------------------------------------------------------
echo "192.0.2.50   # test feeder" >> "$W/banlists/banlist.txt"
banned() { for i in $(seq 1 30); do inns vps nft list set inet modeac banned4 | grep -q 192.0.2.50 && return 0; sleep 0.5; done; return 1; }
check "ban list reaches the VPS over the tunnel" banned
check "banned feeder can no longer connect" bash -c '[ -z "$(send_tcp 30004 again)" ]' 
check "ban key only works from inside the tunnel" bash -c "! inns homegw ssh -i '$W/state/id_ed25519' -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 modeacban@203.0.113.1 true </dev/null 2>/dev/null"
sed -i '/192.0.2.50/d' "$W/banlists/banlist.txt"
unbanned() { for i in $(seq 1 30); do inns vps nft list set inet modeac banned4 | grep -q 192.0.2.50 || return 0; sleep 0.5; done; return 1; }
check "unban reaches the VPS" unbanned

# --- flood limit ---------------------------------------------------------------------------------
n=$(inns feeder python3 -c "
import socket
ok=0
for i in range(60):
    try:
        s=socket.create_connection(('203.0.113.1',30004),timeout=0.5); s.close(); ok+=1
    except OSError: pass
print(ok)")
check "rapid reconnects are cut off ($n of 60 got through)" bash -c "[ $n -ge 10 ] && [ $n -le 40 ]"

# --- home restart --------------------------------------------------------------------------------
kill "$(cat "$W/relay.pid")"; sleep 0.5
for p in $(ip netns pids relay); do [ "$(cat /proc/$p/comm 2>/dev/null)" = wireguard-go ] && kill $p; done
inns relay ip link del wg0 2>/dev/null
: > "$W/relay.log"; start_relay
check "tunnel comes back after the home side restarts" up 60
inns vps nft flush set inet modeac flood4
: > "$W/got/mlat_31090"
check "feeds work again after the restart" bash -c '[ "$(send_tcp 31090 again)" = hello ]' 

echo
echo "$PASS passed, $FAIL failed   (logs in $W)"
[ "$FAIL" = 0 ]
