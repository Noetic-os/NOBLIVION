# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, redacted diagnostics. This module never repairs or starts a store.

Exit codes: 0 means no findings, 1 means a warning/failure, and 2 means an
unavailable check (or invalid arguments). An unavailable check takes precedence.
SQLite checks deliberately decline an active WAL rather than create shared files
or report an immutable read of an obsolete database as current.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit

from noblivion import config, db, launcher

MAX_FILE_BYTES = 1024 * 1024
MAX_ITEMS = 256
STATUSES = frozenset({"pass", "warn", "fail", "unavailable", "not_configured"})
CHECK_KEYS = (
    "data_dir",
    "database_mode",
    "token_mode",
    "wal_mode",
    "shm_mode",
    "venv",
    "install_stamp",
    "model_cache",
    "health",
    "database_schema",
    "database_foreign_keys",
    "filesystem",
    "config_syntax",
    "config_unknown_keys",
    "config_values",
    "environment_values",
    "environment_precedence",
    "embedding_url",
    "memory_folders",
    "current_root",
    "codex_hooks",
    "codex_config",
)
# Configuration is permissive at runtime. Diagnostics flag ignored values without
# printing keys supplied by a user, their values, commands, paths, or URLs.
_LIST_KEYS = frozenset(
    "memory_dirs labels.ticket_prefixes labels.service_prefixes "
    "recall.shared_roots guard.generic_args_extra guard.evidence_stop_extra "
    "continuity.decision_files continuity.open_files stop.shared_checkouts "
    "stop.worktree_prefixes stop.deploy_hosts".split()
)
_TEXT_KEYS = frozenset(
    "namespace global_memory_dir store.bin embedding.model "
    "embedding.ollama_url sync.index_command stop.test_command dedup.model "
    "miner.transcript_glob".split()
)
_INT_KEYS = frozenset(
    "port idle_exit_s index.interval_s index.delete_grace_days "
    "dedup.archive_retention_days recall.index_k dedup.max_pairs "
    "recall.prompt_rows recall.hub_days recall.hub_rows".split()
)
_FLOAT_KEYS = frozenset(
    "recall.timeout_s recall.min_score trust.prior_mined dedup.min_cosine "
    "dedup.daily_cost_cap_usd dedup.timeout_s dedup.min_interval_s dedup.price_in_per_mtok "
    "dedup.price_out_per_mtok miner.max_run_s".split()
)
_SWITCH_KEYS = frozenset(
    "embedding.allow_download embedding.allow_remote "
    "embedding.remote_include_mined trust.events trust.citation_use "
    "trust.correction_contradict trust.behaviour_use recall.rules_only miner.enabled "
    "guard.rewrite guard.ask guard.act_shadow".split()
)
_CHOICES = {
    "embedding.backend": ("fastembed", "ollama", "openrouter", "none"),
    "trust.ranking": config.TRUST_RANKING_MODES,
    "log_level": config.LOG_LEVELS,
    "stop.mode": ("shadow", "enforce"),
    "dedup.judge": ("off", "openrouter"),
    "guard.rewrite_decision": ("ask", "allow"),
}
KNOWN_KEYS = (
    _LIST_KEYS
    | _TEXT_KEYS
    | _INT_KEYS
    | _FLOAT_KEYS
    | _SWITCH_KEYS
    | _CHOICES.keys()
    | {
        "recall.env",
        "stop.checks",
    }
)
ENV_KEYS = {
    "NOBLIVION_PORT": "port",
    "NOBLIVION_IDLE_EXIT_S": "idle_exit_s",
    "NOBLIVION_INDEX_INTERVAL_S": "index.interval_s",
    "NOBLIVION_LOG_LEVEL": "log_level",
    "NOBLIVION_PROJECT": "namespace",
    "NOBLIVION_RECALL_PROJECT": "namespace",
    "NOBLIVION_EMBED_BACKEND": "embedding.backend",
    "NOBLIVION_EMBED_MODEL": "embedding.model",
    "NOBLIVION_OLLAMA_URL": "embedding.ollama_url",
    "NOBLIVION_TRUST_RANKING": "trust.ranking",
    "NOBLIVION_GLOBAL_MEMORY_DIR": "global_memory_dir",
    "NOBLIVION_BIN": "store.bin",
    "NOBLIVION_RECALL_TIMEOUT_S": "recall.timeout_s",
    "NOBLIVION_RECALL_MIN_SCORE": "recall.min_score",
    "NOBLIVION_RECALL_INDEX_K": "recall.index_k",
    "NOBLIVION_RECALL_RULES_ONLY": "recall.rules_only",
    "NOBLIVION_RECALL_PROMPT_ROWS": "recall.prompt_rows",
    "NOBLIVION_RECALL_HUB_DAYS": "recall.hub_days",
    "NOBLIVION_RECALL_HUB_ROWS": "recall.hub_rows",
    "NOBLIVION_DEDUP_JUDGE": "dedup.judge",
    "NOBLIVION_DEDUP_MODEL": "dedup.model",
    "NOBLIVION_STOP_CHECK_MODE": "stop.mode",
    "NOBLIVION_MEMORY_SYNC_CMD": "sync.index_command",
    "NOBLIVION_MINER": "miner.enabled",
    "NOBLIVION_TRUST_EVENTS": "trust.events",
    "NOBLIVION_TRUST_CITATION_USE": "trust.citation_use",
    "NOBLIVION_TRUST_CORRECTION_CONTRADICT": "trust.correction_contradict",
    "NOBLIVION_TRUST_BEHAVIOUR_USE": "trust.behaviour_use",
    "NOBLIVION_GUARD_REWRITE": "guard.rewrite",
    "NOBLIVION_GUARD_ASK": "guard.ask",
    "NOBLIVION_GUARD_ACT_SHADOW": "guard.act_shadow",
    "NOBLIVION_GUARD_REWRITE_DECISION": "guard.rewrite_decision",
}


