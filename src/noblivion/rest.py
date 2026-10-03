# SPDX-License-Identifier: AGPL-3.0-or-later
"""The REST routes of the store (design doc 0001, section 4).

``Handler`` is a stdlib ``BaseHTTPRequestHandler``. It runs the request checks
of section 4.1 (``Host``, token, ``Content-Type``, ``Content-Length``, size
caps) and then one route. The answer shapes are built by plain functions, so
tests can check them without a socket. The handler reaches the store through
``self.server.store`` (see ``noblivion.store.Store``).
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import sqlite3
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from noblivion import db, redaction, trust
from noblivion.launcher import NONCE_RE

if TYPE_CHECKING:
    from noblivion.ranking import Hit, RankResult
    from noblivion.store import Store

log = logging.getLogger("noblivion.store")

MAX_BODY_BYTES = 262144  # section 4.1
MAX_QUERY_BYTES = 8192
MAX_Q_CHARS = 2000
ANSWER_CAP_BYTES = 512 * 1024  # section 4.2: every answer stays below 512 KB
SOCKET_TIMEOUT_S = 10.0

NO_MEMORIES = "No memories available."
INTERNAL_REASON = "index or fetch failed; see the daemon log"  # section 4: fixed text
NOT_FOUND_REASON = "no memory with that id in this namespace"
BAD_ID_REASON = "id is not a number"
ENTRY_SEPARATOR = "\n---\n"
TITLE_CHARS = 120
SUMMARY_CHARS = 180
TRUST_PRIOR_MD = trust.TRUST_PRIOR_MD

SEARCH_PATH = "/api/memories/search"
INDEX_PATH = "/api/memories/index"
FETCH_PREFIX = "/api/memories/fetch/"
FEEDBACK_PATH = "/api/memory/feedback/batch"
TRUST_REPORT_PATH = "/api/memory/trust/report"
HEALTH_PATHS = ("/health", "/api/health")
GET_ROUTES = (SEARCH_PATH, INDEX_PATH, TRUST_REPORT_PATH)

_SOURCE_RE = re.compile(r"^\[claude_code_md: ([^\]\n]+)\]$")
_MARKER_RE = re.compile(r"^\[(?:claude_code_md|transcript_mined): [^\]\n]*\]$")
_ID_RE = re.compile(r"[0-9]+")
MAX_ROW_ID = 2**63 - 1


class RequestError(Exception):
    """A request refused as a whole: ``{"detail": <text>}`` with ``status``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# -- small parsers ----------------------------------------------------------------


