"""One moment, three surfaces, one number.

The scrubber speaks in INSTANTS — a timestamp snapped to the capture grid
(`_instant_expr`) — because the cameras are linked by real time, not by frame
number: clips start seconds apart and a camera that powers on late simply has
no rows for the earlier instants. `frame_idx` is a different thing entirely, a
per-camera counter running 0..~1400.

`crosstab` took that instant and filtered `f.frame_idx = ?`. Instants run in the
tens of millions, so the filter matched nothing, and the strips under the map
said "No cows in view at 08:47" while the map beside them drew the herd. **A
filter that matches nothing is indistinguishable from a quiet moment**, which is
why this shipped and why it needs a test rather than a careful reading.

What is pinned:

  - the three surfaces that describe one moment — the occupancy map
    (`area_counts`), the breakdown strips (`crosstab`) and the headline panel
    (`kpi_summary`) — count the SAME detections for the same instant;
  - an instant is never confused with a frame_idx, in either direction;
  - a genuinely empty instant still reads as empty, so the fix did not simply
    make the empty state unreachable;
  - the whole-day figures are untouched by any of it.

No pytest. Run either way:
    py -3.11 -m tests.test_instant_scope
    py -3.11 tests/test_instant_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from cownting import db, features  # noqa: E402

_FAILED = 0
BIN = 60.0


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


# Two cameras whose clips start 20 s apart — the reason instants exist at all.
# camera_02 also starts LATE, so it has no frame for the first instant.
T0 = pd.Timestamp("2025-07-03 08:00:00")
_MOMENTS = [T0, T0 + pd.Timedelta(minutes=1), T0 + pd.Timedelta(minutes=2)]


def _seed(con) -> None:
    frames, dets = [], []
    for cam, skew, start in (("cam1", 0, 0), ("cam2", 20, 1)):
        for i, t in enumerate(_MOMENTS):
            if i < start:
                continue                      # cam2 powers on late
            ts = t + pd.Timedelta(seconds=skew)
            path = f"/f/{cam}/{i:04d}.jpg"
            # frame_idx restarts per camera and is deliberately NOT the instant.
            frames.append({"dataset_id": "d1", "camera_id": cam, "frame_idx": i - start,
                           "frame_path": path, "processed": True, "ts": ts})
            n = {0: 2, 1: 3, 2: 0}[i]          # the last moment is genuinely empty
            for k in range(n):
                dets.append(dict(
                    dataset_id="d1", camera_id=cam, frame_path=path, ts=ts, time_bin=i,
                    score=0.9, bbox_x1=10.0 + 40 * k, bbox_y1=10.0,
                    bbox_x2=50.0 + 40 * k, bbox_y2=60.0, area_px=100.0,
                    posture="standing" if k % 2 else "lying",
                    region_id=f"{cam}::pen", under_panel=(k == 0),
                    attr_is_cow=True, attr_cow_p=0.99,
                    attr_sun="sun_exposure.shaded" if k else "sun_exposure.direct_sun",
                    attr_sun_p=0.8,
                    attr_behaviour=None if k == 2 else "behaviour.lying",
                    attr_behaviour_p=0.4 if k == 2 else 0.9,
                    attr_model_rev="r1"))
    db.insert_frames(con, pd.DataFrame(frames))
    db.insert_detections(con, pd.DataFrame(dets))
    db.upsert_dataset(con, "d1", date(2025, 7, 3), "Jul 03", status="localized")


def _instants(con) -> list[int]:
    return [int(r[0]) for r in con.execute(
        f"SELECT DISTINCT {db._instant_expr(BIN)} AS i FROM frames f ORDER BY i").fetchall()]


def _crosstab_total(con, instant=None) -> int:
    dfr, _, _ = db.crosstab(con, "posture", "sun", dataset_id="d1",
                            instant=instant, bin_seconds=BIN)
    return int(dfr["n"].sum()) if not dfr.empty else 0


def test_an_instant_is_not_a_frame_idx():
    """The two number spaces must not overlap in this fixture, or the test could
    pass while the confusion survives."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            idx = [int(r[0]) for r in con.execute(
                "SELECT DISTINCT frame_idx FROM frames ORDER BY 1").fetchall()]
            inst = _instants(con)
            check("frame_idx is small and per-camera", idx == [0, 1, 2], str(idx))
            check("instants are timestamp buckets, orders of magnitude larger",
                  min(inst) > 1_000_000, str(inst))
            check("the two spaces cannot be confused by accident",
                  not (set(idx) & set(inst)), f"{idx} vs {inst}")

            # THE BUG, stated directly: a frame_idx passed as an instant finds
            # nothing. It must not look like a quiet moment to any caller.
            check("a frame_idx used as an instant matches no detections",
                  _crosstab_total(con, instant=0) == 0,
                  "if this ever returns rows the two spaces have collided")
        finally:
            con.close()


