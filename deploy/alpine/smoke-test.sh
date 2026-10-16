#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against a simulated hub. Proves the dependencies install on musl (aiohttp,
# cryptography, pydantic-core, and slixmpp via aioharmony), the install layout
# and permissions, that OpenRC starts and stops it, and that an MCP call over
# HTTP reaches the hub.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation (the job log isn't always at
# hand; annotations are), with the end of the service log if there is one.
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/var/log/mcp-server-harmony/server.log}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='upgrade'
# Re-running install.sh must replace the code even though the version number hasn't changed
# (pip would otherwise keep the installed copy). Install a copy with a marker, then look for it.
cp -r /src /tmp/src2
echo '# smoke-test upgrade marker' >>/tmp/src2/src/harmony_mcp/__init__.py
sh /tmp/src2/deploy/alpine/install.sh /tmp/src2
grep -q 'smoke-test upgrade marker' /opt/mcp-server-harmony/venv/lib/python3*/site-packages/harmony_mcp/__init__.py

STEP='pytest on musl'
# The full test suite on musl, from the installed venv
/opt/mcp-server-harmony/venv/bin/pip install --quiet "/src[dev]"
STEP_LOG=/tmp/pytest.log
cd /src
/opt/mcp-server-harmony/venv/bin/pytest -q -p no:cacheprovider -rf >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

STATE=/var/lib/mcp-server-harmony
BIN=/opt/mcp-server-harmony/venv/bin/mcp-server-harmony
STEP='simulator'
# One simulated hub on 127.0.0.1:18088, with its config.json written as the
# service user (the port is in config.json; a real hub is always on 8088)
su -s /bin/sh mcp-harmony -c "$BIN simulate --hubs 1 --port 18088 --step-delay 0 --write-config $STATE" &
for _ in $(seq 100); do grep -q '"port"' "$STATE/config.json" 2>/dev/null && break; sleep 0.1; done

STEP='service start'
# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-harmony start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8713/healthz && break; sleep 0.2; done
rc-service mcp-server-harmony status

STEP='service user'
# The process runs as the service user, not root. (busybox ps truncates user
# names to 8 characters, so ask /proc for the owner of the server's process.)
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-harmony serve' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-harmony ]
STEP='file modes'
[ "$(stat -c '%a %U' $STATE)" = "700 mcp-harmony" ]
[ "$(stat -c %U $STATE/config.json)" = mcp-harmony ]

STEP='mcp over http'
# A real MCP session against the service: initialize, then get_status, which
# must show the simulated hub reachable (so the service user's config.json was
# read and aioharmony connected).
URL=http://127.0.0.1:8713/harmony/mcp
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'
# shellcheck disable=SC2086
SID=$(curl -fsS -D - -o /dev/null $H "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
	| tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id" {print $2}')
[ -n "$SID" ]
# shellcheck disable=SC2086
curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
reachable=no
for _ in $(seq 30); do
	# shellcheck disable=SC2086
	out=$(curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_status","arguments":{}}}')
	echo "$out" | grep -q '\\"reachable\\": *true\|"reachable": *true' && { reachable=yes; break; }
	sleep 0.5
done
echo "$out" | tail -c 600
[ "$reachable" = yes ]

STEP='doctor'
# doctor as the service user, against the simulator
su -s /bin/sh mcp-harmony -c "HARMONY_CONFIG_DIR=$STATE $BIN doctor --timeout 5"

STEP='service stop'
rc-service mcp-server-harmony stop
cat /var/log/mcp-server-harmony/server.log
echo "Alpine smoke test passed"
