# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostic failure, confidentiality, and read-only contracts."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from noblivion import config, db, doctor, launcher


@pytest.fixture
def installed(tmp_path, monkeypatch):
    folder = tmp_path / "private-data"
    folder.mkdir(mode=0o700)
    (folder / "venv").mkdir()
    config.install_stamp(folder).write_text("{}")
    (folder / "models").mkdir()
    token = folder / config.TOKEN_FILE
    token.write_text("a" * 64)
    token.chmod(0o600)
    (folder / config.STORE_JSON_FILE).write_text('{"port": 12345, "pid": 111}')
    database = folder / config.DB_FILE
    with sqlite3.connect(database) as conn:
        db.migrate(conn, random_base=1000000)
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id))")
    database.chmod(0o600)
    home = tmp_path / "client"
    home.mkdir()
    memory = tmp_path / "project" / "memory"
    memory.mkdir(parents=True)
    env = {"CODEX_HOME": str(home), "NOBLIVION_MEMORY_DIR": str(memory)}
    monkeypatch.setattr(launcher, "probe", lambda *a, **kw: {"ok": True, "secret": "health-canary"})
    monkeypatch.setattr(
        doctor, "_filesystem", lambda *a: ("pass", "No network filesystem was detected.")
    )
    return folder, env


def report(installed, **kwargs):
    folder, env = installed
    return doctor.diagnose(data_dir=folder, env=env, **kwargs)


def check(installed, key, **kwargs):
    return report(installed, **kwargs)["checks"][key]["status"]


def test_healthy_fixed_contract(installed):
    result = report(installed)
    assert result["exit_code"] == 0
    assert result["status"] == "healthy"
    assert tuple(result["checks"]) == doctor.CHECK_KEYS
    assert all(set(v) == {"status", "message"} for v in result["checks"].values())
    assert all(v["status"] in doctor.STATUSES for v in result["checks"].values())
    assert "health-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "name,key,mode",
    [
        ("", "data_dir", 0o755),
        (config.DB_FILE, "database_mode", 0o644),
        (config.TOKEN_FILE, "token_mode", 0o644),
        (config.DB_FILE + "-wal", "wal_mode", 0o644),
        (config.DB_FILE + "-shm", "shm_mode", 0o644),
    ],
)
def test_insecure_modes_are_reported_without_repair(installed, name, key, mode):
    folder, _ = installed
    path = folder / name
    if name and not path.exists():
        path.touch()
    path.chmod(mode)
    assert check(installed, key) == "fail"
    assert path.stat().st_mode & 0o777 == mode


def test_missing_installation_does_not_create_files(tmp_path):
    absent = tmp_path / "absent"
    result = doctor.diagnose(data_dir=absent, env={"CODEX_HOME": str(tmp_path / "absent-client")})
    assert result["exit_code"] == 2
    assert result["checks"]["data_dir"]["status"] == "fail"
    assert list(tmp_path.iterdir()) == []


