# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic harness tests use real subprocesses and externally observed file effects."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def replay(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    import eval_replay

    return eval_replay


@pytest.fixture
def core_runtime():
    # The core requires 3.10; the package-free 3.9 lane still runs pure replay tests.
    if sys.version_info < (3, 10):  # noqa: UP036 - package-free CI also runs on 3.9
        pytest.skip("threshold evaluator needs the core runtime (Python >=3.10)")


@pytest.fixture
def thresholds(replay, core_runtime):
    spec = importlib.util.spec_from_file_location(
        "threshold_eval_under_test", ROOT / "tools/eval_thresholds.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def frozen_copy(replay, tmp_path):
    manifest, paths = replay.load_manifest()
    for name, source in paths.items():
        destination = tmp_path / manifest["files"][name]["path"]
        destination.write_bytes(source.read_bytes())
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


def rewrite_fixture(path, manifest, name, data):
    fixture = path.parent / manifest["files"][name]["path"]
    fixture.write_text(json.dumps(data))
    manifest["files"][name]["sha256"] = hashlib.sha256(fixture.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))


def test_frozen_files_and_leakage(replay):
    manifest, paths = replay.load_manifest()
    assert manifest["evidence_kind"] == "synthetic"
    heldout = replay.read_json(paths["heldout"])
    assert len(heldout["recall"]["queries"]) == 3
    assert len(heldout["recall"]["error_queries"]) == 2
    assert heldout["tuning_allowed"] is False


