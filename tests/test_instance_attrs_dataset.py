"""M5 dataset-builder contract: which labels are usable, and where the split cuts.

The two things pinned here are the two that fail silently:

  - THE LABEL RULES. Escape answers excluded, contested instances dropped, a
    false positive carrying no class answers, `not_visible` kept as a real class.
    Each of these is a decision recorded in docs/roadmap/M5_instance_attrs.md,
    and getting one wrong does not raise — it produces a model that has quietly
    learned to imitate annotator uncertainty, or that never learned to say "I
    cannot see it".
  - THE SPLIT UNIT. A per-instance split leaks near-duplicate crops across
    train/val and inflates every validation number reported thereafter. That is
    the failure that looks like success, so it gets the most tests.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_dataset
    py -3.11 tests/test_instance_attrs_dataset.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, labels_db  # noqa: E402
from cownting.instance_attrs import dataset as ds  # noqa: E402

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


_BBOX = [10.0, 20.0, 110.0, 220.0]
_SHADED = {"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
           "class_name": "Shaded"}
_SUN = {"class_key": "sun_exposure.direct_sun", "group_key": "sun_exposure",
        "class_name": "Direct sun"}
_UNSEEN = {"class_key": "sun_exposure.not_visible", "group_key": "sun_exposure",
           "class_name": "Not visible"}
_ESCAPE = {"class_key": "sun_exposure.cannot_tell", "group_key": "sun_exposure",
           "class_name": "Cannot tell"}
_HALF = {"class_key": "sun_exposure.half_shade", "group_key": "sun_exposure",
         "class_name": "Half shade"}


def _store(d: str):
    con = db.connect(os.path.join(d, "labels.duckdb"))
    labels_db.init_labels_db(con)
    return con


def _key(frame: str) -> str:
    return labels_db.instance_key("2025-07-03", "camera_01", frame, _BBOX, 0)


def _prov(frame: str) -> dict:
    return {"dataset_id": "2025-07-03", "camera_id": "camera_01",
            "frame_path": f"data/artifacts/2025-07-03/frames/camera_01/{frame}",
            "frame_basename": frame,
            "bbox_x1": _BBOX[0], "bbox_y1": _BBOX[1],
            "bbox_x2": _BBOX[2], "bbox_y2": _BBOX[3], "ordinal": 0}


def _answer(con, frame: str, choices, annotator="alice") -> str:
    k = _key(frame)
    labels_db.submit_annotation(con, instance_key=k, annotator=annotator,
                                choices=choices, provenance=_prov(frame))
    return k


def _verdict(con, frame: str, kind: str, annotator="alice") -> str:
    k = _key(frame)
    labels_db.submit_mask_edit(con, instance_key=k, annotator=annotator, kind=kind,
                               polygon=[[0, 0], [10, 0], [10, 10]] if kind == "polygon" else None,
                               provenance=_prov(frame))
    return k


# --------------------------------------------------------------------- vocabulary

def test_vocabulary_drops_escape_and_keeps_not_visible():
    """The single most consequential line in the module.

    `cannot_tell` and `not_visible` read like the same thing to anyone skimming
    the taxonomy, and M5 treats them oppositely on purpose: one is annotator
    uncertainty (must not be learned), the other is a property of the crop (must
    be, or the model invents postures for cows it cannot see).
    """
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        vocab = ds.head_vocabulary(con)
        sun = vocab["sun_exposure"]
        check("the escape class is not a trainable class",
              "sun_exposure.cannot_tell" not in sun, str(sun))
        check("...but not_visible is",
              "sun_exposure.not_visible" in sun, str(sun))
        check("head_probing is carried even at n=12",
              "behaviour.head_probing" in vocab["behaviour"], str(vocab["behaviour"]))
        check("the cow gate is a head, and P(cow) is class index 1",
              vocab[ds.COW_HEAD] == ("not_cow", "cow"), str(vocab[ds.COW_HEAD]))
        # Ordering must be the store's own, or a class index means one thing in
        # the dataset and another in the checkpoint that consumes it.
        want = [r[0] for r in con.execute(
            "SELECT class_key FROM label_classes WHERE group_key='sun_exposure' "
            "AND active AND NOT is_escape ORDER BY sort_order, class_key").fetchall()]
        check("class order follows sort_order, class_key", list(sun) == want, str(sun))
        con.close()


def test_multi_select_group_is_refused_not_guessed():
    """A softmax head cannot represent a set; taking one member would fabricate."""
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        con.execute("UPDATE label_groups SET multi_select = TRUE WHERE group_key='behaviour'")
        vocab = ds.head_vocabulary(con)
        check("a multi-select group becomes no head at all",
              "behaviour" not in vocab, str(sorted(vocab)))
        check("...and the single-select group is unaffected", "sun_exposure" in vocab)
        con.close()


def test_an_excluded_class_is_offered_but_not_trained():
    """`exclude_classes` removes the OUTPUT, not the disagreement.

    `sun_exposure.half_shade` (2026-10-04) ships active in the taxonomy and out
    of the head: annotators answer it from the day it lands while the deployed
    three-class sun model stays exactly as it is, because the class starts at
    zero examples and a fourth class on a handful of them is a measured failure
    (M5 §2.2). Two halves to that, and the second one costs something:

      - an instance whose only sun answer is the excluded class trains the other
        heads and contributes nothing to sun, the same as an archived class;
      - an instance one rater called Shaded and another Half shade is CONTESTED
        in SQL, and the unanimity test runs BEFORE the vocabulary filter — so
        the sun label is dropped rather than resolving to the one answer the
        head still has an output for. Adding an option beside an existing one
        therefore takes away some signal that was unanimous before it existed.
        That is the price of the new tile, pinned here so it is a known cost
        rather than a mystery in the next CV run's class counts.
    """
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        offered = con.execute(
            "SELECT active, is_escape FROM label_classes "
            "WHERE class_key = 'sun_exposure.half_shade'").fetchone()
        check("the taxonomy offers Half shade as a normal, active class",
              offered == (True, False), str(offered))
        vocab = ds.head_vocabulary(con, exclude=["sun_exposure.half_shade"])
        check("...and the head has no output for it",
              "sun_exposure.half_shade" not in vocab["sun_exposure"],
              str(vocab["sun_exposure"]))
        check("the three classes the deployed checkpoint knows are untouched",
              vocab["sun_exposure"] == ("sun_exposure.shaded", "sun_exposure.direct_sun",
                                        "sun_exposure.not_visible"),
              str(vocab["sun_exposure"]))

        _answer(con, "00000001.jpg", [_HALF])
        _verdict(con, "00000001.jpg", "ok")
        _answer(con, "00000002.jpg", [_SHADED], annotator="alice")
        _answer(con, "00000002.jpg", [_HALF], annotator="bob")
        samples = {s.instance_key: s for s in
                   ds.collect_samples(con, block_frames=30, vocab=vocab)}
        only_half = samples.get(_key("00000001.jpg"))
        check("an excluded-class answer is not a sun label",
              only_half is not None and "sun_exposure" not in only_half.labels,
              str(only_half))
        check("...but the instance still trains the gate",
              only_half is not None and only_half.labels.get(ds.COW_HEAD) == "cow",
              str(only_half))
        contested = samples.get(_key("00000002.jpg"))
        check("Shaded vs Half shade is contested, not resolved to Shaded",
              contested is None or "sun_exposure" not in contested.labels,
              str(contested))
        con.close()


# ------------------------------------------------------------------------ labels

_MERGE = {"sun_exposure.cannot_tell": "sun_exposure.not_visible"}


def test_an_escape_only_instance_is_a_label_once_merged():
    """Nobody could read the light -> that IS the label, not missing data.

    Two annotators filed unanswerable crops under different escapes for a
    month (one under Cannot tell, dropped; one under Not visible, trained), so
    the trained class was a convention rather than a property of the crop.
    With the merge configured the escape-only instance trains as not_visible;
    without it, it is dropped exactly as before — the SQL change alone is a
    no-op for every deployment that never configures the merge.
    """
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _answer(con, "00000001.jpg", [_ESCAPE], annotator="alice")
        _answer(con, "00000002.jpg", [_SHADED], annotator="alice")
        merged = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30,
                                                                  merge_classes=_MERGE)}
        check("with the merge, the escape-only instance is a not_visible label",
              merged[_key("00000001.jpg")].labels.get("sun_exposure") == "sun_exposure.not_visible",
              str(merged.get(_key("00000001.jpg"))))
        check("...and an ordinary answer is untouched by it",
              merged[_key("00000002.jpg")].labels.get("sun_exposure") == "sun_exposure.shaded")
        plain = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        check("without a merge, the escape-only instance is dropped as before",
              _key("00000001.jpg") not in plain, str(plain.keys()))
        con.close()


def test_escape_answers_are_missing_data_not_disagreement():
    """One rater says Shaded, the other Cannot tell -> unanimous Shaded.

    An escape YIELDS to a substantive answer — the reading labels_db's
    *_NO_ESCAPE agreement queries take. Scoring this as contested would throw
    away a label that is not actually contested. Holds with the merge on, too:
    a merge recovers escape-ONLY instances, it never outvotes a rater who could
    answer.
    """
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _answer(con, "00000001.jpg", [_SHADED], annotator="alice")
        _answer(con, "00000001.jpg", [_ESCAPE], annotator="bob")
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        s = by_key[_key("00000001.jpg")]
        check("escape is treated as missing, not as a disagreement",
              s.labels.get("sun_exposure") == "sun_exposure.shaded", str(s.labels))
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30,
                                                                  merge_classes=_MERGE)}
        s = by_key[_key("00000001.jpg")]
        check("...and the merge does not turn it into a contest either",
              s.labels.get("sun_exposure") == "sun_exposure.shaded", str(s.labels))
        con.close()


def test_a_contested_instance_trains_nothing_on_that_head():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _answer(con, "00000002.jpg", [_SHADED], annotator="alice")
        _answer(con, "00000002.jpg", [_SUN], annotator="bob")
        # ...but the two DO agree it is a cow, so the gate still learns from it.
        _verdict(con, "00000002.jpg", "ok", annotator="alice")
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        s = by_key[_key("00000002.jpg")]
        check("no class label is invented from a coin flip",
              "sun_exposure" not in s.labels, str(s.labels))
        check("...and the head that IS agreed still supervises",
              s.labels.get(ds.COW_HEAD) == "cow", str(s.labels))
        con.close()


def test_a_false_positive_is_a_gate_label_with_no_class_labels():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _verdict(con, "00000003.jpg", "false_positive")
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        s = by_key[_key("00000003.jpg")]
        check("not-a-cow is label 0 on the gate head",
              s.labels.get(ds.COW_HEAD) == "not_cow", str(s.labels))
        check("...and it carries no class answers to be masked in",
              set(s.labels) == {ds.COW_HEAD}, str(s.labels))
        con.close()


def test_an_instance_with_nothing_usable_is_not_a_sample():
    """A flag is a refusal to answer, and an escape-only answer is uncertainty.
    Neither is a training signal, and a sample with an empty label dict would
    contribute a zero-gradient row to every batch it lands in."""
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        labels_db.submit_annotation(
            con, instance_key=_key("00000004.jpg"), annotator="alice",
            outcome="skipped", telemetry={"skip_reason": "low_resolution"},
            provenance=_prov("00000004.jpg"))
        _answer(con, "00000005.jpg", [_ESCAPE])
        keys = {s.instance_key for s in ds.collect_samples(con, block_frames=30)}
        check("a flagged instance is not a sample", _key("00000004.jpg") not in keys)
        check("an escape-only answer is not a sample", _key("00000005.jpg") not in keys)
        con.close()


def test_not_visible_is_carried_through_as_a_real_label():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _answer(con, "00000006.jpg", [_UNSEEN])
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        check("not_visible survives into the sample",
              by_key[_key("00000006.jpg")].labels.get("sun_exposure")
              == "sun_exposure.not_visible")
        con.close()


# ------------------------------------------------------------------------- split

def test_block_is_camera_day_and_time_not_instance():
    b = ds.block_of("2025-07-03", "camera_01", 0, 30)
    check("frames inside one window share a block",
          ds.block_of("2025-07-03", "camera_01", 29, 30) == b)
    check("the next window is a different block",
          ds.block_of("2025-07-03", "camera_01", 30, 30) != b)
    check("another camera at the same minute is a different block",
          ds.block_of("2025-07-03", "camera_02", 0, 30) != b)
    check("another day at the same minute is a different block",
          ds.block_of("2025-07-04", "camera_01", 0, 30) != b)
    check("the frame index comes off the {idx:08d}.jpg name",
          ds.frame_index_of("00000411.jpg") == 411)
    check("a non-numeric name has no index (and is dropped, not guessed)",
          ds.frame_index_of("latest.jpg") is None)


def test_split_targets_the_sample_fraction_not_the_block_count():
    """Blocks vary by an order of magnitude here — one labeled cow on a
    camera-day, forty on another. Splitting on the block count would land
    anywhere between 5% and 40% of the actual data and the configured 0.2 would
    be fiction."""
    blocks = {"big": 100, "a": 2, "b": 2, "c": 2, "d": 2, "e": 2}
    assign = ds.split_blocks(blocks, val_fraction=0.2, seed=1337)
    val = sum(n for b, n in blocks.items() if assign[b] == "val")
    # A block holding 91% of the corpus cannot go in a 20% val, so the honest
    # best is every small block and none of the big one. Taking "big" would give
    # a 96% val, which is what the naive while-under-target loop did.
    check("an oversized block is left out rather than overshooting the target",
          assign["big"] == "train", str(assign))
    check("...and every block that DOES fit is used",
          val == 10, f"val={val}/{sum(blocks.values())}")
    check("every block is assigned exactly one side", set(assign) == set(blocks))

    # The ordinary case: many similar-sized blocks should land close to 20%.
    even = {f"b{i}": 5 for i in range(40)}
    ev = ds.split_blocks(even, val_fraction=0.2, seed=3)
    got = sum(n for b, n in even.items() if ev[b] == "val")
    check("with even blocks the split hits the requested fraction",
          got == 40, f"val={got}/200")


def test_split_is_deterministic_and_whole_blocks_only():
    blocks = {f"b{i}": (i % 5) + 1 for i in range(40)}
    a = ds.split_blocks(blocks, val_fraction=0.2, seed=7)
    b = ds.split_blocks(blocks, val_fraction=0.2, seed=7)
    check("same seed, same split", a == b)
    c = ds.split_blocks(blocks, val_fraction=0.2, seed=8)
    check("a different seed moves the cut", a != c)
    # The property the whole design rests on: no block is on both sides, so no
    # near-duplicate crop can straddle.
    check("no block is split across train and val",
          all(v in ("train", "val") for v in a.values()) and len(a) == len(blocks))


def test_split_never_produces_an_empty_side():
    """The trainer divides by len(val), so neither side may be empty.

    This test used to assert that a single block yields `{"only": "val"}` — which
    is an empty side, just the other one. `fit` then built an empty train
    DataLoader, divided by `max(1, len(tl))` rather than erroring, ran its
    epochs against nothing and SAVED A CHECKPOINT holding random head weights.
    Scoring a corpus with it would have written 46,000 confident-looking verdicts
    from an untrained model.

    One block cannot be split at all: blocking exists because instances within a
    block are near-duplicates, so any cut through one leaks. Refusing is the only
    honest answer, and a raise is recoverable in a way a served random model is
    not."""
    try:
        got = ds.split_blocks({"only": 5}, val_fraction=0.2, seed=1)
        raised = ""
    except ValueError as e:
        got, raised = None, str(e)
    check("a single block is refused rather than split", bool(raised), str(got))
    check("...and the message says what to do about it",
          "at least two" in raised, raised)

    two = ds.split_blocks({"a": 5, "b": 5}, val_fraction=1.0, seed=1)
    check("val_fraction=1.0 still leaves something to train on",
          "train" in set(two.values()), str(two))
    check("...and something to validate on", "val" in set(two.values()), str(two))


def test_samples_land_in_the_block_their_frame_belongs_to():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        _answer(con, "00000005.jpg", [_SHADED])     # window 0
        _answer(con, "00000059.jpg", [_SUN])        # window 1
        by_key = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
        b0 = by_key[_key("00000005.jpg")].block
        b1 = by_key[_key("00000059.jpg")].block
        check("frames 54 minutes apart are different blocks", b0 != b1, f"{b0} vs {b1}")
        check("the block names the camera-day", b0.startswith("2025-07-03|camera_01|"), b0)
        con.close()


def main():
    print("=== test_instance_attrs_dataset ===")
    test_vocabulary_drops_escape_and_keeps_not_visible()
    test_multi_select_group_is_refused_not_guessed()
    test_an_excluded_class_is_offered_but_not_trained()
    test_an_escape_only_instance_is_a_label_once_merged()
    test_escape_answers_are_missing_data_not_disagreement()
    test_a_contested_instance_trains_nothing_on_that_head()
    test_a_false_positive_is_a_gate_label_with_no_class_labels()
    test_an_instance_with_nothing_usable_is_not_a_sample()
    test_not_visible_is_carried_through_as_a_real_label()
    test_block_is_camera_day_and_time_not_instance()
    test_split_targets_the_sample_fraction_not_the_block_count()
    test_split_is_deterministic_and_whole_blocks_only()
    test_split_never_produces_an_empty_side()
    test_samples_land_in_the_block_their_frame_belongs_to()
    print("===================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
