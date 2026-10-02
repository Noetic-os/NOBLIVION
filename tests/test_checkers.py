# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the two repository gate scripts in tools/.

The tests use a fictional pattern list, so this file holds no forbidden name.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


names = _load("check_forbidden_names")
spdx = _load("check_spdx")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "forbidden_names.txt").write_text("# list\nzebra-?host\n")
    (tmp_path / "ok.py").write_text("# SPDX-License-Identifier: AGPL-3.0-or-later\nx = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_forbidden_clean_repo_passes(repo: Path) -> None:
    assert names.main([]) == 0


def test_forbidden_list_file_is_not_flagged(repo: Path) -> None:
    assert names.main(["tools/forbidden_names.txt"]) == 0


def test_forbidden_hit_in_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bad.md").write_text("line one\nssh Zebra-Host now\n")
    assert names.main(["bad.md"]) == 1
    assert "bad.md:2:" in capsys.readouterr().out


def test_forbidden_hit_in_history_fails(repo: Path) -> None:
    (repo / "bad.txt").write_text("zebrahost\n")
    subprocess.run(["git", "add", "bad.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)
    subprocess.run(["git", "rm", "-q", "bad.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove"], cwd=repo, check=True)
    assert names.main([]) == 0
    assert names.main(["--history"]) == 1


def test_forbidden_history_ignores_list_file(repo: Path) -> None:
    assert names.main(["--history"]) == 0


def test_spdx_header_present_passes(repo: Path) -> None:
    assert spdx.main([]) == 0


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("a.py", "x = 1\n", 1),
        ("a.md", "# SPDX-License-Identifier: AGPL-3.0-or-later\n", 1),
        ("a.md", "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->\n", 0),
        ("a.yml", "\n\n\n\n\n# SPDX-License-Identifier: AGPL-3.0-or-later\n", 1),
        ("a.toml", "# SPDX-License-Identifier: MIT\n", 1),
        ("a.txt", "no header needed\n", 0),
        ("LICENSE", "no header needed\n", 0),
    ],
)
def test_spdx_rules(repo: Path, name: str, text: str, expected: int) -> None:
    (repo / name).write_text(text)
    assert spdx.main([name]) == expected