def test_readonly_never_calls_migration_start_or_provider(installed, monkeypatch):
    folder, _ = installed

    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics attempted a write, start, or provider call")

    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr(db, "migrate", forbidden)
    monkeypatch.setattr(config, "private_dir", forbidden)
    monkeypatch.setattr(launcher, "ensure_running", forbidden)
    from noblivion import embedding

    monkeypatch.setattr(embedding, "make_embedder", forbidden)
    before = {
        str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in folder.rglob("*")
        if p.is_file()
    }
    assert report(installed)["exit_code"] == 0
    after = {
        str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in folder.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_old_schema_and_foreign_key_violation_are_reported(installed):
    folder, _ = installed
    with sqlite3.connect(folder / config.DB_FILE) as conn:
        conn.execute("PRAGMA user_version=0")
        conn.execute("INSERT INTO child VALUES (777)")
    result = report(installed)
    assert result["exit_code"] == 1
    assert result["checks"]["database_schema"]["status"] == "fail"
    assert result["checks"]["database_foreign_keys"]["status"] == "fail"
    assert "777" not in json.dumps(result)


def test_nonempty_wal_is_unavailable_without_creating_shm(installed):
    folder, _ = installed
    wal = folder / (config.DB_FILE + "-wal")
    wal.write_bytes(b"pending WAL")
    wal.chmod(0o600)
    assert check(installed, "database_schema") == "unavailable"
    assert not (folder / (config.DB_FILE + "-shm")).exists()
    assert wal.read_bytes() == b"pending WAL"


@pytest.mark.parametrize("body", ["{", "[]", '{"a": NaN}', '{"embedding":{"backend":false}}'])
def test_invalid_config(installed, body):
    folder, _ = installed
    (folder / "config.json").write_text(body)
    result = report(installed)
    assert result["exit_code"] == 1
    assert any(
        result["checks"][k]["status"] in {"fail", "warn"}
        for k in ("config_syntax", "config_values", "config_unknown_keys")
    )


def test_unknown_and_secret_config_is_not_echoed(installed):
    folder, env = installed
    canary = "private-canary-DO-NOT-PRINT"
    (folder / "config.json").write_text(
        json.dumps({canary: canary, "port": "bad", "namespace": canary})
    )
    env["NOBLIVION_PORT"] = "12345"
    result = report(installed)
    assert result["checks"]["config_unknown_keys"]["status"] == "warn"
    assert result["checks"]["config_values"]["status"] == "fail"
    assert "override" in result["checks"]["environment_precedence"]["message"]
    assert canary not in json.dumps(result)
    assert str(folder) not in json.dumps(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("NOBLIVION_PORT", "65536"),
        ("NOBLIVION_PORT", "-1"),
        ("NOBLIVION_PORT", "nan"),
        ("NOBLIVION_IDLE_EXIT_S", "bad"),
        ("NOBLIVION_LOG_LEVEL", "loud"),
        ("NOBLIVION_EMBED_BACKEND", "unknown"),
    ],
)
def test_invalid_environment_values(installed, key, value):
    installed[1][key] = value
    assert check(installed, "environment_values") == "fail"


@pytest.mark.parametrize(
    "url,status",
    [
        ("http://127.0.0.1:11434", "pass"),
        ("http://[::1]:11434", "pass"),
        ("http://localhost:11434", "warn"),
        ("http://192.0.2.20:11434", "warn"),
        ("file:///private/secret", "fail"),
        ("http://user:canary@127.0.0.1", "fail"),
        ("http://127.0.0.1?secret=canary", "fail"),
        ("http://127.0.0.1:99999", "fail"),
    ],
)
def test_embedding_url_safety_without_network(installed, url, status):
    installed[1].update(NOBLIVION_EMBED_BACKEND="ollama", NOBLIVION_OLLAMA_URL=url)
    result = report(installed)
    assert result["checks"]["embedding_url"]["status"] == status
    assert url not in json.dumps(result)
    assert "canary" not in json.dumps(result)


def test_health_probe_bounded_and_receives_no_extra_requests(installed, monkeypatch):
    seen = []

    def probe(port, token, timeout):
        seen.append((port, token, timeout))
        time.sleep(0.25)
        return None

    monkeypatch.setattr(launcher, "probe", probe)
    started = time.monotonic()
    result = report(installed, timeout=0.05)
    assert time.monotonic() - started < 0.2
    assert seen == [(12345, "a" * 64, 0.05)]
    assert result["checks"]["health"]["status"] == "warn"


def test_bad_token_never_probes(installed, monkeypatch):
    folder, _ = installed
    (folder / config.TOKEN_FILE).write_text("secret-canary")
    monkeypatch.setattr(launcher, "probe", lambda *a, **kw: pytest.fail("must not probe"))
    result = report(installed)
    assert result["checks"]["health"]["status"] == "fail"
    assert "secret-canary" not in json.dumps(result)


def test_network_filesystem_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor, "read_bytes", lambda *a, **kw: b"server:/data / nfs4 rw 0 0\n")
    assert doctor._filesystem(tmp_path, 0.05)[0] == "warn"


