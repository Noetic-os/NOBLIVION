# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-29: from ``claude plugin install`` to working recall in at most
two sessions.

The SessionStart line names the slash command ``/noblivion:setup``. The
command (``skills/setup/SKILL.md``) runs ``install.sh`` with the plugin data
dir, and ``install.sh`` ends with ``noblivion ensure-running``. So the store
runs in the session that ran the command; the next prompt has recall.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from noblivion import launcher
from test_store import wait_for

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "setup" / "SKILL.md"
INSTALL = ROOT / "scripts" / "install.sh"
BASH = shutil.which("bash")
COMMAND = 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/install.sh" --data-dir "${CLAUDE_PLUGIN_DATA}"'


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    assert match, "SKILL.md must start with YAML front matter"
    fields = {}
    for line in match.group(1).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, match.group(2)


def _store_client():
    spec = importlib.util.spec_from_file_location(
        "_n29_store_client", ROOT / "hooks" / "store_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the slash command ---------------------------------------------------------------


def test_the_setup_command_file_exists_and_runs_install_sh():
    assert SKILL.is_file(), "the plugin ships skills/setup/SKILL.md (/noblivion:setup)"
    fields, body = _front_matter(SKILL.read_text(encoding="utf-8"))
    assert fields["name"] == "setup"
    assert fields["description"]
    # Only the user starts the install: it builds a venv and downloads a model.
    assert fields["disable-model-invocation"] == "true"
    # The body runs install.sh from the plugin with the plugin data dir.
    assert COMMAND in body
    script = COMMAND.split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
    assert Path(script) == INSTALL and INSTALL.is_file()
    # The allowed tool covers exactly that command, so it needs no extra prompt.
    assert fields["allowed-tools"] == f"Bash({COMMAND}:*)"


def test_the_session_start_line_names_the_setup_command(tmp_path):
    sc = _store_client()
    env = {"NOBLIVION_DATA_DIR": str(tmp_path), "CLAUDE_PLUGIN_DATA": "/pd"}
    missing = sc.install_notice("no_launcher", env, root=ROOT)
    assert "not installed" in missing and "/noblivion:setup" in missing
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "noblivion-install.json").write_text(json.dumps({"version": "0.0.1"}))
    old = sc.install_notice("spawned", env, root=ROOT)
    assert "built for plugin version 0.0.1" in old and "/noblivion:setup" in old


