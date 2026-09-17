"""A backfill yields to uploads — and only to uploads. Never to itself.

The bug this pins: `_upload_waiting` read "any active job that is not a remask",
so the attribute backfill saw its own running job as the upload it should step
aside for. It broke out of `score_detections` on frame one, re-queued itself,
reloaded the checkpoint, and repeated every ~3 s for the life of the process
(found by py-spy on the production box, six minutes into "Loading the
attribute model…"). Two pins: the predicate itself, and the whole in-process
job reaching `done` when nothing else is queued — with the bug that never
happens, and the test times out.

No pytest. Run either way:
    py -3.11 -m tests.test_backfill_yield
    py -3.11 tests/test_backfill_yield.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import uploads  # noqa: E402

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


def _infer_fixture():
    """The 2-frame / 6-detection corpus + real checkpoint from the infer tests,
    loaded by path so this works with or without `tests` being a package."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "_infer_tests", os.path.join(here, "test_instance_attrs_infer.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._fixture


def _with_jobs(jobs):
    """Swap the module job map for `jobs` for the duration of a check."""
    with uploads._LOCK:
        saved = dict(uploads._JOBS)
        uploads._JOBS.clear()
        uploads._JOBS.update({j.job_id: j for j in jobs})
    return saved


def _restore(saved):
    with uploads._LOCK:
        uploads._JOBS.clear()
        uploads._JOBS.update(saved)


def test_the_predicate_means_an_upload_and_nothing_else():
    J = uploads.Job
    cases = [
        ("a running attribute backfill is not an upload",
         [J("a", "", "t", status="running", kind="attrs_score")], False),
        ("a queued attribute backfill is not an upload either",
         [J("a", "", "t", status="queued", kind="attrs_score")], False),
        ("a running remask is not an upload",
         [J("r", "", "t", status="running", kind="remask")], False),
        ("a finished upload is not waiting",
         [J("u", "2025-07-03", "t", status="done", kind="upload")], False),
        ("a queued upload IS waiting",
         [J("u", "2025-07-03", "t", status="queued", kind="upload")], True),
        ("a running upload IS waiting (the backfill was just interrupted by it)",
         [J("u", "2025-07-03", "t", status="running", kind="upload")], True),
        ("...even alongside the backfill's own job",
         [J("a", "", "t", status="running", kind="attrs_score"),
          J("u", "2025-07-03", "t", status="queued", kind="upload")], True),
    ]
    for name, jobs, want in cases:
        saved = _with_jobs(jobs)
        try:
            got = uploads._upload_waiting()
        finally:
            _restore(saved)
        check(name, got is want, f"got {got}")


def test_an_unaccompanied_backfill_runs_to_done():
    """The end-to-end pin. With the self-yield bug this job flips
    running→queued→running forever and never reaches a terminal state."""
    fixture = _infer_fixture()
    with tempfile.TemporaryDirectory() as d:
        cfg, rev = fixture(d)
        uploads.recover_jobs(cfg)             # points the store at this tmp dir
        saved = _with_jobs([])
        try:
            job = uploads.start_attrs_score_job(cfg)
            deadline = time.time() + 90
            while job.status in uploads.ACTIVE and time.time() < deadline:
                time.sleep(0.2)
            check("the job reaches a terminal state", job.status not in uploads.ACTIVE,
                  f"status={job.status} message={job.message!r}")
            check("...and it is 'done'", job.status == "done", job.error or job.message)
            check("every frame was scored in that one pass",
                  "Scored 6 detections across 2 frames" in job.message, job.message)
            check("nothing is left in the queue", uploads._QUEUE.empty())
        finally:
            _restore(saved)


def test_a_restart_tells_each_kind_of_job_the_right_thing():
    """The upload has to be sent again; the backfills only started again. The
    first production restart under a scoring pass told the operator to
    're-upload this day' for a corpus-wide job with no day."""
    import json
    with tempfile.TemporaryDirectory() as d:
        from cownting.config import Config, PathsCfg
        cfg = Config(cameras=[], paths=PathsCfg(db_path=os.path.join(d, "cownting.duckdb")))
        store = os.path.join(d, "upload_jobs.json")
        rows = [
            {"job_id": "u1", "dataset_id": "2025-07-03", "label": "t", "status": "running", "kind": "upload"},
            {"job_id": "r1", "dataset_id": "", "label": "t", "status": "queued", "kind": "remask"},
            {"job_id": "a1", "dataset_id": "", "label": "t", "status": "running", "kind": "attrs_score"},
            {"job_id": "d1", "dataset_id": "2025-07-02", "label": "t", "status": "done", "kind": "upload",
             "message": "Upload complete."},
        ]
        with open(store, "w") as f:
            json.dump(rows, f)
        saved = _with_jobs([])
        try:
            uploads.recover_jobs(cfg)
            J = uploads._JOBS
            check("every active job is failed as interrupted",
                  all(J[k].status == "failed" and J[k].error for k in ("u1", "r1", "a1")))
            check("the finished one is untouched", J["d1"].status == "done" and J["d1"].message == "Upload complete.")
            check("an upload says re-upload", "re-upload" in J["u1"].message, J["u1"].message)
            check("a remask says start the outline pass again",
                  "/api/remask" in J["r1"].message and "re-upload" not in J["r1"].message, J["r1"].message)
            check("a scoring pass says start scoring again and that it resumes",
                  "/api/attrs/score" in J["a1"].message and "resumes" in J["a1"].message
                  and "re-upload" not in J["a1"].message, J["a1"].message)
        finally:
            _restore(saved)


def main():
    test_the_predicate_means_an_upload_and_nothing_else()
    test_an_unaccompanied_backfill_runs_to_done()
    test_a_restart_tells_each_kind_of_job_the_right_thing()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