@pytest.mark.parametrize("name", ["tuning", "heldout", "tasks", "synthetic_runner"])
def test_tamper_rejected_before_execution(replay, tmp_path, name):
    path, manifest = frozen_copy(replay, tmp_path)
    fixture = path.parent / manifest["files"][name]["path"]
    fixture.write_bytes(fixture.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        replay.load_manifest(path)


@pytest.mark.parametrize("kind", ["query_text", "query_id", "memory_text", "memory_id"])
def test_split_rejects_content_and_id_overlap(replay, kind):
    _, paths = replay.load_manifest()
    training = replay.read_json(paths["tuning"])
    heldout = replay.read_json(paths["heldout"])
    if kind == "query_text":
        heldout["recall"]["queries"][0]["q"] = (
            "  " + training["recall"]["queries"][0]["q"].upper() + " \n"
        )
    elif kind == "query_id":
        training["recall"]["queries"][0]["id"] = heldout["recall"]["queries"][0]["id"]
    elif kind == "memory_text":
        heldout["recall"]["memories"][0]["body"] = training["recall"]["memories"][0]["body"].upper()
    else:
        training["recall"]["memories"][0]["id"] = heldout["recall"]["memories"][0]["id"]
        training["recall"]["queries"] = []
        training["recall"]["error_queries"] = []
    with pytest.raises(ValueError, match="leakage"):
        replay.validate_split(training, heldout)


def test_unicode_normalization(replay):
    assert replay.normalized(" ＣＡＴＡＬＯＧＵＥ \t Code ") == "catalogue code"


def test_split_rejects_query_to_memory_content_leak(replay):
    _, paths = replay.load_manifest()
    tuning = replay.read_json(paths["tuning"])
    heldout = replay.read_json(paths["heldout"])
    heldout["recall"]["memories"][0]["body"] = tuning["recall"]["queries"][0]["q"]
    with pytest.raises(ValueError, match="leakage"):
        replay.validate_split(tuning, heldout)


def test_manifest_symlink_escape_rejected(replay, tmp_path):
    path, manifest = frozen_copy(replay, tmp_path)
    fixture = path.parent / manifest["files"]["heldout"]["path"]
    outside = tmp_path.parent / (tmp_path.name + "-outside.json")
    outside.write_bytes(fixture.read_bytes())
    fixture.unlink()
    fixture.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        replay.load_manifest(path)


def test_unknown_labels_rejected(replay):
    _, paths = replay.load_manifest()
    heldout = replay.read_json(paths["heldout"])
    heldout["recall"]["queries"][0]["expect"] = ["not-a-note"]
    with pytest.raises(ValueError, match="unknown expected"):
        replay.validate_split(replay.read_json(paths["tuning"]), heldout)


def test_actual_paired_task_state_and_negative_case(replay):
    _, paths = replay.load_manifest()
    result = replay.replay(replay.MANIFEST, [sys.executable, str(paths["synthetic_runner"])])
    assert result["evidence_kind"] == "synthetic_harness_verification"
    assert result["sample_size"] == result["valid_pairs"] == 3
    assert result["invalid_pairs"] == 0
    assert result["passed"] == {"off": 1, "on": 3}
    assert result["paired_pass_rate_difference"] == pytest.approx(2 / 3)
    for task in result["tasks"]:
        assert task["arms"]["on"]["observation"]["passed"] is True
        assert task["arms"]["on"]["usage"]["iterations"] == 1
    negative = result["tasks"][-1]
    assert negative["arms"]["on"]["observation"]["files"]["catalogue.txt"] is True


def test_runner_claims_cannot_replace_external_files(replay, tmp_path):
    path, manifest = frozen_copy(replay, tmp_path)
    tasks = {
        "evidence_kind": "synthetic",
        "tasks": [
            {
                "id": "lying-runner",
                "prompt": "Produce a file",
                "memory_context": "a hint",
                "expected_files": {"actual.txt": "done\n"},
            }
        ],
    }
    rewrite_fixture(path, manifest, "tasks", tasks)
    response = {
        "passed": True,
        "tasks_completed": 999,
        "usage": {"iterations": 1, "input_tokens": 0, "output_tokens": 0},
    }
    code = "print(" + repr(json.dumps(response)) + ")"
    report = replay.replay(path, [sys.executable, "-c", code])
    assert report["valid_pairs"] == 1
    assert report["passed"] == {"off": 0, "on": 0}
    assert report["paired_pass_rate_difference"] == 0


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import time; time.sleep(10)", "timeout"),
        ("import sys; sys.stdout.write('x'*20000); sys.stdout.flush()", "output_limit"),
        ("import sys; sys.stderr.write('x'*20000); sys.stderr.flush()", "output_limit"),
        ("print('{}')", "invalid_usage"),
        (
            'print(\'{"usage":{"iterations":2,"input_tokens":0,"output_tokens":0}}\')',
            "invalid_usage",
        ),
        (
            'print(\'{"usage":{"iterations":1,"input_tokens":true,"output_tokens":0}}\')',
            "invalid_usage",
        ),
        ("raise SystemExit(7)", "runner_error"),
    ],
)
def test_subprocess_limits_and_protocol(replay, tmp_path, code, expected):
    manifest, _ = replay.load_manifest()
    limits = {**manifest["limits"], "seconds": 0.15}
    result = replay.run_bounded([sys.executable, "-c", code], b"{}", tmp_path, limits)
    assert result["status"] == expected
    assert result["seconds"] < 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signals")
