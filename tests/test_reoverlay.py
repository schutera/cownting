"""Repainting overlays from the database, so they match the current model.

An overlay is a CACHE of a model's opinion. `segment` burns the masks into a
JPEG before the crop model has looked at a single crop, so at the moment the
picture is made, nothing knows which of those blobs are cows; the verdict lands
minutes later and changes again with every retrain. This pass rebuilds the image
from the stored outline and `attr_is_cow`.

It overwrites 18 GB of images that are only recoverable by an hours-long
re-segmentation, which is why the two rules below get pixel-level tests rather
than a careful reading:

  - **NULL IS NOT FALSE.** `attr_is_cow` is NULL for every never-scored
    detection — most of the corpus, most of the time. Hiding those would blank
    the masks off the entire dashboard. Only an explicit FALSE hides.
  - **THE PALETTE IS POSITIONAL.** `render_overlay` colours by `enumerate`
    index, so a hidden detection must still consume its index. Pre-filtering the
    list instead recolours every animal after it, on every frame in the corpus.

Assertions probe actual pixels at each polygon's centre, because "did this mask
get drawn" is not observable from the return value.

No pytest. Run either way:
    py -3.11 -m tests.test_reoverlay
    py -3.11 tests/test_reoverlay.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from cownting import db, pipeline  # noqa: E402
from cownting.config import CameraCfg, Config, PathsCfg  # noqa: E402

_FAILED = 0
DS, CAM = "2025-07-03", "camera_01"
_H, _W = 240, 640


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


@contextlib.contextmanager
def tmpdir():
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _square(cx: int) -> list:
    """A 40x40 box at x=cx on a mid-grey frame."""
    return [[cx - 20, 80], [cx + 20, 80], [cx + 20, 120], [cx - 20, 120]]


def _centre(poly) -> tuple[int, int]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (sum(xs) // len(xs), sum(ys) // len(ys))


def _fixture(d: str, cows: list[bool | None], rev: str | None = "r1") -> Config:
    """One frame, one detection per entry in `cows` (True/False/None = the
    attr_is_cow verdict), each a distinct square. Writes a real frame JPEG and a
    real overlay JPEG so the pass has something to overwrite."""
    art = os.path.join(d, "artifacts")
    fdir = os.path.join(art, DS, "frames", CAM)
    odir = os.path.join(art, DS, "overlays", CAM)
    os.makedirs(fdir, exist_ok=True)
    os.makedirs(odir, exist_ok=True)
    frame_path = os.path.join(fdir, "00000000.jpg")
    overlay_path = os.path.join(odir, "00000000.jpg")
    grey = np.full((_H, _W, 3), 128, dtype=np.uint8)
    cv2.imwrite(frame_path, grey)
    cv2.imwrite(overlay_path, grey)   # stand-in for what `segment` burned in

    dbp = os.path.join(d, "c.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    con.execute("INSERT INTO frames (dataset_id, camera_id, frame_idx, ts, frame_path, "
                "overlay_path, processed) VALUES (?,?,?,?,?,?,TRUE)",
                [DS, CAM, 0, pd.Timestamp("2025-07-03 10:00"), frame_path, overlay_path])
    rows = []
    for i, verdict in enumerate(cows):
        poly = _square(80 + i * 120)
        rows.append(dict(dataset_id=DS, camera_id=CAM, frame_path=frame_path,
                         ts=pd.Timestamp("2025-07-03 10:00"), time_bin=0, score=0.9,
                         bbox_x1=float(poly[0][0]), bbox_y1=80.0,
                         bbox_x2=float(poly[1][0]), bbox_y2=120.0,
                         area_px=1600.0, posture="standing",
                         ground_px_x=float(_centre(poly)[0]), ground_px_y=125.0,
                         mask_poly=json.dumps(poly), mask_parts=1,
                         attr_is_cow=verdict, attr_cow_p=None if verdict is None else 0.9,
                         attr_model_rev=None if verdict is None else rev))
    db.insert_detections(con, pd.DataFrame(rows))
    db.upsert_dataset(con, DS, date(2025, 7, 3), "Jul 03", status="localized")
    con.close()
    return Config(cameras=[CameraCfg(id=CAM, video="x.mp4")],
                  paths=PathsCfg(db_path=dbp, artifacts_dir=art,
                                 labels_db_path=os.path.join(d, "l.duckdb"),
                                 backups_dir=os.path.join(d, "b")))


def _painted(cfg: Config, polys: list) -> list[bool]:
    """Did each polygon get a mask? True where the overlay differs from flat grey
    at that polygon's centre."""
    art = cfg.paths.artifacts_dir
    img = cv2.imread(os.path.join(art, DS, "overlays", CAM, "00000000.jpg"))
    out = []
    for poly in polys:
        x, y = _centre(poly)
        px = img[y, x]
        out.append(bool(abs(int(px[0]) - 128) > 8 or abs(int(px[1]) - 128) > 8
                        or abs(int(px[2]) - 128) > 8))
    return out


