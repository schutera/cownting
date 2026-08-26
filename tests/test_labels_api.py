"""Label HTTP-contract tests on a hermetic synthetic DB (the tests/test_api.py
shape: AuthCfg(enabled=False), temp DuckDB files, no network).

Covers the frozen M3 route table: the queue's item shape and sampling policy,
the queue-key -> submit round trip (the end-to-end version of the SQL/Python key
pin), anchor forgery, the taxonomy-stale 409, served-event telemetry, undo
scoping, and the crop endpoint's path safety, caching and banner masking.

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

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from cownting import db, labels_db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

# Contract tests hit /api/label/* directly; the gates themselves belong to
# tests/test_auth.py. With auth off every write lands as annotator='local'.
_NO_AUTH = AuthCfg(enabled=False)

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


# The frozen §5.3 item shape LabelItem mirrors. Every key must be present on
# every served item — the frontend types against exactly this.
_ITEM_FIELDS = (
    "instance_key", "dataset_id", "day", "camera_id", "frame_file", "bbox",
    "ordinal", "score", "frame_sig", "crop_url", "crop_w", "crop_h", "ring",
    "n_annotators", "target", "overlap", "serve_event_id",
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
    test_crop_endpoint_safety_and_caching()
    test_banner_mask_does_not_blank_the_tile()
    print("=======================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
