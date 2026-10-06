#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the NOBLIVION store venv in the data dir (design doc 0001, section 13.2).
#
# The Claude Code plugin system places the plugin files. This script only
# makes what the store needs: the venv (numpy, fastembed), the embedding
# model cache, the token, the default config file and a first index. Then it
# starts the store, so memory recall works in the session that ran it.
#
# The slash command /noblivion:setup (skills/setup/SKILL.md) runs this script
# from a Claude Code chat with the data dir filled in.
#
# Usage: install.sh [--data-dir DIR] [--no-embed] [--no-model] [--no-start] [--dry-run]
#
#   --data-dir DIR  the data dir. Default: NOBLIVION_DATA_DIR, else
#                   CLAUDE_PLUGIN_DATA, else ${XDG_DATA_HOME:-~/.local/share}/noblivion.
#                   The plugin hooks use CLAUDE_PLUGIN_DATA: the SessionStart
#                   hook prints the exact command when the venv is missing.
#   --no-embed      no numpy and fastembed: keyword search only
#                   (sets embedding.backend to none in config.json).
#   --no-model      do not download the embedding model now.
#   --no-start      do not start the store now: the next session starts it.
#   --dry-run       print the steps, change nothing.
#
# Needs python3 3.9 or newer (the hooks) and uv (the venv). uv fetches a
# python 3.11 or newer for the venv when the system has none.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=""
EMBED=1
MODEL=1
START=1
DRY_RUN=0

die() {
    printf 'noblivion install: %s\n' "$*" >&2
    exit 1
}

say() {
    printf 'noblivion install: %s\n' "$*"
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

# Set embedding.backend to none in config.json: keyword search only. This is
# the one key of an existing config.json that the script changes. It prints
# the value it replaced. A config.json that is not valid JSON, whose
# "embedding" is not an object, or that cannot be read or written, stays as
# it is: one line says so, and the install goes on.
backend_none() {
    NOBLIVION_DATA_DIR="$DATA_DIR" "$VENV/bin/python" - "$1" <<'PY'
import json, os, sys
from noblivion import config

why = sys.argv[1]
path = config.data_dir() / "config.json"


def give_up(problem):
    print(
        f"noblivion install: {path} {problem}; embedding.backend not changed. "
        f"Fix the file and set embedding.backend to none ({why})."
    )
    sys.exit(0)


try:
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except OSError as exc:
    give_up(f"cannot be read ({exc.strerror or exc})")
except ValueError:
    give_up("is not valid JSON")
embedding = doc.get("embedding", {}) if isinstance(doc, dict) else None
if not isinstance(embedding, dict):
    give_up("is not a JSON object" if not isinstance(doc, dict) else 'has an "embedding" that is not an object')
old = embedding.get("backend")
embedding["backend"] = "none"
doc["embedding"] = embedding
tmp = path.with_suffix(".tmp")
try:
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except OSError as exc:
    try:
        if tmp.is_file():
            tmp.unlink()
    except OSError:
        pass
    give_up(f"cannot be written ({exc.strerror or exc}: {exc.filename})")
was = f"; it was {old}" if old is not None and old != "none" else ""
print(f"noblivion install: {why}: set embedding.backend to none in {path}{was} (keyword search only)")
PY
}

while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir)
            [ $# -ge 2 ] && [ -n "$2" ] || die "--data-dir needs a folder"
            DATA_DIR="$2"
            shift 2
            ;;
        --no-embed) EMBED=0; MODEL=0; shift ;;
        --no-model) MODEL=0; shift ;;
        --no-start) START=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help)
            sed -n '4,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

if [ -z "$DATA_DIR" ]; then
    DATA_DIR="${NOBLIVION_DATA_DIR:-${CLAUDE_PLUGIN_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/noblivion}}"
fi
VENV="$DATA_DIR/venv"

# 1. python3 for the hooks, uv for the venv.
command -v python3 >/dev/null 2>&1 || die "python3 not found. The hooks need python3 3.9 or newer."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 is $(python3 -V 2>&1). The hooks need python3 3.9 or newer."
if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' \
        "noblivion install: uv not found. uv builds the store venv." \
        "Install it with the official installer, then run this script again:" \
        "    curl -LsSf https://astral.sh/uv/install.sh | sh" \
        "Other ways: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

VERSION="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' \
    "$ROOT/.claude-plugin/plugin.json")"
say "plugin $VERSION at $ROOT"
say "data dir $DATA_DIR"
[ "$DRY_RUN" = 1 ] && say "dry run: nothing changes"

run mkdir -p -m 700 "$DATA_DIR"
# mkdir -m does not change a folder that exists, and Claude Code makes the
# data dir with the user's umask before this script runs (NOBLIVION-27).
run chmod 700 "$DATA_DIR"

