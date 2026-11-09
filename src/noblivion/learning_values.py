# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared learning values and validation, independent of ledgers and extraction."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class LearningError(ValueError):
    """Invalid input or a refused learning operation."""


class SemanticSnapshotChanged(LearningError):
    """Managed memories changed after semantic comparison; retry the comparison."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_text(value: Any, name: str, maximum: int = 512, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not value.strip() and not empty):
        raise LearningError(f"invalid {name}")
    if "\x00" in value:
        raise LearningError(f"invalid {name}")
    return value


def validate_safe_text(text: str) -> None:
    # Keep the validator authoritative at every writer, including direct CLI edits.
    from .redaction import contains_secret

    if contains_secret(text):
        raise LearningError("unsafe source or memory content")
