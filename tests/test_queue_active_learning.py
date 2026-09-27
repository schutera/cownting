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
           "seek_share": 1.0,
           "targets_per_instance": 2, "overlap_fraction": 0.0, "skip_retire": 3,
           "uncertain_below": 0.75, "uniform_share": 0.0,
           "batch_size": 500, "max_batch_size": 500}
    ann.update(over)
    return Config(cameras=[], annotation=AnnotationCfg(**ann),
                  paths=PathsCfg(db_path=os.path.join(d, "c.duckdb"),
                                 labels_db_path=os.path.join(d, "labels.duckdb"),
                                 artifacts_dir=os.path.join(d, "artifacts")))


def _cfg_ann(**over) -> AnnotationCfg:
    """Just the annotation config, for tests that call `_scan_sql` directly."""
    base = {"min_instance_px": 25, "clear_instance_px": 60, "small_share": 0.15,
            "targets_per_instance": 2, "overlap_fraction": 0.0, "skip_retire": 3,
            "uncertain_below": 0.75, "uniform_share": 0.25}
    base.update(over)
    return AnnotationCfg(**base)


def _seed(cfg: Config, rows_in: list[dict]) -> None:
    """One frame per instance so each gets ordinal 0 and a distinct key.

    Each entry: {"side": px, and optionally "p": attr_behaviour_p,
    "cls": attr_behaviour, "rev": attr_model_rev, "cow_p": attr_cow_p}.
    Omit rev -> never scored.
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
            attr_cow_p=r.get("cow_p"), attr_model_rev=r.get("rev"),
            under_panel=r.get("under_panel")))
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


def test_the_bound_parameters_line_up_with_the_placeholders():
    """THE SILENT-BIAS GUARD.

    `_scan_sql` interpolates positional `?` and appends the matching values to a
    list by hand, in two separate places. If the order ever drifts, the query
    STILL RUNS AND STILL RETURNS ROWS — just the wrong ones, selected against the
    wrong thresholds. Nothing raises, nothing looks broken, and the damage shows
    up months later as a training set quietly skewed toward whatever the
    mis-bound comparison happened to favour. A count check is the cheap half; the
    type sequence below is what catches two same-typed values being swapped."""
    # Every knob distinct, so a swap of any two is visible. Asserting TYPES
    # would not do: `cow_threshold` and `gate_band` are adjacent floats, and
    # exchanging them type-checks perfectly while silently selecting on a band
    # of 0.5 around a threshold of 0.15 — i.e. almost every row, forever.
    cfg = _cfg_ann(uncertain_below=0.75, uniform_share=0.25, gate_band=0.15,
                   clear_instance_px=60, min_instance_px=25, small_share=0.10,
                   overlap_fraction=0.30, seek_share=0.20, abstain_min_px=45,
                   seek_classes=["behaviour.standing"], targets_per_instance=2,
                   overlap_targets=3)
    sql, params = labeling._scan_sql(cfg, "lbl", annotator="alice", dataset=None,
                                     camera=None, day=None, cow_threshold=0.55)
    check("every placeholder has exactly one bound value",
          sql.count("?") == len(params), f"{sql.count('?')} ? vs {len(params)} params")
    hx = labeling.hex_threshold
    expected = [
        cfg.clear_instance_px, hx(cfg.small_share),          # the `clear` tier
        hx(cfg.uniform_share),                               # worth_asking: uniform passes free
        cfg.abstain_min_px, hx(cfg.small_share),             # worth_asking: the legibility gate
        cfg.uncertain_below,                                 #   selector 1: weak posture
        0.55, cfg.gate_band,                                 #   selector 2: gate band
        ["behaviour.standing"], hx(cfg.seek_share),          #   selector 3: sought class, capped
        cfg.under_panel_first,                               #   selector 4: under a panel
        cfg.under_panel_first,                               # under_panel_now: flag...
        cfg.abstain_min_px, hx(cfg.small_share),             #   ...and its own legibility gate
        hx(cfg.uniform_share),                               # reason: uniform (tested first)
        cfg.under_panel_first,                               # reason: under_panel flag...
        cfg.abstain_min_px, hx(cfg.small_share),             #   ...and its floor again
        cfg.abstain_min_px, hx(cfg.small_share),             # reason: the shared legibility gate
        0.55, cfg.gate_band,                                 # reason: gate_boundary
        cfg.uncertain_below,                                 # reason: low_confidence
        ["behaviour.standing"], hx(cfg.seek_share),          # reason: seek_class
        hx(cfg.overlap_fraction), hx(cfg.overlap_fraction),  # deep-overlap slice
        cfg.overlap_targets, cfg.targets_per_instance,
        "alice", "alice", cfg.min_instance_px,
    ]
    check("the bound values arrive in the documented order",
          params == expected,
          next((f"index {i}: {params[i]!r} != {expected[i]!r}"
                for i in range(min(len(params), len(expected))) if params[i] != expected[i]),
               f"lengths {len(params)} vs {len(expected)}"))
    # And the scope filters must stay AHEAD of all of it.
    _, scoped = labeling._scan_sql(cfg, "lbl", annotator="alice", dataset="d1",
                                   camera="cam1", day=None, cow_threshold=0.5)
    check("a scoped call binds its filters first and still balances",
          scoped[:2] == ["d1", "cam1"] and len(scoped) == len(params) + 2, str(scoped[:3]))


def test_a_crop_astride_the_gate_threshold_is_asked_about():
    """The gate decides whether an animal is counted at all, and nothing used to
    select on it: `attr_cow_p` was invisible to the queue, and the gate's recall
    duly sat still across a whole round of labelling while 44 not-cows kept
    passing as cows. A crop whose cow-probability straddles the threshold is the
    one that would move it."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, gate_band=0.15, uncertain_below=0.0, seek_classes=[])
        _seed(cfg,
              # confidently a cow, confidently posture -> nothing to ask
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99,
                "cow_p": 0.99, "rev": "r1"}] * 6 +
              # astride the gate: same confident posture, but the gate is torn
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99,
                "cow_p": 0.52, "rev": "r1"}] * 6)
        head = _reasons(_serve(cfg))[:6]
        check("the gate-boundary crops lead", set(head) == {"gate_boundary"}, str(head))

        # The band is a real cut, on both sides of the threshold.
        cfg2 = _cfg(d, gate_band=0.01, uncertain_below=0.0, seek_classes=[])
        check("narrowing the band excludes them again",
              "gate_boundary" not in set(_reasons(_serve(cfg2))),
              str(set(_reasons(_serve(cfg2)))))


