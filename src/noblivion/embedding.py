# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding backends, consent and the vector backfill job (design doc 0001, section 7).

Backends (section 7.1):

- ``fastembed`` (default): ``BAAI/bge-small-en-v1.5``, 384 dims, ONNX on the
  CPU. Needs the optional extra ``noblivion[embed]``.
- ``ollama``: ``POST <ollama_url>/api/embed``. A non-loopback URL needs
  ``embedding.allow_remote = true`` and the embeddings consent.
- ``openrouter``: ``POST https://openrouter.ai/api/v1/embeddings``. It sends
  memory text and query text off the machine, so it needs the embeddings
  consent in ``meta.embed_consent``. Only ``noblivion consent embeddings``
  writes that consent.
- ``none``: keyword-only ranking.

This module needs no numpy. Vectors are packed as float32 little-endian with
the stdlib ``array`` module. Only the fastembed backend imports third-party
code, and only when it loads.

The ``vectors.model`` column holds the model id ``<backend>:<model>``, so two
backends that use one model name never share vectors.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from noblivion import config, db, redaction

log = logging.getLogger("noblivion.store")
REASON_LOG_CHARS = 300
BACKENDS = ("fastembed", "ollama", "openrouter", "none")
DEFAULT_BACKEND = "fastembed"
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

BATCH_SIZE = 64
HTTP_TIMEOUT_S = 30.0
QUERY_TIMEOUT_S = 1.0
RETRY_AFTER_S = 3600.0
# The default model reads 512 tokens. A cut by characters bounds the request
# size for every backend; the model cuts the rest itself.
MAX_EMBED_CHARS = 8000

CONSENT_VERSION = 1
CONSENT_KEY = "embed_consent"

# The query instruction of the BGE v1.5 English models (section 7.2: "the
# query is embedded with the model's query prefix if the model defines one").
QUERY_PREFIXES = {
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
}

STATE_NONE = "none"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_REEMBEDDING = "reembedding"
STATE_FAILED = "failed"

_MARKER_LINE_RE = re.compile(r"^\[(?:claude_code_md|transcript_mined): [^\]\n]*\]$")


class EmbeddingError(RuntimeError):
    """A backend could not load or could not embed."""


class ConsentRequiredError(EmbeddingError):
    """A backend that sends text off the machine has no consent."""


# -- settings -----------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingSettings:
    backend: str = DEFAULT_BACKEND
    model: str = DEFAULT_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    allow_remote: bool = False
    remote_include_mined: bool = False
    allow_download: bool = False
    models_dir: Path | None = None

    @property
    def model_id(self) -> str:
        return f"{self.backend}:{self.model}"


def load_embedding_settings(env: Mapping[str, str] | None = None) -> EmbeddingSettings:
    """Section 12.3 keys ``embedding.*``. A bad backend name falls back to the default."""
    env = os.environ if env is None else env
    cfg = config.load_file(env)

    def pick(env_key: str | None, dotted: str, default: str) -> str:
        if env_key and env.get(env_key, "").strip():
            return env.get(env_key, "").strip()
        value = config.lookup(cfg, dotted)
        return value.strip() if isinstance(value, str) and value.strip() else default

    backend = pick("NOBLIVION_EMBED_BACKEND", "embedding.backend", DEFAULT_BACKEND).lower()
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    return EmbeddingSettings(
        backend=backend,
        model=pick("NOBLIVION_EMBED_MODEL", "embedding.model", DEFAULT_MODEL),
        ollama_url=pick("NOBLIVION_OLLAMA_URL", "embedding.ollama_url", DEFAULT_OLLAMA_URL),
        allow_remote=config.parse_switch(config.lookup(cfg, "embedding.allow_remote"), False),
        remote_include_mined=config.parse_switch(
            config.lookup(cfg, "embedding.remote_include_mined"), False
        ),
        allow_download=config.parse_switch(config.lookup(cfg, "embedding.allow_download"), False),
        models_dir=config.data_dir(env) / "models",
    )


# -- text and vectors -----------------------------------------------------------


