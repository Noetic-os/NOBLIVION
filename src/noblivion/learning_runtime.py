# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store integration for the opt-in background learner.

Configuration is separate from the permissive legacy settings parser: a
malformed learning configuration stops learning rather than enabling defaults.
Only an explicit project/root mapping can receive source batches or files.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from noblivion.learning import LearningLedger, LeaseLost

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
    value["enabled"] = bool(scopes) and value.get("enabled", True)
    if value["enabled"]:
        try:
            worker_options(value)
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            log.warning("invalid learning provider or limits; learning is off")
            value["enabled"] = False
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


PASS_INTERVAL = 5.0
# A hard bound on worker threads, whatever budget.max_concurrency says.
MAX_WORKERS = 16
# How often a worker that waits for a call slot renews its job lease.
SLOT_WAIT_SECONDS = 5.0


def worker_count(settings: dict | None) -> int:
    """The number of worker loops: budget.max_concurrency, 0 when learning is off."""
    if not settings or settings.get("enabled") is not True:
        return 0
    _, _, budget = worker_options(settings)
    return max(0, min(MAX_WORKERS, math.ceil(budget.max_concurrency)))


class CallSlots:
    """The model calls that may run at once in this store, resizable at run time.

    The ledger counts reservations across processes (``reserve`` refuses at
    ``max_concurrency``). These slots make the workers of one store wait for
    each other in turn, so a scale-down or a scale-up never turns a busy slot
    into a ``BudgetExceeded`` deferral of a started job.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._limit = 0
        self._used = 0

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def used(self) -> int:
        with self._cond:
            return self._used

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = limit
            self._cond.notify_all()

    def acquire(self, stop: threading.Event, renew: Callable[[], None]) -> bool:
        """Take a slot. False when the store stops or the limit falls to 0.

        ``renew`` runs between waits, outside the lock, to keep the job lease.
        """
        while True:
            with self._cond:
                if self._used < self._limit:
                    self._used += 1
                    return True
                if stop.is_set() or self._limit <= 0:
                    return False
                self._cond.wait(SLOT_WAIT_SECONDS)
                if self._used < self._limit:
                    self._used += 1
                    return True
            renew()

    def release(self) -> None:
        with self._cond:
            self._used = max(0, self._used - 1)
            self._cond.notify_all()


class PooledLedger(LearningLedger):
    """A worker's ledger: each model call holds one call slot from reserve to settle."""

    def __init__(self, conn, pool: LearningPool):
        super().__init__(conn)
        self._pool = pool
        self._held = False

    def reserve(self, job_id: str, lease_token: str, *, limits, **estimates):
        if not self._held:
            lease_seconds = self._pool.lease_seconds

            def renew() -> None:
                self.renew_lease(job_id, lease_token, lease_seconds)

            if not self._pool.slots.acquire(self._pool.stop, renew):
                # A stop or a scale-down to 0 while this job waits: give the job
                # back at once, keep its attempt, and write nothing more.
                self.defer(job_id, lease_token, retry_delay=0, error="LearningWorkerStopped")
                raise LeaseLost("learning worker stopped")
            self._held = True
        # The current budget, not the one read when the job started: a limit
        # that changed while the job ran applies to its next call.
        current = self._pool.budget or limits
        try:
            return super().reserve(job_id, lease_token, limits=current, **estimates)
        except BaseException:
            self.release()
            raise

    def settle(self, reservation_id: str, **actuals) -> None:
        try:
            super().settle(reservation_id, **actuals)
        finally:
            self.release()

    def release(self) -> None:
        """Free the call slot. Safe to call when no slot is held."""
        if self._held:
            self._held = False
            self._pool.slots.release()


