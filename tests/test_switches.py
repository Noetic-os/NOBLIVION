# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every on/off switch in docs/configuration.md parses the same way.

``1``, ``true``, ``yes``, ``on`` are on; ``0``, ``false``, ``no``, ``off`` and
the empty value are off; not set or any other value keeps the default. One
helper holds the rule for the hooks (``hooks/hook_config.py``) and one for the
store (``noblivion.config``). Each switch below is read through the function
its hook or command uses, so a reader that bypasses the helper fails here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from hookload import load_hook
from noblivion import config, miner

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "configuration.md"
ROW = re.compile(r"^\| `(NOBLIVION_[A-Z0-9_]+)` \|[^|]*\| (on|off) \(switch\) \|", re.M)

rh = load_hook("recall_hook", "switchtest_recall_hook")
lr = load_hook("label_rows", "switchtest_label_rows")
sc = load_hook("store_client", "switchtest_store_client")
sr = load_hook("subagent_rules_hook", "switchtest_subagent_rules_hook")
ch = load_hook("continuity_hook", "switchtest_continuity_hook")
cg = load_hook("credential_guard", "switchtest_credential_guard")
ms = load_hook("memory_sync_hook", "switchtest_memory_sync_hook")
te = load_hook("trust_events", "switchtest_trust_events")
me = load_hook("mine_session_end", "switchtest_mine_session_end")
ts = load_hook("trust_signals", "switchtest_trust_signals")

Reader = Callable[[Mapping[str, str]], bool]

READERS: dict[str, Reader] = {
    "NOBLIVION_STORE_AUTOSTART": sc.autostart_on,
    "NOBLIVION_RECALL_DISABLE": rh.recall_disabled,
    "NOBLIVION_RECALL_LABELS": lr.prompt_enabled,
    "NOBLIVION_RECALL_MD_ONLY": rh.md_only_on,
    "NOBLIVION_RECALL_INDEX": rh.index_mode,
    "NOBLIVION_RECALL_INDEX_HYGIENE": rh.index_hygiene,
    "NOBLIVION_RECALL_INDEX_RERANK": rh.index_rerank,
    "NOBLIVION_RECALL_INDEX_RULE_ROWS": rh.index_rule_rows,
    "NOBLIVION_RECALL_INDEX_APPLY": rh.index_apply,
    "NOBLIVION_RECALL_SHOWN_SET": rh.shown_set_on,
    "NOBLIVION_RECALL_INDEX_ROW_DEDUPE": rh.index_row_dedupe,
    "NOBLIVION_RECALL_INDEX_CHECK_LINE": rh.index_check_line,
    "NOBLIVION_RECALL_INDEX_DROP_NO_RULE": rh.index_drop_no_rule,
    "NOBLIVION_SUBAGENT_RULES_OFF": sr.is_off,
    "NOBLIVION_CONTINUITY": ch.is_on,
    "NOBLIVION_CONTINUITY_BRIEFING": lambda e: ch._flag(e, ch.BRIEFING_ENV, True),
    "NOBLIVION_CONTINUITY_PRECOMPACT_PRINT": lambda e: ch._flag(e, ch.PRECOMPACT_PRINT_ENV, True),
    "NOBLIVION_CONTINUITY_REQUIRE_STATUS": lambda e: ch._flag(e, ch.REQUIRE_STATUS_ENV, False),
    "NOBLIVION_CONTINUITY_CURATED": lambda e: ch._flag(e, ch.CURATED_ENV, True),
    "NOBLIVION_GUARD_CREDENTIAL": cg.guard_on,
    "NOBLIVION_GUARD_LABELS": lr.enabled,
    "NOBLIVION_GUARD_LABELS_FILE_TOOLS": lr.file_tools_enabled,
    "NOBLIVION_MEMORY_SYNC_OFF": ms.is_off,
    "NOBLIVION_MEMORY_SYNC_BASH_OFF": ms.is_bash_off,
    "NOBLIVION_TRUST_EVENTS": te.enabled,
    "NOBLIVION_TRUST_CITATION_USE": ts.citation_on,
    "NOBLIVION_TRUST_CORRECTION_CONTRADICT": ts.correction_on,
    "NOBLIVION_MINER": me.is_enabled,
}

ON = ("1", "true", "yes", "on", " ON ", "True")
OFF = ("0", "false", "no", "off", "", " Off ", "FALSE")
OTHER = ("maybe", "2x")


def _documented() -> dict[str, bool]:
    return {m.group(1): m.group(2) == "on" for m in ROW.finditer(DOC.read_text(encoding="utf-8"))}


def test_every_documented_switch_has_a_reader_and_back():
    assert set(_documented()) == set(READERS)


@pytest.fixture
def base(tmp_path) -> dict[str, str]:
    """No config file and no other switch set."""
    return {"NOBLIVION_CONFIG": str(tmp_path / "none.json"), "HOME": str(tmp_path)}


@pytest.mark.parametrize("name", sorted(READERS))
def test_each_switch_parses_the_same_way(name, base):
    read, default = READERS[name], _documented()[name]
    assert read(base) is default, "not set"
    for value in ON:
        assert read(dict(base, **{name: value})) is True, value
    for value in OFF:
        assert read(dict(base, **{name: value})) is False, value
    for value in OTHER:
        assert read(dict(base, **{name: value})) is default, value


@pytest.mark.parametrize("value", ON + OFF + OTHER + (None,))
def test_the_store_and_the_hook_helper_agree(value):
    hc = load_hook("hook_config", "switchtest_hook_config")
    for default in (True, False):
        assert config.parse_switch(value, default) is hc.parse_switch(value, default)


@pytest.mark.parametrize(("value", "on"), [(True, True), (False, False), (1, True), (0, False)])
def test_json_values_in_the_config_file(value, on):
    assert config.parse_switch(value, not on) is on


def test_the_miner_command_reads_the_switch_like_the_hook(base):
    for value in ON:
        assert miner.load_miner_settings(dict(base, NOBLIVION_MINER=value)).enabled is True
    for value in OFF:
        assert miner.load_miner_settings(dict(base, NOBLIVION_MINER=value)).enabled is False
    # Not set, or a value that is no switch word: both keep the default, off.
    for env in (base, dict(base, NOBLIVION_MINER="maybe")):
        assert miner.load_miner_settings(env).enabled is False
        assert me.is_enabled(env) is False