# Expected metadata from the packaged schema. A regression test detects drift.
_SCHEMA_MANIFEST_VERSION = 6
_REQUIRED_COLUMNS = {
    "chunk_vectors": frozenset("chunk_id model dim content_hash blob rev".split()),
    "dedup_actions": frozenset(
        (
            "id run_id root kept_id archived_id kept_path archived_path archive_to status "
            "judge_model verdict created_at undone_at"
        ).split()
    ),
    "dedup_vetoes": frozenset("root path_a path_b created_at".split()),
    "feedback": frozenset(
        (
            "memory_id trust_0 trials use_pos contradiction_count trust_score "
            "last_recalled_at last_used_at folded_event_id"
        ).split()
    ),
    "feedback_events": frozenset(
        "id session_id memory_id kind citation_capable ts received_at".split()
    ),
    "learning_batches": frozenset(
        "batch_id payload_hash project root session_id skipped created_at".split()
    ),
    "learning_budget": frozenset(
        "id job_id lease_token tokens seconds cost status created_at updated_at".split()
    ),
    "learning_claim_edges": frozenset("parent_id parent_revision child_id relation".split()),
    "learning_claim_revisions": frozenset("claim_id revision action snapshot created_at".split()),
    "learning_claim_supports": frozenset(
        (
            "claim_id claim_revision support_id note_id note_revision source_id "
            "source_hash start end quote"
        ).split()
    ),
    "learning_claims": frozenset(
        (
            "claim_id project root revision statement kind category scope status "
            "observed_at effective_at job_id config_fingerprint semantic_relation "
            "created_at updated_at"
        ).split()
    ),
    "learning_context_cache": frozenset(
        "cache_id project root session_id payload updated_at".split()
    ),
    "learning_controls": frozenset(
        "project root session_id use_memory contribute paused updated_at".split()
    ),
    "learning_cursors": frozenset(
        "project root client session_id path_id offset updated_at".split()
    ),
    "learning_decisions": frozenset("id job_id note_id action detail created_at".split()),
    "learning_draft_supports": frozenset(
        "draft_id support_id note_id note_revision source_id source_hash start end quote".split()
    ),
    "learning_drafts": frozenset(
        "draft_id project root status active document qualification created_at updated_at".split()
    ),
    "learning_evidence": frozenset(
        "note_id revision source_id source_hash start end quote".split()
    ),
    "learning_extraction_pages": frozenset(
        (
            "job_id unit_index continuation fingerprint spans candidates status reason created_at"
        ).split()
    ),
    "learning_job_sources": frozenset("job_id source_id ordinal".split()),
    "learning_jobs": frozenset(
        (
            "id batch_id project root session_id status attempts lease_token lease_owner "
            "lease_until next_attempt last_error progress created_at updated_at kind"
        ).split()
    ),
    "learning_notes": frozenset(
        (
            "note_id project root logical_key path revision status scope category title "
            "statement observed_at effective_at file_hash created_at updated_at"
        ).split()
    ),
    "learning_outbox": frozenset(
        "note_id revision operation status attempts last_error updated_at".split()
    ),
    "learning_revisions": frozenset("note_id revision action snapshot created_at".split()),
    "learning_sources": frozenset(
        (
            "id project root session_id source_client role content sha256 observed_at "
            "metadata status created_at"
        ).split()
    ),
    "memories": frozenset(
        (
            "id project root path source_type category content hash weight pinned labels "
            "archived_at deleted_at rev created_at updated_at index_text"
        ).split()
    ),
    "memory_chunks": frozenset("id parent_id chunk_index heading text search_text rev".split()),
    "meta": frozenset("key value".split()),
    "miner_state": frozenset("transcript size mtime_ns offset mined_at".split()),
    "vectors": frozenset("memory_id model dim content_hash blob rev".split()),
}
_REQUIRED_INDEXES = {
    "chunk_vectors_rev": "chunk_vectors",
    "feedback_events_memory": "feedback_events",
    "feedback_events_ts": "feedback_events",
    "learning_budget_window": "learning_budget",
    "learning_claim_support_note": "learning_claim_supports",
    "learning_claim_support_source": "learning_claim_supports",
    "learning_claims_scope": "learning_claims",
    "learning_draft_support_note": "learning_draft_supports",
    "learning_evidence_source": "learning_evidence",
    "learning_extraction_pages_status": "learning_extraction_pages",
    "learning_jobs_due": "learning_jobs",
    "learning_notes_current": "learning_notes",
    "learning_sources_revoked_hash": "learning_sources",
    "learning_sources_scope": "learning_sources",
    "memories_hash": "memories",
    "memories_live": "memories",
    "memories_rev": "memories",
    "memory_chunks_rev": "memory_chunks",
    "vectors_rev": "vectors",
}


