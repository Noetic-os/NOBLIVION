# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm e: the local model, over its HTTP API on loopback only.

Two jobs, both optional: paraphrase queries once, frozen before day 1; and a
secondary relevance judge on the pooled pairs. The judge counts only when Cohen's
kappa against the hand labels is 0.6 or more. The model runs on this host
(``127.0.0.1:11434``); no hosted API is used. The host must pass the socket guard:
``generate`` refuses any other address.

This module does not start a model run by itself. The localhost unit calls it
at night, one request at a time, with a cap on the number of calls.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import stats
from .config import PARAMS
from .sandbox import is_loopback
from .util import write_private

DEFAULT_URL = "http://127.0.0.1:11434"
PARAPHRASE_PROMPT = (
    "Rewrite the question below in different words. Keep its meaning. Use none of its "
    "content words. Answer with the question only.\n\nQuestion: {text}"
)
JUDGE_PROMPT = (
    "A user asked: {query}\n\nA saved note says: {note}\n\nDoes the note help with the "
    "user's request? Answer y or n."
)


class LocalModel:
    def __init__(self, model: str, url: str = DEFAULT_URL, timeout: float = 120.0) -> None:
        host = urllib.request.urlparse(url).hostname or ""
        if not is_loopback((host,)):
            raise ValueError(f"the local model must be on loopback, not {host}")
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.calls = 0

    def generate(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "seed": 7121},
            }
        ).encode()
        req = urllib.request.Request(
            self.url + "/api/generate", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as answer:  # noqa: S310
            self.calls += 1
            return str(json.loads(answer.read()).get("response", ""))


def freeze_paraphrases(
    queries: Sequence[Mapping[str, Any]], out: Path, generate: Callable[[str], str]
) -> dict[str, str]:
    """Write the paraphrase of each query once. Refuses to overwrite: the file is
    frozen before day 1 and its hash goes into the manifest."""
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} exists: paraphrases are frozen once")
    rows = {q["id"]: generate(PARAPHRASE_PROMPT.format(text=q["prompt"])).strip() for q in queries}
    write_private(out, json.dumps(rows, indent=1, sort_keys=True) + "\n")
    return rows


def vote(generate: Callable[[str], str], query: str, note: str) -> str:
    answer = generate(JUDGE_PROMPT.format(query=query, note=note[:1500])).strip().lower()
    return "y" if answer.startswith("y") else "n"


def usable(
    judge_labels: Sequence[str], hand_labels: Sequence[str], minimum: float = PARAMS.label_kappa_min
) -> dict[str, Any]:
    """Cohen's kappa against the hand labels; the judge is secondary and counts only
    at or above ``minimum``."""
    kappa = stats.cohen_kappa(judge_labels, hand_labels)
    return {"kappa": round(kappa, 3), "n": len(hand_labels), "usable": kappa >= minimum}