def test_install_refuses_an_empty_data_dir(tmp_path):
    """An old Claude Code that does not fill in ${CLAUDE_PLUGIN_DATA} passes an
    empty value; install.sh must not fall back to another folder."""
    proc = subprocess.run(
        [BASH or "bash", str(INSTALL), "--data-dir", "", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert proc.returncode == 1
    assert "--data-dir needs a folder" in proc.stderr


# -- install.sh starts the store in the same session ---------------------------------------


def _fake_uv(folder: Path, stdin_fault: bool = False) -> None:
    """A ``uv`` that makes a venv whose ``python`` and ``noblivion`` run this
    test's interpreter, where the package is importable. No download. With
    ``stdin_fault`` the venv ``python`` fails on a script from stdin."""
    folder.mkdir(parents=True)
    py = sys.executable
    fault = '[ \\"$1\\" = - ] && exit 9\\n' if stdin_fault else ""
    (folder / "uv").write_text(
        f"""#!{py}
import os, sys
args = sys.argv[1:]
if args[:1] == ["venv"]:
    bin_dir = os.path.join(args[-1], "bin")
    os.makedirs(bin_dir, exist_ok=True)
    # Scripts, not a symlink: a python run through a symlink with no
    # pyvenv.cfg next to it does not see the test venv, so not the package.
    for name, tail in (("python", ""), ("noblivion", " -m noblivion")):
        exe = os.path.join(bin_dir, name)
        fault = "{fault}" if name == "python" else ""
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\\n" + fault + "exec {py}" + tail + " \\"$@\\"\\n")
        os.chmod(exe, 0o755)
elif args[:1] == ["export"]:
    open(args[args.index("--output-file") + 1], "w").close()
""",
        encoding="utf-8",
    )
    (folder / "uv").chmod(0o755)


def _install_env(tmp_path: Path, stdin_fault: bool = False) -> dict[str, str]:
    """A clean env with a temp HOME and the fake ``uv`` first on PATH."""
    home = tmp_path / "home"
    home.mkdir()
    _fake_uv(tmp_path / "bin", stdin_fault)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOBLIVION_", "CLAUDE_"))}
    env.update(
        HOME=str(home),
        PATH=f"{tmp_path / 'bin'}{os.pathsep}{env.get('PATH', '')}",
        NOBLIVION_PORT="0",
        NOBLIVION_IDLE_EXIT_S="120",
    )
    return env


def _install(env: dict[str, str], data: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(INSTALL), "--data-dir", str(data), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _full_health(data: Path) -> dict:
    """The health answer with the token: it names the embedding backend."""
    info = launcher.read_store_json(data)
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/health",
        headers={"Authorization": f"Bearer {launcher.read_token(data)}"},
    )
    with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 - loopback
        return json.loads(answer.read().decode("utf-8"))


def _stop_store(data: Path) -> None:
    info = launcher.read_store_json(data)
    if info is not None:
        os.kill(info["pid"], signal.SIGTERM)
        wait_for(lambda: launcher.read_store_json(data) is None, 30)


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_starts_the_store_so_the_next_prompt_has_recall(tmp_path):
    data = tmp_path / "plugin-data"
    env = _install_env(tmp_path)
    pid = None
    try:
        proc = _install(env, data, "--no-embed")
        info = launcher.read_store_json(data)
        pid = info["pid"] if info else None
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "The store runs" in proc.stdout, proc.stdout + proc.stderr
        # The restart can wait up to 100 s: a line says so before it.
        starting = "noblivion install: starting the store; when an old store runs"
        assert starting in proc.stdout
        assert proc.stdout.index(starting) < proc.stdout.index("The store runs")
        # No new session and no SessionStart hook: the next prompt's hook
        # finds and proves the store through store.json and the token.
        assert info is not None
        sc = _store_client()
        hook_env = {"CLAUDE_PLUGIN_DATA": str(data)}

        def get_json(url: str, timeout_s: float):
            with urllib.request.urlopen(url, timeout=timeout_s) as answer:  # noqa: S310 - loopback
                return json.loads(answer.read().decode("utf-8"))

        base, token = sc.connect(hook_env, get_json, 2.0)
        assert base.startswith("http://127.0.0.1:") and token
        assert (data / "noblivion.db").is_file()
    finally:
        if pid:
            os.kill(pid, signal.SIGTERM)
            wait_for(lambda: launcher.read_store_json(data) is None, 30)


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_no_embed_sets_the_backend_to_none_so_the_store_is_not_degraded(tmp_path):
    """--no-embed installs no fastembed. With the default backend the store
    then fails to load it, reports "degraded" for good and logs a warning
    every hour (NOBLIVION-57)."""
    data = tmp_path / "plugin-data"
    env = _install_env(tmp_path)
    try:
        proc = _install(env, data, "--no-embed")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        doc = json.loads((data / "config.json").read_text(encoding="utf-8"))
        assert doc["embedding"]["backend"] == "none"
        assert doc["recall"]["index_k"] == 30  # the rest of the shipped config stays
        health = _full_health(data)
        assert health["status"] == "ok"
        assert health["embedding"]["backend"] == "none"
        assert health["embedding"]["state"] == "off"
    finally:
        _stop_store(data)


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_no_embed_says_which_backend_it_replaced(tmp_path):
    # NOBLIVION-57 review: the script said "config.json exists, kept as it
    # is", then --no-embed changed a backend that the user chose.
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    doc = {"embedding": {"backend": "ollama", "model": "example-model"}, "recall": {"index_k": 7}}
    (data / "config.json").write_text(json.dumps(doc), encoding="utf-8")
    proc = _install(_install_env(tmp_path), data, "--no-embed", "--no-start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "kept as it is\n" not in proc.stdout
    assert "config.json exists, kept as it is except embedding.backend" in proc.stdout
    line = f"--no-embed: set embedding.backend to none in {data / 'config.json'}; it was ollama"
    assert line in proc.stdout, proc.stdout
    doc["embedding"]["backend"] = "none"  # only this key changes
    assert json.loads((data / "config.json").read_text(encoding="utf-8")) == doc


@pytest.mark.skipif(BASH is None, reason="bash not found")
@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('{"embedding": ', "is not valid JSON"),
        ("[1, 2]", "is not a JSON object"),
        ('{"embedding": "fastembed"}', 'has an "embedding" that is not an object'),
    ],
    ids=["not JSON", "not an object", "embedding not an object"],
)
def test_install_no_embed_goes_on_with_a_broken_config(tmp_path, text, problem):
    # Before, a Python traceback, and set -e stopped the install after the venv.
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text(text, encoding="utf-8")
    proc = _install(_install_env(tmp_path), data, "--no-embed", "--no-start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    line = (
        f"noblivion install: {data / 'config.json'} {problem}; embedding.backend not "
        "changed. Fix the file and set embedding.backend to none (--no-embed).\n"
    )
    assert line in proc.stdout, proc.stdout
    assert (data / "config.json").read_text(encoding="utf-8") == text
    assert "done. The store starts at the next Claude Code session." in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_no_embed_goes_on_when_the_config_cannot_be_written(tmp_path):
    # A folder in the place of the temp file: the write fails. Before, a
    # Python traceback of 9 lines.
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    text = json.dumps({"embedding": {"backend": "fastembed"}})
    (data / "config.json").write_text(text, encoding="utf-8")
    (data / "config.tmp").mkdir()
    proc = _install(_install_env(tmp_path), data, "--no-embed", "--no-start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    line = (
        f"noblivion install: {data / 'config.json'} cannot be written (Is a directory: "
        f"{data / 'config.tmp'}); embedding.backend not changed. Fix the file and set "
        "embedding.backend to none (--no-embed).\n"
    )
    assert line in proc.stdout, proc.stdout
    assert (data / "config.json").read_text(encoding="utf-8") == text
    assert (data / "config.tmp").is_dir()
    assert "done. The store starts at the next Claude Code session." in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not found")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of mode 000")
