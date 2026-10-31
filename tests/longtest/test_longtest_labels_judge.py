# SPDX-License-Identifier: AGPL-3.0-or-later
"""The labelled set through its loader, and the local model judge."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import longtest_path  # noqa: F401 - puts tools on the path
import pytest

from longtest import judge, labels


def make_set(path):
    """A tiny held-out set in the format of tools/eval/heldout.py."""
    h = labels.heldout()
    groups = [
        {
            "group": "g1",
            "cls": "prompt",
            "text": "t1",
            "field": "f",
            "items": [
                {"id": "i1", "note": "n1", "line": "l"},
                {"id": "i2", "note": "n2", "line": "l"},
            ],
        },
        {
            "group": "g2",
            "cls": "prompt",
            "text": "t2",
            "field": "f",
            "items": [{"id": "i3", "note": "n3", "line": "l"}],
        },
        {
            "group": "g3",
            "cls": "prompt",
            "text": "t3",
            "field": "f",
            "items": [{"id": "i4", "note": "n4", "line": "l"}],
        },
    ]
    (path / h.GROUPS).write_text("".join(json.dumps(g) + "\n" for g in groups))
    arms = {
        "g1": {"noblivion": ["n1"], "stock_s2": ["n2"]},
        "g2": {"noblivion": ["n3"], "stock_s2": []},
    }
    (path / h.KEYS["dev"]).write_text(
        json.dumps({g: {"stratum": "home", "arms": a} for g, a in arms.items()})
    )
    (path / h.KEYS["sealed"]).write_text(
        json.dumps({"g3": {"stratum": "home", "arms": {"noblivion": ["n4"], "stock_s2": []}}})
    )
    (path / h.LABELS).write_text(
        "".join(
            json.dumps({"id": i, "label": v}) + "\n"
            for i, v in (("i1", "y"), ("i2", "n"), ("i3", "n"), ("i4", "y"))
        )
    )


def test_dev_baseline_reads_the_dev_split_only(tmp_path):
    make_set(tmp_path)
    got = labels.dev_baseline(tmp_path)
    assert got["prompts"] == 2 and got["split"] == "dev"
    nob, stock = got["arms"]["noblivion"], got["arms"]["stock_s2"]
    assert nob["delivery"]["hits"] == 1 and nob["delivery"]["n"] == 1
    assert nob["false_delivery"]["hits"] == 1 and nob["false_delivery"]["n"] == 1  # g2 has no y
    assert stock["delivery"]["hits"] == 0


def test_sealed_split_is_closed_by_default(tmp_path):
    make_set(tmp_path)
    h = labels.heldout()
    with pytest.raises(h.SealedError):
        h.load_split(tmp_path, "sealed")


def test_sealed_opens_on_day_7_once_and_is_logged(tmp_path):
    make_set(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(SystemExit):
        labels.open_sealed_once(tmp_path, run, 3)
    groups, key = labels.open_sealed_once(tmp_path, run, 7)
    assert [g["group"] for g in groups] == ["g3"] and "g3" in key
    assert (run / "sealed-opened.json").exists()
    assert (tmp_path / "sealed-opened.jsonl").exists()
    with pytest.raises(SystemExit):
        labels.open_sealed_once(tmp_path, run, 7)


def test_pool_additions_and_merge():
    groups = [{"group": "g1", "items": [{"id": "i1", "note": "n1"}]}]
    pairs = labels.pool_additions(groups, {"g1": ["n9"]}, {"g1": ["n1", "n9", "n5"]}, {})
    assert pairs == [{"group": "g1", "note": "n5"}]
    assert labels.merge_labels({"a": "y"}, {"a": "n", "b": "n"}) == {"a": "y", "b": "n"}
    with pytest.raises(ValueError):
        labels.merge_labels({}, {"a": "maybe"})


# -- the judge ----------------------------------------------------------------------


class Model(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = (
            "y"
            if "needle" in body["prompt"]
            else ("a rewritten question" if "Rewrite" in body["prompt"] else "n")
        )
        data = json.dumps({"response": text}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def model():
    server = HTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield judge.LocalModel("stub", f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()


def test_model_must_be_on_loopback():
    with pytest.raises(ValueError):
        judge.LocalModel("m", "http://192.0.2.1:11434")
    with pytest.raises(ValueError):
        judge.LocalModel("m", "https://api.example.com")


def test_vote_and_generate(model):
    assert judge.vote(model.generate, "q", "the needle is here") == "y"
    assert judge.vote(model.generate, "q", "nothing") == "n"
    assert model.calls == 2


def test_paraphrases_are_frozen_once(model, tmp_path):
    out = tmp_path / "paraphrases.json"
    rows = judge.freeze_paraphrases([{"id": "q1", "prompt": "how"}], out, model.generate)
    assert rows == {"q1": "a rewritten question"} and oct(out.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(SystemExit):
        judge.freeze_paraphrases([{"id": "q1", "prompt": "how"}], out, model.generate)


def test_judge_counts_only_above_the_kappa_floor():
    assert judge.usable(list("yynnyn"), list("yynnyn"))["usable"]
    assert not judge.usable(list("yynnyn"), list("yyyyyy"))["usable"]
