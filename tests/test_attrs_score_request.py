"""The shell-side scoring trigger: a request file the ticker consumes.

The HTTP route is poweruser-gated; a shell on the box is at least as trusted,
and the ticker thread is the only in-process hook a shell can reach. What must
hold: an absent file does nothing; a present file enqueues exactly one job and
is deleted BEFORE the enqueue (so a job that raises cannot re-fire forever); a
malformed body still fires (scoring everything) rather than silently doing
nothing; and a scoped body scopes the job.

No pytest. Run either way:
    py -3.11 -m tests.test_attrs_score_request
    py -3.11 tests/test_attrs_score_request.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import labels_backup as LB  # noqa: E402
from cownting import uploads  # noqa: E402
from cownting.config import Config, PathsCfg  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


class _Captured:
    """Stand-in for uploads.start_attrs_score_job: records calls, enqueues nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, config, dataset_id=None, camera_id=None, limit=None, rescore=False):
        self.calls.append({"dataset": dataset_id, "camera": camera_id, "rescore": rescore})
        return uploads.Job(job_id=f"j{len(self.calls)}", dataset_id=dataset_id or "",
                           label="t", kind="attrs_score")


def _cfg(d: str) -> Config:
    return Config(cameras=[], paths=PathsCfg(db_path=os.path.join(d, "cownting.duckdb")))


def test_request_file_enqueues_once_and_is_consumed():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        cap = _Captured()
        real = uploads.start_attrs_score_job
        uploads.start_attrs_score_job = cap
        try:
            check("no file, no job", LB.check_score_request(cfg) is None and cap.calls == [])
            req = os.path.join(d, LB.SCORE_REQUEST)
            open(req, "w").close()                       # empty = everything
            out = LB.check_score_request(cfg)
            check("an empty request file enqueues one unscoped job",
                  out is not None and cap.calls == [{"dataset": None, "camera": None, "rescore": False}],
                  str(cap.calls))
            check("the file is consumed", not os.path.exists(req))
            check("a second tick does nothing", LB.check_score_request(cfg) is None and len(cap.calls) == 1)
        finally:
            uploads.start_attrs_score_job = real


def test_scoped_and_malformed_bodies():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        cap = _Captured()
        real = uploads.start_attrs_score_job
        uploads.start_attrs_score_job = cap
        try:
            req = os.path.join(d, LB.SCORE_REQUEST)
            with open(req, "w") as f:
                json.dump({"dataset": "2025-07-03", "rescore": True}, f)
            LB.check_score_request(cfg)
            check("a JSON body scopes the job",
                  cap.calls[-1] == {"dataset": "2025-07-03", "camera": None, "rescore": True},
                  str(cap.calls[-1]))
            with open(req, "w") as f:
                f.write("{not json")
            LB.check_score_request(cfg)
            check("a malformed body still fires, unscoped, rather than silently nothing",
                  cap.calls[-1] == {"dataset": None, "camera": None, "rescore": False},
                  str(cap.calls[-1]))
            check("...and is consumed too", not os.path.exists(req))
        finally:
            uploads.start_attrs_score_job = real


def test_ticker_calls_it_after_the_milestone():
    import inspect

    src = inspect.getsource(LB._ticker)
    check("the ticker checks for a request every tick", "check_score_request(" in src)
    check("...and never lets it kill the thread",
          src.index("check_score_request(") > src.index("try:"))


def main():
    print("=== test_attrs_score_request ===")
    test_request_file_enqueues_once_and_is_consumed()
    test_scoped_and_malformed_bodies()
    test_ticker_calls_it_after_the_milestone()
    print("================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
