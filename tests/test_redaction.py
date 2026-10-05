# SPDX-License-Identifier: AGPL-3.0-or-later
"""At-rest secret redaction (design doc 0001, section 5.7).

Every secret below is fictional and built at run time from parts, so no
secret-shaped literal sits in the repository for a leak scanner to find.
"""

from __future__ import annotations

import itertools
import re
import time

import pytest

import secret_forms
from hookload import load_hook
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
KEY_BLOCK = secret_forms.key_block("RSA")
KEY_BLOCK_CUT = secret_forms.key_block("EC", end=False)
KEY_BLOCK_IN_JSON = secret_forms.key_block("").replace("\n", "\\n") + "\\n"
KEY_BLOCK_PGP = secret_forms.key_block("PGP", headers=("Version: Example 1.0",))


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
        (f"before\n{KEY_BLOCK}\nafter", f"before\n{REDACTION_TOKEN}\nafter"),
        (f"before\n{KEY_BLOCK_CUT}\n", f"before\n{REDACTION_TOKEN}\n"),
        (f"before\n{KEY_BLOCK_CUT}\n\nafter\n", f"before\n{REDACTION_TOKEN}\n\nafter\n"),
        (f"before\n{KEY_BLOCK_PGP}\nafter", f"before\n{REDACTION_TOKEN}\nafter"),
        (
            f'{{"private_key": "{KEY_BLOCK_IN_JSON}"}}',
            f'{{"private_key": "{REDACTION_TOKEN}\\n"}}',
        ),
        (f"use {secret_forms.GLPAT} here", f"use {REDACTION_TOKEN} here"),
        (f"use {secret_forms.GOOGLE_KEY} here", f"use {REDACTION_TOKEN} here"),
        (f"use {secret_forms.PAYMENT_KEY} here", f"use {REDACTION_TOKEN} here"),
        (f"use {secret_forms.RESTRICTED_KEY} here", f"use {REDACTION_TOKEN} here"),
        (f"use {secret_forms.HF_TOKEN} here", f"use {REDACTION_TOKEN} here"),
        (f"use {secret_forms.NPM_TOKEN} here", f"use {REDACTION_TOKEN} here"),
        ('{"password": "hunter2hunter2"}', f'{{"password": "{FIELD_TOKEN}"}}'),
        (
            '{"user":"alice","password":"hunter2hunter2","port":5432}',
            f'{{"user":"alice","password":"{FIELD_TOKEN}","port":5432}}',
        ),
        ('{"password": "correct horse battery"}', f'{{"password": "{FIELD_TOKEN}"}}'),
        ('{"password": "a \\"quoted\\" word"}', f'{{"password": "{FIELD_TOKEN}"}}'),
        (f"{{'api_key': '{secret_forms.API_KEY}'}}", f"{{'api_key': '{FIELD_TOKEN}'}}"),
        ('password: "hunter2hunter2', f"password: {FIELD_TOKEN}"),
        ("DB_PASS=hunter2hunter2", f"DB_PASS={FIELD_TOKEN}"),
        ("pass = hunter2hunter2", f"pass = {FIELD_TOKEN}"),
        ('{"db_pass": "hunter2hunter2"}', f'{{"db_pass": "{FIELD_TOKEN}"}}'),
        ('{"pass": "hunter2hunter2"}', f'{{"pass": "{FIELD_TOKEN}"}}'),
        ("login?user=bob&pass=hunter2hunter2", f"login?user=bob&pass={FIELD_TOKEN}"),
        ("MYSQL_PWD=hunter2hunter2", f"MYSQL_PWD={FIELD_TOKEN}"),
        ('{"mysql_pwd": "hunter2hunter2"}', f'{{"mysql_pwd": "{FIELD_TOKEN}"}}'),
        ('password="correct horse battery"', f"password={REDACTION_TOKEN}"),
        ("token='correct horse' next", f"token={REDACTION_TOKEN} next"),
        ('password="a \\"quoted\\" word" next', f"password={REDACTION_TOKEN} next"),
        ('secret="correct horse', f"secret={REDACTION_TOKEN} horse"),
        (
            "<password>hunter2hunter2</password>",
            f"<password>{REDACTION_TOKEN}</password>",
        ),
        ("token: 12345678x", f"token: {FIELD_TOKEN}"),
        ('{"token": "12345678"}', f'{{"token": "{FIELD_TOKEN}"}}'),
        ("token=12345678", f"token={REDACTION_TOKEN}"),
    ],
)
def test_redacts(text, expected):
    assert redact_at_rest(text) == expected


