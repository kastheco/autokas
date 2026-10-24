#!/bin/bash
set -euo pipefail
umask 077
: "${CLI_PROXY_API_KEY:?CLI_PROXY_API_KEY is required}"
mkdir -p /data/auths
chmod 700 /data/auths
# Expand secrets and local variables here, not in Render's command field.
escaped_key=$(printf '%s' "$CLI_PROXY_API_KEY" | iconv -f UTF-8 -t UTF-32BE | od -An -v -tx1 | tr -d ' \n' | sed 's/......../\\U&/g')
: "${escaped_key:?encoded API key is empty}"
cat > /CLIProxyAPI/config.yaml <<EOF
host: ""
port: 8318
auth-dir: /data/auths
api-keys:
  - "${escaped_key}"
remote-management:
  allow-remote: false
  secret-key: ""
  disable-control-panel: true
ws-auth: true
usage-statistics-enabled: true
logging-to-file: false
request-log: false
requests:
  payload:
    override:
      - models:
          - name: "gpt-*"
            protocol: "codex"
        params:
          service_tier: priority
      - models:
          - name: "gpt-6-astra-ultrafast"
            protocol: "codex"
        params:
          service_tier: ultrafast
EOF
pids=""
trap 'kill $pids 2>/dev/null || true; wait' EXIT
trap 'exit 143' TERM INT
/usr/sbin/nginx -c /CLIProxyAPI/nginx.conf -g 'daemon off;' &
pids="$!"
/CLIProxyAPI/CLIProxyAPI -config /CLIProxyAPI/config.yaml &
pids="$pids $!"
wait -n $pids
exit 1
