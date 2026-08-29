"""Label HTTP-contract tests on a hermetic synthetic DB (the tests/test_api.py
shape: AuthCfg(enabled=False), temp DuckDB files, no network).

Covers the frozen M3 route table: the queue's item shape and sampling policy,
the queue-key -> submit round trip (the end-to-end version of the SQL/Python key
pin), anchor forgery, the taxonomy-stale 409, served-event telemetry, undo
scoping, and both label image endpoints' path safety, caching and banner
masking — the crop, and the full uncropped frame behind hold-to-peek.

No pytest. Run either way:
    .venv/bin/python -m tests.test_labels_api
    .venv/bin/python tests/test_labels_api.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import duckdb
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from cownting import db, labeling, labels_db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

# Contract tests hit /api/label/* directly; the gates themselves belong to
# tests/test_auth.py. With auth off every write lands as annotator='local'.
_NO_AUTH = AuthCfg(enabled=False)

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """Record one assertion.

    Standalone (`python tests/test_labels_api.py`) this PRINTS every check and
    `main()` exits non-zero at the end, so one run shows every failure at once —
    which is the point of the style.

    Under pytest it must also RAISE, and that is not a nicety: a check that only
    prints is invisible to a test runner, so `pytest -q` reported this file as
    fully passing while four assertions inside it were failing. A test that
    cannot fail is not a test, and it is worse than none — it is a green tick
    that means nothing."""
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


# The frozen §5.3 item shape LabelItem mirrors. Every key must be present on
# every served item — the frontend types against exactly this.
_ITEM_FIELDS = (
    "instance_key", "dataset_id", "day", "camera_id", "frame_file", "bbox",
    "ordinal", "score", "frame_sig", "crop_url", "frame_url", "crop_w", "crop_h",
    "ring", "n_annotators", "target", "overlap", "serve_event_id",
    # M4a: the outline in both spaces, plus the frame's ORIGINAL dimensions the
    # hold-Space peek needs to place full-frame coordinates on a DOWNSCALED image.
    "mask", "mask_frame", "mask_seed", "frame_w", "frame_h", "geom_done",
    "crop_levels", "crop_level",
)


def _mk_app(d: str) -> tuple[TestClient, Config]:
    """Two seeded days x one camera, 3 detections each (two sharing a frame),
    with half-pixel and negative bbox coordinates so the queue's SQL-minted keys
    only round-trip if both key producers really agree."""
    dbp = os.path.join(d, "cownting.duckdb")
    art = os.path.join(d, "artifacts")
    con = db.connect(dbp)
    db.init_db(con)
    for ds, day in (("2026-07-03", date(2026, 7, 3)), ("2026-07-04", date(2026, 7, 4))):
        db.upsert_dataset(con, ds, day, day.strftime("%b %d, %Y"), status="localized")
        f1 = os.path.join(art, ds, "frames", "camera_01", "00000001.jpg")
        f2 = os.path.join(art, ds, "frames", "camera_01", "00000002.jpg")
        db.insert_frames(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": "camera_01", "frame_idx": 1,
             "ts": datetime(day.year, day.month, day.day, 6, 0), "frame_path": f1},
            {"dataset_id": ds, "camera_id": "camera_01", "frame_idx": 2,
             "ts": datetime(day.year, day.month, day.day, 6, 1), "frame_path": f2},
        ]))
        db.insert_detections(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": "camera_01", "frame_path": f1, "score": 0.9,
             "ts": datetime(day.year, day.month, day.day, 6, 0),
             "bbox_x1": 10.5, "bbox_y1": 20.0, "bbox_x2": 110.5, "bbox_y2": 120.0},
            {"dataset_id": ds, "camera_id": "camera_01", "frame_path": f1, "score": 0.8,
             "ts": datetime(day.year, day.month, day.day, 6, 0),
             "bbox_x1": 300.2, "bbox_y1": 40.7, "bbox_x2": 420.9, "bbox_y2": 160.1},
            {"dataset_id": ds, "camera_id": "camera_01", "frame_path": f2, "score": 0.7,
             "ts": datetime(day.year, day.month, day.day, 6, 1),
             "bbox_x1": -4.5, "bbox_y1": 3.2, "bbox_x2": 90.8, "bbox_y2": 77.7},
        ]))
    con.close()
    config = Config(
        cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
        auth=_NO_AUTH,
        paths=PathsCfg(
            db_path=dbp,
            labels_db_path=os.path.join(d, "labels.duckdb"),
            backups_dir=os.path.join(d, "backups"),
            artifacts_dir=art,
            count_areas=os.path.join(d, "areas.json"),
        ),
    )
    return TestClient(create_app(config)), config


def _anchor(item: dict) -> dict:
    """The anchor a real client echoes: exactly what the queue served."""
    return {
        "dataset_id": item["dataset_id"],
        "camera_id": item["camera_id"],
        "frame_file": item["frame_file"],
        "bbox": item["bbox"],
        "ordinal": item["ordinal"],
        "frame_sig": item["frame_sig"],
    }


_ANSWERS = {"sun_exposure": "sun_exposure.shaded", "behaviour": "behaviour.standing"}


def _rev(client: TestClient) -> int:
    return int(client.get("/api/label/taxonomy").json()["revision"])


def _submit(client: TestClient, item: dict, revision: int, **extra) -> "object":
    body = {"instance_key": item["instance_key"], "anchor": _anchor(item),
            "answers": _ANSWERS, "taxonomy_revision": revision, **extra}
    return client.post("/api/label/submit", json=body)


def test_queue_shape():
    with tempfile.TemporaryDirectory() as d:
        client, _config = _mk_app(d)
        r = client.get("/api/label/queue")
        check("GET /api/label/queue -> 200", r.status_code == 200, str(r.status_code))
        body = r.json() if r.status_code == 200 else {}
        items = body.get("items", [])
        check("queue returns the whole 6-instance pool in one batch",
              len(items) == 6, str(len(items)))
        check("matching reports the exact pool size", body.get("matching") == 6,
              str(body.get("matching")))
        check("policy block is present with the study knobs",
              isinstance(body.get("policy"), dict)
              and body["policy"].get("targets_per_instance") == 2, str(body.get("policy")))
        check("filters block echoes the applied scope",
              isinstance(body.get("filters"), dict)
              and body["filters"].get("dataset") is None
              and body["filters"].get("mine") == "todo", str(body.get("filters")))
        for i, item in enumerate(items):
            missing = [f for f in _ITEM_FIELDS if f not in item]
            check(f"item {i} carries every frozen field", not missing, str(missing))
        check("crop_url is server-built under /api/img/label-crop/",
              all(str(it["crop_url"]).startswith("/api/img/label-crop/") for it in items))
        check("frame_url is server-built under /api/img/label-frame/",
              all(str(it["frame_url"]).startswith("/api/img/label-frame/") for it in items),
              str(items[0]["frame_url"]) if items else "")
        # The cross-day guard in URL form: each item names its OWN dataset, so
        # nothing downstream has to (and withDs() would name the wrong one).
        check("frame_url carries the item's own dataset, not the selected day",
              all(f"dataset={it['dataset_id']}" in it["frame_url"] for it in items),
              str(items[0]["frame_url"]) if items else "")
        check("crop canvas is square (crop_w == crop_h)",
              all(it["crop_w"] == it["crop_h"] for it in items))
        check("serve_event_id is a number on every item",
              all(isinstance(it["serve_event_id"], int) for it in items))
        # The cross-day proof: both seeded days present, so the queue cannot be
        # going through resolve_ds (which would pin it to the latest dataset).
        days = {it["dataset_id"] for it in items}
        check("queue spans BOTH seeded days (does not go through resolve_ds)",
              days == {"2026-07-03", "2026-07-04"}, str(days))
        check("day is the ISO date from the datasets dim",
              {it["day"] for it in items} == {"2026-07-03", "2026-07-04"},
              str({it["day"] for it in items}))


def test_queue_key_roundtrips_to_submit():
    """The end-to-end version of the SQL/Python key pin: every key the queue
    mints in SQL re-derives from its own anchor in Python, and a submit of that
    anchor is accepted."""
    with tempfile.TemporaryDirectory() as d:
        client, _config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        rev = _rev(client)
        check("queue served all six items", len(items) == 6, str(len(items)))
        for it in items:
            py_key = labels_db.instance_key(
                it["dataset_id"], it["camera_id"], it["frame_file"],
                it["bbox"], int(it["ordinal"]))
            check(f"key re-derives from its own anchor ({it['frame_file']} ord {it['ordinal']})",
                  py_key == it["instance_key"], f"py={py_key} served={it['instance_key']}")
            r = _submit(client, it, rev)
            check(f"submit of the served anchor -> 200 ({it['instance_key'][:8]}...)",
                  r.status_code == 200, f"{r.status_code}: {r.text[:120]}")
            if r.status_code == 200:
                check("submit echoes ok + annotation_id + version 1",
                      r.json().get("ok") is True and r.json().get("version") == 1,
                      str(r.json()))


def test_submit_rejects_a_forged_anchor():
    with tempfile.TemporaryDirectory() as d:
        client, _config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        rev = _rev(client)
        forged = dict(item)
        forged["bbox"] = [v + 17.0 for v in item["bbox"]]
        r = _submit(client, forged, rev)
        check("a bbox shifted by 17 px -> 400", r.status_code == 400, str(r.status_code))
        check("...naming the anchor mismatch", "anchor" in r.text, r.text[:120])


def test_stale_taxonomy_revision_is_409():
    with tempfile.TemporaryDirectory() as d:
        client, _config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        old_rev = _rev(client)
        # A poweruser archives a class mid-session (auth off, so the gate no-ops
        # here; the gate itself is tests/test_auth.py's job).
        r = client.patch("/api/label/classes/sun_exposure.cannot_tell",
                         json={"active": False})
        check("archive a class -> 200", r.status_code == 200, str(r.status_code))
        new_rev = _rev(client)
        check("archiving bumped the revision", new_rev == old_rev + 1,
              f"{old_rev} -> {new_rev}")

        # An in-flight submit with the old revision must be 409 taxonomy_stale,
        # never a 400 the client would retry forever.
        r = _submit(client, item, old_rev)
        check("stale revision -> 409, not 400", r.status_code == 409, str(r.status_code))
        detail = r.json().get("detail", {}) if r.status_code == 409 else {}
        check("409 carries code=taxonomy_stale and the current revision",
              detail.get("code") == "taxonomy_stale" and detail.get("revision") == new_rev,
              str(detail))

        # Known keys, not active keys: naming the just-archived class with the
        # CURRENT revision still succeeds.
        body = {"instance_key": item["instance_key"], "anchor": _anchor(item),
                "answers": {"sun_exposure": "sun_exposure.cannot_tell",
                            "behaviour": "behaviour.standing"},
                "taxonomy_revision": new_rev}
        r = client.post("/api/label/submit", json=body)
        check("submit naming the ARCHIVED class with the current revision -> 200",
              r.status_code == 200, f"{r.status_code}: {r.text[:120]}")


def test_queue_policy():
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        rev = _rev(client)

        # An instance labeled by another annotator stays in the pool at its own
        # place in this annotator's permutation. It is deliberately NOT sorted to
        # the back (M3 UX §6.7): coverage-first ordering would withhold every
        # second label until the pool had been swept once, so pairs would only
        # ever form under fatigue.
        covered = items[0]["instance_key"]
        lc = db.connect(config.paths.labels_db_path)
        labels_db.submit_annotation(
            lc, instance_key=covered, annotator="alice", outcome="labeled",
            choices=[{"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
                      "class_name": "Shaded"}])
        lc.close()
        got = client.get("/api/label/queue").json()["items"]
        by_key = {it["instance_key"]: it for it in got}
        check("an under-target instance labeled by someone else is still served",
              covered in by_key, str(sorted(by_key)[:3]))
        check("...and it carries that annotator's coverage",
              by_key[covered]["n_annotators"] == 1 if covered in by_key else False,
              str([(it["instance_key"][:8], it["n_annotators"]) for it in got]))

        # Never re-served to the same annotator: local labels one, refetches.
        mine = got[0]
        r = _submit(client, mine, rev)
        check("submit -> 200", r.status_code == 200, str(r.status_code))
        after = client.get("/api/label/queue").json()["items"]
        check("a labeled instance never comes back to the same annotator",
              all(it["instance_key"] != mine["instance_key"] for it in after))

        # Flagging removes it from MY queue but leaves it for others (mine=all
        # shows the pool another annotator would still be offered).
        #
        # An UNEXPLAINED escape is refused: skipping as a bare action was removed
        # deliberately, so this route now demands a written justification as well
        # as a reason code. An instance nobody could answer is a finding about the
        # data, and a finding with no explanation attached is not usable later.
        skipped = after[0]
        bare = {"instance_key": skipped["instance_key"], "anchor": _anchor(skipped),
                "reason": "occluded"}
        r = client.post("/api/label/skip", json=bare)
        check("flag with no explanation -> 400", r.status_code == 400, str(r.status_code))
        r = client.post("/api/label/skip", json={**bare, "note": "   "})
        check("flag with a whitespace-only explanation -> 400",
              r.status_code == 400, str(r.status_code))

        r = client.post("/api/label/skip", json={**bare, "note": "panel leg hides the whole body"})
        check("flag with reason + explanation -> 200", r.status_code == 200, str(r.status_code))
        after_skip = client.get("/api/label/queue").json()["items"]
        check("a flagged instance leaves my queue",
              all(it["instance_key"] != skipped["instance_key"] for it in after_skip))
        pool = client.get("/api/label/queue?mine=all").json()["items"]
        check("...but stays in the pool for other annotators",
              any(it["instance_key"] == skipped["instance_key"] for it in pool),
              str([it["instance_key"][:8] for it in pool]))

        # Every reason in the vocabulary must actually be accepted by the route.
        # `low_resolution` was added after the first annotations existed, so this
        # also pins that widening the tuple did not disturb the older codes: the
        # column is a plain VARCHAR with no CHECK, and validation is Python-side,
        # so a stale value can only ever come back as a 400 here.
        remaining = [it for it in pool
                     if it["instance_key"] != skipped["instance_key"]]
        # low_resolution FIRST, deliberately: the seeded pool is small, and in
        # tuple order this loop ran out of instances before reaching it — so the
        # newest reason was the one code never actually exercised.
        ordered = ("low_resolution",) + tuple(
            r for r in labels_db.SKIP_REASONS if r != "low_resolution")
        exercised = []
        for i, reason in enumerate(ordered):
            if i >= len(remaining):
                break
            target = remaining[i]
            r = client.post("/api/label/skip", json={
                "instance_key": target["instance_key"],
                "anchor": _anchor(target),
                "reason": reason,
                "note": f"exercising the {reason} code",
            })
            check(f"flag reason {reason!r} accepted", r.status_code == 200,
                  f"{r.status_code} {r.text[:100]}")
            if r.status_code == 200:
                exercised.append(reason)
        check("low_resolution was actually exercised through the route, not just "
              "asserted in the tuple", "low_resolution" in exercised, str(exercised))
        check("low_resolution is in the vocabulary",
              "low_resolution" in labels_db.SKIP_REASONS,
              str(labels_db.SKIP_REASONS))
        r = client.post("/api/label/skip", json={
            "instance_key": skipped["instance_key"], "anchor": _anchor(skipped),
            "reason": "not_a_real_reason", "note": "should be refused"})
        check("an unknown reason is still refused -> 400", r.status_code == 400,
              str(r.status_code))

        # limit honoured, and matching still reports the whole pool.
        r = client.get("/api/label/queue?mine=all&limit=2").json()
        check("limit=2 serves exactly 2 items", len(r["items"]) == 2, str(len(r["items"])))
        check("matching still counts the whole pool past the limit",
              r["matching"] >= len(r["items"]), str(r["matching"]))


def test_multi_annotator_default():
    """targets_per_instance defaults to 2: annotator A's label must NOT retire
    the instance for annotator B — the requirement fails silently otherwise."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        target_key = items[0]["instance_key"]
        lc = db.connect(config.paths.labels_db_path)
        labels_db.submit_annotation(
            lc, instance_key=target_key, annotator="alice", outcome="labeled",
            choices=[{"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
                      "class_name": "Shaded"}])
        lc.close()
        served = client.get("/api/label/queue").json()["items"]
        mine = [it for it in served if it["instance_key"] == target_key]
        check("an instance labeled by A is still served to B",
              len(mine) == 1, str([it["instance_key"][:8] for it in served]))
        check("...showing 1 existing annotator against a target of at least 2",
              bool(mine) and mine[0]["n_annotators"] == 1 and mine[0]["target"] >= 2,
              str(mine and (mine[0]["n_annotators"], mine[0]["target"])))


