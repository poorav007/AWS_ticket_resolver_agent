#!/usr/bin/env bash
# Start the Ticket Resolver MCP server behind a public HTTPS tunnel.
#
# Why this exists: TrueForge registers MCP servers by URL and its connector
# refuses loopback/private hosts ("Outbound URL blocked for host 127.0.0.1"),
# which is standard SSRF protection. A tunnel gives it a public URL.
#
#   ./scripts/serve_with_tunnel.sh
#
# Prints the public URL to paste into TrueForge:
#   Settings -> Connectors -> Add MCP Server   (Auth: No auth)
#
# SECURITY: this exposes the MCP server to the public internet for as long as
# it runs. It carries no credentials of its own, but anyone who knows the URL
# can invoke the tools, including the (approval-gated) remediation tool.
# Keep it up only for the demo, and stop it when you are done.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=.venv/bin/python
PORT="${TICKET_RESOLVER_PORT:-8080}"
PIDS=()

cleanup() {
  echo
  echo "Shutting down tunnel + MCP server..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ ! -x "$PY" ]; then
  echo "error: .venv not found. Run:" >&2
  echo "  python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "error: cloudflared not found. Install it with: brew install cloudflared" >&2
  exit 1
fi

echo "Starting MCP server on 0.0.0.0:$PORT ..."
TICKET_RESOLVER_TRANSPORT=streamable-http \
TICKET_RESOLVER_HOST=0.0.0.0 \
TICKET_RESOLVER_PORT="$PORT" \
TICKET_RESOLVER_BACKEND="${TICKET_RESOLVER_BACKEND:-sim}" \
  "$PY" mcp-server/server.py &
PIDS+=($!)

# Wait for the local port to accept connections before tunnelling to it.
for _ in $(seq 1 40); do
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then break; fi
  sleep 0.25
done

LOG=$(mktemp)
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > "$LOG" 2>&1 &
PIDS+=($!)

URL=""
for _ in $(seq 1 60); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 0.5
done

if [ -z "$URL" ]; then
  echo "error: tunnel did not report a URL. cloudflared log:" >&2
  tail -20 "$LOG" >&2
  exit 1
fi

cat <<EOF

Ticket Resolver is live.

  Public URL : $URL/mcp
  Auth       : No auth

In TrueForge:  Settings -> Connectors -> Add MCP Server
  URL: $URL/mcp

Press Ctrl-C to stop the tunnel and the server.

EOF

wait
