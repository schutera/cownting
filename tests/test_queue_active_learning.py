"""The queue asks about what the model cannot answer — without lying about the herd.

Once M5 has scored the corpus, uniform sampling is no longer the best use of an
annotator's twenty seconds: the crops the model abstained on (M5 §9.3) are the
ones a label teaches something. Turning that on is easy and quietly breaks two
things, so both are pinned here.

  - IT MUST NOT UNDO THE SIZE FLOOR. The model is least confident on exactly the
    tiny crops a human cannot read either, so ordering on uncertainty BEFORE
    legibility walks straight back into the problem `min_instance_px` was built
    to fix. Legibility leads; uncertainty only breaks ties within it.
  - IT MUST NOT SILENTLY BIAS THE CORPUS. A label store selected by model
    uncertainty is not a sample of the herd, so prevalence ("how much of the day
    in shade") and the agreement statistics computed over it stop meaning what
    they say. A marked `uniform_share` is held out of the rule, and — the part
    that has to survive a year and a re-score — membership is a pure function of
    the instance key, so the unbiased slice stays recoverable from the key alone
    even after the probabilities it was selected against have been overwritten.

Also pinned: on a corpus the model has never scored, this changes NOTHING. Every
row reads as maximally uncertain, the partition is uniformly true, and the order
falls through to the same per-annotator permutation as before.

No pytest. Run either way:
    py -3.11 -m tests.test_queue_active_learning
    py -3.11 tests/test_queue_active_learning.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from cownting import db, labeling  # noqa: E402
from cownting.config import AnnotationCfg, Config, PathsCfg  # noqa: E402

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


def _cfg(d: str, **over) -> Config:
    ann = {"min_instance_px": 25, "clear_instance_px": 60, "small_share": 0.0,
           "targets_per_instance": 2, "overlap_fraction": 0.0, "skip_retire": 3,
           "uncertain_below": 0.75, "uniform_share": 0.0,
           "batch_size": 500, "max_batch_size": 500}
    ann.update(over)
    return Config(cameras=[], annotation=AnnotationCfg(**ann),
                  paths=PathsCfg(db_path=os.path.join(d, "c.duckdb"),
                                 labels_db_path=os.path.join(d, "labels.duckdb"),
                                 artifacts_dir=os.path.join(d, "artifacts")))


def _seed(cfg: Config, rows_in: list[dict]) -> None:
    """One frame per instance so each gets ordinal 0 and a distinct key.

    Each entry: {"side": px, and optionally "p": attr_behaviour_p,
    "cls": attr_behaviour, "rev": attr_model_rev}. Omit rev -> never scored.
    """
    rows = []
    for i, r in enumerate(rows_in):
        side = float(r["side"])
        rows.append(dict(
            dataset_id="d1", camera_id="cam1", ts=pd.Timestamp("2025-07-03 10:00"),
            time_bin=0, frame_path=f"/f/{i:08d}.jpg", score=0.9,
            bbox_x1=0.0, bbox_y1=0.0, bbox_x2=side, bbox_y2=side,
            area_px=side * side, posture="standing",
            attr_behaviour=r.get("cls"), attr_behaviour_p=r.get("p"),
            attr_model_rev=r.get("rev")))
    con = db.connect(cfg.paths.db_path)
    db.init_db(con)
    db.insert_detections(con, pd.DataFrame(rows))
    con.close()
    labeling.ensure_labels_db(cfg)


def _serve(cfg: Config, annotator="alice", n=500) -> list[dict]:
    con = db.connect(cfg.paths.db_path)
    try:
        return labeling.queue(con, cfg, annotator=annotator, limit=n)["items"]
    finally:
        con.close()


def _reasons(items) -> list:
    return [i.get("queue_reason") for i in items]


# The same arithmetic the SQL does, so the test knows which keys are in the
# held-out slice without reaching into the query.
def _in_uniform(instance_key: str, share: float) -> bool:
    return hashlib.md5(("uni" + instance_key).encode()).hexdigest()[:4] < \
        labeling.hex_threshold(share)


def test_an_unscored_corpus_behaves_exactly_as_before():
    """The no-op property. Nothing scored -> nothing to prefer -> the order is
    the permutation it always was, and every item says so."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg, [{"side": 100.0} for _ in range(12)])
        items = _serve(cfg)
        check("every instance is still served", len(items) == 12, str(len(items)))
        check("...and every one is marked unscored",
              set(_reasons(items)) == {"unscored"}, str(set(_reasons(items))))

        # Same pool, same annotator, with the feature's cut moved to an extreme:
        # if nothing is scored there is nothing for it to act on, so the ORDER
        # must be byte-identical.
        keys_default = [i["instance_key"] for i in items]
        cfg2 = _cfg(d, uncertain_below=0.0)
        keys_off = [i["instance_key"] for i in _serve(cfg2)]
        check("the order does not depend on the setting when nothing is scored",
              keys_default == keys_off, str(keys_default[:3]))


