# SPDX-License-Identifier: AGPL-3.0-or-later
"""Held-out real-store/hook replay, extending the NOBLIVION-64 evaluation.

Compare actual baseline and changed source in separate Python processes. Only
provider/model responses are fixed. SQLite, leases, reservations, validation,
HTTP listener proof and hooks are real. Task completion comes from an external
file oracle, never assistant text, citations or task counters. This is a
fictional contract experiment; real-user causal benefit remains unavailable.
No production data, model download or paid provider is used.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "eval/consolidation_heldout.json"
EXECUTOR = HERE / "eval/consolidation_task.py"
BASELINE_SHA = "d911ddb136da8c1386082c3aad9ba56ee60fb39c"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_revision(root):
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def verify_baseline(source, repository):
    # Verify the complete archived baseline tree against the immutable Git tree.
    tree = subprocess.run(
        ["git", "ls-tree", "-rz", BASELINE_SHA], cwd=repository, capture_output=True, check=True
    ).stdout
    checked = 0
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id = header.split()
        if kind != b"blob":
            continue
        path = source / os.fsdecode(raw_path)
        if mode == b"120000":
            content = os.fsencode(os.readlink(path))
        else:
            content = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != object_id.decode():
            raise ValueError(
                "baseline source differs from required immutable tree: " + str(raw_path)
            )
        checked += 1
    return checked


def load_hook(path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("_heldout_hook", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FixedProvider:
    model = "fixed-extractive-fixture-v1"

    def __init__(self, response_type):
        self.response_type = response_type
        self.calls = 0
        self.fail_once = False
        self.failed = False

    def estimate_cost(self, tokens):
        return 0.0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if self.fail_once and self.calls == 2 and not self.failed:
            self.failed = True
            raise OSError("injected provider outage")
        payload = json.loads(messages[-1]["content"])
        if "sources" not in payload:
            response = {
                "version": 1,
                "relations": [
                    {"target_id": target["target_id"], "relation": "distinct"}
                    for target in payload["targets"]
                ],
            }
        else:
            already = set(payload.get("already_returned", []))
            candidates = []
            for source in payload["sources"]:
                for statement in source["content"].splitlines():
                    match = re.fullmatch(r"Set (archive_partition_\d{2}) to (\d+)\.", statement)
                    if not match or statement in already:
                        continue
                    key = match[1]
                    target = next(
                        (n for n in payload["existing_notes"] if n["logical_key"] == key), None
                    )
                    candidate = dict(
                        action="add",
                        logical_key=key,
                        category="fact",
                        title=key,
                        statement=statement,
                        scope="project",
                        observed_at=source["observed_at"][:10],
                        effective_at=None,
                        evidence=[{"source_id": source["id"], "quote": statement}],
                    )
                    if target and target["statement"] != statement:
                        candidate.update(
                            action="update",
                            note_id=target["note_id"],
                            expected_revision=target["revision"],
                            correction={
                                "previous_statement": target["statement"],
                                "current_statement": statement,
                                "current_evidence": candidate["evidence"],
                            },
                        )
                    candidates.append(candidate)
            response = {"version": 1, "candidates": candidates[:12]}
        return self.response_type(json.dumps(response), 50)


class FixtureEmbedder:
    remote = False
    backend = "fixture"
    model = "fixed-bow-fixture-v1"
    model_id = "fixture:fixed-bow-fixture-v1"

    def load(self):
        pass

    def vector(self, text):
        result = [0.0] * 16
        for word in text.lower().split():
            result[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 16] += 1
        return result if any(result) else [1.0] + [0.0] * 15

    def embed_documents(self, texts):
        return [self.vector(text) for text in texts]

    def embed_query(self, text):
        return self.vector(text)


def batch(text, number=1):
    return dict(
        batch_id=f"heldout-{number}",
        namespace="heldout",
        root="heldout-root",
        session_id=f"learning-session-{number}",
        source_client="codex",
        records=[
            dict(
                source_id=f"heldout-source-{number}",
                record_id=f"heldout-record-{number}",
                role="user",
                content=text,
                source_hash=hashlib.sha256(text.encode()).hexdigest(),
                observed_at="2026-01-02T09:00:00Z" if number == 1 else "2026-02-02T09:00:00Z",
                metadata={"direct_user": True},
            )
        ],
    )


def child(source, memory_on, corpus_path):
    # Set before importing the product: one interpreter uses exactly one source.
    sys.path.insert(0, str(source / "src"))
    from noblivion import __version__, config, db, embedding, store
    from noblivion.learning import BudgetLimits, LearningLedger
    from noblivion.learning_worker import LearningWorker, ProviderResponse, WorkerSettings

    corpus = json.loads(corpus_path.read_text())
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="noblivion-heldout-") as raw:
        root = Path(raw)
        data = root / "data"
        data.mkdir(mode=0o700)
        memory = root / "heldout-root/memory"
        memory.mkdir(parents=True)
        stamp = config.install_stamp(data)
        stamp.parent.mkdir()
        stamp.write_text(json.dumps({"version": __version__}))
        settings = config.Settings(
            data_dir=data,
            namespace="heldout",
            memory_dirs=(memory,),
            delete_grace_days=14,
            archive_retention_days=90,
        )
        service = embedding.EmbeddingService(
            embedding.EmbeddingSettings(backend="fastembed", model=FixtureEmbedder.model),
            factory=lambda *args: FixtureEmbedder(),
        )
        st = store.Store(
            settings,
            config.StoreSettings(port=0, idle_exit_s=0, index_interval_s=0),
            embedding_service=service,
            labeller=None,
        )
        st.open()
        thread = threading.Thread(target=st.serve, daemon=True)
        thread.start()
        provider = FixedProvider(ProviderResponse)
        limits = BudgetLimits(max_calls=256, max_tokens=5000000, max_seconds=10000)
        worker_settings = WorkerSettings()
        result = {}
        try:
            with closing(db.open_db(settings.db_path)) as conn:
                ledger = LearningLedger(conn)
                captured = ledger.capture_batch(
                    batch("\n".join(f["statement"] for f in corpus["facts"]))
                )
                result["dense"] = LearningWorker(
                    ledger, provider, worker_settings, limits
                ).run_once()
                ledger.capture_batch(batch(corpus["correction"]["statement"], 2))
                result["correction"] = LearningWorker(
                    ledger, provider, worker_settings, limits
                ).run_once()
                if not memory_on:
                    ledger.set_controls("heldout", "heldout-root", "task-session", use=False)
                ledger.project_pending("heldout", "heldout-root", memory)
                assert st.scan_once(conn) is not None
                ledger.acknowledge_index("heldout", "heldout-root")
                service.start(conn)
                st.backfill_once(conn)
                notes = ledger.current_notes("heldout", "heldout-root")
                valid = {f["statement"] for f in corpus["facts"]}
                valid.add(corpus["correction"]["statement"])
                result["retained_particulars"] = sum(n["statement"] in valid for n in notes)
                result["expected_particulars"] = len(corpus["facts"])
                result["unsupported_claims"] = sum(n["statement"] not in valid for n in notes)
                result["budget"] = dict(
                    conn.execute(
                        "SELECT count(*) calls,sum(tokens) tokens,"
                        "sum(seconds) seconds,sum(cost) cost "
                        "FROM learning_budget"
                    ).fetchone()
                )
                result["provider_reported_fixture_tokens"] = provider.calls * 50
                result["budget_measurement"] = (
                    "durable conservative reservations, not tokenizer or latency measurements"
                )
                result["coverage"] = (
                    [
                        dict(r)
                        for r in conn.execute(
                            "SELECT unit_index,continuation,status,reason "
                            "FROM learning_extraction_pages "
                            "WHERE job_id=? ORDER BY unit_index,continuation",
                            (captured["job_id"],),
                        )
                    ]
                    if (source / "src/noblivion/learning_pages.py").exists()
                    else None
                )
            env = dict(os.environ)
            env.update(
                NOBLIVION_DATA_DIR=str(data),
                NOBLIVION_RECALL_PROJECT="heldout",
                NOBLIVION_RECALL_ROOT="heldout-root",
                NOBLIVION_RECALL_MEMORY_DIR=str(memory),
                NOBLIVION_CONFIG=str(root / "absent-config.json"),
                NOBLIVION_STORE_AUTOSTART="0",
                CLAUDE_CONFIG_DIR=str(root / "isolated-claude"),
                NOBLIVION_RECALL_CACHE_DIR=str(root / "recall-cache"),
            )
            new_hook = source / "hooks/learning_context.py"
            summary_hook = load_hook(source / "hooks/learning_summary.py")
            query_hook = load_hook(new_hook) if new_hook.exists() else None
            recall_hook = load_hook(source / "hooks/recall_hook.py")
            startup_answer = summary_hook.run(
                dict(
                    hook_event_name="SessionStart",
                    session_id="task-session",
                    source="startup",
                    cwd=str(root),
                ),
                env,
                codex=True,
            )
            startup_context = startup_answer.get("hookSpecificOutput", {}).get(
                "additionalContext", ""
            )

            def session_context(query):
                event = dict(
                    hook_event_name="UserPromptSubmit",
                    session_id="task-session",
                    prompt=query,
                    cwd=str(root),
                )
                query_answer = query_hook.run(event, env, codex=True) if query_hook else {}
                query_context = query_answer.get("hookSpecificOutput", {}).get(
                    "additionalContext", ""
                )
                stream = io.StringIO()
                recall_hook.run(json.dumps(event), stream, dict(env))
                raw_recall = stream.getvalue().strip()
                try:
                    recall_answer = json.loads(raw_recall) if raw_recall else {}
                    recall_context = recall_answer.get("hookSpecificOutput", {}).get(
                        "additionalContext", ""
                    )
                except ValueError:
                    # The native adapter treats plain prompt-hook output as context.
                    recall_context = raw_recall
                context = "\n".join(
                    part for part in (startup_context, query_context, recall_context) if part
                )
                return context, query_context, recall_context

            tasks = []
            for index, task in enumerate(corpus["tasks"]):
                context, query_context, recall_context = session_context(task["query"])
                context_path, output_path = (
                    root / f"context-{index}.txt",
                    root / f"task-{index}.json",
                )
                context_path.write_text(context)
                subprocess.run(
                    [
                        sys.executable,
                        str(EXECUTOR),
                        task["key"],
                        str(context_path),
                        str(output_path),
                    ],
                    check=True,
                )
                actual = json.loads(output_path.read_text())
                expected_statement = f"Set {task['key']} to {task['expected']}."
                tasks.append(
                    dict(
                        key=task["key"],
                        expected=task["expected"],
                        actual=actual,
                        success=actual == {task["key"]: task["expected"]},
                        useful_recall=expected_statement in context,
                        context_chars=len(context),
                        startup_context_chars=len(startup_context),
                        query_context_chars=len(query_context),
                        existing_prompt_recall_chars=len(recall_context),
                        context_sha256=hashlib.sha256(context.encode()).hexdigest(),
                        stale_guidance=corpus["facts"][2]["statement"] in context,
                    )
                )
            negative_queries = []
            for query in corpus["negative_queries"]:
                context, query_context, recall_context = session_context(query)
                negative_queries.append(
                    dict(
                        query=query,
                        full_session_empty=not context,
                        new_query_hook_empty=(not query_context) if query_hook else None,
                        existing_prompt_recall_empty=not recall_context,
                        startup_context_chars=len(startup_context),
                    )
                )
            result["negative_queries"] = negative_queries
            result["zero_relevance_empty"] = sum(n["full_session_empty"] for n in negative_queries)
            result["query_hook_zero_relevance_empty"] = (
                sum(n["new_query_hook_empty"] for n in negative_queries) if query_hook else None
            )
            result["zero_relevance_count"] = len(negative_queries)
            result.update(
                tasks=tasks,
                tasks_completed=sum(t["success"] for t in tasks),
                task_count=len(tasks),
                useful_recall=sum(t["useful_recall"] for t in tasks),
                stale_guidance=sum(t["stale_guidance"] for t in tasks),
                memory_on=memory_on,
                hook_paths=["hooks/learning_summary.py", "hooks/recall_hook.py"]
                + (["hooks/learning_context.py"] if query_hook else []),
                context_composition="startup summary + new query context + existing prompt recall",
            )
        finally:
            st.request_stop("heldout-eval")
            thread.join(15)
        # Recovery uses a fresh persistent DB and a fresh ledger/worker connection.
        recovery_path = root / "recovery.db"
        provider = FixedProvider(ProviderResponse)
        provider.fail_once = True
        with closing(db.open_db(recovery_path, create=True)) as conn:
            ledger = LearningLedger(conn)
            ledger.capture_batch(batch("\n".join(f["statement"] for f in corpus["facts"])))
            first = LearningWorker(ledger, provider, worker_settings, limits).run_once()
            conn.execute("UPDATE learning_jobs SET next_attempt=0")
        with closing(db.open_db(recovery_path)) as conn:
            ledger = LearningLedger(conn)
            resumed = LearningWorker(ledger, provider, worker_settings, limits).run_once()
            actual = ledger.current_notes("heldout", "heldout-root")
            result["recovery"] = dict(
                first=first["status"],
                resumed=resumed["status"],
                retained_particulars=len(actual),
                duplicates=len(actual) - len({n["logical_key"] for n in actual}),
                all_labels_recovered=len(actual) == len(corpus["facts"]),
            )
        result["elapsed_seconds"] = time.monotonic() - started
    return result


def compare(baseline_source, changed_source, output, corpus_path=CORPUS):
    baseline_files_verified = verify_baseline(baseline_source, changed_source)
    results = {}
    for name, source in (("baseline", baseline_source), ("changed", changed_source)):
        for memory in (True, False):
            run = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    "--source",
                    str(source),
                    "--corpus",
                    str(corpus_path),
                    "--memory",
                    "on" if memory else "off",
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            results[f"{name}_memory_{'on' if memory else 'off'}"] = json.loads(run.stdout)
    baseline, changed = results["baseline_memory_on"], results["changed_memory_on"]
    report = dict(
        version=1,
        corpus_sha256=digest(corpus_path),
        executor_sha256=digest(EXECUTOR),
        harness_sha256=digest(__file__),
        baseline_revision=BASELINE_SHA,
        baseline_files_verified=baseline_files_verified,
        required_baseline_revision=BASELINE_SHA,
        changed_revision=git_revision(changed_source),
        revision_note="Digests bind tested source; revision can be the uncommitted worktree base.",
        sources={
            name: {
                file: digest(source / file)
                for file in (
                    "src/noblivion/learning.py",
                    "src/noblivion/learning_worker.py",
                    "src/noblivion/learning_pages.py",
                    "src/noblivion/learning_consolidation.py",
                    "src/noblivion/learning_synthesis.py",
                    "src/noblivion/learning_runtime.py",
                    "src/noblivion/rest.py",
                    "hooks/learning_summary.py",
                    "hooks/learning_context.py",
                    "codex/adapter.py",
                    "hooks/recall_hook.py",
                    "src/noblivion/store.py",
                    "src/noblivion/indexer.py",
                    "src/noblivion/embedding.py",
                    "src/noblivion/ranking.py",
                )
                if (source / file).is_file()
            }
            for name, source in (("baseline", baseline_source), ("changed", changed_source))
        },
        fixed_provider=json.loads(corpus_path.read_text())["provider"],
        results=results,
        work_factor=(
            baseline["budget"]["calls"] / changed["budget"]["calls"]
            if changed["budget"]["calls"]
            else None
        ),
        work_factor_definition=(
            "baseline accounted calls / changed accounted calls; contract workload only"
        ),
        price_factor=None,
        price_factor_reason="both fixture providers are free; zero denominator",
        real_user_outcome_effect=None,
        real_user_outcome_effect_reason="no authorized labeled real-user causal corpus",
        synthesis_accuracy=None,
        synthesis_accuracy_reason="extractive scripted fixture does not label synthesis",
        limitations=json.loads(corpus_path.read_text())["limitations"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--changed-source", type=Path, default=HERE.parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--memory", choices=("on", "off"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.source.resolve(), args.memory == "on", args.corpus)))
        return 0
    if args.baseline_source is None or args.output is None:
        parser.error("--baseline-source and --output are required")
    report = compare(
        args.baseline_source.resolve(), args.changed_source.resolve(), args.output, args.corpus
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "corpus_sha256": report["corpus_sha256"],
                "outcomes": {
                    k: f"{v['tasks_completed']}/{v['task_count']}"
                    for k, v in report["results"].items()
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
