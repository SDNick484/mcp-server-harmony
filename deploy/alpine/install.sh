#!/bin/sh
# Install mcp-server-harmony as an OpenRC service on Alpine (e.g. a Proxmox LXC).
# Same layout as mcp-server-webos's deploy/alpine, so the two sit side by side.
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# Layout (each part owned so the service can read its settings and change
# nothing else):
#   /opt/mcp-server-harmony/venv     the code                     root:root               0755
#   /opt/mcp-server-harmony/run.sh   the service command          root:root               0755
#   /etc/mcp-server-harmony/env      HTTP and Access settings     root:mcp-harmony        0640
#   /var/lib/mcp-server-harmony/     config.json (the hub list,   mcp-harmony             0700
#                                      HARMONY_CONFIG_DIR)
#   /var/log/mcp-server-harmony/     server.log                   mcp-harmony             0750
#   /etc/init.d/mcp-server-harmony   the OpenRC script
# config.json lives in /var/lib, not /etc, because it is the service's own:
# `check` (run as the service user) writes it, saving each hub's name. There are
# no credentials: a Harmony hub answers anyone on the LAN. The LXC needs TCP 8088
# to each hub (and UDP multicast 1900 only if you use `discover`).
# Re-running upgrades the code and keeps settings and the hub list.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-harmony}"
USER_NAME=mcp-harmony
PREFIX=/opt/mcp-server-harmony
ETC=/etc/mcp-server-harmony
STATE=/var/lib/mcp-server-harmony
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; git for the default git+https source. Every
# compiled dependency ships a musl wheel, so no compiler is needed (the smoke
# test proves it in a stock Alpine container).
apk add --no-cache python3 py3-pip openrc git >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
# Every commit carries the same version number, so pip calls an installed copy "already satisfied"
# and keeps it: re-running this script would upgrade nothing. Reinstall the package itself (not its
# dependencies) so it always gets the code from SOURCE.
"$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-harmony
install -m 0755 "$HERE/mcp-server-harmony.initd" /etc/init.d/mcp-server-harmony

AS_SERVICE="su -s /bin/sh $USER_NAME -c"
CMD="HARMONY_CONFIG_DIR=$STATE $PREFIX/venv/bin/mcp-server-harmony"
echo "Installed $("$PREFIX/venv/bin/mcp-server-harmony" --version)."
echo "Next: add each hub as the service user, check, then start:"
echo "  $AS_SERVICE '$CMD check --host <hub ip>'"
echo "  $AS_SERVICE '$CMD doctor'"
echo "  rc-update add mcp-server-harmony default && rc-service mcp-server-harmony start"
