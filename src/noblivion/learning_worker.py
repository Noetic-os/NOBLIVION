# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bounded worker with durable reservations before every local model call.

Run in its own store thread with its own SQLite connection. Provider failures
never acknowledge sources. A process crash leaves a lease and reservation that
are recovered conservatively by the durable ledger.
"""

from __future__ import annotations

import errno
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

from .learning import BudgetExceeded, ConcurrencyExceeded, LeaseLost, SemanticSnapshotChanged
from .learning_context import dependent_decisions, isolate_notes
from .learning_extract import (
    EXTRACTION_SCHEMA,
    RESPONSE_FAILURES,
    SEMANTIC_PROMPT,
    SEMANTIC_SCHEMA,
    SYSTEM_PROMPT,
    ResponseFormatError,
    ResponseRejected,
    SemanticAnswerRejected,
    ValidationError,
    build_windows,
    contains_secret,
    semantic_targets,
    validate_extraction,
    validate_semantic_response,
)
from .learning_nearest import (
    FALLBACK_SELECTIONS,
    MODE_HYBRID,
    MODE_KEYWORD,
    candidate_key,
    nearest_targets,
)
from .learning_pages import CoverageExhausted, ExtractionPages, coverage
from .learning_semantic import (
    MAX_BATCH_ITEMS,
    SEMANTIC_BATCH_PROMPT,
    SEMANTIC_BATCH_SCHEMA,
    learnable_window,
    semantic_batches,
    validate_semantic_batch,
)
from .learning_values import canonical_json

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
# The text-free mismatch records of one job use at most this many encoded bytes
# (NOBLIVION-237). The ledger refuses a progress row over 1 MiB. This leaves three
# quarters of the row for the rest of the progress (budget metrics, context
# exclusions, a cached format retry, semantic state). A count limit is not enough:
# a record with twelve classes and long source IDs takes about 4 KB.
MAX_MISMATCH_BYTES = 256 * 1024


def _encoded_size(value) -> int:
    return len(canonical_json(value).encode("utf-8"))


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
    # Measure the list as the ledger serializes it, with the new record included.
    if _encoded_size(records + [record]) > MAX_MISMATCH_BYTES:
        state["candidate_mismatches_omitted"] = state.get("candidate_mismatches_omitted", 0) + 1
        return
    records.append(record)


class ProviderError(RuntimeError):
    """Allowlisted diagnosis; never retain an HTTP body or exception message."""

    def __init__(self, reason="transport", *, tokens=None):
        allowed = {
            "incomplete",
            "length",
            "empty",
            "timeout",
            "transport",
            "invalid",
            "unavailable",
        }
        self.reason = reason if reason in allowed else "transport"
        self.tokens = tokens if type(tokens) is int and tokens >= 0 else None
        super().__init__("provider output incomplete" if reason == "length" else self.reason)


# NOBLIVION-245: a window whose model reply reaches the output token cap
# ("length") is split in halves, and each half goes on. A piece at depth
# LENGTH_SPLIT_DEPTH, or of one part with fewer than 2 * LENGTH_SPLIT_MIN_CHARS content
# characters, is at the floor and is not split again. At the floor the piece gets
# a rejected receipt (LENGTH_REJECTED) and the job goes on. So one window makes at
# most 2 ** (LENGTH_SPLIT_DEPTH + 1) - 1 calls that end on length.
# These constants are not in the job fingerprint. A change moves the piece
# bounds and receipt keys of started jobs, so change them only with a new
# EXTRACTION_CONTRACT.
LENGTH_SPLIT_DEPTH = 3
LENGTH_SPLIT_MIN_CHARS = 1024
LENGTH_SPLIT_OVERLAP = 256
# A piece stores its receipts at continuation slot * PIECE_STRIDE + continuation.
# Slot 0 is the whole window, so an unsplit window keeps its receipt keys.
# max_continuations is at most 32, so pieces never share a key.
PIECE_STRIDE = 64
# The number of one-hour deferrals that give the attempt back, for a length
# result outside extraction and for ContextDependencyDeferred: 24, so about one
# day. Past it each deferral counts an attempt, so the job ends after at most
# max_attempts more runs. An explicit retry renews the count.
MAX_RETURNED_DEFERRALS = 24
# NOBLIVION-253: a provider that refuses the connection or answers 429, 502,
# 503 or 504 is down; that is not a fault of the job. Such a deferral gives the attempt
# back with its own count, so an outage does not spend the count above. The
# delay grows from retry_delay to UNAVAILABLE_MAX_DELAY. 200 deferrals cover
# about two days at the longest delay; past them each deferral counts an
# attempt, so a provider that never returns still ends the job.
MAX_UNAVAILABLE_DEFERRALS = 200
UNAVAILABLE_COUNTER = "unavailable_deferrals"
UNAVAILABLE_MAX_DELAY = 900.0
# NOBLIVION-255: a hung provider accepts the connection and never answers, so
# each call ends as a timeout. A timeout can also be the fault of the job (a very
# large request on a healthy server). So after a call timed out with no answer,
# the worker sends a one-token request to the same path and model, with at most
# this many seconds (and never more than timeout_seconds). No answer in time, or
# an outage error, makes the call Provider:unavailable. An answer keeps the
# timeout, which counts an attempt. A busy server can also leave the probe in
# its queue past the deadline. So a probe with no answer makes the call
# Provider:unavailable only when no call of the store got a real answer in the
# time of one call and one probe before the probe began. If one did, the server
# is up and busy, and the timeout counts an attempt. A module constant, not a
# setting, so the configuration and the fingerprint of a started job stay the
# same.
PROBE_TIMEOUT_SECONDS = 20.0
# Provider errors where no answer came back from the provider.
_NO_ANSWER_ERRORS = frozenset({"Provider:unavailable", "Provider:timeout", "Provider:transport"})


def provider_answered(result: dict) -> bool:
    """True when this job result shows that the provider really answered.

    Only such a result ends an outage hold (NOBLIVION-253). A job that made no
    call, waited for its budget or failed on a timeout or a transport error
    says nothing about the provider, so the hold keeps its delay.
    """
    if result.get("status") == "done":
        return True
    error = result.get("error")
    return (
        isinstance(error, str) and error.startswith("Provider:") and error not in _NO_ANSWER_ERRORS
    )


# NOBLIVION-240: a semantic answer that parses but does not match its task (for
# example "invalid semantic target") is asked again up to this many times, with
# a fixed corrective note. Past the bound the one affected candidate is not
# stored, and the job goes on. A module constant, not a setting, so the
# execution fingerprint of a started job stays the same.
MAX_SEMANTIC_REASKS = 2
_SEMANTIC_REASK = (
    "\nA previous answer to this comparison was rejected: {error}. Answer again "
    "from the task below (try {attempt}). Return exactly one relation object for "
    "each listed target_id, use each target_id once, and use no other target_id."
)
_SEMANTIC_BATCH_REASK = (
    "\nA previous answer to this comparison was rejected: {error}. Answer again "
    "from the task below (try {attempt}). Return exactly one result for each "
    "item_id, and in it exactly one relation object for each compare id of that "
    "item. Use each id once, and use no other id."
)


# NOBLIVION-243: the nearest-target selection needs a vector for each add
# candidate. When the embedding model is set up but gives none (it still loads,
# or a call fails or times out), the job waits EMBEDDING_RETRY_DELAY seconds.
# Up to MAX_EMBEDDING_DEFERRALS such waits (about one day) give the attempt
# back; past them each wait counts an attempt. An explicit retry renews the count.
EMBEDDING_RETRY_DELAY = 300.0
MAX_EMBEDDING_DEFERRALS = 288
EMBEDDING_COUNTER = "embedding_deferrals"
# The time limit of one embedding call of the semantic selection. A background
# job can wait longer than a search answer.
EMBEDDING_TIMEOUT_S = 30.0
# The task limit for counting the tasks of a stage before its bound is checked.
UNBOUNDED_TASKS = 1 << 40


def _max_semantic_tasks(job: dict) -> int:
    """The task bound of a job: contract 2 packs up to MAX_BATCH_ITEMS per call."""
    if job.get("contract", LEGACY_EXTRACTION_CONTRACT) >= 2:
        return 512 * MAX_BATCH_ITEMS
    return 512


def _over_bound(job: dict, tasks: list[dict], max_chars: int) -> bool:
    """True when these tasks fail the task or batch bound of the job."""
    if len(tasks) > _max_semantic_tasks(job):
        return True
    if job.get("contract", LEGACY_EXTRACTION_CONTRACT) >= 2:
        return len(semantic_batches(tasks, max_chars=max_chars)) > 512
    return False


def _stage_started(stage: dict) -> bool:
    """True when a semantic stage holds a comparison answer or a re-ask."""
    return bool(
        stage.get("next_task", 0)
        or stage.get("next_batch", 0)
        or stage.get("fallback_done")
        or stage.get("reasks")
    )


def _stage_answered(stage: dict, decisions: list[dict], state: dict) -> bool:
    """True when a semantic stage holds an answer that still counts (NOBLIVION-257).

    The caller checks _stage_started first, so this covers a stage that is not
    started: a rejected batch answer that waits for its re-ask
    (``batch_reasks``), a pending format retry of a semantic call, and what a
    refresh kept from an earlier stage (NOBLIVION-250): the comparisons in
    ``covered``, and each add that a comparison turned into a conflict or left
    unvalidated. A re-ask and a format retry name a task or batch by its index,
    so they bind the stage to its tasks.
    """
    if stage.get("covered") or stage.get("batch_reasks"):
        return True
    repair = state.get("format_repair")
    if isinstance(repair, dict) and str(repair.get("step", "")).startswith("semantic"):
        return True
    results = stage.get("decisions") or ()
    return any(
        decision["action"] == "add" and result["action"] != "add"
        for decision, result in zip(decisions, results)  # noqa: B905 - 3.9 lane
    )


# Text-free reason class of a candidate that the worker does not store.
SEMANTIC_UNVALIDATED = "semantic comparison not validated"


def _lesson(decision: dict) -> tuple:
    return (decision["scope"], decision["category"], decision["statement"].casefold())


def _same_lesson(a: dict, b: dict) -> bool:
    return a == b or (_lesson(a) == _lesson(b) and a.get("note_id") == b.get("note_id"))


def merge_window_decisions(decisions: list[dict]) -> tuple[list[dict], set]:
    """Remove repeats of one lesson and drop every logical key that conflicts.

    Overlap and continuations can repeat a lesson. NOBLIVION-240: two windows
    that give one logical key two different lessons do not fail the job. The
    conflicting keys are found over all decisions first, and every decision
    with such a key is dropped. Only then are repeats removed. So the result
    does not depend on the order of the windows, and a key with no conflict is
    never lost because its lesson repeated under a dropped key.
    """
    by_key: dict[tuple, list[dict]] = {}
    for decision in decisions:
        by_key.setdefault((decision["scope"], decision["logical_key"]), []).append(decision)
    conflicting = {
        key
        for key, group in by_key.items()
        if any(not _same_lesson(group[0], other) for other in group[1:])
    }
    unique: dict[tuple, dict] = {}
    for decision in decisions:
        key = (decision["scope"], decision["logical_key"])
        if key in conflicting:
            continue
        # The same lesson under another key, or a repeat under this key.
        if any(_same_lesson(kept, decision) for kept in unique.values()):
            continue
        unique[key] = decision
    return list(unique.values()), conflicting


def piece_depth(slot: int) -> int:
    return (slot + 1).bit_length() - 1


def split_window(window: list[dict]) -> list[list[dict]]:
    """Split one window in two halves that together cover all its characters."""
    if len(window) > 1:
        half = len(window) // 2
        return [window[:half], window[half:]]
    part = window[0]
    text = part["content"]
    middle = len(text) // 2
    overlap = min(LENGTH_SPLIT_OVERLAP, len(text) // 8)
    left = {**part, "content": text[:middle]}
    right = {
        **part,
        "content": text[middle - overlap :],
        "offset": part["offset"] + middle - overlap,
    }
    return [[left], [right]]


def window_piece(window: list[dict], slot: int) -> list[dict]:
    """The piece of a frozen window at a binary tree slot (0 is the whole window)."""
    path = []
    while slot > 0:
        path.append((slot - 1) % 2)
        slot = (slot - 1) // 2
    for side in reversed(path):
        window = split_window(window)[side]
    return window


def splittable(piece: list[dict], slot: int) -> bool:
    return piece_depth(slot) < LENGTH_SPLIT_DEPTH and (
        len(piece) > 1 or len(piece[0]["content"]) >= 2 * LENGTH_SPLIT_MIN_CHARS
    )


def piece_leaves(splits, slot: int = 0):
    """The unsplit pieces of one window in source order."""
    if slot in splits:
        yield from piece_leaves(splits, 2 * slot + 1)
        yield from piece_leaves(splits, 2 * slot + 2)
    else:
        yield slot


class DependencyDeferred(RuntimeError):
    """A hidden existing statement may change a proposed write."""


class EmbeddingDeferred(RuntimeError):
    """The embedding model is set up but gave no vector now (NOBLIVION-243)."""


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
# NOBLIVION-237 keeps version 1 on purpose: it drops a candidate that fails a
# check of its own values instead of the whole response. Every candidate it
# accepts, the version 1 check accepts with the same result, so old and new pages
# mix safely.
# The neutral mark for a legacy redaction marker changes note data, not a prompt.
# NOBLIVION-241 adds version 2 for new extraction jobs. It needs fewer model
# calls: a window holds WINDOW_SCALE times window_chars, a window with no tool
# part and no direct user part gets a no_lesson receipt with no call, and one
# semantic call compares several candidates. A started job with partial work
# keeps the version it froze, so it resumes with version 1 windows and calls. A
# job frozen under version 1 with no partial work freezes again under the newest
# version when a worker leases it (see LearningWorker._may_refreeze).
# NOBLIVION-243 adds version 3. It has the windows and batches of version 2, but
# compares each add candidate only with its nearest notes and earlier candidates
# (learning_nearest), so the semantic calls of a job no longer grow with the
# note count. A job of version 1 or 2 with partial work keeps its version and
# its windows. NOBLIVION-257: while its semantic stage has no answered
# comparison, it takes the same nearest selection; with an answer it keeps the
# full comparison. Over its task bound with no comparison in the current stage,
# it also takes the nearest selection instead of failing (LearningWorker._bound_stage).
EXTRACTION_CONTRACT = 3
# Releases up to 0.1.24 stored no contract. Their contract is version 1.
LEGACY_EXTRACTION_CONTRACT = 1
# Every version this worker can run. A started job continues under its own one.
SUPPORTED_CONTRACTS = (1, 2, 3)
# The first version that always compares with the nearest targets only.
NEAREST_CONTRACT = 3
# Consolidation and draft jobs use only version 1 text, which version 2 keeps
# unchanged. They freeze version 1, so their started stages keep matching.
DERIVATION_CONTRACT = 1
# Progress keys that show partial work of an extraction job. A job with one of
# them never freezes again under a newer contract.
PARTIAL_WORK = (
    "semantic",
    "semantic_exclusions",
    "deferred_decisions",
    "dependency_deferred",
    "format_repair",
    "output_limit_hold",
    "candidate_mismatches",
    # NOBLIVION-245: split slots address pieces of the frozen windows. Under
    # other windows they would split other text.
    "length_splits",
    "rejected_windows",
)
# Characters per window are window_chars times this factor.
WINDOW_SCALE = {1: 1, 2: 2, 3: 2}
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


_OUTAGE_STATUS = frozenset({429, 502, 503, 504})
_DOWN_ERRNOS = frozenset(
    {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}
)


def _endpoint_unavailable(exc: BaseException) -> bool:
    """True when the endpoint is down, not when it rejected this request.

    A 429, 502, 503 or 504 answer, a refused, reset or unreachable connection,
    and a name that does not resolve are outages (NOBLIVION-253). Any other 4xx
    or 5xx answer and an unexpected protocol error stay "transport", so they
    still count attempts: a request that crashes the server (500) must not get
    the free outage retries.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _OUTAGE_STATUS
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return True
    return isinstance(exc, OSError) and exc.errno in _DOWN_ERRNOS


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
        raw = self._post(
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
            },
            timeout,
        )
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

    def probe(self, *, timeout: float) -> bool:
        """True when the chat completions path answers a one-token request.

        NOBLIVION-255: a call that timed out asks this before the job loses an
        attempt. It uses the same path and model as a real call, not GET /health
        or /v1/models: a server can answer those and still hang on completions.
        Any answer counts, also an error status or a reply cut at one token. Only
        no answer in time or an outage error (see _endpoint_unavailable) is False.
        """
        try:
            self._post(
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Reply with one word."}],
                    "temperature": 0,
                    "max_tokens": 1,
                    "reasoning_effort": self.reasoning_effort,
                },
                timeout,
            )
        except ProviderError as exc:
            return exc.reason not in {"timeout", "unavailable"}
        return True

    def _post(self, payload: dict, timeout: float):
        """POST one request to the completions path; return the parsed JSON body."""
        body = json.dumps(payload).encode()
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
                else "unavailable"
                if _endpoint_unavailable(exc)
                else "transport"
            )
            raise ProviderError(reason) from exc
        finally:
            for conn in getattr(request, "learning_connections", ()):
                conn.cancel_deadline()
        return raw