def read_bytes(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Do not follow links or block on a FIFO; bound even a growing file."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            value = handle.read(limit + 1)
        if len(value) > limit:
            raise ValueError("file exceeds diagnostic limit")
        return value
    finally:
        os.close(fd)


def _json_file(path: Path, limit: int = MAX_FILE_BYTES) -> dict:
    def reject_constant(value):
        raise ValueError("non-finite JSON number")

    value = json.loads(read_bytes(path, limit), parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _leaves(value: dict, prefix: str = "", depth: int = 0) -> dict:
    if depth > 16:
        raise ValueError("configuration nesting limit")
    found = {}
    for key, item in value.items():
        name = prefix + key
        if isinstance(item, dict) and name not in KNOWN_KEYS and item:
            found.update(_leaves(item, name + ".", depth + 1))
        else:
            found[name] = item
        if len(found) > MAX_ITEMS:
            raise ValueError("configuration item limit")
    return found


def _valid(key: str, value: object) -> bool:
    if key in _LIST_KEYS:
        return isinstance(value, list) and all(isinstance(p, str) for p in value)
    if key in _TEXT_KEYS:
        return isinstance(value, str) and bool(value.strip()) and "\0" not in value
    if key in _CHOICES:
        return isinstance(value, str) and value.strip().lower() in _CHOICES[key]
    if key in _SWITCH_KEYS:
        return isinstance(value, (bool, int, float)) or (
            isinstance(value, str) and value.strip().lower() in config.SWITCH_ON + config.SWITCH_OFF
        )
    if key in _INT_KEYS | _FLOAT_KEYS:
        if isinstance(value, bool):
            return False
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(number):
            return False
        if key in _INT_KEYS:
            try:
                int(value)
            except (TypeError, ValueError, OverflowError):
                return False
            if not number.is_integer() or number < 0:
                return False
        if key == "port":
            return number <= 65535
        if key == "recall.index_k":
            return 1 <= number <= 100
        if key in {"recall.prompt_rows", "recall.hub_rows"}:
            return number <= 100
        if key == "recall.hub_days":
            return number <= 30
        if key == "recall.min_score":
            return -1 <= number <= 1
        if key == "dedup.min_cosine":
            return 0 <= number <= 1
        if key == "dedup.timeout_s":
            return number >= 1
        if key == "miner.max_run_s":
            return number > 0
        if key == "trust.prior_mined":
            return 0 < number <= 1
        return number >= 0
    if key == "recall.env":
        return isinstance(value, dict) and all(
            k.startswith("NOBLIVION_RECALL_") and isinstance(v, (str, int, float, bool))
            for k, v in value.items()
        )
    if key == "stop.checks":
        return isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(p, str) for p in value)
        )
    return True


