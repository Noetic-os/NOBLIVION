# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedup sweep with a hosted judge (design doc 0001, section 10).

``noblivion dedup <command>``:

- ``pairs``: list the candidate pairs from the local vectors. Sends nothing.
- ``plan [--dry-run] [--yes]``: ask the judge about each pair and write
  ``dedup/plan-<run_id>.json`` in the data dir. Before the first judge call
  it prints the cost estimate and asks the user to type ``yes``; ``--yes``
  skips the question. ``--dry-run`` prints the pairs and the cost estimate
  only: it makes no network call and writes no plan. A plan never changes a
  memory file.
- ``apply <run_id>``: merge the ``MERGE`` pairs of that plan. The memory
  files are the source of truth: the survivor file gets the loser text, the
  loser file moves to ``<memory folder>/.archive/dedup/<run_id>/``, and the
  indexer picks the change up. A write-ahead log, a backup and an undo
  script go to ``dedup/<run_id>/`` in the data dir.
- ``undo <run_id> [--pair A,B]``: restore the files of a run, newest step
  first, and veto the pair.
- ``consent [--revoke]`` and ``clear-latch``.

Dedup is off until the user sets a judge model (``dedup.model``), the key
(``OPENROUTER_API_KEY``) and the dedup consent. The plugin ships no default
model (section 19). Without all three, ``plan`` sends nothing.

The judge is pluggable: anything with a ``model`` and a
``judge(system, user) -> Verdict`` method. Tests use a fake judge.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Protocol

from noblivion import config, db, embedding, indexer, labels, redaction

JUDGES = ("openrouter", "off")
DEFAULT_JUDGE = "openrouter"
EXAMPLE_MODEL = "google/gemini-2.5-flash"  # docs and help only, never a default
DEFAULT_MIN_COSINE = 0.82
DEFAULT_MAX_PAIRS = 50
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MIN_INTERVAL_S = 1.0
MAX_MERGES_PER_RUN = 30
MIN_AGE_S = 30 * 60
MAX_FILE_CHARS = 20_000
MAX_REASON_CHARS = 500
MAX_OUTPUT_TOKENS = 400
MAX_RETRIES_429 = 2
MAX_RETRY_AFTER_S = 60.0
CHARS_PER_TOKEN = 4
EST_OUTPUT_TOKENS = 150

MERGEABLE = ("feedback", "project", "reference", "user")
VERDICTS = ("MERGE", "RELATED", "CONTRADICT", "SUPERSEDE", "UNSURE")
SURVIVORS = ("A", "B", "none")

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_ENV = embedding.OPENROUTER_KEY_ENV

CONSENT_KEY = "dedup_consent"
# 2: the text says that the front matter (name, description) is sent as written.
CONSENT_VERSION = 2

PROMPT_FILE = Path(__file__).with_name("dedup_prompt_v1.txt")
PROMPT_SHA256 = "04ae4615fef1b65353318c8f99c3566d532ce7d47c6693c44519af15beed42cc"
USER_MARKER = "=== USER TEMPLATE ==="

DEDUP_DIR = "dedup"
LATCH_FILE = "apply-failed.json"
STATUS_FILE_ENV = "NOBLIVION_DEDUP_STATUS_FILE"
STATUS_FILE = Path("cache") / "dedup-last-run.json"  # in the data dir
WAL_FILE = "wal.jsonl"
UNDO_SCRIPT = "undo.sh"
ARCHIVE_DIR = Path(".archive") / "dedup"
MEMORY_FIELDS_FILE = "memory_fields.py"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_SCHEMA = 3
EXIT_LOCKED = 4
EXIT_LATCH = 5
EXIT_NO_DB = 6  # no database: dedup never makes one (NOBLIVION-28)

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


class DedupError(RuntimeError):
    """A dedup step cannot run. The message never holds the key."""


class Refusal(Exception):
    """A pair the merge engine does not merge."""


# -- settings -------------------------------------------------------------------


