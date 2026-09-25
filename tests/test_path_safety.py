"""Request strings that become filesystem paths.

Three real holes, all found on 2026-09-24 from one Discord alert that said only
`ValueError: embedded null byte`:

  1. **Arbitrary file read.** `GET /api/areas?dataset=../secrets` returned the
     contents of any file named `count_areas.json` anywhere the app uid could
     reach. Any logged-in role.
  2. **Arbitrary file write.** `POST /api/areas?dataset=../../x` wrote
     attacker-controlled JSON outside the areas root, creating parent
     directories. Poweruser.
  3. **Unauthenticated 500s.** A NUL byte in a URL reached `os.stat()`, which
     rejects it with `ValueError` — NOT `OSError` — so it escaped every
     `except OSError` in pathlib/posixpath and surfaced as an unhandled ASGI
     exception: a 500 plus a full traceback into the ops alert channel.

(1) and (2) existed because `resolve_ds` returned the query param untouched and
`regions.dataset_area_path` interpolated it straight into a path; the *other*
routes taking a dataset id (`label-crop`, `label-frame`, `delete_camera`) called
`_safe_path_id` and were fine, which is exactly how a gap like this survives
review — the codebase looked like it validated.

The distinction that matters throughout: a path that cannot be resolved must
FAIL CLOSED (404/400), never 500, and never fall through to a serve. And
`.resolve()` must stay in the SPA route: its `..` normalisation IS the traversal
guard, so "simplifying" it away would create the traversal these tests forbid.

No pytest. Run either way:
    py -3.11 -m tests.test_path_safety
    py -3.11 tests/test_path_safety.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from cownting import db  # noqa: E402
from cownting import uploads  # noqa: E402
from cownting.api import _safe_path_id, create_app  # noqa: E402
from cownting.config import AuthCfg, Config, PathsCfg  # noqa: E402
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
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


@contextlib.contextmanager
def tmpdir():
    """A temp dir whose cleanup cannot fail the test.

    `create_app` starts background threads (the label-backup scheduler, the
    localize worker) that hold DuckDB handles, and Windows refuses to unlink an
    open file — so TemporaryDirectory's own cleanup raises and masks whatever
    the test actually found."""
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


_POLY = [[0, 0], [1, 0], [1, 1]]
_AREAS = {"areas": {"camera_01": [{"id": "x", "name": "x",
                                   "camera_polygon": _POLY, "ortho_polygon": _POLY}]}}


def _app(d: str):
    art = os.path.join(d, "data", "artifacts")
    os.makedirs(art, exist_ok=True)
    dbp = os.path.join(d, "c.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    con.close()
    cfg = Config(cameras=[], auth=AuthCfg(enabled=False),
                 paths=PathsCfg(db_path=dbp, labels_db_path=os.path.join(d, "l.duckdb"),
                                artifacts_dir=art,
                                count_areas=os.path.join(d, "count.json"),
                                panel_areas=os.path.join(d, "panel.json"),
                                backups_dir=os.path.join(d, "b")))
    # raise_server_exceptions=False so an unhandled error arrives as the 500 a
    # real client would see, instead of being re-raised into the test.
    return TestClient(create_app(cfg), raise_server_exceptions=False), cfg


def test_the_areas_routes_cannot_read_outside_their_root():
    """THE READ PRIMITIVE. Any logged-in role could name any directory."""
    with tmpdir() as d:
        c, _ = _app(d)
        secrets = os.path.join(d, "data", "secrets")
        os.makedirs(secrets, exist_ok=True)
        with open(os.path.join(secrets, "count_areas.json"), "w") as f:
            json.dump({"TOP": ["SECRET"]}, f)
        with open(os.path.join(secrets, "panel_areas.json"), "w") as f:
            json.dump({"TOP": ["SECRET"]}, f)

        for route in ("/api/areas", "/api/panel-areas"):
            r = c.get(f"{route}?dataset=../secrets")
            check(f"GET {route} refuses a traversing dataset id",
                  r.status_code == 400, str(r.status_code))
            check(f"...and leaks nothing from {route}", "SECRET" not in r.text, r.text[:80])


def test_the_areas_routes_cannot_write_outside_their_root():
    """THE WRITE PRIMITIVE — the serious one: JSON anywhere, with mkdir -p."""
    with tmpdir() as d:
        c, _ = _app(d)
        for route, fname in (("/api/areas", "count_areas.json"),
                             ("/api/panel-areas", "panel_areas.json")):
            r = c.post(f"{route}?dataset=../../pwned", json=_AREAS)
            check(f"POST {route} refuses a traversing dataset id",
                  r.status_code == 400, str(r.status_code))
            escaped = os.path.join(d, "pwned", fname)
            check(f"...and wrote nothing outside the areas root ({fname})",
                  not os.path.exists(escaped), escaped)
        check("no stray directory was created at all",
              not os.path.exists(os.path.join(d, "pwned")))


def test_a_legitimate_dataset_id_still_round_trips():
    """The control. A guard that breaks the real flow is not a fix."""
    with tmpdir() as d:
        c, _ = _app(d)
        r = c.post("/api/areas?dataset=2025-07-03", json=_AREAS)
        check("a normal ISO day id is accepted", r.status_code == 200, str(r.status_code))
        got = c.get("/api/areas?dataset=2025-07-03").json()
        check("...and reads back what was written", "camera_01" in got, str(got)[:100])
        check("...landing inside the areas root",
              os.path.exists(os.path.join(d, "data", "areas", "2025-07-03", "count_areas.json")))


def test_a_nul_byte_anywhere_in_a_url_is_a_404_not_a_500():
    """os.stat() raises ValueError on a NUL, which no `except OSError` catches.
    45 scanner probes on 2026-09-22 turned that into 44 tracebacks."""
    with tmpdir() as d:
        c, _ = _app(d)
        for path in ("/.env%00", "/a/b%00c", "/wp-config.php%00", "/assets/x%00"):
            r = c.get(path)
            check(f"GET {path} -> not a 500", r.status_code != 500, str(r.status_code))
        r = c.get("/api/img/label-frame/camera_01/1.jpg?dataset=x%00y")
        check("a NUL in a QUERY param is caught too (the middleware covers both)",
              r.status_code != 500, str(r.status_code))
        r = c.get("/api/does-not-exist%00")
        check("an /api/ path with a NUL is a clean 404", r.status_code == 404, str(r.status_code))


def test_the_spa_route_cannot_be_walked_out_of_its_root():
    """The traversal guard that was always correct — pinned so that removing the
    `.resolve()` (which is what normalises `..`) fails loudly."""
    with tmpdir() as d:
        c, _ = _app(d)
        # Plant a sentinel as a SIBLING of the dist directory the app serves.
        from cownting import api as api_mod
        dist = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(api_mod.__file__))),
                            "frontend", "dist")
        # This used to return early with a GREEN check when `frontend/dist` was
        # absent — and `.gitignore` ignores that directory while CI ran the Python
        # suite BEFORE `npm ci`, so the traversal probes never executed on a
        # single CI run in the gate's entire life. A guard that is skipped in the
        # only place it is meant to guard is not a guard.
        #
        # So: build a minimal dist if there isn't one, and remove exactly what we
        # created. The app resolves `dist` relative to its own module, so it
        # cannot simply be pointed at a tmpdir.
        made_dist = not os.path.isdir(dist)
        if made_dist:
            os.makedirs(dist, exist_ok=True)
            with open(os.path.join(dist, "index.html"), "w") as f:
                f.write("<!doctype html><title>test</title>")
        sentinel = os.path.join(os.path.dirname(dist), "test_sentinel_secret.txt")
        with open(sentinel, "w") as f:
            f.write("SENTINEL-DO-NOT-SERVE")
        try:
            check("the traversal probes actually run (dist exists)",
                  os.path.isdir(dist), dist)
            for path in ("/../test_sentinel_secret.txt",
                         "/..%2ftest_sentinel_secret.txt",
                         "/..%2f..%2f..%2fetc%2fpasswd",
                         "//etc/passwd"):
                r = c.get(path)
                check(f"GET {path} never serves the file outside dist",
                      "SENTINEL-DO-NOT-SERVE" not in r.text and "root:" not in r.text,
                      f"{r.status_code} {r.text[:60]}")
        finally:
            os.unlink(sentinel)
            if made_dist:
                shutil.rmtree(dist, ignore_errors=True)


def test_the_validators_enumerate_what_is_allowed():
    for bad in ("x\x00y", "a\nb", "", ".", "..", "a/b", "a\\b", "..%2f", "-lead",
                "a" * 65, "sp ace"):
        check(f"_safe_path_id rejects {bad!r}", not _safe_path_id(bad))
    for good in ("2025-07-03", "camera_01", "a", "A.b_c-d"):
        check(f"_safe_path_id accepts {good!r}", _safe_path_id(good))

    # `$` matches before a trailing newline; `fullmatch` does not. That newline
    # became a path segment and an upload filename.
    for bad in ("cam1\n", "cam1\x00", "../etc", "", "c" * 65, "_leading"):
        check(f"valid_camera_id rejects {bad!r}", not uploads.valid_camera_id(bad))
    check("valid_camera_id accepts camera_01", uploads.valid_camera_id("camera_01"))


def test_the_path_builder_defends_itself():
    """The API validates, but pipeline and CLI callers never saw that check, so
    the invariant lives with the builder too."""
    with tmpdir() as d:
        _, cfg = _app(d)
        for bad in ("../escape", "../../escape", "a/../../b"):
            try:
                regions.dataset_area_path(cfg, bad, "count")
                check(f"dataset_area_path refuses {bad!r}", False, "it returned a path")
            except ValueError:
                check(f"dataset_area_path refuses {bad!r}", True)
        p = regions.dataset_area_path(cfg, "2025-07-03", "count")
        check("...and still builds the legitimate path", "2025-07-03" in str(p), str(p))
        legacy = regions.dataset_area_path(cfg, None, "count")
        check("...and still honours the legacy flat path for a None id",
              str(legacy).endswith("count.json"), str(legacy))


def main():
    print("=== test_path_safety ===")
    test_the_areas_routes_cannot_read_outside_their_root()
    test_the_areas_routes_cannot_write_outside_their_root()
    test_a_legitimate_dataset_id_still_round_trips()
    test_a_nul_byte_anywhere_in_a_url_is_a_404_not_a_500()
    test_the_spa_route_cannot_be_walked_out_of_its_root()
    test_the_validators_enumerate_what_is_allowed()
    test_the_path_builder_defends_itself()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
