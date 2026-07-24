"""Background localize worker: saving areas enqueues an off-thread, coalesced
re-localize instead of blocking the request.

Drives cownting.localize_worker against a REAL DuckDB + REAL per-dataset area
files (same fixtures as test_dataset_area_scoping), and asserts: a request returns
immediately, the worker actually localizes, a burst of requests coalesces to one
clean pass, and a dataset with no area file completes without crashing.

No pytest. Run either way:
    python -m tests.test_localize_worker
    python tests/test_localize_worker.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, localize_worker  # noqa: E402
from cownting.config import Config, PathsCfg  # noqa: E402
from cownting.scene import regions  # noqa: E402

# Shrink the coalescing debounce so the test is quick (the worker still runs a real
# background thread + real localize).
localize_worker._DEBOUNCE_S = 0.05

_FAILED = 0
DS = "2025-06-28"
SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]  # covers (5,5), not (25,5)


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def _config(d: str) -> Config:
    data = os.path.join(d, "data")
    return Config(cameras=[], paths=PathsCfg(
        artifacts_dir=os.path.join(data, "artifacts"),
        db_path=os.path.join(data, "cownting.duckdb"),
        count_areas=os.path.join(data, "count_areas.json"),
        panel_areas=os.path.join(data, "panel_areas.json"),
    ))


def _seed(config: Config, dataset_id: str, points) -> None:
    con = db.connect(config.paths.db_path)
    try:
        db.init_db(con)
        db.upsert_dataset(con, dataset_id, date(2025, 6, 28), dataset_id)
        rows = [{"dataset_id": dataset_id, "camera_id": "camera_01",
                 "ts": datetime(2025, 1, 1, 0, i, 0),
                 "ground_px_x": float(x), "ground_px_y": float(y)}
                for i, (x, y) in enumerate(points)]
        db.insert_detections(con, pd.DataFrame(rows))
    finally:
        con.close()


def _write_count(config: Config, dataset_id: str, mapping) -> None:
    regions.save_count_areas(regions.dataset_area_path(config, dataset_id, "count"), mapping)


def _region(config: Config, dataset_id: str, x: float):
    con = db.connect(config.paths.db_path)
    try:
        r = con.execute(
            "SELECT region_id FROM detections WHERE dataset_id = ? AND ground_px_x = ?",
            [dataset_id, x],
        ).fetchone()
    finally:
        con.close()
    return r[0] if r else "<<missing row>>"


def _wait_idle(timeout: float = 20.0) -> dict:
    """Poll until the worker is no longer busy (or timeout)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = localize_worker.status()
        if not s["busy"]:
            return s
        time.sleep(0.05)
    return localize_worker.status()


class _LocalizeSpy:
    """Stands in for the pass the worker runs (pipeline.localize, imported into the
    worker as `_localize`). It (a) counts how many passes actually ran, so a burst
    test can PROVE coalescing rather than just observe the end state, and (b) can
    block the pass on a gate, so a test can observe the pre-localize state without a
    timing race against the debounce."""

    def __init__(self, real):
        self._real = real
        self.calls = 0
        self.gate: threading.Event | None = None  # when set, the pass waits until released

    def __call__(self, config, dataset_id=None):
        if self.gate is not None:
            self.gate.wait(timeout=10)
        self.calls += 1
        return self._real(config, dataset_id=dataset_id)


def _install_spy(gate: "threading.Event | None" = None) -> _LocalizeSpy:
    spy = _LocalizeSpy(localize_worker._localize)
    spy.gate = gate
    localize_worker._localize = spy
    return spy


def _restore_spy(spy: _LocalizeSpy) -> None:
    localize_worker._localize = spy._real


def test_request_is_async_and_localizes():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed(config, DS, [(5.0, 5.0), (25.0, 5.0)])
        _write_count(config, DS, {"camera_01": [{"id": "left", "name": "Left",
                                                 "camera_polygon": SQUARE, "ortho_polygon": []}]})
        # Gate the pass so the async proof is deterministic, not a race against the
        # debounce: the worker physically cannot have localized while blocked here.
        gate = threading.Event()
        spy = _install_spy(gate=gate)
        try:
            s0 = localize_worker.request_localize(config, DS)
            check("request_localize returns immediately (pending/busy)",
                  s0["busy"] or s0["status"] == "pending", str(s0))
            check("returns before the pass finished (region still unassigned)",
                  _region(config, DS, 5.0) is None, "pre-localize should be NULL")
            check("pass has not run while gated", spy.calls == 0, f"calls={spy.calls}")

            gate.set()  # release the worker
            s = _wait_idle()
            check("worker completed", s["status"] == "done" and s["error"] is None, str(s))
            check("worker localized the dataset: (5,5) -> camera_01::left",
                  _region(config, DS, 5.0) == "camera_01::left", str(s))
            check("(25,5) outside -> region_id NULL", _region(config, DS, 25.0) is None)
        finally:
            _restore_spy(spy)


def test_burst_coalesces():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed(config, DS, [(5.0, 5.0)])
        _write_count(config, DS, {"camera_01": [{"id": "left", "name": "Left",
                                                 "camera_polygon": SQUARE, "ortho_polygon": []}]})
        spy = _install_spy()
        try:
            # A rapid burst of saves for the SAME dataset must not error or pile up —
            # and, crucially, must collapse into ~one localize pass, not one per save.
            for _ in range(6):
                localize_worker.request_localize(config, DS)
            s = _wait_idle()
            check("burst ends cleanly done (no error)",
                  s["status"] == "done" and s["error"] is None, str(s))
            check("burst still localized the dataset",
                  _region(config, DS, 5.0) == "camera_01::left", str(s))
            # The real proof of coalescing: 6 saves -> at most ~1 pass (allow a single
            # follow-up if one landed mid-pass), NOT 6 independent passes.
            check("burst coalesced to one pass (<=2), not one-per-save",
                  spy.calls <= 2, f"localize ran {spy.calls}x for 6 saves")
        finally:
            _restore_spy(spy)


def test_missing_area_file_completes():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        _seed(config, "C", [(5.0, 5.0)])  # no area file written for C
        localize_worker.request_localize(config, "C")
        s = _wait_idle()
        check("missing-file dataset completes without failing",
              s["status"] == "done" and s["error"] is None, str(s))
        check("missing-file dataset leaves region_id NULL", _region(config, "C", 5.0) is None)


def main():
    print("=== test_localize_worker ===")
    test_request_is_async_and_localizes()
    test_burst_coalesces()
    test_missing_area_file_completes()
    print("============================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
