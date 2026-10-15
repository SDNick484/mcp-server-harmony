#!/bin/sh
# What the OpenRC service runs (as user mcp-harmony). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. Run it by hand to see exactly what the service does:
#   su -s /bin/sh mcp-harmony -c /opt/mcp-server-harmony/run.sh
set -eu
ENV_FILE="${MCP_HARMONY_ENV:-/etc/mcp-server-harmony/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export HARMONY_CONFIG_DIR="${HARMONY_CONFIG_DIR:-/var/lib/mcp-server-harmony}"
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec /opt/mcp-server-harmony/venv/bin/mcp-server-harmony serve --http ${MCP_SERVE_ARGS:-}