def test_serve_event_and_time_on_task():
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        rev = _rev(client)
        lc = db.connect(config.paths.labels_db_path)
        served = lc.execute(
            "SELECT event_id FROM label_events WHERE kind = 'served'").fetchall()
        lc.close()
        check("the queue fetch wrote one served event per item",
              len(served) == len(items), f"{len(served)} events, {len(items)} items")
        ev_ids = {int(r[0]) for r in served}
        check("each item's serve_event_id maps to a stored served event",
              all(int(it["serve_event_id"]) in ev_ids for it in items))

        # Server-measured time on task: the sleep guarantees a positive delta.
        item = items[0]
        time.sleep(0.15)
        r = _submit(client, item, rev, serve_event_id=item["serve_event_id"],
                    session_id="sess-0001")
        check("submit with serve_event_id -> 200", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")
        aid = r.json().get("annotation_id") if r.status_code == 200 else None
        lc = db.connect(config.paths.labels_db_path)
        row = lc.execute(
            "SELECT served_at, time_on_task_ms FROM annotations WHERE annotation_id = ?",
            [aid]).fetchone()
        lc.close()
        check("served_at is populated from the queue's own event",
              row is not None and row[0] is not None, str(row))
        check("time_on_task_ms is server-measured and positive",
              row is not None and row[1] is not None and int(row[1]) > 0, str(row))


def test_decision_event_detail_is_stored():
    """The `answered` payload (M3 UX §6.1) must survive the API boundary.

    Regression: `LabelEventReq` once had no `detail` field, so pydantic's
    default `extra='ignore'` dropped the whole object silently — every event
    landed with detail NULL and per-decision latency was unrecoverable."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        detail = {"group_key": "sun_exposure", "class_key": "sun_exposure.shaded",
                  "ms_since_group_shown": 812, "ms_since_item_shown": 1503,
                  "replaced_class_key": None, "input_mode": "key"}
        r = client.post("/api/label/events", json={
            "session_id": "sess-detail", "kind": "answered",
            "instance_key": items[0]["instance_key"],
            "class_key": "sun_exposure.shaded", "detail": detail})
        check("answered event -> 200", r.status_code == 200,
              f"{r.status_code}: {r.text[:120]}")
        lc = db.connect(config.paths.labels_db_path)
        row = lc.execute(
            "SELECT detail FROM label_events WHERE kind = 'answered'").fetchone()
        lc.close()
        check("the answered event stored its detail payload",
              row is not None and row[0] is not None, str(row))
        stored = json.loads(row[0]) if row and row[0] else {}
        check("every prescribed field survived the round trip",
              stored.get("group_key") == "sun_exposure"
              and stored.get("ms_since_group_shown") == 812
              and stored.get("ms_since_item_shown") == 1503
              and stored.get("input_mode") == "key", str(stored))


def test_undo_is_scoped_to_me():
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        items = client.get("/api/label/queue").json()["items"]
        rev = _rev(client)
        # Alice's answer, written as a second session would write it.
        alice_key = items[0]["instance_key"]
        lc = db.connect(config.paths.labels_db_path)
        alice_id = labels_db.submit_annotation(
            lc, instance_key=alice_key, annotator="alice", outcome="labeled",
            choices=[{"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
                      "class_name": "Shaded"}])
        lc.close()

        # 'local' (annotator B) cannot supersede alice's row.
        r = client.post("/api/label/undo", json={"instance_key": alice_key})
        check("undo of another annotator's instance is a no-op (annotation_id null)",
              r.status_code == 200 and r.json().get("annotation_id") is None, r.text[:120])
        lc = db.connect(config.paths.labels_db_path)
        row = lc.execute(
            "SELECT outcome, superseded_at FROM annotations WHERE annotation_id = ?",
            [alice_id]).fetchone()
        lc.close()
        check("alice's row is untouched",
              row is not None and row[0] == "labeled" and row[1] is None, str(row))

        # Positive control: my own answer IS undoable.
        mine = items[1]
        r = _submit(client, mine, rev)
        check("own submit -> 200", r.status_code == 200, str(r.status_code))
        my_id = r.json().get("annotation_id")
        r = client.post("/api/label/undo", json={"instance_key": mine["instance_key"]})
        check("undo of my own answer supersedes it",
              r.status_code == 200 and r.json().get("annotation_id") == my_id, r.text[:120])


def _noise_frame(art: str, ds: str, name: str, w: int = 200, h: int = 150) -> None:
    from PIL import Image

    p = Path(art) / ds / "frames" / "camera_01"
    p.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(p / name, quality=90)


def test_crop_endpoint_safety_and_caching():
    from PIL import Image

    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        _noise_frame(config.paths.artifacts_dir, "2026-07-03", "00000001.jpg")
        q = "x1=20&y1=30&x2=80&y2=90"

        # Path safety: traversal in the camera segment and the dataset param, and
        # a filename outside the total whitelist, are all 400.
        r = client.get(f"/api/img/label-crop/..%5C..%5Cwindows/00000001.jpg?{q}")
        check("traversal in camera -> 400", r.status_code == 400, str(r.status_code))
        r = client.get(f"/api/img/label-crop/camera_01/00000001.jpg?dataset=..%2F..%2Fetc&{q}")
        check("traversal in dataset -> 400", r.status_code == 400, str(r.status_code))
        r = client.get(f"/api/img/label-crop/camera_01/notaframe.txt?{q}")
        check("a non-frame filename -> 400", r.status_code == 400, str(r.status_code))

        # A missing JPEG is routine (re-ingest rmtrees frames): 404, never 500.
        r = client.get(f"/api/img/label-crop/camera_01/00000002.jpg?dataset=2026-07-03&{q}")
        check("missing JPEG -> 404, never 500", r.status_code == 404, str(r.status_code))

        # The real thing: 200 image/jpeg, private cache, ETag -> 304, square.
        url = f"/api/img/label-crop/camera_01/00000001.jpg?dataset=2026-07-03&{q}"
        r = client.get(url)
        check("real JPEG -> 200", r.status_code == 200, str(r.status_code))
        check("content-type is image/jpeg",
              r.headers.get("content-type", "").startswith("image/jpeg"),
              r.headers.get("content-type", ""))
        check("Cache-Control is private (session-gated; no shared proxy may store it)",
              "private" in r.headers.get("cache-control", ""),
              r.headers.get("cache-control", ""))
        etag = r.headers.get("etag", "")
        check("a strong ETag is set", bool(etag), etag)
        r304 = client.get(url, headers={"If-None-Match": etag})
        check("the ETag round-trips to 304", r304.status_code == 304, str(r304.status_code))
        check("X-Frame-Sig rides on the crop for the submit body",
              bool(r.headers.get("x-frame-sig")), str(r.headers.get("x-frame-sig")))
        im = Image.open(BytesIO(r.content))
        check("the crop is square", im.size[0] == im.size[1], str(im.size))
        # bbox 60x60, pad 0.35 -> side 102 < max_width, so 1:1 scale.
        check("crop_w == crop_h == the geometry's out_size", im.size == (102, 102),
              str(im.size))


def test_banner_mask_does_not_blank_the_tile():
    """A bbox below the Brinno band (y1 > 0.96*H) must come back either as a 404
    (mostly-banner crop refused) or as a crop with REAL pixels above the fill —
    never an all-grey tile that turns fabricated ambiguity into data."""
    from PIL import Image, ImageStat

    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        _noise_frame(config.paths.artifacts_dir, "2026-07-03", "00000001.jpg")  # 200x150

        def probe(x1: float, y1: float, x2: float, y2: float, label: str) -> None:
            r = client.get(
                "/api/img/label-crop/camera_01/00000001.jpg?dataset=2026-07-03"
                f"&x1={x1}&y1={y1}&x2={x2}&y2={y2}")
            check(f"{label}: 404 or 200, never a 500",
                  r.status_code in (200, 404), str(r.status_code))
            if r.status_code == 200:
                im = Image.open(BytesIO(r.content)).convert("L")
                sd = ImageStat.Stat(im).stddev[0]
                check(f"{label}: 200 carries real pixels, not an all-grey tile",
                      sd > 5.0, f"stddev={sd:.2f}")
            else:
                check(f"{label}: refused outright rather than blanked", True)

        # Band top is 150 * 0.96 = 144; both boxes sit entirely below it.
        # Small box: the padded square is mostly banner -> the refusal path.
        probe(50, 146, 60, 149, "small below-band bbox")
        # Wide box: the padded square reaches far above the band -> real pixels.
        probe(0, 145, 200, 149, "wide below-band bbox")


def test_frame_endpoint_safety_and_caching():
    """/api/img/label-frame mirrors label-crop's path safety and cache contract.

    Same three whitelists (valid_camera_id, _safe_frame_file, _safe_path_id) and
    the same resolve-under-artifacts_dir check, because this route rebuilds an
    on-disk path out of URL segments exactly the way the crop route does."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        _noise_frame(config.paths.artifacts_dir, "2026-07-03", "00000001.jpg")  # 200x150

        r = client.get("/api/img/label-frame/..%5C..%5Cwindows/00000001.jpg")
        check("frame: traversal in camera -> 400", r.status_code == 400, str(r.status_code))
        r = client.get("/api/img/label-frame/camera_01/00000001.jpg?dataset=..%2F..%2Fetc")
        check("frame: traversal in dataset -> 400", r.status_code == 400, str(r.status_code))
        r = client.get("/api/img/label-frame/camera_01/..%5C..%5Cwin.ini")
        check("frame: traversal in frame_file -> 400", r.status_code == 400, str(r.status_code))
        r = client.get("/api/img/label-frame/camera_01/notaframe.txt")
        check("frame: a non-frame filename -> 400", r.status_code == 400, str(r.status_code))
        r = client.get("/api/img/label-frame/camera_01/00000001.jpg.exe")
        check("frame: a double extension -> 400", r.status_code == 400, str(r.status_code))
        # An encoded separator either fails to match the route or fails the
        # whitelist; what matters is that it never comes back as an image.
        r = client.get("/api/img/label-frame/camera_01/..%2F..%2Fwin.ini")
        check("frame: an encoded separator in frame_file never returns an image",
              r.status_code in (400, 404), str(r.status_code))

        # A missing JPEG is routine (a re-ingest rmtrees frames out from under a
        # queue the client is still holding): 404, never a 500.
        r = client.get("/api/img/label-frame/camera_01/00000002.jpg?dataset=2026-07-03")
        check("frame: missing JPEG -> 404, never 500", r.status_code == 404, str(r.status_code))

        url = "/api/img/label-frame/camera_01/00000001.jpg?dataset=2026-07-03"
        r = client.get(url)
        check("frame: real JPEG -> 200", r.status_code == 200, str(r.status_code))
        check("frame: content-type is image/jpeg",
              r.headers.get("content-type", "").startswith("image/jpeg"),
              r.headers.get("content-type", ""))
        check("frame: Cache-Control is private (session-gated; no shared proxy may store it)",
              "private" in r.headers.get("cache-control", ""),
              r.headers.get("cache-control", ""))
        check("frame: Cache-Control keeps the one-hour max-age",
              "max-age=3600" in r.headers.get("cache-control", ""),
              r.headers.get("cache-control", ""))
        etag = r.headers.get("etag", "")
        check("frame: a strong ETag is set", bool(etag), etag)
        r304 = client.get(url, headers={"If-None-Match": etag})
        check("frame: the ETag round-trips to 304 (a held key re-decodes nothing)",
              r304.status_code == 304, str(r304.status_code))

        # The two routes render different pixels out of one file; a shared ETag
        # would let a cache hand back the crop where the frame was asked for.
        crop = client.get("/api/img/label-crop/camera_01/00000001.jpg"
                          "?dataset=2026-07-03&x1=20&y1=30&x2=80&y2=90")
        check("frame: the ETag differs from the crop's over the same file",
              bool(etag) and etag != crop.headers.get("etag", ""), etag)

        # `w` is a CEILING, never a target: a small frame is not upscaled into blur.
        r = client.get(url + "&w=4096")
        check("frame: w above the native width does not upscale",
              r.status_code == 200 and Image.open(BytesIO(r.content)).size == (200, 150),
              str(r.status_code))


def test_frame_banner_is_masked():
    """THE point of the route: a full frame shows MORE of the burned-in Brinno
    clock than any crop of it does, so serving the JPEG off disk would hand the
    annotator the wall-clock time — which IS the sun-exposure answer (M3 §4.5) —
    on every hold-to-peek.

    The source file is the negative control: the same band is full-contrast noise
    on disk and flat fill in the response, so this cannot pass by accident on a
    frame that never had anything down there."""
    from PIL import Image, ImageStat

    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        _noise_frame(config.paths.artifacts_dir, "2026-07-03", "00000001.jpg")  # 200x150
        src = (Path(config.paths.artifacts_dir) / "2026-07-03" / "frames"
               / "camera_01" / "00000001.jpg")

        # Control: the band is NOT already flat on disk.
        raw = Image.open(src).convert("L")
        rw, rh = raw.size
        raw_band = ImageStat.Stat(raw.crop((0, int(rh * 0.96), rw, rh))).stddev[0]
        check("control: the source frame's banner band is full-contrast noise",
              raw_band > 5.0, f"stddev={raw_band:.2f}")

        url = "/api/img/label-frame/camera_01/00000001.jpg?dataset=2026-07-03"
        r = client.get(url)
        check("frame: real JPEG -> 200", r.status_code == 200, str(r.status_code))
        check("frame: the body is re-rendered, NOT the file on disk",
              r.content != src.read_bytes(),
              f"{len(r.content)} served vs {src.stat().st_size} on disk")

        im = Image.open(BytesIO(r.content)).convert("L")
        w, h = im.size
        check("frame: served at native size (200x150 is under the 1600 ceiling)",
              (w, h) == (200, 150), str(im.size))
        band = ImageStat.Stat(im.crop((0, int(h * 0.96), w, h)))
        check("frame: the banner band comes back FLAT — masked, not passed through",
              band.stddev[0] < 3.0, f"stddev={band.stddev[0]:.2f}")
        check("frame: ...flat at the neutral fill, not at black or white",
              abs(band.mean[0] - 96.0) < 6.0, f"mean={band.mean[0]:.2f}")
        # The other half of the requirement: masking must not blank the frame.
        above = ImageStat.Stat(im.crop((0, 0, w, int(h * 0.96)))).stddev[0]
        check("frame: everything ABOVE the band is untouched real pixels",
              above > 5.0, f"stddev={above:.2f}")

        # Masking runs BEFORE the downscale. Reverse the order and the band's
        # first row is measured against the wrong height, so the clock survives
        # the resize — the failure that looks like nothing at all on screen.
        r = client.get(url + "&w=100")
        check("frame: w=100 downscales and preserves aspect",
              r.status_code == 200 and Image.open(BytesIO(r.content)).size == (100, 75),
              str(r.status_code))
        small = Image.open(BytesIO(r.content)).convert("L")
        sw, sh = small.size
        # One row in from the seam: LANCZOS bleeds about a pixel across it.
        sband = ImageStat.Stat(small.crop((0, int(sh * 0.96) + 1, sw, sh)))
        check("frame: the band is still flat at the same height fraction after the downscale",
              sband.stddev[0] < 4.0, f"stddev={sband.stddev[0]:.2f}")
        check("frame: ...and still at the neutral fill after resampling",
              abs(sband.mean[0] - 96.0) < 8.0, f"mean={sband.mean[0]:.2f}")


def test_every_queue_item_frame_url_resolves():
    """Fetch each item's `frame_url` verbatim, the way <img src> would.

    The cross-day proof for hold-to-peek: items from BOTH seeded days resolve,
    which a client-built URL could not manage — lib/api.ts's withDs() would stamp
    the selected day onto every one of them and 404 half the queue."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        for ds in ("2026-07-03", "2026-07-04"):
            for name in ("00000001.jpg", "00000002.jpg"):
                _noise_frame(config.paths.artifacts_dir, ds, name)
        items = client.get("/api/label/queue").json()["items"]
        days = {it["dataset_id"] for it in items}
        check("the queue spans both days, so this really is a cross-day test",
              days == {"2026-07-03", "2026-07-04"}, str(days))
        for it in items:
            r = client.get(it["frame_url"])
            check(f"frame_url resolves for {it['dataset_id']}/{it['frame_file']}",
                  r.status_code == 200
                  and r.headers.get("content-type", "").startswith("image/jpeg"),
                  f"{r.status_code} {r.headers.get('content-type', '')}")


def _mask_fix(client: TestClient, item: dict, **extra) -> "object":
    body = {"instance_key": item["instance_key"], "anchor": _anchor(item),
            "kind": "polygon", "seeded_from": "bbox", **extra}
    return client.post("/api/label/mask-fix", json=body)


def test_mask_fix_roundtrip_and_validation():
    """M4a §4.2: the outline write, its geometry validation, and the crop-local
    -> full-frame conversion the store depends on."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        # A triangle well inside the crop box, in crop-local px like the editor.
        tri = [[10.0, 10.0], [40.0, 12.0], [25.0, 44.0]]
        r = _mask_fix(client, item, polygon=tri)
        check("POST /api/label/mask-fix -> 200", r.status_code == 200,
              f"{r.status_code} {r.text[:160]}")
        check("mask fix reports version 1", (r.json() or {}).get("version") == 1)

        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            row = lc.execute(
                "SELECT kind, polygon, n_vertices, area_px, seeded_from FROM mask_edits "
                "WHERE instance_key = ? AND superseded_at IS NULL", [item["instance_key"]]
            ).fetchone()
            check("the edit is stored as a current polygon row",
                  row is not None and row[0] == "polygon", str(row))
            stored = json.loads(row[1]) if row and row[1] else []
            check("stored polygon keeps its vertex count", len(stored) == 3, str(stored))
            check("n_vertices and area are computed server-side",
                  row is not None and row[2] == 3 and (row[3] or 0) > 0, str(row))
            check("seeded_from is recorded", row is not None and row[4] == "bbox")
            # The whole point of storing full-frame px: the polygon must land ON
            # the animal, i.e. within the padded crop square around its bbox.
            src, _out, _ring = labeling.crop_geometry(
                item["bbox"], config.annotation.crop_pad, config.annotation.crop_max_width)
            inside = all(src[0] <= px <= src[2] and src[1] <= py <= src[3]
                         for px, py in stored)
            check("stored points are full-frame px inside the served crop square",
                  inside, f"{stored} vs {src}")
            check("stored points are NOT the crop-local ones that were sent",
                  stored != tri, str(stored))

            # A second save supersedes rather than overwriting — the same
            # append-only rule the answers follow.
            r2 = _mask_fix(client, item, polygon=[[11.0, 11.0], [41.0, 13.0], [26.0, 45.0]])
            check("a second outline save -> 200", r2.status_code == 200, r2.text[:160])
            check("...and is version 2", (r2.json() or {}).get("version") == 2)
            n_current = lc.execute(
                "SELECT count(*) FROM mask_edits WHERE instance_key = ? "
                "AND superseded_at IS NULL", [item["instance_key"]]).fetchone()[0]
            n_all = lc.execute("SELECT count(*) FROM mask_edits WHERE instance_key = ?",
                               [item["instance_key"]]).fetchone()[0]
            check("exactly one current row survives, both are kept",
                  n_current == 1 and n_all == 2, f"current={n_current} all={n_all}")
        finally:
            lc.close()

        # Validation. Each of these would otherwise reach the store as geometry
        # no consumer can use.
        check("a forged anchor is a 400",
              _mask_fix(client, {**item, "bbox": [1.0, 2.0, 3.0, 4.0]},
                        polygon=tri).status_code == 400)
        check("a two-point outline is a 400",
              _mask_fix(client, item, polygon=[[1.0, 1.0], [2.0, 2.0]]).status_code == 400)
        # Sent as a RAW body: `json.dumps` refuses to encode infinity, and
        # JSON.stringify turns it into null, so the only way this reaches the
        # route is a hand-made request — which is exactly the case the finiteness
        # check exists for. `1e999` is valid JSON syntax that parses to inf.
        raw = json.dumps({"instance_key": item["instance_key"], "anchor": _anchor(item),
                          "kind": "polygon", "seeded_from": "bbox",
                          "polygon": [[10.0, 10.0], [40.0, 12.0], [25.0, 44.0]]})
        raw = raw.replace("[25.0, 44.0]", "[1e999, 44.0]")
        r_inf = client.post("/api/label/mask-fix", content=raw,
                            headers={"Content-Type": "application/json"})
        check("a non-finite point is rejected", r_inf.status_code in (400, 422),
              f"{r_inf.status_code} {r_inf.text[:120]}")
        check("a point outside the crop is a 400",
              _mask_fix(client, item,
                        polygon=[[10.0, 10.0], [40.0, 12.0], [99999.0, 44.0]]
                        ).status_code == 400)
        check("an unknown kind is a 400",
              _mask_fix(client, item, kind="nonsense", polygon=tri).status_code == 400)
        check("a false positive carrying geometry is a 400",
              _mask_fix(client, item, kind="false_positive",
                        polygon=tri).status_code == 400)


def test_false_positive_retires_the_instance_for_everyone():
    """M4a §4.2: 'not a cow' is not a skip. It leaves the pool for every
    annotator, because there are no questions to ask about a shadow — and the
    progress numbers have to agree with what the queue will actually serve."""
    with tempfile.TemporaryDirectory() as d:
        client, _config = _mk_app(d)
        before = client.get("/api/label/queue").json()
        item = before["items"][0]
        pool_before = int(client.get("/api/label/progress").json()["pool_total"])
        remaining_before = int(client.get("/api/label/progress").json()["remaining"])

        r = client.post("/api/label/mask-fix", json={
            "instance_key": item["instance_key"], "anchor": _anchor(item),
            "kind": "false_positive", "polygon": None, "seeded_from": "mask"})
        check("removing a false positive -> 200", r.status_code == 200, r.text[:160])

        after = client.get("/api/label/queue").json()
        keys = {i["instance_key"] for i in after["items"]}
        check("the removed instance is no longer served",
              item["instance_key"] not in keys, str(len(keys)))
        check("...and it is the ONLY one that left",
              len(after["items"]) == len(before["items"]) - 1,
              f"{len(before['items'])} -> {len(after['items'])}")

        prog = client.get("/api/label/progress").json()
        check("pool_total is unchanged — nothing was deleted",
              int(prog["pool_total"]) == pool_before, str(prog["pool_total"]))
        check("remaining drops by exactly one, matching the queue",
              int(prog["remaining"]) == remaining_before - 1,
              f"{remaining_before} -> {prog['remaining']}")

        # 'mine=all' is a preference; it must not resurrect a false positive.
        allq = client.get("/api/label/queue?mine=all").json()
        check("mine=all does not bring it back",
              item["instance_key"] not in {i["instance_key"] for i in allq["items"]})


def test_zoom_ladder_and_frame_space_submit():
    """The outline editor edits in FULL-FRAME px and zooms by swapping which crop
    is on screen. Two things have to hold for that to be safe: every rung of the
    ladder must describe a real crop of THIS animal, and a polygon submitted in
    frame space must be stored verbatim — no conversion, so no basis to shear
    against, whatever zoom it was drawn at."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        levels = item["crop_levels"]
        check("the item carries a zoom ladder", len(levels) >= 2, str(len(levels)))
        check("the default level is on the ladder",
              0 <= item["crop_level"] < len(levels), str(item["crop_level"]))
        check("the default level is the configured crop_pad",
              abs(levels[item["crop_level"]]["pad"] - config.annotation.crop_pad) < 1e-6,
              str(levels[item["crop_level"]]["pad"]))
        pads = [lv["pad"] for lv in levels]
        check("the ladder is sorted tight -> wide", pads == sorted(pads), str(pads))

        # Every rung must agree with the geometry the crop endpoint renders with,
        # or the editor's viewBox describes a square the image is not a picture of.
        for lv in levels:
            src, out, _ring = labeling.crop_geometry(
                item["bbox"], lv["pad"], config.annotation.crop_max_width)
            check(f"level pad={lv['pad']} src box matches crop_geometry",
                  [float(v) for v in src] == lv["src"] and out == lv["out"],
                  f"{lv['src']} vs {list(src)}")
            check(f"level pad={lv['pad']} url carries its own pad",
                  f"pad={lv['pad']!r}".replace("pad=", "pad=") and "pad=" in lv["url"],
                  lv["url"][:80])
        widest = levels[-1]["src"]
        tightest = levels[0]["src"]
        check("zooming out really does show more of the frame",
              (widest[2] - widest[0]) > (tightest[2] - tightest[0]),
              f"{tightest} -> {widest}")

        # A polygon drawn at the WIDEST zoom — i.e. one that would fall outside
        # the default crop — must still store, because the annotator could see it.
        wx0, wy0, wx1, wy1 = widest
        poly = [[wx0 + 3.0, wy0 + 3.0], [wx1 - 3.0, wy0 + 4.0], [wx0 + 9.0, wy1 - 3.0]]
        r = client.post("/api/label/mask-fix", json={
            "instance_key": item["instance_key"], "anchor": _anchor(item),
            "kind": "polygon", "space": "frame", "polygon": poly, "seeded_from": "model"})
        check("a frame-space outline from the widest zoom -> 200",
              r.status_code == 200, r.text[:160])

        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            stored = json.loads(lc.execute(
                "SELECT polygon FROM mask_edits WHERE instance_key = ? "
                "AND superseded_at IS NULL", [item["instance_key"]]).fetchone()[0])
        finally:
            lc.close()
        drift = max(abs(a - b) for p, q in zip(stored, poly) for a, b in zip(p, q))
        check("...is stored VERBATIM — frame space needs no conversion",
              drift < 1e-9, f"max drift {drift}")

        # And it comes back on the item in the same space, ready to re-edit.
        again = [i for i in client.get("/api/label/queue").json()["items"]
                 if i["instance_key"] == item["instance_key"]][0]
        back = max(abs(a - b) for p, q in zip(again["mask_frame"], poly)
                   for a, b in zip(p, q))
        check("...and rides back as mask_frame unchanged", back < 1e-9, f"{back}")

        # Beyond the widest crop is refused: the annotator cannot have seen it.
        far = [[wx1 + 500.0, wy1 + 500.0], [wx1 + 600.0, wy1 + 500.0],
               [wx1 + 550.0, wy1 + 600.0]]
        check("a point beyond the widest zoom is a 400",
              client.post("/api/label/mask-fix", json={
                  "instance_key": item["instance_key"], "anchor": _anchor(item),
                  "kind": "polygon", "space": "frame", "polygon": far,
                  "seeded_from": "model"}).status_code == 400)
        check("an unknown space is a 400",
              client.post("/api/label/mask-fix", json={
                  "instance_key": item["instance_key"], "anchor": _anchor(item),
                  "kind": "polygon", "space": "galactic", "polygon": poly,
                  "seeded_from": "model"}).status_code == 400)


def test_ok_verdict_is_a_measurement_not_a_noop():
    """The mandatory geometry step's fast path. Confirming an outline stores an
    'ok' verdict — that is what turns "the annotator looked" into a
    false-positive rate — and must NOT retire the instance: the cow still has to
    be answered."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        before = len(client.get("/api/label/queue").json()["items"])

        r = client.post("/api/label/mask-fix", json={
            "instance_key": item["instance_key"], "anchor": _anchor(item),
            "kind": "ok", "polygon": None, "seeded_from": "bbox"})
        check("confirming an outline -> 200", r.status_code == 200, r.text[:160])

        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            row = lc.execute(
                "SELECT kind, polygon FROM mask_edits WHERE instance_key = ? "
                "AND superseded_at IS NULL", [item["instance_key"]]).fetchone()
            check("an 'ok' verdict is stored with no geometry",
                  row is not None and row[0] == "ok" and row[1] is None, str(row))
        finally:
            lc.close()

        after = client.get("/api/label/queue").json()["items"]
        check("confirming does NOT retire the instance — it still needs answers",
              len(after) == before
              and item["instance_key"] in {i["instance_key"] for i in after},
              f"{before} -> {len(after)}")
        check("an 'ok' verdict carrying geometry is a 400",
              client.post("/api/label/mask-fix", json={
                  "instance_key": item["instance_key"], "anchor": _anchor(item),
                  "kind": "ok", "polygon": [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]],
                  "seeded_from": "bbox"}).status_code == 400)

        # A later correction supersedes the confirmation: latest verdict wins.
        r2 = _mask_fix(client, item, polygon=[[10.0, 10.0], [40.0, 12.0], [25.0, 44.0]])
        check("correcting after confirming -> 200", r2.status_code == 200, r2.text[:160])
        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            rows = lc.execute(
                "SELECT kind FROM mask_edits WHERE instance_key = ? AND superseded_at IS NULL",
                [item["instance_key"]]).fetchall()
            check("exactly one current verdict, and it is the correction",
                  len(rows) == 1 and rows[0][0] == "polygon", str(rows))
        finally:
            lc.close()


