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
# It stops the store and removes the venv and the model cache. It keeps
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

# Stop the store: SIGTERM to the pid in store.json, if that process is the store.
PID="$(python3 -c 'import json, sys
try:
    print(int(json.load(open(sys.argv[1]))["pid"]))
except Exception:
    pass' "$DATA_DIR/store.json" 2>/dev/null || true)"
if [ -n "$PID" ] && [ -r "/proc/$PID/cmdline" ] && tr '\0' ' ' <"/proc/$PID/cmdline" | grep -q 'noblivion'; then
    run kill -TERM "$PID"
elif [ -n "$PID" ] && [ ! -d /proc ] && ps -p "$PID" -o command= 2>/dev/null | grep -q 'noblivion'; then
    run kill -TERM "$PID"
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