def test_config_read_limit_and_fifo(installed):
    folder, _ = installed
    path = folder / "config.json"
    path.write_bytes(b" " * (doctor.MAX_FILE_BYTES + 1))
    assert check(installed, "config_syntax") == "fail"
    path.unlink()
    os.mkfifo(path)
    started = time.monotonic()
    assert check(installed, "config_syntax") == "fail"
    assert time.monotonic() - started < 0.25


def test_current_root_and_missing_memory_folder(installed):
    assert check(installed, "current_root", root="unmapped") == "warn"
    installed[1]["NOBLIVION_MEMORY_DIR"] += "-missing"
    assert check(installed, "memory_folders") == "warn"
    assert check(installed, "current_root") == "warn"


def test_codex_hook_entries_and_redaction(installed):
    _, env = installed
    home = Path(env["CODEX_HOME"])
    events = (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "SubagentStart",
        "Stop",
        "SessionEnd",
    )
    hooks = {
        event: [
            {
                "hooks": [
                    {"command": "TOKEN=canary python /x/codex/adapter.py --config /x/private.json"}
                ]
            }
        ]
        for event in events
    }
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    (home / "config.toml").write_text('[mcp_servers.noblivion]\nsecret="canary"\n')
    result = report(installed)
    assert result["checks"]["codex_hooks"]["status"] == "pass"
    assert result["checks"]["codex_config"]["status"] == "pass"
    assert "canary" not in json.dumps(result)
    assert "/hidden" not in json.dumps(result)
    del hooks["Stop"]
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    assert check(installed, "codex_hooks") == "warn"
    (home / "hooks.json").write_text('{"hooks":[]}')
    assert check(installed, "codex_hooks") == "fail"


@pytest.mark.parametrize("args", [["--timeout", "nan"], ["--timeout", "5"], ["--secret-canary"]])
def test_bad_arguments_do_not_echo_input(args, capsys):
    with pytest.raises(SystemExit) as exc:
        doctor.main(args)
    assert exc.value.code == 2
    assert "canary" not in capsys.readouterr().err


def test_cli_json(installed, monkeypatch, capsys):
    folder, env = installed
    monkeypatch.setattr(doctor.os, "environ", env)
    assert doctor.main(["--json", "--data-dir", str(folder)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["exit_code"] == 0


@pytest.mark.parametrize(
    "body", ['{"memory_dirs": {}}', '{"port": {}}', '{"embedding": {"backend": {}}}']
)
def test_empty_mapping_is_an_invalid_scalar(installed, body):
    (installed[0] / "config.json").write_text(body)
    assert check(installed, "config_values") == "fail"


def test_empty_unknown_mapping_is_not_silently_dropped(installed):
    (installed[0] / "config.json").write_text('{"unknown": {}}')
    assert check(installed, "config_unknown_keys") == "warn"


def test_nonfinite_json_is_invalid_syntax(installed):
    (installed[0] / "config.json").write_text('{"port": NaN}')
    assert check(installed, "config_syntax") == "fail"


def test_blank_environment_does_not_override_non_switch(installed):
    (installed[0] / "config.json").write_text('{"port": 123}')
    installed[1]["NOBLIVION_PORT"] = " "
    assert (
        "No environment override"
        in report(installed)["checks"]["environment_precedence"]["message"]
    )


def test_corrupt_database_is_unavailable_and_preserved(installed):
    database = installed[0] / config.DB_FILE
    database.write_bytes(b"not a sqlite database: private-text-canary")
    result = report(installed)
    assert result["checks"]["database_schema"]["status"] == "unavailable"
    assert "private-text-canary" not in json.dumps(result)
    assert database.read_bytes().startswith(b"not a sqlite database")


def test_reading_fifo_or_symlink_does_not_follow_target(tmp_path):
    target = tmp_path / "private"
    target.write_text("private-canary")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        doctor.read_bytes(link)


def test_rollback_journal_is_unavailable_and_unchanged(installed):
    journal = installed[0] / (config.DB_FILE + "-journal")
    journal.write_bytes(b"active-journal-canary")
    before = journal.read_bytes()
    assert check(installed, "database_schema") == "unavailable"
    assert journal.read_bytes() == before


@pytest.mark.skipif(not doctor.sys.platform.startswith("linux"), reason="Linux proc mount table")
def test_linux_mount_table_does_not_follow_final_symlink(tmp_path):
    result = doctor._filesystem(tmp_path, 0.05)
    assert result[0] in {"pass", "warn"}


def test_current_root_uses_longest_codex_workspace_mapping(installed, monkeypatch, tmp_path):
    _, env = installed
    del env["NOBLIVION_MEMORY_DIR"]
    workspace = tmp_path / "workspace"
    inner = workspace / "nested"
    inner.mkdir(parents=True)
    broad = tmp_path / "broad" / "memory"
    specific = tmp_path / "specific" / "memory"
    broad.mkdir(parents=True)
    specific.mkdir(parents=True)
    own = Path(env["CODEX_HOME"]) / "noblivion"
    own.mkdir()
    config_path = own / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "project_mappings": [
                    {
                        "workspace": str(workspace),
                        "root": "broad",
                        "namespace": "fixture",
                        "memory_dir": str(broad),
                    },
                    {
                        "workspace": str(inner),
                        "root": "specific",
                        "namespace": "fixture",
                        "memory_dir": str(specific),
                    },
                ]
            }
        )
    )
    monkeypatch.chdir(inner)
    assert check(installed, "current_root") == "pass"
    assert doctor._codex_scope(env)[0] == "specific"
    assert str(inner) not in json.dumps(report(installed))
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    monkeypatch.chdir(sibling)
    assert check(installed, "current_root") == "not_configured"


