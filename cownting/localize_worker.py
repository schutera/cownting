"""Background, coalesced localize.

Saving count/panel areas used to run localize synchronously inside the request:
it blocked the POST and held the DuckDB (read-write) for the whole pass. Fine for
one small day, but it starves other dashboard users as data grows, and drawing N
areas (N saves) meant N full re-localizes.

This runs localize OFF the request thread. `request_localize(config, ds)` marks a
dataset dirty and returns instantly; a single worker thread localizes each dirty
dataset in turn. Because areas are now per-dataset, a save only ever re-localizes
the one edited day — no fan-out. Repeated saves of the same day while it is queued
or running coalesce (a set membership + a short debounce), so a burst of edits
collapses into about one pass plus at most one follow-up.

`status()` exposes a `busy` flag + the current/last dataset so the frontend can
show a "the box is working" spinner and a fresh count when it clears.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Optional

from . import db  # noqa: F401  (kept for symmetry / future status queries)
from .config import Config
from .pipeline import localize as _localize

# Wait this long for a burst of rapid saves (same day) to settle before the pass.
_DEBOUNCE_S = 1.5

_LOCK = threading.Lock()
_config: Optional[Config] = None
_pending: set = set()          # dataset_ids awaiting localize (None = legacy bucket)
_running = False               # a worker thread is alive; guarded by _LOCK
_thread: Optional[threading.Thread] = None
_state = {
    "status": "idle",          # idle | pending | running | done | failed
    "dataset": None,           # dataset_id of the current/last pass
    "updated": 0,              # detections reassigned in the last completed pass
    "error": None,
    "at": None,                # epoch secs of the last completion
}


def _snapshot() -> dict:
    # Caller must hold _LOCK.
    s = dict(_state)
    s["pending"] = sorted(d for d in _pending if d is not None)
    s["busy"] = _running or bool(_pending)
    return s


def status() -> dict:
    with _LOCK:
        return _snapshot()


def request_localize(config: Config, dataset_id: Optional[str]) -> dict:
    """Queue a (re)localize of `dataset_id` and ensure the worker is running.

    Returns immediately with a status snapshot — the localize happens on the
    background thread. Coalesces: queuing a day already pending/running is a no-op
    beyond keeping it dirty."""
    global _config, _running, _thread
    with _LOCK:
        _config = config
        _pending.add(dataset_id)
        if _state["status"] in ("idle", "done", "failed"):
            _state["status"] = "pending"
        if _running:
            return _snapshot()
        _running = True
        try:
            _thread = threading.Thread(target=_worker, name="localize-worker", daemon=True)
            _thread.start()
        except BaseException:
            # Spawn failed (e.g. "can't start new thread" under load). Roll back so
            # a dead worker can't wedge _running=True forever — the queue would then
            # never drain and status() would report busy=True until process exit.
            _running = False
            raise
        return _snapshot()


def _worker() -> None:
    global _running
    while True:
        time.sleep(_DEBOUNCE_S)  # let a burst of saves for the same day coalesce
        with _LOCK:
            if not _pending:
                _running = False
                return
            ds = next(iter(_pending))
            _pending.discard(ds)
            cfg = _config
            _state.update(status="running", dataset=ds, error=None)
        try:
            n = _localize(cfg, dataset_id=ds)
            with _LOCK:
                _state.update(status="done", dataset=ds, updated=int(n), at=time.time())
        except Exception as e:  # noqa: BLE001 — surface to status, keep the worker alive
            with _LOCK:
                _state.update(status="failed", dataset=ds, error=str(e))
            traceback.print_exc()
        # Drain check: if nothing else is queued, clear busy NOW rather than lingering
        # through another full debounce — otherwise a spinner keyed on `busy` stays up
        # ~_DEBOUNCE_S past a pass that already finished. Only loop back to the debounce
        # sleep when a follow-up save is actually waiting to coalesce.
        with _LOCK:
            if not _pending:
                _running = False
                return