class LearningPool:
    """Up to ``budget.max_concurrency`` worker loops for one store (NOBLIVION-242).

    One supervisor reads learning.json, prunes, projects and indexes. Each
    worker loop claims jobs through the ledger lease with its own database
    connection and the latest settings the supervisor published.
    """

    def __init__(self, store):
        self.store = store
        self.stop: threading.Event = store._jobs_stop
        self.slots = CallSlots()
        self.settings: dict | None = None
        self.budget = None
        self.lease_seconds: float = 120.0
        self.poll_seconds: float = PASS_INTERVAL
        self.desired = 0
        self._threads: dict[int, threading.Thread] = {}
        self._last_prune = float("-inf")

    def update(self, settings: dict | None) -> None:
        """Publish one settings snapshot and start or retire worker loops."""
        desired = 0
        try:
            desired = worker_count(settings)
            if desired:
                _, worker, budget = worker_options(settings)
                self.budget, self.lease_seconds = budget, worker.lease_seconds
                self.poll_seconds = worker.poll_seconds
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            desired = 0
        self.settings = settings if desired else None
        self.desired = desired
        self.slots.set_limit(desired)
        for index in range(desired):
            thread = self._threads.get(index)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._work,
                    args=(index,),
                    name=f"noblivion-learning-{index}",
                    daemon=True,
                )
                self._threads[index] = thread
                thread.start()

    def alive(self) -> int:
        return sum(thread.is_alive() for thread in self._threads.values())

    def _work(self, index: int) -> None:
        """One worker loop. A loop above the current count ends after its job."""
        worker_id = f"learning-{uuid.uuid4().hex}-{index}"
        while not self.stop.is_set():
            settings = self.settings
            if settings is None or index >= self.desired:
                return
            result: dict = {"status": "failed"}
            ledger = None
            try:
                with self.store.connection() as conn:
                    ledger = PooledLedger(conn, self)
                    result = run_once(ledger, settings, worker_id=worker_id)
            except Exception as exc:  # noqa: BLE001 - pending work survives this pass
                log.error("learning pass failed: %s", type(exc).__name__)
            finally:
                if ledger is not None:
                    ledger.release()
            if result.get("status") != "done":
                self.stop.wait(self.poll_seconds)

    def pass_once(self) -> None:
        """Read the settings, prune, publish them to the workers, project and index."""
        try:
            settings = load_settings(self.store.data_dir)
        except Exception as exc:  # noqa: BLE001 - an invalid file stops the workers
            self.update(None)
            log.error("learning pass failed: %s", type(exc).__name__)
            return
        if not settings["scopes"]:
            self.update(None)
            return
        try:
            with self.store.connection() as conn:
                ledger = LearningLedger(conn)
                options = prune_options(settings)
                if options is not None and time.monotonic() - self._last_prune >= PRUNE_INTERVAL:
                    self._last_prune = time.monotonic()
                    counts = ledger.prune(**options)
                    if any(counts.values()):
                        log.info("learning prune: %s", json.dumps(counts, sort_keys=True))
                self.update(settings)
                project_and_index(self.store, ledger, settings)
        except Exception as exc:  # noqa: BLE001 - pending work survives this pass
            log.error("learning pass failed: %s", type(exc).__name__)

    def shutdown(self, timeout: float = 0.0) -> None:
        """Stop starting calls; join the loops when a timeout is given."""
        self.desired = 0
        self.slots.set_limit(0)
        deadline = time.monotonic() + timeout
        for thread in self._threads.values():
            thread.join(max(0.0, deadline - time.monotonic()))


def run(store) -> None:
    """The supervisor: one pass every 5 s. A bad pass never kills the loop."""
    pool = LearningPool(store)
    try:
        while not store._jobs_stop.is_set():
            pool.pass_once()
            store._jobs_stop.wait(PASS_INTERVAL)
    finally:
        pool.shutdown()


def worker_options(settings: dict):
    """Validate the local provider and finite limits without contacting a model."""
    from noblivion.learning import BudgetLimits
    from noblivion.learning_worker import OpenAICompatibleProvider, WorkerSettings

    provider = OpenAICompatibleProvider(**settings["provider"])
    worker = WorkerSettings(**settings.get("worker", {}))
    budget = BudgetLimits(**settings.get("budget", {}))
    if budget.window_seconds <= 0 or any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in vars(budget).values()
    ):
        raise ValueError("invalid learning budget")
    return provider, worker, budget


def run_once(ledger: LearningLedger, settings: dict, *, worker_id: str | None = None):
    """Construct an explicitly configured local worker; no provider fallback."""
    from noblivion.learning_worker import LearningWorker

    worker = LearningWorker(
        ledger,
        *worker_options(settings),
        worker_id=worker_id,
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
