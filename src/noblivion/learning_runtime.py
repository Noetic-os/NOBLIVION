# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store integration for the opt-in background learner.

Configuration is separate from the permissive legacy settings parser: a
malformed learning configuration stops learning rather than enabling defaults.
Only an explicit project/root mapping can receive source batches or files.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from noblivion.learning import LearningLedger

log = logging.getLogger("noblivion.learning")


def load_settings(data_dir: Path) -> dict:
    path = data_dir / "learning.json"
    if not path.exists():
        return {"enabled": False, "scopes": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("invalid learning settings")
    scopes = value.get("scopes", [])
    if not isinstance(scopes, list):
        raise ValueError("learning scopes must be a list")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            raise ValueError("invalid learning scope")
        project, root = scope.get("project"), scope.get("root")
        if not isinstance(project, str) or not project or not isinstance(root, str) or not root:
            raise ValueError("learning scopes require project and root")
        folder = Path(scope.get("memory_dir", "")).expanduser()
        if not folder.is_absolute() or folder.parent.name != root:
            raise ValueError("learning memory_dir must be absolute and match root")
        if (project, root) in seen:
            raise ValueError("duplicate learning scope")
        seen.add((project, root))
    value["scopes"] = scopes
    prune_options(value)
    return value


def permitted(settings: dict, project: str, root: str) -> bool:
    return settings.get("enabled") is True and any(
        s["project"] == project and s["root"] == root for s in settings["scopes"]
    )


def capture(store, payload: dict) -> dict:
    settings = load_settings(store.data_dir)
    project = payload.get("project", payload.get("namespace", store.namespace))
    root = payload.get("root", "")
    if project != store.namespace or not permitted(settings, project, root):
        raise ValueError("learning is disabled for this scope")
    with store.connection() as conn:
        return LearningLedger(conn).capture_batch(dict(payload, project=project))


def memory_dirs(data_dir: Path, namespace: str) -> list[Path]:
    try:
        settings = load_settings(data_dir)
    except (ValueError, OSError, TypeError):
        log.error("invalid learning settings; scanning legacy folders only")
        return []
    # Continue indexing accepted notes after contribution is disabled.
    return [
        Path(s["memory_dir"]).expanduser() for s in settings["scopes"] if s["project"] == namespace
    ]


def use_allowed(conn, project: str, root: str, session_id: str = "") -> bool:
    return LearningLedger(conn).read_allowed(project, root, session_id)


def summary(store, project: str, root: str, session_id: str = "") -> dict:
    with store.connection() as conn:
        if not use_allowed(conn, project, root, session_id):
            return {"summary": "", "reason": "memory use disabled"}
        ledger = LearningLedger(conn)
        return {
            "summary": ledger.summary(project, root, session_id=session_id),
            "reason": None,
            # What a prompt context could draw on. 0 lets the prompt hook skip its call.
            "context_items": ledger.context_items(project, root, session_id),
        }


def controls(store, body: dict) -> dict:
    project = body.get("project", store.namespace)
    root, session_id = body.get("root", ""), body.get("session_id", "")
    if not all(isinstance(v, str) and len(v) <= 512 for v in (project, root, session_id)):
        raise ValueError("invalid control scope")
    flags = {k: body[k] for k in ("use", "contribute", "paused") if k in body}
    if not flags or any(type(v) is not bool for v in flags.values()):
        raise ValueError("controls must be booleans")
    with store.connection() as conn:
        ledger = LearningLedger(conn)
        ledger.set_controls(project, root, session_id, **flags)
        return ledger.effective_controls(project, root, session_id)


def project_and_index(store, ledger: LearningLedger, settings: dict) -> None:
    """Replay file effects before indexing, then acknowledge verified hashes."""
    for scope in settings["scopes"]:
        ledger.project_pending(
            scope["project"], scope["root"], Path(scope["memory_dir"]).expanduser()
        )
    # Index under the store's existing lock; an outage leaves the outbox
    # written, not acknowledged, so a later pass can complete it.
    with store.connection() as conn:
        result = store.scan_once(conn)
    if result is not None:
        for scope in settings["scopes"]:
            ledger.acknowledge_index(scope["project"], scope["root"])


PRUNE_INTERVAL = 3600.0


def prune_options(settings: dict) -> dict | None:
    """The ``prune`` block of learning.json, or None when automatic pruning is off.

    Pruning deletes data, so it is opt in: ``"prune": {"enabled": true}``, with optional
    ``pending_days`` and ``retention_days`` (14 each). ``noblivion learning prune`` runs it by hand.
    """
    block = settings.get("prune")
    if block is None:
        return None
    if (
        not isinstance(block, dict)
        or set(block) - {"enabled", "pending_days", "retention_days"}
        or type(block.get("enabled", False)) is not bool
    ):
        raise ValueError("invalid learning prune settings")
    if block.get("enabled", False) is not True:
        return None
    return {key: value for key, value in block.items() if key != "enabled"}


def run(store) -> None:
    """Dedicated bounded worker. A bad pass never kills the retry loop."""
    last_prune = float("-inf")
    while not store._jobs_stop.is_set():
        try:
            settings = load_settings(store.data_dir)
            if not settings["scopes"]:
                store._jobs_stop.wait(5.0)
                continue
            with store.connection() as conn:
                ledger = LearningLedger(conn)
                options = prune_options(settings)
                if options is not None and time.monotonic() - last_prune >= PRUNE_INTERVAL:
                    last_prune = time.monotonic()
                    counts = ledger.prune(**options)
                    if any(counts.values()):
                        log.info("learning prune: %s", json.dumps(counts, sort_keys=True))
                if settings.get("enabled") is True:
                    run_once(ledger, settings)
                project_and_index(store, ledger, settings)
        except Exception as exc:  # noqa: BLE001 - pending work survives this pass
            log.error("learning pass failed: %s", type(exc).__name__)
        store._jobs_stop.wait(5.0)


def run_once(ledger: LearningLedger, settings: dict):
    """Construct an explicitly configured local worker; no provider fallback."""
    from noblivion.learning import BudgetLimits
    from noblivion.learning_worker import LearningWorker, OpenAICompatibleProvider, WorkerSettings

    provider = OpenAICompatibleProvider(**settings["provider"])
    worker = LearningWorker(
        ledger,
        provider,
        WorkerSettings(**settings.get("worker", {})),
        BudgetLimits(**settings.get("budget", {})),
        allowed_scopes={(s["project"], s["root"]) for s in settings["scopes"]},
    )
    return worker.run_once()


def context(store, body: dict) -> dict:
    """Current, bounded source-labelled context for one caller and project root."""
    from noblivion.learning_consolidation import ConsolidationLedger

    project = body.get("project", body.get("namespace", store.namespace))
    root = body.get("root", "")
    query = body.get("query", body.get("q", ""))
    session = body.get("session_id", "")
    if project != store.namespace:
        raise ValueError("invalid context namespace")
    with store.connection() as conn:
        return ConsolidationLedger(LearningLedger(conn)).context(
            project, root, query, session_id=session, limit_chars=body.get("limit_chars", 6000)
        )
