# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bounded worker with durable reservations before every local model call.

Run in its own store thread with its own SQLite connection. Provider failures
never acknowledge sources. A process crash leaves a lease and reservation that
are recovered conservatively by the durable ledger.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from threading import Event, Timer

from .learning import BudgetExceeded, LeaseLost, SemanticSnapshotChanged
from .learning_context import dependent_decisions, isolate_notes
from .learning_extract import (
    EXTRACTION_SCHEMA,
    SEMANTIC_PROMPT,
    SEMANTIC_SCHEMA,
    SYSTEM_PROMPT,
    ResponseFormatError,
    ValidationError,
    build_windows,
    contains_secret,
    semantic_targets,
    validate_extraction,
    validate_semantic_response,
)
from .learning_pages import CoverageExhausted, ExtractionPages

LOG = logging.getLogger(__name__)
_FORMAT_RETRY = (
    "\nThe previous response was not valid JSON. Generate the response again from "
    "the original evidence below. Return exactly one JSON object matching the "
    "response schema, without Markdown fences, commentary or trailing delimiters. "
    "All evidence and safety requirements still apply."
)
CONTINUATION_PROMPT = (
    "\nThis is a coverage continuation. Return only remaining lessons; "
    "already_returned statements must not be emitted again. A response "
    "with twelve candidates signals saturation and requires another page."
)
# A job keeps at most this many text-free mismatch records (NOBLIVION-237), so
# a long job cannot push its progress row past the ledger's size limit.
MAX_MISMATCH_RECORDS = 600


def extraction_outcome(value) -> dict:
    """Normalize a validated extraction page.

    A format retry cached before NOBLIVION-237 holds the accepted list only. Such
    a list passed the strict all-or-nothing check, so its raw count is its length.
    """
    if isinstance(value, list):
        return {"accepted": value, "proposed": len(value), "mismatches": []}
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("accepted"), list)
        or type(value.get("proposed")) is not int
        or not isinstance(value.get("mismatches"), list)
    ):
        raise ValidationError("invalid durable extraction outcome")
    return value


def record_mismatches(
    state, unit, continuation, outcome, *, proposed, accepted, mismatches, error=None
):
    """Keep a text-free class for each dropped or rejected candidate of one page.

    Each record names the page (unit and continuation), so a later measurement can
    join it to the page spans and the job sources. Classes are fixed validator
    messages; sources are IDs of validated sources. No conversation text is kept.
    """
    if not mismatches and error is None:
        return
    records = state.setdefault("candidate_mismatches", [])
    if len(records) >= MAX_MISMATCH_RECORDS:
        state["candidate_mismatches_omitted"] = state.get("candidate_mismatches_omitted", 0) + 1
        return
    classes = {}
    for item in mismatches:
        key = (item["class"], item.get("detail"))
        entry = classes.setdefault(key, {"class": item["class"], "count": 0, "sources": set()})
        if item.get("detail"):
            entry["detail"] = item["detail"]
        entry["count"] += 1
        entry["sources"].update(item.get("sources", ()))
    record = {
        "unit": unit,
        "continuation": continuation,
        "outcome": outcome,
        "proposed": proposed,
        "accepted": accepted,
        "classes": [
            {**entry, "sources": sorted(entry["sources"])[:8]} for entry in classes.values()
        ],
    }
    if error is not None:
        record["error"] = error
    records.append(record)


class ProviderError(RuntimeError):
    """Allowlisted diagnosis; never retain an HTTP body or exception message."""

    def __init__(self, reason="transport", *, tokens=None):
        allowed = {"incomplete", "length", "empty", "timeout", "transport", "invalid"}
        self.reason = reason if reason in allowed else "transport"
        self.tokens = tokens if type(tokens) is int and tokens >= 0 else None
        super().__init__("provider output incomplete" if reason == "length" else self.reason)


class DependencyDeferred(RuntimeError):
    """A hidden existing statement may change a proposed write."""