def embed_text(content: str) -> str:
    """The text of a row that is embedded: the content without the source
    marker line, cut to ``MAX_EMBED_CHARS`` (section 7.2)."""
    kept = [line for line in content.split("\n") if not _MARKER_LINE_RE.match(line.strip())]
    return "\n".join(kept).strip()[:MAX_EMBED_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(vector: Sequence[float]) -> list[float]:
    """L2-normalise. A zero or non-finite vector raises ``EmbeddingError``."""
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    if not values or not math.isfinite(norm) or norm == 0.0:
        raise EmbeddingError("the backend returned an empty, zero or non-finite vector")
    return [x / norm for x in values]


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """float32 little-endian, L2-normalised (section 6.2)."""
    packed = array("f", normalize(vector))
    if sys.byteorder == "big":
        packed.byteswap()
    return packed.tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    packed = array("f")
    packed.frombytes(blob)
    if sys.byteorder == "big":
        packed.byteswap()
    return list(packed)


# -- outbound scrub (section 10.2) ----------------------------------------------

_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# A run of hex digits and colons with at least two colons; ``ipaddress``
# decides whether it is an IPv6 literal (so a time like 12:30:45 is kept).
_IPV6_CANDIDATE_RE = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f]*:[0-9A-Fa-f:]*:[0-9A-Fa-f]*")


def _ipv6_or_keep(match: re.Match[str]) -> str:
    try:
        ipaddress.IPv6Address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<ip>"


def scrub_outbound(text: str, *, home: str | None = None, user: str | None = None) -> str:
    """Secret redactor, then the outbound scrub: the home folder becomes ``~``,
    the user name becomes ``<user>``, IPv4 and IPv6 literals become ``<ip>``."""
    out = redaction.redact_at_rest(text)
    home = os.path.expanduser("~") if home is None else home
    if home and home not in ("/", "~"):
        out = out.replace(home.rstrip("/"), "~")
    if user is None:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if user and len(user) >= 3:
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", "<user>", out)
    out = _IPV4_RE.sub("<ip>", out)
    return _IPV6_CANDIDATE_RE.sub(_ipv6_or_keep, out)


# -- consent --------------------------------------------------------------------

CONSENT_TEXT = """\
NOBLIVION embeddings consent (version {version})

Backend: {provider}, model: {model}

With this backend NOBLIVION sends text off this machine:
- the text of every memory file in the store (not transcript-mined rows,
  unless embedding.remote_include_mined is true);
- the text of every prompt you send to Claude Code, as the search query.

Before sending, each text goes through the secret redactor and an outbound
scrub: your home folder path becomes ~, your user name becomes <user>, and
IP addresses become <ip>. Host names and project names inside the prose
cannot be found reliably and are sent as written.

The receiver is {receiver}. Their data policies apply.
Type yes to agree. Any other answer keeps the backend off.
"""

_RECEIVERS = {
    "openrouter": "OpenRouter and the model provider it routes to "
    '(the request asks for providers with data_collection "deny")',
    "ollama": "the Ollama server at the configured URL",
}


def consent_text(provider: str, model: str) -> str:
    return CONSENT_TEXT.format(
        version=CONSENT_VERSION,
        provider=provider,
        model=model,
        receiver=_RECEIVERS.get(provider, provider),
    )


