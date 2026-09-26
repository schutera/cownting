#!/usr/bin/env python3
"""Is there enough new labeled work to justify retraining? Read-only.

Run this FIRST, before backing anything up or stopping the container. It answers
the only question that decides whether the rest of the drill is worth doing, and
it answers it by comparing the live label store against the DEPLOYED MODEL'S OWN
DATASET MANIFEST — not against a remembered count.

Two traps it exists to avoid, both of which caught me live:

  * THE TRAINING POPULATION IS `annotations` UNION `mask_edits`. Behaviour and
    sun come from `annotations`; the cow gate comes from `mask_edits`
    (`false_positive` -> not_cow, `ok`/`polygon` -> cow — see
    instance_attrs/dataset.py). Counting only `v_current_answers` understated the
    corpus by ~450 instances and made the comparison look like a catastrophic
    divergence when nothing was wrong at all.

  * `manifest.json`'s `instance_key` field holds the store's `effective_key` at
    build time, so compare against `effective_key`, never `instance_key`.

Usage, on the box (paths are the in-container ones):

    scp .claude/skills/new-labels/scripts/label-delta.py <box>:/tmp/
    ssh <box> 'docker cp /tmp/label-delta.py cownting-cownting-1:/tmp/ && \
               docker exec -u cownting cownting-cownting-1 python /tmp/label-delta.py'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import duckdb

# Below this much new work, a retrain chases noise: the 5-fold spread on this
# corpus is around +/-0.013 macro-F1, and a single-digit-percent increase in
# training data does not clear it. Costed against a container stop, a GPU run, a
# deploy and a whole-corpus rescore. Raise it, never silently ignore it.
MIN_NEW_INSTANCES = 150
MIN_NEW_FRACTION = 0.05


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/app/data/labels.duckdb")
    ap.add_argument("--manifest",
                    default="/app/data/instance_attrs/dataset/manifest.json")
    ap.add_argument("--weights", default="/app/data/models/instance_attrs.pt")
    a = ap.parse_args()

    if not os.path.exists(a.manifest):
        print(f"no dataset manifest at {a.manifest} — nothing has ever been built, "
              f"so every label is new. Run the full drill.")
        return 0

    man = json.load(open(a.manifest, encoding="utf-8"))
    mkeys = {i["instance_key"] for i in man["items"] if i.get("instance_key")}
    built = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(os.path.getmtime(a.manifest)))
    print(f"deployed dataset : {len(mkeys)} instances, built {built}")
    if os.path.exists(a.weights):
        import hashlib
        blob = open(a.weights, "rb").read()
        print(f"deployed weights : rev {hashlib.sha256(blob).hexdigest()[:12]}, "
              f"written {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(a.weights)))}")

    con = duckdb.connect(a.labels, read_only=True)
    try:
        # THE UNION. Anything less is not the training population.
        ann = {r[0] for r in con.execute(
            "SELECT DISTINCT effective_key FROM v_current_answers").fetchall()}
        edits = {r[0] for r in con.execute(
            "SELECT DISTINCT effective_key FROM mask_edits "
            "WHERE superseded_at IS NULL").fetchall()}
        have = ann | edits
        fresh = have - mkeys
        lost = mkeys - have

        print(f"\nlabel store      : {len(have)} instances carrying signal "
              f"({len(ann)} answered, {len(edits)} with a geometry verdict)")
        print(f"  already trained on : {len(have & mkeys)}")
        print(f"  NEW               : {len(fresh)}")
        if lost:
            print(f"  in the dataset but no longer carrying signal: {len(lost)} "
                  f"(expect 0 — investigate before retraining)")

        newest = con.execute("SELECT greatest("
                             " (SELECT max(submitted_at) FROM annotations),"
                             " (SELECT max(submitted_at) FROM mask_edits))").fetchone()[0]
        print(f"  newest submission  : {newest}")

        if fresh:
            print("\nwhich selector served the new work "
                  "(this is the active-learning audit's raw material):")
            rows = con.execute(
                "SELECT coalesce(queue_reason,'(none)'), count(DISTINCT effective_key) "
                "FROM annotations WHERE superseded_at IS NULL "
                "  AND effective_key IN (SELECT unnest(?::VARCHAR[])) "
                "GROUP BY 1 ORDER BY 2 DESC", [list(fresh)]).fetchall()
            for r in rows:
                print(f"    {r[0]:<16} {r[1]}")
            if not any(r[0] == "uniform" for r in rows):
                print("    !! no `uniform` instances in this batch — the unbiased")
                print("       control slice got nothing, so step 9's audit has no")
                print("       baseline to compare the selectors against.")

            print("\nwhat the new work was answered as:")
            for r in con.execute(
                    "SELECT group_key, class_name, count(*) FROM v_current_answers "
                    "WHERE effective_key IN (SELECT unnest(?::VARCHAR[])) "
                    "GROUP BY 1,2 ORDER BY 1, 3 DESC", [list(fresh)]).fetchall():
                print(f"    {r[0]:<14} {r[1]:<18} {r[2]}")
    finally:
        con.close()

    frac = len(fresh) / max(1, len(mkeys))
    print(f"\n=== {len(fresh)} new instances, +{frac * 100:.1f}% on {len(mkeys)} ===")
    if len(fresh) >= MIN_NEW_INSTANCES or frac >= MIN_NEW_FRACTION:
        print("ENOUGH — run the drill.")
        return 0
    print(f"NOT ENOUGH (threshold: {MIN_NEW_INSTANCES} instances or "
          f"{MIN_NEW_FRACTION * 100:.0f}%).")
    print("A retrain on this would chase noise: the 5-fold spread is around")
    print("+/-0.013 macro-F1 and this much extra data does not clear it. Report the")
    print("numbers, say so, and stop. Do NOT stop the container or rescore.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
