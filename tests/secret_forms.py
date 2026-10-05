# SPDX-License-Identifier: AGPL-3.0-or-later
"""The secret forms that both redactors must remove, and the text that they
must keep (NOBLIVION-46).

The at-rest redactor of the store and the redactor of the hooks
(``hooks/memory_text.py``) are two pieces of code. ``tests/test_redaction.py``
feeds every form below to the first, ``tests/test_hook_memory_text.py`` feeds
it to the second. A form added here is checked on both sides, so the two
cannot drift apart again without a failing test.

Every secret is fictional and built at run time from parts, so no
secret-shaped literal sits in the repository for a leak scanner to find.
The token bodies are lower-case letters only: no rule for a long mixed-case
string removes them, only the rule for the prefix.

Valid for Python 3.9: the hook tests import this file.
"""

from __future__ import annotations

ALNUM = "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ1cF0xR"
LOWER = "abcdefghijklmnopqrstuvwxyz"
B64 = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"


def fake(prefix: str, n: int, alphabet: str = ALNUM) -> str:
    return prefix + (alphabet * (n // len(alphabet) + 1))[:n]


KEY_BODY = fake("", 64, B64)
KEY_TAIL = fake("", 22, B64[::-1]) + "=="


def key_name(kind: str) -> str:
    """The name in the BEGIN and END line of a key block of ``kind`` ("RSA",
    "" for the plain form, "PGP" for the PGP form)."""
    name = (kind + " " if kind else "") + "PRIVATE " + "KEY"
    return name + " BLOCK" if kind == "PGP" else name


def begin_line(kind: str) -> str:
    return f"-----BEGIN {key_name(kind)}-----"


def end_line(kind: str) -> str:
    return f"-----END {key_name(kind)}-----"


def key_block(kind: str, end: bool = True, headers: tuple[str, ...] = ()) -> str:
    """A private key block of ``kind``, in the shape a key tool writes: full
    lines, then a short padded line. ``headers`` are the ``Name: value`` lines
    of an encrypted or PGP block; a blank line follows them. A PGP block has
    the blank line with no header too. With ``end`` False the END line is cut
    off."""
    lines = [begin_line(kind), *headers]
    if headers or kind == "PGP":
        lines.append("")
    lines += [KEY_BODY, KEY_BODY, KEY_TAIL]
    if end:
        lines.append(end_line(kind))
    return "\n".join(lines)


PASSWORD = "hunter2" + "hunter2"
DIGITS = "1234" + "5678"  # a password of digits only
# A value with spaces. A rule that stops at the first space leaves the tail.
PHRASE = "tango uniform delta"
PHRASE_TAIL = "uniform delta"
API_KEY = fake("", 24)
GLPAT = fake("gl" + "pat-", 20, LOWER)
GOOGLE_KEY = fake("AI" + "za", 35, LOWER)
PAYMENT_KEY = fake("sk" + "_live_", 24, LOWER)
RESTRICTED_KEY = fake("rk" + "_live_", 24, LOWER)
HF_TOKEN = fake("h" + "f_", 34, LOWER)
NPM_TOKEN = fake("np" + "m_", 36, LOWER)
ENCRYPTED = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-256-CBC," + "0123ABCD" * 4)

# (name, text, the secret that must be gone)
FORMS: list[tuple[str, str, str]] = [
    ("openssh key block", f"The key file:\n{key_block('OPENSSH')}\nKeep it safe.", KEY_BODY),
    ("rsa key block", f"The key file:\n{key_block('RSA')}\nKeep it safe.", KEY_BODY),
    ("ec key block", f"The key file:\n{key_block('EC')}\nKeep it safe.", KEY_BODY),
    ("plain key block", f"The key file:\n{key_block('')}\nKeep it safe.", KEY_BODY),
    ("key block with no END line", f"The key file:\n{key_block('RSA', end=False)}", KEY_BODY),
    (
        "key block with no END line, then a paragraph",
        f"The key file:\n{key_block('EC', end=False)}\n\nKeep it safe.",
        KEY_BODY,
    ),
    ("pgp key block", f"The key file:\n{key_block('PGP')}\nKeep it safe.", KEY_BODY),
    (
        "pgp key block with a header line",
        f"The key file:\n{key_block('PGP', headers=('Version: Example 1.0',))}\nKeep it safe.",
        KEY_BODY,
    ),
    ("pgp key block with no END line", f"The key file:\n{key_block('PGP', end=False)}", KEY_BODY),
    ("encrypted key block", f"{key_block('RSA', headers=ENCRYPTED)}\nKeep it safe.", KEY_BODY),
    (
        "encrypted key block with no END line",
        key_block("RSA", end=False, headers=ENCRYPTED),
        KEY_BODY,
    ),
    ("key block in one line", key_block("RSA").replace("\n", " "), KEY_BODY),
    ("key block in a string", key_block("").replace("\n", "\\n"), KEY_BODY),
    (
        "key block in a string, no END line",
        key_block("", end=False).replace("\n", "\\r\\n"),
        KEY_BODY,
    ),
    ("json password", '{"password": "' + PASSWORD + '"}', PASSWORD),
    ("json api_key", '{"api_key": "' + API_KEY + '"}', API_KEY),
    ("json access_key", '{"access_key": "' + API_KEY + '"}', API_KEY),
    ("compact json", '{"user":"alice","password":"' + PASSWORD + '","port":5432}', PASSWORD),
    ("json value with spaces", '{"password": "' + PHRASE + '"}', PHRASE_TAIL),
    ("pass suffix", f"DB_PASS={PASSWORD}", PASSWORD),
    ("pass alone", f"login?user=alice&pass={PASSWORD}", PASSWORD),
    ("json pass suffix", '{"DB_PASS": "' + PASSWORD + '"}', PASSWORD),
    ("json pass suffix, value with spaces", '{"db_pass": "' + PHRASE + '"}', PHRASE_TAIL),
    ("pwd suffix", f"MYSQL_PWD={PASSWORD}", PASSWORD),
    # A password key with a value of digits only. A count key (`input_token`)
    # keeps a number, see ORDINARY.
    ("password of digits", f"password: {DIGITS}", DIGITS),
    ("password of digits after =", f"db_password={DIGITS}", DIGITS),
    ("json password of digits", '{"password": ' + DIGITS + ', "port": 5432}', DIGITS),
    ("secret of digits", f"client_secret: {DIGITS}", DIGITS),
    ("api key of digits", f"api_key: {DIGITS}", DIGITS),
    ("pwd of digits", f"pwd: {DIGITS}", DIGITS),
    ("pwd suffix of digits", f"mysql_pwd: {DIGITS}", DIGITS),
    # A key that ends in `pass` with ":" (YAML): the value looks like a secret.
    ("yaml pass suffix", f"smtp:\n  smtp-pass: {PASSWORD}\n", PASSWORD),
    ("yaml pass suffix in capitals", f"DB_PASS: {PASSWORD}", PASSWORD),
    ("yaml pass suffix, value in quotes", f'db_pass: "{PASSWORD}"', PASSWORD),
    ("yaml pass suffix of digits", f"smtp_pass: {DIGITS}", DIGITS),
    ("yaml pass suffix with a symbol", "redis.pass: correct-horse-battery", "horse"),
    ("yaml pass suffix with mixed case", "smtp_pass: hunterHunter", "hunterHunter"),
    ("value in quotes with spaces", f'password="{PHRASE}"', PHRASE_TAIL),
    ("value in single quotes with spaces", f"SECRET_KEY='{PHRASE}'", PHRASE_TAIL),
    ("yaml value in quotes with spaces", f'password: "{PHRASE}"', PHRASE_TAIL),
    ("xml password element", f"<password>{PASSWORD}</password>", PASSWORD),
    ("xml password element with spaces", f"<password>{PHRASE}</password>", PHRASE_TAIL),
    ("glpat- prefix", f"the token {GLPAT} was used", GLPAT),
    ("AIza prefix", f"the key {GOOGLE_KEY} was used", GOOGLE_KEY),
    ("sk_live_ prefix", f"the key {PAYMENT_KEY} was used", PAYMENT_KEY),
    ("rk_live_ prefix", f"the key {RESTRICTED_KEY} was used", RESTRICTED_KEY),
    ("hf_ prefix", f"the token {HF_TOKEN} was used", HF_TOKEN),
    ("npm_ prefix", f"the token {NPM_TOKEN} was used", NPM_TOKEN),
]

# Text that no redactor may change.
ORDINARY: list[str] = [
    "Ask the admin to reset the password.",
    'The "password" field must not be empty.',
    "See https://example.com/docs/setup?page=2 for the steps.",
    "commit 2656d01f3a9c is the fix",
    "The second pass over the list is faster.",
    '{"passed": 2730, "failed": 0}',
    "npm_config_registry and hf_hub_download are plain names.",
    "AIza and sk_live_ are prefixes of two key types.",
    "The file starts with a BEGIN line and stops with an END line.",
    # A line that only names the BEGIN line of a key block is prose.
    f"The file starts with {begin_line('RSA')} on the first line.\n\nThe next paragraph stays.",
    f"The loader checks for the `{begin_line('OPENSSH')}` header and rejects every other file.",
    f"A key file starts with {begin_line('')} and ends with {end_line('')}.",
    f"{begin_line('PGP')} is the header of an exported key.",
    f"The header line\n{begin_line('RSA')}\nmust be the first line of the file.",
    "The header `-----BEGIN CERTIFICATE-----` marks a public certificate, not a key.",
    # A number, true, false or null is not a secret.
    '{"input_token": 12345678, "output_token": 99}',
    '{"prompt_tokens": 1200, "completion_tokens": 350, "total_tokens": 1550}',
    '{"max_token": 819200}',
    '{"price_per_token": 0.000015, "use_token": false, "token": null}',
    '{"elapsed_ms": 4521, "port": 5432, "pid": 31337}',
    '{"passed": 2730, "failed": 0, "pass_rate": 0.9993}',
    '{"retries": 3, "rotate_secret": false, "secret_count": 12}',
    "<password> is the name of the XML element.",
    "The config holds `<password>...</password>`; fill it in before the first run.",
    "`<password>${env.DB_PASSWORD}</password>` reads the value from the environment.",
    "rk_live_ and sk_live_ are prefixes of two payment key types.",
    "The compiler runs the inlining pass before the dead code pass.",
    "The order of the optimizer passes is: inline, fold constants, remove dead code.",
    "A two-pass assembler resolves forward labels in the second pass.",
    "The first pass builds the index and the second pass ranks the rows.",
    "tests/test_indexer.py::test_namespaces_are_separate PASSED",
    "Result: 2730 passed, 2 skipped in 241.50s",
    "PASS  src/app.test.js (5.2 s)",
    "smoke test: PASS, load test: PASS, soak test: FAIL",
    "The CI log shows `PASS: 41, FAIL: 0, SKIP: 2`.",
    "Passes: 3 of 3",
    "The prompt used 1200 input tokens and 350 output tokens.",
    "The session ended at 100K tokens.",
    "The secret of a fast build is a warm cache.",
    "`git commit -m 'second pass'` was the last commit.",
    "The rate limit is 60 requests per minute for each API key.",
    "The pass manager runs `module_pass` before `function_pass`.",
    # A count key keeps a number. A password key does not, see FORMS.
    '{"input_tokens": 12345678, "token_count": 152000, "max_tokens": 16384}',
    '{"output_token": 99999999, "cached_token": 12345678}',
    '{"secret_count": 12345678, "password_length": 12345678}',
]

# Text of a note that the store redactor and the memory text redactor of the
# hooks must keep. The query redactor of the hooks (``redact``) is broader on
# purpose: it also takes a word after a key name as a value ("token budget").
NOTES: list[str] = [
    *ORDINARY,
    # `pass` is a word of a compiler and of a test report, not only a key.
    "first_pass: complete",
    "two-pass: compile",
    "compiler.pass: optimizer",
    "data-pass: nightly",
    "second_pass: skipped because the first pass found no change",
    "Each pass: parse, resolve the names, check the types, emit the code.",
    "multi-pass: enabled, single-pass: disabled",
    "lint-pass: clean, type-pass: 3 errors",
    "one-pass: 0.42s, two-pass: 0.61s",
    "high-pass: 80 Hz, low-pass: 12000 Hz",
    "--- PASS: TestIndexer (0.01s)",
    "PASS: test_scan_inserts_files (0.12s)",
    "passed: 2730, failed: 0, skipped: 2",
    "all 12 tests passed: see the report",
    "bypass=disabled and compass: north-east",
    # Token counts.
    "max_tokens: 4096",
    "MAX_TOKENS=16384",
    "total_tokens: 152000 at the handoff",
    "token count: 152000",
    "input_token: 12345678",
    "The token budget is 100K to 140K tokens for one session.",
    "Startup is 46K tokens, and every token added is paid again on every later turn.",
    "Use a token bucket for the rate limit.",
    "The password rule is in the guide.",
    "Set `password_min_length = 12` in the config.",
    "output_token: 99999999",
    "max_tokens: 16384, token_count: 152000",
    # A key that ends in `pass` with ":": the value is a plain word.
    "first_pass: Complete",
    "second_pass: finished, third_pass: optimized",
    "render-pass: disabled.",
    "lint_pass: (skipped)",
    "verify-pass: COMPLETE",
    "cleanup_pass: `disabled`",
    'db_pass: "{{ vault_db_pass }}"',
    "db_pass: ${DB_PASS}",
    "final-pass: complete; the report is in the log",
]

# (name, a hostile text of 100 KB or more): a new or changed rule must stay
# linear on each one.
_BEGIN = "-----BEGIN PRIVATE " + "KEY-----"
SIZE = 100_000
HOSTILE: list[tuple[str, str]] = [
    ("BEGIN lines, no END line", _BEGIN * (SIZE // len(_BEGIN) + 1)),
    ("one BEGIN line, a long body", _BEGIN + "A" * SIZE),
    ("a BEGIN line with a long name", "-----BEGIN " + "A " * (SIZE // 2)),
    ("END lines that never close", _BEGIN + ("-----END " + "A" * 20) * (SIZE // 29 + 1)),
    ("dashes", "-" * SIZE),
    ("letters", "a" * SIZE),
    ("a key name and spaces", "password" + " " * SIZE),
    ("key names", "password " * (SIZE // 9 + 1)),
    ("pass words", "_pass pass " * (SIZE // 11 + 1)),
    ("quoted keys", 'password":"' * (SIZE // 11 + 1)),
    ("single-quoted keys", "password':'" * (SIZE // 11 + 1)),
    ("a quote that never closes", 'password: "' + "a" * SIZE),
    ("escapes that never close", 'password: "' + '\\"' * (SIZE // 2)),
    ("AIza prefixes", "AIza" * (SIZE // 4)),
    ("glpat- prefixes", "glpat-" * (SIZE // 6 + 1)),
    ("sk_live_ prefixes", "sk_live_" * (SIZE // 8)),
    ("one long hf_ token", "hf_" + "a" * SIZE),
    ("npm_ prefixes", "npm_" * (SIZE // 4)),
    ("a quote and a long name", '"' + "a-" * (SIZE // 2)),
    ("quotes and names", '"secret-a' * (SIZE // 9 + 1)),
    ("BEGIN lines, one in each line", (_BEGIN + "\n") * (SIZE // len(_BEGIN) + 1)),
    ("a BEGIN line and spaces", _BEGIN + " " * SIZE),
    ("a BEGIN line and short runs", _BEGIN + "\nAAAAAAAAAAAAAAA" * (SIZE // 16 + 1)),
    ("a BEGIN line and header lines", _BEGIN + "\nA: b" * (SIZE // 5)),
    ("header lines that hold a BEGIN line", ("A: " + _BEGIN + "\n") * (SIZE // 31 + 1)),
    ("a BEGIN line and END words", _BEGIN + "\n" + "-----END " * (SIZE // 9)),
    ("rk_live_ prefixes", "rk_live_" * (SIZE // 8)),
    ("password elements", "<password>" * (SIZE // 10)),
    ("a password element that never closes", "<password>" + "a" * SIZE),
    ("password elements that never close", "<password>aaaa" * (SIZE // 14 + 1)),
    ("values that open a quote", 'password="' * (SIZE // 10)),
    ("values that open a quote, one in each line", 'password="a\n' * (SIZE // 12 + 1)),
    ("values that open two kinds of quote", "password=\"a password='a " * (SIZE // 24 + 1)),
    ("a value in quotes that never closes", 'token="' + "a" * SIZE),
    ("a key and a long number", "token: " + "1" * SIZE),
    ("a key, a colon and spaces", "token:" + " " * SIZE),
    ("keys and numbers", "token: 1 " * (SIZE // 9 + 1)),
    ("pass keys", "_pass=" * (SIZE // 6 + 1)),
    ("quoted pass keys", 'pass":"' * (SIZE // 7 + 1)),
    ("pwd keys and spaces", "_pwd " * (SIZE // 5) + "=" + " " * SIZE),
    ("session words", "session" * (SIZE // 7 + 1)),
    ("cookie words with a long name", "cookie-" * (SIZE // 7 + 1)),
    ("a session key and spaces", "session" + " " * SIZE),
    ("a quote and a long name with dots", '"' + "a." * (SIZE // 2)),
    ("a BEGIN line and a long text", _BEGIN + "\n" + "x " * (SIZE // 2)),
    ("a pass key and spaces", "pass" + " " * SIZE),
    ("passphrase words", "passphrase" * (SIZE // 10)),
    ("a pass key and a long plain word", "_pass: " + "a" * SIZE),
    ("pass keys with a plain word", "_pass: aaaaaaaa " * (SIZE // 16 + 1)),
    ("pass keys with a marker", "_pass:[REDACTED]" * (SIZE // 16 + 1)),
    ("pass keys with no value", "_pass:" * (SIZE // 6 + 1)),
    ("pass keys with symbols", "_pass: " + "a-" * (SIZE // 2)),
    ("pass keys with capitals", "_pass: " + "Aa" * (SIZE // 2)),
    ("a pass key and symbols only", "_pass: a" + "-" * SIZE),
    ("a password key and a long number", "password: " + "1" * SIZE + "x"),
    ("password keys and numbers", "password: 1 " * (SIZE // 12 + 1)),
    ("pwd keys and numbers", "pwd: 1234 " * (SIZE // 10)),
    ("quoted token keys and numbers", '"token": 1 ' * (SIZE // 11 + 1)),
    ("a quoted token key and a long number", '"token": ' + "1" * SIZE),
    ("a quoted token key and spaces", '"token":' + " " * SIZE),
    ("a quote and token words", '"' + "token" * (SIZE // 5)),
    ("a pwd key and a long number", "pwd: " + "1" * SIZE + "x"),
]
