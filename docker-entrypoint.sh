#!/bin/sh
set -eu

APP_USER="telegram"
CONFIG_SOURCE="${CONFIG_SOURCE_FILE:-}"
CONFIG_RUNTIME="/run/telegram-panel/config.json"

# Named volumes may have been created by an older root-running image. Correct
# ownership before dropping privileges so persisted sessions and logs remain
# writable after an upgrade.
mkdir -p /app/data /app/logs /data /run/telegram-panel /tmp
chown -R "$APP_USER:$APP_USER" /app/data /app/logs
# /data is on the immutable image layer in Compose; only repair it when an
# operator explicitly mounted it as the runtime data volume on another host.
if [ "${DATA_DIR:-}" = "/data" ]; then
  chown -R "$APP_USER:$APP_USER" /data
fi
chown "$APP_USER:$APP_USER" /run/telegram-panel

if [ -n "$CONFIG_SOURCE" ]; then
  if [ ! -f "$CONFIG_SOURCE" ] || [ ! -r "$CONFIG_SOURCE" ]; then
    echo "ERROR: config file is missing or unreadable: $CONFIG_SOURCE" >&2
    echo "Copy config.example.json to config/config.json, fill it in, and restart." >&2
    exit 1
  fi

  # The host config is mounted read-only and may be mode 0600. Copy it into the
  # ephemeral /run tmpfs with app-only permissions; it is refreshed on every
  # container start, while the source file survives restarts/rebuilds on the VPS.
  install -o "$APP_USER" -g "$APP_USER" -m 0600 "$CONFIG_SOURCE" "$CONFIG_RUNTIME"
  export CONFIG_FILE="$CONFIG_RUNTIME"
fi

# CONFIG_SOURCE_FILE is set by Compose. Other Docker platforms can use their
# protected environment-variable store instead and omit the host JSON mount.
exec gosu "$APP_USER" "$@"
