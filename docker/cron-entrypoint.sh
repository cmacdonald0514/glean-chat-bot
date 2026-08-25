#!/bin/sh
# Materialise supercronic's crontab from the environment, then hand off to it.
#
# supercronic reads a crontab file and does no variable expansion of its own, so
# a schedule that comes from the environment has to be written to a file at
# start. Doing it here rather than as a `command:` in docker-compose.yml keeps
# the shell quoting out of YAML and makes the sidecar runnable on its own.
set -eu

: "${INDEX_SCHEDULE:?is not set, e.g. 0 3 * * *}"

printf '%s glean-index-trigger\n' "$INDEX_SCHEDULE" > /tmp/crontab
echo "scheduling 'glean-index-trigger' at '${INDEX_SCHEDULE}' -> ${INDEXER_URL:-<default>}" >&2

# exec so supercronic is PID 1 and receives SIGTERM from `docker compose down`
# directly, instead of this shell swallowing it. By absolute path, not on PATH:
# as PID 1 supercronic re-execs itself to enable child reaping, and that re-exec
# is a bare syscall on os.Args[0] with no PATH lookup -- "supercronic" alone
# fails there with a fatal "Failed to fork exec: no such file or directory".
exec /usr/local/bin/supercronic /tmp/crontab