@pytest.mark.parametrize("exited", [False, True])
def test_group_signal_refusal_still_reaps_owned_child(replay, tmp_path, monkeypatch, exited):
    manifest, _ = replay.load_manifest()
    processes = []
    popen, killpg = replay.subprocess.Popen, replay.os.killpg

    def record_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def deny_group_signal(pid, sig):
        if sig == replay.signal.SIGKILL:
            raise PermissionError("simulated group signal refusal")
        return killpg(pid, sig)

    monkeypatch.setattr(replay.subprocess, "Popen", record_process)
    monkeypatch.setattr(replay.os, "killpg", deny_group_signal)
    code = (
        'print(\'{"usage":{"iterations":1,"input_tokens":0,"output_tokens":0}}\')'
        if exited
        else "import sys,time;sys.stderr.write('x'*20000);sys.stderr.flush();time.sleep(10)"
    )
    try:
        result = replay.run_bounded(
            [sys.executable, "-c", code], b"{}", tmp_path, manifest["limits"]
        )
        assert result["status"] == ("ok" if exited else "output_limit")
        assert result["cleanup"]["group_signal_denied"] is True
        assert result["cleanup"]["child_reaped"] is True
        assert result["cleanup"]["errors"] == []
        assert processes[0].poll() is not None
        assert all(
            pipe.closed for pipe in (processes[0].stdin, processes[0].stdout, processes[0].stderr)
        )
    finally:
        # The regression also runs against old code, where refusal escaped
        # before reaping. Keep that intentionally failing probe self-cleaning.
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1.0)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signals")
@pytest.mark.parametrize("inaccessible", [False, True])
def test_unconfirmed_group_cleanup_cannot_report_success(
    replay, tmp_path, monkeypatch, inaccessible
):
    manifest, _ = replay.load_manifest()

    def deny_or_find_group(pid, sig):
        if sig == replay.signal.SIGKILL or inaccessible:
            raise PermissionError("simulated surviving or inaccessible group")
        assert sig == 0  # A group still exists after the owned child was reaped.

    monkeypatch.setattr(replay.os, "killpg", deny_or_find_group)
    code = 'print(\'{"usage":{"iterations":1,"input_tokens":0,"output_tokens":0}}\')'
    result = replay.run_bounded([sys.executable, "-c", code], b"{}", tmp_path, manifest["limits"])
    assert result["status"] == "cleanup_error"
    assert result["runner_status"] == "ok"
    assert result["usage"] is None
    assert result["cleanup"]["child_reaped"] is True
    assert result["cleanup"]["errors"] == ["process_group_cleanup_unconfirmed"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signals")
@pytest.mark.parametrize("unreapable", [False, True])
def test_owned_child_cleanup_race_and_bounded_reap(replay, monkeypatch, unreapable):
    class Process:
        pid = 123

        def poll(self):
            return None

        def kill(self):
            if unreapable:
                raise PermissionError("simulated child signal refusal")
            raise ProcessLookupError("child exited before direct kill")

        def wait(self, *, timeout):
            assert timeout == 1.0
            if unreapable:
                raise replay.subprocess.TimeoutExpired("synthetic runner", timeout)
            return -9

    def missing_group(pid, sig):
        raise ProcessLookupError("group already exited")

    monkeypatch.setattr(replay.os, "killpg", missing_group)
    result = replay.cleanup_runner(Process())
    assert result["child_reaped"] is not unreapable
    assert result["child_signal_denied"] is unreapable
    assert result["errors"] == (["owned_child_not_reaped"] if unreapable else [])


@pytest.mark.parametrize("failed_invocation", [1, 2])
def test_cleanup_failure_stops_replay_before_next_arm_or_task(
    replay, monkeypatch, failed_invocation
):
    invocations, observations = [], []

    def run(argv, request, root, limits):
        invocations.append(json.loads(request)["task_id"])
        if len(invocations) == failed_invocation:
            return {
                "status": "cleanup_error",
                "runner_status": "timeout",
                "usage": None,
                "cleanup": {"child_reaped": False, "errors": ["owned_child_not_reaped"]},
            }
        return {"status": "ok", "usage": {"iterations": 1, "input_tokens": 0, "output_tokens": 0}}

    def observe(*args):
        observations.append(True)
        return {"files": {}, "passed": True}

    monkeypatch.setattr(replay, "run_bounded", run)
    monkeypatch.setattr(replay, "observe", observe)
    report = replay.replay(replay.MANIFEST, [sys.executable])
    assert len(invocations) == failed_invocation
    assert len(observations) == failed_invocation - 1
    assert report["aborted"] == "cleanup_error"
    assert report["sample_size"] == report["invalid_pairs"] == 1
    assert report["planned_sample_size"] == 3
    assert report["not_run_tasks"] == 2
    assert report["valid_pairs"] == 0
    assert report["paired_pass_rate_difference"] is None
    arms = report["tasks"][0]["arms"]
    assert len(arms) == failed_invocation
    failed_arm = list(arms.values())[-1]
    assert failed_arm["runner_status"] == "timeout"
    assert failed_arm["usage"] is None
    assert failed_arm["passed"] is False
    assert failed_arm["observation"] == {"files": {}, "passed": False}


def test_input_limit_prevents_spawn(replay, tmp_path):
    manifest, _ = replay.load_manifest()
    result = replay.run_bounded(["/does/not/exist"], b"x" * 17000, tmp_path, manifest["limits"])
    assert result["status"] == "input_limit"


def test_invalid_pairs_do_not_look_like_a_measured_improvement(replay):
    report = replay.replay(replay.MANIFEST, [sys.executable, "-c", "print('{}')"])
    assert report["sample_size"] == report["invalid_pairs"] == 3
    assert report["valid_pairs"] == 0
    assert report["paired_pass_rate_difference"] is None


def test_oracle_labels_are_withheld_from_runner(replay, tmp_path):
    path, manifest = frozen_copy(replay, tmp_path)
    tasks = {
        "evidence_kind": "synthetic",
        "tasks": [
            {
                "id": "private-label",
                "prompt": "Write the marker to output.txt.",
                "memory_context": "Use the marker you independently know.",
                "expected_files": {"output.txt": "withheld-answer"},
            }
        ],
    }
    rewrite_fixture(path, manifest, "tasks", tasks)
    code = """import json, sys
from pathlib import Path
request = sys.stdin.read()
assert 'withheld-answer' not in request
assert 'expected_files' not in request
Path('output.txt').write_text('withheld-answer')
print(json.dumps({'usage': {'iterations': 1, 'input_tokens': 0, 'output_tokens': 0}}))
"""
    report = replay.replay(path, [sys.executable, "-c", code])
    assert report["valid_pairs"] == 1
    assert report["passed"] == {"off": 1, "on": 1}


@pytest.mark.skipif(sys.platform != "linux", reason="inspect Linux child process state")
def test_timeout_stops_descendant_process(replay, tmp_path):
    import os

    manifest, _ = replay.load_manifest()
    limits = {**manifest["limits"], "seconds": 0.2}
    code = """import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
Path('child.pid').write_text(str(child.pid))
time.sleep(20)
"""
    result = replay.run_bounded([sys.executable, "-c", code], b"{}", tmp_path, limits)
    assert result["status"] == "timeout"
    pid = int((tmp_path / "child.pid").read_text())
    stat = Path(f"/proc/{pid}/stat")
    # A terminated orphan may remain a zombie until the host init reaps it.
    if stat.exists():
        assert stat.read_text().split()[2] == "Z"
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file resource limit")
def test_actual_artifact_writes_are_bounded(replay, tmp_path):
    manifest, _ = replay.load_manifest()
    code = "from pathlib import Path; Path('large.bin').write_bytes(b'x' * 20000)"
    result = replay.run_bounded([sys.executable, "-c", code], b"{}", tmp_path, manifest["limits"])
    assert result["status"] == "runner_error"
    assert (tmp_path / "large.bin").stat().st_size <= manifest["limits"]["artifact_bytes"]


def test_environment_and_workspace_isolation(replay, tmp_path, monkeypatch):
    manifest, _ = replay.load_manifest()
    monkeypatch.setenv("PRIVATE_TEST_VALUE", "must-not-reach-runner")
    code = """import json, os
from pathlib import Path
assert 'PRIVATE_TEST_VALUE' not in os.environ
assert not Path('seen').exists()
assert Path(os.environ['HOME']).parent == Path.cwd()
assert Path(os.environ['TMPDIR']).parent == Path.cwd()
Path('seen').write_text('yes')
print(json.dumps({'usage': {'iterations': 1, 'input_tokens': 0, 'output_tokens': 0}}))
"""
    for name in ("off", "on"):
        root = tmp_path / name
        root.mkdir()
        result = replay.run_bounded([sys.executable, "-c", code], b"{}", root, manifest["limits"])
        assert result["status"] == "ok"
        assert (root / "seen").read_text() == "yes"


def test_observer_rejects_symlinks_escapes_wrong_and_oversized_files(replay, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("done")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "link.txt").symlink_to(outside)
    (root / "wrong.txt").write_text("wrong")
    (root / "large.txt").write_text("done" * 10)
    result = replay.observe(
        root,
        {"link.txt": "done", "../outside.txt": "done", "wrong.txt": "done", "large.txt": "done"},
        8,
    )
    assert result["passed"] is False
    assert not any(result["files"].values())


def test_evaluate_rejects_override_and_tune_rejects_heldout(thresholds, replay, monkeypatch):
    monkeypatch.setattr(
        thresholds, "run", lambda *a, **kw: pytest.fail("must fail before store startup")
    )
    _, paths = replay.load_manifest()
    with pytest.raises(SystemExit) as exc:
        thresholds.main(["--mode", "evaluate", "--data", str(paths["tuning"])])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        thresholds.main(["--mode", "tune", "--data", str(paths["heldout"])])
    assert exc.value.code == 2


def test_evaluate_never_calls_best_or_emits_tuning_scores(thresholds, monkeypatch, tmp_path):
    current = {
        k: 0.5 for k in ("recall", "index", "subagent", "error_recall", "error_local", "dedup")
    }
    point = thresholds.Point(0.5, 1, 0, 0)
    results = {name: [point] for name in current}

    def measured(data, *, evaluate):
        assert evaluate is True
        assert data["tuning_allowed"] is False
        return results, current, {}, {"off": point, "unset": point, "subagent_unset": point}

    monkeypatch.setattr(thresholds, "run", measured)
    monkeypatch.setattr(thresholds, "best", lambda *a: pytest.fail("heldout must not choose best"))
    destination = tmp_path / "report.json"
    assert thresholds.main(["--mode", "evaluate", "--check", "--json", str(destination)]) == 0
    report = json.loads(destination.read_text())
    assert not {"best", "curves", "dedup_pairs"} & report.keys()
    assert report["mode"] == "evaluate"
    assert set(report["index_paths"]) == {"unset"}
    assert report["sample_sizes"]["queries"] == 3
    assert len(report["manifest_sha256"]) == 64


def test_evaluation_uses_one_shipped_value_per_measurement(thresholds, monkeypatch):
    class FakeStore:
        def __init__(self, *args):
            self.folders = {thresholds.RECALL_ROOT: Path("unused")}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    values = {
        name: 0.5 + index * 0.01
        for index, name in enumerate(
            ("recall", "index", "subagent", "error_recall", "error_local", "dedup")
        )
    }
    observed = {}
    monkeypatch.setattr(thresholds, "shipped", lambda *a: values)
    monkeypatch.setattr(thresholds, "load_hook", lambda *a: SimpleNamespace())
    monkeypatch.setattr(thresholds, "EvalStore", FakeStore)
    monkeypatch.setattr(thresholds, "dedup_cosines", lambda *a: {})

    def measurement(name):
        def measure(*args, **kwargs):
            if name == "index":
                assert kwargs == {"include_off": False}
            selected = args[-1]
            observed[name] = selected
            point = thresholds.Point(selected[0], 0, 0, 0)
            return (
                ([point], point, point)
                if name == "index"
                else (([point], point) if name == "subagent" else [point])
            )

        return measure

    for name in values:
        monkeypatch.setattr(thresholds, "measure_" + name, measurement(name))
    data = {"recall": {"queries": [], "error_queries": []}, "dedup": {"duplicates": []}}
    result = thresholds.run(data, evaluate=True)
    assert observed == {name: [value] for name, value in values.items()}
    assert result[2] == {}


def test_data_read_is_bounded(replay, tmp_path):
    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * (replay.MAX_DATA_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        replay.read_json(path)


def test_threshold_module_is_importable_as_namespace(monkeypatch, core_runtime):
    monkeypatch.syspath_prepend(str(ROOT))
    import tools.eval_thresholds as module

    assert module.MANIFEST.is_file()


@pytest.mark.parametrize("entry", ["spec", "namespace", "script"])
@pytest.mark.parametrize("shadow", ["none", "module", "path"])
def test_threshold_import_entry_points_in_clean_process(tmp_path, entry, shadow, core_runtime):
    # The legacy CI fixture assigns an arbitrary module name and does not add
    # tools/ to sys.path. A fresh isolated interpreter prevents earlier tests'
    # module imports and monkeypatches from hiding that execution path.
    code = r"""
import importlib.util
import runpy
import sys
import types
from pathlib import Path

root = Path(sys.argv[1])
entry, shadow = sys.argv[2], sys.argv[3]
sys.path = [p for p in sys.path if Path(p).resolve() != root / "tools"]
sys.path.insert(0, str(root / "src"))
if entry == "namespace":
    sys.path.insert(0, str(root))
for name in ("eval_replay", "tools.eval_replay"):
    sys.modules.pop(name, None)
if shadow == "path":
    sys.path.insert(0, str(Path.cwd()))
if shadow == "module":
    poison = types.ModuleType("eval_replay")
    poison.MANIFEST = "unrelated module must not be used"
    sys.modules["eval_replay"] = poison
before = list(sys.path)
if entry == "spec":
    spec = importlib.util.spec_from_file_location(
        "fresh_ci_fixture_alias", root / "tools/eval_thresholds.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # Point's dataclass needs its owning module.
    spec.loader.exec_module(module)
elif entry == "namespace":
    import tools.eval_thresholds as module
else:
    sys.argv = [str(root / "tools/eval_thresholds.py"), "--help"]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("script main did not run")
if entry != "script":
    assert module.MANIFEST == root / "tools/eval/replay_manifest.json"
    assert module.load_manifest()[0]["version"] == 1
assert sys.path == before
assert "tools.eval_replay" not in sys.modules
if shadow == "module":
    assert sys.modules["eval_replay"] is poison
    assert poison.MANIFEST == "unrelated module must not be used"
else:
    assert "eval_replay" not in sys.modules
"""
    # A same-name file in the unrelated working directory is also not a
    # substitute for the helper beside the script under test.
    (tmp_path / "eval_replay.py").write_text("raise RuntimeError('wrong sibling')\n")
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), entry, str(shadow)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_heldout_index_measurement_never_disables_the_floor(thresholds, monkeypatch, tmp_path):
    seen = []
    store = SimpleNamespace(
        tmp=tmp_path,
        memory_ids=lambda root: {},
        hook_env=lambda root, **extra: extra,
    )
    hook = SimpleNamespace(
        DEFAULT_INDEX_MIN_SCORE=0.68,
        run=lambda event, out, env: seen.append(env.get("NOBLIVION_RECALL_INDEX_MIN_SCORE")),
    )
    monkeypatch.setattr(thresholds, "index_status", lambda *args: "ok")
    queries = [{"q": "heldout question", "expect": []}]
    curve, off, unset = thresholds.measure_index(store, hook, queries, [0.68], include_off=False)
    assert seen == ["0.68", None]
    assert off is None
    assert curve[0].threshold == unset.threshold == 0.68
    seen.clear()
    _, off, _ = thresholds.measure_index(store, hook, queries, [0.68])
    assert seen == ["0.68", "off", None]
    assert off.threshold == -1