@pytest.mark.parametrize(
    "body",
    [
        "{",
        '{"project_mappings": {}}',
        json.dumps({"project_mappings": [{}] * (doctor.MAX_ITEMS + 1)}),
    ],
)
def test_invalid_or_oversized_codex_mapping_is_reported(installed, body):
    _, env = installed
    del env["NOBLIVION_MEMORY_DIR"]
    own = Path(env["CODEX_HOME"]) / "noblivion"
    own.mkdir()
    (own / "config.json").write_text(body)
    assert check(installed, "current_root") == "fail"


def test_sqlite_progress_budget_interrupts_large_foreign_key_scan(installed, monkeypatch):
    database = installed[0] / config.DB_FILE
    with sqlite3.connect(database) as conn:
        conn.executemany("INSERT INTO parent VALUES (?)", ((n,) for n in range(1000)))
        conn.executemany("INSERT INTO child VALUES (?)", ((n,) for n in range(1000)))
    clock = iter(range(1000))
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock))
    schema, foreign = doctor._database(database, 0.05)
    assert schema[0] == "unavailable"
    assert foreign[0] == "unavailable"
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_memory_folder_limit_is_bounded(installed):
    installed[1]["NOBLIVION_MEMORY_DIRS"] = ":".join(["/missing"] * (doctor.MAX_ITEMS + 2))
    assert check(installed, "memory_folders") == "warn"


def test_schema_manifest_matches_packaged_migrations():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.migrate(conn, random_base=1000000)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            table: frozenset(row[1] for row in conn.execute("PRAGMA table_info(" + table + ")"))
            for table in tables
        }
        indexes = dict(
            conn.execute(
                "SELECT name,tbl_name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
            )
        )
        assert doctor._SCHEMA_MANIFEST_VERSION == db.latest_version()
        assert doctor._REQUIRED_COLUMNS == columns
        assert doctor._REQUIRED_INDEXES == indexes
    finally:
        conn.close()


def test_latest_version_without_schema_is_not_healthy(installed):
    database = installed[0] / config.DB_FILE
    database.unlink()
    with sqlite3.connect(database) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version()}")
    database.chmod(0o600)
    result = report(installed)
    assert result["checks"]["database_schema"]["status"] == "fail"
    assert result["status"] != "healthy"