def test_install_no_embed_goes_on_when_the_config_cannot_be_read(tmp_path):
    # Before, the line said "is not valid JSON".
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text("{}", encoding="utf-8")
    (data / "config.json").chmod(0)
    try:
        proc = _install(_install_env(tmp_path), data, "--no-embed", "--no-start")
    finally:
        (data / "config.json").chmod(0o600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    line = (
        f"noblivion install: {data / 'config.json'} cannot be read (Permission denied); "
        "embedding.backend not changed."
    )
    assert line in proc.stdout, proc.stdout
    assert "not valid JSON" not in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_no_embed_goes_on_after_an_error_that_the_script_does_not_expect(tmp_path):
    # The Python step that changes config.json fails (here: the venv python
    # exits 9). set -e must not stop the install there: one line says so.
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    text = json.dumps({"embedding": {"backend": "fastembed"}})
    (data / "config.json").write_text(text, encoding="utf-8")
    proc = _install(_install_env(tmp_path, stdin_fault=True), data, "--no-embed", "--no-start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"could not change embedding.backend in {data / 'config.json'}\n" in proc.stdout
    assert (data / "config.json").read_text(encoding="utf-8") == text
    assert "done. The store starts at the next Claude Code session." in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_with_backend_none_does_not_say_the_model_is_ready(tmp_path):
    data = tmp_path / "plugin-data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text(json.dumps({"embedding": {"backend": "none"}}))
    proc = _install(_install_env(tmp_path), data, "--no-start")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "embedding model ready" not in proc.stdout
    assert "embedding.backend is none: no model to download" in proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash not found")
def test_install_again_restarts_the_store_so_it_reads_the_new_config(tmp_path):
    """docs/install.md: to turn the model on later, set embedding.backend and
    run /noblivion:setup again. The store reads its config once, at start, so
    install.sh must replace a running store of the same version too
    (NOBLIVION-57)."""
    data = tmp_path / "plugin-data"
    env = _install_env(tmp_path)
    data.mkdir(mode=0o700)
    # The state after a first install without network: keyword search only.
    (data / "config.json").write_text(json.dumps({"embedding": {"backend": "none"}}))
    try:
        proc = _install(env, data, "--no-model")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        old_pid = launcher.read_store_json(data)["pid"]
        assert _full_health(data)["embedding"]["backend"] == "none"
        (data / "config.json").write_text(json.dumps({"embedding": {"backend": "fastembed"}}))
        proc = _install(env, data, "--no-model")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "The store runs" in proc.stdout, proc.stdout + proc.stderr
        info = launcher.read_store_json(data)
        assert info is not None and info["pid"] != old_pid
        assert _full_health(data)["embedding"]["backend"] == "fastembed"
    finally:
        _stop_store(data)
