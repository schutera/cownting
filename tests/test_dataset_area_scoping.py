"""Per-dataset count/panel area scoping (the "global orthophoto + per-dataset areas" model change).

Proves that count/panel areas are stored and applied PER DATASET (keyed by
dataset_id), not globally by camera name. Two uploads that both use "camera_01"
but frame the scene differently get their OWN polygons, and editing one dataset's
areas never disturbs another dataset's detections. The orthophoto stays global and
is not exercised here.

Builds a REAL DuckDB on a tempfile plus REAL per-dataset area files under a temp
<data>/areas/<dataset_id>/ tree -- a *sibling* of artifacts/, so pipeline.ingest's
rmtree of artifacts/<dataset_id>/ on re-ingest can never delete them -- then drives
cownting.pipeline.localize and reads the assignments back through cownting.db.

No pytest. Run either way:
    .venv/bin/python -m tests.test_dataset_area_scoping
    .venv/bin/python tests/test_dataset_area_scoping.py

Prints each check and a final PASS; sys.exit(1) on any failure.

Note: each test builds its own world and never holds a DuckDB connection open across
a pipeline.localize call (localize opens its own short-lived connection), so the two
never contend for the single-writer file handle.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Allow `python tests/test_dataset_area_scoping.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, pipeline  # noqa: E402
from cownting.config import Config, PathsCfg  # noqa: E402
from cownting.scene import regions  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


# --------------------------------------------------------------------------- fixtures
# Two datasets, SAME camera name ("camera_01"), DIFFERENT framing: the ground point
# that lands in dataset A's area lands nowhere in dataset B's, and vice versa.
DS_A = "2025-06-28"
DS_B = "2025-07-01"

# Ground points (image space). x is unique within a dataset, so we key rows by it.
P_LEFT = (5.0, 5.0)     # inside LEFT_SQUARE, outside RIGHT_SQUARE
P_RIGHT = (25.0, 5.0)   # inside RIGHT_SQUARE, outside LEFT_SQUARE

# Non-overlapping 10x10 squares; neither contains the other's point.
LEFT_SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]    # covers P_LEFT
RIGHT_SQUARE = [[20.0, 0.0], [30.0, 0.0], [30.0, 10.0], [20.0, 10.0]]  # covers P_RIGHT


def _config(d: str) -> Config:
    """A Config whose paths all live under the tempdir, so dataset_area_path resolves
    to <tmp>/data/areas/<dataset_id>/ (sibling of <tmp>/data/artifacts/)."""
    data = os.path.join(d, "data")
    return Config(
        cameras=[],
        paths=PathsCfg(
            artifacts_dir=os.path.join(data, "artifacts"),
            db_path=os.path.join(data, "cownting.duckdb"),
            count_areas=os.path.join(data, "count_areas.json"),
            panel_areas=os.path.join(data, "panel_areas.json"),
        ),
    )


def _area(area_id: str, polygon) -> dict:
    return {"id": area_id, "name": area_id.title(),
            "camera_polygon": polygon, "ortho_polygon": []}


def _insert(con, dataset_id: str, points) -> None:
    """Insert camera_01 detections for one dataset at the given ground points."""
    rows = [
        {"dataset_id": dataset_id, "camera_id": "camera_01",
         "ts": datetime(2025, 1, 1, 0, i, 0),
         "ground_px_x": float(x), "ground_px_y": float(y)}
        for i, (x, y) in enumerate(points)
    ]
    db.insert_detections(con, pd.DataFrame(rows))


def _seed_two(config: Config) -> None:
    """A real DB with datasets A and B, each holding a P_LEFT and a P_RIGHT camera_01
    detection. Both datasets are registered in the datasets dimension so the whole-DB
    fan-out can discover them. Connection is closed before we return."""
    con = db.connect(config.paths.db_path)
    try:
        db.init_db(con)
        db.upsert_dataset(con, DS_A, date(2025, 6, 28), "A")
        db.upsert_dataset(con, DS_B, date(2025, 7, 1), "B")
        _insert(con, DS_A, [P_LEFT, P_RIGHT])
        _insert(con, DS_B, [P_LEFT, P_RIGHT])
    finally:
        con.close()


def _write_areas(config: Config, dataset_id: str, kind: str, mapping) -> None:
    """Write a per-dataset area file at exactly the path localize will read from."""
    regions.save_count_areas(regions.dataset_area_path(config, dataset_id, kind), mapping)


def _flag(v):
    """Normalize a DuckDB BOOLEAN cell to True / False / None (guards against numpy bools)."""
    return None if v is None else bool(v)


def _by_x(config: Config, dataset_id: str) -> dict:
    """{ground_px_x -> {region_id, under_panel, panel_id}} for one dataset. Opens and
    closes its own connection so nothing is held open across a localize call."""
    con = db.connect(config.paths.db_path)
    try:
        rows = con.execute(
            "SELECT ground_px_x, region_id, under_panel, panel_id "
            "FROM detections WHERE dataset_id = ? ORDER BY ground_px_x",
            [dataset_id],
        ).fetchall()
    finally:
        con.close()
    return {round(float(r[0]), 3): {"region_id": r[1],
                                    "under_panel": _flag(r[2]),
                                    "panel_id": r[3]} for r in rows}


# --------------------------------------------------------------------------- 1+2: per-dataset isolation
def test_per_dataset_isolation():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed_two(config)
        # Each dataset gets its OWN camera_01 polygon, in its OWN file.
        _write_areas(config, DS_A, "count", {"camera_01": [_area("left", LEFT_SQUARE)]})
        _write_areas(config, DS_B, "count", {"camera_01": [_area("right", RIGHT_SQUARE)]})

        pipeline.localize(config, dataset_id=DS_A)
        pipeline.localize(config, dataset_id=DS_B)

        a = _by_x(config, DS_A)
        b = _by_x(config, DS_B)

        # Guard 1: A localized against A's own file only.
        check("A: (5,5) -> camera_01::left",
              a[5.0]["region_id"] == "camera_01::left", str(a))
        check("A: (25,5) not covered by A's 'left' -> region_id NULL",
              a[25.0]["region_id"] is None, str(a))
        # Guard 2: B localized against B's own file; A's polygon is NOT applied to B.
        check("B: (5,5) NOT matched by A's 'left' polygon -> region_id NULL",
              b[5.0]["region_id"] is None, str(b))
        check("B: (25,5) -> camera_01::right",
              b[25.0]["region_id"] == "camera_01::right", str(b))


# --------------------------------------------------------------------------- 3: edit isolation
def test_edit_isolation():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed_two(config)
        _write_areas(config, DS_A, "count", {"camera_01": [_area("left", LEFT_SQUARE)]})
        _write_areas(config, DS_B, "count", {"camera_01": [_area("right", RIGHT_SQUARE)]})
        pipeline.localize(config, dataset_id=DS_A)
        pipeline.localize(config, dataset_id=DS_B)

        a_before = _by_x(config, DS_A)

        # Re-draw ONLY B's areas (its 'right' now covers the LEFT point) and re-localize
        # ONLY B. Editing + re-localizing one dataset must not touch the other's rows.
        _write_areas(config, DS_B, "count", {"camera_01": [_area("right", LEFT_SQUARE)]})
        pipeline.localize(config, dataset_id=DS_B)

        a_after = _by_x(config, DS_A)
        b_after = _by_x(config, DS_B)

        check("editing B leaves A (5,5) unchanged -> still camera_01::left",
              a_before[5.0]["region_id"] == a_after[5.0]["region_id"] == "camera_01::left",
              f"before={a_before} after={a_after}")
        check("editing B leaves A (25,5) unchanged -> still NULL",
              a_before[25.0]["region_id"] is None and a_after[25.0]["region_id"] is None,
              f"before={a_before} after={a_after}")
        check("re-localized B: (5,5) now -> camera_01::right",
              b_after[5.0]["region_id"] == "camera_01::right", str(b_after))
        check("re-localized B: (25,5) no longer covered -> NULL",
              b_after[25.0]["region_id"] is None, str(b_after))


# --------------------------------------------------------------------------- 4: whole-DB fan-out
def test_whole_db_localize():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed_two(config)
        _write_areas(config, DS_A, "count", {"camera_01": [_area("left", LEFT_SQUARE)]})
        _write_areas(config, DS_B, "count", {"camera_01": [_area("right", RIGHT_SQUARE)]})

        # dataset_id=None must fan out over BOTH datasets, each against its OWN file.
        pipeline.localize(config)

        a = _by_x(config, DS_A)
        b = _by_x(config, DS_B)
        check("whole-DB: A (5,5) -> camera_01::left",
              a[5.0]["region_id"] == "camera_01::left", str(a))
        check("whole-DB: A (25,5) -> NULL",
              a[25.0]["region_id"] is None, str(a))
        check("whole-DB: B (5,5) -> NULL (A's file not applied to B)",
              b[5.0]["region_id"] is None, str(b))
        check("whole-DB: B (25,5) -> camera_01::right",
              b[25.0]["region_id"] == "camera_01::right", str(b))


# --------------------------------------------------------------------------- 5: missing area file is a clean no-op
def test_missing_area_file_no_crash():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, "C", date(2025, 7, 5), "C")
            _insert(con, "C", [P_LEFT])
        finally:
            con.close()

        # No area file exists for dataset C at all.
        crashed = None
        try:
            pipeline.localize(config, dataset_id="C")
        except Exception as e:  # noqa: BLE001 -- the point is that it must NOT raise
            crashed = repr(e)
        check("missing area file: localize(dataset_id='C') does not crash",
              crashed is None, str(crashed))

        c = _by_x(config, "C")
        check("missing area file: C's detection region_id stays NULL",
              c[5.0]["region_id"] is None, str(c))
        check("missing area file: C's detection under_panel stays NULL",
              c[5.0]["under_panel"] is None, str(c))


# --------------------------------------------------------------------------- 6: path lives beside artifacts/, not under it
def test_area_path_is_artifacts_sibling():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        artifacts = Path(config.paths.artifacts_dir)

        p_count = regions.dataset_area_path(config, DS_A, "count")
        p_panel = regions.dataset_area_path(config, DS_A, "panel")

        check("count path == <artifacts>/../areas/<ds>/count_areas.json",
              p_count == artifacts.parent / "areas" / DS_A / "count_areas.json", str(p_count))
        check("panel path == <artifacts>/../areas/<ds>/panel_areas.json",
              p_panel == artifacts.parent / "areas" / DS_A / "panel_areas.json", str(p_panel))

        # rmtree hazard: areas MUST NOT live under artifacts/<ds>/, which ingest rmtrees.
        ds_artifacts = artifacts / DS_A
        check("area file is NOT under artifacts/<ds>/ (re-ingest rmtree-safe)",
              ds_artifacts not in p_count.parents,
              f"{ds_artifacts} unexpectedly a parent of {p_count}")

        # Legacy fallback: dataset_id=None -> the flat, global config paths.
        check("dataset_id=None -> legacy flat count path",
              regions.dataset_area_path(config, None, "count") == Path(config.paths.count_areas),
              str(regions.dataset_area_path(config, None, "count")))
        check("dataset_id=None -> legacy flat panel path",
              regions.dataset_area_path(config, None, "panel") == Path(config.paths.panel_areas),
              str(regions.dataset_area_path(config, None, "panel")))


# --------------------------------------------------------------------------- 7: panel/shelter areas are per-dataset too
def test_panel_isolation():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed_two(config)
        # A has a panel area over the LEFT point; B has NO panel file at all.
        _write_areas(config, DS_A, "panel", {"camera_01": [_area("shed", LEFT_SQUARE)]})
        # Count files too, so the count pass also runs (realistic full localize).
        _write_areas(config, DS_A, "count", {"camera_01": [_area("left", LEFT_SQUARE)]})
        _write_areas(config, DS_B, "count", {"camera_01": [_area("right", RIGHT_SQUARE)]})

        pipeline.localize(config, dataset_id=DS_A)
        pipeline.localize(config, dataset_id=DS_B)

        a = _by_x(config, DS_A)
        b = _by_x(config, DS_B)

        check("panel: A (5,5) under_panel True",
              a[5.0]["under_panel"] is True, str(a))
        check("panel: A (5,5) panel_id -> camera_01::shed",
              a[5.0]["panel_id"] == "camera_01::shed", str(a))
        check("panel: A (25,5) processed-but-outside -> under_panel False",
              a[25.0]["under_panel"] is False, str(a))
        check("panel: A (25,5) panel_id NULL",
              a[25.0]["panel_id"] is None, str(a))
        # B has no panel file -> A's panel polygon is NOT applied; B stays unprocessed.
        check("panel: B (5,5) NOT under A's panel -> under_panel NULL",
              b[5.0]["under_panel"] is None, str(b))
        check("panel: B (5,5) panel_id NULL",
              b[5.0]["panel_id"] is None, str(b))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_dataset_area_scoping ===")
    test_per_dataset_isolation()
    test_edit_isolation()
    test_whole_db_localize()
    test_missing_area_file_no_crash()
    test_area_path_is_artifacts_sibling()
    test_panel_isolation()
    print("=================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
