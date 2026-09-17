"""Score `detections` with a trained checkpoint, one frame at a time.

GROUPED BY FRAME, NOT BY DETECTION, and that is the difference between a
practical pass and an impractical one: 46,486 detections sit on 7,261 frames —
about 6.4 each — so a per-detection loop decodes every JPEG six times over. Here
each frame is opened once and all its crops are cut from the one decode.

The crops are cut by `labeling.render_crop`, the same call the dataset builder
made, and the checkpoint carries the geometry it was trained with so the two can
be compared rather than assumed. Serving a model on differently-cut pixels than
it trained on is silent: the numbers stay plausible and the accuracy is gone.

Writes are batched per frame-chunk and every row carries `attr_model_rev`, so a
pass interrupted halfway leaves a database that can say exactly which rows the
new model has reached.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import db, labeling
from ..config import Config, resolve_device
from . import model as M
from .dataset import COW_HEAD

# Rows updated per transaction. Small enough that an interrupted pass loses
# little, large enough that the per-statement overhead disappears.
CHUNK_FRAMES = 64


def _scored_columns() -> tuple[str, ...]:
    return ("attr_is_cow", "attr_cow_p", "attr_sun", "attr_sun_p",
            "attr_behaviour", "attr_behaviour_p", "attr_model_rev")


def pending_frames(con, *, dataset_id: str | None = None, camera_id: str | None = None,
                   rev: str | None = None, rescore: bool = False) -> list[tuple]:
    """The (dataset, camera, frame_path) groups still needing this model's opinion.

    `rev` + `rescore=False` is what makes a pass resumable and idempotent: a
    frame every one of whose detections already carries THIS rev is skipped, so
    re-running after an interruption continues rather than restarts, and running
    twice is free. A different rev means a different model, and those rows are
    re-scored — which is the correct default, because a database holding two
    models' opinions in one column is not interpretable.
    """
    where = ["frame_path IS NOT NULL"]
    params: list[Any] = []
    if dataset_id:
        where.append("dataset_id = ?")
        params.append(dataset_id)
    if camera_id:
        where.append("camera_id = ?")
        params.append(camera_id)
    if rev is not None and not rescore:
        where.append("(attr_model_rev IS NULL OR attr_model_rev <> ?)")
        params.append(rev)
    return con.execute(
        f"""SELECT dataset_id, camera_id, frame_path, count(*) AS n
            FROM detections WHERE {' AND '.join(where)}
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
        params,
    ).fetchall()


def _preprocess(jpeg: bytes, img_size: int, mean, std):
    """JPEG bytes -> a normalised CHW tensor, matching the val transform exactly.

    Deliberately NOT the training transform: RandomResizedCrop and the flip are
    there to perturb, and perturbing at inference would make the same crop score
    differently on two passes.
    """
    from io import BytesIO

    import torch
    from PIL import Image
    from torchvision import transforms as T

    tf = T.Compose([T.Resize((img_size, img_size)), T.ToTensor(), T.Normalize(mean, std)])
    with Image.open(BytesIO(jpeg)) as im:
        return tf(im.convert("RGB"))


