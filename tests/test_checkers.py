# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the repository gate scripts in tools/

The tests use fictional pattern lists, so this file holds no forbidden name.
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
changelog = _load("check_changelog")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "forbidden_names.example.txt").write_text("# example\nzebra-?host\n")
    (tmp_path / "ok.py").write_text("# SPDX-License-Identifier: AGPL-3.0-or-later\nx = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    for var in (names.ENV_PATTERNS, names.ENV_FILE, "GITHUB_ACTIONS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(names.ENV_PATTERNS, "# private list\nzebra-?host\n")
    return tmp_path


def test_forbidden_clean_repo_passes(repo: Path) -> None:
    assert names.main([]) == 0


def test_forbidden_list_files_are_not_flagged(repo: Path) -> None:
    (repo / "tools" / "forbidden_names.local.txt").write_text("zebrahost\n")
    assert names.main(["tools/forbidden_names.example.txt"]) == 0
    assert names.main(["tools/forbidden_names.local.txt"]) == 0


def test_forbidden_hit_in_file_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bad.md").write_text("line one\nssh Zebra-Host now\n")
    assert names.main(["bad.md"]) == 1
    out = capsys.readouterr().out
    assert "bad.md:2: forbidden name pattern #1" in out
    # A private list never shows the matched text in a (public) CI log.
    assert "Zebra-Host" not in out


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


def test_forbidden_env_file_is_used(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = tmp_path_factory.mktemp("list") / "names.txt"
    listing.write_text("gnu-?farm\n")
    monkeypatch.delenv(names.ENV_PATTERNS)
    monkeypatch.setenv(names.ENV_FILE, str(listing))
    (repo / "a.md").write_text("zebrahost\n")
    (repo / "b.md").write_text("the gnufarm\n")
    assert names.main(["a.md"]) == 0
    assert names.main(["b.md"]) == 1


def test_forbidden_env_patterns_beat_the_env_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(names.ENV_FILE, str(repo / "does-not-exist.txt"))
    (repo / "a.md").write_text("zebrahost\n")
    assert names.main(["a.md"]) == 1


def test_forbidden_local_file_is_used(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(names.ENV_PATTERNS)
    (repo / "tools" / "forbidden_names.local.txt").write_text("gnu-?farm\n")
    (repo / "b.md").write_text("gnufarm\n")
    assert names.main(["b.md"]) == 1


@pytest.mark.parametrize("value", ["", "  \n"])
def test_forbidden_empty_secret_falls_back_to_the_example_list(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    # A pull request from a fork gets an empty secret.
    monkeypatch.setenv(names.ENV_PATTERNS, value)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    (repo / "bad.md").write_text("zebra-host\n")
    assert names.main(["bad.md"]) == 1
    captured = capsys.readouterr()
    assert "NOTICE:" in captured.err and "NOT checked" in captured.err
    assert "::notice title=Forbidden names::" in captured.out
    # The example list is public, so the finding shows the matched text.
    assert "bad.md:1: forbidden name 'zebra-host'" in captured.out


def test_forbidden_fallback_without_github_prints_no_annotation(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(names.ENV_PATTERNS)
    assert names.main([]) == 0
    captured = capsys.readouterr()
    assert "NOTICE:" in captured.err
    assert "::notice" not in captured.out


def test_forbidden_bad_private_regex_does_not_show_the_pattern(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(names.ENV_PATTERNS, "ok\nsecret-name(\n")
    with pytest.raises(SystemExit) as exc:
        names.main([])
    assert "secret-name" not in str(exc.value)
    assert ":2: bad regex:" in str(exc.value)


def test_forbidden_shipped_example_list_parses_and_is_generic() -> None:
    path = TOOLS / "forbidden_names.example.txt"
    patterns = names.load_patterns(path, private=False)
    assert patterns
    assert not (TOOLS / "forbidden_names.txt").exists()


def _commit(repo: Path, files: dict[str, str], message: str = "change") -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_changelog_product_change_without_entry_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"hooks/a.py": "x = 1\n"})
    assert changelog.main(["--base", "HEAD~1"]) == 1
    assert "hooks/a.py changed, but CHANGELOG.md did not" in capsys.readouterr().out


def test_changelog_product_change_with_entry_passes(repo: Path) -> None:
    _commit(repo, {"src/p/a.py": "x = 1\n", "CHANGELOG.md": "## Unreleased\n- a\n"})
    assert changelog.main(["--base", "HEAD~1"]) == 0


def test_changelog_waiver_line_passes(repo: Path) -> None:
    _commit(repo, {"mcp/a.py": "x = 1\n"}, "refactor\n\nChangelog: none\n")
    assert changelog.main(["--base", "HEAD~1"]) == 0


def test_changelog_change_outside_product_dirs_passes(repo: Path) -> None:
    _commit(repo, {"tests/test_a.py": "x = 1\n", "docs/a.md": "text\n"})
    assert changelog.main(["--base", "HEAD~1"]) == 0


def test_changelog_owner_placeholder_fails_outside_changelog(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"CHANGELOG.md": "the `<owner>/x` short form\n"})
    assert changelog.main([]) == 0
    _commit(repo, {"docs/install.md": "one\ngit clone <owner>/x\n"})
    assert changelog.main([]) == 1
    assert "docs/install.md:2" in capsys.readouterr().out