def first(params: Mapping[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def clamp_int(raw: str, default: int, low: int, high: int) -> int:
    """An int query value clamped to ``low..high``; a non-number reads as ``default``."""
    try:
        number = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return max(low, min(high, number))


def namespace_of(params: Mapping[str, list[str]], default: str) -> str:
    """The ``project`` the client sent, else the configured namespace (section 4.1)."""
    return first(params, "project").strip() or default


def root_of(params: Mapping[str, list[str]]) -> str | None:
    root = first(params, "root").strip()
    return root or None


def _clean(text: str, limit: int) -> str:
    text = " ".join(redaction.redact_at_rest(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def title_and_summary(content: str, memory_id: int) -> tuple[str, str]:
    """Section 4.3: the first and the next non-empty line that is not a source marker."""
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not _MARKER_RE.match(line.strip())
    ]
    title = lines[0] if lines else ""
    if title.startswith("# "):
        title = title[2:]
    title = _clean(title, TITLE_CHARS) if title else ""
    summary = _clean(lines[1], SUMMARY_CHARS) if len(lines) > 1 else ""
    return title or f"memory {memory_id}", summary


def source_of(content: str) -> str | None:
    """The memory file path from the ``[claude_code_md: <path>]`` marker, else None."""
    for line in content.splitlines():
        match = _SOURCE_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _json_len(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


# -- answers ----------------------------------------------------------------------


def search_answer(result: RankResult | None, namespace: str) -> dict:
    """Section 4.2: one joined text, each entry redacted on its own, below 512 KB."""
    entries: list[str] = []
    budget = ANSWER_CAP_BYTES - 4096  # room for the keys and the namespace
    used = 0
    sep_len = _json_len(ENTRY_SEPARATOR)
    for hit in result.hits if result is not None else []:
        text = redaction.redact_at_rest(hit.row.content)
        if text == redaction.REDACTION_FAILED_TOKEN:
            continue
        size = _json_len(text) + (sep_len if entries else 0)
        if used + size > budget:
            break  # entries that do not fit are left out from the end
        entries.append(text)
        used += size
    joined = ENTRY_SEPARATOR.join(entries) if entries else NO_MEMORIES
    return {"results": [joined], "namespace": namespace}


def index_row(hit: Hit, trust: Mapping[int, tuple[float, int]] | None, prior_mined: float) -> dict:
    row = hit.row
    title, summary = title_and_summary(row.content, row.id)
    item: dict[str, object] = {
        "rank": hit.rank,
        "id": row.id,
        "title": title,
        "summary": summary,
        "score": hit.score,
        "fusion_score": hit.fusion_score,
        "source": source_of(row.content),
        "source_type": row.source_type,
    }
    if trust is not None:
        rollup = trust.get(row.id)
        item["trust"] = round(rollup[0], 4) if rollup else None
        item["trials"] = rollup[1] if rollup else None
        item["trust_prior"] = prior_mined if row.source_type == db.SOURCE_MINED else TRUST_PRIOR_MD
    return item


def read_trust(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[int, tuple[float, int]]:
    """The rollup rows of ``ids``. Read only; ``noblivion.trust`` writes them."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT memory_id, trust_score, trials FROM feedback WHERE memory_id IN ({marks})",
        tuple(ids),
    ).fetchall()
    return {int(r[0]): (float(r[1]), int(r[2])) for r in rows}


def index_answer(
    result: RankResult | None,
    namespace: str,
    *,
    query: str,
    mode: str,
    trust: Mapping[int, tuple[float, int]] | None = None,
    prior_mined: float = 0.3,
) -> dict:
    """Section 4.3."""
    answer: dict[str, object] = {"namespace": namespace, "reason": None, "mode": mode}
    if result is None:
        answer.update(reason=INTERNAL_REASON, results=[])
        return answer
    answer["mode"] = result.mode
    if not query.strip() or result.pool_size == 0:
        answer.update(reason=NO_MEMORIES, results=[])
        return answer
    answer["results"] = [index_row(hit, trust, prior_mined) for hit in result.hits]
    return answer


def fetch_answer(conn: sqlite3.Connection | None, raw_id: str, namespace: str) -> dict:
    """Section 4.4. Archived, deleted and other-namespace rows are not found."""
    if not _ID_RE.fullmatch(raw_id):
        return {
            "namespace": namespace,
            "id": raw_id,
            "title": "",
            "source": None,
            "text": "",
            "reason": BAD_ID_REASON,
        }
    memory_id = int(raw_id)
    missing = {
        "namespace": namespace,
        "id": memory_id,
        "title": "",
        "source": None,
        "text": "",
        "reason": NOT_FOUND_REASON,
    }
    if conn is None:
        return dict(missing, reason=INTERNAL_REASON)
    if memory_id > MAX_ROW_ID:
        return missing
    row = conn.execute(
        "SELECT id, content FROM memories WHERE id = ? AND project = ? "
        "AND archived_at IS NULL AND deleted_at IS NULL",
        (memory_id, namespace),
    ).fetchone()
    if row is None:
        return missing
    content = str(row["content"])
    text = redaction.redact_at_rest(content)
    title, _ = title_and_summary(content, memory_id)
    return {
        "namespace": namespace,
        "id": memory_id,
        "title": title,
        "source": source_of(content),
        "text": text,
        "reason": None,
    }


# -- the handler ------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """One request. ``self.server.store`` is the running ``Store``."""

    server_version = "noblivion"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_S  # a client that stalls cannot hold a thread forever

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    # The stdlib logs the request line with its query text to stderr. Logs hold
    # no query text (section 15.3), so log the method, the path and the status.
    def log_request(self, code: object = "-", size: object = "-") -> None:
        path = urlsplit(getattr(self, "path", "") or "").path
        log.debug("%s %s %s", getattr(self, "command", "-"), path[:120], code)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.debug("http: %s", "message suppressed")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._send(code, {"detail": "bad request" if code < 500 else "not implemented"})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # A CORS preflight gets no allow headers (section 4.1).
        self._dispatch("OPTIONS")

    # -- plumbing

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        store = self.store
        store.request_started()
        try:
            status, body = self._route(method)
        except RequestError as exc:
            status, body = exc.status, {"detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 - never leak the message to the client
            log.error("request failed: %s", type(exc).__name__)
            status, body = 503, {"detail": "store error"}
        finally:
            store.request_finished()
        self._send(status, body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.store.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self) -> bool:
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return False
        return hmac.compare_digest(given.strip().encode("utf-8"), self.store.token.encode())

    def _json_body(self) -> dict:
        """The section 4.1 checks of a POST, then the JSON object."""
        media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise RequestError(415, "content type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(411, "Content-Length is required")
        try:
            length = int(raw_length.strip())
        except ValueError:
            raise RequestError(400, "bad Content-Length") from None
        if length < 0:
            raise RequestError(400, "bad Content-Length")
        if length > MAX_BODY_BYTES:
            raise RequestError(413, "body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RequestError(400, "body is not valid JSON") from None
        if not isinstance(body, dict):
            raise RequestError(400, "body must be a JSON object")
        return body

    def _route(self, method: str) -> tuple[int, dict]:
        if not self._host_ok():
            return 421, {"detail": "bad host"}
        split = urlsplit(self.path)
        if len(split.query.encode("utf-8")) > MAX_QUERY_BYTES:
            return 414, {"detail": "query string too long"}
        params = parse_qs(split.query, keep_blank_values=True)
        path = split.path

        if path in HEALTH_PATHS:
            if method != "GET":
                return 405, {"detail": "method not allowed"}
            nonce = first(params, "nonce")
            return 200, self.store.health(
                nonce=nonce if NONCE_RE.fullmatch(nonce) else None, full=self._token_ok()
            )

        if not self._token_ok():
            return 401, {"detail": "missing or wrong token"}

        if path == FEEDBACK_PATH:
            if method != "POST":
                return 405, {"detail": "method not allowed"}
            return self._feedback(self._json_body())
        if path in GET_ROUTES or path.startswith(FETCH_PREFIX):
            if method != "GET":
                return 405, {"detail": "method not allowed"}
            if path == SEARCH_PATH:
                return 200, self._search(params)
            if path == INDEX_PATH:
                return 200, self._index(params)
            if path == TRUST_REPORT_PATH:
                return self._trust_report(params)
            raw_id = unquote(path[len(FETCH_PREFIX) :])
            if "/" not in raw_id:
                return 200, self._fetch(raw_id, params)
        return 404, {"detail": "not found"}

    # -- routes

    def _search(self, params: Mapping[str, list[str]]) -> dict:
        store = self.store
        namespace = namespace_of(params, store.namespace)
        query = first(params, "q")[:MAX_Q_CHARS]
        top_k = clamp_int(first(params, "top_k"), 5, 1, 50)
        result = store.rank(query, namespace, top_k=top_k, root=root_of(params))
        answer = search_answer(result, namespace)
        if result is None:
            answer["reason"] = INTERNAL_REASON
        return answer

    def _index(self, params: Mapping[str, list[str]]) -> dict:
        store = self.store
        namespace = namespace_of(params, store.namespace)
        query = first(params, "q")[:MAX_Q_CHARS]
        top_k = clamp_int(first(params, "top_k"), 35, 1, 200)
        include_mined = first(params, "include_mined").strip() == "1"
        result = store.rank(
            query, namespace, top_k=top_k, root=root_of(params), include_mined=include_mined
        )
        trust = None
        if result is not None and store.store_settings.trust_ranking in ("shadow", "on"):
            trust = store.read_trust([hit.row.id for hit in result.hits])
        return index_answer(
            result,
            namespace,
            query=query,
            mode=store.mode(),
            trust=trust,
            prior_mined=store.store_settings.prior_mined,
        )

    def _fetch(self, raw_id: str, params: Mapping[str, list[str]]) -> dict:
        store = self.store
        namespace = namespace_of(params, store.namespace)
        try:
            with store.connection() as conn:
                return fetch_answer(conn, raw_id, namespace)
        except (sqlite3.Error, OSError) as exc:
            log.error("fetch failed: %s", type(exc).__name__)
            return fetch_answer(None, raw_id, namespace)

    def _feedback(self, body: dict) -> tuple[int, dict]:
        """Section 4.5. ``persona`` and ``project`` in the body or query are ignored."""
        try:
            batch = trust.parse_batch(body)
        except trust.BatchRefused as exc:
            return exc.status, {"detail": exc.detail}
        counts = self.store.ingest_feedback(batch)
        if counts is None:
            return 503, {"detail": "memory feedback store failed"}
        return 200, counts

    def _trust_report(self, params: Mapping[str, list[str]]) -> tuple[int, dict]:
        """Section 4.6."""
        persona = first(params, "persona", "claude_code").strip() or "claude_code"
        if persona != trust.PERSONA:
            return 400, {"detail": "the trust report serves persona claude_code only"}
        answer = self.store.trust_report()
        if answer is None:
            return 503, {"detail": "trust report unavailable"}
        return 200, answer
