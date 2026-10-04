# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session set up by ``conftest.py`` never points at the user's data
(NOBLIVION-36)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from conftest import CLAUDE_VARS


def test_home_and_xdg_data_home_are_temporary():
    tmp = Path(tempfile.gettempdir()).resolve()
    for name in ("HOME", "XDG_DATA_HOME"):
        assert tmp in Path(os.environ[name]).resolve().parents, name
    assert Path.home() == Path(os.environ["HOME"])


def test_no_inherited_data_settings():
    assert not [n for n in CLAUDE_VARS if n in os.environ]
    left = [
        n for n in os.environ if n.startswith("NOBLIVION_") and not n.startswith("NOBLIVION_TEST_")
    ]
    assert not left