def test_an_unscored_crop_is_not_mistaken_for_a_gate_boundary():
    """`attr_cow_p` is NULL on everything the model has never seen, and NULL
    comparisons yield NULL rather than FALSE. If that leaked into the CASE, an
    entire unscored corpus would report `gate_boundary` and the audit trail would
    be a lie."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, gate_band=0.15, seek_classes=[])
        _seed(cfg, [{"side": 100.0}] * 8)          # no rev, no cow_p
        reasons = set(_reasons(_serve(cfg)))
        check("an unscored corpus still reads as unscored", reasons == {"unscored"},
              str(reasons))


def test_seeking_a_scarce_class_claims_only_the_confident_ones():
    """`seek_class` and `low_confidence` answer different needs — one balances
    the training set, the other teaches the model where it is unsure — and a
    crop can satisfy both. The CASE tests uncertainty FIRST on purpose: a crop
    the model is unsure of teaches more than one it is sure of, and if
    `seek_class` came first it would swallow every unsure `standing` and the
    audit could no longer tell balancing from teaching."""
    with tempfile.TemporaryDirectory() as d:
        # seek_share=1.0: this test is about the RULE, not the cap. The cap has
        # its own test below.
        cfg = _cfg(d, uncertain_below=0.75, gate_band=0.0, seek_share=1.0,
                   seek_classes=["behaviour.standing"])
        _seed(cfg,
              # confidently standing: wanted purely because training is short of it
              [{"side": 100.0, "cls": "behaviour.standing", "p": 0.95,
                "cow_p": 0.99, "rev": "r1"}] * 4 +
              # unsure standing: the same class, but uncertainty owns the label
              [{"side": 100.0, "cls": "behaviour.standing", "p": 0.60,
                "cow_p": 0.99, "rev": "r1"}] * 4 +
              # confidently a class nobody is seeking
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.95,
                "cow_p": 0.99, "rev": "r1"}] * 4)
        reasons = _reasons(_serve(cfg))
        check("the unsure ones report low_confidence, not seek_class",
              reasons.count("low_confidence") == 4, str(reasons[:12]))
        check("the confident sought ones report seek_class",
              reasons.count("seek_class") == 4, str(reasons[:12]))
        check("an unsought confident class is not promoted",
              reasons.count("confident") == 4, str(reasons[:12]))
        head = set(reasons[:8])
        check("both sought tiers lead the unsought one",
              head == {"low_confidence", "seek_class"}, str(head))


def test_an_empty_seek_list_disables_the_rule():
    """The config documents `seek_classes: []` as off. An empty list bound to a
    DuckDB VARCHAR[] must therefore match nothing — not everything, and not
    raise."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uncertain_below=0.0, gate_band=0.0, seek_share=1.0,
                   seek_classes=[])
        _seed(cfg, [{"side": 100.0, "cls": "behaviour.standing", "p": 0.95,
                     "cow_p": 0.99, "rev": "r1"}] * 6)
        reasons = set(_reasons(_serve(cfg)))
        check("nothing is sought when the list is empty", reasons == {"confident"},
              str(reasons))