def _float(value: object, default: float | None, low: float = 0.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = float(value)
    except ValueError:
        return default
    return number if number >= low and number == number else default


@dataclass(frozen=True)
class DedupSettings:
    judge: str = DEFAULT_JUDGE
    model: str = ""  # no default (section 19)
    min_cosine: float = DEFAULT_MIN_COSINE
    max_pairs: int = DEFAULT_MAX_PAIRS
    timeout_s: float = DEFAULT_TIMEOUT_S
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    price_in_per_mtok: float | None = None  # USD per million input tokens, for the estimate
    price_out_per_mtok: float | None = None


def load_dedup_settings(env: Mapping[str, str] | None = None) -> DedupSettings:
    """Section 12.3 keys ``dedup.*``."""
    env = os.environ if env is None else env
    cfg = config.load_file(env)

    def pick(env_key: str, dotted: str) -> str:
        if env.get(env_key, "").strip():
            return env[env_key].strip()
        value = config.lookup(cfg, dotted)
        return value.strip() if isinstance(value, str) else ""

    judge = pick("NOBLIVION_DEDUP_JUDGE", "dedup.judge").lower() or DEFAULT_JUDGE
    if judge not in JUDGES:
        judge = "off"  # an unknown judge name sends nothing
    cosine = _float(config.lookup(cfg, "dedup.min_cosine"), DEFAULT_MIN_COSINE)
    if cosine is None or cosine > 1.0:
        cosine = DEFAULT_MIN_COSINE
    max_pairs = config._non_negative_int(config.lookup(cfg, "dedup.max_pairs"), DEFAULT_MAX_PAIRS)
    return DedupSettings(
        judge=judge,
        model=pick("NOBLIVION_DEDUP_MODEL", "dedup.model"),
        min_cosine=cosine,
        max_pairs=max_pairs,
        timeout_s=_float(config.lookup(cfg, "dedup.timeout_s"), DEFAULT_TIMEOUT_S, 1.0)
        or DEFAULT_TIMEOUT_S,
        min_interval_s=_float(
            config.lookup(cfg, "dedup.min_interval_s"), DEFAULT_MIN_INTERVAL_S
        )  # fmt: skip
        or 0.0,
        price_in_per_mtok=_float(config.lookup(cfg, "dedup.price_in_per_mtok"), None),
        price_out_per_mtok=_float(config.lookup(cfg, "dedup.price_out_per_mtok"), None),
    )


# -- consent (section 10.2) -----------------------------------------------------

CONSENT_TEXT = """\
NOBLIVION dedup consent (version {version})

Judge: OpenRouter, model: {model}

`noblivion dedup plan` sends text off this machine, once per candidate pair:
- a fixed judge prompt;
- the text of the two memory files of the pair, each at most 20,000
  characters. The file names are replaced by A and B, but the text
  includes the front matter, so its name and description fields are sent
  as written. No trust data and no other memory is sent.

Before sending, each text goes through the secret redactor and an outbound
scrub: your home folder path becomes ~, your user name becomes <user>, and
IP addresses become <ip>. Host names and project names inside the prose
cannot be found reliably and are sent as written.

The receiver is OpenRouter and the model provider it routes to (the request
asks for providers with data_collection "deny"). Their data policies apply.
Each judge call is billed to your OpenRouter account.
Type yes to agree. Any other answer keeps dedup off.
"""


def consent_text(model: str) -> str:
    return CONSENT_TEXT.format(version=CONSENT_VERSION, model=model)


def read_consent(conn: sqlite3.Connection) -> dict | None:
    raw = db.get_meta(conn, CONSENT_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def has_consent(conn: sqlite3.Connection, model: str) -> bool:
    """True only for a consent of this text version, provider and model."""
    record = read_consent(conn)
    return bool(
        record
        and record.get("version") == CONSENT_VERSION
        and record.get("provider") == "openrouter"
        and record.get("model") == model
    )


def grant_consent(conn: sqlite3.Connection, model: str) -> None:
    record = {
        "version": CONSENT_VERSION,
        "provider": "openrouter",
        "model": model,
        "at": db.utc_now(),
    }
    with db.write_tx(conn):
        db.set_meta(conn, CONSENT_KEY, json.dumps(record, sort_keys=True))


def revoke_consent(conn: sqlite3.Connection) -> None:
    with db.write_tx(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (CONSENT_KEY,))


def ask_consent(conn: sqlite3.Connection, model: str, stdin, stdout) -> bool:
    print(consent_text(model), file=stdout)
    print("> ", end="", file=stdout, flush=True)
    answer = (stdin.readline() or "").strip().lower()
    if answer != "yes":
        print("noblivion dedup: consent not given; nothing was sent", file=stdout)
        return False
    grant_consent(conn, model)
    print("noblivion dedup: dedup consent recorded", file=stdout)
    return True


# -- prompt (section 10.3) ------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    system: str
    user_template: str
    sha256: str


def load_prompt(path: Path = PROMPT_FILE, expect: str = PROMPT_SHA256) -> Prompt:
    """The frozen judge prompt, checked by sha256."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expect:
        raise DedupError(f"the judge prompt {path.name} has sha256 {digest}, expected {expect}")
    system, sep, user = raw.decode("utf-8").partition(USER_MARKER)
    if not sep:
        raise DedupError(f"the judge prompt {path.name} has no {USER_MARKER} line")
    return Prompt(system.strip(), user.strip("\n"), digest)


_PLACEHOLDER_RE = re.compile(r"\{(kind|text_a|text_b)\}")


def render_user(prompt: Prompt, kind: str, text_a: str, text_b: str) -> str:
    """One pass, so a brace in a memory text is never a placeholder."""
    values = {"kind": kind, "text_a": text_a, "text_b": text_b}
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], prompt.user_template)


# -- judge ----------------------------------------------------------------------


@dataclass
class Verdict:
    verdict: str
    survivor: str = "none"
    reason: str = ""
    sent: bool = True
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "survivor": self.survivor, "reason": self.reason}


def unsure(reason: str, *, sent: bool, error: str = "") -> Verdict:
    return Verdict("UNSURE", "none", reason, sent=sent, error=error)


class Judge(Protocol):
    model: str

    def judge(self, system: str, user: str) -> Verdict: ...


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdict(content: object) -> Verdict:
    """The judge answer is data: parsed against the schema, never followed.
    Anything that does not parse is ``UNSURE``."""
    if not isinstance(content, str):
        return unsure("the judge answer is not text", sent=True, error="parse")
    match = _JSON_OBJECT_RE.search(content)
    try:
        data = json.loads(match.group(0) if match else content)
    except ValueError:
        return unsure("the judge answer is not JSON", sent=True, error="parse")
    if not isinstance(data, dict):
        return unsure("the judge answer is not an object", sent=True, error="parse")
    verdict = str(data.get("verdict", "")).strip().upper()
    survivor = str(data.get("survivor", "none")).strip()
    survivor = survivor.upper() if survivor.upper() in ("A", "B") else survivor.lower()
    reason = data.get("reason")
    reason = reason.strip()[:MAX_REASON_CHARS] if isinstance(reason, str) else ""
    if verdict not in VERDICTS or survivor not in SURVIVORS:
        return unsure("the judge answer does not match the schema", sent=True, error="parse")
    return Verdict(verdict, survivor, reason)


class OpenRouterJudge:
    """OpenRouter chat completions over ``urllib`` (section 10.3)."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        url: str = OPENROUTER_CHAT_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise DedupError("no judge model is set (dedup.model)")
        if not api_key:
            raise DedupError(f"{KEY_ENV} is not set")
        self.model = model
        self._api_key = api_key
        self.url = url
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def __repr__(self) -> str:  # never show the key
        return f"OpenRouterJudge(model={self.model!r})"

    def _wait_turn(self) -> None:
        if self._last_call is not None and self.min_interval_s > 0:
            wait = self.min_interval_s - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "X-Title": "NOBLIVION dedup",
            },
        )
        for attempt in range(MAX_RETRIES_429 + 1):
            self._wait_turn()
            try:
                with self._opener(request, timeout=self.timeout_s) as response:  # noqa: S310
                    body = json.loads(response.read().decode("utf-8"))
                return body if isinstance(body, dict) else {}
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == MAX_RETRIES_429:
                    raise
                retry_after = _float(exc.headers.get("Retry-After") if exc.headers else None, 5.0)
                self._sleep(min(retry_after or 5.0, MAX_RETRY_AFTER_S))
        return {}  # not reached

    def judge(self, system: str, user: str) -> Verdict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "provider": {"data_collection": "deny"},
            "usage": {"include": True},
        }
        try:
            body = self._post(payload)
        except urllib.error.HTTPError as exc:
            return unsure(f"judge error: HTTP {exc.code}", sent=True, error=f"http {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The message names the error class only; it never holds the key.
            name = type(exc).__name__
            return unsure(f"judge error: {name}", sent=True, error=name)
        choices = body.get("choices")
        content = None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            content = message.get("content") if isinstance(message, dict) else None
        verdict = parse_verdict(content)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        verdict.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        verdict.completion_tokens = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost")
        verdict.cost = float(cost) if isinstance(cost, (int, float)) else None
        return verdict


# -- candidates (section 10.1) --------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    root: str
    category: str
    path_a: str
    path_b: str
    id_a: int
    id_b: int
    cosine: float


def root_folders(dirs: Iterable[Path]) -> dict[str, Path]:
    """Root key -> memory folder, the same rule as the indexer."""
    out: dict[str, Path] = {}
    for d in dirs:
        folder = Path(os.path.expanduser(str(d)))
        out.setdefault(folder.parent.name, folder)
    return out


def is_index_file(path: str) -> bool:
    return path in indexer.INDEX_FILES or path.startswith("topic_")


def load_vectors(
    conn: sqlite3.Connection, project: str, model_id: str
) -> list[tuple[int, str, str, str, list[float]]]:
    """Live markdown rows with a vector of ``model_id``."""
    with db.read_tx(conn):
        cur = conn.execute(
            "SELECT m.id, m.root, m.path, m.category, v.blob FROM memories m "
            "JOIN vectors v ON v.memory_id = m.id AND v.model = ? "
            "WHERE m.project = ? AND m.source_type = ? "
            "AND m.archived_at IS NULL AND m.deleted_at IS NULL ORDER BY m.root, m.path",
            (model_id, project, db.SOURCE_MD),
        )
        rows = cur.fetchall()
    return [(r[0], r[1], r[2], r[3], embedding.blob_to_vector(r[4])) for r in rows]


def load_vetoes(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    with db.read_tx(conn):
        return {tuple(r) for r in conn.execute("SELECT root, path_a, path_b FROM dedup_vetoes")}


def select_pairs(
    rows: Sequence[tuple[int, str, str, str, Sequence[float]]],
    *,
    min_cosine: float,
    vetoes: set[tuple[str, str, str]] = frozenset(),  # type: ignore[assignment]
    eligible: Callable[[str, str], bool] = lambda root, path: True,
) -> list[Candidate]:
    """Pairs in one root and one mergeable category with cosine >= ``min_cosine``,
    best first. Index and topic files are never candidates."""
    import numpy as np  # the store venv has it (noblivion[embed])

    groups: dict[tuple[str, str], list[tuple[int, str, Sequence[float]]]] = {}
    for mid, root, path, category, vector in rows:
        if category not in MERGEABLE or is_index_file(path) or not eligible(root, path):
            continue
        groups.setdefault((root, category), []).append((mid, path, vector))
    out: list[Candidate] = []
    for (root, category), members in groups.items():
        if len(members) < 2:
            continue
        dims = {len(v) for _, _, v in members}
        if len(dims) != 1:
            continue  # a mixed model change in flight: skip this group this run
        matrix = np.asarray([v for _, _, v in members], dtype=np.float64)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        matrix = matrix / norms[:, None]
        sims = matrix @ matrix.T
        ii, jj = np.nonzero(np.triu(sims >= min_cosine, k=1))
        for i, j in zip(ii.tolist(), jj.tolist(), strict=True):
            (id_a, pa, _), (id_b, pb, _) = sorted((members[i], members[j]), key=lambda m: m[1])
            if (root, pa, pb) in vetoes:
                continue
            out.append(Candidate(root, category, pa, pb, id_a, id_b, float(sims[i, j])))
    out.sort(key=lambda c: (-c.cosine, c.root, c.path_a, c.path_b))
    return out


def find_candidates(
    conn: sqlite3.Connection,
    settings: config.Settings,
    dedup_settings: DedupSettings,
    *,
    model_id: str,
    folders: Mapping[str, Path],
    min_age_s: float = MIN_AGE_S,
    now: float | None = None,
) -> list[Candidate]:
    now = time.time() if now is None else now

    def eligible(root: str, path: str) -> bool:
        folder = folders.get(root)
        if folder is None:
            return False
        try:
            return now - (folder / path).stat().st_mtime >= min_age_s
        except OSError:
            return False

    return select_pairs(
        load_vectors(conn, settings.namespace, model_id),
        min_cosine=dedup_settings.min_cosine,
        vetoes=load_vetoes(conn),
        eligible=eligible,
    )


# -- plan -----------------------------------------------------------------------


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def outbound_text(raw: bytes) -> str:
    """Secret redactor plus outbound scrub (section 10.2)."""
    return embedding.scrub_outbound(raw.decode("utf-8", errors="replace"))


def estimate(
    pairs: Sequence[Candidate], folders: Mapping[str, Path], prompt: Prompt, ds: DedupSettings
) -> tuple[int, int, float | None]:
    """(input tokens, output tokens, USD or None) for judging ``pairs``."""
    chars = 0
    for c in pairs:
        chars += len(prompt.system) + len(prompt.user_template)
        for path in (c.path_a, c.path_b):
            try:
                chars += min((folders[c.root] / path).stat().st_size, MAX_FILE_CHARS)
            except (OSError, KeyError):
                pass
    tokens_in = chars // CHARS_PER_TOKEN
    tokens_out = EST_OUTPUT_TOKENS * len(pairs)
    usd = None
    if ds.price_in_per_mtok is not None and ds.price_out_per_mtok is not None:
        usd = (tokens_in * ds.price_in_per_mtok + tokens_out * ds.price_out_per_mtok) / 1e6
    return tokens_in, tokens_out, usd


def judge_pair(judge: Judge, prompt: Prompt, c: Candidate, raw_a: bytes, raw_b: bytes) -> Verdict:
    text_a, text_b = outbound_text(raw_a), outbound_text(raw_b)
    if redaction.REDACTION_FAILED_TOKEN in text_a or redaction.REDACTION_FAILED_TOKEN in text_b:
        return unsure("redaction failed; the pair was not sent", sent=False)
    if len(text_a) > MAX_FILE_CHARS or len(text_b) > MAX_FILE_CHARS:
        return unsure(f"a file is longer than {MAX_FILE_CHARS} characters; not sent", sent=False)
    try:
        return judge.judge(prompt.system, render_user(prompt, c.category, text_a, text_b))
    except Exception as exc:  # noqa: BLE001 - a judge error is UNSURE, never a merge
        return unsure(f"judge error: {type(exc).__name__}", sent=True, error=type(exc).__name__)


def build_plan(
    candidates: Sequence[Candidate],
    folders: Mapping[str, Path],
    judge: Judge,
    prompt: Prompt,
    *,
    run_id: str,
    out,
) -> dict:
    """Judge every pair. Reads files, writes nothing."""
    pairs: list[dict] = []
    used_in = used_out = 0
    cost = 0.0
    cost_seen = False
    for c in candidates:
        folder = folders[c.root]
        try:
            raw_a, raw_b = (folder / c.path_a).read_bytes(), (folder / c.path_b).read_bytes()
        except OSError:
            print(f"  skip {c.path_a} <> {c.path_b}: a file is gone", file=out)
            continue
        v = judge_pair(judge, prompt, c, raw_a, raw_b)
        used_in += v.prompt_tokens
        used_out += v.completion_tokens
        if v.cost is not None:
            cost += v.cost
            cost_seen = True
        pairs.append(
            {
                "root": c.root,
                "category": c.category,
                "cosine": round(c.cosine, 5),
                "a": {"path": c.path_a, "id": c.id_a, "sha256": sha256_bytes(raw_a)},
                "b": {"path": c.path_b, "id": c.id_b, "sha256": sha256_bytes(raw_b)},
                "sent": v.sent,
                "error": v.error,
                **v.as_dict(),
            }
        )
        print(
            f"  {c.cosine:.3f} {v.verdict:<10} survivor={v.survivor:<4} "
            f"{c.root}: {c.path_a} <> {c.path_b}" + (f"\n         {v.reason}" if v.reason else ""),
            file=out,
        )
    return {
        "version": 1,
        "run_id": run_id,
        "created_at": db.utc_now(),
        "judge_model": judge.model,
        "prompt_sha256": prompt.sha256,
        "pairs": pairs,
        "usage": {
            "prompt_tokens": used_in,
            "completion_tokens": used_out,
            "cost_usd": round(cost, 6) if cost_seen else None,
        },
    }


# -- files ----------------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def dedup_dir(settings: config.Settings) -> Path:
    return settings.data_dir / DEDUP_DIR


def plan_path(settings: config.Settings, run_id: str) -> Path:
    return dedup_dir(settings) / f"plan-{run_id}.json"


def latch_path(settings: config.Settings) -> Path:
    return dedup_dir(settings) / LATCH_FILE


def read_folder(folder: Path) -> dict[str, bytes]:
    """Top-level ``*.md`` files, as the indexer sees them."""
    return {
        p.name: p.read_bytes()
        for p in sorted(folder.iterdir())
        if p.suffix == ".md" and not p.name.startswith(".") and p.is_file()
    }


# -- memory fields (the hooks' front matter module) -----------------------------

_MF_CACHE: dict[str, ModuleType] = {}


def load_memory_fields(env: Mapping[str, str] | None = None) -> ModuleType:
    """``hooks/memory_fields.py``, found like ``labels.rules_path``."""
    for folder in labels.hooks_dirs(env):
        path = folder / MEMORY_FIELDS_FILE
        if not path.is_file():
            continue
        key = str(path.resolve())
        if key not in _MF_CACHE:
            spec = importlib.util.spec_from_file_location("_noblivion_hook_memory_fields", path)
            if spec is None or spec.loader is None:
                break
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _MF_CACHE[key] = mod
        return _MF_CACHE[key]
    raise DedupError(f"{MEMORY_FIELDS_FILE} not found; merges need the plugin hooks folder")


# -- merge engine (section 10.4) ------------------------------------------------

GROUP = ("violates", "example_repeat", "example_ok", "complies")
_WIKI_ANY = re.compile(r"\[\[([^\[\]|#\n]+?)(?:\.md)?(?:[|#][^\]\n]*)?\]\]")
_MD_ANY = re.compile(r"\]\((?:\./)?([^()\s/#]+?)\.md(?:#[^()\s]*)?\)")


def stem_of(name: str) -> str:
    return name[:-3] if name.endswith(".md") else name


def link_stems(text: str) -> list[str]:
    out = [m.group(1).strip() for m in _WIKI_ANY.finditer(text)]
    out += [m.group(1) for m in _MD_ANY.finditer(text)]
    return out


def rewrite_links(text: str, loser: str, survivor: str) -> str:
    """``[[loser]]`` and ``](loser.md)`` forms point at the survivor. A longer
    stem that starts with the loser's is not touched."""
    wiki = re.compile(r"\[\[" + re.escape(loser) + r"(?=(?:\.md)?[\]|#])")
    md = re.compile(r"\]\((\./)?" + re.escape(loser) + r"(?=\.md[)#])")
    text = wiki.sub(lambda _m: "[[" + survivor, text)
    return md.sub(lambda m: "](" + (m.group(1) or "") + survivor, text)


def drop_pointer_lines(text: str, loser: str, survivor: str) -> str:
    """In an index file: drop each line whose only link is the loser, when the
    file already points at the survivor."""
    if survivor not in link_stems(text):
        return text
    return "\n".join(ln for ln in text.split("\n") if set(link_stems(ln)) != {loser})


def _text(raw: bytes) -> str:
    return raw.decode("utf-8")


def _quoted(label: str, value: str) -> str:
    return "\n".join("> " + ln for ln in f"{label}: {value}".split("\n"))


def _check_kind(name: str) -> str:
    for kind in ("feedback", "project"):
        if name.startswith(kind + "_"):
            return kind
    return "other"


def merge_texts(
    mf: ModuleType, s_name: str, s_raw: bytes, l_name: str, l_raw: bytes, date: str
) -> bytes:
    """The survivor with the loser merged in, or ``Refusal``."""
    try:
        s_text, l_text = _text(s_raw), _text(l_raw)
    except UnicodeDecodeError:
        raise Refusal("a file is not UTF-8") from None
    s_split, l_split = mf.split(s_text), mf.split(l_text)
    if s_split is None or l_split is None:
        raise Refusal("a file has no closed front matter")
    sf, lf = mf.read_fields(s_text), mf.read_fields(l_text)
    if str(sf.get("scope") or "") != str(lf.get("scope") or ""):
        raise Refusal("scope differs")
    s_group = tuple(str(sf.get(k) or "") for k in GROUP)
    l_group = tuple(str(lf.get(k) or "") for k in GROUP)
    if s_group[0] and l_group[0] and s_group != l_group:
        raise Refusal("both files hold a violates group and the groups differ")
    for key in ("status", "project"):
        if str(sf.get(key) or "") != str(lf.get(key) or ""):
            raise Refusal(f"{key} differs")
    s_trig = [str(t) for t in (sf.get("triggers") or [])]
    union = list(s_trig)
    for t in (str(t) for t in (lf.get("triggers") or [])):
        if t not in union:
            union.append(t)
    if len(union) > mf.TRIGGERS_MAX_ITEMS:
        raise Refusal(f"the trigger union has {len(union)} items")
    changes: dict[str, object] = {}
    if union != s_trig:
        changes["triggers"] = union
    if l_group[0] and not s_group[0]:
        changes.update({k: (lf.get(k) or None) for k in GROUP})
    lines, close = s_split
    meta, _ = indexer.parse_frontmatter(l_text)
    description = meta.get("description")
    block = [f"Merged from {stem_of(l_name)} on {date}:"]
    for label, value in (
        ("Description", description),
        ("Rule", lf.get("rule")),
        ("Apply", lf.get("apply")),
    ):
        v = value.strip() if isinstance(value, str) else ""
        if v and v not in s_text:
            block.append(_quoted(label, v))
    body = mf.body_of(s_text).rstrip("\n") + "\n\n" + "\n".join(block) + "\n" + mf.body_of(l_text)
    if not body.endswith("\n"):
        body += "\n"
    text = "\n".join(lines[: close + 1]) + "\n" + body
    if changes:
        try:
            text = mf.write_fields(text, changes)
        except (ValueError, KeyError, mf.BodyChanged) as exc:
            raise Refusal(f"the merged front matter cannot be written: {exc}") from None
    text = rewrite_links(text, stem_of(l_name), stem_of(s_name))
    kind = _check_kind(s_name)
    had = set(mf.check_fields(sf, kind)) | set(mf.check_fields(lf, kind))
    new = [p for p in mf.check_fields(mf.read_fields(text), kind) if p not in had]
    if new:
        raise Refusal("the merged file fails the field check: " + "; ".join(new[:3]))
    return text.encode("utf-8")


@dataclass
class FileChange:
    name: str
    before: bytes
    after: bytes | None  # None: the loser, which moves to the archive


def plan_merge(
    mf: ModuleType, view: Mapping[str, bytes], survivor: str, loser: str, date: str
) -> list[FileChange]:
    """Every file change of one merge: survivor first, the loser last."""
    merged = merge_texts(mf, survivor, view[survivor], loser, view[loser], date)
    s_stem, l_stem = stem_of(survivor), stem_of(loser)
    changes = [FileChange(survivor, view[survivor], merged)]
    for name in sorted(view):
        if name in (survivor, loser):
            continue
        try:
            old = _text(view[name])
        except UnicodeDecodeError:
            continue
        new = drop_pointer_lines(old, l_stem, s_stem) if is_index_file(name) else old
        new = rewrite_links(new, l_stem, s_stem)
        if new != old:
            changes.append(FileChange(name, view[name], new.encode("utf-8")))
    changes.append(FileChange(loser, view[loser], None))
    return changes


def choose_survivor(pair: Mapping, view: Mapping[str, bytes]) -> tuple[str, str]:
    """The judge's survivor; for ``none``, the file with more inbound links,
    then the first name."""
    a, b = pair["a"]["path"], pair["b"]["path"]
    if pair.get("survivor") == "A":
        return a, b
    if pair.get("survivor") == "B":
        return b, a
    inbound: dict[str, int] = {}
    for name, raw in view.items():
        for s in link_stems(raw.decode("utf-8", errors="replace")):
            if s != stem_of(name):
                inbound[s] = inbound.get(s, 0) + 1
    ia, ib = inbound.get(stem_of(a), 0), inbound.get(stem_of(b), 0)
    if ia != ib:
        return (a, b) if ia > ib else (b, a)
    return (a, b) if a < b else (b, a)


# -- write-ahead log, backup, latch ---------------------------------------------


def _append_wal(run_dir: Path, record: Mapping) -> None:
    path = run_dir / WAL_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_wal(run_dir: Path) -> list[dict]:
    path = run_dir / WAL_FILE
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn last line after a crash
        if isinstance(record, dict):
            out.append(record)
    return out


def _begin_record(run_dir: Path, archived_id: int) -> dict | None:
    found = None
    for record in read_wal(run_dir):
        if record.get("phase") == "begin" and record.get("archived_id") == archived_id:
            found = record  # the newest one
    return found


def _write_backup(path: Path, changes: Sequence[FileChange]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for ch in changes:
            info = tarfile.TarInfo(ch.name)
            info.size = len(ch.before)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(ch.before))
    write_atomic(path, buf.getvalue())


def read_backup(path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                continue
            fh = tar.extractfile(member)
            if fh is not None:
                out[member.name] = fh.read()
    return out


def _set_latch(settings: config.Settings, run_id: str, reason: str) -> None:
    _private_dir(dedup_dir(settings))
    data = {"run_id": run_id, "reason": reason, "at": db.utc_now()}
    write_atomic(latch_path(settings), json.dumps(data, sort_keys=True).encode("utf-8"))


def status_path(settings: config.Settings, env: Mapping[str, str]) -> Path:
    """``NOBLIVION_DEDUP_STATUS_FILE``, else ``<data dir>/cache/dedup-last-run.json``."""
    raw = env.get(STATUS_FILE_ENV, "").strip()
    return Path(os.path.expanduser(raw)) if raw else settings.data_dir / STATUS_FILE


def write_status(settings: config.Settings, env: Mapping[str, str], **fields: object) -> None:
    """The last-run status that the session start line reads (contract C4).
    Written at the end of every ``plan``, ``apply`` and ``undo``. Never raises:
    a status file is a convenience, not a step of the run."""
    doc: dict[str, object] = {"version": 1, "finished_at": db.utc_now(), "outcome": "ok"}
    doc.update(fields)
    try:
        latch = json.loads(latch_path(settings).read_text(encoding="utf-8"))
        if isinstance(latch, dict) and isinstance(latch.get("run_id"), str):
            doc["failed"] = latch["run_id"]
    except (OSError, ValueError):
        pass
    try:
        path = status_path(settings, env)
        _private_dir(path.parent)
        write_atomic(path, json.dumps(doc, sort_keys=True).encode("utf-8"))
    except OSError:
        return


def _set_archived(conn: sqlite3.Connection, memory_id: int, archived_at: str | None) -> None:
    """Inside ``write_tx``: set or clear ``archived_at`` and stamp the row, so
    the store's RAM index drops or takes back the row (section 6.3)."""
    rev = db.bump_rev(conn)
    conn.execute(
        "UPDATE memories SET archived_at = ?, rev = ?, updated_at = ? WHERE id = ?",
        (archived_at, rev, db.utc_now(), memory_id),
    )


# -- apply ----------------------------------------------------------------------


@dataclass
class Step:
    run_id: str
    step: int
    root: str
    folder: Path
    survivor: str
    loser: str
    kept_id: int
    archived_id: int
    changes: list[FileChange]
    pair: Mapping

    @property
    def archive_to(self) -> str:
        return str(ARCHIVE_DIR / self.run_id / self.loser)

    def begin_record(self, backup: Path) -> dict:
        """Names and hashes only; never file text, never the judge reason."""
        return {
            "phase": "begin",
            "run_id": self.run_id,
            "step": self.step,
            "root": self.root,
            "folder": str(self.folder),
            "survivor": self.survivor,
            "loser": self.loser,
            "kept_id": self.kept_id,
            "archived_id": self.archived_id,
            "archive_to": self.archive_to,
            "backup": backup.name,
            "files": [
                {
                    "name": ch.name,
                    "before": sha256_bytes(ch.before),
                    "after": None if ch.after is None else sha256_bytes(ch.after),
                }
                for ch in self.changes
            ],
        }


def _restore_files(folder: Path, begin: Mapping, backup: Mapping[str, bytes]) -> None:
    """Put every file of a step back to its bytes before the step."""
    archived = folder / str(begin["archive_to"])
    for f in begin["files"]:
        name = f["name"]
        before = backup[name]
        if f["after"] is None:
            target = folder / name
            if archived.is_file() and sha256_bytes(archived.read_bytes()) == f["before"]:
                os.replace(archived, target)
            else:
                write_atomic(target, before, 0o644)
        else:
            path = folder / name
            if not path.is_file() or sha256_bytes(path.read_bytes()) != f["before"]:
                write_atomic(path, before, 0o644)
    _prune_empty(archived.parent, stop=folder)


def _prune_empty(path: Path, stop: Path) -> None:
    while path != stop and stop in path.parents:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _run_step(conn: sqlite3.Connection, settings: config.Settings, step: Step, model: str) -> None:
    run_dir = _private_dir(dedup_dir(settings) / step.run_id)
    backup = run_dir / f"step-{step.step:03d}.tar.gz"
    _write_backup(backup, step.changes)
    begin = step.begin_record(backup)
    _append_wal(run_dir, begin)
    now = db.utc_now()
    with db.write_tx(conn):
        cur = conn.execute(
            "INSERT INTO dedup_actions (run_id, root, kept_id, archived_id, kept_path, "
            "archived_path, archive_to, status, judge_model, verdict, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                step.run_id,
                step.root,
                step.kept_id,
                step.archived_id,
                step.survivor,
                step.loser,
                step.archive_to,
                model,
                json.dumps(
                    {k: step.pair.get(k) for k in ("verdict", "survivor", "reason", "cosine")}
                ),
                now,
            ),
        )
        action_id = int(cur.lastrowid)
        _set_archived(conn, step.archived_id, now)
    try:
        for ch in step.changes:
            if ch.after is not None:
                write_atomic(step.folder / ch.name, ch.after)
        archived = step.folder / step.archive_to
        archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(step.folder / step.loser, archived)
    except Exception as exc:
        _restore_files(step.folder, begin, {ch.name: ch.before for ch in step.changes})
        with db.write_tx(conn):
            _set_archived(conn, step.archived_id, None)
            conn.execute("UPDATE dedup_actions SET status = 'failed' WHERE id = ?", (action_id,))
        _append_wal(run_dir, {"phase": "failed", "step": step.step, "action_id": action_id})
        raise DedupError(f"step {step.step} failed: {type(exc).__name__}: {exc}") from exc
    with db.write_tx(conn):
        conn.execute("UPDATE dedup_actions SET status = 'done' WHERE id = ?", (action_id,))
    _append_wal(run_dir, {"phase": "done", "step": step.step, "action_id": action_id})


def recover_pending(conn: sqlite3.Connection, settings: config.Settings, out) -> int:
    """Roll back every ``pending`` action a crash left (section 10.4)."""
    with db.read_tx(conn):
        pending = conn.execute(
            "SELECT id, run_id, archived_id FROM dedup_actions WHERE status = 'pending' "
            "ORDER BY id DESC"
        ).fetchall()
    for action_id, run_id, archived_id in pending:
        run_dir = dedup_dir(settings) / run_id
        begin = _begin_record(run_dir, archived_id)
        if begin is not None:
            backup = read_backup(run_dir / begin["backup"])
            _restore_files(Path(begin["folder"]), begin, backup)
        with db.write_tx(conn):
            _set_archived(conn, archived_id, None)
            conn.execute("UPDATE dedup_actions SET status = 'failed' WHERE id = ?", (action_id,))
        print(f"noblivion dedup: rolled back an unfinished step of run {run_id}", file=out)
    return len(pending)


def load_plan(settings: config.Settings, run_id: str) -> dict:
    if not _RUN_ID_RE.match(run_id):
        raise DedupError(f"not a run id: {run_id!r}")
    path = plan_path(settings, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DedupError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        raise DedupError(f"{path.name} is not a plan of run {run_id}")
    return data


def apply_plan(
    conn: sqlite3.Connection,
    settings: config.Settings,
    plan: Mapping,
    *,
    folders: Mapping[str, Path],
    out,
    mf: ModuleType | None = None,
    max_merges: int = MAX_MERGES_PER_RUN,
) -> int:
    """Merge the ``MERGE`` pairs of ``plan``. Returns the number merged.
    Raises ``DedupError`` after a failed step (the latch is then set)."""
    run_id = str(plan["run_id"])
    if latch_path(settings).exists():
        raise DedupError(
            "a failed apply set the latch; run `noblivion dedup undo <run_id>` "
            "or `noblivion dedup clear-latch`"
        )
    recover_pending(conn, settings, out)
    merges = [p for p in plan.get("pairs", []) if p.get("verdict") == "MERGE"]
    if not merges:
        print("noblivion dedup: the plan has no MERGE pair; nothing changed", file=out)
        return 0
    mf = mf or load_memory_fields()
    with db.read_tx(conn):
        step_no = conn.execute(
            "SELECT count(*) FROM dedup_actions WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    merged = 0
    for pair in merges:
        if merged >= max_merges:
            print(f"  stop: at most {max_merges} merges per run", file=out)
            break
        a, b = pair["a"], pair["b"]
        label = f"{pair['root']}: {a['path']} <> {b['path']}"
        folder = folders.get(pair["root"])
        if folder is None or not folder.is_dir():
            print(f"  skip {label}: the memory folder is not configured", file=out)
            continue
        view = read_folder(folder)
        if any(view.get(x["path"]) is None for x in (a, b)) or any(
            sha256_bytes(view[x["path"]]) != x["sha256"] for x in (a, b)
        ):
            print(f"  skip {label}: a file changed since the plan", file=out)
            continue
        survivor, loser = choose_survivor(pair, view)
        try:
            changes = plan_merge(mf, view, survivor, loser, date)
        except Refusal as exc:
            print(f"  skip {label}: {exc}", file=out)
            continue
        ids = {a["path"]: int(a["id"]), b["path"]: int(b["id"])}
        step_no += 1
        step = Step(
            run_id,
            step_no,
            pair["root"],
            folder,
            survivor,
            loser,
            ids[survivor],
            ids[loser],
            changes,
            pair,
        )
        try:
            _run_step(conn, settings, step, str(plan.get("judge_model", "")))
        except DedupError as exc:
            _set_latch(settings, run_id, str(exc))
            raise
        merged += 1
        print(f"  merged {loser} into {survivor} ({pair['root']})", file=out)
    if merged:
        write_undo_script(settings, run_id)
    return merged


def write_undo_script(settings: config.Settings, run_id: str) -> Path:
    run_dir = _private_dir(dedup_dir(settings) / run_id)
    path = run_dir / UNDO_SCRIPT
    script = (
        "#!/bin/sh\n"
        f"# Undo the dedup run {run_id}: restore the memory files, newest step first.\n"
        f'exec "{sys.executable}" -m noblivion dedup undo {run_id} "$@"\n'
    )
    write_atomic(path, script.encode("utf-8"), 0o700)
    os.chmod(path, 0o700)
    return path


# -- undo -----------------------------------------------------------------------


def undo_run(
    conn: sqlite3.Connection,
    settings: config.Settings,
    run_id: str,
    *,
    pair: tuple[str, str] | None = None,
    out,
) -> int:
    """Undo the done steps of a run, newest first. Returns the number undone.
    Raises ``DedupError`` and names the file when a file changed since."""
    if not _RUN_ID_RE.match(run_id):
        raise DedupError(f"not a run id: {run_id!r}")
    recover_pending(conn, settings, out)
    run_dir = dedup_dir(settings) / run_id
    with db.read_tx(conn):
        actions = conn.execute(
            "SELECT id, root, archived_id, kept_path, archived_path FROM dedup_actions "
            "WHERE run_id = ? AND status = 'done' ORDER BY id DESC",
            (run_id,),
        ).fetchall()
    if pair is not None:
        actions = [r for r in actions if {r[3], r[4]} == set(pair)]
    undone = 0
    for action_id, root, archived_id, kept_path, archived_path in actions:
        begin = _begin_record(run_dir, archived_id)
        if begin is None:
            raise DedupError(f"no write-ahead log record for {archived_path}")
        folder = Path(begin["folder"])
        archived = folder / begin["archive_to"]
        for f in begin["files"]:
            if f["after"] is None:
                ok = archived.is_file() and sha256_bytes(archived.read_bytes()) == f["before"]
                ok = ok and not (folder / f["name"]).exists()
                where = archived
            else:
                path = folder / f["name"]
                ok = path.is_file() and sha256_bytes(path.read_bytes()) == f["after"]
                where = path
            if not ok:
                raise DedupError(f"stop: {where} changed since the merge; nothing more undone")
        _restore_files(folder, begin, read_backup(run_dir / begin["backup"]))
        path_a, path_b = sorted((kept_path, archived_path))
        now = db.utc_now()
        with db.write_tx(conn):
            _set_archived(conn, archived_id, None)
            conn.execute(
                "INSERT OR IGNORE INTO dedup_vetoes (root, path_a, path_b, created_at) "
                "VALUES (?, ?, ?, ?)",
                (root, path_a, path_b, now),
            )
            conn.execute(
                "UPDATE dedup_actions SET status = 'undone', undone_at = ? WHERE id = ?",
                (now, action_id),
            )
        _append_wal(run_dir, {"phase": "undone", "action_id": action_id})
        undone += 1
        print(f"  restored {archived_path} and {kept_path} ({root})", file=out)
    latch_path(settings).unlink(missing_ok=True)
    return undone


# -- CLI ------------------------------------------------------------------------

HELP_EPILOG = f"""\
Dedup is off until you set all three:
  dedup.model in config.json or NOBLIVION_DEDUP_MODEL (no default; for
    example {EXAMPLE_MODEL})
  {KEY_ENV} in the environment
  the dedup consent: noblivion dedup consent
"""

JudgeFactory = Callable[[DedupSettings, str], Judge]


def openrouter_judge(ds: DedupSettings, api_key: str) -> Judge:
    return OpenRouterJudge(
        ds.model, api_key, timeout_s=ds.timeout_s, min_interval_s=ds.min_interval_s
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noblivion dedup",
        description="Find memory files that say the same thing and merge them.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=None, help="database file; default: data dir")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pairs", help="list candidate pairs; sends nothing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("plan", help="judge the pairs and write a plan; changes no file")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the pairs and the cost estimate; no network call, no plan",
    )
    p.add_argument(
        "--yes", action="store_true", help="send without the question after the cost estimate"
    )
    p.add_argument("--max-pairs", type=int, default=None, help="judge calls in this run")
    p = sub.add_parser("apply", help="merge the MERGE pairs of a plan")
    p.add_argument("run_id")
    p = sub.add_parser("undo", help="restore the files of a run")
    p.add_argument("run_id")
    p.add_argument("--pair", default=None, help="A.md,B.md: undo only this pair")
    p = sub.add_parser("consent", help="agree that plan may send memory text to OpenRouter")
    p.add_argument("--revoke", action="store_true")
    sub.add_parser("clear-latch", help="allow apply again after a failed apply")
    return parser


def _print_estimate(pairs, folders, prompt, ds, out) -> None:
    t_in, t_out, usd = estimate(pairs, folders, prompt, ds)
    cost = f", about ${usd:.4f}" if usd is not None else " (set dedup.price_* for a USD estimate)"
    print(
        f"noblivion dedup: {len(pairs)} judge calls, about {t_in} input and "
        f"{t_out} output tokens{cost}",
        file=out,
    )


def confirm_send(n_pairs: int, stdin, out) -> bool:
    """The question after the cost estimate. Only ``yes`` sends."""
    print(
        f"noblivion dedup: type yes to send {n_pairs} pairs to the judge. Each call is "
        "billed to your OpenRouter account. Any other answer sends nothing.",
        file=out,
    )
    print("> ", end="", file=out, flush=True)
    answer = (stdin.readline() or "").strip().lower()
    if answer != "yes":
        print("noblivion dedup: not confirmed; nothing was sent", file=out)
        return False
    return True


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin=None,
    stdout=None,
    judge_factory: JudgeFactory | None = None,
    min_age_s: float = MIN_AGE_S,
) -> int:
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    settings = config.load_settings(env)
    ds = load_dedup_settings(env)
    if args.command == "clear-latch":
        latch_path(settings).unlink(missing_ok=True)
        print("noblivion dedup: latch cleared", file=out)
        return EXIT_OK
    try:
        conn = db.open_db(args.db or settings.db_path, allow_migrate=False)
    except db.SchemaError as exc:
        print(f"noblivion dedup: {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except db.DatabaseMissing as exc:
        print(f"noblivion dedup: {exc}", file=sys.stderr)
        return EXIT_NO_DB
    try:
        return _dispatch(args, conn, settings, ds, env, stdin, out, judge_factory, min_age_s)
    except DedupError as exc:
        print(f"noblivion dedup: {exc}", file=out)
        if args.command in ("apply", "undo"):
            write_status(
                settings,
                env,
                mode=args.command,
                run_id=args.run_id,
                outcome="refused",
                refused_reason=str(exc),
            )
        return EXIT_LATCH if latch_path(settings).exists() else EXIT_REFUSED
    finally:
        conn.close()


def _dispatch(args, conn, settings, ds, env, stdin, out, judge_factory, min_age_s) -> int:
    folders = root_folders(settings.resolved_memory_dirs())
    if args.command == "consent":
        if args.revoke:
            revoke_consent(conn)
            print("noblivion dedup: dedup consent removed", file=out)
            return EXIT_OK
        if not ds.model:
            print("noblivion dedup: set dedup.model first; the consent names the model", file=out)
            return EXIT_REFUSED
        return EXIT_OK if ask_consent(conn, ds.model, stdin, out) else EXIT_REFUSED

    model_id = embedding.load_embedding_settings(env).model_id

    def candidates() -> list[Candidate]:
        return find_candidates(
            conn, settings, ds, model_id=model_id, folders=folders, min_age_s=min_age_s
        )

    if args.command == "pairs":
        pairs = candidates()
        if args.json:
            print(json.dumps([c.__dict__ for c in pairs], indent=1), file=out)
        else:
            for c in pairs:
                print(f"  {c.cosine:.3f} {c.root}: {c.path_a} <> {c.path_b}", file=out)
            print(f"noblivion dedup: {len(pairs)} candidate pairs", file=out)
        return EXIT_OK

    if args.command == "plan" and args.dry_run:
        # A dry run never builds a judge: no network call, nothing billed.
        prompt = load_prompt()
        pairs = candidates()
        max_pairs = ds.max_pairs if args.max_pairs is None else max(0, args.max_pairs)
        for c in pairs[:max_pairs]:
            print(f"  {c.cosine:.3f} {c.root}: {c.path_a} <> {c.path_b}", file=out)
        if len(pairs) > max_pairs:
            print(
                f"noblivion dedup: {len(pairs)} pairs; a plan judges the best {max_pairs} "
                "(dedup.max_pairs)",
                file=out,
            )
        pairs = pairs[:max_pairs]
        _print_estimate(pairs, folders, prompt, ds, out)
        print("noblivion dedup: dry run; nothing was sent, no plan written", file=out)
        write_status(settings, env, mode="dry-run", pairs=len(pairs), merged=0, proposals=0)
        return EXIT_OK

    if args.command == "plan":
        # Every check that keeps text on this machine comes before any judge.
        if ds.judge == "off":
            print("noblivion dedup: dedup.judge is off; nothing was sent", file=out)
            return EXIT_REFUSED
        if not ds.model:
            print(
                "noblivion dedup: no judge model is set (dedup.model, for example "
                f"{EXAMPLE_MODEL}); nothing was sent",
                file=out,
            )
            return EXIT_REFUSED
        api_key = env.get(KEY_ENV, "").strip()
        if not api_key:
            print(f"noblivion dedup: {KEY_ENV} is not set; nothing was sent", file=out)
            return EXIT_REFUSED
        if not has_consent(conn, ds.model) and not ask_consent(conn, ds.model, stdin, out):
            return EXIT_REFUSED
        prompt = load_prompt()
        pairs = candidates()
        max_pairs = ds.max_pairs if args.max_pairs is None else max(0, args.max_pairs)
        if len(pairs) > max_pairs:
            print(
                f"noblivion dedup: {len(pairs)} pairs; judging the best {max_pairs} "
                "(dedup.max_pairs)",
                file=out,
            )
        pairs = pairs[:max_pairs]
        _print_estimate(pairs, folders, prompt, ds, out)
        if pairs and not args.yes and not confirm_send(len(pairs), stdin, out):
            return EXIT_REFUSED
        judge = (judge_factory or openrouter_judge)(ds, api_key)
        run_id = new_run_id()
        plan = build_plan(pairs, folders, judge, prompt, run_id=run_id, out=out)
        counts: dict[str, int] = {}
        for p in plan["pairs"]:
            counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
        usage = plan["usage"]
        cost = f", cost ${usage['cost_usd']:.4f}" if usage["cost_usd"] is not None else ""
        print(
            "noblivion dedup: verdicts "
            + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
            + f"; used {usage['prompt_tokens']} input and {usage['completion_tokens']} "
            f"output tokens{cost}",
            file=out,
        )
        path = plan_path(settings, run_id)
        _private_dir(path.parent)
        write_atomic(path, json.dumps(plan, indent=1, sort_keys=True).encode("utf-8"))
        print(
            f"noblivion dedup: plan {run_id} written to {path}\n"
            f"  read it, then run: noblivion dedup apply {run_id}",
            file=out,
        )
        write_status(
            settings, env, mode="plan", run_id=run_id, merged=0, proposals=counts.get("MERGE", 0)
        )
        return EXIT_OK

    lock = settings.index_lock_path
    try:
        with indexer.index_lock(lock, timeout_s=60.0):
            if args.command == "apply":
                plan = load_plan(settings, args.run_id)
                n = apply_plan(conn, settings, plan, folders=folders, out=out)
                write_status(settings, env, mode="apply", run_id=args.run_id, merged=n, proposals=0)
                print(f"noblivion dedup: {n} merged in run {args.run_id}", file=out)
                if n:
                    print(f"  undo: noblivion dedup undo {args.run_id}", file=out)
                return EXIT_OK
            pair = None
            if args.pair:
                parts = [p.strip() for p in args.pair.split(",")]
                if len(parts) != 2 or not all(parts):
                    print("noblivion dedup: --pair needs A.md,B.md", file=out)
                    return EXIT_USAGE
                pair = (parts[0], parts[1])
            n = undo_run(conn, settings, args.run_id, pair=pair, out=out)
            write_status(settings, env, mode="undo", run_id=args.run_id, merged=0, proposals=0)
            print(f"noblivion dedup: {n} steps undone in run {args.run_id}", file=out)
            return EXIT_OK
    except indexer.LockTimeoutError:
        print("noblivion dedup: the index lock is busy; try again", file=out)
        return EXIT_LOCKED
