# SPDX-License-Identifier: AGPL-3.0-or-later
"""At-rest secret redaction (design doc 0001, section 5.7).

Every secret below is fictional and built at run time from parts, so no
secret-shaped literal sits in the repository for a leak scanner to find.
"""

from __future__ import annotations

import itertools
import re

import pytest

from noblivion import redaction
from noblivion.redaction import EMAIL_TOKEN, FIELD_TOKEN, REDACTION_TOKEN, redact_at_rest

ALNUM = "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ1cF0xR"


def fake(prefix: str, n: int, alphabet: str = ALNUM) -> str:
    return prefix + (alphabet * (n // len(alphabet) + 1))[:n]


SK_KEY = fake("sk" + "-proj-", 40)
GH_CLASSIC = fake("gh" + "p_", 36)
GH_FINE = fake("github" + "_pat_", 70)
SLACK = fake("xo" + "xb-", 30)
BOT = "bot" + "123456789" + ":" + fake("", 35)
CLOUD_KEY = "AK" + "IA" + "Q3EXAMPLEQ3EXAMP"
JWT = "ey" + "J" + fake("", 20) + ".ey" + "J" + fake("", 30) + "." + fake("", 25)
BASIC = "YWxpY2U6c2VjcmV0cGFzc3dvcmQ="
BEARER_VALUE = "abcd" + "efgh" + "1234" * 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"key {SK_KEY} end", f"key {REDACTION_TOKEN} end"),
        (f"token {GH_CLASSIC}", f"token {REDACTION_TOKEN}"),
        (f"pat {GH_FINE}", f"pat {REDACTION_TOKEN}"),
        (f"slack {SLACK}", f"slack {REDACTION_TOKEN}"),
        (f"bot url {BOT}", f"bot url bot{REDACTION_TOKEN}"),
        (f"aws {CLOUD_KEY} x", f"aws {REDACTION_TOKEN} x"),
        (f"jwt {JWT}", f"jwt {REDACTION_TOKEN}"),
        (f"Authorization: Basic {BASIC}", f"Authorization: Basic {REDACTION_TOKEN}"),
        (
            f"Proxy-Authorization: Basic {BASIC}",
            f"Proxy-Authorization: Basic {REDACTION_TOKEN}",
        ),
        (f"curl -H 'Bearer {BEARER_VALUE}'", f"curl -H '{'Bearer'} {REDACTION_TOKEN}'"),
        (f"header Basic {BASIC}", f"header Basic {REDACTION_TOKEN}"),
        ("run --password=hunter2hunter2", f"run --password={REDACTION_TOKEN}"),
        ("run --token=abc123", f"run --token={REDACTION_TOKEN}"),
        ("mysql -u alice -p s3cretpw", f"mysql -u alice -p {REDACTION_TOKEN}"),
        ("export API_KEY=abcdef123", f"export API_KEY={REDACTION_TOKEN}"),
        ("password: correct-horse", f"password: {FIELD_TOKEN}"),
        ("client_secret: zyxwvu987", f"client_secret: {FIELD_TOKEN}"),
        (
            "postgres://alice:pa55word@db.example.com:5432/app",
            f"postgres://alice:{FIELD_TOKEN}@db.example.com:5432/app",
        ),
        ("://alice:pw@db.example.com/app", f"://alice:{FIELD_TOKEN}@db.example.com/app"),
        (
            "postgres://u:aB3/xY9+zQ==@db.example.com/prod",
            f"postgres://u:{FIELD_TOKEN}@db.example.com/prod",
        ),
        (
            "https://example.com/p?next=http://bob:pw@inner.example.com/x",
            f"https://example.com/p?next=http://bob:{FIELD_TOKEN}@inner.example.com/x",
        ),
        ("mail alice@example.com now", f"mail {EMAIL_TOKEN} now"),
    ],
)
def test_redacts(text, expected):
    assert redact_at_rest(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a basic rule for the team",
        "commit 3f9c2a7d1e8b4c6a9f0e2d4b6a8c0e1f3a5b7c9d is the fix",
        "sha256 " + "ab" * 32,
        "https://example.com/2026-01-01T12:00:00@rev",
        "docker run -p8080:80 demo",
        "the password rule is in the guide",
        "use a token bucket",
    ],
)
def test_keeps_text_that_is_not_a_secret(text):
    assert redact_at_rest(text) == text


def test_cluster_secret_data_only_in_a_secret_manifest():
    value = fake("", 24, "QWxhZGRpbjpvcGVuIHNlc2FtZQ")
    manifest = f"apiVersion: v1\nkind: Secret\ndata:\n  db-pass: {value}\n"
    assert f"  db-pass: {FIELD_TOKEN}" in redact_at_rest(manifest)
    config_map = f"apiVersion: v1\nkind: ConfigMap\ndata:\n  db-pass: {value}\n"
    assert redact_at_rest(config_map) == config_map


def test_word_boundary_created_by_a_replacement_still_settles():
    # Pass 1 masks the key in front of "Basic"; only then does "\bBasic" match.
    text = CLOUD_KEY + "Basic YYYYYYYYYYYY"
    out = redact_at_rest(text)
    assert out == f"{REDACTION_TOKEN}Basic {REDACTION_TOKEN}"
    assert redact_at_rest(out) == out


FRAGMENTS = [
    CLOUD_KEY,
    "Basic YYYYYYYYYYYY",
    "password=",
    "password: ",
    "token=",
    "Bearer ",
    BEARER_VALUE,
    "Authorization: ",
    "://u:",
    "pw@",
    "host.example.com",
    "-p ",
    " ",
    "alice@example.com",
    SK_KEY,
    "secret_key=",
]


def test_idempotent_over_fragment_combinations():
    for n in (2, 3):
        for combo in itertools.product(FRAGMENTS, repeat=n):
            text = "".join(combo)
            once = redact_at_rest(text)
            assert once != redaction.REDACTION_FAILED_TOKEN, text
            assert redact_at_rest(once) == once, text


def test_no_secret_survives_in_fragment_combinations():
    hidden = (CLOUD_KEY, SK_KEY, BEARER_VALUE)
    for combo in itertools.product(FRAGMENTS, repeat=2):
        out = redact_at_rest("".join(combo))
        for secret in hidden:
            if secret in "".join(combo) and combo[0] in ("Bearer ", "password=", "token="):
                assert secret not in out, combo


def test_fails_closed_when_a_pattern_raises(monkeypatch):
    def boom(_m):
        raise RuntimeError("bad replacement")

    monkeypatch.setattr(redaction, "_SHAPE_PATTERNS", [(re.compile("x"), boom)])
    assert redact_at_rest("x marks the spot") == redaction.REDACTION_FAILED_TOKEN


def test_fails_closed_when_the_text_does_not_settle(monkeypatch):
    flip = [(re.compile(r"A|B"), lambda m: "B" if m.group(0) == "A" else "A")]
    monkeypatch.setattr(redaction, "_SHAPE_PATTERNS", flip)
    assert redact_at_rest("A") == redaction.REDACTION_FAILED_TOKEN


def test_would_change():
    assert redaction.would_change(f"key {SK_KEY}")
    assert not redaction.would_change("deploy-checklist")
