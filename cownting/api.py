"""FastAPI backend: thin JSON + image layer over the DuckDB store and pipeline.

Serves the React frontend in production (mounts frontend/dist at /).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth as auth_mod
from . import db
from . import features as features_mod
from . import uploads as uploads_mod
from .config import Config
from .ingest import capture_time
from .pipeline import localize as run_localize
from .scene import regions


class AreasReq(BaseModel):
    # areas[camera] = [{"id","name","camera_polygon","ortho_polygon"}, ...]
    areas: dict[str, list[dict]] = {}


class LoginReq(BaseModel):
    username: str
    password: str


class CreateUserReq(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserReq(BaseModel):
    # Both optional: send only the field(s) to change (new password and/or role).
    password: str | None = None
    role: str | None = None


def _session_secret(config: Config) -> str:
    """The signing key for the session cookie. `COWNTING_SECRET` wins; otherwise a
    key is generated once and persisted next to the DB so restarts don't log
    everyone out. Never committed to the YAML."""
    env = os.environ.get("COWNTING_SECRET")
    if env:
        return env
    p = Path(config.paths.db_path).parent / ".session_secret"
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(32)
    p.write_text(s)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return s


def _records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _img_size(path: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


def create_app(config: Config) -> FastAPI:
    auth_on = config.auth.enabled

    # Endpoints reachable without a session: the login handshake itself + the
    # "who am I" probe the SPA uses to decide whether to show the login screen.
    PUBLIC_API = {"/api/login", "/api/logout", "/api/me"}

    def require_login(request: Request):
        """App-wide gate: every /api/* call needs a session, except the public
        handshake routes. A no-op when auth is disabled (tests / trusted LAN)."""
        if not auth_on:
            return
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API:
            return
        if not request.session.get("user"):
            raise HTTPException(401, "login required")

    def require_admin(request: Request):
        """Extra gate for /api/admin/*: the session user must be an admin. Listed
        alongside require_login on the admin routes, so it runs after login is
        already assured."""
        if not auth_on:
            return
        user = request.session.get("user")
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "admin only")

    def require_poweruser(request: Request):
        """Extra gate for data-management routes (upload / download / delete):
        the session user must be a poweruser or admin. Plain `user` accounts can
        view the dashboard but not mutate or export data. Runs after login is
        already assured by the app-wide require_login."""
        if not auth_on:
            return
        user = request.session.get("user")
        if not user or not auth_mod.can_manage_data(user.get("role")):
            raise HTTPException(403, "poweruser or admin only")

    app = FastAPI(title="Cownting API", dependencies=[Depends(require_login)])
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    # SessionMiddleware must be added AFTER CORS here so it ends up the inner
    # middleware — it needs to have populated request.session before the
    # require_login dependency runs. Only mounted when auth is on so request.session
    # is never touched in the disabled path.
    if auth_on:
        app.add_middleware(
            SessionMiddleware,
            secret_key=_session_secret(config),
            https_only=config.auth.https_only,
            same_site="lax",
            max_age=config.auth.session_max_age,
        )

    # Self-heal the schema on boot: a legacy DB gains the datasets table + the
    # dataset_id columns (NULL until `cownting migrate` stamps a day-0 package),
    # so serve never 500s on a pre-dataset DB. Idempotent (CREATE/ALTER IF NOT EXISTS).
    _boot = db.connect(config.paths.db_path)
    db.init_db(_boot)
    # Reload any upload-job snapshot from a prior process so an in-flight day's
    # progress bar survives a restart (interrupted jobs are marked failed, not
    # left pretending to run). Also fixes the job store's on-disk location.
    uploads_mod.recover_jobs(config)
    if auth_on:
        # Users live in the same DuckDB; guarantee one admin so a fresh install
        # is reachable (bootstrap creds via COWNTING_ADMIN_* env, else admin/admin).
        auth_mod.init_auth(_boot)
        warn = auth_mod.ensure_bootstrap_admin(_boot)
        if warn:
            print(f"[cownting.auth] {warn}")
    _boot.close()

    def con():
        # Read-write, NOT read_only: DuckDB rejects opening a second connection to
        # the same file with a different mode in one process ("Can't open a
        # connection ... with a different configuration"). The save path
        # (run_localize) needs a writer, so a read_only reader open at the same
        # moment (e.g. the dashboard polling during a save) would make that write
        # connection fail and 500 the POST. Everyone shares one mode.
        return db.connect(config.paths.db_path)

    def resolve_ds(c, requested: str | None) -> str | None:
        """The dataset to serve: the requested one, else the latest package. Returns
        None only when no dataset dimension exists yet (pre-migration DB) -> every
        db helper then runs whole-DB, exactly as before this feature."""
        return requested if requested is not None else db.latest_dataset(c)

    # Width of a cross-camera "instant" bucket: the capture interval for a
    # time-lapse, else the real-time bin. Cameras are linked by this timestamp
    # bucket (not frame_idx), so clips that started seconds apart still align.
    bin_seconds = config.ingest.frame_interval_seconds or config.ingest.time_bin_seconds

    # ------------------------------------------------------------------ auth
    @app.get("/api/me")
    def me(request: Request):
        """The logged-in user, or 401. When auth is disabled, reports a synthetic
        admin so the SPA renders without a login gate."""
        if not auth_on:
            return {"username": "local", "role": "admin", "auth_disabled": True}
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "not logged in")
        return {**user, "auth_disabled": False}

    @app.post("/api/login")
    def login(body: LoginReq, request: Request):
        if not auth_on:
            return {"username": "local", "role": "admin", "auth_disabled": True}
        c = con()
        user = auth_mod.authenticate(c, body.username.strip(), body.password)
        c.close()
        if not user:
            raise HTTPException(401, "invalid username or password")
        request.session["user"] = user
        print(f"[cownting.alert] LOGIN user={body.username.strip()}", flush=True)
        return {**user, "auth_disabled": False}

    @app.post("/api/logout")
    def logout(request: Request):
        if auth_on:
            request.session.clear()
        return {"ok": True}

    # --------------------------------------------------------------- admin: users
    @app.get("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_list_users():
        c = con()
        users = auth_mod.list_users(c)
        c.close()
        return users

    @app.post("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_create_user(body: CreateUserReq):
        c = con()
        try:
            auth_mod.create_user(c, body.username.strip(), body.password, body.role)
        except ValueError as e:
            c.close()
            raise HTTPException(400, str(e))
        users = auth_mod.list_users(c)
        c.close()
        return {"ok": True, "users": users}

    @app.patch("/api/admin/users/{username}", dependencies=[Depends(require_admin)])
    def admin_update_user(username: str, body: UpdateUserReq):
        c = con()
        try:
            if body.password is not None:
                auth_mod.set_password(c, username, body.password)
            if body.role is not None:
                auth_mod.set_role(c, username, body.role)
        except ValueError as e:
            c.close()
            raise HTTPException(400, str(e))
        users = auth_mod.list_users(c)
        c.close()
        return {"ok": True, "users": users}

    @app.delete("/api/admin/users/{username}", dependencies=[Depends(require_admin)])
    def admin_delete_user(username: str, request: Request):
        # Guard against locking yourself out mid-session; the store also refuses
        # to delete the last admin.
        current = request.session.get("user", {}) if auth_on else {}
        if auth_on and current.get("username") == username:
            raise HTTPException(400, "you cannot delete the account you are logged in as")
        c = con()
        try:
            auth_mod.delete_user(c, username)
        except ValueError as e:
            c.close()
            raise HTTPException(400, str(e))
        users = auth_mod.list_users(c)
        c.close()
        return {"ok": True, "users": users}

    # ------------------------------------------------------------------ datasets
    @app.get("/api/datasets")
    def get_datasets():
        """The days / data-packages available, newest first, for the day picker."""
        c = con()
        df = db.datasets(c)
        c.close()
        return _records(df)

    @app.delete("/api/datasets/{dataset_id}", dependencies=[Depends(require_poweruser)])
    def delete_dataset(dataset_id: str, confirm: str):
        """Remove a day from the dashboard by MOVING it into the archive DB — the
        day vanishes from every view but its rows are preserved, not destroyed.

        Guarded: the caller must pass `confirm` = the capture day as `ddmmyy` (or the
        dataset id itself when the day is unknown). The server re-derives the expected
        value from the stored day, so the typed-date gate holds even if the frontend
        is bypassed. Mismatch -> 400; unknown id -> 404."""
        c = con()
        day = db.dataset_day(c, dataset_id)
        exists = c.execute(
            "SELECT count(*) FROM datasets WHERE dataset_id = ?", [dataset_id]
        ).fetchone()[0]
        if not exists:
            c.close()
            raise HTTPException(404, f"unknown dataset {dataset_id!r}")
        expected = day.strftime("%d%m%y") if day is not None else dataset_id
        if confirm.strip() != expected:
            c.close()
            raise HTTPException(400, "confirmation does not match the day's date (ddmmyy)")
        moved = db.archive_dataset(c, dataset_id, config.paths.archive_db_path)
        c.close()
        return {"ok": True, "dataset_id": dataset_id, "detections_archived": moved}

    # ------------------------------------------------------------------ data
    @app.get("/api/site")
    def site(dataset: str | None = None):
        c = con()
        ds = resolve_ds(c, dataset)
        cams = db.cameras(c, ds)
        kpis = db.kpi_summary(c, ds)
        refs = {}
        for cam in cams:
            rf = db.reference_frame(c, cam, ds)
            if rf:
                w, h = _img_size(rf)
                q = f"?dataset={ds}" if ds else ""
                refs[cam] = {"url": f"/api/img/reference/{cam}{q}", "width": w, "height": h}
        c.close()
        ortho = None
        if config.paths.orthophoto and Path(config.paths.orthophoto).exists():
            w, h = _img_size(config.paths.orthophoto)
            ortho = {"url": "/api/img/orthophoto", "width": w, "height": h}
        return {"cameras": cams, "kpis": kpis, "orthophoto": ortho,
                "references": refs, "dataset": ds,
                "posture_enabled": config.posture.enabled,
                "pose_enabled": config.flags.pose_enabled}

    @app.get("/api/counts")
    def counts(camera: str, trunc: str = "hour", dataset: str | None = None):
        c = con()
        df = db.counts_over_time(c, camera, trunc, resolve_ds(c, dataset))
        c.close()
        return _records(df)

    @app.get("/api/posture")
    def posture(camera: str, trunc: str = "hour", dataset: str | None = None):
        c = con()
        df = db.posture_over_time(c, camera, trunc, resolve_ds(c, dataset))
        c.close()
        if df.empty:
            return []
        wide = df.pivot_table(index="t", columns="posture", values="n", fill_value=0).reset_index()
        return _records(wide)

    @app.get("/api/frames")
    def frames(camera: str, dataset: str | None = None):
        c = con()
        df = db.frames_df(c, camera, resolve_ds(c, dataset))
        c.close()
        return _records(df[["frame_idx", "ts"]])

    @app.get("/api/areas")
    def get_areas():
        return regions.load_count_areas(config.paths.count_areas)

    @app.post("/api/areas")
    def set_areas(req: AreasReq):
        regions.save_count_areas(config.paths.count_areas, req.areas)
        run_localize(config)
        return {"ok": True}

    @app.get("/api/panel-areas")
    def get_panel_areas():
        """Shelter regions (same polygon shape as count areas). A cow inside one
        is 'under a panel'."""
        return regions.load_count_areas(config.paths.panel_areas)

    @app.post("/api/panel-areas")
    def set_panel_areas(req: AreasReq):
        regions.save_count_areas(config.paths.panel_areas, req.areas)
        run_localize(config)
        return {"ok": True}

    @app.get("/api/area-counts")
    def area_counts(frame: int | None = None, dataset: str | None = None):
        """Cow counts per region, split by posture, for the occupancy map.

        With `frame` (an *instant* bucket key from the timeline, not a frame_idx):
        the cows present in each area at that instant, summed across every camera
        whose footage falls in the bucket (does NOT accumulate over the day).
        Without `frame` (the map's "whole day" toggle): the whole-day PEAK
        simultaneous occupancy per area — NOT the latest frame, which at dusk is
        empty and used to blank the map.

        Returns `counts` (total per region) plus `postures`
        (`{region_id: {standing, lying, unknown}}`) for the per-area composition
        ring, and `sheltering` (`{region_id: n}`) = that area's cows under a panel,
        for the unit-block indicator. Posture is the reused proxy; NULL -> unknown.
        """
        c = con()
        ds = resolve_ds(c, dataset)
        if frame is None:
            df = db.area_counts_whole_day(c, ds, bin_seconds)
            c.close()
            counts = {r.region_id: int(r.peak) for r in df.itertuples()}
            sheltering = {r.region_id: int(r.sheltering) for r in df.itertuples()}
            postures = {
                r.region_id: {
                    "standing": int(r.standing),
                    "lying": int(r.lying),
                    "unknown": int(r.unknown),
                }
                for r in df.itertuples()
            }
            return {
                "counts": counts,
                "postures": postures,
                "sheltering": sheltering,
                "frame": None,
            }
        ds_filter = " AND f.dataset_id = ?" if ds else ""
        rows = c.execute(
            "SELECT d.region_id, coalesce(d.posture, 'unknown') AS posture, "
            "       count(*) AS n, count(*) FILTER (WHERE d.under_panel) AS shel "
            "FROM detections d "
            "JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path "
            f"WHERE d.region_id IS NOT NULL AND {db._instant_expr(bin_seconds)} = ?{ds_filter} "
            "GROUP BY d.region_id, posture",
            [frame] + ([ds] if ds else []),
        ).fetchall()
        c.close()
        counts: dict[str, int] = {}
        postures: dict[str, dict[str, int]] = {}
        sheltering: dict[str, int] = {}
        for rid, posture, n, shel in rows:
            n = int(n)
            counts[rid] = counts.get(rid, 0) + n
            sheltering[rid] = sheltering.get(rid, 0) + int(shel or 0)
            slot = postures.setdefault(rid, {"standing": 0, "lying": 0, "unknown": 0})
            slot[posture if posture in slot else "unknown"] += n
        return {"counts": counts, "postures": postures, "sheltering": sheltering, "frame": frame}

    @app.get("/api/day-series")
    def day_series(dataset: str | None = None):
        """Per-instant metric arrays (summed across cameras) for the time-of-day
        bar strips: total / standing / lying / sheltering / open. `frames` is the
        instant-bucket axis (shared with the scrubber); `times` gives each
        bucket's wall-clock ts for labelling. Cameras are linked by timestamp."""
        c = con()
        df = db.day_series(c, resolve_ds(c, dataset), bin_seconds)
        c.close()
        keys = ["total", "standing", "lying", "sheltering", "open"]
        if df.empty:
            return {"frames": [], "times": [], **{k: [] for k in keys}}
        return {
            "frames": [int(x) for x in df["instant"].tolist()],
            "times": [t.isoformat() for t in df["ts"].tolist()],
            **{k: [int(x) for x in df[k].tolist()] for k in keys},
        }

    @app.get("/api/area-summary")
    def area_summary(dataset: str | None = None):
        """Whole-day per-area totals + standing/lying split (static KPI list)."""
        c = con()
        df = db.area_summary(c, resolve_ds(c, dataset))
        c.close()
        return _records(df)

    @app.get("/api/area-counts/over-time")
    def area_counts_over_time(camera: str | None = None, trunc: str = "hour", dataset: str | None = None):
        c = con()
        df = db.area_counts_over_time(c, camera, trunc, resolve_ds(c, dataset))
        c.close()
        return {"series": _records(df)}

    @app.get("/api/timeline")
    def timeline(dataset: str | None = None):
        """Instant axis for the day scrubber: sorted instant-bucket keys, each
        bucket's wall-clock `times`, and total cow detections per instant (summed
        across cameras) for the activity strip. Instants link cameras by
        timestamp, so staggered start times still line up."""
        c = con()
        df = db.timeline_series(c, resolve_ds(c, dataset), bin_seconds)
        c.close()
        if df.empty:
            return {"frames": [], "times": [], "counts": [], "min_frame": 0, "max_frame": 0}
        frames = [int(x) for x in df["instant"].tolist()]
        counts = [int(x) for x in df["n"].tolist()]
        times = [t.isoformat() for t in df["ts"].tolist()]
        return {"frames": frames, "times": times, "counts": counts,
                "min_frame": frames[0], "max_frame": frames[-1]}

    @app.get("/api/frame-map")
    def frame_map(frame: int, dataset: str | None = None):
        """The per-camera frame_idx to show at one instant bucket (`frame`), so the
        seg views can display each camera's own frame for that timestamp. Cameras
        with no footage in the bucket are omitted (not yet online / offline)."""
        c = con()
        df = db.frames_at_instant(c, frame, bin_seconds, resolve_ds(c, dataset))
        c.close()
        return {r.camera_id: int(r.frame_idx) for r in df.itertuples()}

    # ------------------------------------------------------------------ images
    @app.get("/api/img/orthophoto")
    def img_ortho():
        p = config.paths.orthophoto
        if not p or not Path(p).exists():
            raise HTTPException(404, "no orthophoto")
        return FileResponse(p)

    @app.get("/api/img/reference/{camera}")
    def img_reference(camera: str, dataset: str | None = None):
        """A representative frame for a camera."""
        c = con()
        rf = db.reference_frame(c, camera, resolve_ds(c, dataset))
        c.close()
        if not rf or not Path(rf).exists():
            raise HTTPException(404, "no reference frame")
        return FileResponse(rf)

    @app.get("/api/img/frame/{camera}/{frame_idx}")
    def img_frame(camera: str, frame_idx: int, kind: str = "overlay", dataset: str | None = None):
        c = con()
        ds = resolve_ds(c, dataset)
        ds_filter = " AND dataset_id = ?" if ds else ""
        row = c.execute(
            f"SELECT frame_path, overlay_path, pose_overlay_path FROM frames "
            f"WHERE camera_id = ? AND frame_idx = ?{ds_filter}",
            [camera, frame_idx] + ([ds] if ds else []),
        ).fetchone()
        c.close()
        if not row:
            raise HTTPException(404, "frame not found")
        # kind: raw -> source frame, overlay -> seg masks, pose -> keypoint skeleton.
        # Each baked layer falls back to the raw frame if it wasn't produced.
        picks = {"overlay": row[1], "pose": row[2]}
        path = picks.get(kind) or row[0]
        if not path or not Path(path).exists():
            raise HTTPException(404, "image missing")
        return FileResponse(path)

    # ------------------------------------------------------------------ shelter
    @app.get("/api/shelter")
    def shelter(camera: str = "all", trunc: str = "hour", dataset: str | None = None):
        """Sheltering (under-panel) counts over time. `camera='all'` (or missing)
        aggregates across all cameras."""
        cam = None if camera == "all" else camera
        c = con()
        df = db.shelter_over_time(c, cam, trunc, resolve_ds(c, dataset))
        c.close()
        return _records(df)

    @app.post("/api/localize")
    def localize():
        return {"updated": run_localize(config)}

    # ------------------------------------------------------------------ uploads
    @app.post("/api/uploads", dependencies=[Depends(require_poweruser)])
    def create_upload(
        videos: list[UploadFile] = File(...),
        cameras: list[str] = Form(...),
        day: str | None = Form(None),
        label: str | None = Form(None),
    ):
        """Land one video per camera, then auto-process the new day in the
        background. Sync def so Starlette runs it in a threadpool — streaming
        multi-GB files must not block the event loop. Returns 202 + a job id the
        frontend polls at GET /api/uploads/{job_id}.

        The capture time is read from each file itself (see cownting.ingest.
        capture_time): the MP4/QuickTime container creation_time first, else the
        timestamp Brinno burns into the video's bottom bar — the only in-file
        source of date AND time for that hardware, which zeroes its container
        timestamps. It falls to the user only when neither yields a date for any
        camera, or the cameras DISAGREE on the date — the endpoint then returns
        422 with detail.code == 'capture_day_required' so the frontend prompts for
        the day and re-submits it as `day` (an explicit override). Otherwise the
        day is the single date the files agree on, each camera keeping its own
        time as its start (cameras lacking any source borrow the earliest known)."""
        if not videos:
            raise HTTPException(400, "no videos uploaded")
        if len(videos) != len(cameras):
            raise HTTPException(400, "each video needs exactly one camera name")

        ids = [c.strip() for c in cameras]
        for cid in ids:
            if not uploads_mod.valid_camera_id(cid):
                raise HTTPException(400, f"invalid camera name {cid!r} (use letters, digits, _ or -)")
        if len(set(ids)) != len(ids):
            raise HTTPException(400, "camera names must be unique")
        for up in videos:
            if not uploads_mod.allowed_ext(up.filename or ""):
                raise HTTPException(400, f"unsupported file type: {up.filename!r}")

        # Optional user override (only used as a fallback when metadata is
        # unreadable): a date the user typed into the prompted picker.
        manual_day = None
        if day and day.strip():
            try:
                manual_day = date.fromisoformat(day.strip())
            except ValueError:
                raise HTTPException(400, f"day must be ISO 'YYYY-MM-DD', got {day!r}")

        # Land the clips in a temp inbox first; the day (needed for the durable
        # inbox path + dataset id) isn't known until we've read their metadata.
        tmp = Path(config.paths.artifacts_dir) / "_uploads" / f"_incoming-{uuid.uuid4().hex}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            landed: list[tuple[str, Path]] = []
            for up, cid in zip(videos, ids):
                ext = Path(up.filename or "").suffix.lower()
                dest = tmp / f"{cid}{ext}"
                with open(dest, "wb") as f:
                    shutil.copyfileobj(up.file, f)
                landed.append((cid, dest))

            # Capture time from each video file itself: container creation_time,
            # else the burned-in Brinno bar (the only in-file source for that
            # hardware). A user override, when supplied, wins and skips both
            # (time-of-day unknown from a typed date, so midnight).
            starts: dict[str, datetime] = {}
            if manual_day is not None:
                midnight = datetime(manual_day.year, manual_day.month, manual_day.day)
                for cid, _dest in landed:
                    starts[cid] = midnight
                iso_day = manual_day.isoformat()
            else:
                read: dict[str, datetime] = {}
                for cid, dest in landed:
                    when = capture_time.read_container_time(dest)
                    if when is None:
                        when = capture_time.read_burned_timestamp(dest)
                    if when is not None:
                        read[cid] = when
                distinct_dates = sorted({dt.date() for dt in read.values()})

                # Ask the user for the day only when the files can't decide it:
                # neither metadata nor the burned-in bar yields a date for ANY
                # camera, or the cameras DISAGREE on it. A single readable date
                # (even if some cameras lack any source) stands.
                if not read or len(distinct_dates) > 1:
                    msg = (
                        "Couldn't read a recording date from the video "
                        "(no metadata, no on-screen date). Please set the capture day."
                        if not read else
                        "Cameras disagree on the recording date "
                        f"({', '.join(d.isoformat() for d in distinct_dates)}). "
                        "Please set the capture day."
                    )
                    raise HTTPException(422, detail={
                        "code": "capture_day_required",
                        "dates": [d.isoformat() for d in distinct_dates],
                        "message": msg,
                    })

                iso_day = distinct_dates[0].isoformat()
                # Cameras with metadata keep their own start; any without borrow
                # the earliest known start (same day) so time-of-day stays aligned.
                proxy = min(read.values())
                for cid, _dest in landed:
                    starts[cid] = read.get(cid, proxy)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        # A stable dataset id = the day slug, so re-uploading the same day replaces
        # rather than duplicates (ingest purges the dataset before re-inserting).
        the_label = (label or "").strip() or date.fromisoformat(iso_day).strftime("%b %d, %Y")

        # Fresh inbox per (re-)upload of this day so a replaced camera set leaves no
        # stale videos behind.
        inbox = Path(config.paths.artifacts_dir) / "_uploads" / iso_day
        if inbox.exists():
            shutil.rmtree(inbox)
        tmp.rename(inbox)

        saved: list[tuple[str, str, str]] = []
        for cid, dest in landed:
            saved.append((cid, str(inbox / dest.name), starts[cid].isoformat()))

        job = uploads_mod.start_upload_job(config, saved, iso_day, iso_day, the_label)
        return JSONResponse(status_code=202, content=uploads_mod.job_dict(job))

    @app.get("/api/uploads")
    def list_uploads():
        """All known upload jobs, newest first (active ones lead). Lets any client
        — a page refresh, a second tab, another user — discover a running upload
        and reconnect its progress bar; the job store is process-wide, not tied to
        the tab that started it."""
        return uploads_mod.list_jobs()

    @app.get("/api/uploads/{job_id}")
    def upload_status(job_id: str):
        job = uploads_mod.get_job(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return uploads_mod.job_dict(job)

    # ------------------------------------------------------------------ cross-filter
    @app.get("/api/features")
    def features():
        """The pivotable features + whether each is populated yet (reserved ones
        like shade light up automatically once their column is written)."""
        c = con()
        avail = db.available_features(c)
        c.close()
        return [{"key": k, "kind": s.kind, "available": avail.get(k, True)}
                for k, s in features_mod.FEATURES.items()]

    @app.get("/api/crosstab")
    def crosstab(primary: str, breakdown: str | None = None,
                 dataset: str | None = None, camera: str | None = None,
                 frame: int | None = None):
        """Generic two-feature pivot: count of detections grouped by primary x
        breakdown, for the interactive analysis. `dataset='all'` = whole DB;
        omitted = latest package."""
        c = con()
        try:
            ds = None if dataset == "all" else resolve_ds(c, dataset)
            df, pdom, bdom = db.crosstab(c, primary, breakdown, dataset_id=ds,
                                         camera_id=camera, frame=frame)
        except ValueError as e:
            c.close()
            raise HTTPException(400, str(e))
        c.close()
        return {
            "primary": primary, "breakdown": breakdown,
            "primary_domain": pdom, "breakdown_domain": bdom,
            "cells": _records(df),
            "primary_totals": {str(k): int(v) for k, v in
                               df.groupby("primary")["n"].sum().items()},
            "total": int(df["n"].sum()) if not df.empty else 0,
        }

    @app.get("/api/export.csv", dependencies=[Depends(require_poweruser)])
    def export_csv(dataset: str | None = None):
        """CSV export: one row per detection joined to its frame. Whole DB by
        default (all days); pass ?dataset=<id> to scope to one data-package."""
        c = con()
        df = db.export_df(c, dataset)  # raw param: None -> whole DB, not latest
        c.close()
        return Response(
            content=df.to_csv(index=False),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="cownting_export.csv"'},
        )

    # ------------------------------------------------------------------ static frontend (prod)
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(dist / "index.html"))  # client-side routing fallback

    return app
