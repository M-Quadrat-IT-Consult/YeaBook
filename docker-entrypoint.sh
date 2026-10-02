#!/bin/sh
set -eu

: "${DATA_DIR:=/data}"
mkdir -p "$DATA_DIR"

# Respect an explicit Docker --user; this user must be able to write DATA_DIR.
if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

# Preserve bind-mount ownership unless the operator explicitly selects IDs.
# APP_UID/APP_GID remain aliases with precedence over PUID/PGID.
data_uid=$(stat -c '%u' "$DATA_DIR")
data_gid=$(stat -c '%g' "$DATA_DIR")
run_uid=${APP_UID:-${PUID:-$data_uid}}
run_gid=${APP_GID:-${PGID:-$data_gid}}

# A fresh Docker volume belongs to root; initialize it for an unprivileged user.
if [ "$run_uid" = "0" ]; then
    run_uid=1000
fi
if [ "$run_gid" = "0" ]; then
    run_gid=1000
fi

if [ "$data_uid" != "$run_uid" ] || [ "$data_gid" != "$run_gid" ] ||
   [ -n "${APP_UID:-}${APP_GID:-}${PUID:-}${PGID:-}" ]; then
    echo "[entrypoint] setting ownership of $DATA_DIR (uid=$run_uid gid=$run_gid)"
    chown -R "$run_uid:$run_gid" "$DATA_DIR"
fi

exec gosu "$run_uid:$run_gid" "$@"
