"""Regression test for db.connect() under concurrency.

No pytest. Run either way:
    python -m tests.test_db_concurrency
    python tests/test_db_concurrency.py

Reproduces the condition that 500'd the dashboard: many short-lived connections
to the same DuckDB file opened at once. db.connect() must retry past the
transient "unique file handle conflict" instead of raising.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def main() -> None:
    print("=== test_db_concurrency ===")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.duckdb")
        c0 = db.connect(path)
        db.init_db(c0)
        c0.close()

        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                c = db.connect(path)
                c.execute("SELECT count(*) FROM frames").fetchall()
                c.close()
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check("30 concurrent connect+query -> no file-handle conflict",
              not errors, f"{len(errors)} errors; first={errors[0] if errors else '-'}")

    print("===========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
