"""Cownting command-line interface (Typer).

Typical flow:
    cownting ingest                  # video -> sampled timestamped frames
    cownting segment                 # frames -> instance segmentation + overlays
    cownting dashboard               # open the dashboard; use the Calibration tab
    cownting localize                # project detections through the saved homography
    cownting kpis                    # print a quick summary
    cownting spotcheck manual.csv    # predicted-vs-manual count error

Stage 1b (fine-tune / re-label loop):
    cownting label-select            # pick a diverse frame subset to correct
    cownting label-export            # bootstrap masks -> CVAT task
    (correct masks in CVAT)
    cownting dataset-build           # corrections -> YOLO-seg dataset
    cownting train                   # fine-tune YOLO11-seg
    cownting eval-detect             # mask/box mAP on the val split
"""
from __future__ import annotations

import typer

from .config import Config

app = typer.Typer(add_completion=False, help="Offline cow / solar-field analysis pipeline.")

CONFIG_OPT = typer.Option("config/cownting.yaml", "--config", "-c", help="Path to the YAML config.")


def _load(config_path: str) -> Config:
    return Config.load(config_path)


@app.command("init-db")
def init_db(config: str = CONFIG_OPT):
    """Create the DuckDB schema."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path)
    db.init_db(con)
    con.close()
    typer.echo(f"Initialized {cfg.paths.db_path}")


@app.command()
def ingest(config: str = CONFIG_OPT):
    """Decode each camera's video into timestamped, fps-subsampled frames."""
    from .pipeline import ingest as run

    run(_load(config))


@app.command()
def segment(config: str = CONFIG_OPT, limit: int = typer.Option(None, help="Only process N frames.")):
    """Run instance segmentation over unprocessed frames."""
    from .pipeline import segment as run

    run(_load(config), limit=limit)


@app.command()
def localize(config: str = CONFIG_OPT):
    """Project detections to world coordinates using the saved calibration."""
    from .pipeline import localize as run

    run(_load(config))


@app.command()
def kpis(config: str = CONFIG_OPT):
    """Print a quick KPI summary."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path, read_only=True)
    summary = db.kpi_summary(con)
    con.close()
    for k, v in summary.items():
        typer.echo(f"{k:>16}: {v}")


@app.command()
def spotcheck(manual_csv: str, config: str = CONFIG_OPT):
    """Predicted-vs-manual count error from a frame_path,manual_count CSV."""
    from .eval import count_error

    result = count_error(_load(config), manual_csv)
    for k, v in result.items():
        typer.echo(f"{k:>14}: {v}")


@app.command("label-select")
def label_select(config: str = CONFIG_OPT):
    """Stage 1b: pick a diverse frame subset to hand-correct (writes selected.txt)."""
    from .finetune import select_frames

    select_frames(_load(config))


@app.command("label-export")
def label_export(
    config: str = CONFIG_OPT,
    launch: bool = typer.Option(True, help="Open the CVAT editor after pushing."),
):
    """Stage 1b: bootstrap masks (Grounded-SAM2) and push a CVAT annotation task."""
    from .finetune import export_to_cvat

    export_to_cvat(_load(config), launch=launch)


@app.command("dataset-build")
def dataset_build(config: str = CONFIG_OPT):
    """Stage 1b: pull CVAT corrections into a YOLO-seg dataset (images/labels/data.yaml)."""
    from .finetune import build_dataset

    build_dataset(_load(config))


@app.command()
def train(config: str = CONFIG_OPT):
    """Stage 1b: fine-tune YOLO11-seg on the corrected masks."""
    from .finetune import train as run

    run(_load(config))


@app.command("eval-detect")
def eval_detect(
    config: str = CONFIG_OPT,
    weights: str = typer.Option(None, help="Weights to eval; defaults to finetune.weights_out."),
):
    """Stage 1b: mask/box mAP for the fine-tuned weights on the val split."""
    from .finetune import evaluate

    evaluate(_load(config), weights=weights)


@app.command()
def serve(config: str = CONFIG_OPT, host: str = "127.0.0.1", port: int = 8000):
    """Run the FastAPI backend + serve the built React frontend (frontend/dist)."""
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(_load(config)), host=host, port=port)


@app.command()
def dashboard(
    config: str = CONFIG_OPT,
    host: str = "127.0.0.1",
    port: int = 8050,
    debug: bool = False,
):
    """Launch the dashboard (Overview / Segmentation / Heatmap / Calibration)."""
    from .app import run_dashboard

    run_dashboard(_load(config), host=host, port=port, debug=debug)


if __name__ == "__main__":
    app()
