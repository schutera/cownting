"""The outline backfill and the polygon reduction it depends on.

`cownting remask` re-runs the segmenter over already-processed footage and writes
`mask_poly` / `mask_parts` onto EXISTING detection rows. Its entire safety
argument is a negative one — that it changes nothing else — because `bbox_*`,
`frame_path`, `camera_id`, `dataset_id` and the ordinal are key material for
`labels_db.instance_key`, and `score` / `area_px` / `ground_px_*` / `ts` are the
ORDERING columns of the window that ordinal comes from. A backfill that
"improved" any of them from the fresh prediction would renumber ordinals, re-mint
every key, and detach every label already collected. Nothing else in the suite
pins that.

NO GPU AND NO WEIGHTS. `build_segmenter` is monkeypatched with a stub returning
synthetic instances, the same shape tests/test_upload_status.py already uses for
the pipeline stages, so this runs on a bare CI box.

No pytest. Run either way:
    python -m tests.test_remask
    python tests/test_remask.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402

from cownting import db, labels_db, pipeline  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402
from cownting.detect.base import Instance  # noqa: E402
from cownting.detect.geometry import mask_to_polygon  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else "")
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


# One dataset, one camera, two frames, three detections. The boxes are far apart
# so IoU matching is unambiguous; the half-pixel coordinates are there because
# the key quantises them and a backfill must not disturb that.
_DS, _CAM = "2026-07-03", "camera_01"
_BOXES = [
    (40.5, 60.0, 140.5, 200.0),
    (300.2, 40.7, 420.9, 160.1),
    (150.0, 250.0, 260.0, 380.0),
]


def _mk(d: str) -> Config:
    art = os.path.join(d, "artifacts")
    frames_dir = os.path.join(art, _DS, "frames", _CAM)
    os.makedirs(frames_dir, exist_ok=True)
    paths = []
    for i in (1, 2):
        p = os.path.join(frames_dir, f"{i:08d}.jpg")
        # A real JPEG: remask cv2.imreads it, and the shape guard compares the
        # mask's dimensions against the decoded frame's.
        cv2.imwrite(p, np.full((480, 640, 3), 120, np.uint8))
        paths.append(p)

    con = db.connect(os.path.join(d, "cownting.duckdb"))
    db.init_db(con)
    db.upsert_dataset(con, _DS, date(2026, 7, 3), "Jul 03, 2026", status="localized")
    db.insert_frames(con, pd.DataFrame([
        {"dataset_id": _DS, "camera_id": _CAM, "frame_idx": i,
         "ts": datetime(2026, 7, 3, 6, i), "frame_path": p, "processed": True}
        for i, p in enumerate(paths, start=1)
    ]))
    db.insert_detections(con, pd.DataFrame([
        {"dataset_id": _DS, "camera_id": _CAM, "ts": datetime(2026, 7, 3, 6, 1),
         "frame_path": paths[0], "score": 0.9, "area_px": 1234.0,
         "ground_px_x": 90.0, "ground_px_y": 200.0,
         "bbox_x1": _BOXES[0][0], "bbox_y1": _BOXES[0][1],
         "bbox_x2": _BOXES[0][2], "bbox_y2": _BOXES[0][3]},
        {"dataset_id": _DS, "camera_id": _CAM, "ts": datetime(2026, 7, 3, 6, 1),
         "frame_path": paths[0], "score": 0.8, "area_px": 999.0,
         "ground_px_x": 360.0, "ground_px_y": 160.0,
         "bbox_x1": _BOXES[1][0], "bbox_y1": _BOXES[1][1],
         "bbox_x2": _BOXES[1][2], "bbox_y2": _BOXES[1][3]},
        {"dataset_id": _DS, "camera_id": _CAM, "ts": datetime(2026, 7, 3, 6, 2),
         "frame_path": paths[1], "score": 0.7, "area_px": 555.0,
         "ground_px_x": 200.0, "ground_px_y": 380.0,
         "bbox_x1": _BOXES[2][0], "bbox_y1": _BOXES[2][1],
         "bbox_x2": _BOXES[2][2], "bbox_y2": _BOXES[2][3]},
    ]))
    con.close()
    return Config(
        cameras=[CameraCfg(id=_CAM, video="unused.mp4")],
        auth=AuthCfg(enabled=False),
        paths=PathsCfg(
            db_path=os.path.join(d, "cownting.duckdb"),
            labels_db_path=os.path.join(d, "labels.duckdb"),
            backups_dir=os.path.join(d, "backups"),
            artifacts_dir=art,
            count_areas=os.path.join(d, "areas.json"),
        ),
    )


class _StubSegmenter:
    """Returns one instance per box in `boxes`, each with a filled elliptical mask
    inside its own box — the shape a real segmenter produces, without the model."""

    def __init__(self, boxes, jitter: float = 0.0):
        self.boxes = boxes
        self.jitter = jitter

    def segment(self, image):
        h, w = image.shape[:2]
        out = []
        for (x1, y1, x2, y2) in self.boxes:
            x1, y1, x2, y2 = x1 + self.jitter, y1, x2 + self.jitter, y2
            m = np.zeros((h, w), bool)
            cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
            rx, ry = max(2, int((x2 - x1) / 2) - 1), max(2, int((y2 - y1) / 2) - 1)
            yy, xx = np.ogrid[:h, :w]
            m[((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0] = True
            out.append(Instance(bbox=(x1, y1, x2, y2), score=0.5, area_px=float(m.sum()),
                                ground_px=(float(cx), float(y2)), mask=m))
        return out


def _install(monkey_boxes, jitter: float = 0.0):
    """Swap the segmenter factory. Returned undo restores the real one."""
    real = pipeline.build_segmenter
    pipeline.build_segmenter = lambda *a, **k: _StubSegmenter(monkey_boxes, jitter)
    return lambda: setattr(pipeline, "build_segmenter", real)


def _keys(db_path: str) -> set:
    con = db.connect(db_path, read_only=True)
    try:
        sql = (f"SELECT {labels_db.instance_key_sql('r')} FROM ("
               f"SELECT d.*, {labels_db.instance_ordinal_sql('d')} AS ordinal "
               "FROM detections d) r")
        return {r[0] for r in con.execute(sql).fetchall()}
    finally:
        con.close()


def _rows(db_path: str) -> list:
    con = db.connect(db_path, read_only=True)
    try:
        return con.execute(
            "SELECT detection_id, score, area_px, ground_px_x, ground_px_y, ts, "
            "bbox_x1, bbox_y1, bbox_x2, bbox_y2, mask_poly, mask_parts "
            "FROM detections ORDER BY detection_id").fetchall()
    finally:
        con.close()


def test_mask_to_polygon_is_full_frame_and_bounded():
    """The reduction that produces every stored outline."""
    m = np.zeros((200, 300), bool)
    m[40:90, 100:180] = True
    got = mask_to_polygon(m, (100, 40, 180, 90))
    check("a solid blob yields a polygon and a part count", got is not None)
    poly, parts = got
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    check("the outline is in FULL-FRAME px, on the blob",
          99 <= min(xs) <= 102 and 178 <= max(xs) <= 181
          and 39 <= min(ys) <= 42 and 88 <= max(ys) <= 91,
          f"x {min(xs)}..{max(xs)} y {min(ys)}..{max(ys)}")
    check("a single blob is one part", parts == 1, str(parts))

    two = np.zeros((200, 300), bool)
    two[10:40, 10:40] = True
    two[100:160, 100:190] = True
    poly2, parts2 = mask_to_polygon(two)
    check("two blobs report two parts", parts2 == 2, str(parts2))
    check("...and the LARGEST is the one kept",
          min(p[0] for p in poly2) > 90, str(min(p[0] for p in poly2)))

    check("a speck is not an animal", mask_to_polygon(np.zeros((50, 50), bool)) is None)
    sliver = np.zeros((50, 50), bool)
    sliver[10, 5:35] = True
    check("a one-pixel sliver collapses below 3 points and is refused",
          mask_to_polygon(sliver) is None)

    # The floor and cap are restated in detect/ rather than imported from the
    # label store; if they ever drift, a served polygon stops round-tripping
    # through /api/label/mask-fix.
    from cownting.detect import geometry as g
    check("the polygon floor matches the store's",
          g._MIN_POLY_PTS == labels_db.MASK_MIN_POINTS,
          f"{g._MIN_POLY_PTS} vs {labels_db.MASK_MIN_POINTS}")
    check("the polygon cap matches the store's",
          g._MAX_POLY_PTS == labels_db.MASK_MAX_POINTS,
          f"{g._MAX_POLY_PTS} vs {labels_db.MASK_MAX_POINTS}")

    # A ragged mask must still produce a STORABLE shape: the cap is enforced by
    # escalating epsilon, never by returning something the submit route rejects.
    rng = np.random.default_rng(7)
    ragged = np.zeros((900, 1600), bool)
    yy, xx = np.ogrid[:900, :1600]
    ragged[((xx - 800) / 600.0) ** 2 + ((yy - 450) / 300.0) ** 2 <= 1.0] = True
    noise = rng.random((900, 1600)) < 0.25
    ragged ^= noise & (((xx - 800) / 620.0) ** 2 + ((yy - 450) / 320.0) ** 2 <= 1.0)
    big = mask_to_polygon(ragged)
    check("a ragged mask still yields a polygon within the cap",
          big is not None and len(big[0]) <= labels_db.MASK_MAX_POINTS,
          str(len(big[0]) if big else None))

    # The guard that stops a mis-configured segmenter writing outlines at the
    # wrong scale — silent corruption, since a backfill renders no overlay.
    raised = False
    try:
        mask_to_polygon(m, expect_shape=(1080, 1920))
    except ValueError:
        raised = True
    check("a mask that is not frame-shaped is REFUSED, not rescaled", raised)


def test_remask_writes_only_the_outline_and_never_moves_a_key():
    """THE safety property of the backfill."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _mk(d)
        before_keys = _keys(cfg.paths.db_path)
        before_rows = _rows(cfg.paths.db_path)
        check("the fixture starts with no outlines",
              all(r[10] is None for r in before_rows))

        # Boxes shifted 2px and re-scored: exactly the drift a re-prediction
        # brings, and precisely what must NOT be written back.
        undo = _install([(x1 + 2, y1, x2 + 2, y2) for (x1, y1, x2, y2) in _BOXES])
        try:
            stats = pipeline.remask(cfg)
        finally:
            undo()
        check("every detection matched", stats["matched"] == 3 and stats["unmatched"] == 0,
              str(stats))
        check("both frames were re-run", stats["frames"] == 2, str(stats))

        after_rows = _rows(cfg.paths.db_path)
        check("every outline is now stored",
              all(r[10] for r in after_rows), str([bool(r[10]) for r in after_rows]))
        check("...and the part count with it",
              all(r[11] == 1 for r in after_rows), str([r[11] for r in after_rows]))

        # The negative half, column by column: identity and ordering material is
        # byte-identical. This is what makes the pass safe to run over a corpus
        # that already carries labels.
        for i, (b, a) in enumerate(zip(before_rows, after_rows)):
            check(f"row {i}: score/area/ground/ts/bbox untouched by the backfill",
                  b[:10] == a[:10], f"{b[:10]} -> {a[:10]}")
        check("NO instance_key moved — every label stays attached",
              _keys(cfg.paths.db_path) == before_keys)


