# SPDX-License-Identifier: AGPL-3.0-or-later
"""Put ``tools`` on the path so the tests import the ``longtest`` package."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
