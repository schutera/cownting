#!/usr/bin/env python3
"""Did the rescore actually finish, and does the corpus hold ONE model's opinion?

Read-only. Run after step 7. `scored == detections` and `revs == 1` are not
sufficient checks, and a workflow of 32 agents proved why on throwaway databases:

  * A detection whose CROP CANNOT BE RENDERED is skipped without a rev being
    written (infer.py, `if not jpeg: unreadable += 1; continue`). The production
    route is not a disk fault — `render_crop` deterministically refuses any crop
    the timestamp-banner mask would cover more than `max_banner_fraction` of, and
    these cameras look down with the herd crossing the bottom of frame. A frame
    is pending if ANY of its detections lacks the rev, so one such row keeps that
    frame in `pending_frames` FOREVER: `scored < detections` permanently, and an
    operator told to expect equality re-runs the pass forever.
  * `count(DISTINCT attr_model_rev)` IGNORES NULL, so `revs == 1` passes happily
    over unscored rows.
  * A row the PREVIOUS model scored, whose crop stops rendering before the new
    pass, keeps its OLD rev — that is the real source of `revs == 2`.
  * `clipped_detections` and the archive DB are NOT reached by any rescore, in
    any mode. Restoring a clip puts old-rev or unscored rows back into the live
    table, and nothing prompts anyone to look.

So the honest check is `scored + unscorable == detections`, with the shortfall
written down once as a baseline.

Usage, on the box:

    scp .claude/skills/new-labels/scripts/verify-rescore.py <box>:/tmp/
    ssh <box> 'docker cp /tmp/verify-rescore.py cownting-cownting-1:/tmp/ && \
               docker exec -u cownting cownting-cownting-1 python /tmp/verify-rescore.py'
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import duckdb

FRAME_SCOPE = ("d.frame_path = f.frame_path AND d.camera_id = f.camera_id "
               "AND d.dataset_id IS NOT DISTINCT FROM f.dataset_id")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/cownting.duckdb")
    ap.add_argument("--archive", default="/app/data/cownting_archive.duckdb")
    ap.add_argument("--weights", default="/app/data/models/instance_attrs.pt")
    a = ap.parse_args()
    bad = 0

    expect = None
    if os.path.exists(a.weights):
        expect = hashlib.sha256(open(a.weights, "rb").read()).hexdigest()[:12]
        print(f"deployed weights rev: {expect}")

    con = duckdb.connect(a.db, read_only=True)
    try:
        n, scored, revs = con.execute(
            "SELECT count(*), count(attr_model_rev), count(DISTINCT attr_model_rev) "
            "FROM detections").fetchone()
        print(f"\ndetections {n}, scored {scored}, distinct rev(s) {revs}")
        for r in con.execute("SELECT attr_model_rev, count(*) FROM detections "
                             "GROUP BY 1 ORDER BY 2 DESC").fetchall():
            tag = ""
            if r[0] is None:
                tag = "  <- NEVER SCORED (invisible to the revs count)"
            elif expect and r[0] != expect:
                tag = "  <- NOT the deployed rev"
            print(f"  {r[0]}: {r[1]}{tag}")

        if revs > 1:
            print("\nFAIL: the corpus holds more than one model's opinion. Every gated")
            print("      count and every pivot is a blend. Find the stragglers:")
            print("      SELECT dataset_id, camera_id, attr_model_rev, count(*) FROM")
            print("      detections GROUP BY 1,2,3 ORDER BY 3;")
            bad += 1
        if expect and revs == 1 and scored:
            only = con.execute("SELECT DISTINCT attr_model_rev FROM detections "
                               "WHERE attr_model_rev IS NOT NULL").fetchone()[0]
            if only != expect:
                print(f"\nFAIL: the corpus is scored by {only}, not the deployed {expect}.")
                bad += 1

        unscored = n - scored
        if unscored:
            print(f"\n{unscored} row(s) carry no rev at all. Distinguish two cases:")
            nb = con.execute("SELECT count(*) FROM detections "
                             "WHERE attr_model_rev IS NULL AND bbox_x1 IS NULL").fetchone()[0]
            print(f"  NULL bbox (permanently unscorable): {nb}")
            print(f"  other (unrenderable crop, or the pass has not reached them): "
                  f"{unscored - nb}")
            print("  Read the job message on GET /api/uploads — the pass reports an")
            print("  `unreadable` count. If unscored == that count, the pass IS")
            print("  complete and this is the standing condition: record the number")
            print("  as a baseline. If unscored is larger, the pass really is unfinished.")
            bad += 1
        else:
            print("\nok: every detection carries a rev — no permanently-stuck frames.")

        # --- overlays
        frames, stamped, stale = con.execute(f"""
            SELECT count(*),
                   count(*) FILTER (WHERE f.overlay_rev IS NOT NULL),
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM detections d WHERE {FRAME_SCOPE})
                     AND f.overlay_rev IS DISTINCT FROM (
                       SELECT max(d.attr_model_rev) FROM detections d WHERE {FRAME_SCOPE}))
            FROM frames f""").fetchone()
        print(f"\nframes {frames}, overlay_rev stamped {stamped}, "
              f"stale AND carrying detections {stale}")
        if stale:
            print("  FAIL: those overlays still show the previous model's mask")
            print("        decisions. POST /api/reoverlay.")
            bad += 1
        else:
            print("  ok: every frame with detections has an overlay painted for "
                  "its own rev.")
            print("  (frames with no detections keep overlay_rev NULL by design)")

        mixed = con.execute(f"""
            SELECT count(*) FROM (
              SELECT f.frame_path FROM frames f
              JOIN detections d ON {FRAME_SCOPE}
              GROUP BY 1 HAVING count(DISTINCT d.attr_model_rev) > 1)""").fetchone()[0]
        if mixed:
            print(f"\n  {mixed} frame(s) carry TWO revs at once. The repaint resolves a")
            print("  frame's rev with `max(attr_model_rev)` — a LEXICOGRAPHIC max over")
            print("  sha256 prefixes, not a version — so such a frame can be stamped")
            print("  with the OLDER rev and then read as clean forever.")
            bad += 1

        # --- what a rescore cannot reach
        print("\n--- outside every rescore's reach ---")
        for t in ("clipped_detections", "clipped_frames"):
            try:
                c = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except Exception:
                c = 0
            print(f"  {t}: {c}")
        try:
            rows = con.execute(
                "SELECT dataset_id, camera_id, count(*), "
                "       count(*) FILTER (WHERE attr_model_rev IS NULL) "
                "FROM clipped_detections GROUP BY 1,2").fetchall()
            for r in rows:
                print(f"    {r[0]} / {r[1]}: {r[2]} row(s), {r[3]} unscored")
            if rows:
                print("    A `restore_clip` on these puts them back into the live table")
                print("    at their old rev (or unscored). RE-SCORE AFTER ANY RESTORE.")
        except Exception:
            pass
    finally:
        con.close()

    if os.path.exists(a.archive):
        ar = duckdb.connect(a.archive, read_only=True)
        try:
            cols = {r[0] for r in ar.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'detections'").fetchall()}
            nd = ar.execute("SELECT count(*) FROM detections").fetchone()[0]
            print(f"  archive DB: {nd} detection(s)"
                  f"{'' if 'attr_model_rev' in cols else ', with NO attr_* columns at all'}")
            print("    Archived days are never scored and there is no implemented")
            print("    restore. Nothing warns about this anywhere else.")
        finally:
            ar.close()
    else:
        print("  archive DB: absent")

    print(f"\n=== {'OK' if not bad else str(bad) + ' PROBLEM(S)'} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
