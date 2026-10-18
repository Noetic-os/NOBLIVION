# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen split validation and paired, external-file task replay (standard library only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "eval/replay_manifest.json"
MAX_DATA_BYTES = 2_000_000


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def bounded_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_DATA_BYTES + 1)
    if len(raw) > MAX_DATA_BYTES:
        raise ValueError("evaluation data exceeds size limit")
    return raw


def read_json(path: Path):
    return json.loads(bounded_bytes(path))


def query_keys(data: dict) -> tuple[set[str], set[str]]:
    ids, content = set(), set()
    for group in ("queries", "error_queries"):
        for query in data["recall"][group]:
            text = normalized(query["q"])
            key = query.get("id", hashlib.sha256(text.encode()).hexdigest())
            if key in ids:
                raise ValueError("duplicate query id")
            ids.add(key)
            content.add(text)
    return ids, content


def validate_split(tuning: dict, heldout: dict) -> None:
    if heldout.get("tuning_allowed") is not False or heldout.get("split") != "heldout":
        raise ValueError("evaluation requires a heldout split with tuning forbidden")
    train_ids, train_content = query_keys(tuning)
    test_ids, test_content = query_keys(heldout)
    if train_ids & test_ids or train_content & test_content:
        raise ValueError("query leakage between tuning and heldout")
    train_notes, test_notes = set(), set()
    train_text, test_text = set(), set()
    for data, ids, content in (
        (tuning, train_notes, train_text),
        (heldout, test_notes, test_text),
    ):
        for group in ("recall", "dedup"):
            for note in data[group]["memories"]:
                if note["id"] in ids:
                    raise ValueError("duplicate memory id")
                ids.add(note["id"])
                # Check each semantic field, not just the concatenation.
                content.update(normalized(note[field]) for field in ("name", "description", "body"))
        recall_ids = {n["id"] for n in data["recall"]["memories"]}
        for group in ("queries", "error_queries"):
            for query in data["recall"][group]:
                if not set(query["expect"]) <= recall_ids:
                    raise ValueError("unknown expected memory")
        dedup_ids = {n["id"] for n in data["dedup"]["memories"]}
        for pair in data["dedup"]["duplicates"]:
            if len(pair) != 2 or pair[0] == pair[1] or not set(pair) <= dedup_ids:
                raise ValueError("invalid duplicate label")
    if (train_ids | train_notes) & (test_ids | test_notes) or (train_text | train_content) & (
        test_text | test_content
    ):
        raise ValueError("memory leakage between tuning and heldout")


def load_manifest(path: Path = MANIFEST) -> tuple[dict, dict[str, Path]]:
    manifest = read_json(path)
    if manifest.get("version") != 1 or manifest.get("evidence_kind") != "synthetic":
        raise ValueError("unsupported manifest or evidence kind")
    paths = {}
    for name in ("tuning", "heldout", "tasks", "synthetic_runner"):
        entry = manifest["files"][name]
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("manifest paths must stay in its directory")
        candidate = (path.parent / relative).resolve()
        if not candidate.is_relative_to(path.parent.resolve()):
            raise ValueError("manifest path escapes through a symlink")
        if hashlib.sha256(bounded_bytes(candidate)).hexdigest() != entry["sha256"]:
            raise ValueError(f"frozen file hash mismatch: {name}")
        paths[name] = candidate
    validate_split(read_json(paths["tuning"]), read_json(paths["heldout"]))
    return manifest, paths


def cleanup_runner(process: subprocess.Popen) -> dict:
    """Stop the owned child and report any unconfirmed process-group cleanup."""
    result = {
        "group_signal_denied": False,
        "child_signal_denied": False,
        "child_reaped": False,
        "errors": [],
    }
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            result["group_signal_denied"] = True
    # A group signal may be refused or race with exit. The owned child still
    # needs a separate, bounded kill/reap attempt before its pipes are closed.
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except PermissionError:
            result["child_signal_denied"] = True
    try:
        process.wait(timeout=1.0)
        result["child_reaped"] = True
    except subprocess.TimeoutExpired:
        result["errors"].append("owned_child_not_reaped")
    if result["group_signal_denied"]:
        # Darwin can refuse a signal to an exiting group. Only absence after
        # reaping resolves that refusal; a surviving or inaccessible group
        # must not be reported as successfully terminated.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            result["errors"].append("process_group_cleanup_unconfirmed")
        else:
            result["errors"].append("process_group_cleanup_unconfirmed")
    return result