def score_detections(
    config: Config,
    con,
    *,
    dataset_id: str | None = None,
    camera_id: str | None = None,
    limit: int | None = None,
    rescore: bool = False,
    weights: str | Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the model over `detections` and write the verdicts. Returns a summary.

    `con` is an OPEN main-DB connection supplied by the caller, never opened
    here. That is the whole in-process contract: the server already holds the
    single permitted read-write handle, so this must borrow it rather than try
    to take a second one (see `api.remask` and `uploads.start_remask_job` for
    the same rule and what breaks without it).
    """
    import torch

    cfg = config.instance_attrs
    ckpt_path = Path(weights) if weights is not None else Path(cfg.weights)
    if not ckpt_path.exists():
        raise FileNotFoundError(
            f"{ckpt_path}: no checkpoint. Train one with `cownting attrs train`.")
    blob = M.load_checkpoint(ckpt_path, device="cpu")
    device = resolve_device(cfg.device)
    net = blob["model"].to(device)
    heads: dict[str, list[str]] = blob["heads"]
    img_size = int(blob["img_size"])
    mean, std = blob["norm"]["mean"], blob["norm"]["std"]
    rev = blob["rev"]

    # The crop geometry the model was TRAINED with, against the one this
    # deployment would serve. A mismatch is not fatal — a re-tuned crop_pad is a
    # legitimate config change — but it is reported, because it silently costs
    # accuracy and nothing else would ever mention it.
    acfg = config.annotation
    trained = blob.get("crop") or {}
    geom_mismatch = {
        k: {"trained": trained.get(k), "serving": v}
        for k, v in (("crop_pad", acfg.crop_pad),
                     ("crop_max_width", acfg.crop_max_width),
                     ("mask_timestamp_banner", acfg.mask_timestamp_banner))
        if trained.get(k) is not None and trained.get(k) != v
    }

    groups = pending_frames(con, dataset_id=dataset_id, camera_id=camera_id,
                            rev=rev, rescore=rescore)
    if limit is not None:
        groups = groups[:limit]
    total_frames = len(groups)

    cow_i = heads.get(COW_HEAD, []).index("cow") if "cow" in heads.get(COW_HEAD, []) else None
    # The abstention rule needs to know which behaviour output is not_visible
    # and which are the substantive postures. Resolved once from the checkpoint's
    # own vocabulary; a checkpoint trained without the class simply never
    # abstains on p(nv) and only the confidence half applies.
    beh = heads.get("behaviour", [])
    nv_i = beh.index("behaviour.not_visible") if "behaviour.not_visible" in beh else None
    subst_i = [i for i in range(len(beh)) if i != nv_i]
    scored = 0
    unreadable = 0
    gated_out = 0
    abstained = 0
    pending: list[tuple] = []
    done_frames = 0

    for ds, cam, frame_path, _n in groups:
        if should_stop is not None and should_stop():
            break
        rows = con.execute(
            """SELECT detection_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2
               FROM detections
               WHERE frame_path = ? AND camera_id = ?
                 AND dataset_id IS NOT DISTINCT FROM ?""",
            [frame_path, cam, ds],
        ).fetchall()

        tensors, ids = [], []
        for det_id, x1, y1, x2, y2 in rows:
            if x1 is None:
                continue
            jpeg, _sig = labeling.render_crop(
                frame_path, (float(x1), float(y1), float(x2), float(y2)),
                pad=acfg.crop_pad, max_width=acfg.crop_max_width, cfg=acfg)
            if not jpeg:
                # Missing frame, torn JPEG, or a crop the banner mask would
                # swallow. Routine; the row simply stays unscored and a later
                # pass will retry it.
                unreadable += 1
                continue
            tensors.append(_preprocess(jpeg, img_size, mean, std))
            ids.append(det_id)

        if tensors:
            with torch.no_grad():
                out = net(torch.stack(tensors).to(device))
            probs = {h: torch.softmax(v, dim=1).cpu() for h, v in out.items()}
            for i, det_id in enumerate(ids):
                vals: dict[str, Any] = {}
                for h, cs in heads.items():
                    p = probs[h][i]
                    j = int(p.argmax())
                    if h == COW_HEAD:
                        pc = float(p[cow_i]) if cow_i is not None else float(p[j])
                        vals["attr_cow_p"] = pc
                        vals["attr_is_cow"] = bool(pc >= cfg.cow_threshold)
                        if not vals["attr_is_cow"]:
                            gated_out += 1
                    elif h == "sun_exposure":
                        vals["attr_sun"] = cs[j]
                        vals["attr_sun_p"] = float(p[j])
                    elif h == "behaviour":
                        # Abstain rather than show a posture the model cannot
                        # back (config docstring for the why). NULL class, but
                        # the probability of the posture it WOULD have shown is
                        # kept, and attr_model_rev is still written, so the row
                        # reads as 'unsure' — distinct from 'unscored'.
                        best = max(subst_i, key=lambda i: float(p[i])) if subst_i else j
                        conf = float(p[best])
                        pnv = float(p[nv_i]) if nv_i is not None else 0.0
                        if pnv >= cfg.abstain_nv_p or conf < cfg.abstain_min_conf:
                            vals["attr_behaviour"] = None
                            vals["attr_behaviour_p"] = conf
                            abstained += 1
                        else:
                            vals["attr_behaviour"] = cs[best]
                            vals["attr_behaviour_p"] = conf
                pending.append((
                    vals.get("attr_is_cow"), vals.get("attr_cow_p"),
                    vals.get("attr_sun"), vals.get("attr_sun_p"),
                    vals.get("attr_behaviour"), vals.get("attr_behaviour_p"),
                    rev, det_id,
                ))
                scored += 1

        done_frames += 1
        if len(pending) >= CHUNK_FRAMES * 8:
            _flush(con, pending)
            pending = []
        if progress is not None:
            progress(done_frames, total_frames)

    _flush(con, pending)
    return {
        "rev": rev,
        "frames": done_frames,
        "frames_total": total_frames,
        "scored": scored,
        "unreadable": unreadable,
        "gated_out": gated_out,
        "abstained": abstained,
        "cow_threshold": cfg.cow_threshold,
        "abstain": {"nv_p": cfg.abstain_nv_p, "min_conf": cfg.abstain_min_conf},
        "device": device,
        "geometry_mismatch": geom_mismatch,
        "metrics": blob.get("metrics", {}).get("mean_macro_f1"),
    }


def run_score(config: Config, **kw) -> dict[str, Any]:
    """`score_detections` on a connection of its own, opened and closed here.

    Mirrors `pipeline.run_remask`: the pass runs INSIDE the API server, where a
    leaked write handle is not a leaked descriptor but a lock every later request
    then fails on for the life of the process. So the close is in a `finally`,
    unconditionally.
    """
    con = db.connect(config.paths.db_path)
    try:
        return score_detections(config, con, **kw)
    finally:
        con.close()


def _flush(con, rows: Sequence[tuple]) -> None:
    """Write one batch. `executemany` inside an explicit transaction so an
    interrupted pass leaves whole frames written rather than half a frame."""
    if not rows:
        return
    con.execute("BEGIN")
    try:
        con.executemany(
            """UPDATE detections SET
                   attr_is_cow = ?, attr_cow_p = ?,
                   attr_sun = ?, attr_sun_p = ?,
                   attr_behaviour = ?, attr_behaviour_p = ?,
                   attr_model_rev = ?
               WHERE detection_id = ?""",
            list(rows),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def coverage(con, *, dataset_id: str | None = None) -> dict[str, Any]:
    """How much of the corpus this model has an opinion on, and what it says.

    Read by the dashboard so the gate's effect is visible as a number rather
    than as counts that quietly moved.
    """
    where, params = "", []
    if dataset_id:
        where, params = "WHERE dataset_id = ?", [dataset_id]
    row = con.execute(
        f"""SELECT count(*) AS detections,
                   count(attr_model_rev) AS scored,
                   count(*) FILTER (WHERE attr_is_cow = false) AS not_cow,
                   count(DISTINCT attr_model_rev) AS revs,
                   count(*) FILTER (WHERE attr_model_rev IS NOT NULL
                                      AND attr_behaviour IS NULL) AS unsure
            FROM detections {where}""",
        params,
    ).fetchone()
    return {"detections": int(row[0]), "scored": int(row[1]),
            "not_cow": int(row[2]), "model_revs": int(row[3]),
            "behaviour_unsure": int(row[4])}