def test_abstention_only_promotes_a_crop_worth_answering():
    """The model abstains for two unrelated reasons: the posture is genuinely
    ambiguous (worth asking) and the crop is thirty pixels of brown (worth
    nothing — the annotator skips it). Only size separates them before serving,
    and in the first production round 28 of the abstained tier's 39 skips were
    `low_resolution`. Legibility is now a condition of promotion, not just of
    the tier above."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, abstain_min_px=60, small_share=0.0, uncertain_below=0.75,
                   gate_band=0.0, seek_classes=[])
        _seed(cfg,
              # big and abstained: exactly what the rule is for
              [{"side": 100.0, "cls": None, "p": 0.30, "cow_p": 0.99, "rev": "r1"}] * 4 +
              # big and confident: nothing to ask
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99,
                "cow_p": 0.99, "rev": "r1"}] * 4)
        head = _reasons(_serve(cfg))[:4]
        check("a legible abstention leads", set(head) == {"abstained"}, str(head))

        # The floor is real: the same abstention on a small crop is not promoted
        # ahead of a large confident one.
        with tempfile.TemporaryDirectory() as d2:
            cfg2 = _cfg(d2, abstain_min_px=60, small_share=0.0, uncertain_below=0.75,
                        gate_band=0.0, seek_classes=[])
            _seed(cfg2,
                  [{"side": 30.0, "cls": None, "p": 0.30, "cow_p": 0.99, "rev": "r1"}] * 4 +
                  [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99,
                    "cow_p": 0.99, "rev": "r1"}] * 4)
            items = _serve(cfg2)
            sides = [round(i["bbox"][2] - i["bbox"][0]) for i in items[:4]]
            check("the legible confident crops still come first",
                  set(sides) == {100}, str(sides))


def test_every_reason_the_queue_emits_is_in_the_vocabulary():
    """`api.py` stores `queue_reason` only if it is in `QUEUE_REASONS`, else
    NULL. So a CASE branch added without updating that tuple does not fail — it
    silently throws away the audit trail that tells us which selector paid for
    which label, which is the only way the next round can be evaluated."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uniform_share=0.5, uncertain_below=0.75, gate_band=0.15,
                   seek_share=1.0, seek_classes=["behaviour.standing"])
        _seed(cfg,
              [{"side": 100.0, "cls": "behaviour.standing", "p": 0.95, "cow_p": 0.99, "rev": "r1"}] * 3 +
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.60, "cow_p": 0.99, "rev": "r1"}] * 3 +
              [{"side": 100.0, "cls": None, "p": 0.30, "cow_p": 0.99, "rev": "r1"}] * 3 +
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "cow_p": 0.52, "rev": "r1"}] * 3 +
              [{"side": 100.0}] * 3)
        seen = {r for r in _reasons(_serve(cfg)) if r is not None}
        check("every emitted reason is one the store will keep",
              seen <= set(labeling.QUEUE_REASONS), str(seen - set(labeling.QUEUE_REASONS)))
        check("...and the new ones are genuinely reachable",
              {"gate_boundary", "seek_class"} & seen, str(sorted(seen)))


