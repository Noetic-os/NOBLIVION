# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small helpers shared by the modules: private files, hashes, tool loading."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import types
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent.parent
REPO = TOOLS.parent
DATA_NAME = "noblivion-noblivion"


_LOAD_LOCK = threading.RLock()


def load_tool(relative: str, alias: str) -> types.ModuleType:
    """A module of ``tools/`` by path. The tools are scripts, not a package. The lock
    keeps a second thread from taking a module that is still being executed."""
    with _LOAD_LOCK:
        if alias in sys.modules:
            return sys.modules[alias]
        spec = importlib.util.spec_from_file_location(alias, TOOLS / relative)
        if spec is None or spec.loader is None:
            raise ImportError(relative)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod  # a dataclass resolves its annotations here
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(alias, None)
            raise
        return mod


def private_dir(path: Path) -> Path:
    """A folder of mode 0700, made if absent."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """A file of mode 0600, written whole through a temporary name."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def append_private(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    append_private(path, json.dumps(row, sort_keys=True))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def salted_hash(salt: str, text: str) -> str:
    """The form of a text in an output file: a short salted hash, never the text."""
    return hashlib.sha256((salt + "\0" + text).encode("utf-8")).hexdigest()[:12]


def json_text(value: Any) -> str:
    return json.dumps(value, indent=1, sort_keys=True) + "\n"


def percentile(values: Iterable[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return ordered[index]


def summarize_ms(values: Iterable[float]) -> dict[str, Any]:
    values = list(values)
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.5), 1),
        "p95": round(percentile(values, 0.95), 1),
        "max": round(max(values), 1) if values else 0.0,
    }