def run_bounded(argv: list[str], request: bytes, cwd: Path, limits: dict) -> dict:
    """One invocation. Cap pipe bytes and wall time; kill descendants on POSIX.

    This is process hygiene for a trusted runner, not an OS security sandbox.
    The runner is responsible for enforcing its internal model/token budget.
    """
    if len(request) > limits["input_bytes"]:
        return {"status": "input_limit", "usage": None}
    environment = {
        "PATH": os.defpath,
        "HOME": str(cwd / "home"),
        "TMPDIR": str(cwd / "tmp"),
        "LANG": "C.UTF-8",
    }
    for folder in ("home", "tmp"):
        (cwd / folder).mkdir()
    started = time.monotonic()

    def child_limits():
        import resource

        resource.setrlimit(
            resource.RLIMIT_FSIZE, (limits["artifact_bytes"], limits["artifact_bytes"])
        )
        cpu = math.ceil(limits["seconds"]) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=child_limits if os.name == "posix" else None,
    )
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    status = "ok"
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            sent = 0
            while selector.get_map():
                if time.monotonic() - started > limits["seconds"]:
                    status = "timeout"
                    break
                for key, _ in selector.select(timeout=0.02):
                    if key.data == "stdin":
                        try:
                            sent += os.write(key.fd, request[sent : sent + 4096])
                        except BrokenPipeError:
                            sent = len(request)
                        if sent == len(request):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            streams[key.data].extend(chunk)
                            if sum(map(len, streams.values())) > limits["output_bytes"]:
                                status = "output_limit"
                                break
                if status != "ok":
                    break
            if status == "ok":
                remaining = max(0.001, limits["seconds"] - (time.monotonic() - started))
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    status = "timeout"
    finally:
        # Also remove descendants that retained no pipes after their parent exited.
        try:
            cleanup = cleanup_runner(process)
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
    usage = None
    if status == "ok":
        if process.returncode != 0:
            status = "runner_error"
        else:
            try:
                response = json.loads(streams["stdout"])
                usage = response["usage"]
                for name in ("iterations", "input_tokens", "output_tokens"):
                    value = usage[name]
                    if type(value) is not int or value < 0 or value > limits[name]:
                        raise ValueError("invalid usage")
                if usage["iterations"] < 1:
                    raise ValueError("missing invocation usage")
            except (ValueError, KeyError, TypeError):
                status, usage = "invalid_usage", None
    runner_status = status
    if cleanup["errors"]:
        status, usage = "cleanup_error", None
    return {
        "status": status,
        "runner_status": runner_status,
        "cleanup": cleanup,
        "returncode": process.returncode,
        "seconds": round(time.monotonic() - started, 6),
        "stdout_bytes": len(streams["stdout"]),
        "stderr_bytes": len(streams["stderr"]),
        "usage": usage,
    }


def safe_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("task file must be a relative path")
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("task file escapes workspace")
    return result


def observe(root: Path, expected: dict[str, str], maximum: int, absent=()) -> dict:
    matches = {}
    for name, wanted in expected.items():
        try:
            path = safe_file(root, name)
            # Do not follow symlinks, including those within the workspace.
            parents = [path, *path.parents]
            if any(p.is_symlink() for p in parents if p.is_relative_to(root)):
                raise ValueError("symlink output")
            if not path.is_file() or path.stat().st_size > maximum:
                raise ValueError("missing or oversized output")
            matches[name] = path.read_bytes() == wanted.encode()
        except (OSError, ValueError):
            matches[name] = False
    for name in absent:
        try:
            path = safe_file(root, name)
            matches[name] = not path.exists() and not path.is_symlink()
        except (OSError, ValueError):
            matches[name] = False
    return {"files": matches, "passed": bool(matches) and all(matches.values())}