def test_the_sought_class_is_capped_to_a_share():
    """Confidently-predicted crops of a common posture are an enormous pool --
    far larger than the abstentions or the gate-boundary cases. Uncapped, they
    crowd the front tier with the one signal carrying no uncertainty at all."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uncertain_below=0.0, gate_band=0.0, seek_share=0.25,
                   seek_classes=["behaviour.standing"])
        _seed(cfg, [{"side": 100.0, "cls": "behaviour.standing", "p": 0.95,
                     "cow_p": 0.99, "rev": "r1"}] * 120)
        reasons = _reasons(_serve(cfg))
        sought = reasons.count("seek_class")
        check("only a share of the sought crops is promoted",
              0 < sought < len(reasons), f"{sought} of {len(reasons)}")
        check("...and it is near the configured share",
              0.10 < sought / len(reasons) < 0.45, f"{sought / len(reasons):.2f}")
        # Membership is a pure function of the key, so it survives a re-score.
        again = _reasons(_serve(cfg))
        check("the slice is stable across serves", again.count("seek_class") == sought)


def test_an_unscored_corpus_is_untouched_by_the_legibility_floor():
    """THE INVARIANT AN EARLIER DRAFT BROKE, and the reason this test exists.

    The floor was first ANDed onto the weak-posture term -- which is also the
    term that carries unscored rows through `coalesce(attr_behaviour_p, 0)`. So
    on any day the model had not scored, every sub-floor crop silently dropped
    out of `worth_asking`, including the `small_share` slice that exists to stop
    exactly that. Measured at ~4x fewer small crops served. Nothing raised; the
    queue looked healthy; the bias would have surfaced months later as a model
    that fails on distant animals.

    The floor now applies only to rows the model has actually scored, so an
    unscored corpus must serve in byte-identical order however the floor is set."""
    with tempfile.TemporaryDirectory() as d:
        mixed = [{"side": 30.0}] * 60 + [{"side": 100.0}] * 60   # never scored
        cfg_a = _cfg(d, abstain_min_px=60, small_share=0.15)
        _seed(cfg_a, mixed)
        order_a = [i["instance_key"] for i in _serve(cfg_a)]
        small_a = sum(1 for i in _serve(cfg_a)[:40]
                      if round(i["bbox"][2] - i["bbox"][0]) == 30)

        # Same store, floor effectively disabled: must be indistinguishable.
        cfg_b = _cfg(d, abstain_min_px=1, small_share=0.15)
        order_b = [i["instance_key"] for i in _serve(cfg_b)]
        check("an unscored corpus serves identically whatever the floor is set to",
              order_a == order_b, f"first divergence at "
              f"{next((i for i in range(len(order_a)) if order_a[i] != order_b[i]), None)}")
        check("...and the small_share slice still reaches the front",
              small_a > 0, f"{small_a} small crops in the first 40")


def test_the_legibility_floor_guards_every_selector():
    """The floor is not a property of abstention -- it is a property of acting on
    the model's opinion at all. `gate_boundary` and `seek_class` fire on 30 px of
    brown just as readily, so one floor guards all three rather than one being
    bolted onto the first."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, abstain_min_px=60, small_share=0.0, uncertain_below=0.0,
                   gate_band=0.15, seek_share=1.0, seek_classes=["behaviour.standing"])
        _seed(cfg,
              # tiny, and astride the gate: the model is torn, the human cannot help
              [{"side": 30.0, "cls": "behaviour.lying", "p": 0.99,
                "cow_p": 0.52, "rev": "r1"}] * 6 +
              # tiny, and a sought class
              [{"side": 30.0, "cls": "behaviour.standing", "p": 0.99,
                "cow_p": 0.99, "rev": "r1"}] * 6 +
              # legible and astride the gate: this is the one worth serving
              [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99,
                "cow_p": 0.52, "rev": "r1"}] * 6)
        items = _serve(cfg)
        head = [(round(i["bbox"][2] - i["bbox"][0]), i.get("queue_reason"))
                for i in items[:6]]
        check("only the legible gate-boundary crops lead",
              set(head) == {(100, "gate_boundary")}, str(head))