def test_remask_is_resumable_and_reports_what_it_could_not_match():
    with tempfile.TemporaryDirectory() as d:
        cfg = _mk(d)
        undo = _install(_BOXES)
        try:
            first = pipeline.remask(cfg)
            # A second pass has nothing to do: the frame selection only picks
            # frames with a detection still missing an outline, which is what
            # makes a killed multi-hour run resumable rather than restarted.
            second = pipeline.remask(cfg)
        finally:
            undo()
        check("the first pass does the work", first["matched"] == 3, str(first))
        check("the second pass is a no-op — the pass is resumable",
              second["frames"] == 0 and second["matched"] == 0, str(second))

    with tempfile.TemporaryDirectory() as d:
        cfg = _mk(d)
        # The segmenter finds only ONE of the three animals — weights drift, or a
        # cow that is no longer detected. The rest must stay NULL and be reported.
        undo = _install([_BOXES[0]])
        try:
            stats = pipeline.remask(cfg)
        finally:
            undo()
        check("only the matched detection gets an outline",
              stats["matched"] == 1 and stats["unmatched"] == 2, str(stats))
        rows = _rows(cfg.paths.db_path)
        check("the unmatched rows keep NULL rather than a guess",
              sum(1 for r in rows if r[10] is None) == 2,
              str([bool(r[10]) for r in rows]))

    with tempfile.TemporaryDirectory() as d:
        cfg = _mk(d)
        # Boxes far from the stored ones: below the IoU floor, so nothing is
        # attached. A backfill must never bind an outline to the wrong animal.
        undo = _install([(x1 + 300, y1 + 200, x2 + 300, y2 + 200)
                         for (x1, y1, x2, y2) in _BOXES])
        try:
            stats = pipeline.remask(cfg)
        finally:
            undo()
        check("a mask that does not overlap its row is not attached",
              stats["matched"] == 0, str(stats))