def replay(manifest_path: Path, argv: list[str]) -> dict:
    manifest, paths = load_manifest(manifest_path)
    limits = manifest["limits"]
    for name in (
        "input_bytes",
        "output_bytes",
        "artifact_bytes",
        "iterations",
        "input_tokens",
        "output_tokens",
        "max_tasks",
    ):
        if type(limits[name]) is not int or not 0 < limits[name] <= 2_000_000:
            raise ValueError("invalid replay limit")
    if not math.isfinite(limits["seconds"]) or not 0 < limits["seconds"] <= 60:
        raise ValueError("invalid wall time limit")
    tasks = read_json(paths["tasks"])
    if (
        tasks.get("evidence_kind") != "synthetic"
        or not 0 < len(tasks["tasks"]) <= limits["max_tasks"]
    ):
        raise ValueError("invalid task set")
    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError("runner executable must be an absolute path; arguments are a list")
    rows, seen = [], set()
    aborted = None
    for index, task in enumerate(tasks["tasks"]):
        if task["id"] in seen:
            raise ValueError("duplicate task id")
        seen.add(task["id"])
        if not task["expected_files"] and not task.get("absent_files"):
            raise ValueError("task needs an external file oracle")
        row = {"id": task["id"], "arms": {}}
        # Alternate order to avoid putting the same arm first on every task.
        for arm in ("off", "on") if index % 2 == 0 else ("on", "off"):
            with tempfile.TemporaryDirectory(prefix="memory-replay-") as raw:
                root = Path(raw)
                for name, content in task.get("initial_files", {}).items():
                    file = safe_file(root, name)
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(content, encoding="utf-8")
                request = json.dumps(
                    {
                        "version": 1,
                        "task_id": task["id"],
                        "prompt": task["prompt"],
                        "memory_context": task["memory_context"] if arm == "on" else "",
                        "limits": limits,
                    }
                ).encode()
                result = run_bounded(argv, request, root, limits)
                if result["status"] == "cleanup_error":
                    # Do not inspect task files or start another runner while
                    # this invocation may still have a live child or group.
                    aborted = "cleanup_error"
                    observation = {"files": {}, "passed": False}
                else:
                    observation = observe(
                        root,
                        task["expected_files"],
                        limits["artifact_bytes"],
                        task.get("absent_files", ()),
                    )
                row["arms"][arm] = {
                    **result,
                    "observation": observation,
                    "passed": result["status"] == "ok" and observation["passed"],
                }
            if aborted:
                break
        rows.append(row)
        if aborted:
            break
    passed = {
        arm: sum(row["arms"].get(arm, {}).get("passed", False) for row in rows)
        for arm in ("off", "on")
    }
    valid = [
        row
        for row in rows
        if all(row["arms"].get(arm, {}).get("status") == "ok" for arm in ("off", "on"))
    ]
    difference = sum(
        int(row["arms"]["on"]["passed"]) - int(row["arms"]["off"]["passed"]) for row in valid
    )
    return {
        "version": 1,
        "evidence_kind": "synthetic_harness_verification",
        "qualification": "Synthetic runner proof only; actual client replay is separate.",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "frozen_files": manifest["files"],
        "limits": limits,
        "runner": {
            "executable_sha256": hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest(),
            "argument_count": len(argv) - 1,
            "identity_note": "Executable hash only; dependencies and arguments are not archived.",
        },
        "sample_size": len(rows),
        "planned_sample_size": len(tasks["tasks"]),
        "not_run_tasks": len(tasks["tasks"]) - len(rows),
        "aborted": aborted,
        "valid_pairs": len(valid),
        "invalid_pairs": len(rows) - len(valid),
        "passed": passed,
        "paired_pass_rate_difference": difference / len(valid) if valid else None,
        "usage_source": (
            "runner-reported; iterations and tokens are validated, not independently metered"
        ),
        "tasks": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("runner", nargs=argparse.REMAINDER, help="-- /absolute/executable args...")
    args = parser.parse_args(argv)
    runner = args.runner[1:] if args.runner[:1] == ["--"] else args.runner
    try:
        report = replay(args.manifest, runner)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "sample_size",
                    "valid_pairs",
                    "passed",
                    "paired_pass_rate_difference",
                    "evidence_kind",
                )
            }
        )
    )
    return 1 if report["invalid_pairs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