def read_consent(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, CONSENT_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def has_consent(conn: sqlite3.Connection, provider: str, model: str) -> bool:
    """True only for a consent of this text version, provider and model."""
    record = read_consent(conn)
    return bool(
        record
        and record.get("version") == CONSENT_VERSION
        and record.get("provider") == provider
        and record.get("model") == model
    )


def grant_consent(conn: sqlite3.Connection, provider: str, model: str) -> None:
    record = {"version": CONSENT_VERSION, "provider": provider, "model": model, "at": db.utc_now()}
    with db.write_tx(conn):
        db.set_meta(conn, CONSENT_KEY, json.dumps(record, sort_keys=True))


def revoke_consent(conn: sqlite3.Connection) -> None:
    with db.write_tx(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (CONSENT_KEY,))


# -- backends -------------------------------------------------------------------


class Embedder(Protocol):
    """What the store needs from a backend.

    ``remote`` is true when text leaves the machine. ``load`` may be slow
    (model files); the embed calls may block on the network.
    """

    backend: str
    model: str
    remote: bool

    @property
    def model_id(self) -> str: ...

    def load(self) -> None: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class _Base:
    backend = ""
    remote = False

    def __init__(self, model: str) -> None:
        self.model = model

    @property
    def model_id(self) -> str:
        return f"{self.backend}:{self.model}"

    def load(self) -> None:
        return None

    def _query_text(self, text: str) -> str:
        return QUERY_PREFIXES.get(self.model, "") + text[:MAX_EMBED_CHARS]

    def _outbound(self, texts: Sequence[str]) -> list[str]:
        return [scrub_outbound(t) if self.remote else t for t in texts]


class FastEmbedEmbedder(_Base):
    backend = "fastembed"

    def __init__(
        self, model: str, *, models_dir: Path | None = None, allow_download: bool = False
    ) -> None:
        super().__init__(model)
        self.models_dir = models_dir
        self.allow_download = allow_download
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                from fastembed import TextEmbedding  # type: ignore[import-not-found]
            except ImportError as exc:
                raise EmbeddingError(
                    "fastembed is not installed; install noblivion[embed]"
                ) from exc
            kwargs: dict[str, object] = {"model_name": self.model}
            if self.models_dir is not None:
                kwargs["cache_dir"] = str(self.models_dir)
            if not self.allow_download:
                kwargs["local_files_only"] = True
            try:
                self._model = TextEmbedding(**kwargs)
            except Exception as exc:  # noqa: BLE001 - any load fault means keyword mode
                raise EmbeddingError(f"fastembed could not load {self.model}: {exc}") from exc

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.load()
        try:
            return [[float(x) for x in v] for v in self._model.embed(list(texts))]  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"fastembed embed failed: {exc}") from exc

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_text(text)])[0]


def _post_json(url: str, payload: dict, headers: Mapping[str, str], timeout: float) -> object:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The message never holds the key: it names the URL and the error class.
        raise EmbeddingError(f"POST {url} failed: {type(exc).__name__}") from exc


def is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaEmbedder(_Base):
    backend = "ollama"

    def __init__(self, model: str, url: str = DEFAULT_OLLAMA_URL, timeout: float = HTTP_TIMEOUT_S):
        super().__init__(model)
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.remote = not is_loopback_url(url)

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = _post_json(
            f"{self.url}/api/embed",
            {"model": self.model, "input": self._outbound(texts)},
            {},
            self.timeout,
        )
        vectors = body.get("embeddings") if isinstance(body, dict) else None
        return _check_vectors(vectors, len(texts))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_text(text)])[0]


class OpenRouterEmbedder(_Base):
    backend = "openrouter"
    remote = True

    def __init__(
        self,
        model: str,
        api_key: str,
        url: str = OPENROUTER_URL,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        super().__init__(model)
        if not api_key:
            raise EmbeddingError(f"{OPENROUTER_KEY_ENV} is not set")
        self._api_key = api_key
        self.url = url
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"OpenRouterEmbedder(model={self.model!r})"

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = _post_json(
            self.url,
            {
                "model": self.model,
                "input": self._outbound(texts),
                "provider": {"data_collection": "deny"},
            },
            {"Authorization": f"Bearer {self._api_key}"},
            self.timeout,
        )
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise EmbeddingError("OpenRouter answer has no data list")
        ordered = sorted(
            (d for d in data if isinstance(d, dict)), key=lambda d: int(d.get("index", 0))
        )
        return _check_vectors([d.get("embedding") for d in ordered], len(texts))

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_text(text)])[0]