def test_the_model_s_abstentions_are_served_before_what_it_is_sure_of():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        # 10 confident, 10 abstained — all comfortably legible, so the size tier
        # cannot be what separates them.
        _seed(cfg,
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.95, "rev": "r1"}] * 10 +
              [{"side": 100.0, "cls": None, "p": 0.41, "rev": "r1"}] * 10)
        head = _reasons(_serve(cfg))[:10]
        check("the first batch is entirely crops the model could not call",
              set(head) == {"abstained"}, str(head))
        tail = _reasons(_serve(cfg))[10:]
        check("...and the confident ones queue behind", set(tail) == {"confident"}, str(tail))


def test_a_thinly_supported_answer_counts_as_worth_asking():
    """Not only outright abstentions: a posture shown with support under
    `uncertain_below` is a coin flip the annotator can settle."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uncertain_below=0.75)
        _seed(cfg,
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.95, "rev": "r1"}] * 6 +
              [{"side": 100.0, "cls": "behaviour.standing", "p": 0.70, "rev": "r1"}] * 6)
        head = _reasons(_serve(cfg))[:6]
        check("the thinly-supported ones lead", set(head) == {"low_confidence"}, str(head))

        # And the cut is a real cut: raise it above both and everything qualifies.
        cfg2 = _cfg(d, uncertain_below=0.99)
        check("raising the cut pulls the confident ones in too",
              set(_reasons(_serve(cfg2))) == {"low_confidence"},
              str(set(_reasons(_serve(cfg2)))))


def test_legibility_still_leads_uncertainty():
    """THE REGRESSION THAT MATTERS. The model is least sure about the smallest
    crops, which are the ones a human cannot answer either. If uncertainty were
    allowed to outrank the size tier, the queue would spend the whole session on
    them again — the exact failure `min_instance_px` exists to prevent."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg,
              # tiny and maximally uncertain — the trap
              [{"side": 30.0, "cls": None, "p": 0.30, "rev": "r1"}] * 10 +
              # large and confident — dull, but answerable
              [{"side": 200.0, "cls": "behaviour.lying", "p": 0.97, "rev": "r1"}] * 10)
        items = _serve(cfg)
        head = items[:10]
        sides = {round(max(i["bbox"][2] - i["bbox"][0], i["bbox"][3] - i["bbox"][1]))
                 for i in head}
        check("the legible crops come first even though the model is sure of them",
              sides == {200}, str(sides))
        check("...and the uncertain-but-unreadable ones queue behind",
              {round(max(i["bbox"][2] - i["bbox"][0], i["bbox"][3] - i["bbox"][1]))
               for i in items[10:]} == {30})


