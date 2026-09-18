"""Turn the annotation store into a training set of instance crops.

Reads `data/labels.duckdb` and the frame JPEGs; touches the main DB not at all.
That is possible because every annotation carries denormalised provenance
(`labels_db.PROVENANCE_COLS`) so it stays readable after its detection row is
gone — the same property that makes the label store safe to back up on its own.

THE CROPS ARE CUT BY `labeling.render_crop`, and that is a methodological
requirement rather than code reuse. Brinno burns wall-clock time into the bottom
~4% of every frame, time of day predicts sun exposure, and `render_crop` masks
that band — `labeling.py` says a visible banner "hands the annotator the answer
and inflates agreement artificially". A network reads a timestamp far more
diligently than a tired human does. Cut these crops with a plain `Image.crop`
and the sun head learns to read the clock, reports ~95% val accuracy, and
collapses on the first camera whose clock is set differently. Going through
`render_crop` also guarantees the model trains on EXACTLY the pixels the label
describes: same padding, same square canvas, same neutral out-of-frame fill,
same JPEG quantisation.

Output layout — crops and split are deliberately separate:

    <dataset_dir>/crops/<instance_key>.jpg
    <dataset_dir>/manifest.json

The manifest carries the split, so re-splitting (a new seed, a different block
size) is a rewrite of one JSON file rather than a re-render of every crop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb

from .. import labeling
from ..config import Config

# The gate's head is not a taxonomy group — there is no question on the Label
# page called "is this a cow". It is read off the OUTLINE verdict M4a collects:
# `false_positive` says the detection is not an animal, `ok`/`polygon` say it is.
# Index order is the head's class order, so `cow` is class 1 and the reported
# probability is P(cow) without a lookup.
COW_HEAD = "cow"
COW_CLASSES: tuple[str, ...] = ("not_cow", "cow")

# Kinds that assert an animal IS present. Frozen here rather than read from the
# store because `mask_edits.kind` is a code-level vocabulary (labels_db's DDL
# says so explicitly), not a poweruser-editable list.
_COW_KINDS = ("ok", "polygon")


@dataclass(frozen=True)
class Sample:
    """One instance: where its pixels are, and what it is known to be.

    `labels` is head -> class name, and a head is ABSENT when that instance
    carries no answer for it. Absence is normal and load-bearing: a false
    positive has no class answers (verified in the corpus — the overlap is
    exactly zero), and an instance answered before the outline step existed has
    no cow verdict. The training loss masks per head rather than dropping the
    sample, so each label supervises what it actually knows about.
    """
    instance_key: str
    dataset_id: str
    camera_id: str
    frame_file: str
    frame_idx: int
    bbox: tuple[float, float, float, float]
    block: str
    labels: dict[str, str] = field(default_factory=dict)


def frame_index_of(frame_file: str) -> int | None:
    """The frame's ordinal, parsed from its name (`ingest/video.py`: `{idx:08d}.jpg`).

    Parsed rather than joined from `frames.frame_idx` because this module never
    opens the main DB, and because the annotation's own `ts` provenance column is
    NULL on every row in the corpus — the queue does not stamp it (it deliberately
    withholds clock time from the annotator, see `labeling._item`). The basename
    is therefore the only timestamp available here, and at
    `ingest.frame_interval_seconds = 60` it is a minute counter, which is exactly
    what the block split wants.
    """
    stem = Path(frame_file).stem
    return int(stem) if stem.isdigit() else None


def block_of(dataset_id: str, camera_id: str, frame_idx: int, block_frames: int) -> str:
    """The split unit: a contiguous run of frames from ONE camera on ONE day.

    Not the instance. The footage is 1 frame/minute and cows barely move
    minute-to-minute, so two instances from adjacent frames are near-duplicates;
    splitting at random puts one in train and its twin in val and the resulting
    score measures memorisation. `finetune/dataset.py` made this same call for
    the mask fine-tune ("near-duplicate adjacent minutes never straddle the
    split, which would inflate val metrics") — this follows it rather than
    inventing a second rule.
    """
    return f"{dataset_id}|{camera_id}|{frame_idx // max(1, block_frames)}"


def split_blocks(
    blocks: dict[str, int],
    *,
    val_fraction: float,
    seed: int,
) -> dict[str, str]:
    """Assign whole blocks to 'train' or 'val'. `blocks` is block -> sample count.

    Blocks are shuffled under `seed` and taken into val until val holds
    `val_fraction` of the SAMPLES — not of the blocks. Blocks vary in size by an
    order of magnitude here (a camera-day with one labeled cow, another with
    forty), so splitting on the block count would land anywhere between 5% and
    40% of the actual data and the "80/20" in the config would be fiction.

    Deterministic given (blocks, seed): the shuffle runs over a SORTED key list,
    so a dict that iterates differently between runs cannot change the split.
    """
    import random

    keys = sorted(blocks)
    total = sum(blocks.values())
    rng = random.Random(seed)
    rng.shuffle(keys)
    want = total * max(0.0, min(1.0, val_fraction))
    out: dict[str, str] = {}
    got = 0
    for k in keys:
        n = blocks[k]
        # Take a block only when taking it lands CLOSER to the target than
        # leaving it. Naively taking blocks "while got < want" overshoots by a
        # whole block: with one 100-sample block among five 2-sample ones, a 20%
        # request produced a 96% val, because the big block was taken while the
        # running total was still under target. Skipping rather than stopping
        # also matters — the loop keeps looking, so a block too big to fit does
        # not strand every smaller block behind it in train.
        if got < want and abs((got + n) - want) <= abs(got - want):
            out[k] = "val"
            got += n
        else:
            out[k] = "train"
    # Degenerate corpora (one block, or val_fraction 0) must still produce both
    # splits rather than an empty val the trainer would divide by.
    if got == 0 and keys:
        out[keys[0]] = "val"
    if all(v == "val" for v in out.values()) and len(keys) > 1:
        out[keys[-1]] = "train"
    return out


def head_vocabulary(lcon: duckdb.DuckDBPyConnection, *,
                    exclude: Sequence[str] = ()) -> dict[str, tuple[str, ...]]:
    """head -> its ordered class names, derived from the LIVE taxonomy.

    Three rules, each of which is one of M5's frozen decisions:

    - `is_escape` classes are excluded from the VOCABULARY. That is
      `cannot_tell`, defined as "the body is visible, but the pose will not
      resolve" — genuine annotator uncertainty. `labels_db`'s `SQL_*_NO_ESCAPE`
      variants treat it as missing data for the agreement statistics; the
      model has no output for it. But an escape ANSWER is not thrown away here
      any more: `_SQL_ANSWERS` carries it when it is all an instance has, and
      `merge_classes` may fold it into a trained class (2026-09-18: both
      cannot_tell → not_visible, see M5 §2.3). `not_visible` is NOT escape and
      IS trained: its definition is a property of the crop ("too little of the
      body is visible"), so it is predictable from pixels, and it is the
      model's only honest output on the many unjudgeable crops in the corpus.
    - Archived classes are excluded, but only from the vocabulary — an answer
      recorded against a class since archived simply has nowhere to go and is
      dropped for that head, exactly as the Label page stops offering it.
    - `multi_select` groups are skipped entirely. A softmax head cannot represent
      a set, and silently taking one member of it would fabricate a label. Both
      shipped groups are single-select; this refuses rather than guesses if that
      changes.

    Ordering is `sort_order, class_key` — the store's own ordering rule, the same
    one the tiles and the hotkeys use — so a class index means the same thing
    across the dataset, the checkpoint and the dashboard.
    """
    drop = set(exclude)
    out: dict[str, tuple[str, ...]] = {COW_HEAD: COW_CLASSES}
    groups = lcon.execute(
        "SELECT group_key FROM label_groups WHERE active AND NOT multi_select "
        "ORDER BY sort_order, group_key"
    ).fetchall()
    for (gk,) in groups:
        rows = lcon.execute(
            "SELECT class_key FROM label_classes WHERE group_key = ? AND active "
            "AND NOT is_escape ORDER BY sort_order, class_key",
            [gk],
        ).fetchall()
        keep = tuple(r[0] for r in rows if r[0] not in drop)
        if keep:
            out[gk] = keep
    return out


# One row per instance that carries at least one usable label.
#
# The unanimity rule matches the Label panel's class block
# (`labels_db.SQL_CLASS_DISTRIBUTION`): an instance counts under a class when
# every annotator who answered it agreed. An instance two annotators disagree
# about has no single truth to train on and is dropped for that head — silently
# picking one rater's answer would teach the model to reproduce a coin flip.
#
# Escape answers YIELD to substantive ones: "one rater said Shaded, the other
# said Cannot tell" reads as unanimous Shaded rather than as a disagreement —
# the same "escape as missing data" reading the `*_NO_ESCAPE` agreement queries
# take, and it recovers labels that are not actually contested. But an escape
# is no longer DISCARDED when it is all an instance has. The first version
# filtered escapes out before the unanimity test, full stop, which silently
# dropped every instance whose only answer was Cannot tell — 319 for sun, 108
# for behaviour by 2026-09-18 — and did so asymmetrically: one annotator put
# unanswerable crops under Cannot tell (dropped), the other under Not visible
# (trained), so the trained class became one person's convention. Now an
# all-escape instance is emitted with the escape's class_key; `collect_samples`
# then either merges it into a trained class (`merge_classes`) or, with no
# merge configured, drops it at the vocabulary check exactly as before.
#
# ARCHIVED classes are deliberately NOT filtered here. `collect_samples` may
# remap an answer (a class merge) before it checks the vocabulary, and a merge
# only works if the answer reaches it: with `AND c.active` in this query the
# head_probing answers vanished in SQL and the merge silently carried nothing.
# The active-class filter lives in Python, AFTER the remap. One consequence,
# and the right one: two raters who said head_probing and feeding are contested
# here and stay contested — archiving a class must not manufacture agreement.
_SQL_ANSWERS = """
SELECT v.effective_key, v.group_key,
       count(DISTINCT CASE WHEN NOT c.is_escape THEN v.class_key END) AS n_classes,
       coalesce(max(CASE WHEN NOT c.is_escape THEN v.class_key END),
                max(CASE WHEN c.is_escape THEN v.class_key END))     AS class_key
FROM v_current_answers v
JOIN label_classes c ON c.class_key = v.class_key
WHERE NOT coalesce(v.acting_preview, FALSE)
GROUP BY 1, 2
HAVING count(DISTINCT CASE WHEN NOT c.is_escape THEN v.class_key END) = 1
    OR (count(DISTINCT CASE WHEN NOT c.is_escape THEN v.class_key END) = 0
        AND count(DISTINCT CASE WHEN c.is_escape THEN v.class_key END) = 1)
"""

# The outline verdict, collapsed per instance. An instance carrying BOTH a
# false_positive and an ok/polygon is contested about its own existence and gets
# no cow label at all — the same refusal-to-guess as a contested class answer.
_SQL_COW = f"""
SELECT effective_key,
       max(CASE WHEN kind = 'false_positive' THEN 1 ELSE 0 END) AS any_fp,
       max(CASE WHEN kind IN {_COW_KINDS} THEN 1 ELSE 0 END)    AS any_cow
FROM mask_edits
WHERE superseded_at IS NULL AND NOT coalesce(acting_preview, FALSE)
GROUP BY 1
"""

# Where the pixels are. Unioned across both tables because either can be the only
# witness: a false positive has no annotation row, and an instance answered
# before M4a shipped has no mask_edits row. `any_value` is safe because both
# tables denormalise the SAME detection's provenance, and the instance key is a
# hash OF that provenance — two rows for one key cannot disagree about the bbox
# without also being different keys.
_SQL_PROV = """
SELECT effective_key,
       any_value(dataset_id)     AS dataset_id,
       any_value(camera_id)      AS camera_id,
       any_value(frame_basename) AS frame_basename,
       any_value(bbox_x1) AS bbox_x1, any_value(bbox_y1) AS bbox_y1,
       any_value(bbox_x2) AS bbox_x2, any_value(bbox_y2) AS bbox_y2
FROM (
    SELECT effective_key, dataset_id, camera_id, frame_basename,
           bbox_x1, bbox_y1, bbox_x2, bbox_y2
    FROM annotations
    WHERE superseded_at IS NULL AND outcome = 'labeled'
      AND NOT coalesce(acting_preview, FALSE)
    UNION ALL
    SELECT effective_key, dataset_id, camera_id, frame_basename,
           bbox_x1, bbox_y1, bbox_x2, bbox_y2
    FROM mask_edits
    WHERE superseded_at IS NULL AND NOT coalesce(acting_preview, FALSE)
)
WHERE frame_basename IS NOT NULL AND bbox_x1 IS NOT NULL
GROUP BY 1
"""


def load_relabels(path: str | Path | None) -> dict[tuple[str, str], str]:
    """(instance_key, group_key) -> class_key from a relabels CSV. Empty if None."""
    if not path:
        return {}
    import csv

    out: dict[tuple[str, str], str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[(row["instance_key"], row["group_key"])] = row["class_key"]
    return out


def collect_samples(
    lcon: duckdb.DuckDBPyConnection,
    *,
    block_frames: int,
    vocab: dict[str, tuple[str, ...]] | None = None,
    merge_classes: Mapping[str, str] | None = None,
    relabels: Mapping[tuple[str, str], str] | None = None,
) -> list[Sample]:
    """Every instance with at least one usable label, with its provenance.

    Pure read; renders nothing. Split out from `build_dataset` so the label
    arithmetic is testable without a filesystem full of JPEGs.

    `merge_classes` and `relabels` rewrite an answer BEFORE the vocabulary
    check, so an answer against a now-archived class can be carried into an
    active one instead of dropped. The store is not touched. Note the order:
    the SQL unanimity test runs on the stored classes, so two raters who said
    head_probing and feeding are contested in the store and stay contested —
    a merge recovers single-rater answers, not disagreements.
    """
    vocab = vocab if vocab is not None else head_vocabulary(lcon)
    known = {h: set(cs) for h, cs in vocab.items()}
    merge = dict(merge_classes or {})
    over = dict(relabels or {})

    answers: dict[str, dict[str, str]] = {}
    for key, group_key, _n, class_key in lcon.execute(_SQL_ANSWERS).fetchall():
        class_key = over.get((key, group_key), merge.get(class_key, class_key))
        if group_key in known and class_key in known[group_key]:
            answers.setdefault(key, {})[group_key] = class_key

    cow: dict[str, str] = {}
    for key, any_fp, any_cow in lcon.execute(_SQL_COW).fetchall():
        if bool(any_fp) != bool(any_cow):       # exactly one verdict, not both
            cow[key] = COW_CLASSES[1] if any_cow else COW_CLASSES[0]

    out: list[Sample] = []
    for row in lcon.execute(_SQL_PROV).fetchall():
        key, ds, cam, base, x1, y1, x2, y2 = row
        labels = dict(answers.get(key, {}))
        if key in cow:
            labels[COW_HEAD] = cow[key]
        if not labels:
            continue                             # nothing to learn from
        idx = frame_index_of(base or "")
        if idx is None or not cam:
            continue                             # cannot be placed in a block
        out.append(Sample(
            instance_key=key, dataset_id=ds or "", camera_id=cam,
            frame_file=base, frame_idx=idx,
            bbox=(float(x1), float(y1), float(x2), float(y2)),
            block=block_of(ds or "", cam, idx, block_frames),
            labels=labels,
        ))
    out.sort(key=lambda s: s.instance_key)       # deterministic manifest order
    return out


def build_dataset(
    config: Config,
    *,
    out_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Render every labeled instance to a crop and write the manifest.

    Returns a summary dict (also the manifest's `summary` block). Crops already
    on disk are reused unless `force` — re-rendering 500 JPEGs is cheap but
    re-rendering is also the only step that needs the frames present, so a
    manifest can be rebuilt with a different split on a box that no longer has
    them.
    """
    cfg = config.instance_attrs
    root = Path(out_dir) if out_dir is not None else Path(cfg.dataset_dir)
    crops = root / "crops"
    crops.mkdir(parents=True, exist_ok=True)

    relabels = load_relabels(cfg.relabels)
    lcon = labeling.labels_connect(config)
    try:
        vocab = head_vocabulary(lcon, exclude=cfg.exclude_classes)
        samples = collect_samples(lcon, block_frames=cfg.block_frames, vocab=vocab,
                                  merge_classes=cfg.merge_classes, relabels=relabels)
    finally:
        lcon.close()

    acfg = config.annotation
    kept: list[Sample] = []
    unrendered: list[dict[str, str]] = []
    for s in samples:
        dst = crops / f"{s.instance_key}.jpg"
        if dst.exists() and not force:
            kept.append(s)
            continue
        src = labeling.frame_path_for(config, s.dataset_id, s.camera_id, s.frame_file)
        jpeg, _sig = labeling.render_crop(
            src, s.bbox, pad=acfg.crop_pad, max_width=acfg.crop_max_width, cfg=acfg)
        if not jpeg:
            # render_crop's ONLY failure channel: a missing/torn frame, or a crop
            # so far into the banner that masking it would leave a grey square.
            # Both are routine (a re-ingest rmtrees frames out from under labels
            # that outlive them), so they are counted and reported, never raised.
            unrendered.append({"instance_key": s.instance_key, "frame": str(src)})
            continue
        dst.write_bytes(jpeg)
        kept.append(s)

    counts = {b: 0 for b in {s.block for s in kept}}
    for s in kept:
        counts[s.block] += 1
    assign = split_blocks(counts, val_fraction=cfg.val_fraction, seed=cfg.split_seed)

    per_head: dict[str, dict[str, dict[str, int]]] = {
        h: {"train": {c: 0 for c in cs}, "val": {c: 0 for c in cs}}
        for h, cs in vocab.items()
    }
    items = []
    for s in kept:
        split = assign.get(s.block, "train")
        for h, c in s.labels.items():
            per_head[h][split][c] += 1
        items.append({
            "instance_key": s.instance_key, "crop": f"crops/{s.instance_key}.jpg",
            "split": split, "block": s.block,
            "dataset_id": s.dataset_id, "camera_id": s.camera_id,
            "frame_file": s.frame_file, "frame_idx": s.frame_idx,
            "bbox": list(s.bbox), "labels": s.labels,
        })

    n_val = sum(1 for i in items if i["split"] == "val")
    summary = {
        "instances": len(items),
        "train": len(items) - n_val,
        "val": n_val,
        "val_fraction_actual": round(n_val / len(items), 4) if items else 0.0,
        "blocks": len(counts),
        "blocks_val": sum(1 for v in assign.values() if v == "val"),
        "unrendered": len(unrendered),
        "per_head": per_head,
    }
    manifest = {
        "version": 1,
        # Everything needed to reproduce this split, recorded beside it: a
        # manifest that cannot say how it was made cannot be compared with the
        # next one.
        "config": {
            "block_frames": cfg.block_frames,
            "val_fraction": cfg.val_fraction,
            "split_seed": cfg.split_seed,
            # What was rewritten on the way in, so a manifest can be told apart
            # from one built on the raw store.
            "merge_classes": dict(cfg.merge_classes),
            "exclude_classes": list(cfg.exclude_classes),
            "relabels": cfg.relabels,
            "relabels_applied": len(relabels),
            "crop_pad": acfg.crop_pad,
            "crop_max_width": acfg.crop_max_width,
            "mask_timestamp_banner": acfg.mask_timestamp_banner,
        },
        "heads": {h: list(cs) for h, cs in vocab.items()},
        "summary": summary,
        "unrendered": unrendered,
        "items": items,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Read a manifest by directory or by file path — callers have both."""
    p = Path(path)
    if p.is_dir():
        p = p / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8"))