@pytest.mark.parametrize(
    "change",
    [
        "DROP TABLE miner_state",
        "DROP TABLE meta; CREATE TABLE meta(key TEXT)",
        "DROP INDEX memories_rev",
        "DROP TABLE miner_state; CREATE VIEW miner_state AS SELECT 1 AS transcript",
    ],
)
def test_missing_required_schema_objects_or_columns(installed, change):
    database = installed[0] / config.DB_FILE
    with sqlite3.connect(database) as conn:
        conn.executescript(change)
    result = report(installed)
    assert result["checks"]["database_schema"]["status"] == "fail"
    assert result["status"] != "healthy"


@pytest.mark.parametrize(
    "key,value,valid",
    [
        ("dedup.min_cosine", -0.01, False),
        ("dedup.min_cosine", 0, True),
        ("dedup.min_cosine", 1, True),
        ("dedup.min_cosine", 1.01, False),
        ("dedup.timeout_s", 0, False),
        ("dedup.timeout_s", 0.99, False),
        ("dedup.timeout_s", 1, True),
        ("dedup.timeout_s", 1.01, True),
        ("miner.max_run_s", 0, False),
        ("miner.max_run_s", -0.01, False),
        ("miner.max_run_s", 0.01, True),
        ("recall.min_score", -1, True),
        ("recall.min_score", -1.01, False),
        ("trust.prior_mined", 0, False),
        ("trust.prior_mined", 1, True),
        ("recall.index_k", 0, False),
        ("recall.index_k", 100, True),
        ("recall.index_k", 101, False),
        ("port", 65535, True),
        ("port", 65536, False),
    ],
)
def test_raw_numeric_range_boundaries(installed, key, value, valid):
    (installed[0] / "config.json").write_text(json.dumps({key: value}))
    assert check(installed, "config_values") == ("pass" if valid else "fail")


def test_numeric_diagnostics_match_dedup_and_miner_fallbacks(installed, monkeypatch):
    from noblivion import dedup, miner

    cfg = {"dedup.min_cosine": -0.5, "dedup.timeout_s": 0.5, "miner.max_run_s": 0}
    (installed[0] / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config, "load_file", lambda env=None: cfg)
    settings = dedup.load_dedup_settings({})
    assert settings.min_cosine == dedup.DEFAULT_MIN_COSINE
    assert settings.timeout_s == dedup.DEFAULT_TIMEOUT_S
    assert miner.load_miner_settings({}).max_run_s == miner.DEFAULT_MAX_RUN_S
    assert check(installed, "config_values") == "fail"


@pytest.mark.parametrize(
    "key,value",
    [
        ("port", "123.0"),
        ("idle_exit_s", "1e2"),
        ("dedup.max_pairs", "50.0"),
        ("recall.index_k", "30.0"),
    ],
)
def test_integer_text_rejected_by_runtime_is_not_healthy(installed, key, value):
    (installed[0] / "config.json").write_text(json.dumps({key: value}))
    assert check(installed, "config_values") == "fail"


class _MountOutput(io.BytesIO):
    def __init__(self, body, *, blocked=False):
        super().__init__(body)
        self.read_limits = []
        self.blocked = blocked
        self.release = threading.Event()

    def read(self, size=-1):
        self.read_limits.append(size)
        if self.blocked:
            assert self.release.wait(1), "diagnostic did not stop its blocked child"
        return super().read(size)


class _MountProcess:
    def __init__(self, body, *, returncode=0, blocked=False, wait_timeout=False):
        self.stdout = _MountOutput(body, blocked=blocked)
        self.returncode = None
        self.final_returncode = returncode
        self.wait_timeout = wait_timeout
        self.wait_limits = []
        self.killed = False

    def kill(self):
        self.killed = True
        self.stdout.release.set()

    def wait(self, timeout=None):
        self.wait_limits.append(timeout)
        if self.wait_timeout and not self.killed:
            raise subprocess.TimeoutExpired("fixture-mount", timeout)
        self.returncode = -9 if self.killed else self.final_returncode
        return self.returncode


def _mock_darwin_mount(monkeypatch, process):
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor.subprocess, "Popen", popen)
    return calls


