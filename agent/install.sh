#!/bin/sh
# Landscape agent installer for Ubuntu.
# Usage: curl -fsS http://<server>:8081/agent-src/install.sh | sudo sh -s -- \
#          --server http://<server>:8443 --token <ENROLL_TOKEN>
set -eu

SERVER="" TOKEN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token)  TOKEN="$2";  shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "need --server and --token" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# The HTTP asset port serves the agent source; derive it from the mgmt URL host.
HOST=$(echo "$SERVER" | sed -E 's|https?://([^:/]+).*|\1|')
HTTP_PORT="${LANDSCAPE_HTTP_PORT:-8081}"

echo "Fetching agent from http://$HOST:$HTTP_PORT/agent-src/agent.py"
curl -fsS "http://$HOST:$HTTP_PORT/agent-src/agent.py" -o /usr/local/sbin/landscape-agent
chmod 0755 /usr/local/sbin/landscape-agent

/usr/local/sbin/landscape-agent --enroll --server "$SERVER" --token "$TOKEN"

cat > /etc/systemd/system/landscape-agent.service <<'EOF'
[Unit]
Description=Landscape management agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/sbin/landscape-agent
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now landscape-agent
echo "landscape-agent installed and running."
