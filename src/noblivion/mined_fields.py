# SPDX-License-Identifier: AGPL-3.0-or-later
"""The labels, the rule line and the apply line of a mined row (NOBLIVION-105).

A memory file carries a ``rule:`` and an ``apply:`` line in its front matter. A
mined row has no file, so the miner derives both lines from the mined text, and
the index answer sends them for a mined row. This module holds that one
derivation, so a row that an older miner wrote (no ``index_text``, no labels)
gets the same lines at answer time as a row that the miner writes today.

Standard library only. No other ``noblivion`` module is imported.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

KIND_CORRECTION = "correction"
KIND_REVIEW = "review_request_changes"
KIND_TOOL_ERROR = "tool_error"

RULE_MAX_CHARS = 200
APPLY_MAX_CHARS = 200
MAX_LABELS = 6
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,39}")
_ERROR_CLASS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:error|exception))\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A cut that leaves fewer characters than this is a stop inside a quoted phrase or a
# list number ("1. Do x"), not the end of the rule.
_MIN_SENTENCE_CHARS = 12

_USER_TURN_RE = re.compile(r"The user wrote: (.*?)(?: The assistant had just said:|$)", re.S)
_ERROR_RE = re.compile(r" failed with: (.*)$", re.S)
_FINDINGS_RE = re.compile(r"Findings: (.*)$", re.S)
_TOOL_RE = re.compile(r" in session \S+: (\S+)")

_APPLY = {
    KIND_CORRECTION: (
        "The user corrected the assistant on this point. Do not repeat what the user corrected."
    ),
    KIND_REVIEW: (
        "A review asked for changes on this point. Check your change against these findings."
    ),
}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def first_sentence(text: str, limit: int = RULE_MAX_CHARS) -> str:
    """The first sentence of ``text`` on one line, at most ``limit`` characters."""
    flat = _flat(text)
    for match in _SENTENCE_END.finditer(flat):
        if match.start() >= _MIN_SENTENCE_CHARS:
            return _clip(flat[: match.start()], limit)
    return _clip(flat, limit)


_BODY_RE = {
    KIND_CORRECTION: _USER_TURN_RE,
    KIND_TOOL_ERROR: _ERROR_RE,
    KIND_REVIEW: _FINDINGS_RE,
}


def rule_of(kind: str, text: str) -> str:
    """The derived rule line of a mined text: the first sentence of the part that holds the
    lesson (the user's words, the error, the findings), else of the whole text."""
    pattern = _BODY_RE.get(kind)
    match = pattern.search(text) if pattern else None
    return first_sentence(match.group(1) if match else text)


def apply_of(kind: str, text: str) -> str:
    """The derived apply line of a mined text, by kind. Fixed text: no mined word enters it
    except the tool name of a tool error."""
    if kind == KIND_TOOL_ERROR:
        match = _TOOL_RE.search(text)
        tool = match.group(1) if match else "A tool call"
        return _clip(
            f"{tool} failed with this error before. Fix the cause. Do not retry the same call.",
            APPLY_MAX_CHARS,
        )
    return _APPLY.get(kind, "")


def index_text(kind: str, text: str) -> str:
    """The ``index_text`` column of a mined row: the rule line, then the apply line."""
    rule = rule_of(kind, text)
    apply = apply_of(kind, text)
    return "\n".join(part for part in (rule, apply) if part)


def split_index_text(value: str) -> tuple[str, str]:
    """``(rule, apply)`` from an ``index_text`` that ``index_text`` wrote."""
    lines = [line for line in value.split("\n") if line.strip()]
    return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")


def fields_of(kind: str, stored: str, text: str) -> tuple[str, str]:
    """``(rule, apply)`` of a mined row: the stored lines, else derived from the text."""
    rule, apply = split_index_text(stored)
    if rule:
        return rule, apply or apply_of(kind, text)
    return rule_of(kind, text), apply_of(kind, text)


def _label(value: str) -> str:
    value = value.strip().lower()
    return value if _LABEL_RE.fullmatch(value) else ""


def command_head(command: str) -> str:
    """The program of a shell command: the first word that is not an assignment or ``sudo``,
    without its folder."""
    for word in command.split():
        if "=" in word.split("/")[0] or word in ("sudo", "env", "time", "nohup"):
            continue
        return word.rsplit("/", 1)[-1]
    return ""


def error_class(error: str) -> str:
    """The first word of an error text that names an error class (``KeyError``,
    ``tool_use_error``), else ""."""
    match = _ERROR_CLASS_RE.search(error)
    return match.group(1) if match else ""


def labels_of_error(tool: str, command: str, error: str) -> list[str]:
    """The labels of a tool error row: the tool name, the command head and the error class,
    lower case, without repeats. A value that is not a plain word is left out."""
    found: list[str] = []
    for value in (tool, command_head(command), error_class(error)):
        label = _label(value)
        if label and label not in found:
            found.append(label)
    return found[:MAX_LABELS]


def join_labels(values: Sequence[str]) -> str:
    """The labels as the JSON list that the ``labels`` column holds."""
    return json.dumps(list(values))