def test_ok_never_destroys_my_own_correction():
    """REGRESSION. `mask_edits`' supersede matches on (annotator, key) with no
    `kind` predicate, so a later 'ok' used to wipe an earlier corrected polygon —
    silently, and by the single most-pressed key on the screen.

    The path is the ordinary one, not a corner case: correct an outline, leave
    the questions unanswered (a correction retires nothing, by design), reload.
    The instance is served again, the geometry step asks again, the annotator
    presses Enter — and the correction they made ten seconds ago is gone from
    v_current_mask_edits, from the queue's join and from any future export, with
    nothing on screen to say so.

    The rule: an 'ok' is the WEAKEST verdict — it endorses what the model drew.
    It must never overrule a stronger statement the same annotator has already
    made about the same instance."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        tri = [[10.0, 10.0], [40.0, 12.0], [25.0, 44.0]]
        check("correcting the outline -> 200",
              _mask_fix(client, item, polygon=tri).status_code == 200)

        # ...the annotator reloads and the geometry step asks again.
        r = client.post("/api/label/mask-fix", json={
            "instance_key": item["instance_key"], "anchor": _anchor(item),
            "kind": "ok", "polygon": None, "seeded_from": "mask"})
        check("pressing Enter on an already-corrected instance -> 200",
              r.status_code == 200, r.text[:160])

        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            rows = lc.execute(
                "SELECT kind, polygon FROM mask_edits WHERE instance_key = ? "
                "AND superseded_at IS NULL", [item["instance_key"]]).fetchall()
            check("the correction is STILL the current verdict",
                  len(rows) == 1 and rows[0][0] == "polygon" and rows[0][1],
                  str(rows))
        finally:
            lc.close()

        # And it must still ride the queue item, or the editor reseeds from the
        # box and the annotator's work is invisible even though it is stored.
        again = [i for i in client.get("/api/label/queue").json()["items"]
                 if i["instance_key"] == item["instance_key"]]
        check("...and still comes back on the item",
              again and again[0]["mask"] is not None and again[0]["mask_seed"] == "edit",
              str(again[0]["mask_seed"] if again else "gone"))
        # The trigger, closed at the source: the server tells the client the
        # geometry step is already passed, so a reload does not ask again.
        check("...and the item says the geometry step is already passed",
              again and again[0]["geom_done"] is True,
              str(again[0]["geom_done"] if again else "gone"))

        # A false positive is also stronger than 'ok': confirming afterwards must
        # not resurrect a detection someone judged not to be an animal.
        item2 = [i for i in client.get("/api/label/queue").json()["items"]
                 if i["instance_key"] != item["instance_key"]][0]
        client.post("/api/label/mask-fix", json={
            "instance_key": item2["instance_key"], "anchor": _anchor(item2),
            "kind": "false_positive", "polygon": None, "seeded_from": "bbox"})
        client.post("/api/label/mask-fix", json={
            "instance_key": item2["instance_key"], "anchor": _anchor(item2),
            "kind": "ok", "polygon": None, "seeded_from": "bbox"})
        keys = {i["instance_key"] for i in client.get("/api/label/queue").json()["items"]}
        check("a removed instance stays removed after a later 'ok'",
              item2["instance_key"] not in keys)


def test_crop_frame_coordinate_roundtrip():
    """M4a §4.1: the editor works in crop-local px and the store in full-frame px,
    so the two converters must be exact inverses. A drift here would shear every
    saved polygon away from the pixels it was drawn on — silently, because both
    ends would still look plausible."""
    for bbox in ([300.2, 40.7, 420.9, 160.1],      # ordinary
                 [-4.5, 3.2, 90.8, 77.7],          # crosses the frame edge
                 [10.0, 10.0, 11.0, 2000.0]):      # extreme aspect
        for pad, w in ((0.35, 768), (0.0, 768), (1.0, 64)):
            crop = [[1.0, 2.0], [30.0, 4.0], [17.5, 40.25], [3.0, 39.0]]
            frame = labeling.crop_to_frame(crop, bbox, pad=pad, max_width=w)
            back = labeling.frame_to_crop(frame, bbox, pad=pad, max_width=w)
            worst = max(abs(a - b) for p, q in zip(crop, back) for a, b in zip(p, q))
            check(f"crop->frame->crop is exact (bbox={bbox[0]}, pad={pad}, w={w})",
                  worst < 1e-6, f"max drift {worst}")

    # And the ring the queue serves must be what crop_to_frame maps back onto the
    # bbox itself — the pin between the two halves of the contract.
    bbox = [300.2, 40.7, 420.9, 160.1]
    _src, _out, ring = labeling.crop_geometry(bbox, 0.35, 768)
    corners = [[ring[0], ring[1]], [ring[2], ring[1]], [ring[2], ring[3]]]
    mapped = labeling.crop_to_frame(corners, bbox, pad=0.35, max_width=768)
    check("the served ring maps back onto the original bbox",
          all(abs(mapped[0][i] - bbox[i]) < 0.75 for i in (0, 1))
          and abs(mapped[2][0] - bbox[2]) < 0.75 and abs(mapped[2][1] - bbox[3]) < 0.75,
          f"{mapped} vs {bbox}")


def test_saved_outline_comes_back_on_the_queue_item():
    """A correction the annotator saved must reappear: as `mask` (crop-local, so
    the editor reopens it instead of the bare rectangle) and as `mask_frame`
    (full-frame, so hold-Space can draw it on the whole scene)."""
    with tempfile.TemporaryDirectory() as d:
        client, config = _mk_app(d)
        item = client.get("/api/label/queue").json()["items"][0]
        check("before any edit the item carries no outline",
              item["mask"] is None and item["mask_frame"] is None
              and item["mask_seed"] == "bbox", str(item["mask_seed"]))
        check("...and the geometry step is not yet passed",
              item["geom_done"] is False, str(item["geom_done"]))
        # The seeded fixture records frame PATHS without writing the JPEGs, so
        # dimensions are legitimately null there — which is itself the contract
        # (a frame rmtree'd by a re-ingest must yield null, not a guess, or the
        # peek would draw its marks in the wrong place). Write a real frame to
        # exercise the populated branch.
        check("a missing frame yields null dimensions rather than a guess",
              item["frame_w"] is None and item["frame_h"] is None,
              f"{item['frame_w']}x{item['frame_h']}")
        _noise_frame(config.paths.artifacts_dir, item["dataset_id"],
                     item["frame_file"], w=640, h=480)
        sized = [i for i in client.get("/api/label/queue").json()["items"]
                 if i["instance_key"] == item["instance_key"]][0]
        check("a present frame carries its ORIGINAL dimensions",
              sized["frame_w"] == 640 and sized["frame_h"] == 480,
              f"{sized['frame_w']}x{sized['frame_h']}")

        tri = [[10.0, 10.0], [40.0, 12.0], [25.0, 44.0]]
        r = _mask_fix(client, item, polygon=tri)
        check("saving an outline -> 200", r.status_code == 200, r.text[:160])

        again = client.get("/api/label/queue").json()["items"]
        same = [i for i in again if i["instance_key"] == item["instance_key"]]
        check("the instance is still served (an outline fix is not an answer)",
              len(same) == 1, str(len(same)))
        it = same[0] if same else {}
        check("...and now carries the saved outline, crop-local",
              it.get("mask") is not None and len(it["mask"]) == 3, str(it.get("mask")))
        check("...seeded_from flips to 'edit' — my own correction, not the model's",
              it.get("mask_seed") == "edit", str(it.get("mask_seed")))
        check("...and the same outline in full-frame px",
              it.get("mask_frame") is not None and len(it["mask_frame"]) == 3,
              str(it.get("mask_frame")))
        # The crop-local copy must land back where the annotator drew it.
        drift = max(abs(a - b) for p, q in zip(it.get("mask") or [], tri)
                    for a, b in zip(p, q)) if it.get("mask") else 999
        check("the returned crop-local outline matches what was drawn",
              drift < 0.75, f"max drift {drift}")
        # The two projections must be projections of ONE polygon.
        back = labeling.frame_to_crop(it["mask_frame"], it["bbox"],
                                      pad=config.annotation.crop_pad,
                                      max_width=config.annotation.crop_max_width)
        agree = max(abs(a - b) for p, q in zip(back, it["mask"]) for a, b in zip(p, q))
        check("mask and mask_frame are the same polygon in two spaces",
              agree < 0.75, f"max disagreement {agree}")

        # REGRESSION: each projection must be IN the space its consumer draws in.
        # `mask` is drawn by InstanceCrop into a `0 0 crop_w crop_h` viewBox — feed
        # it full-frame coordinates and the overlay does not misdraw, it lands off
        # the canvas entirely and simply disappears, which reads as "the feature
        # is gone" rather than as a bug.
        check("mask is CROP-LOCAL — inside the served crop box",
              all(-1 <= v <= it["crop_w"] + 1 for p in it["mask"] for v in p),
              str(it["mask"][:2]))
        src, _out, _ring = labeling.crop_geometry(
            it["bbox"], config.annotation.crop_pad, config.annotation.crop_max_width)
        check("mask_frame is FULL-FRAME — inside the crop's source square",
              all(src[0] - 1 <= p[0] <= src[2] + 1 and src[1] - 1 <= p[1] <= src[3] + 1
                  for p in it["mask_frame"]), str(it["mask_frame"][:2]))
        check("...and the two are genuinely different numbers, not one space twice",
              it["mask"] != it["mask_frame"],
              "both projections are identical — one of them is in the wrong space")

        # Another annotator's outline must never leak into my item: the same
        # independence rule that keeps n_annotators from carrying WHAT was said.
        lc = duckdb.connect(config.paths.labels_db_path)
        try:
            lc.execute("UPDATE mask_edits SET annotator = 'someone_else'")
        finally:
            lc.close()
        mine = client.get("/api/label/queue").json()["items"]
        theirs = [i for i in mine if i["instance_key"] == item["instance_key"]]
        check("another annotator's outline is not shown to me",
              theirs and theirs[0]["mask"] is None, str(theirs[0]["mask"] if theirs else None))


def main():
    print("=== test_labels_api ===")
    test_queue_shape()
    test_queue_key_roundtrips_to_submit()
    test_submit_rejects_a_forged_anchor()
    test_stale_taxonomy_revision_is_409()
    test_queue_policy()
    test_multi_annotator_default()
    test_serve_event_and_time_on_task()
    test_decision_event_detail_is_stored()
    test_undo_is_scoped_to_me()
    test_mask_fix_roundtrip_and_validation()
    test_false_positive_retires_the_instance_for_everyone()
    test_zoom_ladder_and_frame_space_submit()
    test_ok_verdict_is_a_measurement_not_a_noop()
    test_ok_never_destroys_my_own_correction()
    test_crop_frame_coordinate_roundtrip()
    test_saved_outline_comes_back_on_the_queue_item()
    test_crop_endpoint_safety_and_caching()
    test_banner_mask_does_not_blank_the_tile()
    test_frame_endpoint_safety_and_caching()
    test_frame_banner_is_masked()
    test_every_queue_item_frame_url_resolves()
    print("=======================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