def test_the_three_surfaces_agree_on_every_instant():
    """THE REGRESSION. The map, the strips and the headline panel each describe
    the same moment; if they disagree, at least one of them is lying to the
    person comparing them."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            for i, inst in enumerate(_instants(con)):
                # The detections the map draws at this instant, counted the way
                # the map's own query counts them.
                rows = con.execute(
                    f"""SELECT count(*) FROM detections d
                        JOIN frames f ON d.camera_id = f.camera_id
                                     AND d.frame_path = f.frame_path
                        WHERE {db._instant_expr(BIN)} = ?""", [inst]).fetchone()[0]
                xt = _crosstab_total(con, instant=inst)
                kp = db.kpi_summary(con, "d1", instant=inst, bin_seconds=BIN)
                check(f"instant {i}: crosstab agrees with the detections at that moment",
                      xt == rows, f"crosstab {xt} vs {rows}")
                check(f"instant {i}: the headline panel agrees too",
                      kp["detections"] == rows, f"kpi {kp['detections']} vs {rows}")
        finally:
            con.close()


def test_a_genuinely_empty_instant_still_reads_as_empty():
    """The fix must not make the empty state unreachable — the third moment has
    no cows on either camera and has to say so."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            last = _instants(con)[-1]
            check("no detections at the empty moment", _crosstab_total(con, instant=last) == 0)
            kp = db.kpi_summary(con, "d1", instant=last, bin_seconds=BIN)
            check("...and the panel says zero cows", kp["detections"] == 0, str(kp["detections"]))
            check("...while still reporting the cameras that were looking",
                  kp["valid_frames"] == 2, str(kp["valid_frames"]))
        finally:
            con.close()


