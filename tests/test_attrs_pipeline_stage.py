"""Scoring is a PIPELINE STAGE, and it must never be able to fail an upload.

Segmentation writes its results into `detections` as part of processing a day.
Scoring now does the same, so a day cannot arrive segmented-but-unscored while
every other day has its attribute columns filled. `POST /api/attrs/score` is the
backfill for footage that predates the model — the exception, exactly as
`/api/remask` is for outlines, not the normal path.

What is pinned here is the stage's REFUSAL TO BE FATAL, because both of its
failure routes are ordinary rather than exotic:

  - no checkpoint at all. The model is optional; a deployment that has never
    trained one still has to be able to ingest footage.
  - scoring raising on its own (a corrupt checkpoint, a torch that will not
    load). By the time this stage runs, the day's frames, detections and areas
    are already written — refusing the whole upload over a regenerable attribute
    column would trade something valuable for something cheap.

Both must land as a WARNING on the job, the same channel camera-health already
uses for "this arrived, but look at it".

No pytest. Run either way:
    py -3.11 -m tests.test_attrs_pipeline_stage
    py -3.11 tests/test_attrs_pipeline_stage.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import uploads  # noqa: E402
from cownting.config import Config, InstanceAttrsCfg, PathsCfg  # noqa: E402

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


def _cfg(weights: str) -> Config:
    return Config(cameras=[],
                  instance_attrs=InstanceAttrsCfg(weights=weights, device="cpu"),
                  paths=PathsCfg())


def _job() -> uploads.Job:
    return uploads.Job(job_id="t", dataset_id="d1", label="test", kind="upload")


def test_the_stage_is_wired_into_both_processing_paths():
    """Not a style check. If a path forgets the stage, days processed through it
    are silently unscored — which looks exactly like a day nobody has got to
    yet, and only shows up as a hole in a dashboard months later."""
    import inspect

    whole = inspect.getsource(uploads._run)
    added = inspect.getsource(uploads._run_add_camera)
    check("the whole-day upload scores what it segmented",
          "_score_stage(" in whole)
    check("adding a camera scores it too", "_score_stage(" in added)
    # ...and scoped to that camera, or adding one camera re-walks the whole day.
    check("...scoped to the camera just added, not the whole day",
          "camera_id=camera_id" in added.split("_score_stage(")[1][:200],
          added.split("_score_stage(")[1][:200])
    check("'scoring' is a declared stage in the job vocabulary",
          "scoring" in inspect.getsource(uploads.Job))


def test_no_checkpoint_skips_silently():
    """The model is optional. A box that has never trained one must still ingest."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(os.path.join(d, "absent.pt"))
        job = _job()
        warnings = uploads._score_stage(job, cfg, dataset_id="d1", camera_id=None,
                                        base_progress=0.92)
        check("a missing checkpoint produces no warning at all", warnings == [], str(warnings))
        check("...and does not move the job into the scoring stage",
              job.stage != "scoring", job.stage)


def test_a_broken_checkpoint_warns_instead_of_failing_the_upload():
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "broken.pt")
        with open(bad, "wb") as f:
            f.write(b"this is not a torch checkpoint")
        cfg = _cfg(bad)
        job = _job()
        warnings = uploads._score_stage(job, cfg, dataset_id="d1", camera_id=None,
                                        base_progress=0.92)
        check("a corrupt checkpoint yields exactly one warning",
              len(warnings) == 1, str(warnings))
        check("...that names the recovery route", "attrs/score" in warnings[0], warnings[0])
        check("...and says the day itself is fine",
              "complete" in warnings[0], warnings[0])
        check("the job is NOT failed by it", job.status != "failed", job.status)


def main():
    print("=== test_attrs_pipeline_stage ===")
    test_the_stage_is_wired_into_both_processing_paths()
    test_no_checkpoint_skips_silently()
    test_a_broken_checkpoint_warns_instead_of_failing_the_upload()
    print("=================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
