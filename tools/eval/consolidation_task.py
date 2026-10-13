# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed fixture executor. Expected values are deliberately unavailable here.

Read delivered memory, then write an external configuration file only when
it supplies one unambiguous exact value for the requested key. The independent
replay oracle checks the resulting file against held-out labels.
"""

import json
import re
import sys
from pathlib import Path


def execute(key, context_path, output_path):
    if not re.fullmatch(r"archive_partition_\d{2}", key):
        raise ValueError("unknown fixture key")
    context = Path(context_path).read_text(encoding="utf-8")
    values = set(re.findall(r"\bSet " + re.escape(key) + r" to (\d+)\.", context))
    result = {key: int(values.pop())} if len(values) == 1 else {}
    Path(output_path).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    execute(*sys.argv[1:])