def test_null_is_not_false():
    """THE RULE THAT PROTECTS THE CORPUS. One of each verdict: the rejected one
    loses its mask, the accepted one keeps it, and the NEVER-SCORED one keeps it
    too. Getting this wrong blanks every mask on every unscored day."""
    with tmpdir() as d:
        cfg = _fixture(d, [True, False, None])
        polys = [_square(80), _square(200), _square(320)]
        stats = pipeline.reoverlay(cfg)
        check("the frame was repainted", stats["painted"] == 1, str(stats))
        check("one mask was hidden", stats["hidden"] == 1, str(stats))
        got = _painted(cfg, polys)
        check("attr_is_cow TRUE keeps its mask", got[0] is True, str(got))
        check("attr_is_cow FALSE loses it", got[1] is False, str(got))
        check("attr_is_cow NULL (never scored) KEEPS it", got[2] is True, str(got))


def test_the_palette_does_not_shift_when_one_is_hidden():
    """The colour is chosen by position. If a hidden detection is filtered out of
    the list instead of skipped in place, every animal after it changes colour —
    across the whole corpus, silently."""
    with tmpdir() as d1, tmpdir() as d2:
        polys = [_square(80), _square(200), _square(320)]
        # All three visible.
        cfg_all = _fixture(d1, [True, True, True])
        pipeline.reoverlay(cfg_all)
        img_all = cv2.imread(os.path.join(cfg_all.paths.artifacts_dir, DS, "overlays",
                                          CAM, "00000000.jpg"))
        # Middle one rejected.
        cfg_mid = _fixture(d2, [True, False, True])
        pipeline.reoverlay(cfg_mid)
        img_mid = cv2.imread(os.path.join(cfg_mid.paths.artifacts_dir, DS, "overlays",
                                          CAM, "00000000.jpg"))
        x0, y0 = _centre(polys[0])
        x2, y2 = _centre(polys[2])
        check("the first cow keeps its colour",
              tuple(img_all[y0, x0]) == tuple(img_mid[y0, x0]),
              f"{img_all[y0, x0]} vs {img_mid[y0, x0]}")
        check("the THIRD cow keeps its colour even though the second vanished",
              tuple(img_all[y2, x2]) == tuple(img_mid[y2, x2]),
              f"{img_all[y2, x2]} vs {img_mid[y2, x2]}")


def test_the_ground_dot_goes_with_the_mask():
    """A red dot still sitting where a rejected mask used to be is still
    'showing something there'."""
    with tmpdir() as d:
        cfg = _fixture(d, [False])
        pipeline.reoverlay(cfg)
        img = cv2.imread(os.path.join(cfg.paths.artifacts_dir, DS, "overlays",
                                      CAM, "00000000.jpg"))
        # The ground point sits at y=125, just under the box.
        region = img[120:131, 55:106]
        reds = int(((region[:, :, 2] > 180) & (region[:, :, 0] < 80)).sum())
        check("no ground dot is drawn for a rejected detection", reds == 0, f"{reds} red px")


