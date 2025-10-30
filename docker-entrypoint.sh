#!/bin/sh
set -e

: "${APP_USER:=appuser}"
: "${PUID:=}"
: "${PGID:=}"
APP_UID=${APP_UID:-${PUID:-1000}}
APP_GID=${APP_GID:-${PGID:-1000}}
: "${DATA_DIR:=/data}"

# Ensure group exists with expected GID
if getent group "$APP_USER" >/dev/null 2>&1; then
    current_gid=$(getent group "$APP_USER" | cut -d: -f3)
    if [ "$current_gid" != "$APP_GID" ]; then
        groupmod -g "$APP_GID" "$APP_USER"
    fi
else
    groupadd -g "$APP_GID" "$APP_USER"
fi

# Ensure user exists with expected UID/GID
if id "$APP_USER" >/dev/null 2>&1; then
    current_uid=$(id -u "$APP_USER")
    if [ "$current_uid" != "$APP_UID" ]; then
        usermod -u "$APP_UID" "$APP_USER"
    fi
else
    useradd -M -u "$APP_UID" -g "$APP_GID" "$APP_USER"
fi

# Make sure data directory exists and is owned correctly
echo "[entrypoint] ensuring ownership of $DATA_DIR (uid=$APP_UID gid=$APP_GID)"
mkdir -p "$DATA_DIR"
chown -R "$APP_UID":"$APP_GID" "$DATA_DIR"

# If working directory is bind mounted ensure access
if [ -n "$APP_WORKDIR" ]; then
    mkdir -p "$APP_WORKDIR"
    chown -R "$APP_UID":"$APP_GID" "$APP_WORKDIR"
fi

# Drop privileges and exec
exec gosu "$APP_USER" "$@"