def test_a_pass_key_with_a_colon_counts_when_its_value_looks_like_a_secret():
    # `pass` is a word of prose too. After ":" with no quotes the value must be
    # one word of 8 or more characters with a digit, a symbol, or a capital
    # letter after a small one.
    for value in ("hunter2hunter2", "12345678", "correct-horse", "hunterHunter", "$ecretword"):
        assert redact_at_rest(f"smtp-pass: {value}") == f"smtp-pass: {FIELD_TOKEN}", value
    assert redact_at_rest("smtp:\n  smtp-pass: hunter2hunter2\n") == (
        f"smtp:\n  smtp-pass: {FIELD_TOKEN}\n"
    )
    assert redact_at_rest('db_pass: "hunter2hunter2"') == f'db_pass: "{FIELD_TOKEN}"'
    assert redact_at_rest("mysql_pwd: hunter2hunter2") == f"mysql_pwd: {FIELD_TOKEN}"
    plain = ("complete", "Complete", "COMPLETE", "optimizer.", "enabled,", "hunter2", "(skipped)")
    for value in plain:
        assert redact_at_rest(f"first_pass: {value}") == f"first_pass: {value}", value
    # `PASS` in capitals and `pwd` alone are not a key in front of ":".
    for text in ("PASS: TestIndexer12", "pwd: /srv/app/current"):
        assert redact_at_rest(text) == text
    assert redact_at_rest("smtp-pass=hunter2") == f"smtp-pass={FIELD_TOKEN}"
    assert redact_at_rest('"smtp-pass": "hunter2"') == f'"smtp-pass": "{FIELD_TOKEN}"'


def test_a_plain_pass_key_at_the_start_of_a_line_counts_when_its_value_looks_like_a_secret():
    # `pass: value` as a line of its own, also after an indent (YAML).
    plain, symbols = secret_forms.PLAIN_PASS, secret_forms.SYMBOL_PASS
    assert redact_at_rest(f"pass: {plain}") == f"pass: {FIELD_TOKEN}"
    assert redact_at_rest(f"  pass: {symbols}") == f"  pass: {FIELD_TOKEN}"
    assert redact_at_rest(f"smtp:\n  user: alice\n  pass: {plain}\n  port: 587\n") == (
        f"smtp:\n  user: alice\n  pass: {FIELD_TOKEN}\n  port: 587\n"
    )
    assert redact_at_rest(f'\tpass: "{plain}"') == f'\tpass: "{FIELD_TOKEN}"'
    assert redact_at_rest(f"User: alice\nPass: {plain}") == f"User: alice\nPass: {FIELD_TOKEN}"
    # `PASS` in capitals is a word of a test report, a plain word is not a
    # secret, and `pass` in the middle of a line is prose.
    for text in (
        "PASS: test_name",
        f"PASS: {plain}",
        "pass: complete",
        "  pass: complete",
        "The first pass: read the file",
        f"The first pass: {plain}",
        f"pass:\n  {plain}",
    ):
        assert redact_at_rest(text) == text, text


def test_a_number_is_a_secret_for_a_password_key_and_a_count_for_a_token_key():
    for key in ("password", "passwd", "secret", "api_key", "client_secret", "access_key"):
        assert redact_at_rest(f"{key}: 12345678") == f"{key}: {FIELD_TOKEN}", key
        # the comma and the brace of a JSON line stay
        assert redact_at_rest(f'{{"{key}": 12345678, "n": 1}}') == (
            f'{{"{key}": {FIELD_TOKEN}, "n": 1}}'
        )
        assert redact_at_rest(f'{{"{key}": 12345678}}') == f'{{"{key}": {FIELD_TOKEN}}}'
        for word in ("true", "false", "null"):
            assert redact_at_rest(f'{{"use_{key}": {word}}}') == f'{{"use_{key}": {word}}}'
    assert redact_at_rest("pwd: 12345678") == f"pwd: {FIELD_TOKEN}"
    for text in ("token: 12345678", '{"input_token": 12345678}', "token: 0.000015"):
        assert redact_at_rest(text) == text


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
        "all 12 tests passed: see the report",
        "bypass=disabled and compass: north-east",
        "--- PASS: TestIndexer (0.01s)",
        "the first pass: compile everything",
        *secret_forms.ORDINARY,
    ],
)
def test_keeps_text_that_is_not_a_secret(text):
    assert redact_at_rest(text) == text