def _check_vectors(vectors: object, expected: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbeddingError("the backend answer has the wrong number of vectors")
    out: list[list[float]] = []
    for v in vectors:
        if not isinstance(v, list) or not v:
            raise EmbeddingError("the backend answer holds a vector that is not a list")
        out.append([float(x) for x in v])
    return out


def make_embedder(
    settings: EmbeddingSettings,
    conn: sqlite3.Connection,
    env: Mapping[str, str] | None = None,
) -> Embedder | None:
    """Build the configured backend. ``None`` for backend ``none``.

    Raises ``ConsentRequiredError`` for a backend that sends text off the
    machine without a matching ``meta.embed_consent`` (section 7.1), and
    ``EmbeddingError`` for a remote Ollama URL without ``allow_remote``.
    """
    env = os.environ if env is None else env
    if settings.backend == "none":
        return None
    if settings.backend == "fastembed":
        return FastEmbedEmbedder(
            settings.model, models_dir=settings.models_dir, allow_download=settings.allow_download
        )
    if settings.backend == "ollama":
        embedder = OllamaEmbedder(settings.model, settings.ollama_url)
        if embedder.remote:
            if not settings.allow_remote:
                raise EmbeddingError(
                    "the Ollama URL is not loopback; set embedding.allow_remote = true"
                )
            if not has_consent(conn, "ollama", settings.model):
                raise ConsentRequiredError(
                    "a non-loopback Ollama URL needs: noblivion consent embeddings"
                )
        return embedder
    if settings.backend == "openrouter":
        if not has_consent(conn, "openrouter", settings.model):
            raise ConsentRequiredError("OpenRouter embeddings need: noblivion consent embeddings")
        return OpenRouterEmbedder(settings.model, env.get(OPENROUTER_KEY_ENV, ""))
    raise EmbeddingError(f"unknown embedding backend {settings.backend!r}")


# -- backfill and model change (section 7.3) ------------------------------------


@dataclass(frozen=True)
class BackfillResult:
    embedded: int
    failed: int
    skipped_mined: int
    switched: bool


def needs_reembed(conn: sqlite3.Connection, embedder: Embedder) -> bool:
    """True when ``meta`` names another backend or model than ``embedder``."""
    return (db.get_meta(conn, "embed_backend"), db.get_meta(conn, "embed_model")) != (
        embedder.backend,
        embedder.model,
    )


def _todo(
    conn: sqlite3.Connection, model_id: str, include_mined: bool
) -> tuple[list[tuple[int, str, str]], int]:
    """Live rows whose vector for ``model_id`` is missing or stale."""
    todo: list[tuple[int, str, str]] = []
    skipped = 0
    with db.read_tx(conn):
        cur = conn.execute(
            "SELECT m.id, m.content, m.source_type, v.content_hash FROM memories m "
            "LEFT JOIN vectors v ON v.memory_id = m.id AND v.model = ? "
            "WHERE m.archived_at IS NULL AND m.deleted_at IS NULL ORDER BY m.id",
            (model_id,),
        )
        for memory_id, content, source_type, have in cur.fetchall():
            if source_type == db.SOURCE_MINED and not include_mined:
                skipped += 1
                continue
            text = embed_text(content)
            if not text:
                continue
            digest = text_hash(text)
            if digest != have:
                todo.append((int(memory_id), text, digest))
    return todo, skipped


def _embed_batch(
    embedder: Embedder, batch: Sequence[tuple[int, str, str]]
) -> tuple[list[tuple[int, str, list[float]]], int]:
    """Embed a batch. When the batch call fails, embed row by row, so one bad
    row does not cost the batch (section 7.4)."""
    try:
        vectors = embedder.embed_documents([text for _, text, _ in batch])
        if len(vectors) != len(batch):
            raise EmbeddingError("wrong number of vectors")
        return [(m, d, v) for (m, _, d), v in zip(batch, vectors, strict=True)], 0
    except Exception:  # noqa: BLE001 - fall back to single rows
        pass
    done: list[tuple[int, str, list[float]]] = []
    failed = 0
    for memory_id, text, digest in batch:
        try:
            done.append((memory_id, digest, embedder.embed_documents([text])[0]))
        except Exception:  # noqa: BLE001 - the row stays without a vector
            failed += 1
    return done, failed


def _write_vectors(
    conn: sqlite3.Connection, model_id: str, rows: Sequence[tuple[int, str, list[float]]]
) -> tuple[int, int, int | None]:
    written = failed = 0
    dim: int | None = None
    packed: list[tuple[int, str, int, bytes]] = []
    for memory_id, digest, vector in rows:
        try:
            blob = vector_to_blob(vector)
        except EmbeddingError:
            failed += 1
            continue
        packed.append((memory_id, digest, len(vector), blob))
    if not packed:
        return 0, failed, None
    with db.write_tx(conn):
        rev = db.bump_rev(conn, "vector_rev")
        for memory_id, digest, size, blob in packed:
            # The row may be gone since the read: write only while it exists.
            cur = conn.execute(
                "INSERT INTO vectors (memory_id, model, dim, content_hash, blob, rev) "
                "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM memories WHERE id = ?) "
                "ON CONFLICT (memory_id, model) DO UPDATE SET dim = excluded.dim, "
                "content_hash = excluded.content_hash, blob = excluded.blob, rev = excluded.rev",
                (memory_id, model_id, size, digest, blob, rev, memory_id),
            )
            if cur.rowcount:
                written += 1
                dim = size
    return written, failed, dim


def finish_model_switch(conn: sqlite3.Connection, embedder: Embedder) -> bool:
    """End of a re-embed: in one transaction, delete vectors of other models
    and write the backend, model and dim to ``meta``. Returns True when
    ``meta`` changed. Old-model vectors are outside every pool of the new
    model, so the delete needs no ``vector_rev`` stamp (section 6.3)."""
    with db.write_tx(conn):
        if not needs_reembed(conn, embedder):
            return False
        conn.execute("DELETE FROM vectors WHERE model <> ?", (embedder.model_id,))
        row = conn.execute(
            "SELECT dim FROM vectors WHERE model = ? LIMIT 1", (embedder.model_id,)
        ).fetchone()
        db.set_meta(conn, "embed_backend", embedder.backend)
        db.set_meta(conn, "embed_model", embedder.model)
        db.set_meta(conn, "embed_dim", "" if row is None else str(int(row[0])))
    return True


def backfill(
    conn: sqlite3.Connection,
    embedder: Embedder,
    *,
    batch_size: int = BATCH_SIZE,
    include_mined: bool = True,
    finish: bool = True,
) -> BackfillResult:
    """Embed every live row that has no vector for this model, or a vector of
    an older text, in batches; one write transaction per batch.

    For a remote backend pass ``include_mined=settings.remote_include_mined``.
    With ``finish`` the pass ends with ``finish_model_switch``.
    """
    model_id = embedder.model_id
    todo, skipped = _todo(conn, model_id, include_mined)
    embedded = failed = 0
    for start in range(0, len(todo), max(1, batch_size)):
        done, batch_failed = _embed_batch(embedder, todo[start : start + batch_size])
        written, bad, _ = _write_vectors(conn, model_id, done)
        embedded += written
        failed += batch_failed + bad
    switched = finish_model_switch(conn, embedder) if finish else False
    return BackfillResult(embedded, failed, skipped, switched)


# -- the service the store holds -------------------------------------------------


def _log_reason(exc: BaseException) -> str:
    """The failure text for the log: secrets redacted, one line, cut."""
    text = " ".join(redaction.redact_at_rest(str(exc) or "-").split())
    if len(text) > REASON_LOG_CHARS:
        text = text[: REASON_LOG_CHARS - 1] + "…"
    return text


class EmbeddingService:
    """The backend, its state and the query embed with a time limit.

    The store (E3c) makes one instance, calls ``start`` at store start (it may
    run in a thread), ``backfill`` from a background job, and passes the
    instance to ``ranking.Ranker``. States: ``none`` (backend none),
    ``loading``, ``ready``, ``reembedding``, ``failed``. A failed load is
    retried at most once per ``RETRY_AFTER_S`` (section 7.4).
    """

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        factory: Callable[..., Embedder | None] = make_embedder,
        clock: Callable[[], float] = time.monotonic,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self._factory = factory
        self._clock = clock
        self._env = env
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="noblivion-embed")
        self.embedder: Embedder | None = None
        self.state = STATE_NONE if settings.backend == "none" else STATE_LOADING
        self.error: str | None = None
        self._failed_at: float | None = None

    @property
    def model_id(self) -> str | None:
        """The model the cosine leg uses, or None while no model is usable."""
        if self.embedder is not None and self.state in (STATE_READY, STATE_REEMBEDDING):
            return self.embedder.model_id
        return None

    @property
    def include_mined(self) -> bool:
        if self.embedder is not None and self.embedder.remote:
            return self.settings.remote_include_mined
        return True

    def start(self, conn: sqlite3.Connection) -> str:
        """Build and load the backend. Returns the new state."""
        if self.settings.backend == "none":
            self.state = STATE_NONE
            return self.state
        with self._lock:
            if self.state == STATE_FAILED and self._failed_at is not None:
                if self._clock() - self._failed_at < RETRY_AFTER_S:
                    return self.state
            self.state = STATE_LOADING
            try:
                embedder = self._factory(self.settings, conn, self._env)
                if embedder is None:
                    self.state = STATE_NONE
                    return self.state
                embedder.load()
            except Exception as exc:  # noqa: BLE001 - any fault means keyword mode
                self.embedder = None
                self.state = STATE_FAILED
                self.error = str(exc)
                self._failed_at = self._clock()
                # Once per failed attempt: the retry gate above returns early
                # without a new attempt, so it logs nothing.
                log.warning(
                    "model load failed (%s): %s; keyword search only, next try in %d s",
                    type(exc).__name__,
                    _log_reason(exc),
                    int(RETRY_AFTER_S),
                )
                return self.state
            self.embedder = embedder
            self.error = None
            self._failed_at = None
            self.state = STATE_REEMBEDDING if needs_reembed(conn, embedder) else STATE_READY
            return self.state

    def backfill(self, conn: sqlite3.Connection, **kwargs: object) -> BackfillResult | None:
        """One backfill pass; ends a re-embed. None while no backend is usable."""
        embedder = self.embedder
        if embedder is None or self.state not in (STATE_READY, STATE_REEMBEDDING):
            return None
        kwargs.setdefault("include_mined", self.include_mined)
        result = backfill(conn, embedder, **kwargs)  # type: ignore[arg-type]
        if self.state == STATE_REEMBEDDING and not needs_reembed(conn, embedder):
            self.state = STATE_READY
        return result

    def embed_query(self, text: str, timeout_s: float = QUERY_TIMEOUT_S) -> list[float] | None:
        """The query vector, or None when no model is usable, the call fails or
        it takes longer than ``timeout_s`` (section 7.4: keyword-only answer)."""
        embedder = self.embedder
        if embedder is None or self.model_id is None or not text.strip():
            return None
        future = self._pool.submit(embedder.embed_query, text)
        try:
            return normalize(future.result(timeout=timeout_s))
        except FutureTimeout:
            return None
        except Exception:  # noqa: BLE001 - keyword-only for this answer
            return None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# -- CLI: noblivion consent embeddings -------------------------------------------


