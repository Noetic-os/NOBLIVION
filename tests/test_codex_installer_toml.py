# SPDX-License-Identifier: AGPL-3.0-or-later
"""Managed MCP upgrades preserve user TOML sections and opaque hook state."""

from pathlib import Path

import pytest

from test_codex_adapter import _installer


def replacement(installer):
    return installer._mcp_block(
        Path("/new/release"), Path("/data"), Path("/memory"), "test", "demo"
    )


def without_owned_mcp(text):
    tomllib = pytest.importorskip("tomllib")
    parsed = tomllib.loads(text)
    parsed.get("mcp_servers", {}).pop("noblivion", None)
    return parsed


@pytest.mark.parametrize(
    "header", ["mcp_servers.noblivion", 'mcp_servers."noblivion"', "'mcp_servers'.'noblivion'"]
)
def test_managed_block_preserves_inserted_hook_state_and_unrelated_sections(header):
    installer = _installer()
    preserved = (
        "# Keep this opaque state exactly as written.\n"
        "[hooks.state]\n"
        '"fixture-rule-a" = { approved = true, value = "fixture-state-a" }\n'
        '"fixture-rule-b" = { approved = false, value = "fixture-state-b" }\n'
        "\n[unrelated.settings]\n"
        'label = "unchanged" # retained comment and spacing  \n'
        "[mcp_servers.other]\n"
        'command = "another-server"\n'
    )
    old = (
        'model = "fixture-model"\n'
        + installer.BEGIN
        + "\n"
        + f"[{header}]\ncommand = 'old-command'\n"
        + f"[{header}.env]\nOLD_VALUE = 'old-env'\n"
        + preserved
        + installer.END
        + "\n[after]\nenabled = true\n"
    )
    new = installer._merge_toml(old, replacement(installer))
    assert without_owned_mcp(new) == without_owned_mcp(old)
    assert preserved in new
    assert new.index(preserved) < new.index(installer.BEGIN)
    assert "old-command" not in new and "old-env" not in new
    assert new.count(installer.BEGIN) == new.count(installer.END) == 1
    assert installer._merge_toml(new, replacement(installer)) == new


def test_table_looking_multiline_content_and_markers_remain_literal():
    installer = _installer()
    preserved = (
        "[unrelated]\n"
        "text = '''\n[mcp_servers.noblivion]\n" + installer.BEGIN + "\n" + installer.END + "\n'''\n"
        'nested = [\n["a", "b"],\n["c"],\n]\n'
        'basic = """\n[mcp_servers.noblivion.env]\nnot a table\n"""\n'
        "[[unrelated.rows]]\nlabel = 'first'\n"
        "[[unrelated.rows]]\nlabel = 'second'\n"
    )
    old = (
        installer.BEGIN
        + "\n[mcp_servers.noblivion]\ncommand='old'\n"
        + preserved
        + installer.END
        + "\n"
    )
    new = installer._merge_toml(old, replacement(installer))
    assert preserved in new
    assert without_owned_mcp(new) == without_owned_mcp(old)
    assert installer._merge_toml(new, replacement(installer)) == new


def test_repeated_or_unbalanced_markers_fail_without_mutation():
    installer = _installer()
    for old in (
        installer.BEGIN,
        installer.END,
        installer.END + "\n" + installer.BEGIN,
        installer.BEGIN + "\n" + installer.BEGIN + "\n" + installer.END,
    ):
        with pytest.raises(ValueError, match="incomplete or repeated"):
            installer._merge_toml(old, replacement(installer))


def test_unmanaged_owned_subtable_is_not_overwritten():
    installer = _installer()
    with pytest.raises(ValueError, match="unmanaged"):
        installer._merge_toml("[mcp_servers.noblivion.env]\nvalue='keep'\n", replacement(installer))


@pytest.mark.parametrize("quote", [chr(34), chr(39)])
@pytest.mark.parametrize("closing_width", [4, 5])
def test_owned_multiline_closing_quotes_do_not_swallow_next_section(quote, closing_width):
    installer = _installer()
    preserved = "[hooks.state]\nfixture = { approved = true, value = 'opaque' }\n"
    old = (
        installer.BEGIN
        + "\n[mcp_servers.noblivion]\ntext = "
        + quote * 3
        + "old value"
        + quote * closing_width
        + "\n"
        + preserved
        + installer.END
        + "\n"
    )
    new = installer._merge_toml(old, replacement(installer))
    assert preserved in new
    assert without_owned_mcp(new) == without_owned_mcp(old)
