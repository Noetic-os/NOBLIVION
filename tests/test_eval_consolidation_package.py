# SPDX-License-Identifier: AGPL-3.0-or-later
"""The package-dependent delayed-start regression stays separate from hook-only tests."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_child_waits_for_the_real_store_to_answer_before_running_hooks():
    script = """
import importlib.util
import json
import sys
import time
from pathlib import Path
source = Path(sys.argv[1])
sys.path.insert(0, str(source / "src"))
from noblivion import store
serve = store.Store.serve
def delayed_serve(self):
    time.sleep(3)
    return serve(self)
store.Store.serve = delayed_serve
spec = importlib.util.spec_from_file_location(
    "delayed_eval", source / "tools/eval_consolidation.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.child(source, True, module.CORPUS)))
"""
    run = subprocess.run(
        [sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["tasks_completed"] == 8