def _mode(path: Path, *, directory: bool = False, optional: bool = False) -> tuple[str, str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return (
            ("not_configured", "Absent optional file.")
            if optional
            else ("fail", "Missing required path.")
        )
    except OSError:
        return "unavailable", "Cannot inspect path."
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        return "fail", "Expected path type or private ownership is invalid."
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        return "fail", "Path is not private to the current user."
    return "pass", "Private path is present."


def _presence(path: Path, *, directory: bool = False) -> tuple[str, str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "warn", "Required installation component is absent."
    except OSError:
        return "unavailable", "Cannot inspect installation component."
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        ("pass", "Installation component is present.")
        if expected(info.st_mode)
        else ("fail", "Installation component has an unexpected type.")
    )


def _filesystem(path: Path, timeout: float) -> tuple[str, str]:
    try:
        resolved = str(path.resolve())
        if sys.platform.startswith("linux"):
            rows = read_bytes(Path("/proc/self/mounts")).decode("utf-8", "replace").splitlines()
            mounts = []
            for row in rows:
                parts = row.split()
                if len(parts) >= 3:
                    mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), parts[1])
                    if resolved == mount or resolved.startswith(mount.rstrip("/") + "/"):
                        mounts.append((len(mount), parts[2]))
            if not mounts:
                return "unavailable", "Cannot identify filesystem."
            kind = max(mounts)[1]
        elif sys.platform == "darwin":
            # Never buffer an unbounded mount listing or wait without a deadline.
            proc = subprocess.Popen(
                ["/sbin/mount"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            chunks = []
            reader = threading.Thread(
                target=lambda: chunks.append(proc.stdout.read(MAX_FILE_BYTES + 1)), daemon=True
            )
            reader.start()
            reader.join(timeout)
            if reader.is_alive() or (chunks and len(chunks[0]) > MAX_FILE_BYTES):
                proc.kill()
                proc.wait(timeout=timeout)
                reader.join(timeout)
                proc.stdout.close()
                return "unavailable", "Filesystem inspection exceeded diagnostic limits."
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
                return "unavailable", "Filesystem inspection exceeded diagnostic limits."
            finally:
                proc.stdout.close()
            output = chunks[0] if chunks else b""
            if proc.returncode != 0:
                return "unavailable", "Cannot identify filesystem."
            matches = []
            for row in output.decode("utf-8", "replace").splitlines():
                match = re.search(r" on (.+) \(([^, )]+)", row)
                if match and (
                    resolved == match[1] or resolved.startswith(match[1].rstrip("/") + "/")
                ):
                    matches.append((len(match[1]), match[2]))
            if not matches:
                return "unavailable", "Cannot identify filesystem."
            kind = max(matches)[1]
        else:
            return "unavailable", "Filesystem inspection is unsupported."
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "unavailable", "Cannot identify filesystem."
    if kind.lower() in {"nfs", "nfs4", "cifs", "smbfs", "smb3"}:
        return "warn", "Network filesystem may not support SQLite locking safely."
    return "pass", "No network filesystem was detected."


def _schema_present(conn: sqlite3.Connection) -> bool:
    objects = conn.execute(
        "SELECT type,name,tbl_name FROM sqlite_schema WHERE type IN (?,?) LIMIT ?",
        ("table", "index", MAX_ITEMS + 1),
    ).fetchall()
    if len(objects) > MAX_ITEMS:
        raise ValueError("schema object limit")
    tables = {name for kind, name, _ in objects if kind == "table"}
    indexes = {name: table for kind, name, table in objects if kind == "index"}
    if not _REQUIRED_COLUMNS.keys() <= tables:
        return False
    if any(indexes.get(name) != table for name, table in _REQUIRED_INDEXES.items()):
        return False
    for table, required in _REQUIRED_COLUMNS.items():
        rows = conn.execute(
            "SELECT name FROM pragma_table_info(?) LIMIT ?",
            (table, MAX_ITEMS + 1),
        ).fetchall()
        if len(rows) > MAX_ITEMS:
            raise ValueError("schema column limit")
        if not required <= {row[0] for row in rows}:
            return False
    return True


def _database(path: Path, timeout: float) -> tuple[tuple[str, str], tuple[str, str]]:
    unavailable = ("unavailable", "Database inspection is unavailable.")
    if not path.is_file() or path.is_symlink():
        return unavailable, unavailable
    try:
        wal = path.with_name(path.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            return (("unavailable", "Active WAL prevents a side-effect-free database check."),) * 2
        # immutable=1 prevents SQLite creating WAL/SHM even on a read-only URI.
        journal = path.with_name(path.name + "-journal")
        if journal.exists() and journal.stat().st_size:
            return (
                ("unavailable", "Rollback journal prevents a side-effect-free database check."),
            ) * 2
        before = path.stat()
        uri = "file:" + quote(str(path.resolve())) + "?mode=ro&immutable=1"
        deadline = time.monotonic() + timeout
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        try:
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            latest = db.latest_version()
            schema = (
                ("pass", "Database schema matches this installation.")
                if version == latest == _SCHEMA_MANIFEST_VERSION and _schema_present(conn)
                else ("fail", "Database schema differs from this installation.")
            )
            try:
                violation = conn.execute("PRAGMA foreign_key_check").fetchone()
                foreign = (
                    ("pass", "No foreign key violation was found.")
                    if violation is None
                    else ("fail", "Database has a foreign key violation.")
                )
            except sqlite3.Error:
                foreign = unavailable
            after = path.stat()
            if (
                (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)
                or (wal.exists() and wal.stat().st_size)
                or (journal.exists() and journal.stat().st_size)
            ):
                return (("unavailable", "Database changed during read-only inspection."),) * 2
            return schema, foreign
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error):
        return unavailable, unavailable


def _health(folder: Path, timeout: float) -> tuple[str, str]:
    try:
        info = _json_file(folder / config.STORE_JSON_FILE, 4096)
        token = read_bytes(folder / config.TOKEN_FILE, 256).decode("ascii").strip()
        port = info.get("port")
        if not launcher.TOKEN_RE.fullmatch(token) or type(port) is not int or not 0 < port < 65536:
            return "fail", "Store discovery or token is malformed."
    except FileNotFoundError:
        return "warn", "Store discovery or token is absent; store was not started."
    except (OSError, ValueError, UnicodeError):
        return "fail", "Store discovery or token cannot be read safely."
    result = []

    def check():
        try:
            result.append(launcher.probe(port, token, timeout=timeout))
        except Exception:
            result.append(None)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout)
    if not worker.is_alive() and result and result[0] is not None:
        return "pass", "Store proved possession of its token."
    return "warn", "Store health could not be authenticated; store was not started."


def _codex(env: Mapping[str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        doc = _json_file(home / "hooks.json")
        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            raise ValueError("invalid hooks")
        events = (
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "SubagentStart",
            "Stop",
            "SessionEnd",
        )
        present = set()
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                raise ValueError("invalid groups")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError("invalid group")
                for hook in group["hooks"]:
                    if not isinstance(hook, dict):
                        raise ValueError("invalid hook")
                    command = hook.get("command", "")
                    if isinstance(command, str) and (
                        "/codex/adapter.py" in command or "/codex/remote_bridge.py" in command
                    ):
                        present.add(event)
        hook_result = (
            ("pass", "Required Codex memory hook entries are present.")
            if all(e in present for e in events)
            else ("warn", "Required Codex memory hook entries are incomplete.")
        )
    except FileNotFoundError:
        hook_result = "not_configured", "Codex hook file is absent."
    except (OSError, ValueError):
        hook_result = "fail", "Codex hook file cannot be read safely or has invalid structure."
    try:
        text = read_bytes(home / "config.toml").decode("utf-8")
        # Python 3.10 has no stdlib TOML parser. Do not mistake readability for
        # syntax validation; this check reports only readability and integration.
        integration = "[mcp_servers.noblivion]" in text or '[mcp_servers."noblivion"]' in text
        cfg_result = (
            ("pass", "Codex configuration is readable and names the memory server.")
            if integration
            else ("not_configured", "Codex configuration does not name the memory server.")
        )
    except FileNotFoundError:
        cfg_result = "not_configured", "Codex configuration file is absent."
    except (OSError, ValueError):
        cfg_result = "fail", "Codex configuration cannot be read safely."
    return hook_result, cfg_result


def _codex_scope(env: Mapping[str, str]) -> tuple[str | None, Path | None, str]:
    """Match the adapter's longest explicit workspace prefix, without executing it."""
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        cfg = _json_file(home / "noblivion" / "config.json")
        mappings = cfg.get("project_mappings", [])
        if not isinstance(mappings, list) or len(mappings) > MAX_ITEMS:
            raise ValueError("mapping limit or shape")
        cwd = os.path.normpath(str(Path.cwd()))
        selected = None
        longest = -1
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            workspace = mapping.get("workspace")
            root = mapping.get("root")
            namespace = mapping.get("namespace")
            folder = mapping.get("memory_dir")
            if not all(
                isinstance(v, str) and v and "\0" not in v
                for v in (workspace, root, namespace, folder)
            ):
                continue
            workspace = os.path.normpath(workspace)
            if not os.path.isabs(workspace) or not os.path.isabs(folder):
                continue
            if (cwd == workspace or cwd.startswith(workspace.rstrip(os.sep) + os.sep)) and len(
                workspace
            ) > longest:
                selected = (root, Path(folder), "pass")
                longest = len(workspace)
        return selected or (None, None, "not_configured")
    except FileNotFoundError:
        return None, None, "not_configured"
    except (OSError, ValueError, RecursionError):
        return None, None, "fail"


def diagnose(
    *,
    data_dir: Path | None = None,
    root: str | None = None,
    timeout: float = 1.0,
    env: Mapping[str, str] | None = None,
) -> dict:
    if not math.isfinite(timeout) or not 0.05 <= timeout <= 2:
        raise ValueError("timeout outside diagnostic bounds")
    env = dict(os.environ if env is None else env)
    folder = Path(data_dir).expanduser() if data_dir is not None else config.data_dir(env)
    checks = {}

    def put(key: str, result: tuple[str, str]):
        checks[key] = {"status": result[0], "message": result[1]}

    put("data_dir", _mode(folder, directory=True))
    for key, filename, optional in (
        ("database_mode", config.DB_FILE, False),
        ("token_mode", config.TOKEN_FILE, False),
        ("wal_mode", config.DB_FILE + "-wal", True),
        ("shm_mode", config.DB_FILE + "-shm", True),
    ):
        put(key, _mode(folder / filename, optional=optional))
    put("venv", _presence(folder / "venv", directory=True))
    put("install_stamp", _presence(config.install_stamp(folder)))
    put("filesystem", _filesystem(folder, timeout))
    cfg_path = (
        Path(env["NOBLIVION_CONFIG"]).expanduser()
        if env.get("NOBLIVION_CONFIG", "").strip()
        else folder / "config.json"
    )
    cfg = {}
    try:
        cfg = _json_file(cfg_path)
        leaves = _leaves(cfg)
        put(
            "config_syntax",
            ("pass", "Configuration is a valid JSON object within diagnostic limits."),
        )
    except FileNotFoundError:
        leaves = {}
        put("config_syntax", ("not_configured", "Configuration file is absent; defaults apply."))
    except (OSError, ValueError, RecursionError):
        leaves = {}
        cfg = {}
        put("config_syntax", ("fail", "Configuration cannot be read safely or is invalid."))
    unknown = bool(leaves.keys() - KNOWN_KEYS)
    put(
        "config_unknown_keys",
        ("warn", "Configuration contains ignored unknown keys.")
        if unknown
        else ("pass", "No unknown configuration keys were found."),
    )
    invalid = any(not _valid(k, v) for k, v in leaves.items() if k in KNOWN_KEYS)
    put(
        "config_values",
        ("fail", "Configuration contains invalid values that may fall back to defaults.")
        if invalid
        else ("pass", "Checked configuration values are valid."),
    )
    invalid_env = any(not _valid(k, env[e]) for e, k in ENV_KEYS.items() if env.get(e, "").strip())
    put(
        "environment_values",
        ("fail", "Environment contains invalid setting values.")
        if invalid_env
        else ("pass", "Checked environment setting values are valid."),
    )
    overrides = any(
        e in env and k in leaves and (bool(env[e].strip()) or k in _SWITCH_KEYS)
        for e, k in ENV_KEYS.items()
    )
    put(
        "environment_precedence",
        ("pass", "Environment settings override configuration values.")
        if overrides
        else ("pass", "No environment override of a configuration value was found."),
    )
    backend = config._choice(
        config._env_or_cfg(env, cfg, "NOBLIVION_EMBED_BACKEND", "embedding.backend"),
        _CHOICES["embedding.backend"],
        "fastembed",
    )
    put(
        "model_cache",
        _presence(folder / "models", directory=True)
        if backend == "fastembed"
        else ("not_configured", "Selected embedding backend does not use the local model cache."),
    )
    url = (
        config._env_or_cfg(env, cfg, "NOBLIVION_OLLAMA_URL", "embedding.ollama_url")
        or "http://127.0.0.1:11434"
    )
    url_result = "pass", "Embedding endpoint is a literal loopback HTTP endpoint."
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsafe endpoint")
        _ = parsed.port
        if parsed.hostname.lower() == "localhost":
            url_result = (
                "warn",
                "Localhost may select an unsupported listener; use a literal loopback address.",
            )
        elif not ipaddress.ip_address(parsed.hostname).is_loopback:
            url_result = (
                "warn",
                "Embedding endpoint is remote; check remote permission and consent before use.",
            )
    except (ValueError, TypeError):
        url_result = "fail", "Embedding endpoint is unsafe or is not a literal IP address."
    put(
        "embedding_url",
        url_result
        if backend == "ollama"
        else ("not_configured", "Ollama embedding endpoint is not selected."),
    )
    raw_dirs = env.get("NOBLIVION_MEMORY_DIRS", "").strip()
    raw_cfg_dirs = config.lookup(cfg, "memory_dirs")
    dirs = (
        raw_dirs.split(":", MAX_ITEMS)
        if raw_dirs
        else raw_cfg_dirs[: MAX_ITEMS + 1]
        if isinstance(raw_cfg_dirs, list)
        else ()
    )
    paths = [Path(p).expanduser() for p in dirs if isinstance(p, str) and p.strip()]
    for key in ("NOBLIVION_MEMORY_DIR", "NOBLIVION_RECALL_MEMORY_DIR"):
        if env.get(key, "").strip():
            paths.append(Path(env[key]).expanduser())
    extra = config.global_memory_dir(env, cfg)
    if extra is not None:
        paths.append(extra)
    current = root or env.get("NOBLIVION_RECALL_ROOT")
    scoped = env.get("NOBLIVION_RECALL_MEMORY_DIR") or env.get("NOBLIVION_MEMORY_DIR")
    if not current and scoped:
        current = Path(scoped).expanduser().parent.name
    mapping_status = "not_configured"
    if not current:
        current, mapped_folder, mapping_status = _codex_scope(env)
        if mapped_folder is not None:
            paths.append(mapped_folder)
    missing = len(paths) > MAX_ITEMS or any(not p.is_dir() for p in paths)
    put(
        "memory_folders",
        ("warn", "Configured memory folders are missing or exceed diagnostic limits.")
        if missing
        else (
            ("pass", "Configured memory folders exist.")
            if paths
            else (
                "not_configured",
                "No explicit memory folders are configured; automatic discovery is not scanned.",
            )
        ),
    )
    matched = any(p.parent.name == current and p.is_dir() for p in paths) if current else False
    root_result = (
        ("pass", "Current root has a configured memory folder.")
        if matched
        else (
            ("warn", "Current root has no existing configured memory folder.")
            if current
            else ("not_configured", "Current root cannot be derived from explicit settings.")
        )
    )
    if not current and mapping_status == "fail":
        root_result = (
            "fail",
            "Codex project mappings cannot be read safely or exceed diagnostic limits.",
        )
    put("current_root", root_result)
    put("health", _health(folder, timeout))
    schema, foreign = _database(folder / config.DB_FILE, timeout)
    put("database_schema", schema)
    put("database_foreign_keys", foreign)
    hooks, codex_cfg = _codex(env)
    put("codex_hooks", hooks)
    put("codex_config", codex_cfg)
    checks = {k: checks[k] for k in CHECK_KEYS}
    statuses = {c["status"] for c in checks.values()}
    code = 2 if "unavailable" in statuses else 1 if statuses & {"warn", "fail"} else 0
    return {
        "schema_version": 1,
        "status": "unavailable" if code == 2 else "findings" if code else "healthy",
        "exit_code": code,
        "checks": checks,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "doctor: invalid arguments; use --help.\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(prog="noblivion doctor", description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--root")
    parser.add_argument(
        "--timeout", type=float, default=1.0, help="Per-check limit in seconds (0.05 to 2)."
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0.05 <= args.timeout <= 2:
        parser.error("timeout outside range")
    try:
        report = diagnose(data_dir=args.data_dir, root=args.root, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError):
        report = {
            "schema_version": 1,
            "status": "unavailable",
            "exit_code": 2,
            "checks": {
                k: {"status": "unavailable", "message": "Diagnostic check is unavailable."}
                for k in CHECK_KEYS
            },
        }
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for key, item in report["checks"].items():
            print(f"{key}: {item['status']}: {item['message']}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
