# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic protocol runner: write the fictional catalogue code found in context."""

import json
import re
import sys
from pathlib import Path

request = json.load(sys.stdin)
match = re.search(r"Catalogue code: ([A-Z]{3}-\d{3})\.", request["memory_context"])
if match:
    Path("catalogue.txt").write_text(match[1] + "\n", encoding="utf-8")
print(json.dumps({"usage": {"iterations": 1, "input_tokens": 0, "output_tokens": 0}}))