def test_a_reason_is_never_credited_to_a_rule_that_did_not_promote_the_row():
    """`queue_reason` is the column the entire active-learning audit is computed
    over — "abstained yields 3.7x more not_visible than uniform" is a claim ABOUT
    THIS COLUMN. So a row must never be stamped with a selector that did not
    actually select it.

    `worth_asking` gates all three selectors behind a legibility floor; the
    reason CASE did not. A 30 px crop above `min_instance_px` but below the
    floor, sitting exactly astride the gate threshold, was `worth_asking = FALSE`
    — refused — and still stamped `gate_boundary` the moment anything served it.
    At the shipped defaults that was over half of every `gate_boundary` row.

    The floor is set ABOVE `clear_instance_px` here, which is the documented use
    of the knob and the configuration where those rows stop sorting last and land
    in the front tier instead."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, abstain_min_px=90, clear_instance_px=60, small_share=0.0,
                   uncertain_below=0.75, gate_band=0.15, seek_share=1.0,
                   seek_classes=["behaviour.standing"])
        _seed(cfg, [
            # Illegible (70 px < the 90 px floor) but matching every selector.
            {"side": 70.0, "cls": "behaviour.lying", "p": 0.10, "cow_p": 0.50, "rev": "r1"},
            {"side": 70.0, "cls": "behaviour.standing", "p": 0.99, "cow_p": 0.99, "rev": "r1"},
            {"side": 70.0, "cls": None, "p": 0.30, "cow_p": 0.99, "rev": "r1"},
            # Legible, and genuinely selected.
            {"side": 120.0, "cls": "behaviour.lying", "p": 0.10, "cow_p": 0.50, "rev": "r1"},
            {"side": 120.0, "cls": None, "p": 0.30, "cow_p": 0.99, "rev": "r1"},
        ])
        items = _serve(cfg)
        by_side = {}
        for it in items:
            side = round(it["bbox"][2] - it["bbox"][0])
            by_side.setdefault(side, []).append(it.get("queue_reason"))

        selectors = {"gate_boundary", "abstained", "low_confidence", "seek_class"}
        illegible = set(by_side.get(70, []))
        check("no illegible crop claims a selector as its reason",
              not (illegible & selectors),
              f"70px rows were stamped {sorted(illegible & selectors)}")
        check("...they report `confident`, which is what actually served them",
              illegible <= {"confident", "uniform"}, str(sorted(illegible)))
        check("the legible ones DO carry their selector",
              set(by_side.get(120, [])) & selectors,
              str(sorted(set(by_side.get(120, [])))))


def test_the_reason_and_the_rule_agree_across_a_realistic_corpus():
    """The general invariant behind the test above, asserted over a mixed
    population rather than a hand-picked one: any row carrying a selector reason
    must be one the selector could actually have promoted."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, abstain_min_px=60, clear_instance_px=60, small_share=0.0,
                   uncertain_below=0.75, gate_band=0.15, seek_share=1.0,
                   seek_classes=["behaviour.standing"])
        rows = []
        for i in range(120):
            side = (25.0, 40.0, 70.0, 150.0)[i % 4]
            rows.append({"side": side, "rev": "r1",
                         "cls": (None, "behaviour.standing", "behaviour.lying")[i % 3],
                         "p": (0.2, 0.95, 0.5)[i % 3],
                         "cow_p": (0.5, 0.99, 0.2)[i % 3]})
        _seed(cfg, rows)
        selectors = {"gate_boundary", "abstained", "low_confidence", "seek_class"}
        bad = [(round(it["bbox"][2] - it["bbox"][0]), it.get("queue_reason"))
               for it in _serve(cfg)
               if it.get("queue_reason") in selectors
               and round(it["bbox"][2] - it["bbox"][0]) < cfg.annotation.abstain_min_px]
        check("every selector-stamped row clears the legibility floor",
              not bad, f"{len(bad)} do not, e.g. {bad[:4]}")


