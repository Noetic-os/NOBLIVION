# SPDX-License-Identifier: AGPL-3.0-or-later
"""NOBLIVION-142. The code default of each shipped recall setting equals
``config/config.default.json``, so a hook that runs with no such key in
``config.json`` behaves like a new install."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
SHIPPED = json.loads((ROOT / "config" / "config.default.json").read_text())["recall"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"t142_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _empty_env(tmp_path: Path) -> dict:
    """An env with no recall variable and a config file that holds nothing."""
    (tmp_path / "config.json").write_text("{}")
    return {"NOBLIVION_CONFIG": str(tmp_path / "config.json")}


def test_recall_hook_defaults_equal_the_shipped_config(tmp_path):
    env = _empty_env(tmp_path)
    recall = _load("recall_hook")
    assert recall.rules_only(env) is bool(SHIPPED["rules_only"])
    assert recall.prompt_rows(env) == SHIPPED["prompt_rows"]


def test_label_rows_default_equals_the_shipped_config(tmp_path):
    assert _load("label_rows").rules_only(_empty_env(tmp_path)) is bool(SHIPPED["rules_only"])


def test_hub_window_default_equals_the_shipped_config(tmp_path):
    assert _load("hub_stats").window_days(_empty_env(tmp_path)) == SHIPPED["hub_days"]


def test_a_set_value_still_wins_over_the_default(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"recall": {"rules_only": 0, "prompt_rows": 0, "hub_days": 0}}))
    env = {"NOBLIVION_CONFIG": str(cfg)}
    recall = _load("recall_hook")
    assert recall.rules_only(env) is False
    assert recall.prompt_rows(env) == 0
    assert _load("hub_stats").window_days(env) == 0
    env2 = {**env, "NOBLIVION_RECALL_PROMPT_ROWS": "8", "NOBLIVION_RECALL_RULES_ONLY": "1"}
    assert recall.prompt_rows(env2) == 8
    assert recall.rules_only(env2) is True
