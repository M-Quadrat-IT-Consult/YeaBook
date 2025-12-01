#!/bin/sh
set -e

: "${APP_USER:=appuser}"
: "${DATA_DIR:=/data}"

# track whether the user explicitly requested a UID/GID so we only chown then
explicit_uid_gid=0
if [ -n "${APP_UID:-}" ] || [ -n "${APP_GID:-}" ] || [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
    explicit_uid_gid=1
fi

mkdir -p "$DATA_DIR"

target_uid=${APP_UID:-${PUID:-}}
target_gid=${APP_GID:-${PGID:-}}

# If nothing was provided, default to the existing ownership of the data dir
if [ -z "$target_uid" ] || [ -z "$target_gid" ]; then
    if stat_output=$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null); then
        [ -z "$target_uid" ] && target_uid="${stat_output%%:*}"
        [ -z "$target_gid" ] && target_gid="${stat_output##*:}"
    fi
fi

: "${target_uid:=0}"
: "${target_gid:=0}"

if [ "$target_uid" = "0" ] && [ "$target_gid" = "0" ]; then
    echo "[entrypoint] no UID/GID override supplied; running as root and leaving permissions untouched"
    exec "$@"
fi

# Ensure group exists with expected GID (reuse existing group when possible)
if getent group "$target_gid" >/dev/null 2>&1; then
    :
else
    if getent group "$APP_USER" >/dev/null 2>&1; then
        groupmod -g "$target_gid" "$APP_USER"
    else
        groupadd -g "$target_gid" "$APP_USER"
    fi
fi

# Ensure user exists with expected UID/GID
if id "$APP_USER" >/dev/null 2>&1; then
    usermod -u "$target_uid" -g "$target_gid" "$APP_USER"
else
    useradd -M -u "$target_uid" -g "$target_gid" "$APP_USER"
fi

if [ "$explicit_uid_gid" -eq 1 ]; then
    echo "[entrypoint] ensuring ownership of $DATA_DIR (uid=$target_uid gid=$target_gid)"
    chown -R "$target_uid":"$target_gid" "$DATA_DIR"

    # If working directory is bind mounted ensure access
    if [ -n "$APP_WORKDIR" ]; then
        mkdir -p "$APP_WORKDIR"
        chown -R "$target_uid":"$target_gid" "$APP_WORKDIR"
    fi
else
    echo "[entrypoint] leaving ownership of $DATA_DIR unchanged (uid=$target_uid gid=$target_gid)"
fi

# Drop privileges and exec
exec gosu "$APP_USER" "$@"