def test_a_cow_under_a_panel_is_served_next():
    """Agrivoltaic shade use is the one question this deployment can answer that
    a barn dataset cannot, and `under_panel` is TRUE on a few hundred of ~46k
    detections — so those crops are worth spending annotator time on ahead of an
    equally legible cow standing in the open.

    Ahead of `worth_asking`, behind `clear`: shade makes a crop HARDER to read,
    so the legibility floor applies here exactly as it does to the other
    selectors."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, uncertain_below=0.0, gate_band=0.0, seek_classes=[],
                   abstain_min_px=60, small_share=0.0, under_panel_first=True)
        _seed(cfg, [
            # confident, legible, in the open — the model wants nothing from these
            {"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "cow_p": 0.99,
             "rev": "r1"} for _ in range(20)
        ] + [
            # same in every way EXCEPT that it stands under a panel
            {"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "cow_p": 0.99,
             "rev": "r1", "under_panel": True} for _ in range(4)
        ] + [
            # under a panel but far too small to answer — must NOT be promoted
            {"side": 30.0, "cls": "behaviour.lying", "p": 0.99, "cow_p": 0.99,
             "rev": "r1", "under_panel": True} for _ in range(4)
        ])
        items = _serve(cfg)
        head = items[:4]
        check("the first four served are all under a panel",
              all(i.get("queue_reason") == "under_panel" for i in head),
              str(_reasons(items)[:8]))
        check("...and all of them are legible",
              all(round(i["bbox"][2] - i["bbox"][0]) == 100 for i in head),
              str([round(i["bbox"][2] - i["bbox"][0]) for i in head]))
        n_up = sum(1 for r in _reasons(items) if r == "under_panel")
        check("exactly the four legible panel crops carry the reason",
              n_up == 4, f"{n_up}")
        check("the illegible panel crops are not promoted",
              all(r != "under_panel" for i, r in zip(items, _reasons(items))
                  if round(i["bbox"][2] - i["bbox"][0]) == 30), str(_reasons(items)))


def test_the_under_panel_rule_can_be_turned_off():
    """It is a config flag, and a flag nobody can turn off is not a flag. With it
    off the queue must behave exactly as it did before the rule existed."""
    with tempfile.TemporaryDirectory() as d:
        rows = [{"side": 100.0, "cls": "behaviour.lying", "p": 0.99, "cow_p": 0.99,
                 "rev": "r1", "under_panel": i % 5 == 0} for i in range(30)]
        a = _cfg(d, uncertain_below=0.0, gate_band=0.0, seek_classes=[],
                 under_panel_first=False)
        _seed(a, rows)
        off = [i["instance_key"] for i in _serve(a)]
        check("with the rule off, no row claims the reason",
              "under_panel" not in _reasons(_serve(a)), str(set(_reasons(_serve(a)))))

        b = _cfg(d, uncertain_below=0.0, gate_band=0.0, seek_classes=[],
                 under_panel_first=True)
        on = [i["instance_key"] for i in _serve(b)]
        check("with it on, the order actually changes", off != on,
              "the rule had no effect at all — it is not wired in")
        check("...and the same rows are still all served, just re-ordered",
              sorted(off) == sorted(on), "the rule DROPPED rows, which it must never do")


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
    test_the_bound_parameters_line_up_with_the_placeholders()
    test_a_crop_astride_the_gate_threshold_is_asked_about()
    test_an_unscored_crop_is_not_mistaken_for_a_gate_boundary()
    test_seeking_a_scarce_class_claims_only_the_confident_ones()
    test_an_empty_seek_list_disables_the_rule()
    test_abstention_only_promotes_a_crop_worth_answering()
    test_every_reason_the_queue_emits_is_in_the_vocabulary()
    test_the_sought_class_is_capped_to_a_share()
    test_an_unscored_corpus_is_untouched_by_the_legibility_floor()
    test_the_legibility_floor_guards_every_selector()
    test_a_reason_is_never_credited_to_a_rule_that_did_not_promote_the_row()
    test_the_reason_and_the_rule_agree_across_a_realistic_corpus()
    test_a_cow_under_a_panel_is_served_next()
    test_the_under_panel_rule_can_be_turned_off()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
