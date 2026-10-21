#!/bin/bash
# Installs the lockdown as a service: applied at every boot, and again whenever Docker restarts.
#   sudo bash install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
install -m 755 "$here/modeac-lockdown.sh" /usr/local/sbin/modeac-lockdown
cat > /etc/systemd/system/modeac-lockdown.service <<'UNIT'
[Unit]
Description=modeac.net lockdown (containers can't reach the home network or tailnet)
After=docker.service tailscaled.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/modeac-lockdown apply
ExecStop=/usr/local/sbin/modeac-lockdown remove

[Install]
WantedBy=multi-user.target docker.service
UNIT
systemctl daemon-reload
systemctl enable modeac-lockdown.service >/dev/null
systemctl restart modeac-lockdown.service
echo "installed; it runs at every boot and whenever Docker restarts"
/usr/local/sbin/modeac-lockdown check