@pytest.mark.parametrize("kind,status", [("nfs", "warn"), ("smbfs", "warn"), ("apfs", "pass")])
def test_darwin_filesystem_type_uses_bounded_mount_output(tmp_path, monkeypatch, kind, status):
    process = _MountProcess(f"fixture-device on / ({kind}, local)\n".encode())
    calls = _mock_darwin_mount(monkeypatch, process)
    assert doctor._filesystem(tmp_path, 0.05)[0] == status
    assert calls == [
        (
            ["/sbin/mount"],
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]
    assert process.stdout.read_limits == [doctor.MAX_FILE_BYTES + 1]
    assert process.wait_limits == [0.05]
    assert process.stdout.closed
    assert not process.killed


@pytest.mark.parametrize(
    "specific_kind,status", [("nfs", "warn"), ("smbfs", "warn"), ("apfs", "pass")]
)
def test_darwin_filesystem_selects_longest_matching_mount(
    tmp_path, monkeypatch, specific_kind, status
):
    broad = tmp_path.resolve()
    specific = broad / "nested"
    target = specific / "memory"
    body = (
        f"fixture-local on / (apfs, local)\n"
        f"fixture-network on {broad} (nfs)\n"
        f"fixture-nested on {specific} ({specific_kind})\n"
    )
    process = _MountProcess(body.encode())
    _mock_darwin_mount(monkeypatch, process)
    assert doctor._filesystem(target, 0.05)[0] == status
    assert process.stdout.closed


def test_darwin_filesystem_ignores_mount_with_only_shared_name_prefix(tmp_path, monkeypatch):
    target = tmp_path.resolve() / "project"
    body = f"fixture-local on / (apfs)\nfixture-network on {target}-other (smbfs)\n"
    process = _MountProcess(body.encode())
    _mock_darwin_mount(monkeypatch, process)
    assert doctor._filesystem(target, 0.05)[0] == "pass"


@pytest.mark.parametrize(
    "body,returncode",
    [
        (b"fixture-private-canary", 0),
        (b"", 0),
        (b"fixture-device on / (nfs)\n", 1),
    ],
)
def test_darwin_invalid_or_refused_mount_listing_is_unavailable(
    tmp_path, monkeypatch, body, returncode
):
    process = _MountProcess(body, returncode=returncode)
    _mock_darwin_mount(monkeypatch, process)
    result = doctor._filesystem(tmp_path, 0.05)
    assert result[0] == "unavailable"
    assert "canary" not in str(result)
    assert process.stdout.closed


def test_darwin_mount_command_refusal_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    def refused(*args, **kwargs):
        raise PermissionError("fixture-private-canary")

    monkeypatch.setattr(doctor.subprocess, "Popen", refused)
    result = doctor._filesystem(tmp_path, 0.05)
    assert result[0] == "unavailable"
    assert "canary" not in str(result)


def test_darwin_oversized_mount_output_is_bounded_and_child_stopped(tmp_path, monkeypatch):
    process = _MountProcess(b"x" * (doctor.MAX_FILE_BYTES + 100))
    _mock_darwin_mount(monkeypatch, process)
    assert doctor._filesystem(tmp_path, 0.05)[0] == "unavailable"
    assert process.stdout.read_limits == [doctor.MAX_FILE_BYTES + 1]
    assert process.killed
    assert process.wait_limits == [0.05]
    assert process.stdout.closed


def test_darwin_blocked_mount_output_is_bounded_and_child_stopped(tmp_path, monkeypatch):
    process = _MountProcess(b"", blocked=True)
    _mock_darwin_mount(monkeypatch, process)
    started = time.monotonic()
    assert doctor._filesystem(tmp_path, 0.05)[0] == "unavailable"
    assert time.monotonic() - started < 0.5
    assert process.killed
    assert process.stdout.read_limits == [doctor.MAX_FILE_BYTES + 1]
    assert process.stdout.closed


def test_darwin_child_wait_timeout_stops_child_and_closes_pipe(tmp_path, monkeypatch):
    process = _MountProcess(b"fixture-device on / (apfs)\n", wait_timeout=True)
    _mock_darwin_mount(monkeypatch, process)
    assert doctor._filesystem(tmp_path, 0.05)[0] == "unavailable"
    assert process.killed
    assert process.wait_limits == [0.05, 0.05]
    assert process.stdout.closed