# 2. The venv with the locked store dependencies, then the package itself.
EXTRA=()
[ "$EMBED" = 1 ] && EXTRA=(--extra embed)
REQS="$DATA_DIR/.install-requirements.txt"
run uv venv --quiet --allow-existing --python ">=3.11" "$VENV"
run uv export --quiet --project "$ROOT" --frozen --no-dev --no-emit-project --no-hashes \
    ${EXTRA[@]+"${EXTRA[@]}"} --output-file "$REQS"
run uv pip install --quiet --python "$VENV/bin/python" -r "$REQS"
run rm -f "$REQS"
run uv pip install --quiet --python "$VENV/bin/python" --no-deps --reinstall-package noblivion "$ROOT"

# 3. The default config file, unless one exists. It holds the shipped recall
#    tuning (recall.env). An existing file is not replaced. Step 4 can change
#    one key in it: embedding.backend (--no-embed, or a failed download).
if [ ! -e "$DATA_DIR/config.json" ]; then
    run install -m 600 "$ROOT/config/config.default.json" "$DATA_DIR/config.json"
elif [ "$EMBED" = 0 ]; then
    say "config.json exists, kept as it is except embedding.backend (--no-embed, next step)"
else
    say "config.json exists, kept as it is"
fi

# 4. The embedding model. A failure is not fatal: keyword search still works.
#    Without fastembed the backend is none, else the store tries to load it,
#    reports "degraded" and logs a warning every hour (NOBLIVION-57).
#    --no-embed sets the backend to none also when the user chose another
#    one; the line says which value it replaced.
if [ "$EMBED" = 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        say "would set embedding.backend to none in config.json (keyword search only)"
    else
        backend_none "--no-embed" || say "could not change embedding.backend in $DATA_DIR/config.json"
    fi
elif [ "$MODEL" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
        say "would download the embedding model to $DATA_DIR/models"
    elif ! NOBLIVION_DATA_DIR="$DATA_DIR" "$VENV/bin/python" - <<'PY'
from noblivion import config, embedding

s = embedding.load_embedding_settings()
if s.backend == "fastembed":
    print(f"noblivion install: downloading {s.model} (about 70 MB)")
    embedding.FastEmbedEmbedder(
        s.model, models_dir=config.data_dir() / "models", allow_download=True
    ).load()
    print("noblivion install: embedding model ready")
else:
    print(f"noblivion install: embedding.backend is {s.backend}: no model to download")
PY
    then
        say "the model download failed (keyword search only)"
        backend_none "the model download failed" \
            || say "could not change embedding.backend in $DATA_DIR/config.json"
    fi
fi

# 5. The token (mode 0600), unless one exists. The store also makes it.
if [ ! -e "$DATA_DIR/token" ]; then
    if [ "$DRY_RUN" = 1 ]; then
        say "would write $DATA_DIR/token"
    else
        (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' >"$DATA_DIR/token")
    fi
fi

# 6. The install stamp: the SessionStart hook compares its version with the
#    plugin's and asks for a new run of this script after a plugin update.
if [ "$DRY_RUN" = 1 ]; then
    say "would write $VENV/noblivion-install.json"
else
    python3 -c 'import json, sys
json.dump({"version": sys.argv[1], "plugin_root": sys.argv[2]}, open(sys.argv[3], "w"))' \
        "$VERSION" "$ROOT" "$VENV/noblivion-install.json"
fi

# 7. A first index of the memory files, then the check for old hand-installed
#    hooks (a dry run: it only prints what --apply would remove).
run env NOBLIVION_DATA_DIR="$DATA_DIR" CLAUDE_PLUGIN_ROOT="$ROOT" "$VENV/bin/noblivion" index
run env NOBLIVION_DATA_DIR="$DATA_DIR" "$VENV/bin/noblivion" migrate-from-legacy

# 8. Start the store now (design doc section 3.2, ensure-running), so the
#    session that ran this script has memory recall from its next prompt
#    (NOBLIVION-29). A running store is replaced: it reads the config and
#    loads the model only at its start (NOBLIVION-57). The store runs
#    detached; a failure is not fatal.
if [ "$START" = 0 ]; then
    say "done. The store starts at the next Claude Code session."
elif [ "$DRY_RUN" = 1 ]; then
    run env NOBLIVION_DATA_DIR="$DATA_DIR" CLAUDE_PLUGIN_ROOT="$ROOT" "$VENV/bin/noblivion" ensure-running --restart
else
    # The restart waits up to 10 s for the new store and up to 90 s for an
    # old store to stop, with no output.
    say "starting the store; when an old store runs, it is stopped first (up to 100 s)"
    if env NOBLIVION_DATA_DIR="$DATA_DIR" CLAUDE_PLUGIN_ROOT="$ROOT" "$VENV/bin/noblivion" ensure-running --restart; then
        say "done. The store runs. Memory recall works from the next prompt in this session."
    else
        say "done, but the store did not start (reason above). The next Claude Code session tries again."
    fi
fi
