"""Class merges happen at dataset-build time; the store is never rewritten.

The label store is append-only because annotator variability is the product.
So retiring a class is two separate acts: ARCHIVE it in the taxonomy (it stops
being offered; every stored answer stays), and MAP its answers when the training
set is built — as a blanket class->class merge, or per instance from a CSV.

What fails silently here: a mapping applied AFTER the vocabulary check (the
archived class is filtered out first and the answer is dropped, so the merge
looks like it worked and the class just quietly vanishes from training); a
relabel that targets a class the taxonomy has also archived; and the store
being written to by any of it.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_relabel
    py -3.11 tests/test_instance_attrs_relabel.py
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
_PROBE = {"class_key": "behaviour.head_probing", "group_key": "behaviour",
          "class_name": "Head probing"}
_FEED = {"class_key": "behaviour.feeding", "group_key": "behaviour", "class_name": "Feeding"}


def _store(d):
    con = db.connect(os.path.join(d, "labels.duckdb"))
    labels_db.init_labels_db(con)
    return con


def _key(frame):
    return labels_db.instance_key("2025-07-03", "camera_01", frame, _BBOX, 0)


def _prov(frame):
    return {"dataset_id": "2025-07-03", "camera_id": "camera_01",
            "frame_path": f"data/artifacts/2025-07-03/frames/camera_01/{frame}",
            "frame_basename": frame, "bbox_x1": _BBOX[0], "bbox_y1": _BBOX[1],
            "bbox_x2": _BBOX[2], "bbox_y2": _BBOX[3], "ordinal": 0}


def _answer(con, frame, choices, annotator="alice"):
    k = _key(frame)
    labels_db.submit_annotation(con, instance_key=k, annotator=annotator,
                                choices=choices, provenance=_prov(frame))
    return k


def test_archiving_alone_drops_the_answers_and_a_merge_carries_them():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        try:
            k = _answer(con, "00000001.jpg", [_PROBE])
            labels_db.update_class(con, "behaviour.head_probing", active=False, actor="t")
            vocab = ds.head_vocabulary(con)
            check("an archived class leaves the vocabulary",
                  "behaviour.head_probing" not in vocab["behaviour"], str(vocab["behaviour"]))
            # This instance's ONLY answer is against the archived class, so with
            # no merge it has nothing usable left and is not a sample at all.
            plain = {s.instance_key: s for s in ds.collect_samples(con, block_frames=30)}
            check("with no merge, the archived class's answer is DROPPED",
                  k not in plain or "behaviour" not in plain[k].labels, str(plain.get(k)))
            merged = {s.instance_key: s for s in ds.collect_samples(
                con, block_frames=30,
                merge_classes={"behaviour.head_probing": "behaviour.feeding"})}
            check("with a merge, it is carried into the target class",
                  k in merged and merged[k].labels.get("behaviour") == "behaviour.feeding",
                  str(merged.get(k)))
            # ...and the store is untouched: the stored answer is still head_probing.
            stored = con.execute(
                "SELECT class_key FROM v_current_answers WHERE effective_key = ? "
                "AND group_key='behaviour'", [k]).fetchone()[0]
            check("the store still says head_probing", stored == "behaviour.head_probing", stored)
        finally:
            con.close()


def test_per_instance_relabels_override_the_blanket_merge():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        try:
            k1 = _answer(con, "00000001.jpg", [_PROBE])
            k2 = _answer(con, "00000002.jpg", [_PROBE])
            labels_db.update_class(con, "behaviour.head_probing", active=False, actor="t")
            out = {s.instance_key: s for s in ds.collect_samples(
                con, block_frames=30,
                merge_classes={"behaviour.head_probing": "behaviour.feeding"},
                relabels={(k2, "behaviour"): "behaviour.standing"})}
            check("an instance without an override follows the merge",
                  k1 in out and out[k1].labels.get("behaviour") == "behaviour.feeding",
                  str(out.get(k1)))
            check("an instance WITH an override goes where the CSV says",
                  k2 in out and out[k2].labels.get("behaviour") == "behaviour.standing",
                  str(out.get(k2)))
        finally:
            con.close()


def test_a_relabel_targeting_an_archived_class_is_dropped_not_kept():
    """The target has to be something an annotator could still pick, or the
    training set gains a class the taxonomy no longer has."""
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        try:
            k = _answer(con, "00000001.jpg", [_FEED])
            labels_db.update_class(con, "behaviour.head_probing", active=False, actor="t")
            out = {s.instance_key: s for s in ds.collect_samples(
                con, block_frames=30, relabels={(k, "behaviour"): "behaviour.head_probing"})}
            check("relabelling INTO an archived class drops the answer",
                  k not in out or "behaviour" not in out[k].labels, str(out.get(k)))
        finally:
            con.close()


def test_relabel_csv_round_trip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "r.csv")
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("instance_key,group_key,class_key\nabc,behaviour,behaviour.standing\n"
                    "def,sun_exposure,sun_exposure.shaded\n")
        r = ds.load_relabels(p)
        check("the CSV loads keyed by (instance, group)",
              r == {("abc", "behaviour"): "behaviour.standing",
                    ("def", "sun_exposure"): "sun_exposure.shaded"}, str(r))
        check("None means no relabels", ds.load_relabels(None) == {})


def test_archive_class_survives_a_reseed():
    """seed_taxonomy runs on every boot. If it re-activated archived rows the
    deployed change would undo itself on the next restart."""
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        try:
            labels_db.update_class(con, "behaviour.head_probing", active=False, actor="t")
            labels_db.seed_taxonomy(con)
            labels_db.init_labels_db(con)
            active = con.execute(
                "SELECT active FROM label_classes WHERE class_key='behaviour.head_probing'"
            ).fetchone()[0]
            check("a re-seed and a re-init leave the archive in place", active is False)
            n = con.execute("SELECT count(*) FROM taxonomy_audit "
                            "WHERE target_key='behaviour.head_probing'").fetchone()[0]
            check("the archive wrote an audit row", n >= 1, str(n))
        finally:
            con.close()


def main():
    print("=== test_instance_attrs_relabel ===")
    test_archiving_alone_drops_the_answers_and_a_merge_carries_them()
    test_per_instance_relabels_override_the_blanket_merge()
    test_a_relabel_targeting_an_archived_class_is_dropped_not_kept()
    test_relabel_csv_round_trip()
    test_archive_class_survives_a_reseed()
    print("===================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