def test_it_is_idempotent_and_re_score_makes_it_dirty_again():
    """The rev stamp is what makes the pass free to re-run and automatic after a
    score. Without it this is either a manual chore or a corpus-wide repaint
    every time anything touches the DB."""
    with tmpdir() as d:
        cfg = _fixture(d, [True, False])
        first = pipeline.reoverlay(cfg)
        check("the first pass paints", first["painted"] == 1, str(first))
        second = pipeline.reoverlay(cfg)
        check("the second pass is a no-op", second["painted"] == 0, str(second))
        check("...because the frame is no longer stale", second["frames"] == 0, str(second))

        # A new model disagrees: same rows, new rev.
        con = db.connect(cfg.paths.db_path)
        con.execute("UPDATE detections SET attr_is_cow = TRUE, attr_model_rev = 'r2'")
        con.close()
        third = pipeline.reoverlay(cfg)
        check("a re-score makes the frame dirty again", third["painted"] == 1, str(third))
        check("...and the formerly hidden mask is back",
              all(_painted(cfg, [_square(80), _square(200)])), str(_painted(cfg, [_square(200)])))

        forced = pipeline.reoverlay(cfg, force=True)
        check("--force repaints regardless of the rev", forced["painted"] == 1, str(forced))


def test_it_is_not_tied_to_the_gate_flag():
    """`gate_enabled` decides what COUNTS; this decides what is PAINTED. Nobody
    opts in to not having a false positive tinted as a cow."""
    with tmpdir() as d:
        cfg = _fixture(d, [True, False])
        polys = [_square(80), _square(200)]
        try:
            db.set_gate(False)
            pipeline.reoverlay(cfg)
            off = _painted(cfg, polys)
            db.set_gate(True)
            pipeline.reoverlay(cfg, force=True)
            on = _painted(cfg, polys)
            check("the rejected mask is hidden with the gate OFF", off[1] is False, str(off))
            check("...and identically with the gate ON", on == off, f"{off} vs {on}")
        finally:
            db.set_gate(False)


def test_a_missing_frame_keeps_its_overlay_rather_than_destroying_it():
    """The raw frame can be gone — a clipped camera, a pruned day. Losing the
    source is not a reason to blank the only picture that remains."""
    with tmpdir() as d:
        cfg = _fixture(d, [True, False])
        os.unlink(os.path.join(cfg.paths.artifacts_dir, DS, "frames", CAM, "00000000.jpg"))
        before = open(os.path.join(cfg.paths.artifacts_dir, DS, "overlays", CAM,
                                   "00000000.jpg"), "rb").read()
        stats = pipeline.reoverlay(cfg)
        after = open(os.path.join(cfg.paths.artifacts_dir, DS, "overlays", CAM,
                                  "00000000.jpg"), "rb").read()
        check("it is counted as missing, not painted",
              stats["missing_frames"] == 1 and stats["painted"] == 0, str(stats))
        check("...and the existing overlay is untouched", before == after)


def test_it_never_touches_identity_columns():
    """Same contract as remask: this writes IMAGES and one frames column. A
    detection's identity — the bbox, the ordinal's ordering columns — is key
    material for instance_key, and moving any of it orphans collected labels."""
    with tmpdir() as d:
        cfg = _fixture(d, [True, False, None])
        con = db.connect(cfg.paths.db_path)
        before = con.execute("SELECT detection_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, "
                             "score, area_px, ground_px_x, ground_px_y, ts, mask_poly, "
                             "attr_is_cow, attr_model_rev FROM detections "
                             "ORDER BY detection_id").fetchall()
        con.close()
        pipeline.reoverlay(cfg)
        con = db.connect(cfg.paths.db_path)
        after = con.execute("SELECT detection_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, "
                            "score, area_px, ground_px_x, ground_px_y, ts, mask_poly, "
                            "attr_is_cow, attr_model_rev FROM detections "
                            "ORDER BY detection_id").fetchall()
        revs = con.execute("SELECT overlay_rev FROM frames").fetchall()
        con.close()
        check("not one detection column moved", before == after)
        check("only frames.overlay_rev was written", revs == [("r1",)], str(revs))


def main():
    print("=== test_reoverlay ===")
    test_null_is_not_false()
    test_the_palette_does_not_shift_when_one_is_hidden()
    test_the_ground_dot_goes_with_the_mask()
    test_it_is_idempotent_and_re_score_makes_it_dirty_again()
    test_it_is_not_tied_to_the_gate_flag()
    test_a_missing_frame_keeps_its_overlay_rather_than_destroying_it()
    test_it_never_touches_identity_columns()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
