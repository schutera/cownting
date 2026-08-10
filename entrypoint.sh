#!/bin/sh
# Boot-time self-heal for the /app/data bind mount, then drop to the app user.
#
# The app runs as the unprivileged `cownting` user (uid 10001), but the data
# directory is a HOST bind mount: any root-run host tool that writes into it
# (a migration script, a manual copy) plants root-owned files the app cannot
# create siblings of or overwrite — which surfaces later as a 500 on some
# unrelated save (PermissionError on mkdir/open). The build-time chown in the
# Dockerfile cannot fix this: it runs before the mount exists.
#
# So the container starts as root just long enough to re-own anything in
# /app/data that drifted, then execs the real command as `cownting` with root
# privileges gone. The find only touches wrong-owned files, so a clean boot
# does no chown work at all.
set -eu

if [ "$(id -u)" = "0" ]; then
    if [ -d /app/data ]; then
        find /app/data ! -user cownting -exec chown cownting:cownting {} + || \
            echo "[cownting.entrypoint] WARNING: could not re-own some of /app/data" >&2
    fi
    exec setpriv --reuid=cownting --regid=cownting --init-groups "$@"
fi

# Already unprivileged (e.g. compose overrides `user:`): just run.
exec "$@"
