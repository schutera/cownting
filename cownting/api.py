"""FastAPI backend: thin JSON + image layer over the DuckDB store and pipeline.

Serves the React frontend in production (mounts frontend/dist at /).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from . import auth as auth_mod
from . import db
from . import features as features_mod
from . import labeling
from . import labels_backup
from . import labels_db
from . import uploads as uploads_mod
from .config import Config
from .ingest import capture_time
from . import localize_worker
from .scene import regions


class AreasReq(BaseModel):
    # areas[camera] = [{"id","name","camera_polygon","ortho_polygon"}, ...]
    areas: dict[str, list[dict]] = {}


class ClipReq(BaseModel):
    # Keep frames whose ts is in [start, end]; ISO 8601 timestamps.
    start: str
    end: str


class LoginReq(BaseModel):
    username: str
    password: str


class ActAsReq(BaseModel):
    # The role an admin wants to temporarily act as; "admin" returns to normal.
    role: str


class CreateUserReq(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserReq(BaseModel):
    # Both optional: send only the field(s) to change (new password and/or role).
    password: str | None = None
    role: str | None = None


class InstanceAnchor(BaseModel):
    # The provenance the queue served with an item, echoed back verbatim on
    # submit/skip. The server re-hashes it (labeling.verify_anchor) — the
    # instance key IS the hash of this, so a stored row whose key and anchor
    # disagree is unrepresentable. Field names frozen in M3 §4.1.
    dataset_id: str | None = None
    camera_id: str
    frame_file: str          # basename, "00000450.jpg"
    bbox: list[float]        # [x1, y1, x2, y2] full-frame px
    ordinal: int = 0
    ts: str | None = None
    frame_sig: str | None = None


class LabelSubmitReq(BaseModel):
    instance_key: str
    anchor: InstanceAnchor
    answers: dict[str, str | list[str]]   # group_key -> class_key(s)
    taxonomy_revision: int
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None
    input_mode: str | None = None         # 'key' | 'mouse'
    note: str | None = None


class LabelSkipReq(BaseModel):
    # The FLAG body. Skipping is no longer a user-facing concept: an instance that
    # cannot be answered is flagged, and a flag must say why. `reason` picks from
    # the SKIP_REASONS vocabulary and `explanation` is the annotator's own words —
    # both required (see label_skip). The route path and the stored outcome stay
    # 'skipped' so existing rows and the frozen M3 §4.1 route table still hold.
    # `note` is that frozen field name and remains the wire spelling; `explanation`
    # is accepted as its alias so the request reads as what it now is.
    instance_key: str
    anchor: InstanceAnchor
    reason: str                            # labels_db.SKIP_REASONS
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None
    note: str | None = None
    explanation: str | None = None


class LabelMaskFixReq(BaseModel):
    # The OUTLINE body (docs/roadmap/M4a_instance_mask_fixup.md §4.2). `kind`
    # discriminates: 'polygon' carries a corrected shape in CROP-LOCAL px (the
    # space the queue served `ring` in, converted server-side before storage),
    # 'false_positive' carries no geometry and says the detection is not a cow.
    # `mask_rev` is what the annotator's outline was drawn against; it is echoed
    # so a model re-run mid-session is a 409 rather than a correction silently
    # re-attaching to a shape nobody saw.
    instance_key: str
    anchor: InstanceAnchor
    kind: str = "polygon"
    # Which space `polygon` is in. 'frame' is what the outline editor sends: it
    # works in FULL-FRAME px so that zooming changes only which crop is on
    # screen, never the coordinates being edited. 'crop' is the original
    # crop-local contract, kept because a queued write from a client loaded
    # before this change may still be in flight.
    space: str = "crop"
    polygon: list[list[float]] | None = None
    mask_rev: str | None = None
    seeded_from: str = "mask"
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None


class LabelUndoReq(BaseModel):
    instance_key: str


class LabelEventReq(BaseModel):
    # Client-side effort telemetry: session boundaries + info_opened. The
    # server refuses the kinds it writes itself (see the route).
    session_id: str
    kind: str
    instance_key: str | None = None
    class_key: str | None = None
    detail: dict[str, Any] | None = None


class LabelGroupReq(BaseModel):
    group_key: str
    name: str
    description: str | None = None
    multi_select: bool = False
    required: bool = True


class LabelGroupPatchReq(BaseModel):
    # All optional: send only the field(s) to change. `active` False archives,
    # True restores — there is no DELETE anywhere in the label feature.
    name: str | None = None
    description: str | None = None
    multi_select: bool | None = None
    required: bool | None = None
    active: bool | None = None


class LabelClassReq(BaseModel):
    name: str
    description: str        # required server-side: an undefined option is the
                            # largest source of annotator disagreement (§5.4)
    class_key: str | None = None
    icon: str | None = None  # a labels_db.CLASS_ICONS name, picked from the fixed
                             # vocabulary — never free text, never markup
    is_escape: bool = False


class LabelClassPatchReq(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    is_escape: bool | None = None
    active: bool | None = None


class LabelMoveReq(BaseModel):
    dir: str                # "up" | "down"


class LabelBackupRunReq(BaseModel):
    force: bool = False


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


def _safe_path_id(value: str) -> bool:
    """True if `value` is safe to use as a single filesystem path segment — no
    separators, no parent refs, not empty/dot. Dataset ids are normally clean ISO
    day slugs, but they can be set via CLI/config (DatasetCfg does no slug check),
    so any id that becomes an rmtree/mkdir path segment is checked before use."""
    return bool(value) and value not in (".", "..") \
        and "/" not in value and "\\" not in value and ".." not in value


# A frame filename as ingest writes it: digits + ".jpg", nothing else. This is a
# TOTAL whitelist, deliberately narrower than "a safe filename" — the label-crop
# endpoint rebuilds an on-disk path from it (M3 §4.5), and the frame name is one
# of the very few places in this codebase where a total whitelist is available.
# Never loosen it to accept a general filename; treat any change here as a
# security review, not a feature.
_FRAME_FILE_RE = re.compile(r"^[0-9]{1,12}\.jpg$")


def _safe_frame_file(value: str) -> bool:
    return bool(_FRAME_FILE_RE.fullmatch(value or ""))


def _clip(value: str | None, limit: int) -> str | None:
    """Length-cap a client-supplied string at the API boundary (M3 §4.1).
    `require_labeler` admits every known role, so a viewer session can write
    label rows; unbounded strings would let a scripted client bloat the store
    that later gets zipped under Discord's ~10 MB cap."""
    return value if value is None else value[:limit]