@pytest.mark.parametrize("text", secret_forms.NOTES)
def test_keeps_the_text_of_a_note(text):
    assert redact_at_rest(text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    [form[1:] for form in secret_forms.FORMS],
    ids=[form[0] for form in secret_forms.FORMS],
)
def test_removes_every_shared_secret_form(text, secret):
    out = redact_at_rest(text)
    assert out != redaction.REDACTION_FAILED_TOKEN
    assert secret not in out
    assert redact_at_rest(out) == out


def test_the_hook_redactor_holds_the_same_shared_shapes():
    # The hooks run without the package, so they hold a copy of the table.
    hook = load_hook("memory_text", "memory_text_t_redaction")
    assert hook._SHARED_SHAPES == redaction.SHARED_SHAPES
    # Both sides use every row of the table.
    in_use = {pattern.pattern for pattern, _repl in redaction._SHAPE_PATTERNS}
    assert all(shape in in_use for shape, _flags in redaction.SHARED_SHAPES)
    assert all(row in hook._SECRET_PATTERNS for row in hook._SHARED_SHAPES)


@pytest.mark.parametrize(
    "text",
    [text for _name, text in secret_forms.HOSTILE],
    ids=[name for name, _text in secret_forms.HOSTILE],
)
def test_new_rules_run_in_linear_time(text):
    # Each new or changed rule alone, on 100 KB: a quadratic rule needs many
    # seconds. The next test times the whole redactor.
    rules = [re.compile(shape, flags) for shape, flags in redaction.SHARED_SHAPES]
    rules.append(redaction._SECRET_FIELD_RE)
    rules.append(redaction._KEYVALUE_PATTERNS[0][0])
    start = time.perf_counter()
    for rule in rules:
        rule.sub("", text)
    assert time.perf_counter() - start < 1.0


# 100 KB runs of the characters of a URL scheme, a URL user or a mail name,
# with and without the "://" or "@" where a match starts. The URL rule and the
# email rule read each such run once (NOBLIVION-62).
_URL_AND_EMAIL_HOSTILE = [
    ("letters", "a" * secret_forms.SIZE),
    ("dashes", "-" * secret_forms.SIZE),
    ("digits", "1" * secret_forms.SIZE),
    ("letters and dashes", "a-" * (secret_forms.SIZE // 2)),
    ("-p words", "-p" * (secret_forms.SIZE // 2)),
    ("pass- words", "pass-" * (secret_forms.SIZE // 5)),
    ("letters and dots", "a." * (secret_forms.SIZE // 2)),
    ("scheme characters", "ab+c.d-" * (secret_forms.SIZE // 7)),
    ("a BEGIN word and capitals", "-----BEGIN " + "A" * secret_forms.SIZE),
    ("URLs with a colon and no @", "x://a:b" * (secret_forms.SIZE // 7)),
    ("authorities with a colon", "://a:" * (secret_forms.SIZE // 5)),
    ("a long name and one @ at the end", "a" * secret_forms.SIZE + "@"),
]


@pytest.mark.parametrize(
    "text",
    [text for _name, text in _URL_AND_EMAIL_HOSTILE],
    ids=[name for name, _text in _URL_AND_EMAIL_HOSTILE],
)
def test_the_whole_redactor_runs_in_linear_time(text):
    # The store runs the redactor at index time and on each hit of a search,
    # so a slow note held back recall (NOBLIVION-62).
    start = time.perf_counter()
    redact_at_rest(text)
    assert time.perf_counter() - start < 1.0


def test_cluster_secret_data_only_in_a_secret_manifest():
    # The key is not a secret field name: a key that ends in "pass" is masked
    # in any text when its value looks like a secret (NOBLIVION-46).
    value = fake("", 24, "QWxhZGRpbjpvcGVuIHNlc2FtZQ")
    manifest = f"apiVersion: v1\nkind: Secret\ndata:\n  db-conn: {value}\n"
    assert f"  db-conn: {FIELD_TOKEN}" in redact_at_rest(manifest)
    config_map = f"apiVersion: v1\nkind: ConfigMap\ndata:\n  db-conn: {value}\n"
    assert redact_at_rest(config_map) == config_map
    assert f"  db-pass: {FIELD_TOKEN}" in redact_at_rest(config_map.replace("db-conn", "db-pass"))


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
    'password": "',
    "DB_PASS=",
    '"',
    KEY_BLOCK_CUT,
    secret_forms.GLPAT,
    'password="',
    "MYSQL_PWD=",
    "<password>",
    "</password>",
    secret_forms.begin_line("PGP") + "\n",
    "smtp-pass: ",
    "12345678",
    "\n  pass: ",
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