def batches_digest(batches: list[dict]) -> str:
    """The identity of the semantic batches of one job, in their order."""
    return hashlib.sha256(
        json.dumps(batches, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _apply_relations(candidate: dict, relations: list[dict], notes: list[dict]) -> None:
    """The first relation that is not distinct turns an add into a visible conflict."""
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
        target = next((n for n in notes if str(n["note_id"]) == blocked["target_id"]), None)
        if target is not None:
            candidate["note_id"] = target["note_id"]
            candidate["expected_revision"] = target["revision"]


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
        embedding=None,
    ):
        self.ledger = ledger
        self.provider = provider
        self.settings = settings
        self.budget_limits = budget_limits
        # The store's EmbeddingService, or None: the semantic selection then
        # uses keyword order only (NOBLIVION-243).
        self.embedding = embedding
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
            progress = job.get("progress") or {}
            if self._may_refreeze(job, progress, sources):
                # A job frozen under an older contract that has done no work
                # yet starts again as a new job under the newest contract.
                progress = {key: progress[key] for key in ("provider_metrics",) if key in progress}
                progress["refrozen_from"] = self._job_contract(job["progress"], sources)
                state = dict(progress)
                job["progress"] = progress
            # The contract decides the windows and the request shape, so it is
            # resolved before either is hashed (NOBLIVION-241).
            contract = self._job_contract(progress, sources)
            job["contract"] = contract
            windows = self.windows(sources, contract)
            current = self._configuration(contract)
            execution = input_fingerprint(sources, windows, request_configuration(current))
            started = progress.get("execution_fingerprint", progress.get("fingerprint"))
            if not execution_matches(progress, sources, windows, current):
                if (
                    not self._output_recovery(state, contract)
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
            decisions, conflicting = merge_window_decisions(decisions)
            # The receipts are fixed, so a resume computes the same count.
            if conflicting:
                state["inconsistent_decisions"] = len(conflicting)
            else:
                state.pop("inconsistent_decisions", None)
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
            return self._bounded_defer(ident, lease, "ContextDependencyDeferred")
        except EmbeddingDeferred:
            self.ledger.checkpoint(ident, lease, state)
            error = "SemanticEmbeddingUnavailable"
            outcome = self.ledger.defer(
                ident,
                lease,
                retry_delay=EMBEDDING_RETRY_DELAY,
                error=error,
                cap=MAX_EMBEDDING_DEFERRALS,
                max_attempts=cfg.max_attempts,
                counter=EMBEDDING_COUNTER,
            )
            if outcome == "deferred":
                return {"status": "deferred", "job_id": ident, "error": error}
            return {"status": "failed", "job_id": ident, "error": error, "permanent": False}
        except ProviderError as exc:
            reason = "Provider:" + exc.reason
            if exc.reason == "length":
                # An extraction window splits on length (_extract). This path is
                # for a semantic or derivation call. Its progress is durable:
                # _call checkpoints the output-limit hold before it raises.
                return self._bounded_defer(ident, lease, reason)
            if exc.reason == "unavailable":
                return self._unavailable_defer(ident, lease, reason)
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
            # NOBLIVION-250: a stage whose note set changed keeps its valid
            # comparisons; the next run compares only the new or revised notes.
            # Any other cause (the note set did not change) drops the stage.
            stage = state.get("semantic")
            if not isinstance(stage, dict) or stage.get("snapshot") == self._semantic_snapshot(job):
                state.pop("semantic", None)
            state.pop("derivation", None)
            state.pop("format_repair", None)
            self.ledger.checkpoint(ident, lease, state)
            self.ledger.defer(ident, lease, retry_delay=1, error="SemanticSnapshotChanged")
            return {"status": "deferred", "job_id": ident, "error": "SemanticSnapshotChanged"}
        except ConcurrencyExceeded:
            # Other reserved calls hold every slot, for example the calls of a
            # process that stopped mid-call, until their leases expire. Retry
            # after the poll interval; defer gives the attempt back.
            self.ledger.defer(
                ident, lease, retry_delay=cfg.poll_seconds, error="ConcurrencyExceeded"
            )
            return {"status": "deferred", "job_id": ident, "error": "ConcurrencyExceeded"}
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

    def _bounded_defer(self, ident, lease, error):
        """Defer by one hour; past MAX_RETURNED_DEFERRALS the attempt counts."""
        outcome = self.ledger.defer(
            ident,
            lease,
            retry_delay=3600,
            error=error,
            cap=MAX_RETURNED_DEFERRALS,
            max_attempts=self.settings.max_attempts,
        )
        if outcome == "deferred":
            return {"status": "deferred", "job_id": ident, "error": error}
        return {"status": "failed", "job_id": ident, "error": error, "permanent": False}

    def _unavailable_defer(self, ident, lease, error):
        """The provider is down: wait with a growing delay, keep the attempt.

        NOBLIVION-253. The count is separate from returned_deferrals, so an
        outage does not use up the deferrals of a dependency or length hold.
        The count comes from the stored progress, the same value defer counts
        on: a derivation keeps its own copy of the progress, so the state of
        run_once can be older than the row.
        """
        row = self.ledger.conn.execute(
            "SELECT progress FROM learning_jobs WHERE id=?", (ident,)
        ).fetchone()
        count = json.loads(row[0]).get(UNAVAILABLE_COUNTER, 0) if row else 0
        count = count if type(count) is int and count >= 0 else 0
        delay = min(self.settings.retry_delay * 2 ** min(count, 16), UNAVAILABLE_MAX_DELAY)
        outcome = self.ledger.defer(
            ident,
            lease,
            retry_delay=delay,
            error=error,
            cap=MAX_UNAVAILABLE_DEFERRALS,
            max_attempts=self.settings.max_attempts,
            counter=UNAVAILABLE_COUNTER,
        )
        if outcome == "deferred":
            return {"status": "deferred", "job_id": ident, "error": error}
        return {"status": "failed", "job_id": ident, "error": error, "permanent": False}

    def _output_recovery(self, state, contract=DERIVATION_CONTRACT):
        """Only a recorded length result permits a reviewed provider-limit change."""
        previous = state.get("call_configuration")
        if not state.get("output_limit_hold") or not isinstance(previous, dict):
            return False
        current = self._configuration(contract)

        def policy(configuration):
            # Policy settings and budgets are already outside the request part.
            frozen = request_configuration(configuration)
            frozen.pop("model", None)
            frozen.pop("reasoning_effort", None)
            frozen["settings"].pop("max_output_tokens", None)
            return frozen

        return policy(previous) == policy(current)

    def windows(self, sources, contract=LEGACY_EXTRACTION_CONTRACT):
        cfg = self.settings
        window_chars = min(cfg.window_chars * WINDOW_SCALE[contract], 262144)
        return build_windows(
            sources,
            window_chars=window_chars,
            overlap=min(cfg.overlap_chars, window_chars // 4),
            max_windows=cfg.max_windows,
        )

    def _job_contract(self, progress, sources):
        """The contract of one extraction job: its frozen one, or the newest one.

        A started job runs under the supported contract whose configuration
        reproduces its frozen fingerprint. The stored configuration names the
        first one to try; releases up to 0.1.24 stored none, which means 1. If
        no contract matches, the stored one is returned and the normal check
        fails the job visibly, as before.
        """
        frozen = progress.get("execution_fingerprint", progress.get("fingerprint"))
        if frozen is None:
            return EXTRACTION_CONTRACT
        hint = LEGACY_EXTRACTION_CONTRACT
        for key in ("execution_configuration", "call_configuration"):
            stored = progress.get(key)
            if isinstance(stored, dict):
                hint = stored.get("contract", LEGACY_EXTRACTION_CONTRACT)
                break
        if hint not in SUPPORTED_CONTRACTS:
            # An unknown, newer contract: fail visibly under it, never re-freeze.
            raise ValidationError("unsupported extraction contract")
        for contract in dict.fromkeys((hint, *SUPPORTED_CONTRACTS)):
            try:
                windows = self.windows(sources, contract)
            except ValidationError:
                continue
            if execution_matches(progress, sources, windows, self._configuration(contract)):
                return contract
        return hint

    def _may_refreeze(self, job, progress, sources) -> bool:
        """True when a job frozen under an older contract has no partial work.

        Such a job has no completed window, no page receipt, no semantic stage,
        no decision, no format retry, no output-limit hold and no length split,
        so nothing in it depends on its frozen windows. It may freeze again under the newest
        contract. A job with any partial work keeps its frozen contract, and a
        job whose freeze no longer matches still fails visibly (NOBLIVION-236).
        """
        if progress.get("execution_fingerprint", progress.get("fingerprint")) is None:
            return False
        conn = getattr(self.ledger, "conn", None)
        if conn is None or coverage(conn, job["id"]):
            return False
        if (
            progress.get("next_window", 0) != 0
            or progress.get("next_continuation", 0) != 0
            or progress.get("decisions")
            or any(key in progress for key in PARTIAL_WORK)
        ):
            return False
        try:
            contract = self._job_contract(progress, sources)
        except ValidationError:
            return False
        if contract >= EXTRACTION_CONTRACT:
            return False
        try:
            windows = self.windows(sources, contract)
        except ValidationError:
            return False
        return execution_matches(progress, sources, windows, self._configuration(contract))

    def _configuration(self, contract=DERIVATION_CONTRACT):
        return {
            "contract": contract,
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
        for unit_index in range(state["next_window"], len(windows)):
            window = windows[unit_index]
            # NOBLIVION-245: the frozen plan stays the same. A window that ends on
            # length gets a split recorded in progress, and its pieces go on.
            done = set()
            while True:
                splits = set(state.get("length_splits", {}).get(str(unit_index), []))
                slot = next((s for s in piece_leaves(splits) if s not in done), None)
                if slot is None:
                    break
                if self._extract_piece(
                    job, sources, window, unit_index, slot, existing, state, pages
                ):
                    done.add(slot)
            state["next_window"] = unit_index + 1
            state.pop("next_continuation", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
        return pages.decisions()

    def _extract_piece(self, job, sources, window, unit_index, slot, existing, state, pages):
        """Extract one piece of a window. Return False when the piece was split."""
        from .learning_pages import (
            CONTINUING_REJECTIONS,
            LEGACY_EXHAUSTED,
            LENGTH_REJECTED,
            WINDOW_EXHAUSTED,
            WINDOW_REJECTED,
        )

        cfg = self.settings
        piece = window_piece(window, slot)
        # Slot 0 keeps the cursor and step names of releases before the split.
        continuation = state.get("next_continuation", 0) if slot == 0 else 0
        while True:
            if self.stop is not None and self.stop.is_set():
                raise BudgetExceeded("worker stopping")
            page = slot * PIECE_STRIDE + continuation
            step = (
                f"extract:{unit_index}:{continuation}"
                if slot == 0
                else f"extract:{unit_index}:{slot}:{continuation}"
            )
            receipt = pages.receipt(unit_index, page)
            if receipt is None:
                if pages.count() >= cfg.max_pages:
                    raise CoverageExhausted("extraction page limit reached; coverage incomplete")
                if (
                    continuation == 0
                    and job.get("contract", LEGACY_EXTRACTION_CONTRACT) >= 2
                    and not learnable_window(piece)
                ):
                    # No part of this piece can hold accepted evidence, so a model
                    # call cannot add a note. The receipt still covers the piece.
                    pages.save(
                        unit_index,
                        page,
                        piece,
                        [],
                        "no_lesson",
                        "no tool or direct user text in the window; no model call",
                    )
                    # The resume path below reads the saved receipt and goes on.
                    continue
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
                                "sources": piece,
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
                        step,
                        EXTRACTION_SCHEMA,
                    )
                    outcome = self._validate_window(extraction_outcome(outcome), piece)
                except ProviderError as exc:
                    if exc.reason != "length":
                        raise
                    # The reply was cut off. Nothing of it is kept as a candidate.
                    if splittable(piece, slot):
                        splits = state.setdefault("length_splits", {})
                        splits[str(unit_index)] = sorted(
                            set(splits.get(str(unit_index), [])) | {slot}
                        )
                        # The pieces are new requests: clear the hold, the format
                        # retry and the cursor of the whole window in one write.
                        state.pop("output_limit_hold", None)
                        state.pop("format_repair", None)
                        state.pop("next_continuation", None)
                        self.ledger.checkpoint(job["id"], job["lease_token"], state)
                        return False
                    pages.save(unit_index, page, piece, [], "rejected", LENGTH_REJECTED)
                    # The resume path below reads the saved receipt and goes on.
                    continue
                except ValidationError as exc:
                    record_mismatches(
                        state,
                        unit_index,
                        page,
                        "rejected",
                        proposed=getattr(exc, "proposed", None),
                        accepted=getattr(exc, "accepted", 0),
                        mismatches=getattr(exc, "mismatches", []),
                        error=str(exc),
                    )
                    self.ledger.checkpoint(job["id"], job["lease_token"], state)
                    # Only a RESPONSE_FAILURES class marks the window. A schema
                    # failure or an unclassified failure still fails the job.
                    unsafe = isinstance(exc, ResponseRejected) and str(exc) in RESPONSE_FAILURES
                    pages.save(
                        unit_index,
                        page,
                        piece,
                        [],
                        "rejected",
                        WINDOW_REJECTED
                        if unsafe
                        else "candidate validation failed; no coverage acknowledgement",
                    )
                    if not unsafe:
                        raise
                    # The resume path below reads the saved receipt and
                    # goes on to the next window (NOBLIVION-244).
                    continue
                accepted, proposed = outcome["accepted"], outcome["proposed"]
                if outcome["mismatches"]:
                    record_mismatches(
                        state,
                        unit_index,
                        page,
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
                hard = len(total) > cfg.max_decisions
                # NOBLIVION-247: a continuation that dropped all twelve proposals
                # makes no progress. Only this window stops; the job goes on.
                stalled = not hard and continuation > 0 and saturated and not accepted
                exhausted = (
                    hard
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
                    WINDOW_EXHAUSTED
                    if stalled
                    else LEGACY_EXHAUSTED
                    if exhausted
                    else "twelve candidates require continuation"
                    if status == "saturated"
                    else "provider reported remaining lessons"
                    if status == "processed"
                    else "every candidate failed an evidence check"
                    if outcome["mismatches"]
                    else "provider reported no eligible lesson"
                )
                receipt = pages.save(unit_index, page, piece, accepted, status, reason)
            if receipt["status"] == "rejected" and receipt["reason"] in CONTINUING_REJECTIONS:
                # The whole reply was rejected and none of it is kept. The
                # unit index list stays the same when a resume reads this
                # receipt again.
                rejected = state.setdefault("rejected_windows", [])
                if unit_index not in rejected:
                    rejected.append(unit_index)
                if receipt["reason"] == LENGTH_REJECTED:
                    state.pop("output_limit_hold", None)
            elif receipt["status"] == "exhausted" and self._window_exhausted(receipt, pages):
                # Like a rejected window: the incomplete coverage stays visible
                # in the receipt and in the progress, and the job goes on. The
                # list stays the same when a resume reads this receipt again.
                exhausted_windows = state.setdefault("exhausted_windows", [])
                if unit_index not in exhausted_windows:
                    exhausted_windows.append(unit_index)
            elif receipt["status"] in {"exhausted", "rejected"}:
                raise CoverageExhausted("extraction receipt is incomplete")
            state.pop("format_repair", None)
            if receipt["status"] == "saturated":
                continuation += 1
                if slot == 0:
                    state["next_continuation"] = continuation
                self.ledger.checkpoint(job["id"], job["lease_token"], state)
                continue
            state.pop("next_continuation", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
            return True

    def _window_exhausted(self, receipt, pages):
        """True for an exhausted receipt that ends only its window (NOBLIVION-247).

        A receipt saved before NOBLIVION-247 has the legacy reason for every
        cause. Only the all-dropped continuation has no candidate on a page past
        continuation 0, so that shape alone continues. The hard decision limit
        always ends the job.
        """
        from .learning_pages import LEGACY_EXHAUSTED, WINDOW_EXHAUSTED

        total = {(d["scope"], d["category"], d["statement"].casefold()) for d in pages.decisions()}
        if len(total) > self.settings.max_decisions:
            return False
        if receipt["reason"] == WINDOW_EXHAUSTED:
            return True
        return (
            receipt["reason"] == LEGACY_EXHAUSTED
            and not receipt["candidates"]
            and receipt["continuation"] % PIECE_STRIDE > 0
        )

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
        state["call_configuration"] = self._configuration(job.get("contract", DERIVATION_CONTRACT))
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
            # The provider answered: a later outage starts at retry_delay again,
            # and the checkpoint in finally stores this (NOBLIVION-253).
            state.pop(UNAVAILABLE_COUNTER, None)
            state.pop("unavailable_since", None)
        except ProviderError as exc:
            failure = exc
            if exc.reason == "length":
                state["output_limit_hold"] = call_key
            if exc.reason == "timeout" and response is None and self._provider_hangs(job):
                failure = ProviderError("unavailable")
                raise failure from exc
            raise
        except Exception:
            failure = ProviderError("transport")
            raise failure from None
        finally:
            # The elapsed time includes a probe after a timeout (NOBLIVION-255).
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

    def _provider_hangs(self, job: dict) -> bool:
        """True when a one-token probe gets no answer after a call timed out.

        NOBLIVION-255. The probe runs inside the reservation of the call that
        timed out, so it holds the same call slot and its time is measured into
        that settlement; settle keeps the larger of the reserved and the measured
        values. A second reservation could fail on the budget or on a call slot
        and hide the timeout. The probe adds at most PROBE_TIMEOUT_SECONDS, not
        another timeout_seconds, and its few tokens fall inside the reservation
        of the call. A provider with no probe keeps the timeout as it is.

        A probe with no answer is not enough: a busy server can keep it in its
        queue. If any call of the store got a real answer in the time of one
        call and one probe before the probe began, or during the probe, the
        server is up, and the timeout counts an attempt.
        """
        probe = getattr(self.provider, "probe", None)
        if probe is None:
            return False
        seconds = min(PROBE_TIMEOUT_SECONDS, self.settings.timeout_seconds)
        since = time.time() - self.settings.timeout_seconds - seconds
        # The reservation kept the lease for timeout_seconds + 30 s, and the
        # call used the timeout. Keep the lease for the probe as well.
        self.ledger.renew_lease(job["id"], job["lease_token"], seconds + 30)
        try:
            answered = probe(timeout=seconds)
        except Exception:  # noqa: BLE001 - an unknown probe fault proves no outage
            return False
        return not answered and not self.ledger.provider_answered_since(since)

    def _semantic(
        self, job: dict, decisions: list[dict], context: list[dict], state: dict
    ) -> list[dict]:
        """Compare the managed notes, including zero lexical overlap, before adds.

        Contract 3 compares each add with its nearest notes and earlier adds
        only. Contracts 1 and 2 do the same while their stage has no answered
        comparison; with an answer they compare all of them while they fit
        their task bound (_bound_stage, NOBLIVION-243, NOBLIVION-257).

        A semantic judgment can prevent a write, never authorize a correction.
        Equivalent, contradictory or uncertain candidates become visible conflicts.
        Each completed comparison is durable so budget pauses do not repeat it.
        When another job changes the note set, the valid comparisons stay, and
        only the new or revised notes get new comparisons (NOBLIVION-250).
        """
        if not any(d["action"] == "add" for d in decisions):
            return decisions
        stage = state.get("semantic")
        if stage is None:
            notes, snapshot = self._semantic_notes(job, decisions, context, state)
            stage = {
                "notes": notes,
                "snapshot": snapshot,
                "next_task": 0,
                "decisions": [dict(d) for d in decisions],
            }
            state["semantic"] = stage
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
        max_chars = min(self.settings.max_existing_chars, 16000)
        while True:
            if stage["snapshot"] != self._semantic_snapshot(job):
                stage = self._refresh_semantic(job, decisions, context, state, stage, max_chars)
            self._bound_stage(job, decisions, stage, state, max_chars)
            if job.get("contract", LEGACY_EXTRACTION_CONTRACT) >= 2:
                self._semantic_batched(job, decisions, stage, state, max_chars)
            else:
                self._semantic_round(job, decisions, stage, state, max_chars)
            # The commit checks the snapshot again in its write transaction.
            if stage["snapshot"] == self._semantic_snapshot(job):
                break
        stage["complete"] = True
        self.ledger.checkpoint(job["id"], job["lease_token"], state)
        # Each add carries the snapshot to the commit, but the stored stage
        # keeps it once: one copy per add made the progress of a job with many
        # adds and notes exceed its size limit (NOBLIVION-243).
        return [
            {**c, "semantic_snapshot": stage["snapshot"]} if c["action"] == "add" else c
            for c in stage["decisions"]
            if c["action"] != SEMANTIC_UNVALIDATED
        ]

    def _semantic_snapshot(self, job: dict) -> list:
        """The sorted [note_id, revision] pairs of the current semantic note set."""
        managed = self.ledger.semantic_notes(
            job["project"], job["root"], session_id=job.get("session_id", "")
        )
        return sorted([[n["note_id"], n["revision"]] for n in managed])

    def _semantic_notes(
        self, job: dict, decisions: list[dict], context: list[dict], state: dict
    ) -> tuple[list[dict], list]:
        """Return the bounded comparison notes and the snapshot of all managed notes."""
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
        state["semantic_exclusions"] = exclusions
        dependent = dependent_decisions(decisions, exclusions)
        if dependent:
            state["deferred_decisions"] = dependent
            raise DependencyDeferred()
        return safe_notes, snapshot

    def _refresh_semantic(self, job, decisions, context, state, stage, max_chars):
        """Keep the valid comparisons of a stage whose note set changed (NOBLIVION-250).

        A comparison stays valid while its target note has the same revision. A
        comparison with another candidate always stays valid. A candidate that a
        removed or revised note blocked gets a fresh comparison with all notes,
        because its later tasks were skipped. The new stage covers every current
        note: its tasks are all targets minus the kept comparisons.
        """
        if stage["next_task"] or stage.get("fallback_done"):
            tasks = semantic_targets(
                decisions,
                stage["notes"],
                max_chars=max_chars,
                max_tasks=_max_semantic_tasks(job),
                skip=stage.get("covered"),
                nearest=stage.get("nearest"),
            )
        else:
            # No task has an answer yet, so no task is finished. A stage that
            # exceeds its bound is not built (NOBLIVION-243).
            tasks = []
        done: dict[int, set[str]] = {}
        for key, ids in (stage.get("covered") or {}).items():
            done.setdefault(int(key), set()).update(ids)
        finished = [*tasks[: stage["next_task"]]]
        # Contract 2: tasks answered alone after a rejected batch, before the
        # batch itself counts in next_task.
        for i in stage.get("fallback_done", ()):
            if type(i) is not int or not 0 <= i < len(tasks):
                raise ValidationError("invalid durable semantic progress")
            if i >= stage["next_task"]:
                finished.append(tasks[i])
        for task in finished:
            done.setdefault(task["candidate_index"], set()).update(
                target["target_id"] for target in task["targets"]
            )
        before = {str(n["note_id"]): n.get("revision") for n in stage["notes"]}
        notes, snapshot = self._semantic_notes(job, decisions, context, state)
        after = {str(n["note_id"]): n.get("revision") for n in notes}

        def valid(target_id: str) -> bool:
            if target_id not in before:
                return target_id.startswith("candidate:")
            return target_id in after and after[target_id] == before[target_id]

        results = stage["decisions"]
        covered, reused = {}, 0
        for index, decision in enumerate(decisions):
            if decision["action"] != "add":
                continue
            result = results[index]
            if result["action"] == "conflict":
                review = result.get("semantic_review") or {}
                if not valid(str(review.get("target_id"))):
                    results[index] = dict(decision)
                continue
            if result["action"] != "add":
                continue  # not validated: never stored, no new comparison
            kept = sorted(target for target in done.get(index, ()) if valid(target))
            if kept:
                covered[str(index)] = kept
                reused += len(kept)
        fresh = {"notes": notes, "snapshot": snapshot, "next_task": 0, "decisions": results}
        if covered:
            fresh["covered"] = covered
        if stage.get("rejected"):
            fresh["rejected"] = stage["rejected"]
        if stage.get("nearest_upgrade"):
            # An old-contract stage that took the nearest selection selects
            # again for the new note set; its answers never bring back the
            # full comparison (NOBLIVION-257).
            fresh["nearest_upgrade"] = stage["nearest_upgrade"]
        state["semantic"] = fresh
        state["semantic_restarts"] = state.get("semantic_restarts", 0) + 1
        state["semantic_reused"] = state.get("semantic_reused", 0) + reused
        # Task numbers start again, so a format retry of an old task is void.
        state.pop("format_repair", None)
        self.ledger.checkpoint(job["id"], job["lease_token"], state)
        return fresh

    def _bound_stage(self, job, decisions, stage, state, max_chars) -> None:
        """Store the nearest-target selection of a stage that needs one (NOBLIVION-243).

        Contract 3 selects the nearest targets of each add candidate once per
        stage, before the first comparison.

        Contracts 1 and 2 keep their windows and stored extraction answers.
        NOBLIVION-257: a stage of theirs with no answered comparison, also none
        that a refresh kept (_stage_answered), takes the nearest selection, so
        its calls no longer grow with the note count. The stage records this in
        ``nearest_upgrade``, and a refresh keeps that mark, so the job never
        mixes the two selections. A stage with an answer keeps the full
        comparison while it fits the task bound. Over the bound, with no answer
        in the current stage, it takes the nearest selection instead of failing
        (as in 0.1.34). The smaller selection is used when the first exceeds
        the bound. The selection is stored, so a resume builds the same tasks.
        """
        if "nearest" in stage:
            return
        contract = job.get("contract", LEGACY_EXTRACTION_CONTRACT)
        if contract >= NEAREST_CONTRACT:
            if _stage_started(stage):
                raise ValidationError("invalid durable semantic selection")
            vectors, mode = self._semantic_vectors(job, decisions, stage["notes"])
            stage["nearest"] = nearest_targets(decisions, stage["notes"], vectors)
            stage["selection"] = mode
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
            return
        if _stage_started(stage):
            # Reachable: contract 2 batch packing is not monotone, so no selection
            # may fit 512 batches where the full one does; a refreshed stage then
            # runs the full one.
            if stage.get("nearest_upgrade"):
                raise ValidationError("invalid durable semantic selection")
            return
        upgrade = bool(stage.get("nearest_upgrade")) or not _stage_answered(stage, decisions, state)
        full = semantic_targets(
            decisions,
            stage["notes"],
            max_chars=max_chars,
            max_tasks=UNBOUNDED_TASKS,
            skip=stage.get("covered"),
        )
        over = _over_bound(job, full, max_chars)
        if not (upgrade or over):
            return
        vectors, mode = self._semantic_vectors(job, decisions, stage["notes"])
        for notes_limit, candidates_limit in FALLBACK_SELECTIONS:
            selection = nearest_targets(
                decisions,
                stage["notes"],
                vectors,
                notes_limit=notes_limit,
                candidates_limit=candidates_limit,
            )
            tasks = semantic_targets(
                decisions,
                stage["notes"],
                max_chars=max_chars,
                max_tasks=UNBOUNDED_TASKS,
                skip=stage.get("covered"),
                nearest=selection,
            )
            if not _over_bound(job, tasks, max_chars):
                stage["nearest"] = selection
                stage["selection"] = mode
                # Contract 2 binds next_batch to the digest of its batches
                # before the first batch call. No batch has an answer here, so
                # the digest of the new batches replaces it.
                stage.pop("batch_digest", None)
                if upgrade:
                    stage["nearest_upgrade"] = [notes_limit, candidates_limit]
                if over:
                    stage["bound_fallback"] = [notes_limit, candidates_limit]
                self.ledger.checkpoint(job["id"], job["lease_token"], state)
                return
        # Even the smaller selection exceeds the bound: the round fails visibly.

    def _semantic_vectors(self, job, decisions, notes):
        """Normalized vectors for the nearest selection, and the selection mode.

        A note uses its stored vector of the current model. A note with none
        yet, and each add candidate, is embedded now. No embedding service, a
        service with no model or a failed one, and a remote model that may not
        receive transcript-derived text all mean keyword order (None). A model
        that is set up but gives no vector now defers the job (EmbeddingDeferred).
        """
        from .embedding import STATE_FAILED, STATE_NONE

        service = self.embedding
        if service is None or getattr(service, "state", STATE_NONE) in (STATE_NONE, STATE_FAILED):
            return None, MODE_KEYWORD
        embedder = getattr(service, "embedder", None)
        if embedder is not None and embedder.remote and not service.include_mined:
            return None, MODE_KEYWORD
        model = service.model_id
        if model is None:
            raise EmbeddingDeferred()
        vectors = self.ledger.note_vectors(job["project"], [n["note_id"] for n in notes], model)

        def embed(text: str) -> list[float]:
            # Many embedding calls can outlast the lease of the job; keep it
            # alive for each call. renew_lease never shortens a lease.
            self.ledger.renew_lease(job["id"], job["lease_token"], EMBEDDING_TIMEOUT_S + 30)
            vector = service.embed_query(text, EMBEDDING_TIMEOUT_S, self.ledger.conn)
            if vector is None or service.model_id != model:
                raise EmbeddingDeferred()
            return vector

        for note in notes:
            if str(note["note_id"]) not in vectors:
                vectors[str(note["note_id"])] = embed(note["statement"])
        for index, decision in enumerate(decisions):
            if decision["action"] == "add":
                vectors[candidate_key(index)] = embed(decision["statement"])
        return vectors, MODE_HYBRID

    def _semantic_round(self, job, decisions, stage, state, max_chars) -> None:
        """Run the open tasks of the stage; each finished task is durable."""
        tasks = semantic_targets(
            decisions,
            stage["notes"],
            max_chars=max_chars,
            max_tasks=_max_semantic_tasks(job),
            skip=stage.get("covered"),
            nearest=stage.get("nearest"),
        )
        results = stage["decisions"]
        for index in range(stage["next_task"], len(tasks)):
            task = tasks[index]
            candidate = results[task["candidate_index"]]
            if candidate["action"] == "add":
                if self.stop is not None and self.stop.is_set():
                    raise BudgetExceeded("worker stopping")
                relations = self._semantic_answer(job, task, index, stage, state)
                if relations is None:
                    # NOBLIVION-240: no validated comparison, so the candidate is
                    # never stored. Its later tasks are skipped, and the job goes
                    # on. Only its index and a fixed class are kept.
                    candidate["action"] = SEMANTIC_UNVALIDATED
                    stage.setdefault("rejected", []).append(task["candidate_index"])
                else:
                    _apply_relations(candidate, relations, stage["notes"])
            stage["next_task"] = index + 1
            state.pop("format_repair", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)

    def _semantic_answer(self, job, task, index, stage, state):
        """Return validated relations, or None past the bounded re-asks.

        stage["reasks"] counts the failed answers of each task. The count is
        saved before the next request, so a resume sends that re-ask, not the
        first request again. A crash during a call sends the same call again,
        as for every other call. Every call goes through _call, so the budget
        charges it. Other failures (an unsafe answer, a format retry that fails
        again) still fail the job.
        """
        reasks = stage.setdefault("reasks", {})
        while True:
            record = reasks.get(str(index), {"failures": 0, "error": ""})
            failures = record["failures"]
            if type(failures) is not int or not 0 <= failures <= MAX_SEMANTIC_REASKS + 1:
                raise ValidationError("invalid durable semantic re-ask")
            if failures > MAX_SEMANTIC_REASKS:
                return None
            prompt = SEMANTIC_PROMPT
            step = f"semantic:{index}"
            if failures:
                prompt += _SEMANTIC_REASK.format(error=record["error"], attempt=failures + 1)
                step += f":reask:{failures}"
            try:
                return self._validated_call(
                    job,
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(task, ensure_ascii=False)},
                    ],
                    lambda raw, task=task: validate_semantic_response(raw, task),
                    state,
                    step,
                    SEMANTIC_SCHEMA,
                )
            except SemanticAnswerRejected as exc:
                reasks[str(index)] = {"failures": failures + 1, "error": str(exc)}
                # The next request is new: renew its one format retry.
                state.pop("format_repair", None)
                self.ledger.checkpoint(job["id"], job["lease_token"], state)

    def _semantic_batched(self, job, decisions, stage, state, max_chars):
        """Contract 2: the same comparisons as contract 1, several per call.

        The batches come from the same tasks in the same order, so every
        (candidate, target) pair of contract 1 is still judged. A batch whose
        candidates are all blocked already makes no call, as in contract 1.
        """
        tasks = semantic_targets(
            decisions,
            stage["notes"],
            max_chars=max_chars,
            max_tasks=_max_semantic_tasks(job),
            skip=stage.get("covered"),
            nearest=stage.get("nearest"),
        )
        batches = semantic_batches(tasks, max_chars=max_chars)
        if len(batches) > 512:
            raise ValidationError("semantic reconciliation exceeds bounded task count")
        results = stage["decisions"]
        # The digest binds next_batch to the batches it counts. A resume that
        # builds other batches, for example after a change of the batch limits,
        # fails visibly instead of skipping or repeating comparisons. Progress
        # from before the digest has none; it resumes and gets one now.
        digest = batches_digest(batches)
        stored = stage.get("batch_digest")
        if stored is None:
            stage["batch_digest"] = digest
            self.ledger.checkpoint(job["id"], job["lease_token"], state)
        elif stored != digest:
            raise ValidationError("semantic batches changed during a job")
        start = stage.get("next_batch", 0)
        if type(start) is not int or not 0 <= start <= len(batches):
            raise ValidationError("invalid durable semantic progress")
        offset = 0
        for index in range(len(batches)):
            if index < start:
                offset += len(batches[index]["items"])
                continue
            batch = batches[index]
            # Only the tasks whose candidate is still an add are sent. The
            # batches hold consecutive tasks, so offset is the global index of
            # the first task of this batch.
            live = [
                offset + position
                for position, item in enumerate(batch["items"])
                if results[item["candidate_index"]]["action"] == "add"
            ]
            offset += len(batch["items"])
            if live:
                if self.stop is not None and self.stop.is_set():
                    raise BudgetExceeded("worker stopping")
                if len(live) < len(batch["items"]):
                    # A subset of one batch always fits in one batch.
                    (batch,) = semantic_batches(
                        [tasks[i] for i in live], max_chars=max_chars, max_items=len(live)
                    )
                answers = self._semantic_batch_answer(job, batch, index, stage, state)
                if answers is None:
                    # Past the bounded re-asks of the batch, each task is asked
                    # alone, as in contract 1. So one bad item of a batch answer
                    # cannot drop the other candidates of the batch.
                    self._semantic_batch_fallback(job, tasks, live, stage, state)
                else:
                    for item, relations in zip(batch["items"], answers):  # noqa: B905 - 3.9 lane
                        candidate = results[item["candidate_index"]]
                        # A candidate split over several tasks keeps its first conflict.
                        if candidate["action"] == "add":
                            _apply_relations(candidate, relations, stage["notes"])
            stage["next_batch"] = index + 1
            # A refresh after a peer commit reads next_task: the batches hold
            # consecutive tasks, so this counts the tasks already compared.
            stage["next_task"] = sum(len(b["items"]) for b in batches[: index + 1])
            state.pop("format_repair", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)

    def _semantic_batch_fallback(self, job, tasks, live, stage, state):
        """Ask each still-add task of a rejected batch alone, as in contract 1.

        stage["reasks"] keeps the bounded re-asks of each task under its global
        index, and stage["fallback_done"] the tasks already answered, so a
        resume does not ask a finished task again. A task past its own bound
        is not stored (NOBLIVION-240).
        """
        results = stage["decisions"]
        done = stage.setdefault("fallback_done", [])
        if not isinstance(done, list) or any(type(i) is not int for i in done):
            raise ValidationError("invalid durable semantic progress")
        for task_index in live:
            candidate = results[tasks[task_index]["candidate_index"]]
            if task_index in done or candidate["action"] != "add":
                continue
            if self.stop is not None and self.stop.is_set():
                raise BudgetExceeded("worker stopping")
            relations = self._semantic_answer(job, tasks[task_index], task_index, stage, state)
            if relations is None:
                candidate["action"] = SEMANTIC_UNVALIDATED
                stage.setdefault("rejected", []).append(tasks[task_index]["candidate_index"])
            else:
                _apply_relations(candidate, relations, stage["notes"])
            done.append(task_index)
            state.pop("format_repair", None)
            self.ledger.checkpoint(job["id"], job["lease_token"], state)

    def _semantic_batch_answer(self, job, batch, index, stage, state):
        """Return validated answers for one batch, or None past the bounded re-asks.

        The same rules as _semantic_answer, with its own durable count in
        stage["batch_reasks"], keyed by the batch index.
        """
        reasks = stage.setdefault("batch_reasks", {})
        while True:
            record = reasks.get(str(index), {"failures": 0, "error": ""})
            failures = record["failures"]
            if type(failures) is not int or not 0 <= failures <= MAX_SEMANTIC_REASKS + 1:
                raise ValidationError("invalid durable semantic re-ask")
            if failures > MAX_SEMANTIC_REASKS:
                return None
            prompt = SEMANTIC_BATCH_PROMPT
            step = f"semantic-batch:{index}"
            if failures:
                prompt += _SEMANTIC_BATCH_REASK.format(error=record["error"], attempt=failures + 1)
                step += f":reask:{failures}"
            try:
                return self._validated_call(
                    job,
                    [
                        {"role": "system", "content": prompt},
                        {
                            "role": "user",
                            "content": json.dumps(batch["request"], ensure_ascii=False),
                        },
                    ],
                    lambda raw, batch=batch: validate_semantic_batch(raw, batch),
                    state,
                    step,
                    SEMANTIC_BATCH_SCHEMA,
                )
            except SemanticAnswerRejected as exc:
                reasks[str(index)] = {"failures": failures + 1, "error": str(exc)}
                state.pop("format_repair", None)
                self.ledger.checkpoint(job["id"], job["lease_token"], state)

    def run_forever(self, stop: Event) -> None:
        """Keep independent jobs alive after one failure; shutdown is bounded."""
        self.stop = stop
        backoff = 0.0
        while not stop.is_set():
            try:
                result = self.run_once()
            except Exception as exc:
                # A ledger outage is not permission to bypass accounting.
                LOG.warning("learning pass failed: %s", type(exc).__name__)
                result = {"status": "failed"}
            if result.get("error") == "Provider:unavailable":
                # NOBLIVION-253: do not claim the next job from a provider that is down.
                backoff = min(max(self.settings.retry_delay, backoff * 2), UNAVAILABLE_MAX_DELAY)
                stop.wait(backoff)
                continue
            if provider_answered(result):
                backoff = 0.0
            if result["status"] != "done":
                stop.wait(self.settings.poll_seconds)