def test_remask_scopes_and_bbox_iou():
    check("identical boxes are IoU 1", abs(pipeline._bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) - 1.0) < 1e-9)
    check("disjoint boxes are IoU 0", pipeline._bbox_iou((0, 0, 10, 10), (50, 50, 60, 60)) == 0.0)
    check("half-overlap is 1/3", abs(pipeline._bbox_iou((0, 0, 10, 10), (5, 0, 15, 10)) - (50 / 150)) < 1e-9)
    check("a degenerate box cannot divide by zero",
          pipeline._bbox_iou((0, 0, 0, 0), (0, 0, 10, 10)) == 0.0)

    with tempfile.TemporaryDirectory() as d:
        cfg = _mk(d)
        undo = _install(_BOXES)
        try:
            scoped = pipeline.remask(cfg, camera_id="camera_99")
        finally:
            undo()
        check("scoping to an absent camera does nothing",
              scoped["frames"] == 0 and scoped["matched"] == 0, str(scoped))


def main():
    print("=== test_remask ===")
    test_mask_to_polygon_is_full_frame_and_bounded()
    test_remask_writes_only_the_outline_and_never_moves_a_key()
    test_remask_is_resumable_and_reports_what_it_could_not_match()
    test_remask_scopes_and_bbox_iou()
    print("===================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
