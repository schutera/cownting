"""M5 scoring contract: resumability, the write, and the pixels it scores on.

Builds a real 2-frame corpus with real JPEGs and runs a real (randomly
initialised) checkpoint over it, so the whole path is exercised — crop render,
batch, softmax, UPDATE — rather than mocked. Predictions from random weights are
meaningless and nothing here asserts on their VALUE; what is asserted is that
every row gets one, that the rev is recorded, and that a second pass is free.

The two failures this guards are both silent:

  - A NON-RESUMABLE pass. On a CPU-only box the corpus walk is long and yields
    to uploads, so it WILL be interrupted. If `pending_frames` does not narrow,
    every resume restarts from zero and the pass never finishes.
  - SCORING ON DIFFERENT PIXELS THAN TRAINING. The checkpoint carries the crop
    geometry it was trained with; if a deployment's crop_pad has since changed,
    accuracy quietly drops and no number anywhere would say so.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_infer
    py -3.11 tests/test_instance_attrs_infer.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PIL import Image  # noqa: E402

from cownting import db  # noqa: E402
from cownting.config import AnnotationCfg, Config, InstanceAttrsCfg, PathsCfg  # noqa: E402
from cownting.instance_attrs import infer as I  # noqa: E402
from cownting.instance_attrs import model as M  # noqa: E402

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


HEADS = {"cow": ["not_cow", "cow"],
         "sun_exposure": ["sun_exposure.shaded", "sun_exposure.direct_sun"],
         "behaviour": ["behaviour.feeding", "behaviour.lying"]}

_FRAMES = 2
_PER_FRAME = 3


def _fixture(d: str) -> tuple[Config, str]:
    """A 2-frame, 6-detection corpus with real JPEGs, plus a real checkpoint."""
    art = os.path.join(d, "artifacts")
    fdir = os.path.join(art, "2025-07-03", "frames", "camera_01")
    os.makedirs(fdir, exist_ok=True)
    rng = np.random.default_rng(0)
    rows = []
    for i in range(_FRAMES):
        name = f"{i:08d}.jpg"
        p = os.path.join(fdir, name)
        Image.fromarray(rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)).save(p)
        for k in range(_PER_FRAME):
            rows.append({
                "dataset_id": "2025-07-03", "camera_id": "camera_01",
                "ts": pd.Timestamp("2025-07-03 10:00:00") + pd.Timedelta(minutes=i),
                "time_bin": i, "frame_path": p, "score": 0.9,
                "bbox_x1": 10.0 + 40 * k, "bbox_y1": 20.0,
                "bbox_x2": 90.0 + 40 * k, "bbox_y2": 160.0,
                "area_px": 100.0, "posture": "standing",
            })
    cfg = Config(
        cameras=[],
        annotation=AnnotationCfg(crop_pad=0.35, crop_max_width=768,
                                 mask_timestamp_banner=True),
        # Abstention OFF here: random weights never clear a 0.6 confidence, so
        # with the default rule every behaviour class would be NULL and "every
        # column is written" could not be tested. The rule gets its own test.
        instance_attrs=InstanceAttrsCfg(weights=os.path.join(d, "m.pt"),
                                        device="cpu", cow_threshold=0.5,
                                        abstain_min_conf=0.0, abstain_nv_p=1.01),
        paths=PathsCfg(artifacts_dir=art, db_path=os.path.join(d, "cownting.duckdb")),
    )
    con = db.connect(cfg.paths.db_path)
    db.init_db(con)
    db.insert_detections(con, pd.DataFrame(rows))
    con.close()

    net = M.build_model(HEADS, pretrained=False)
    rev = M.save_checkpoint(cfg.instance_attrs.weights, model=net, heads=HEADS,
                            img_size=64, metrics={"mean_macro_f1": 0.0},
                            manifest_config={"crop_pad": 0.35, "crop_max_width": 768,
                                             "mask_timestamp_banner": True})
    return cfg, rev


def test_every_detection_gets_a_verdict_and_a_rev():
    with tempfile.TemporaryDirectory() as d:
        cfg, rev = _fixture(d)
        s = I.run_score(cfg)
        check("all frames were visited", s["frames"] == _FRAMES, str(s["frames"]))
        check("all detections were scored",
              s["scored"] == _FRAMES * _PER_FRAME, str(s["scored"]))
        check("no crop was unreadable", s["unreadable"] == 0, str(s["unreadable"]))

        con = db.connect(cfg.paths.db_path)
        row = con.execute(
            """SELECT count(*), count(attr_model_rev), count(attr_cow_p),
                      count(attr_sun), count(attr_behaviour),
                      count(*) FILTER (WHERE attr_is_cow IS NOT NULL)
               FROM detections""").fetchone()
        check("every row carries every attribute column",
              row[0] == 6 and all(v == 6 for v in row[1:]), str(row))
        revs = [r[0] for r in con.execute(
            "SELECT DISTINCT attr_model_rev FROM detections").fetchall()]
        check("...stamped with the checkpoint's own rev", revs == [rev], str(revs))
        # Probabilities must be probabilities. A missing softmax would still write
        # a plausible-looking float here and nothing downstream would notice.
        bad = con.execute(
            "SELECT count(*) FROM detections WHERE attr_cow_p < 0 OR attr_cow_p > 1"
        ).fetchone()[0]
        check("cow probability is in [0,1]", bad == 0, str(bad))
        # The predicted class must come from the checkpoint's vocabulary, not
        # from an index into some other list.
        vals = {r[0] for r in con.execute(
            "SELECT DISTINCT attr_behaviour FROM detections").fetchall()}
        check("predicted classes come from the checkpoint's vocabulary",
              vals <= set(HEADS["behaviour"]), str(vals))
        con.close()


def test_a_second_pass_is_free_and_a_partial_pass_resumes():
    with tempfile.TemporaryDirectory() as d:
        cfg, rev = _fixture(d)
        # Score ONE frame, as an interrupted pass would.
        first = I.run_score(cfg, limit=1)
        check("the bounded pass stopped where it was told", first["frames"] == 1)

        con = db.connect(cfg.paths.db_path)
        left = I.pending_frames(con, rev=rev)
        check("the scored frame drops out of the pending set",
              len(left) == _FRAMES - 1, str(len(left)))
        con.close()

        second = I.run_score(cfg)
        check("resuming does the REMAINING work, not all of it",
              second["frames"] == _FRAMES - 1, str(second["frames"]))

        third = I.run_score(cfg)
        check("a fully-scored corpus is a no-op", third["frames"] == 0, str(third))

        # ...unless asked. Re-scoring is how a NEW model reaches old rows.
        again = I.run_score(cfg, rescore=True)
        check("--rescore revisits everything", again["frames"] == _FRAMES, str(again))


def test_a_different_model_rev_reclaims_every_row():
    """A column holding two models' opinions is not interpretable, so a new rev
    must not be allowed to leave half the corpus on the old one."""
    with tempfile.TemporaryDirectory() as d:
        cfg, _rev = _fixture(d)
        I.run_score(cfg)
        # A second checkpoint: same architecture, different weights -> different rev.
        net2 = M.build_model(HEADS, pretrained=False)
        rev2 = M.save_checkpoint(cfg.instance_attrs.weights, model=net2, heads=HEADS,
                                 img_size=64, metrics={}, manifest_config={})
        con = db.connect(cfg.paths.db_path)
        left = I.pending_frames(con, rev=rev2)
        con.close()
        check("every frame is pending again under the new rev",
              len(left) == _FRAMES, str(len(left)))
        s = I.run_score(cfg)
        check("...and the new model reaches all of them", s["frames"] == _FRAMES, str(s))


def test_the_gate_threshold_decides_the_flag_not_the_argmax():
    """attr_is_cow is a THRESHOLD on P(cow), not the argmax, so an operator can
    demand high confidence before dropping a real animal from the counts."""
    with tempfile.TemporaryDirectory() as d:
        cfg, _rev = _fixture(d)
        cfg.instance_attrs.cow_threshold = 1.01     # nothing can clear it
        I.run_score(cfg)
        con = db.connect(cfg.paths.db_path)
        n_true = con.execute(
            "SELECT count(*) FROM detections WHERE attr_is_cow").fetchone()[0]
        check("an unreachable threshold flags everything not-a-cow", n_true == 0, str(n_true))
        con.close()

        cfg.instance_attrs.cow_threshold = 0.0      # everything clears it
        I.run_score(cfg, rescore=True)
        con = db.connect(cfg.paths.db_path)
        n_false = con.execute(
            "SELECT count(*) FROM detections WHERE attr_is_cow = false").fetchone()[0]
        check("a zero threshold flags nothing not-a-cow", n_false == 0, str(n_false))
        con.close()


def test_a_crop_geometry_change_is_reported_not_swallowed():
    with tempfile.TemporaryDirectory() as d:
        cfg, _rev = _fixture(d)
        clean = I.run_score(cfg)
        check("matching geometry reports no mismatch",
              clean["geometry_mismatch"] == {}, str(clean["geometry_mismatch"]))

        cfg.annotation.crop_pad = 0.60              # re-tuned since training
        s = I.run_score(cfg, rescore=True)
        check("a changed crop_pad is surfaced",
              "crop_pad" in s["geometry_mismatch"], str(s["geometry_mismatch"]))
        check("...with both values, so the operator can see which way",
              s["geometry_mismatch"]["crop_pad"] == {"trained": 0.35, "serving": 0.60},
              str(s["geometry_mismatch"]))


def test_abstention_leaves_a_rev_and_a_probability_but_no_class():
    """An abstained row must read as 'unsure', not 'unscored': the rev says the
    model looked, the probability says how close it came, and only the class is
    withheld. Random weights sit well under any real confidence threshold, so
    the default rule abstains on every row — which is exactly the case to pin."""
    with tempfile.TemporaryDirectory() as d:
        cfg, rev = _fixture(d)
        cfg.instance_attrs.abstain_min_conf = 0.99       # nothing clears it
        cfg.instance_attrs.abstain_nv_p = 1.01
        s = I.run_score(cfg)
        check("the summary counts the abstentions", s["abstained"] == 6, str(s["abstained"]))
        con = db.connect(cfg.paths.db_path)
        row = con.execute(
            """SELECT count(*), count(attr_behaviour), count(attr_behaviour_p),
                      count(attr_model_rev), count(attr_sun)
               FROM detections""").fetchone()
        check("no behaviour class is written", row[1] == 0, str(row))
        check("...but every row keeps its probability and its rev — 'unsure', not 'unscored'",
              row[2] == 6 and row[3] == 6, str(row))
        check("the sun head is untouched by the behaviour rule", row[4] == 6, str(row))
        cov = I.coverage(con)
        check("coverage reports the unsure count", cov["behaviour_unsure"] == 6, str(cov))
        con.close()

        cfg.instance_attrs.abstain_min_conf = 0.0        # everything clears it
        I.run_score(cfg, rescore=True)
        con = db.connect(cfg.paths.db_path)
        n = con.execute("SELECT count(attr_behaviour) FROM detections").fetchone()[0]
        check("with the rule off every row gets a class", n == 6, str(n))
        con.close()


def test_missing_weights_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        cfg, _rev = _fixture(d)
        os.unlink(cfg.instance_attrs.weights)
        try:
            I.run_score(cfg)
            check("scoring without a checkpoint raises", False, "returned instead")
        except FileNotFoundError as e:
            check("scoring without a checkpoint raises", "attrs train" in str(e), str(e))


def test_coverage_reports_what_the_gate_is_doing():
    with tempfile.TemporaryDirectory() as d:
        cfg, _rev = _fixture(d)
        con = db.connect(cfg.paths.db_path)
        c0 = I.coverage(con)
        check("an unscored corpus reports zero scored",
              c0["detections"] == 6 and c0["scored"] == 0, str(c0))
        con.close()

        cfg.instance_attrs.cow_threshold = 1.01
        I.run_score(cfg)
        con = db.connect(cfg.paths.db_path)
        c1 = I.coverage(con)
        check("...and afterwards reports the full corpus scored",
              c1["scored"] == 6 and c1["model_revs"] == 1, str(c1))
        check("...and how many the gate is hiding", c1["not_cow"] == 6, str(c1))
        con.close()


def main():
    print("=== test_instance_attrs_infer ===")
    test_every_detection_gets_a_verdict_and_a_rev()
    test_a_second_pass_is_free_and_a_partial_pass_resumes()
    test_a_different_model_rev_reclaims_every_row()
    test_the_gate_threshold_decides_the_flag_not_the_argmax()
    test_a_crop_geometry_change_is_reported_not_swallowed()
    test_abstention_leaves_a_rev_and_a_probability_but_no_class()
    test_missing_weights_fail_loudly()
    test_coverage_reports_what_the_gate_is_doing()
    print("=================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
