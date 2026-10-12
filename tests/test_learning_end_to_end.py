# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real hook spool, listener proof, HTTP, worker, projection and recall seams.

Only the model is substituted: its local HTTP contract is exercised. Client
UI behavior and live-model accuracy require separate installation evidence.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from noblivion.learning import LearningLedger
from test_learning_capture import capture
from test_store import Running, make_store, wait_for


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sources = json.loads(body["messages"][-1]["content"])["sources"]
        source = next(s for s in sources if s["role"] == "user")
        self.server.calls.append(body)
        candidate = {
            "action": "add",
            "logical_key": "answer-style",
            "category": "preference",
            "title": "Brief answer preference",
            "statement": source["content"],
            "scope": "general",
            "observed_at": source["observed_at"][:10],
            "effective_at": None,
            "evidence": [{"source_id": source["id"], "quote": source["content"]}],
        }
        data = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps({"version": 1, "candidates": [candidate]})
                        },
                    }
                ],
                "usage": {"total_tokens": 200},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_capture_to_background_memory_to_independent_recall(tmp_path):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model.calls = []
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    st = make_store(tmp_path)
    folder = st.settings.memory_dirs[0]
    namespace, root = st.namespace, folder.parent.name
    settings = {
        "enabled": True,
        "scopes": [{"project": namespace, "root": root, "memory_dir": str(folder)}],
        "provider": {
            "endpoint": f"http://127.0.0.1:{model.server_port}/v1",
            "model": "fixture-model",
            "local": True,
            "cost_per_million_tokens": 0,
        },
    }
    (st.data_dir / "learning.json").write_text(json.dumps(settings))
    run = Running(st)
    client = {
        "repo_root": str(Path(__file__).resolve().parents[1]),
        "data_dir": str(st.data_dir),
        "state_dir": str(tmp_path / "client"),
        "namespace": namespace,
        "recall_root": root,
        "learning": {"contribute": True, "use": True},
        "project_mappings": [
            {"workspace": str(tmp_path / "project"), "namespace": namespace, "root": root}
        ],
    }
    statement = "For every project, I prefer brief answers with one concrete example."
    transcript = tmp_path / "native.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-02T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": statement}],
                },
            }
        )
        + "\n"
    )
    event = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "source-session",
        "cwd": str(tmp_path / "project"),
        "transcript_path": str(transcript),
    }
    try:
        captured = capture.capture_hook(client, event)
        assert captured["chunks"] == 1
        query = (
            f"/api/memories/search?project={namespace}&root={root}"
            + "&q=brief+answers&session_id=fresh-session"
        )

        def recalled():
            return statement in json.dumps(run.get(query)[1])

        assert wait_for(recalled, timeout=25), run.get("/learning/status")[1]
        assert len(model.calls) == 1
        summary = run.get(f"/learning/summary?project={namespace}&root={root}")[1]
        assert statement in json.dumps(summary)
        (st.data_dir / "learning-client.json").write_text(json.dumps(client))

        def hook_summary(client_kind, session_id="fresh-session", cwd=None):
            env = {"NOBLIVION_DATA_DIR": str(st.data_dir)}
            env.update(NOBLIVION_RECALL_PROJECT=namespace, NOBLIVION_RECALL_ROOT=root)
            result = subprocess.run(
                [sys.executable, str(Path(client["repo_root"]) / "hooks/learning_summary.py")]
                + (["--codex"] if client_kind == "codex" else []),
                input=json.dumps({**event, "session_id": session_id, "cwd": cwd or event["cwd"]}),
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        for kind in ("codex", "claude"):
            assert statement in hook_summary(kind)
        assert hook_summary("claude", cwd=str(tmp_path / "unmapped")) == ""
        for flags in ({"use": False}, {"sessions": {"fresh-session": {"use": False}}}):
            (st.data_dir / "learning-client.json").write_text(
                json.dumps({**client, "learning": {**client["learning"], **flags}})
            )
            assert hook_summary("claude") == ""
        (st.data_dir / "learning-client.json").write_text(json.dumps(client))
        with st.connection() as conn:
            assert len(LearningLedger(conn).current_notes(namespace, root)) == 1
            assert conn.execute("SELECT status FROM learning_outbox").fetchone()[0] == "indexed"
        # Replaying the source event must not create a second model call or note.
        assert capture.capture_hook(client, event)["chunks"] == 0
        status, _, _ = run.request(
            "POST",
            "/learning/control",
            body={
                "project": namespace,
                "root": root,
                "session_id": "fresh-session",
                "use": False,
            },
        )
        assert status == 200
        assert not recalled()
        for kind in ("codex", "claude"):
            assert hook_summary(kind) == ""
            assert statement in hook_summary(kind, "another-session")
        assert statement in json.dumps(
            run.get(query.replace("fresh-session", "another-session"))[1]
        )
    finally:
        run.stop()
        model.shutdown()
        model.server_close()
        thread.join(timeout=2)
