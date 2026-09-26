#!/usr/bin/env bash
# Launch the Ticket Resolver MCP server as a remote streamable-HTTP server.
#
# TrueForge connects to MCP servers by URL (Settings -> Connectors), not by
# spawning local processes, so the demo needs this HTTP transport rather than
# the stdio transport OpenCode uses.
#
#   ./scripts/serve_for_trueforge.sh            # sim backend, port 8080
#   TICKET_RESOLVER_BACKEND=boto3 ./scripts/serve_for_trueforge.sh
#
# Then in TrueForge: Settings -> Connectors -> Add MCP Server
#   URL: http://127.0.0.1:8080/mcp
#   Auth: No auth
set -euo pipefail

cd "$(dirname "$0")/.."

export TICKET_RESOLVER_TRANSPORT="${TICKET_RESOLVER_TRANSPORT:-streamable-http}"
export TICKET_RESOLVER_HOST="${TICKET_RESOLVER_HOST:-127.0.0.1}"
export TICKET_RESOLVER_PORT="${TICKET_RESOLVER_PORT:-8080}"
export TICKET_RESOLVER_BACKEND="${TICKET_RESOLVER_BACKEND:-sim}"

if [ ! -x .venv/bin/python ]; then
  echo "error: .venv not found. Create it first:" >&2
  echo "  /opt/homebrew/bin/python3.13 -m venv .venv" >&2
  echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

echo "Ticket Resolver MCP server"
echo "  transport : $TICKET_RESOLVER_TRANSPORT"
echo "  url       : http://$TICKET_RESOLVER_HOST:$TICKET_RESOLVER_PORT/mcp"
echo "  backend   : $TICKET_RESOLVER_BACKEND"
echo
exec .venv/bin/python mcp-server/server.py
