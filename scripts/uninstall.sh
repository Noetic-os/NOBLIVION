#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Remove the NOBLIVION store venv (design doc 0001, section 13.3).
#
# Usage: uninstall.sh [--data-dir DIR] [--purge] [--dry-run]
#
#   --data-dir DIR  as in install.sh.
#   --purge         also remove the database, the config, the token and every
#                   other file in the data dir.
#   --dry-run       print the steps, change nothing.
#
# It stops the store, waits for it to exit, and removes the venv and the model cache. It keeps
# noblivion.db, config.json and the token unless --purge is given. It never
# touches memory files. Then remove the plugin and keep the data with:
#   claude plugin uninstall noblivion --keep-data
# Without --keep-data, Claude Code deletes the whole data dir, the database
# (memory index and trust history), config.json and the token included.
set -euo pipefail

DATA_DIR=""
PURGE=0
DRY_RUN=0

die() {
    printf 'noblivion uninstall: %s\n' "$*" >&2
    exit 1
}

run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf 'would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir)
            [ $# -ge 2 ] || die "--data-dir needs a folder"
            DATA_DIR="$2"
            shift 2
            ;;
        --purge) PURGE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help)
            sed -n '4,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

if [ -z "$DATA_DIR" ]; then
    DATA_DIR="${NOBLIVION_DATA_DIR:-${CLAUDE_PLUGIN_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/noblivion}}"
fi
[ -d "$DATA_DIR" ] || { printf 'noblivion uninstall: no data dir at %s\n' "$DATA_DIR"; exit 0; }
[ -e "$DATA_DIR/venv" ] || [ -e "$DATA_DIR/store.json" ] || [ -e "$DATA_DIR/noblivion.db" ] \
    || die "$DATA_DIR does not look like a NOBLIVION data dir; nothing removed"

# Stop the store and wait for it to exit (NOBLIVION-28): a store that still
# stops after this script ends could write in the data dir after Claude Code
# deleted it. SIGTERM to the pid in store.json, if that process is the store.
# The store finishes its current job batch first (at most about 70 s).
STOP_WAIT_S=90
is_store() {
    if [ -r "/proc/$1/cmdline" ]; then
        tr '\0' ' ' <"/proc/$1/cmdline" | grep -q 'noblivion'
    elif [ ! -d /proc ]; then
        ps -p "$1" -o command= 2>/dev/null | grep -q 'noblivion'
    else
        return 1
    fi
}
PID="$(python3 -c 'import json, sys
try:
    print(int(json.load(open(sys.argv[1]))["pid"]))
except Exception:
    pass' "$DATA_DIR/store.json" 2>/dev/null || true)"
if [ -n "$PID" ] && is_store "$PID"; then
    run kill -TERM "$PID"
    if [ "$DRY_RUN" = 1 ]; then
        printf 'would wait up to %s s for the store (pid %s) to exit\n' "$STOP_WAIT_S" "$PID"
    else
        printf 'noblivion uninstall: waiting for the store (pid %s) to exit\n' "$PID"
        waited=0
        while kill -0 "$PID" 2>/dev/null && is_store "$PID" && [ "$waited" -lt $((STOP_WAIT_S * 10)) ]; do
            sleep 0.1
            waited=$((waited + 1))
        done
        if kill -0 "$PID" 2>/dev/null && is_store "$PID"; then
            printf 'noblivion uninstall: the store did not exit in %s s; sending SIGKILL\n' "$STOP_WAIT_S" >&2
            kill -KILL "$PID" 2>/dev/null || true
            while kill -0 "$PID" 2>/dev/null; do sleep 0.1; done
        fi
        printf 'noblivion uninstall: the store stopped\n'
    fi
fi

if [ "$DRY_RUN" = 1 ]; then
    if [ "$PURGE" = 1 ]; then
        printf 'would remove: %s\n' "$DATA_DIR"
    else
        printf 'would remove: %s %s\n' "$DATA_DIR/venv" "$DATA_DIR/models"
    fi
    exit 0
fi
if [ "$PURGE" = 1 ]; then
    rm -rf -- "$DATA_DIR"
    printf 'noblivion uninstall: removed %s\n' "$DATA_DIR"
    printf 'noblivion uninstall: now remove the plugin: claude plugin uninstall noblivion\n'
else
    rm -rf -- "$DATA_DIR/venv" "$DATA_DIR/models"
    printf 'noblivion uninstall: removed the venv and the model cache; kept the database, config and token in %s\n' "$DATA_DIR"
    printf 'noblivion uninstall: now remove the plugin and keep this data: claude plugin uninstall noblivion --keep-data\n'
    printf 'noblivion uninstall: without --keep-data, Claude Code deletes %s, the database included\n' "$DATA_DIR"
fi