def consent_main(argv: Sequence[str] | None = None, stdin=None, stdout=None) -> int:
    parser = argparse.ArgumentParser(
        prog="noblivion consent embeddings",
        description="Agree that the embedding backend may send text off this machine.",
    )
    parser.add_argument("--revoke", action="store_true", help="remove the consent")
    parser.add_argument("--db", type=Path, default=None, help="database file; default: data dir")
    args = parser.parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    settings = load_embedding_settings()
    db_path = args.db or config.load_settings().db_path
    try:
        conn = db.open_db(db_path, allow_migrate=False)
    except db.SchemaError as exc:
        print(f"noblivion consent: {exc}", file=sys.stderr)
        return 3
    try:
        if args.revoke:
            revoke_consent(conn)
            print("noblivion consent: embeddings consent removed", file=stdout)
            return 0
        if settings.backend not in ("openrouter", "ollama"):
            print(
                f"noblivion consent: backend {settings.backend} keeps text on this machine; "
                "no consent is needed",
                file=stdout,
            )
            return 0
        print(consent_text(settings.backend, settings.model), file=stdout)
        print("> ", end="", file=stdout, flush=True)
        answer = stdin.readline().strip().lower()
        if answer != "yes":
            print("noblivion consent: not given", file=stdout)
            return 1
        grant_consent(conn, settings.backend, settings.model)
        print("noblivion consent: embeddings consent recorded", file=stdout)
        return 0
    finally:
        conn.close()