def _dict_rows(cur) -> list[dict]:
    """DuckDB cursor -> list of dicts, WITHOUT pandas: `.df()` would coerce the
    label store's NULL columns (dataset_id, skip_reason, median_ms) to NaN,
    which is not JSON and does not compare equal to None downstream."""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


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

    def effective_role(request: Request) -> str | None:
        """The role the gates enforce. An admin may set session['acting_role']
        (via /api/act-as) to temporarily act as a lower role and experience the
        app exactly as that role — the gates then really 403. Only honoured when
        the REAL session role is admin, so nothing else can plant an override."""
        user = request.session.get("user")
        if not user:
            return None
        acting = request.session.get("acting_role")
        if acting in auth_mod.ROLES and user.get("role") == "admin":
            return acting
        return user.get("role")

    def require_admin(request: Request):
        """Extra gate for /api/admin/*: the session user must be an admin (by
        effective role — an admin acting as a lower role is kept out, that's the
        point of the preview). Listed alongside require_login on the admin
        routes, so it runs after login is already assured."""
        if not auth_on:
            return
        if effective_role(request) != "admin":
            raise HTTPException(403, "admin only")

    def require_poweruser(request: Request):
        """Extra gate for data-management routes (upload / download / delete):
        the session user must be a poweruser or admin (by effective role). Plain
        `user` accounts can view the dashboard but not mutate or export data.
        Runs after login is already assured by the app-wide require_login."""
        if not auth_on:
            return
        if not auth_mod.can_manage_data(effective_role(request)):
            raise HTTPException(403, "poweruser or admin only")

    def require_labeler(request: Request):
        """Labeling is the one mutation a plain `user` may perform — that is the
        entire point of the Label page — so this admits every KNOWN role. It is a
        real gate, not a bypass: it re-derives effective_role(), so an admin
        previewing a role is treated as that role, and it 403s an unknown or absent
        one. A no-op when auth is disabled, like the other two."""
        if not auth_on:
            return
        if effective_role(request) not in auth_mod.ROLES:
            raise HTTPException(403, "login required to label")

    def current_user(request: Request) -> dict:
        """The REAL identity behind the session, for stamping label writes.

        Identity is split from gating on purpose (M3 §3.3): the `annotator`
        column must always carry the real account — if it followed an admin's
        act-as preview, their labels would masquerade as another annotator and
        poison agreement — while the gates always use effective_role(), so the
        preview genuinely enforces. `acting_role` is non-None exactly when a
        preview is active, which the writes stamp as `acting_preview` so
        reporting can exclude those rows explicitly. Touches request.session
        only behind auth_on: SessionMiddleware is mounted only when auth is on,
        and tests run with AuthCfg(enabled=False)."""
        if not auth_on:
            return {"username": "local", "role": "admin", "acting_role": None}
        user = request.session.get("user") or {}
        acting = request.session.get("acting_role")
        acting_role = acting if (acting in auth_mod.ROLES and user.get("role") == "admin") else None
        return {"username": user.get("username"), "role": user.get("role"),
                "acting_role": acting_role}

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
    # The label store lives in its OWN DuckDB file (labels must survive the
    # purge/archive the main DB routinely undergoes). Schema-init + seed it here
    # the same way init_db heals the main DB — on its own short-lived connection
    # (labels_db.init_labels_db via ensure_labels_db), never by ATTACHing first:
    # ATTACH on a missing path silently creates an empty database.
    labeling.ensure_labels_db(config)
    # The weekly label-store backup ticker (M3 §6.1) — in-process so its files
    # land owned by uid 10001 by construction and zero cross-process DuckDB lock
    # contention. Started unconditionally: `backup.enabled` is read on every
    # tick, and its 120 s first tick outlives any test run, so the ~20 apps the
    # test suite builds never fire a backup or hold a file handle.
    labels_backup.start_scheduler(config)
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
        # connection ... with a different configuration"). The background localize
        # worker holds a writer while it reassigns detections, so a read_only reader
        # open at the same moment (e.g. the dashboard polling during a save) would
        # make that write connection fail and 500 the request. Everyone shares one mode.
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
        """The logged-in user, or 401. `role` is the EFFECTIVE role (honouring an
        admin's act-as preview); `real_role` is the account's actual role, so the
        SPA knows an admin is previewing and can offer the way back. When auth is
        disabled, reports a synthetic admin so the SPA renders without a login
        gate."""
        if not auth_on:
            return {"username": "local", "role": "admin", "real_role": "admin",
                    "auth_disabled": True}
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "not logged in")
        return {"username": user.get("username"), "role": effective_role(request),
                "real_role": user.get("role"), "auth_disabled": False}

    @app.post("/api/login")
    def login(body: LoginReq, request: Request):
        if not auth_on:
            return {"username": "local", "role": "admin", "real_role": "admin",
                    "auth_disabled": True}
        c = con()
        user = auth_mod.authenticate(c, body.username.strip(), body.password)
        c.close()
        if not user:
            raise HTTPException(401, "invalid username or password")
        request.session["user"] = user
        # A fresh sign-in always starts at the account's real role.
        request.session.pop("acting_role", None)
        print(f"[cownting.alert] LOGIN user={body.username.strip()}", flush=True)
        return {**user, "real_role": user["role"], "auth_disabled": False}

    @app.post("/api/act-as")
    def act_as(body: ActAsReq, request: Request):
        """Admin-only role preview: switch this session's EFFECTIVE role to test
        the app as a plain user or poweruser — the gates then genuinely enforce
        it (hidden controls 403 for real). Gated on the REAL role, not the
        effective one, so an acting admin can always switch back; "admin" clears
        the override. Session-only: the account's stored role never changes."""
        if not auth_on:
            raise HTTPException(400, "auth is disabled — every request already runs as admin")
        user = request.session.get("user")
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "admin only")
        if body.role not in auth_mod.ROLES:
            raise HTTPException(400, f"role must be one of {list(auth_mod.ROLES)}")
        if body.role == "admin":
            request.session.pop("acting_role", None)
        else:
            request.session["acting_role"] = body.role
        print(f"[cownting.alert] ACT-AS user={user.get('username')} role={body.role}", flush=True)
        return me(request)

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
        # Drop any reversible-clip staging for the day too (it isn't part of the
        # archive move), so a deleted day leaves nothing behind.
        for t in ("clipped_detections", "clipped_frames"):
            if db._table_exists(c, t):
                c.execute(f"DELETE FROM {t} WHERE dataset_id = ?", [dataset_id])
        c.close()
        # The day's per-dataset area files live under data/areas/<id>/ (a sibling of
        # artifacts/); drop them too so a re-upload of this id starts area-clean.
        shutil.rmtree(Path(config.paths.artifacts_dir).parent / "areas" / dataset_id, ignore_errors=True)
        return {"ok": True, "dataset_id": dataset_id, "detections_archived": moved}

    # ------------------------------------------------------ per-camera management
    @app.get("/api/dataset/{dataset_id}/camera-health",
             dependencies=[Depends(require_poweruser)])
    def camera_health(dataset_id: str):
        """Per-camera data-quality verdict for one day: brightness, time span,
        detections, and any of 'dark' / 'truncated' / 'no_detections'. ADVISORY —
        the frontend warns and offers delete/replace; nothing here mutates data.
        Poweruser-gated: the whole camera manager (view + delete + replace) is a
        data-management surface, so plain viewers can't reach it. Each camera also
        carries `restorable` — staged frames from a prior clip that Undo can bring
        back (0 when not clipped)."""
        from .quality import camera_health as _camera_health
        health = _camera_health(config, dataset_id)
        c = con()
        try:
            restorable = db.clipped_counts(c, dataset_id)
        finally:
            c.close()
        for h in health:
            h["restorable"] = restorable.get(h["camera_id"], 0)
        return health

    @app.delete("/api/dataset/{dataset_id}/camera/{camera}",
                dependencies=[Depends(require_poweruser)])
    def delete_camera(dataset_id: str, camera: str):
        """Permanently remove ONE camera's stream from a day — its frames,
        detections, on-disk images, and its per-dataset count/panel areas — leaving
        every other camera untouched, then re-localize the day. For dropping a
        malformed stream (e.g. an obscured camera) so a healthy one can replace it.

        Unlike deleting a whole day (which archives), a single bad stream is dropped
        outright — there is nothing worth preserving. 400 on a bad camera name,
        404 if the dataset or that camera isn't present."""
        if not uploads_mod.valid_camera_id(camera):
            raise HTTPException(400, f"invalid camera name {camera!r}")
        # dataset_id becomes a filesystem path segment below (rmtree / area dir);
        # a real dataset row is required, but reject path-escape chars defensively
        # in case an id was ever set (via CLI/config) to something non-slug.
        if not _safe_path_id(dataset_id):
            raise HTTPException(400, f"invalid dataset id {dataset_id!r}")
        c = con()
        try:
            exists = c.execute(
                "SELECT count(*) FROM datasets WHERE dataset_id = ?", [dataset_id]
            ).fetchone()[0]
            if not exists:
                raise HTTPException(404, f"unknown dataset {dataset_id!r}")
            nf = c.execute(
                "SELECT count(*) FROM frames WHERE dataset_id = ? AND camera_id = ?",
                [dataset_id, camera],
            ).fetchone()[0]
            nd = c.execute(
                "SELECT count(*) FROM detections WHERE dataset_id = ? AND camera_id = ?",
                [dataset_id, camera],
            ).fetchone()[0]
            if nf == 0 and nd == 0:
                raise HTTPException(404, f"camera {camera!r} not in dataset {dataset_id!r}")
            db.purge_dataset(c, dataset_id, camera_id=camera)
            remaining = c.execute(
                "SELECT count(DISTINCT camera_id) FROM frames WHERE dataset_id = ?",
                [dataset_id],
            ).fetchone()[0]
        finally:
            c.close()
        # On-disk frames/overlays/pose overlays for this camera.
        ds_art = Path(config.paths.artifacts_dir) / dataset_id
        for sub in ("frames", "overlays", "pose_overlays"):
            shutil.rmtree(ds_art / sub / camera, ignore_errors=True)
        # Drop this camera's polygons from the day's per-dataset area files so a
        # replacement re-upload of the same camera name starts area-clean.
        for kind in ("count", "panel"):
            p = regions.dataset_area_path(config, dataset_id, kind)
            areas = regions.load_count_areas(p)
            if camera in areas:
                areas.pop(camera, None)
                regions.save_count_areas(p, areas)
        # Reassign the day's remaining detections (off-thread) so counts refresh.
        status = localize_worker.request_localize(config, dataset_id)
        return {"ok": True, "dataset_id": dataset_id, "camera": camera,
                "frames_removed": int(nf), "detections_removed": int(nd),
                "remaining_cameras": int(remaining), "localize": status}

    @app.post("/api/dataset/{dataset_id}/camera", dependencies=[Depends(require_poweruser)])
    def add_camera(dataset_id: str,
                   video: UploadFile = File(...),
                   camera: str = Form(...),
                   start: str | None = Form(None)):
        """Add (or replace) ONE camera stream in an existing day, then auto-process
        just that stream (ingest -> segment -> re-localize) in the background.
        Returns 202 + a job id the frontend polls at GET /api/uploads/{job_id}.

        The start time is read from the video itself (container creation_time, else
        the burned-in Brinno bar); when neither yields a time it anchors to the
        day's date at midnight so the new stream lands on the right day. Pass an
        explicit `start` (ISO 8601) to override."""
        cam = camera.strip()
        if not uploads_mod.valid_camera_id(cam):
            raise HTTPException(400, f"invalid camera name {cam!r} (use letters, digits, _ or -)")
        if not _safe_path_id(dataset_id):  # becomes a filesystem path segment below
            raise HTTPException(400, f"invalid dataset id {dataset_id!r}")
        if not uploads_mod.allowed_ext(video.filename or ""):
            raise HTTPException(400, f"unsupported file type: {video.filename!r}")
        c = con()
        try:
            exists = c.execute(
                "SELECT count(*) FROM datasets WHERE dataset_id = ?", [dataset_id]
            ).fetchone()[0]
            day = db.dataset_day(c, dataset_id)
        finally:
            c.close()
        if not exists:
            raise HTTPException(404, f"unknown dataset {dataset_id!r}")

        # Land the clip in the day's inbox under the camera name (a re-add of the
        # same camera overwrites its prior clip, matching the replace semantics).
        inbox = Path(config.paths.artifacts_dir) / "_uploads" / dataset_id
        inbox.mkdir(parents=True, exist_ok=True)
        ext = Path(video.filename or "").suffix.lower()
        dest = inbox / f"{cam}{ext}"
        try:
            with open(dest, "wb") as f:
                shutil.copyfileobj(video.file, f)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

        # Resolve the start time; unlink the just-landed clip on any reject path so a
        # 400/422 doesn't leave an orphan video in the inbox.
        try:
            start_dt = None
            if start and start.strip():
                # Explicit override — trusted as-is (the caller may deliberately
                # place the stream at a specific instant).
                try:
                    start_dt = datetime.fromisoformat(start.strip())
                except ValueError:
                    raise HTTPException(400, "start must be an ISO 8601 timestamp")
            else:
                read = (capture_time.read_container_time(dest)
                        or capture_time.read_burned_timestamp(dest))
                if read is not None and day is not None:
                    # The day is fixed (this dataset already exists). Keep the clip's
                    # time-of-day but PIN its date to the dataset's day, so a camera
                    # whose clock drifted to a different calendar date still lines up
                    # with the other cameras (cross-camera linking is by timestamp)
                    # instead of landing on a day nothing else recorded.
                    start_dt = datetime(day.year, day.month, day.day,
                                        read.hour, read.minute, read.second)
                elif read is not None:
                    start_dt = read
                elif day is not None:
                    start_dt = datetime(day.year, day.month, day.day)  # midnight on the day
            if start_dt is None:
                raise HTTPException(422, detail={
                    "code": "capture_day_required",
                    "message": ("Couldn't read a recording time from the video and the "
                                "day has no date on record — set a start time."),
                })
        except BaseException:
            dest.unlink(missing_ok=True)
            raise

        job = uploads_mod.start_add_camera_job(
            config, dataset_id, cam, str(dest), start_dt.isoformat(), f"add {cam}")
        return JSONResponse(status_code=202, content=uploads_mod.job_dict(job))

    @app.post("/api/dataset/{dataset_id}/camera/{camera}/clip",
              dependencies=[Depends(require_poweruser)])
    def clip_camera(dataset_id: str, camera: str, req: ClipReq):
        """Trim ONE camera's stream to the time window [start, end], then re-localize.
        REVERSIBLE: the out-of-window frames + detections are moved to staging (the
        images are kept), so it can be undone via .../camera/{camera}/restore. For
        lining an over-long camera up with the others (see the dashboard coverage
        strip). 400 on a bad name/dates or an empty window, 404 if the dataset or
        camera isn't present."""
        if not uploads_mod.valid_camera_id(camera):
            raise HTTPException(400, f"invalid camera name {camera!r}")
        if not _safe_path_id(dataset_id):
            raise HTTPException(400, f"invalid dataset id {dataset_id!r}")
        try:
            start = datetime.fromisoformat(req.start)
            end = datetime.fromisoformat(req.end)
        except ValueError:
            raise HTTPException(400, "start and end must be ISO 8601 timestamps")
        if end <= start:
            raise HTTPException(400, "end must be after start")
        c = con()
        try:
            exists = c.execute(
                "SELECT count(*) FROM datasets WHERE dataset_id = ?", [dataset_id]
            ).fetchone()[0]
            if not exists:
                raise HTTPException(404, f"unknown dataset {dataset_id!r}")
            total = c.execute(
                "SELECT count(*) FROM frames WHERE dataset_id = ? AND camera_id = ?",
                [dataset_id, camera],
            ).fetchone()[0]
            if not total:
                raise HTTPException(404, f"camera {camera!r} not in dataset {dataset_id!r}")
            in_window = c.execute(
                "SELECT count(*) FROM frames WHERE dataset_id = ? AND camera_id = ? "
                "AND ts >= ? AND ts <= ?",
                [dataset_id, camera, start.isoformat(), end.isoformat()],
            ).fetchone()[0]
            if in_window == 0:
                raise HTTPException(400, "the window keeps no frames for this camera — widen it")
            result = db.clip_camera(c, dataset_id, camera, start.isoformat(), end.isoformat())
        finally:
            c.close()
        status = localize_worker.request_localize(config, dataset_id)
        return {"ok": True, "dataset_id": dataset_id, "camera": camera,
                "frames_removed": result["removed"], "frames_kept": result["kept"],
                "localize": status}

    @app.post("/api/dataset/{dataset_id}/camera/{camera}/restore",
              dependencies=[Depends(require_poweruser)])
    def restore_camera(dataset_id: str, camera: str):
        """Undo clipping for ONE camera: move its staged (clipped-out) frames +
        detections back into the live tables, restoring its full pre-clip extent, then
        re-localize. A no-op (frames_restored 0) when nothing was clipped. 404 if the
        dataset or camera isn't present."""
        if not uploads_mod.valid_camera_id(camera):
            raise HTTPException(400, f"invalid camera name {camera!r}")
        c = con()
        try:
            exists = c.execute(
                "SELECT count(*) FROM datasets WHERE dataset_id = ?", [dataset_id]
            ).fetchone()[0]
            if not exists:
                raise HTTPException(404, f"unknown dataset {dataset_id!r}")
            restored = db.restore_clip(c, dataset_id, camera)
        finally:
            c.close()
        status = localize_worker.request_localize(config, dataset_id)
        return {"ok": True, "dataset_id": dataset_id, "camera": camera,
                "frames_restored": restored, "localize": status}

    # ------------------------------------------------------------------ data
    @app.get("/api/site")
    def site(dataset: str | None = None):
        c = con()
        try:
            ds = resolve_ds(c, dataset)
            cams = db.cameras(c, ds)
            kpis = db.kpi_summary(c, ds)
            refs = {}
            for cam in cams:
                rf = db.reference_frame(c, cam, ds)
                # A DB row can outlive its JPEG (deleted / half-ingested); without
                # this exists-guard _img_size would 500 the dashboard's primary
                # endpoint (and, pre-try/finally, leak the connection). Mirrors the
                # orthophoto and /api/img/reference guards.
                if rf and Path(rf).exists():
                    w, h = _img_size(rf)
                    q = f"?dataset={ds}" if ds else ""
                    refs[cam] = {"url": f"/api/img/reference/{cam}{q}", "width": w, "height": h}
        finally:
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
    def get_areas(dataset: str | None = None):
        # Areas are per-dataset now: the same camera name is re-framed by each
        # upload, so a camera's polygon only means anything within the package it
        # was drawn on. ds None (pre-migration DB, no packages) -> legacy flat file.
        c = con()
        ds = resolve_ds(c, dataset)
        c.close()
        return regions.load_count_areas(regions.dataset_area_path(config, ds, "count"))

    @app.post("/api/areas", dependencies=[Depends(require_poweruser)])
    def set_areas(req: AreasReq, dataset: str | None = None):
        c = con()
        ds = resolve_ds(c, dataset)
        has_dataset = db.latest_dataset(c) is not None
        c.close()
        # Refuse to write into the flat legacy file while packages exist — such an
        # edit would silently belong to no dataset. Force the caller to name one.
        if ds is None and has_dataset:
            raise HTTPException(400, "dataset required to edit areas")
        regions.save_count_areas(regions.dataset_area_path(config, ds, "count"), req.areas)
        # Localize off the request thread so the save returns instantly; the
        # frontend polls /api/localize/status for the "assigning cows…" spinner.
        return {"ok": True, "localize": localize_worker.request_localize(config, ds)}

    @app.get("/api/panel-areas")
    def get_panel_areas(dataset: str | None = None):
        """Shelter regions (same polygon shape as count areas). A cow inside one
        is 'under a panel'. Per-dataset, exactly like count areas."""
        c = con()
        ds = resolve_ds(c, dataset)
        c.close()
        return regions.load_count_areas(regions.dataset_area_path(config, ds, "panel"))

    @app.post("/api/panel-areas", dependencies=[Depends(require_poweruser)])
    def set_panel_areas(req: AreasReq, dataset: str | None = None):
        c = con()
        ds = resolve_ds(c, dataset)
        has_dataset = db.latest_dataset(c) is not None
        c.close()
        if ds is None and has_dataset:
            raise HTTPException(400, "dataset required to edit areas")
        regions.save_count_areas(regions.dataset_area_path(config, ds, "panel"), req.areas)
        return {"ok": True, "localize": localize_worker.request_localize(config, ds)}

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

    @app.get("/api/camera-coverage")
    def camera_coverage(dataset: str | None = None):
        """Which cameras contribute frames in which time ranges, plus an `uneven`
        flag — feeds the dashboard coverage strip so lopsided per-camera recording
        (e.g. one camera stopping hours early) is visible, not silent. Read-only,
        login-gated like the rest of the dashboard data."""
        c = con()
        out = db.camera_coverage(c, resolve_ds(c, dataset), bin_seconds)
        c.close()
        return out

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

    @app.post("/api/localize", dependencies=[Depends(require_poweruser)])
    def localize(dataset: str | None = None):
        c = con()
        ds = resolve_ds(c, dataset)
        c.close()
        return localize_worker.request_localize(config, ds)

    @app.get("/api/localize/status")
    def localize_status():
        """Background-localize progress for the 'the box is working' spinner."""
        return localize_worker.status()

    @app.post("/api/remask", dependencies=[Depends(require_poweruser)])
    def remask(dataset: str | None = None, camera: str | None = None,
               limit: int | None = None):
        """Backfill segmentation outlines onto already-processed footage.

        Runs IN THIS PROCESS on the upload queue, not as a separate command, and
        that is a hard requirement rather than a convenience: `cownting remask`
        holds a DuckDB write handle for its whole pass, and DuckDB allows one
        read-write process per file — so running the CLI against a live server
        does not slow it down, it takes it off the air until the pass ends. Here
        the handle is already ours.

        Poweruser-gated because it is a long, expensive pass that occupies the
        queue every upload also uses. Returns the job immediately; progress rides
        `GET /api/uploads` like every other job, so there is no second status
        route and no second poll.
        """
        job = uploads_mod.start_remask_job(config, dataset_id=dataset,
                                           camera_id=camera, limit=limit)
        return uploads_mod.job_dict(job)

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

        # Different days queue up freely, but the SAME day twice at once cannot:
        # the rmtree below would delete the videos a running ingest is still
        # reading, and ingest purges that dataset's rows underneath it. Checked
        # here, before anything destructive, and the incoming clips are dropped.
        busy = uploads_mod.active_job_for(iso_day)
        if busy is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(409, detail={
                "code": "day_already_processing",
                "dataset_id": iso_day,
                "message": (f"{busy.label} is already being processed "
                            f"({busy.message}). Wait for it to finish before "
                            "uploading that same day again."),
            })

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

    # ------------------------------------------------------------------ label
    # The in-app annotation tool (docs/roadmap/M3_labeling.md). Handlers stay
    # thin: the store, the key arithmetic and the queue SQL live in labels_db /
    # labeling; this section owns HTTP shapes, gates and boundary validation.
    # Registered BEFORE the static-frontend block below — the SPA catch-all
    # shadows anything added after it.

    def _labels_con():
        # A fresh read-write connection to the LABEL store (data/labels.duckdb),
        # one per request: submit_annotation runs its own explicit transaction
        # and must never nest inside another. Read-write even for readers —
        # DuckDB refuses a second connection to one file with a different mode
        # in one process (the same trap con() documents for the main DB).
        return labeling.labels_connect(config)

    def _valid_anchor(a: InstanceAnchor) -> None:
        """The anchor's ids become provenance and (rebuilt server-side) a stored
        frame_path, and the client can compute a valid hash over ANY strings —
        verify_anchor proves consistency, not cleanliness — so the same
        whitelists the crop endpoint applies run here first."""
        if not uploads_mod.valid_camera_id(a.camera_id):
            raise HTTPException(400, f"invalid camera name {a.camera_id!r}")
        if not _safe_frame_file(a.frame_file):
            raise HTTPException(400, f"invalid frame file {a.frame_file!r}")
        if a.dataset_id is not None and not _safe_path_id(a.dataset_id):
            raise HTTPException(400, f"invalid dataset id {a.dataset_id!r}")

    def _anchor_ts(a: InstanceAnchor) -> datetime | None:
        if not a.ts:
            return None
        try:
            return datetime.fromisoformat(a.ts)
        except ValueError:
            raise HTTPException(400, "anchor.ts must be an ISO 8601 timestamp")

    def _anchor_provenance(a: InstanceAnchor, ts: datetime | None) -> dict:
        """The denormalised columns that keep a label self-describing after its
        detection row is purged/archived/clipped (M3 §3.1). frame_path is
        rebuilt from config + the whitelisted ids — the client never supplies a
        path. det_score is absent on purpose: the key does not hash it, so the
        anchor does not carry it."""
        return {
            "dataset_id": a.dataset_id,
            "camera_id": a.camera_id,
            "frame_path": str(labeling.frame_path_for(config, a.dataset_id,
                                                      a.camera_id, a.frame_file)),
            "frame_basename": a.frame_file,
            "frame_sig": _clip(a.frame_sig, 128),
            "ts": ts,
            "bbox_x1": float(a.bbox[0]), "bbox_y1": float(a.bbox[1]),
            "bbox_x2": float(a.bbox[2]), "bbox_y2": float(a.bbox[3]),
            "ordinal": int(a.ordinal),
        }

    def _label_telemetry(request: Request, user: dict, *, revision: int,
                         session_id: str | None, serve_event_id: int | None,
                         client_elapsed_ms: int | None, input_mode: str | None = None,
                         skip_reason: str | None = None, note: str | None = None) -> dict:
        """Everything a submission reports about the act of answering. The role
        columns record the acting-preview split (§3.3) and `auth_disabled`, so
        reporting can exclude both populations from the headline agreement
        numbers instead of discovering them later."""
        return {
            "skip_reason": skip_reason,
            "flag_note": _clip(note, config.annotation.max_note_chars),
            "session_id": _clip(session_id, 64),
            "serve_event_id": serve_event_id,
            "client_elapsed_ms": client_elapsed_ms,
            "input_mode": input_mode if input_mode in ("key", "mouse") else None,
            "annotator_role": user["acting_role"] or user["role"],
            "annotator_real_role": user["role"],
            "acting_preview": user["acting_role"] is not None,
            "auth_disabled": not auth_on,
            "app_version": __version__,
            "taxonomy_revision": revision,
            "client_info": _clip(request.headers.get("user-agent"), 200),
        }

    def _annotation_version(lc, annotation_id: int) -> int:
        # The one value the response needs that submit_annotation computes but
        # does not return (LabelWriteResult carries `version` for the client's
        # relabel indicator).
        row = lc.execute("SELECT version FROM annotations WHERE annotation_id = ?",
                         [annotation_id]).fetchone()
        return int(row[0]) if row else 1

    @app.get("/api/label/taxonomy")
    def label_taxonomy():
        """The questions, their options, and the current revision — the one the
        client must echo on submit. Archived rows ARE included (`active` False):
        the Label page filters to active client-side (labelKeys does), while the
        poweruser editor needs archived rows on first load or restoring one
        would be impossible before the first mutation."""
        lc = _labels_con()
        try:
            return labels_db.taxonomy(lc, include_archived=True)
        finally:
            lc.close()

    @app.get("/api/label/queue")
    def label_queue(request: Request, limit: int | None = None,
                    exclude: str | None = None, camera: str | None = None,
                    day: str | None = None, dataset: str | None = None,
                    mine: str = "todo", order: str = "fresh"):
        """One batch of instances to label (M3 §4.2). No leasing, no cursor:
        `exclude` is the comma-joined keys already in the client's buffer, and
        the queue is self-consuming, so re-fetching always advances. `dataset`
        deliberately does NOT go through resolve_ds — labeling is cross-day by
        design, and the response echoes the applied scope in `filters` as the
        defence against the frontend's withDs() stamping the selected day on."""
        if day is not None:
            try:
                date.fromisoformat(day)
            except ValueError:
                raise HTTPException(400, "day must be an ISO date 'YYYY-MM-DD'")
        keys = [k for k in (exclude or "").split(",") if k]
        user = current_user(request)
        c = con()
        try:
            return labeling.queue(c, config, annotator=user["username"] or "local",
                                  limit=limit, exclude=keys, camera=camera,
                                  day=day, dataset=dataset, mine=mine, order=order)
        finally:
            c.close()

    @app.get("/api/label/progress")
    def label_progress(request: Request, dataset: str | None = None,
                       camera: str | None = None):
        """Pool + per-annotator effort for the progress panel. The pool numbers
        need the main DB (labeling.progress); effort is the label store's
        SQL_EFFORT_BY_ANNOTATOR — this route merges the two rather than either
        module growing the other's SQL. `auth_disabled` rides along because with
        auth off every row is annotator='local' and agreement is undefined by
        construction; the page says so instead of showing a meaningless kappa."""
        user = current_user(request)
        annotator = user["username"] or "local"
        c = con()
        try:
            pool = labeling.progress(c, config, annotator=annotator,
                                     dataset=dataset, camera=camera)
        finally:
            c.close()
        lc = _labels_con()
        try:
            effort = _dict_rows(lc.execute(labels_db.SQL_EFFORT_BY_ANNOTATOR))
        finally:
            lc.close()
        me = next((r for r in effort if r["annotator"] == annotator), {})
        return {
            # The LabelStats shape types.ts declares...
            "pool_total": pool.get("pool_total", 0),
            "pool_labeled": pool.get("labeled", 0),
            "pool_covered": pool.get("at_target", 0),
            "remaining": pool.get("remaining", 0),
            "my_labeled": int(me.get("labeled") or 0),
            "my_skipped": int(me.get("skipped") or 0),
            "my_median_ms": int(me["median_ms"]) if me.get("median_ms") is not None else None,
            "annotators": len(effort),
            "auth_disabled": not auth_on,
            "filters": {"dataset": dataset, "camera": camera},
            # ...plus the rest of the scan, which costs nothing extra to return.
            "mine_remaining": pool.get("mine_remaining", 0),
            "retired": pool.get("retired", 0),
            "annotations_labeled": pool.get("annotations_labeled", 0),
            "targets_total": pool.get("targets_total", 0),
            "policy": pool.get("policy", {}),
        }

    @app.get("/api/label/mine")
    def label_mine(request: Request, limit: int = 50, before: str | None = None):
        """The caller's own CURRENT answers, newest first, for the review strip.
        Pagination is a `submitted_at` cursor (`before` = the previous page's
        `next_before`), never an offset: the list only ever changes at the head.

        The two SELECTs below are a noted exception to "api.py contains no SQL":
        the M3 ownership table gives this read no owning function, so it reads
        the views labels_db declares (v_current_annotations, annotation_choices)
        rather than inventing an unlisted labels_db export. Move it there when
        the spec grows one."""
        n = max(1, min(int(limit), 200))
        cut = None
        if before:
            try:
                cut = datetime.fromisoformat(before)
            except ValueError:
                raise HTTPException(400, "before must be an ISO 8601 timestamp")
        user = current_user(request)
        lc = _labels_con()
        try:
            where = "WHERE annotator = ?"
            params: list = [user["username"] or "local"]
            if cut is not None:
                where += " AND submitted_at < ?"
                params.append(cut)
            rows = _dict_rows(lc.execute(
                "SELECT annotation_id, instance_key, version, outcome, skip_reason, "
                "submitted_at, dataset_id, camera_id, frame_basename, "
                "bbox_x1, bbox_y1, bbox_x2, bbox_y2 "
                f"FROM v_current_annotations {where} "
                "ORDER BY submitted_at DESC, annotation_id DESC LIMIT ?",
                params + [n],
            ))
            choices: dict[int, list[dict]] = {}
            ids = [r["annotation_id"] for r in rows]
            if ids:
                for ch in _dict_rows(lc.execute(
                    "SELECT annotation_id, group_key, class_key, class_name "
                    "FROM annotation_choices "
                    f"WHERE annotation_id IN ({', '.join('?' * len(ids))}) "
                    "ORDER BY annotation_id, ordinal",
                    ids,
                )):
                    choices.setdefault(int(ch["annotation_id"]), []).append({
                        "group_key": ch["group_key"], "class_key": ch["class_key"],
                        "class_name": ch["class_name"],
                    })
        finally:
            lc.close()
        cfg = config.annotation
        items = []
        for r in rows:
            bbox = [float(r[k] if r[k] is not None else 0.0)
                    for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")]
            items.append({
                "annotation_id": int(r["annotation_id"]),
                "instance_key": r["instance_key"],
                "version": int(r["version"]),
                "outcome": r["outcome"],
                "skip_reason": r["skip_reason"],
                "submitted_at": r["submitted_at"].isoformat()
                                if r["submitted_at"] is not None else None,
                "dataset_id": r["dataset_id"],
                "camera_id": r["camera_id"],
                "frame_file": r["frame_basename"],
                # Server-built, like the queue's: a client-built crop URL would
                # route through withDs() and 404 items from another day.
                "crop_url": labeling.crop_url(
                    camera_id=r["camera_id"] or "", frame_file=r["frame_basename"] or "",
                    bbox=bbox, dataset_id=r["dataset_id"],
                    pad=cfg.crop_pad, max_width=cfg.crop_max_width),
                "choices": choices.get(int(r["annotation_id"]), []),
            })
        next_before = items[-1]["submitted_at"] if len(items) == n and items else None
        return {"items": items, "next_before": next_before}

    @app.get("/api/img/label-crop/{camera}/{frame_file}")
    def img_label_crop(camera: str, frame_file: str, request: Request,
                       x1: float, y1: float, x2: float, y2: float,
                       pad: float | None = None, w: int | None = None,
                       dataset: str | None = None):
        """The padded square crop a queue item points at — ringless (the client
        draws the ring as SVG from the crop-local `ring` the queue supplied) and
        banner-masked server-side (the burned-in Brinno clock IS the sun-exposure
        answer). NOT an extension of /api/img/frame's `kind=`: that endpoint
        silently falls back to the raw frame on an unknown kind, and a typo here
        would show an uncropped, unringed image the annotator would confidently
        mislabel (M3 §4.5).

        Path safety is three whitelists + a resolve-under-artifacts_dir check, a
        deliberate departure from "the path always comes from the DB": a lookup
        here is a full frames scan per image an annotator sees, and the frame
        filename admits a TOTAL whitelist (_safe_frame_file — never loosen it)."""
        if not uploads_mod.valid_camera_id(camera):
            raise HTTPException(400, f"invalid camera name {camera!r}")
        if not _safe_frame_file(frame_file):
            raise HTTPException(400, f"invalid frame file {frame_file!r}")
        if dataset is not None and not _safe_path_id(dataset):
            raise HTTPException(400, f"invalid dataset id {dataset!r}")
        bbox = [x1, y1, x2, y2]
        if not all(math.isfinite(v) for v in bbox):
            raise HTTPException(400, "bbox coordinates must be finite")
        cfg = config.annotation
        pad_v = cfg.crop_pad if pad is None else min(max(float(pad), 0.0), 2.0)
        w_v = cfg.crop_max_width if w is None else max(16, min(int(w), 4096))
        src, out, _ring = labeling.crop_geometry(bbox, pad_v, w_v)
        if src[2] - src[0] > 8192:
            # The square's side is client-controlled through the bbox; without a
            # ceiling the renderer would allocate a gigapixel canvas. No real
            # frame is anywhere near this large.
            raise HTTPException(400, "crop region too large")

        p = labeling.frame_path_for(config, dataset, camera, frame_file)
        root = Path(config.paths.artifacts_dir).resolve()
        candidate = p.resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(400, "invalid path")
        try:
            st = candidate.stat()
        except OSError:
            # Routine, not exceptional: a re-ingest rmtrees the frames out from
            # under a queue the client is still holding.
            raise HTTPException(404, "frame not found")

        # A computed Response gets no validators of its own (unlike every other
        # image route's FileResponse), so paging back and forth would re-decode a
        # full-res JPEG per keystroke: strong ETag + 304 fast path, computed
        # BEFORE any decode. `private` is load-bearing — the image is
        # session-gated and must never be stored by Caddy or a shared proxy. Not
        # `immutable`: a re-ingest rewrites the JPEG at the same path, and
        # mtime_ns here is what invalidates it.
        etag = '"' + hashlib.sha256(repr(
            (str(candidate), st.st_mtime_ns, st.st_size, tuple(bbox), pad_v, out,
             labeling.RENDER_VERSION)
        ).encode()).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=3600"}
        if etag in (request.headers.get("if-none-match") or ""):
            return Response(status_code=304, headers=headers)

        jpeg, sig = labeling.render_crop(candidate, bbox, pad=pad_v,
                                         max_width=w_v, cfg=cfg)
        if not jpeg:
            # Missing/torn JPEG and a mostly-banner crop come back the same way
            # (§5.6's single terminal state): 404, never a 500, never a blank tile.
            raise HTTPException(404, "crop unavailable")
        if sig:
            # The fingerprint from the bytes just read, so the submit body can
            # carry it without a second pass over the file.
            headers["X-Frame-Sig"] = sig
        return Response(content=jpeg, media_type="image/jpeg", headers=headers)

    @app.get("/api/img/label-frame/{camera}/{frame_file}")
    def img_label_frame(camera: str, frame_file: str, request: Request,
                        w: int | None = None, dataset: str | None = None):
        """The WHOLE uncropped frame behind a queue item — what hold-to-peek
        shows instead of zooming the square crop.

        **Banner-masked exactly like the crop, and that is the point of the
        route.** A full frame contains MORE of the burned-in Brinno clock strip
        than any crop of it does, so a plain FileResponse of the JPEG on disk
        would hand the annotator the wall-clock time on every key-hold — and
        time of day IS the "Sun exposure" answer (M3 §4.5). The masking is not
        re-derived here: labeling.render_frame() shares labeling.mask_banner()
        with render_crop(), so the peek and the tile cannot drift apart about
        where the band begins.

        Like /api/img/label-crop this is deliberately NOT an extension of
        /api/img/frame's `kind=`. That endpoint silently falls back to the raw
        frame on an unknown kind, and here the fallback would serve an UNMASKED
        frame — a typo in a query string turning into the exact leak this route
        exists to prevent, with nothing visibly wrong on screen.

        Path safety is label-crop's, unchanged: three whitelists
        (uploads.valid_camera_id, _safe_frame_file, _safe_path_id) plus the
        resolve-under-artifacts_dir check. `_safe_frame_file` is a TOTAL
        whitelist — never loosen it. Auth is the app-wide require_login
        dependency on the FastAPI() instance, same as every other /api/img/*
        route; there is no per-route gate to add or forget.
        """
        if not uploads_mod.valid_camera_id(camera):
            raise HTTPException(400, f"invalid camera name {camera!r}")
        if not _safe_frame_file(frame_file):
            raise HTTPException(400, f"invalid frame file {frame_file!r}")
        if dataset is not None and not _safe_path_id(dataset):
            raise HTTPException(400, f"invalid dataset id {dataset!r}")
        w_v = labeling.FRAME_MAX_WIDTH if w is None else max(16, min(int(w), 4096))

        p = labeling.frame_path_for(config, dataset, camera, frame_file)
        root = Path(config.paths.artifacts_dir).resolve()
        candidate = p.resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(400, "invalid path")
        try:
            st = candidate.stat()
        except OSError:
            # Routine, not exceptional: a re-ingest rmtrees the frames out from
            # under a queue the client is still holding.
            raise HTTPException(404, "frame not found")

        # A computed Response gets no validators of its own, and this one is
        # fetched on a KEY-HOLD: without the 304 fast path every peek would
        # re-decode and re-encode a full-resolution JPEG. Computed BEFORE any
        # decode, off the stat alone. `private` is load-bearing — the image is
        # session-gated and must never be stored by Caddy or a shared proxy. Not
        # `immutable`: a re-ingest rewrites the JPEG at the same path, and
        # mtime_ns here is what invalidates it. The literal tag keeps this ETag
        # from ever colliding with label-crop's over the same file.
        etag = '"' + hashlib.sha256(repr(
            ("label-frame", str(candidate), st.st_mtime_ns, st.st_size, w_v,
             labeling.RENDER_VERSION)
        ).encode()).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, max-age=3600"}
        if etag in (request.headers.get("if-none-match") or ""):
            return Response(status_code=304, headers=headers)

        jpeg, sig = labeling.render_frame(candidate, max_width=w_v, cfg=config.annotation)
        if not jpeg:
            # A missing or torn JPEG comes back as 404, never a 500 and never a
            # blank image the annotator might read as an empty pen.
            raise HTTPException(404, "frame unavailable")
        if sig:
            headers["X-Frame-Sig"] = sig
        return Response(content=jpeg, media_type="image/jpeg", headers=headers)

    @app.post("/api/label/submit", dependencies=[Depends(require_labeler)])
    def label_submit(body: LabelSubmitReq, request: Request):
        """Record an answer. NEVER opens the main DB (M3 §4.3): the client echoes
        the anchor the queue served, verify_anchor re-hashes it in Python, and a
        mismatch is a 400 — no contention with localize_worker on the hottest
        mutation, and no dependency on a detection row that may be purged.

        Validation is against the taxonomy the annotator was SERVED: class keys
        are checked against ever-existing keys (archiving mid-session must not
        400 an answer already on screen), and any revision skew is a 409
        `taxonomy_stale` the client recovers from with one refetch — never a
        permanent 400 loop."""
        a = body.anchor
        _valid_anchor(a)
        if not labeling.verify_anchor(body.instance_key, a):
            raise HTTPException(400, "anchor does not hash to instance_key")
        ts = _anchor_ts(a)
        user = current_user(request)
        lc = _labels_con()
        try:
            current = labels_db.taxonomy_revision(lc)
            if body.taxonomy_revision != current:
                raise HTTPException(409, detail={
                    "code": "taxonomy_stale", "revision": current,
                    "message": "the questions changed while you were answering — "
                               "refresh the taxonomy and resubmit",
                })
            try:
                choices = labels_db.resolve_choices(lc, body.answers)
            except ValueError as e:
                raise HTTPException(400, str(e))
            if not choices:
                raise HTTPException(400, "no answers submitted")
            try:
                annotation_id = labels_db.submit_annotation(
                    lc,
                    instance_key=body.instance_key,
                    annotator=user["username"] or "local",
                    outcome="labeled",
                    choices=choices,
                    provenance=_anchor_provenance(a, ts),
                    telemetry=_label_telemetry(
                        request, user, revision=body.taxonomy_revision,
                        session_id=body.session_id,
                        serve_event_id=body.serve_event_id,
                        client_elapsed_ms=body.client_elapsed_ms,
                        input_mode=body.input_mode, note=body.note),
                )
            except ValueError as e:
                # The lost UNIQUE (instance_key, annotator, version) race — a
                # concurrent submit won; the client re-fetches and resubmits.
                raise HTTPException(409, str(e))
            version = _annotation_version(lc, annotation_id)
        finally:
            lc.close()
        return {"ok": True, "annotation_id": annotation_id, "version": version}

    @app.post("/api/label/skip", dependencies=[Depends(require_labeler)])
    def label_skip(body: LabelSkipReq, request: Request):
        """FLAG an instance that cannot be answered. Stored as an annotation with
        outcome='skipped' — the same provenance and uniqueness rule as an answer,
        counted separately from coverage, and re-served to the next annotator
        until `skip_retire` distinct declines. `multiple_cows` in particular is a
        direct signal the detector merged two animals.

        A REASON IS ENOUGH; the explanation is optional. It was mandatory for one
        release, on the argument that an escape hatch nobody has to justify is
        what silently drains the ambiguous instances out of the corpus — the ones
        inter-rater variability is most informative about. Two things retired
        that: the vocabulary now carries the signal the prose was standing in for
        (`multiple_cows` and `low_resolution` are already machine-countable
        statements about capture quality, which a sentence is not), and once
        instance masks land the defective crop can be inspected directly instead
        of described. The cost is real and worth naming: a crop broken in a way
        the six codes do not cover now arrives as `other` with nothing attached.

        A blank explanation is stored as NULL, never as "", so "no note" and "an
        empty note" cannot be confused downstream. Rows written while it was
        mandatory keep their text — there is no migration either way.

        The outcome, the route path and the stored columns are unchanged — this
        is a validation tightening, not a schema change. No revision check: a
        flag names no classes, so the frozen body carries no revision, and the
        current one is stamped as telemetry."""
        a = body.anchor
        _valid_anchor(a)
        if not labeling.verify_anchor(body.instance_key, a):
            raise HTTPException(400, "anchor does not hash to instance_key")
        if body.reason not in labels_db.SKIP_REASONS:
            raise HTTPException(400, f"reason must be one of {list(labels_db.SKIP_REASONS)}")
        # `explanation` wins, `note` is the frozen wire name (see LabelSkipReq).
        # Whitespace-only is blank: " " would satisfy a truthiness check and turn
        # the requirement back into a button press.
        explanation = ((body.explanation or "").strip()
                       or (body.note or "").strip())
        # An explanation is OPTIONAL. It used to be mandatory (decision 6), on the
        # argument that an escape hatch nobody has to justify gets pulled whenever
        # the work gets hard. That was overridden: the reason codes now carry the
        # signal the prose was there to supply -- `multiple_cows` and
        # `low_resolution` in particular are already machine-countable statements
        # about capture quality -- and once instance masks land the defective crop
        # is inspectable directly rather than needing to be described. A blank one
        # is stored as NULL rather than as an empty string, so "no note" and "an
        # empty note" cannot be told apart downstream by accident.
        ts = _anchor_ts(a)
        user = current_user(request)
        lc = _labels_con()
        try:
            try:
                annotation_id = labels_db.submit_annotation(
                    lc,
                    instance_key=body.instance_key,
                    annotator=user["username"] or "local",
                    outcome="skipped",
                    provenance=_anchor_provenance(a, ts),
                    telemetry=_label_telemetry(
                        request, user,
                        revision=labels_db.taxonomy_revision(lc),
                        session_id=body.session_id,
                        serve_event_id=body.serve_event_id,
                        client_elapsed_ms=body.client_elapsed_ms,
                        skip_reason=body.reason, note=explanation or None),
                )
            except ValueError as e:
                raise HTTPException(409, str(e))
            version = _annotation_version(lc, annotation_id)
        finally:
            lc.close()
        return {"ok": True, "annotation_id": annotation_id, "version": version}

    @app.post("/api/label/mask-fix", dependencies=[Depends(require_labeler)])
    def label_mask_fix(body: LabelMaskFixReq, request: Request):
        """Correct this instance's outline, or declare the detection a false
        positive (M4a §4.2).

        Same anchor discipline as submit/skip: the client echoes what the queue
        served, `verify_anchor` re-hashes it, a mismatch is a 400 — so this route
        also never has to open the main DB to know which cow is meant.

        The polygon arrives in CROP-LOCAL px and is converted here, by
        `labeling.crop_to_frame`, using the SAME pad/max_width the crop endpoint
        renders with. Storing what the client sent, or converting on the client,
        would both make the stored geometry depend on a number the client chose.

        `iou_source` is computed server-side for the same reason: it is the
        statistic that says how wrong the model was, and a client that reports it
        could flatter it. It is null here until masks are persisted (M4 phase 0)
        — there is no model polygon to compare against yet, which is also why the
        `mask_rev` staleness check is a no-op for now rather than a 409 on every
        submit: the honest 409 needs a stored mask to re-hash, and inventing one
        would reject every correction the feature exists to collect."""
        a = body.anchor
        _valid_anchor(a)
        if not labeling.verify_anchor(body.instance_key, a):
            raise HTTPException(400, "anchor does not hash to instance_key")
        if body.kind not in labels_db.MASK_EDIT_KINDS:
            raise HTTPException(
                400, f"kind must be one of {list(labels_db.MASK_EDIT_KINDS)}")
        if body.seeded_from not in labels_db.MASK_SEEDS:
            raise HTTPException(
                400, f"seeded_from must be one of {list(labels_db.MASK_SEEDS)}")
        if body.space not in ("crop", "frame"):
            raise HTTPException(400, "space must be 'crop' or 'frame'")

        cfg = config.annotation
        polygon: list[list[float]] | None = None
        if body.kind == "polygon":
            pts = body.polygon or []
            if len(pts) < labels_db.MASK_MIN_POINTS:
                raise HTTPException(
                    400, f"an outline needs at least {labels_db.MASK_MIN_POINTS} points")
            if len(pts) > labels_db.MASK_MAX_POINTS:
                raise HTTPException(
                    400, f"an outline may not exceed {labels_db.MASK_MAX_POINTS} points")
            if any(len(p) != 2 for p in pts):
                raise HTTPException(400, "every outline point must be [x, y]")
            if any(not math.isfinite(float(v)) for p in pts for v in p):
                raise HTTPException(400, "outline points must be finite")
            if body.space == "frame":
                # Already full-frame: no conversion, so nothing can shear. It is
                # bounded by the WIDEST zoom level the editor can reach — the
                # annotator can only have drawn inside a crop we served, and that
                # is the largest one — rather than by the frame, which would
                # accept an outline nobody could have seen.
                widest = max(labeling.ZOOM_PADS + (cfg.crop_pad,))
                sx0, sy0, sx1, sy1 = labeling.crop_geometry(
                    a.bbox, widest, cfg.crop_max_width)[0]
                if any(not (sx0 - 1 <= float(p[0]) <= sx1 + 1
                            and sy0 - 1 <= float(p[1]) <= sy1 + 1) for p in pts):
                    raise HTTPException(
                        400, "outline points must lie inside the widest crop of this animal")
                polygon = [[float(p[0]), float(p[1])] for p in pts]
            else:
                # Inside the crop the annotator was actually served. A point
                # outside it is either a client bug or a hand-made request;
                # either way the stored polygon would not describe the animal.
                _src, out, _ring = labeling.crop_geometry(
                    a.bbox, cfg.crop_pad, cfg.crop_max_width)
                if any(not (-1.0 <= float(v) <= out + 1.0) for p in pts for v in p):
                    raise HTTPException(400, "outline points must lie inside the crop")
                try:
                    polygon = labeling.crop_to_frame(
                        pts, a.bbox, pad=cfg.crop_pad, max_width=cfg.crop_max_width)
                except ValueError as e:
                    raise HTTPException(400, str(e))
        elif body.polygon:
            raise HTTPException(400, f"a {body.kind!r} verdict carries no polygon")

        ts = _anchor_ts(a)
        user = current_user(request)
        lc = _labels_con()
        try:
            try:
                edit_id = labels_db.submit_mask_edit(
                    lc,
                    instance_key=body.instance_key,
                    annotator=user["username"] or "local",
                    kind=body.kind,
                    polygon=polygon,
                    seeded_from=body.seeded_from,
                    mask_rev=_clip(body.mask_rev, 64),
                    provenance=_anchor_provenance(a, ts),
                    telemetry=_label_telemetry(
                        request, user, revision=labels_db.taxonomy_revision(lc),
                        session_id=body.session_id,
                        serve_event_id=body.serve_event_id,
                        client_elapsed_ms=body.client_elapsed_ms),
                )
            except ValueError as e:
                # Same split as the other two writes: a lost UNIQUE race is a
                # 409 the client retries, a rejected shape is a 400 it must not.
                if "already has version" in str(e):
                    raise HTTPException(409, str(e))
                raise HTTPException(400, str(e))
            row = lc.execute(
                "SELECT version FROM mask_edits WHERE edit_id = ?", [edit_id]
            ).fetchone()
        finally:
            lc.close()
        # Echo the STORED polygon back in both spaces, so the client can patch the
        # item it already holds instead of waiting for the next queue fetch to see
        # its own correction. Re-derived from `polygon` (what was actually
        # written) rather than from the request, and converted here for the reason
        # everything else is: the crop<->frame arithmetic lives in exactly one
        # place. Without this the editor and the hold-Space peek disagree about
        # the same instance for the rest of the session.
        #
        # It also hands back a crop RE-CENTRED on the corrected shape. The crop
        # the annotator was served is cut around the DETECTOR'S box, so after a
        # correction that shrinks or shifts the animal, the questions are asked
        # over a tile with the cow off to one side and dead space around it. The
        # crop is a server render, so re-framing it is the server's job — and
        # doing it here rather than letting the client rebuild the URL keeps
        # crop_geometry in one place, which is the same rule the zoom ladder
        # follows.
        #
        # `bbox` is deliberately NOT part of this. It is hashed into
        # `instance_key`, so the item keeps the box it was served and every
        # later submit still echoes the anchor the queue signed. What changes is
        # only what is DRAWN: the crop, its ring, and the polygon expressed in
        # that new crop's pixels.
        mask_crop = None
        recentred: dict = {}
        if polygon is not None:
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            fixed = [min(xs), min(ys), max(xs), max(ys)]
            _src, out, ring = labeling.crop_geometry(
                fixed, cfg.crop_pad, cfg.crop_max_width)
            # Crop-local against the NEW crop, because that is the image the
            # client is about to draw it over. Converting against the old one
            # here is exactly how the outline would end up offset from the cow.
            mask_crop = [[round(v, 2) for v in p] for p in labeling.frame_to_crop(
                polygon, fixed, pad=cfg.crop_pad, max_width=cfg.crop_max_width)]
            levels, level = labeling.zoom_levels(
                fixed, camera_id=a.camera_id, frame_file=a.frame_file,
                dataset_id=a.dataset_id, pad=cfg.crop_pad,
                max_width=cfg.crop_max_width)
            recentred = {
                "crop_url": labeling.crop_url(
                    camera_id=a.camera_id, frame_file=a.frame_file, bbox=fixed,
                    dataset_id=a.dataset_id, pad=cfg.crop_pad,
                    max_width=cfg.crop_max_width),
                "crop_w": out, "crop_h": out,
                "ring": [round(v, 2) for v in ring],
                "crop_levels": levels, "crop_level": level,
            }
        return {"ok": True, "annotation_id": int(edit_id),
                "version": int(row[0]) if row else 1,
                "mask": mask_crop, "mask_frame": polygon, **recentred}

    @app.post("/api/label/undo", dependencies=[Depends(require_labeler)])
    def label_undo(body: LabelUndoReq, request: Request):
        """Supersede the caller's OWN current answer on one instance. Scoped to
        the real session user inside undo_last, so B can never undo A's row, and
        never keyed by annotation_id — a dense sequence any client could guess.
        Nothing is deleted: the row stays with outcome='undone' (§3.3). A no-op
        (annotation_id null) when there is nothing to undo."""
        user = current_user(request)
        lc = _labels_con()
        try:
            annotation_id = labels_db.undo_last(
                lc, user["username"] or "local", _clip(body.instance_key, 64) or "")
        finally:
            lc.close()
        return {"ok": True, "instance_key": body.instance_key,
                "annotation_id": annotation_id}

    # The event kinds a CLIENT may post: session boundaries + the info-icon
    # signal (+ relabel, the client-side "re-answering after undo" marker).
    # 'served' is minted only by the queue and 'submitted'/'skipped'/'undo' only
    # inside the write transactions — accepting them here would let a scripted
    # client forge the served clock that makes time_on_task_ms non-forgeable,
    # and double-log the rest.
    # 'presented'/'answered' are the per-decision timing pair (M3_labeling_ux.md
    # §6.1). They MUST come from the client: only the browser knows when the crop
    # actually reached the screen and when a key was pressed, and 'served' is
    # written once per BATCH so it cannot separate "how long did Sun exposure
    # take" from "how long did Behaviour take" — which is exactly what the
    # redesign is judged on.
    #
    # Accepting them does not weaken the paragraph above. They are advisory
    # measurements stored beside the server's own timestamps, never a substitute
    # for them: served_at and submitted_at still come from the server, so
    # time_on_task_ms stays non-forgeable and a client that lies about these two
    # only corrupts its own effort statistics, which the server/client delta then
    # exposes. They are read-only telemetry, not a write path.
    _CLIENT_EVENT_KINDS = frozenset({"session_start", "session_end",
                                     "info_opened", "relabel",
                                     "presented", "answered"})

    @app.post("/api/label/events", dependencies=[Depends(require_labeler)])
    def label_event(body: LabelEventReq, request: Request):
        """Effort telemetry from the client. `info_opened` carries the class_key
        whose description was read — the cheapest available signal that a class
        definition is ambiguous, and what makes SQL_INFO_ICON_PRESSURE return
        anything at all."""
        if body.kind not in _CLIENT_EVENT_KINDS:
            raise HTTPException(400, f"kind must be one of {sorted(_CLIENT_EVENT_KINDS)}")
        user = current_user(request)
        lc = _labels_con()
        try:
            labels_db.log_event(
                lc, kind=body.kind, session_id=_clip(body.session_id, 64),
                annotator=user["username"] or "local",
                instance_key=_clip(body.instance_key, 64),
                class_key=_clip(body.class_key, 128),
                detail=_clip(json.dumps(body.detail), 2000)
                if body.detail is not None else None)
        finally:
            lc.close()
        return {"ok": True}

    def _taxonomy_write(request: Request, fn, *args, **kwargs):
        """Shared shell for the six taxonomy mutations: real-actor stamping (the
        audit trail records who, by their account, with the effective role that
        authorised it), the ValueError -> 400 mapping, and the whole-taxonomy
        response the editor replaces its state with (M3 §5.7)."""
        user = current_user(request)
        lc = _labels_con()
        try:
            try:
                return fn(lc, *args,
                          actor=user["username"] or "local",
                          actor_role=user["acting_role"] or user["role"],
                          **kwargs)
            except ValueError as e:
                raise HTTPException(400, str(e))
        finally:
            lc.close()

    @app.post("/api/label/groups", dependencies=[Depends(require_poweruser)])
    def label_create_group(body: LabelGroupReq, request: Request):
        """Add a question."""
        return _taxonomy_write(
            request, labels_db.create_group,
            group_key=_clip(body.group_key, 64) or "",
            name=_clip(body.name, 200) or "",
            description=_clip(body.description, 4000),
            multi_select=body.multi_select, required=body.required)

    @app.patch("/api/label/groups/{group_key}", dependencies=[Depends(require_poweruser)])
    def label_update_group(group_key: str, body: LabelGroupPatchReq, request: Request):
        """Edit a question; `{"active": false}` archives, `{"active": true}`
        restores. There are no DELETE routes anywhere in this feature — a hard
        delete would orphan every stored answer."""
        return _taxonomy_write(
            request, labels_db.update_group, group_key,
            name=_clip(body.name, 200), description=_clip(body.description, 4000),
            multi_select=body.multi_select, required=body.required,
            active=body.active)

    @app.post("/api/label/groups/{group_key}/move", dependencies=[Depends(require_poweruser)])
    def label_move_group(group_key: str, body: LabelMoveReq, request: Request):
        """Reorder a question one slot up/down. sort_order is ALSO the hotkey
        row index, so the editor shows a live key preview beside it."""
        return _taxonomy_write(request, labels_db.move_group, group_key, body.dir)

    @app.post("/api/label/groups/{group_key}/classes", dependencies=[Depends(require_poweruser)])
    def label_create_class(group_key: str, body: LabelClassReq, request: Request):
        """Add an option to a question (description required — enforced in
        labels_db, not only by the editor's disabled button).

        `icon` is validated against labels_db.CLASS_ICONS and a ValueError there
        becomes a 400 through _taxonomy_write. It is checked server-side because
        the editor's picker is not the only way to reach this route and the value
        is rendered into every annotator's DOM."""
        return _taxonomy_write(
            request, labels_db.create_class, group_key,
            name=_clip(body.name, 200) or "",
            description=_clip(body.description, 4000) or "",
            class_key=_clip(body.class_key, 128),
            icon=_clip(body.icon, 32),
            is_escape=body.is_escape)

    @app.patch("/api/label/classes/{class_key}", dependencies=[Depends(require_poweruser)])
    def label_update_class(class_key: str, body: LabelClassPatchReq, request: Request):
        """Edit an option; `active` archives/restores. Safe by construction —
        answers snapshot class_name at label time. `icon` takes a CLASS_ICONS
        name ('dot' is the neutral one); anything else is a 400."""
        return _taxonomy_write(
            request, labels_db.update_class, class_key,
            name=_clip(body.name, 200), description=_clip(body.description, 4000),
            icon=_clip(body.icon, 32),
            is_escape=body.is_escape, active=body.active)

    @app.post("/api/label/classes/{class_key}/move", dependencies=[Depends(require_poweruser)])
    def label_move_class(class_key: str, body: LabelMoveReq, request: Request):
        """Reorder an option within its group."""
        return _taxonomy_write(request, labels_db.move_class, class_key, body.dir)

    @app.get("/api/labels/backup/status", dependencies=[Depends(require_poweruser)])
    def labels_backup_status():
        """Scheduler + recent-runs snapshot for the weekly label-store backup.
        Reports WHETHER a webhook is configured, never the URL; degrades to an
        `error` field rather than 500ing when the store is held elsewhere."""
        return labels_backup.status(config)

    @app.post("/api/labels/backup/run", dependencies=[Depends(require_poweruser)])
    def labels_backup_run(body: LabelBackupRunReq | None = None):
        """Trigger a backup inside the process that already holds the store —
        the contention-free path §6.2 designs for, so an operator without shell
        access never converts a transient lock into a disabled weekly job.
        run_backup never raises; contention comes back status='skipped'."""
        return labels_backup.run_backup(config, trigger="api",
                                        force=bool(body and body.force))

    # ------------------------------------------------------------------ static frontend (prod)
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            # Resolve and confirm the target stays within dist/ before serving, so a
            # "../" in the path can't escape the static root and read arbitrary files.
            root = dist.resolve()
            candidate = (dist / full_path).resolve()
            if full_path and (candidate == root or root in candidate.parents) and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(dist / "index.html"))  # client-side routing fallback

    return app