@dataclass(frozen=True)
class WorkerSettings:
    max_attempts: int = 4
    lease_seconds: int = 120
    retry_delay: float = 30.0
    timeout_seconds: float = 60.0
    max_output_tokens: int = 2048
    window_chars: int = 12000
    overlap_chars: int = 2000
    max_windows: int = 128
    max_existing_chars: int = 24000
    poll_seconds: float = 5.0
    max_pages: int = 256
    max_continuations: int = 16
    max_decisions: int = 512
    enable_consolidation: bool = False

    def __post_init__(self):
        for name in (
            "max_attempts",
            "lease_seconds",
            "max_output_tokens",
            "window_chars",
            "max_windows",
            "max_existing_chars",
            "max_pages",
            "max_continuations",
            "max_decisions",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError("invalid worker limits")
        for name in ("retry_delay", "timeout_seconds", "poll_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid worker limits")
        if self.max_attempts > 10 or self.timeout_seconds > 300 or self.max_output_tokens > 8192:
            raise ValueError("worker limits exceed hard bounds")
        if (
            self.max_pages > 512
            or self.max_continuations > 32
            or self.max_decisions > 512
            or self.max_windows > 256
            or self.window_chars > 262144
            or self.max_existing_chars > 65536
            or type(self.enable_consolidation) is not bool
        ):
            raise ValueError("worker limits exceed hard bounds")


# NOBLIVION-236: a started job freezes only the configuration that can change a
# provider request, the accepted output or a durable coverage receipt. Policy
# settings decide when, how often and under which admission limit the worker
# calls. They never enter a request, so a started job uses their current value.
# Every WorkerSettings and BudgetLimits field must be in exactly one set; a test
# enforces this for new fields.
REQUEST_SETTINGS = frozenset(
    {
        "window_chars",  # window boundaries
        "overlap_chars",
        "max_windows",
        "max_existing_chars",  # reconciliation context in every request
        "max_output_tokens",  # provider max_tokens and the length-hold key
        "max_pages",  # coverage limits decide the durable receipt status
        "max_continuations",
        "max_decisions",
    }
)
POLICY_SETTINGS = frozenset(
    {
        "max_attempts",  # retry and lease scheduling
        "lease_seconds",
        "retry_delay",
        "poll_seconds",
        "timeout_seconds",  # call deadline and reservation estimate, not request text
        "enable_consolidation",  # follow-up job after a finished extraction
    }
)
# Budgets are admission control over the shared learning_budget table. reserve()
# reads the current limits on every call and settlement never reads them.
REQUEST_BUDGETS = frozenset()
POLICY_BUDGETS = frozenset(
    {"window_seconds", "max_calls", "max_tokens", "max_seconds", "max_cost", "max_concurrency"}
)
REQUEST_PROVIDER = ("provider_type", "endpoint", "model", "reasoning_effort")
# Version of the prompt and schema text of every learning request. Bump it when
# that text changes; started jobs then fail visibly instead of mixing outputs. A
# test pins the twelve prompt and schema strings to this version. It does not
# cover validator code, so bump it by hand when acceptance rules change.
# NOBLIVION-237 keeps version 1 on purpose: it drops a candidate that fails an
# evidence check instead of the whole response. Every candidate it accepts, the
# version 1 check accepts with the same result, so old and new pages mix safely.
# The neutral mark for a legacy redaction marker changes note data, not a prompt.
EXTRACTION_CONTRACT = 1
# Releases up to 0.1.24 stored no contract. Their contract is version 1.
LEGACY_EXTRACTION_CONTRACT = 1
# The exact configuration shape frozen by releases up to 0.1.24.
LEGACY_SETTINGS = (
    "max_attempts",
    "lease_seconds",
    "retry_delay",
    "timeout_seconds",
    "max_output_tokens",
    "window_chars",
    "overlap_chars",
    "max_windows",
    "max_existing_chars",
    "poll_seconds",
    "max_pages",
    "max_continuations",
    "max_decisions",
    "enable_consolidation",
)
LEGACY_BUDGETS = (
    "window_seconds",
    "max_calls",
    "max_tokens",
    "max_seconds",
    "max_cost",
    "max_concurrency",
)


def request_configuration(configuration: dict | None) -> dict:
    """The frozen part of a worker configuration, from the current or a stored one."""
    configuration = configuration if isinstance(configuration, dict) else {}
    settings = configuration.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    budgets = configuration.get("budgets")
    budgets = budgets if isinstance(budgets, dict) else {}
    return {
        "contract": configuration.get("contract", LEGACY_EXTRACTION_CONTRACT),
        "settings": {key: settings.get(key) for key in sorted(REQUEST_SETTINGS)},
        "budgets": {key: budgets.get(key) for key in sorted(REQUEST_BUDGETS)},
        **{key: configuration.get(key, "") for key in REQUEST_PROVIDER},
    }


def legacy_configuration(configuration: dict) -> dict:
    """A configuration in the shape that releases up to 0.1.24 hashed."""
    settings, budgets = configuration["settings"], configuration["budgets"]
    return {
        "settings": {key: settings[key] for key in LEGACY_SETTINGS},
        "budgets": {key: budgets[key] for key in LEGACY_BUDGETS},
        **{key: configuration[key] for key in REQUEST_PROVIDER},
    }


def input_fingerprint(sources, windows, configuration) -> str:
    """The frozen identity of one extraction job's sources, windows and configuration."""
    return hashlib.sha256(
        json.dumps(
            {
                "sources": [(s["id"], s["sha256"]) for s in sources],
                "windows": windows,
                "configuration": configuration,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def worker_fingerprint(configuration) -> str:
    """The frozen identity of one derivation job's worker configuration."""
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def _frozen_hashes(stored: dict, digest) -> set:
    hashes = {digest(stored), digest(request_configuration(stored))}
    try:
        hashes.add(digest(legacy_configuration(stored)))
    except (KeyError, TypeError):
        pass
    return hashes


def frozen_execution(progress: dict, sources, windows) -> dict | None:
    """The stored configuration that reproduces the frozen extraction hash, or None.

    A stored configuration counts only when it hashes, with the current sources
    and windows, to the frozen value in the old full shape or the request shape.
    So a source or window change, or an edited progress row, still fails.
    """
    frozen = progress.get("execution_fingerprint", progress.get("fingerprint"))
    for key in ("execution_configuration", "call_configuration"):
        stored = progress.get(key)
        if isinstance(stored, dict) and frozen in _frozen_hashes(
            stored, lambda value: input_fingerprint(sources, windows, value)
        ):
            return stored
    return None


def execution_matches(progress: dict, sources, windows, current: dict) -> bool:
    """True when a started extraction job may continue under the current configuration."""
    frozen = progress.get("execution_fingerprint", progress.get("fingerprint"))
    if frozen is None:
        return True
    request = request_configuration(current)
    if frozen == input_fingerprint(sources, windows, request):
        return True
    stored = frozen_execution(progress, sources, windows)
    return stored is not None and request_configuration(stored) == request


def worker_matches(progress: dict, current: dict) -> bool:
    """True when a started derivation job may continue under the current configuration.

    Releases up to 0.1.24 stored only the hash before the first call. Such a job
    continues when the current configuration is unchanged, or when a stored call
    configuration proves the frozen one.
    """
    frozen = progress.get("worker_fingerprint")
    if frozen is None:
        return True
    request = request_configuration(current)
    if frozen in {worker_fingerprint(request), worker_fingerprint(legacy_configuration(current))}:
        return True
    for key in ("worker_configuration", "call_configuration"):
        stored = progress.get(key)
        if isinstance(stored, dict) and frozen in _frozen_hashes(stored, worker_fingerprint):
            return request_configuration(stored) == request
    return False


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    tokens: int | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("transport")


class _DeadlineConnection(http.client.HTTPConnection):
    """Interrupt slow headers and body reads at the absolute request deadline."""

    def __init__(self, *args, deadline: float, **kwargs):
        self.deadline = deadline
        self.deadline_timer = None
        self.transport = None
        super().__init__(*args, **kwargs)

    def connect(self):
        self.timeout = max(0.001, min(self.timeout, self.deadline - time.monotonic()))
        super().connect()
        self.transport = self.sock
        self.deadline_timer = Timer(max(0, self.deadline - time.monotonic()), self.expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()

    def expire(self):
        try:
            if self.transport is not None:
                self.transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def cancel_deadline(self):
        if self.deadline_timer is not None:
            self.deadline_timer.cancel()


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        connections = []
        request.learning_connections = connections

        def connection(host, **kwargs):
            conn = _DeadlineConnection(host, deadline=request.learning_deadline, **kwargs)
            connections.append(conn)
            return conn

        try:
            response = self.do_open(connection, request)
        except BaseException:
            for conn in connections:
                conn.cancel_deadline()
            raise
        response.learning_connections = connections
        return response


class OpenAICompatibleProvider:
    """Explicitly configured loopback-only, zero-dollar provider. No fallbacks.

    Network proxies and redirects are disabled: a local URL must never silently
    submit conversation data to an external endpoint. Paid providers require a
    separate implementation with verified pricing and are not accepted here.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        local: bool = False,
        cost_per_million_tokens: float | None = None,
        reasoning_effort: str = "low",
    ):
        url = urllib.parse.urlsplit(endpoint)
        if (
            not local
            or cost_per_million_tokens != 0
            or url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") not in {"", "/v1", "/v1/chat/completions"}
        ):
            raise ValueError("learning requires an explicitly priced local loopback provider")
        if not isinstance(model, str) or not model.strip() or contains_secret(model):
            raise ValueError("invalid local model")
        if reasoning_effort not in {"low", "none"}:
            raise ValueError("invalid local reasoning effort")
        self.endpoint = urllib.parse.urlunsplit(
            (url.scheme, url.netloc, "/v1/chat/completions", "", "")
        )
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler()
        )

    def estimate_cost(self, tokens: int) -> float:
        return 0.0

    def complete(
        self, messages: list[dict], *, max_tokens: int, timeout: float, response_schema=None
    ) -> ProviderResponse:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": (
                    {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "learning_response",
                            "strict": True,
                            "schema": response_schema,
                        },
                    }
                    if response_schema is not None
                    else {"type": "json_object"}
                ),
                "reasoning_effort": self.reasoning_effort,
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        deadline = time.monotonic() + timeout
        request.learning_deadline = deadline
        response = None
        try:
            with self.opener.open(request, timeout=timeout) as response:
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ProviderError("timeout")
                    # read1 returns after one underlying read, so a trickling
                    # local server cannot reset the wall clock indefinitely.
                    remaining = max(0.001, deadline - time.monotonic())
                    transport = getattr(
                        getattr(getattr(response, "fp", None), "raw", None), "_sock", None
                    )
                    if transport is not None:
                        transport.settimeout(remaining)
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 524288:
                        raise ProviderError("invalid")
                    chunks.append(chunk)
                raw = json.loads(b"".join(chunks))
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
            timed_out = (
                time.monotonic() >= deadline
                or isinstance(exc, TimeoutError)
                or isinstance(getattr(exc, "reason", None), TimeoutError)
            )
            reason = (
                "timeout"
                if timed_out
                else "invalid"
                if isinstance(exc, ValueError)
                else "transport"
            )
            raise ProviderError(reason) from exc
        finally:
            for conn in getattr(request, "learning_connections", ()):
                conn.cancel_deadline()
        try:
            choice = raw["choices"][0]
            finish = choice["finish_reason"]
            usage = raw.get("usage", {})
            tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            invalid_usage = not isinstance(usage, dict) or (
                tokens is not None and (type(tokens) is not int or tokens < 0)
            )
            if finish == "length":
                raise ProviderError("length", tokens=None if invalid_usage else tokens)
            if invalid_usage:
                raise ProviderError("invalid")
            if finish != "stop":
                raise ProviderError("incomplete", tokens=tokens)
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ProviderError("invalid", tokens=tokens)
            if not content.strip():
                raise ProviderError("empty", tokens=tokens)
            return ProviderResponse(content, tokens)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("invalid") from exc


class LearningWorker:
    def __init__(
        self,
        ledger,
        provider,
        settings: WorkerSettings,
        budget_limits,
        *,
        worker_id: str | None = None,
        allowed_scopes: set[tuple[str, str]] | None = None,
    ):
        self.ledger = ledger
        self.provider = provider
        self.settings = settings
        self.budget_limits = budget_limits
        self.worker_id = worker_id or "learning-" + uuid.uuid4().hex
        self.allowed_scopes = allowed_scopes
        self.stop: Event | None = None

    def run_once(self) -> dict:
        """Run one job. A lost lease is normal, not a pass failure.

        Another worker may hold the job after an expired lease, and a pause or an
        exclusion revokes the lease on purpose. This worker stops with no write,
        and the job state stays with whoever owns it now.
        """
        try:
            return self._run_once()
        except LeaseLost:
            LOG.info("learning job lease lost; the job is no longer this worker's")
            return {"status": "lease_lost"}

    def _run_once(self) -> dict:
        """Claim one job and process all windows, or leave a visible failure.

        There are no invisible provider retries. Each call reserves its budget
        durably, including the one allowed JSON format retry. Completed windows resume
        from their persisted cursor without another provider call. A reservation
        failure means no provider call occurs.
        """
        cfg = self.settings
        job = self.ledger.claim(
            self.worker_id, lease_seconds=cfg.lease_seconds, max_attempts=cfg.max_attempts
        )
        if job is None:
            return {"status": "idle"}
        ident, lease = job["id"], job["lease_token"]
        if (
            self.allowed_scopes is not None
            and (job["project"], job["root"]) not in self.allowed_scopes
        ):
            self.ledger.defer(ident, lease, retry_delay=3600, error="ScopeNotEnabled")
            return {"status": "deferred", "job_id": ident, "error": "ScopeNotEnabled"}
        state = dict(job.get("progress") or {})
        try:
            if job.get("kind", "extract") != "extract":
                from .learning_synthesis import run_derivation

                current = self._configuration()
                if not worker_matches(state, current):
                    if not self._output_recovery(state):
                        raise ValidationError("derivation worker configuration changed")
                state["worker_fingerprint"] = worker_fingerprint(request_configuration(current))
                state["worker_configuration"] = current
                self.ledger.checkpoint(ident, lease, state)
                job["progress"] = state
                return run_derivation(self.ledger, job, self._validated_call)
            sources = job["sources"]
            windows = self.windows(sources)
            current = self._configuration()
            execution = input_fingerprint(sources, windows, request_configuration(current))
            progress = job.get("progress") or {}
            started = progress.get("execution_fingerprint", progress.get("fingerprint"))
            if not execution_matches(progress, sources, windows, current):
                if (
                    not self._output_recovery(state)
                    or frozen_execution(progress, sources, windows) is None
                ):
                    raise ValidationError("window configuration changed during a job")
                started = None  # an explicit output-limit recovery freezes again
            # Receipts keep their immutable identity across a policy change and
            # an explicit, safe output-limit recovery. Source, window, coverage,
            # provider and contract changes still fail (NOBLIVION-236). A policy
            # change keeps the original freeze, so its stored proof stays valid.
            fingerprint = progress.get("fingerprint", execution)
            if started is None:
                state["execution_fingerprint"] = execution
                state["execution_configuration"] = current
            existing = progress.get("existing_notes")
            exclusions = progress.get("context_exclusions", [])
            if existing is None or progress.get("dependency_deferred"):
                existing, exclusions = isolate_notes(
                    self.ledger.reconciliation_context(
                        job["project"],
                        job["root"],
                        query=" ".join(s["content"] for s in sources),
                        limit=12,
                    ),
                    max_chars=cfg.max_existing_chars,
                )
                # The source receipts remain valid. A changed dependency requires
                # a fresh comparison against the complete current revision set.
                state.pop("semantic", None)
            context = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
            if len(context) > cfg.max_existing_chars or contains_secret(context):
                raise ValidationError("unsafe durable reconciliation context")
            start_window = progress.get("next_window", 0)
            if type(start_window) is not int or not 0 <= start_window <= len(windows):
                raise ValidationError("invalid durable window progress")
            state.update(
                fingerprint=fingerprint,
                next_window=start_window,
                existing_notes=existing,
                context_exclusions=exclusions,
            )
            self.ledger.checkpoint(ident, lease, state)
            pages = ExtractionPages(self.ledger, job, state, fingerprint)
            decisions = self._extract(job, sources, windows, existing, state, pages)
            # Overlap can repeat identical candidates. Conflicting proposals in
            # one batch fail closed instead of depending on their order.
            unique = {}
            for decision in decisions:
                key = (decision["scope"], decision["logical_key"])
                semantic = (
                    decision["scope"],
                    decision["category"],
                    decision["statement"].casefold(),
                )
                duplicate = next(
                    (
                        d
                        for d in unique.values()
                        if (d["scope"], d["category"], d["statement"].casefold()) == semantic
                    ),
                    None,
                )
                if duplicate is not None and duplicate.get("note_id") == decision.get("note_id"):
                    continue
                if key in unique and unique[key] != decision:
                    raise ValidationError("inconsistent window decisions")
                unique[key] = decision
            decisions = list(unique.values())
            dependent = dependent_decisions(decisions, exclusions)
            if dependent:
                state["deferred_decisions"] = dependent
                raise DependencyDeferred()
            reconciled = self._semantic(job, decisions, existing, state)
            state.pop("dependency_deferred", None)
            state.pop("deferred_decisions", None)
            self.ledger.checkpoint(ident, lease, state)
            if cfg.enable_consolidation:
                result = self.ledger.finish(ident, lease, reconciled, enqueue_consolidation=True)
            else:
                result = self.ledger.finish(ident, lease, reconciled)
            return {"status": "done", "job_id": ident, "results": result}
        except DependencyDeferred:
            state["dependency_deferred"] = True
            self.ledger.checkpoint(ident, lease, state)
            self.ledger.defer(ident, lease, retry_delay=3600, error="ContextDependencyDeferred")
            return {"status": "deferred", "job_id": ident, "error": "ContextDependencyDeferred"}
        except ProviderError as exc:
            reason = "Provider:" + exc.reason
            if exc.reason == "length":
                self.ledger.defer(ident, lease, retry_delay=3600, error=reason)
                return {"status": "deferred", "job_id": ident, "error": reason}
            self.ledger.fail(
                ident,
                lease,
                reason,
                retry_delay=min(cfg.retry_delay * 2 ** max(0, job["attempts"] - 1), 3600),
                max_attempts=cfg.max_attempts,
            )
            return {"status": "failed", "job_id": ident, "error": reason, "permanent": False}
        except SemanticSnapshotChanged:
            # Preserve page receipts; only the comparison snapshot is stale.
            state.pop("semantic", None)
            state.pop("derivation", None)
            state.pop("format_repair", None)
            self.ledger.checkpoint(ident, lease, state)
            self.ledger.defer(ident, lease, retry_delay=1, error="SemanticSnapshotChanged")
            return {"status": "deferred", "job_id": ident, "error": "SemanticSnapshotChanged"}
        except BudgetExceeded:
            self.ledger.defer(
                ident,
                lease,
                retry_delay=min(self.budget_limits.window_seconds, 3600),
                error="BudgetExceeded",
            )
            return {"status": "deferred", "job_id": ident, "error": "BudgetExceeded"}
        except Exception as exc:
            # Error class is enough for operational diagnosis without persisting
            # a provider error body, credentials, or raw conversation fragments.
            permanent = isinstance(exc, ValidationError)
            reason = type(exc).__name__
            diagnostic = reason + (": " + str(exc) if permanent else "")
            self.ledger.fail(
                ident,
                lease,
                diagnostic,
                retry_delay=min(cfg.retry_delay * 2 ** max(0, job["attempts"] - 1), 3600),
                max_attempts=cfg.max_attempts,
                permanent=permanent,
            )
            return {"status": "failed", "job_id": ident, "error": reason, "permanent": permanent}

    def _output_recovery(self, state):
        """Only a recorded length result permits a reviewed provider-limit change."""
        previous = state.get("call_configuration")
        if not state.get("output_limit_hold") or not isinstance(previous, dict):
            return False
        current = self._configuration()

        def policy(configuration):
            # Policy settings and budgets are already outside the request part.
            frozen = request_configuration(configuration)
            frozen.pop("model", None)
            frozen.pop("reasoning_effort", None)
            frozen["settings"].pop("max_output_tokens", None)
            return frozen

        return policy(previous) == policy(current)

    def windows(self, sources):
        cfg = self.settings
        return build_windows(
            sources,
            window_chars=cfg.window_chars,
            overlap=min(cfg.overlap_chars, cfg.window_chars // 4),
            max_windows=cfg.max_windows,
        )

    def _configuration(self):
        return {
            "contract": EXTRACTION_CONTRACT,
            "settings": asdict(self.settings),
            "budgets": vars(self.budget_limits),
            "provider_type": type(self.provider).__module__
            + "."
            + type(self.provider).__qualname__,
            "endpoint": getattr(self.provider, "endpoint", ""),
            "model": getattr(self.provider, "model", ""),
            "reasoning_effort": getattr(self.provider, "reasoning_effort", ""),
        }

    def _extract(self, job, sources, windows, existing, state, pages):
        cfg = self.settings
        for unit_index in range(state["next_window"], len(windows)):
            window = windows[unit_index]
            continuation = state.get("next_continuation", 0)
            while True:
                if self.stop is not None and self.stop.is_set():
                    raise BudgetExceeded("worker stopping")
                receipt = pages.receipt(unit_index, continuation)
                if receipt is None:
                    if pages.count() >= cfg.max_pages:
                        raise CoverageExhausted(
                            "extraction page limit reached; coverage incomplete"
                        )
                    previous = [
                        d
                        for row in pages._rows()
                        if row["unit_index"] == unit_index
                        for d in row["candidates"]
                    ]
                    messages = [
                        {
                            "role": "system",
                            "content": SYSTEM_PROMPT + CONTINUATION_PROMPT,
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "sources": window,
                                    "existing_notes": existing,
                                    "already_returned": [d["statement"] for d in previous],
                                    "continuation": continuation,
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ]
                    try:
                        outcome = self._validated_call(
                            job,
                            messages,
                            lambda raw: validate_extraction(raw, sources, existing),
                            state,
                            f"extract:{unit_index}:{continuation}",
                            EXTRACTION_SCHEMA,
                        )
                        outcome = self._validate_window(extraction_outcome(outcome), window)
                    except ValidationError as exc:
                        record_mismatches(
                            state,
                            unit_index,
                            continuation,
                            "rejected",
                            proposed=getattr(exc, "proposed", None),
                            accepted=getattr(exc, "accepted", 0),
                            mismatches=getattr(exc, "mismatches", []),
                            error=str(exc),
                        )
                        self.ledger.checkpoint(job["id"], job["lease_token"], state)
                        pages.save(
                            unit_index,
                            continuation,
                            window,
                            [],
                            "rejected",
                            "candidate validation failed; no coverage acknowledgement",
                        )
                        raise
                    accepted, proposed = outcome["accepted"], outcome["proposed"]
                    if outcome["mismatches"]:
                        record_mismatches(
                            state,
                            unit_index,
                            continuation,
                            "dropped",
                            proposed=proposed,
                            accepted=len(accepted),
                            mismatches=outcome["mismatches"],
                        )
                        self.ledger.checkpoint(job["id"], job["lease_token"], state)
                    # Saturation counts the raw proposals: a page of twelve with a
                    # dropped candidate may still hide more lessons (NOBLIVION-237).
                    saturated = proposed == 12
                    novel = [
                        d
                        for d in accepted
                        if d["statement"] not in {prior["statement"] for prior in previous}
                    ]
                    total = {
                        (d["scope"], d["category"], d["statement"].casefold())
                        for d in pages.decisions() + novel
                    }
                    exhausted = (
                        len(total) > cfg.max_decisions
                        or (saturated and continuation + 1 >= cfg.max_continuations)
                        # A continuation that adds nothing new makes no progress,
                        # also when all twelve of its proposals were dropped.
                        or (continuation > 0 and not novel and (bool(accepted) or saturated))
                    )
                    status = (
                        "exhausted"
                        if exhausted
                        else "saturated"
                        if saturated
                        else "processed"
                        if previous or accepted
                        else "no_lesson"
                    )
                    reason = (
                        "hard limit or repeated saturated response; coverage incomplete"
                        if exhausted
                        else "twelve candidates require continuation"
                        if status == "saturated"
                        else "provider reported remaining lessons"
                        if status == "processed"
                        else "every candidate failed an evidence check"
                        if outcome["mismatches"]
                        else "provider reported no eligible lesson"
                    )
                    receipt = pages.save(unit_index, continuation, window, accepted, status, reason)
                if receipt["status"] in {"exhausted", "rejected"}:
                    raise CoverageExhausted("extraction receipt is incomplete")
                state.pop("format_repair", None)
                if receipt["status"] == "saturated":
                    continuation += 1
                    state["next_continuation"] = continuation
                    self.ledger.checkpoint(job["id"], job["lease_token"], state)
                    continue
                state["next_window"] = unit_index + 1
                state.pop("next_continuation", None)
                self.ledger.checkpoint(job["id"], job["lease_token"], state)
                break
        return pages.decisions()

    @staticmethod
    def _validate_window(outcome, window):
        """Drop a candidate whose evidence lies outside the submitted window.

        Full sources establish hashes; exact windows establish what was submitted.
        This is an evidence check of one candidate, so it drops that candidate only.
        """
        kept, mismatches = [], list(outcome["mismatches"])
        for decision in outcome["accepted"]:
            cited = (
                decision["evidence"]
                + decision.get("proof", {}).get("before", [])
                + decision.get("proof", {}).get("after", [])
                + decision.get("correction", {}).get("current_evidence", [])
            )
            if all(
                any(
                    str(part["id"]) == str(evidence["source_id"])
                    and evidence["start"] >= part["offset"]
                    and evidence["end"] <= part["offset"] + len(part["content"])
                    for part in window
                )
                for evidence in cited
            ):
                kept.append(decision)
                continue
            mismatches.append(
                {
                    "class": "evidence was not present in the model window",
                    "sources": sorted({str(e["source_id"]) for e in cited})[:8],
                }
            )
        return {**outcome, "accepted": kept, "mismatches": mismatches}

    def _validated_call(self, job, messages, validate, state, step, schema):
        """Retry invalid JSON syntax once, never repair or replay rejected text.

        Reserve before marking the retry consumed, so budget deferral can resume.
        Mark before submission, so a crash cannot cause an uncounted retry. An
        accepted retry is cached before returning, then removed atomically with
        caller progress. A crash before that cache commits leaves an uncertain
        consumed retry, which fails closed and needs an explicit job retry.
        """
        repair = state.get("format_repair")
        if repair is None:
            response = self._call(job, messages, state=state, response_schema=schema)
            try:
                return validate(response.content)
            except ResponseFormatError:
                repair = {"step": step, "calls": 0}
                state["format_repair"] = repair
                self.ledger.checkpoint(job["id"], job["lease_token"], state)
        if (
            not isinstance(repair, dict)
            or repair.get("step") != step
            or type(repair.get("calls")) is not int
            or repair["calls"] not in {0, 1}
        ):
            raise ValidationError("invalid durable format retry")
        if "accepted" in repair:
            if repair["calls"] != 1 or not isinstance(repair["accepted"], (list, dict)):
                raise ValidationError("invalid durable accepted retry")
            return repair["accepted"]
        if repair["calls"] and not state.get("output_limit_hold"):
            raise ValidationError("format retry already consumed")

        def consume():
            repair["calls"] = 1
            self.ledger.checkpoint(job["id"], job["lease_token"], state)

        retry_messages = [dict(message) for message in messages]
        retry_messages[0]["content"] += _FORMAT_RETRY
        response = self._call(
            job, retry_messages, state=state, response_schema=schema, before_provider=consume
        )
        try:
            accepted = validate(response.content)
        except ResponseFormatError as exc:
            raise ValidationError("invalid JSON after bounded format retry") from exc
        repair["accepted"] = accepted
        self.ledger.checkpoint(job["id"], job["lease_token"], state)
        return accepted

    def _call(
        self,
        job: dict,
        messages: list[dict],
        *,
        state: dict,
        response_schema=None,
        before_provider=None,
    ) -> ProviderResponse:
        """One reservation per extraction or semantic call, including retries."""
        cfg = self.settings
        submitted = json.dumps(messages, ensure_ascii=False)
        if contains_secret(submitted):
            raise ValidationError("unsafe final provider context")
        call_key = hashlib.sha256(
            json.dumps(
                [
                    messages,
                    response_schema,
                    {
                        key: self._configuration()[key]
                        for key in ("provider_type", "endpoint", "model", "reasoning_effort")
                    },
                    cfg.max_output_tokens,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if state.get("output_limit_hold") == call_key:
            raise ProviderError("length")
        tokens = len(submitted.encode()) + cfg.max_output_tokens
        if response_schema is not None:
            tokens += len(json.dumps(response_schema).encode())
        cost = self.provider.estimate_cost(tokens)
        reservation = self.ledger.reserve(
            job["id"],
            job["lease_token"],
            limits=self.budget_limits,
            estimated_tokens=tokens,
            estimated_seconds=cfg.timeout_seconds,
            estimated_cost=cost,
        )
        state["call_configuration"] = self._configuration()
        state.pop("output_limit_hold", None)
        self.ledger.checkpoint(job["id"], job["lease_token"], state)
        if before_provider is not None:
            before_provider()
        started = time.monotonic()
        response, failure = None, None
        try:
            response = self.provider.complete(
                messages,
                max_tokens=cfg.max_output_tokens,
                timeout=cfg.timeout_seconds,
                response_schema=response_schema,
            )
            if time.monotonic() - started > cfg.timeout_seconds:
                raise ProviderError("timeout", tokens=response.tokens)
        except ProviderError as exc:
            failure = exc
            if exc.reason == "length":
                state["output_limit_hold"] = call_key
            raise
        except Exception:
            failure = ProviderError("transport")
            raise failure from None
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            usage = response.tokens if response is not None else getattr(failure, "tokens", None)
            metrics = state.setdefault(
                "provider_metrics", {"calls": 0, "seconds": 0.0, "tokens": 0, "reasons": {}}
            )
            metrics["calls"] += 1
            metrics["seconds"] += elapsed
            metrics["tokens"] += usage or 0
            reason = failure.reason if failure else "success"
            metrics["reasons"][reason] = metrics["reasons"].get(reason, 0) + 1
            # Preserve a known output-limit hold even if settlement fails.
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
            # Measured durations are independent of conservative reservations.
            # Unknown failed usage stays uncertain, never refunded. Measured
            # time overruns still count when token usage is unavailable.
            self.ledger.settle(
                reservation["id"],
                actual_tokens=usage,
                actual_seconds=elapsed,
                actual_cost=cost,
                uncertain=failure is not None and usage is None,
            )
        return response

    def _semantic(
        self, job: dict, decisions: list[dict], context: list[dict], state: dict
    ) -> list[dict]:
        """Compare all managed notes, including zero lexical overlap, before adds.

        A semantic judgment can prevent a write, never authorize a correction.
        Equivalent, contradictory or uncertain candidates become visible conflicts.
        Each completed comparison is durable so budget pauses do not repeat it.
        """
        if not any(d["action"] == "add" for d in decisions):
            return decisions
        stage = state.get("semantic")
        if stage is None:
            managed = self.ledger.semantic_notes(
                job["project"], job["root"], session_id=job.get("session_id", "")
            )
            snapshot = sorted([[n["note_id"], n["revision"]] for n in managed])
            notes = [
                {
                    key: n[key]
                    for key in ("note_id", "revision", "statement", "scope", "read_only")
                    if key in n
                }
                for n in managed
            ]
            present = {n["note_id"] for n in notes}
            notes.extend(n for n in context if n.get("read_only") and n["note_id"] not in present)
            # Each semantic target gets a bound; never truncate the revision
            # snapshot to the subset whose bodies were safe to submit.
            safe_notes, exclusions = [], []
            for note in notes:
                kept, rejected = isolate_notes(
                    [note], max_chars=min(self.settings.max_existing_chars, 16000) - 2400
                )
                safe_notes.extend(kept)
                exclusions.extend(rejected)
            notes = safe_notes
            state["semantic_exclusions"] = exclusions
            dependent = dependent_decisions(decisions, exclusions)
            if dependent:
                state["deferred_decisions"] = dependent
                raise DependencyDeferred()
            stage = {
                "notes": notes,
                "snapshot": snapshot,
                "next_task": 0,
                "decisions": [dict(d) for d in decisions],
            }
            state["semantic"] = stage
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
        tasks = semantic_targets(
            decisions, stage["notes"], max_chars=min(self.settings.max_existing_chars, 16000)
        )
        results = stage["decisions"]
        for index in range(stage["next_task"], len(tasks)):
            task = tasks[index]
            candidate = results[task["candidate_index"]]
            if candidate["action"] == "add":
                if self.stop is not None and self.stop.is_set():
                    raise BudgetExceeded("worker stopping")
                relations = self._validated_call(
                    job,
                    [
                        {"role": "system", "content": SEMANTIC_PROMPT},
                        {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
                    ],
                    lambda raw, task=task: validate_semantic_response(raw, task),
                    state,
                    f"semantic:{index}",
                    SEMANTIC_SCHEMA,
                )
                blocked = next((r for r in relations if r["relation"] != "distinct"), None)
                if blocked:
                    candidate.update(
                        action="conflict",
                        reason="semantic " + blocked["relation"],
                        semantic_review={
                            "target_id": blocked["target_id"],
                            "relation": blocked["relation"],
                        },
                    )
                    target = next(
                        (n for n in stage["notes"] if str(n["note_id"]) == blocked["target_id"]),
                        None,
                    )
                    if target is not None:
                        candidate["note_id"] = target["note_id"]
                        candidate["expected_revision"] = target["revision"]
            stage["next_task"] = index + 1
            state.pop("format_repair", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
        for candidate in results:
            if candidate["action"] == "add":
                candidate["semantic_snapshot"] = stage["snapshot"]
        stage["complete"] = True
        self.ledger.checkpoint(job["id"], job["lease_token"], state)
        return results

    def run_forever(self, stop: Event) -> None:
        """Keep independent jobs alive after one failure; shutdown is bounded."""
        self.stop = stop
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception as exc:
                # A ledger outage is not permission to bypass accounting.
                LOG.warning("learning pass failed: %s", type(exc).__name__)
                result = {"status": "failed"}
            if result["status"] != "done":
                stop.wait(self.settings.poll_seconds)
