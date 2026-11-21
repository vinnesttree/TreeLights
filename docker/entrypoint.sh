#!/bin/sh
# Make the data volume writable for the unprivileged user, then drop root.
set -e
if [ "$(id -u)" = "0" ]; then
  chown -R treelights:treelights /data 2>/dev/null || true
  exec setpriv --reuid=treelights --regid=treelights --init-groups "$@"
fi
exec "$@"