def test_the_panel_counts_cameras_not_frames_at_an_instant():
    """`valid_frames` at an instant is "how many cameras reported", so
    cows_per_frame reads as cows per camera. The first moment predates camera_02,
    which is the case that proves it is not just a constant."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            inst = _instants(con)
            first = db.kpi_summary(con, "d1", instant=inst[0], bin_seconds=BIN)
            second = db.kpi_summary(con, "d1", instant=inst[1], bin_seconds=BIN)
            check("only camera_01 was up for the first moment",
                  first["valid_frames"] == 1, str(first["valid_frames"]))
            check("both cameras for the second", second["valid_frames"] == 2,
                  str(second["valid_frames"]))
            check("cows per camera is computed against that, not against the day",
                  second["cows_per_frame"] == round(second["detections"] / 2, 2),
                  f'{second["cows_per_frame"]} from {second["detections"]}/2')
        finally:
            con.close()


def test_the_model_split_is_scoped_too_and_the_whole_day_is_untouched():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            inst = _instants(con)
            day = db.kpi_summary(con, "d1")
            mom = db.kpi_summary(con, "d1", instant=inst[1], bin_seconds=BIN)
            check("the whole day sees every detection", day["detections"] == 8,
                  str(day["detections"]))
            check("the moment sees only its own", mom["detections"] == 6,
                  str(mom["detections"]))
            check("attrs are scoped with them",
                  mom["attrs"]["scored"] == 6 and day["attrs"]["scored"] == 8,
                  f'{mom["attrs"]["scored"]} / {day["attrs"]["scored"]}')
            check("...including the unsure bucket, which must not leak the day's total",
                  mom["attrs"]["behaviour"]["unsure"] == 2
                  and day["attrs"]["behaviour"]["unsure"] == 2,
                  str(mom["attrs"]["behaviour"]))
            check("the day figures are byte-identical to a call with no instant",
                  db.kpi_summary(con, "d1") == day)
        finally:
            con.close()


def test_a_cow_in_no_count_area_is_counted_not_dropped():
    """Most of the herd stands outside every drawn count area — 53.8% of the
    corpus on 2026-09-18, and 100% of two whole days whose areas were never
    drawn. A breakdown BY count area used to discard them silently, so it
    reported 4 cows where the map beside it drew 15: a discrepancy
    indistinguishable from a bug, and on those two days a breakdown of nothing.

    They are their own group now, so the pivot reconciles with every other
    surface and the uncovered share is a number on the screen."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            # Half the fixture's detections are moved outside every area.
            con.execute("UPDATE detections SET region_id = NULL WHERE bbox_x1 >= 50")
            total = con.execute("SELECT count(*) FROM detections").fetchone()[0]
            inside = con.execute(
                "SELECT count(region_id) FROM detections").fetchone()[0]
            check("the fixture has cows both inside and outside an area",
                  0 < inside < total, f"{inside} of {total}")

            dfr, dom, _ = db.crosstab(con, "region", "behaviour", dataset_id="d1")
            got = int(dfr["n"].sum())
            check("a breakdown by count area keeps every cow",
                  got == total, f"{got} vs {total}")
            check("...with the uncovered ones in their own named group",
                  features.REGION_NONE in [str(x) for x in dom], str(dom))
            n_outside = int(dfr[dfr["primary"] == features.REGION_NONE]["n"].sum())
            check("...carrying exactly the cows no area covers",
                  n_outside == total - inside, f"{n_outside} vs {total - inside}")

            check("and the helper the map captions with agrees",
                  db.detections_outside_areas(con, "d1") == total - inside,
                  str(db.detections_outside_areas(con, "d1")))

            # The case that used to produce an empty chart rather than a finding.
            con.execute("UPDATE detections SET region_id = NULL")
            dfr2, dom2, _ = db.crosstab(con, "region", "behaviour", dataset_id="d1")
            check("a day with NO areas drawn breaks down to one honest group",
                  int(dfr2["n"].sum()) == total and [str(x) for x in dom2] == [features.REGION_NONE],
                  f'{int(dfr2["n"].sum())} in {dom2}')
        finally:
            con.close()


def test_the_uncovered_count_is_scoped_to_the_instant_too():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con); _seed(con)
            con.execute("UPDATE detections SET region_id = NULL WHERE bbox_x1 >= 50")
            day = db.detections_outside_areas(con, "d1")
            inst = _instants(con)
            per = [db.detections_outside_areas(con, "d1", instant=i, bin_seconds=BIN)
                   for i in inst]
            check("the per-instant counts sum to the day's", sum(per) == day,
                  f"{per} sums to {sum(per)}, day is {day}")
            check("an empty instant contributes none", per[-1] == 0, str(per))
        finally:
            con.close()


def main():
    print("=== test_instant_scope ===")
    test_an_instant_is_not_a_frame_idx()
    test_the_three_surfaces_agree_on_every_instant()
    test_a_genuinely_empty_instant_still_reads_as_empty()
    test_the_panel_counts_cameras_not_frames_at_an_instant()
    test_the_model_split_is_scoped_too_and_the_whole_day_is_untouched()
    test_a_cow_in_no_count_area_is_counted_not_dropped()
    test_the_uncovered_count_is_scoped_to_the_instant_too()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