def test_the_uniform_slice_is_held_out_marked_and_recomputable():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uniform_share=0.5)
        # Everything is confident, so the ONLY reason anything is promoted into
        # the front tier is the held-out uniform sample.
        _seed(cfg, [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "rev": "r1"}
                    for _ in range(40)])
        items = _serve(cfg)
        uni = [i for i in items if i["queue_reason"] == "uniform"]
        check("a substantial slice is held out of the rule",
              10 <= len(uni) <= 30, f"{len(uni)} of {len(items)}")
        check("the held-out slice is served FIRST, so it cannot starve",
              _reasons(items)[:len(uni)] == ["uniform"] * len(uni), str(_reasons(items)[:5]))
        check("everything else is marked as what it is",
              set(_reasons(items[len(uni):])) == {"confident"}, str(set(_reasons(items))))

        # THE PROPERTY THAT HAS TO OUTLIVE THE PROBABILITIES: membership is a
        # pure function of the key, so an analysis run after any number of
        # re-scores can rebuild the unbiased slice with no stored column.
        recomputed = {i["instance_key"] for i in items if _in_uniform(i["instance_key"], 0.5)}
        check("the slice is recoverable from the instance key alone",
              recomputed == {i["instance_key"] for i in uni},
              f"{len(recomputed)} vs {len(uni)}")


def test_uniform_wins_the_label_when_a_row_is_both():
    """A row can be in the held-out slice AND be one the model abstained on. It
    must be recorded as `uniform`: the unbiased-sample claim is the one a later
    prevalence estimate depends on, and mislabelling it would shrink the slice
    the analyst thinks they have."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uniform_share=1.0)  # everything is in the slice
        _seed(cfg, [{"side": 100.0, "cls": None, "p": 0.2, "rev": "r1"} for _ in range(8)])
        check("every row reports the uniform reason, not the uncertainty one",
              set(_reasons(_serve(cfg))) == {"uniform"}, str(set(_reasons(_serve(cfg)))))


def test_two_annotators_still_meet_in_the_middle():
    """The tiers are stable partitions of the pool, not per-annotator
    preferences — so two annotators walk the same tiers in DIFFERENT orders and
    still pair up on the same instances. Ordering on anything annotator-specific
    (or on coverage) would have them race down one identical list."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg, [{"side": 100.0, "cls": None, "p": 0.3, "rev": "r1"} for _ in range(30)])
        a = [i["instance_key"] for i in _serve(cfg, annotator="alice")]
        b = [i["instance_key"] for i in _serve(cfg, annotator="bob")]
        check("both are offered the same pool", set(a) == set(b), f"{len(a)} vs {len(b)}")
        check("...in different orders", a != b, f"first served: {a[0][:8]} vs {b[0][:8]}")


def test_progress_still_agrees_with_what_the_queue_will_serve():
    """The ask-tier reorders; it must never REMOVE. If it filtered, the panel
    would count down toward items that never arrive."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg,
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "rev": "r1"}] * 7 +
              [{"side": 100.0, "cls": None, "p": 0.2, "rev": "r1"}] * 5 +
              [{"side": 100.0}] * 3)
        con = db.connect(cfg.paths.db_path)
        try:
            p = labeling.progress(con, cfg, annotator="alice")
        finally:
            con.close()
        check("every servable instance is still in the pool", p["pool_total"] == 15, str(p))
        check("queue and progress agree exactly", len(_serve(cfg)) == p["remaining"],
              f'{len(_serve(cfg))} vs {p["remaining"]}')


def main():
    print("=== test_queue_active_learning ===")
    test_an_unscored_corpus_behaves_exactly_as_before()
    test_the_model_s_abstentions_are_served_before_what_it_is_sure_of()
    test_a_thinly_supported_answer_counts_as_worth_asking()
    test_legibility_still_leads_uncertainty()
    test_the_uniform_slice_is_held_out_marked_and_recomputable()
    test_uniform_wins_the_label_when_a_row_is_both()
    test_two_annotators_still_meet_in_the_middle()
    test_progress_still_agrees_with_what_the_queue_will_serve()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
