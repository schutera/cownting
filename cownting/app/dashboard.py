"""Plotly Dash dashboard — single-page analytics + a calibration tab.

Everything you watch (KPIs, heatmap, segmentation, trends) lives on one page.
Calibration is a second tab that gets nudged only when a camera is uncalibrated.
Calibration images are drawn as go.Image TRACES so clicks return pixel coords;
they're downscaled for the browser and coordinates are rescaled to full frame px.
"""
from __future__ import annotations

import base64
from pathlib import Path

import cv2
import dash_bootstrap_components as dbc
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html, no_update

from .. import db
from ..calib import compute_homography, load_all, save_homography
from ..config import Config

_ASSETS = str(Path(__file__).resolve().parent / "assets")
_COLORWAY = ["#34d399", "#38bdf8", "#f472b6", "#fbbf24", "#a78bfa"]
_CAL_MAX_W = 920  # calibration images are downscaled to this width for the browser


def _data_uri(path: str) -> str:
    ext = Path(path).suffix.lstrip(".").lower() or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    with open(path, "rb") as f:
        return f"data:image/{ext};base64," + base64.b64encode(f.read()).decode()


def _disp_scale(path: str | None) -> float:
    """Display scale = min(1, MAX_W / original_width). Maps full-res px <-> browser px."""
    if not path or not Path(path).exists():
        return 1.0
    from PIL import Image

    with Image.open(path) as im:
        w, _ = im.size
    return min(1.0, _CAL_MAX_W / w)


def _theme(fig: go.Figure, title: str | None = None) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color="#e8eef6", size=13),
        margin=dict(l=12, r=12, t=44 if title else 14, b=12),
        colorway=_COLORWAY,
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.1, x=0),
    )
    fig.update_xaxes(gridcolor="#1b2432", zerolinecolor="#1b2432")
    fig.update_yaxes(gridcolor="#1b2432", zerolinecolor="#1b2432")
    if title:
        fig.update_layout(title=dict(text=title, font=dict(size=14, color="#e8eef6"), x=0.01))
    return fig


def _bg_image_figure(path: str | None, title: str, height: int) -> go.Figure:
    """Full-res background image via layout image (lightweight; for the heatmap)."""
    fig = go.Figure()
    if path and Path(path).exists():
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        fig.add_layout_image(dict(source=_data_uri(path), xref="x", yref="y", x=0, y=0,
                                  sizex=w, sizey=h, sizing="stretch", layer="below", yanchor="top"))
        fig.update_xaxes(visible=False, range=[0, w])
        fig.update_yaxes(visible=False, range=[h, 0], scaleanchor="x")
    fig.update_layout(title=dict(text=title, font=dict(size=13, color="#8ba0b6"), x=0.01),
                      margin=dict(l=0, r=0, t=30, b=0), height=height,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, system-ui, sans-serif"))
    return fig


def _clickable_figure(path: str | None, points: list | None, title: str) -> go.Figure:
    """Downscaled go.Image trace (clicks return display px). `points` are display px."""
    fig = go.Figure()
    if path and Path(path).exists():
        img = cv2.imread(path)
        scale = min(1.0, _CAL_MAX_W / img.shape[1])
        if scale < 1.0:
            img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)))
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        fig.add_trace(go.Image(z=rgb, hoverinfo="none"))
        fig.update_xaxes(visible=False, range=[0, w])
        fig.update_yaxes(visible=False, range=[h, 0], scaleanchor="x")
    if points:
        pts = np.asarray(points, dtype=float)
        fig.add_trace(go.Scatter(
            x=pts[:, 0], y=pts[:, 1], mode="markers+text",
            text=[str(i + 1) for i in range(len(pts))], textposition="top center",
            textfont=dict(color="#eafff5", size=13),
            marker=dict(size=13, color="#34d399", line=dict(width=2, color="#05261a")),
            hoverinfo="none",
        ))
    fig.update_layout(title=dict(text=title, font=dict(size=13, color="#8ba0b6"), x=0.01),
                      margin=dict(l=0, r=0, t=28, b=0), showlegend=False,
                      clickmode="event", dragmode=False, height=440,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, system-ui, sans-serif"))
    return fig


def _kpi(value, label: str, cls: str = "") -> html.Div:
    return html.Div(className=f"kpi {cls}".strip(), children=[
        html.Div(str(value), className="kpi-value"),
        html.Div(label, className="kpi-label"),
    ])


def _select(id_, options, value):
    return dbc.Select(id=id_, options=options, value=value)


def _graph(id_, height=None):
    style = {"height": f"{height}px"} if height else None
    return html.Div(className="chart-card", children=[
        dcc.Graph(id=id_, style=style, config={"displaylogo": False, "displayModeBar": False})
    ])


def build_app(config: Config) -> Dash:
    con = db.connect(config.paths.db_path, read_only=True)
    cams = db.cameras(con)
    kpi = db.kpi_summary(con)
    con.close()
    cam0 = cams[0] if cams else None
    cam_opts = [{"label": c, "value": c} for c in cams]

    app = Dash(__name__, title="Cownting", assets_folder=_ASSETS,
               external_stylesheets=[dbc.themes.DARKLY])

    topbar = html.Div(className="topbar", children=[
        html.Div(className="brand", children=[
            html.Div("🐄", className="brand-logo"),
            html.Div([html.Div("Cownting", className="brand-title"),
                      html.Div("solar-field herd analytics", className="brand-sub")]),
        ]),
        html.Div(className="topbar-meta", children=[
            html.Span(f"{len(cams)} camera" + ("s" if len(cams) != 1 else ""), className="pill"),
            html.Span([html.B(f"{kpi['detections']:,}"), " detections"], className="pill"),
            html.Span([html.B(f"{kpi['valid_frames']:,}"), " valid frames"], className="pill accent"),
        ]),
    ])

    dashboard_page = html.Div(className="tab-body", children=[
        html.Div(className="controls", children=[
            html.Div(className="control-group", children=[
                html.Label("Camera", className="control-label"), _select("ov-cam", cam_opts, cam0)]),
            html.Div(className="control-group", children=[
                html.Label("Granularity", className="control-label"),
                dbc.RadioItems(id="ov-trunc", className="seg", inline=True, value="hour",
                               options=[{"label": "Hourly", "value": "hour"}, {"label": "Daily", "value": "day"}])]),
        ]),
        html.Div(id="calib-nudge", className="nudge-hidden", children=[
            html.Span(id="calib-nudge-text"),
            dbc.Button("Calibrate now", id="go-calib", n_clicks=0, size="sm",
                       className="btn-primary", style={"marginLeft": "6px"}),
        ]),
        html.Div(id="ov-cards", className="kpi-grid"),

        html.Div(className="section-title",
                 children=["Occupancy heatmap", html.Span("where the herd spends time on the field", className="sub")]),
        html.Div(className="heatmap-card panel", children=[
            dcc.Graph(id="heat-fig", style={"height": "600px"},
                      config={"displaylogo": False, "displayModeBar": False})]),

        html.Div("Activity trends", className="section-title"),
        _graph("ov-counts"),
        _graph("ov-posture"),

        html.Div("Segmentation review", className="section-title"),
        html.Div(id="seg-caption", className="caption"),
        dcc.Slider(id="seg-slider", min=0, max=0, step=1, value=0, tooltip={"placement": "bottom"}),
        html.Div(className="panel", children=[html.Img(id="seg-img", className="seg-image")]),
    ])

    calibration_page = html.Div(className="tab-body", children=[
        html.Div(className="controls", children=[
            html.Div(className="control-group", children=[
                html.Label("Camera", className="control-label"), _select("cal-cam", cam_opts, cam0)]),
        ]),
        html.Div(id="cal-saved-info", className="saved-info"),
        html.Div("Click a ground-level landmark on the camera frame, then the matching point on the "
                 "orthophoto. Add ≥4 pairs (panel-post bases, barn corners, tank), then Compute & save.",
                 className="hint", style={"margin": "10px 0"}),
        dbc.Row([
            dbc.Col(html.Div(className="panel", children=[dcc.Graph(
                id="cal-cam-fig", config={"displaylogo": False, "displayModeBar": False})]), md=6),
            dbc.Col(html.Div(className="panel", children=[dcc.Graph(
                id="cal-ortho-fig", config={"displaylogo": False, "displayModeBar": False})]), md=6),
        ]),
        html.Div(style={"marginTop": "14px", "display": "flex", "alignItems": "center", "flexWrap": "wrap"}, children=[
            dbc.Button("Compute & save", id="cal-save", n_clicks=0, className="btn-primary"),
            dbc.Button("Undo last point", id="cal-undo", n_clicks=0, className="btn-ghost", style={"marginLeft": "10px"}),
            dbc.Button("Clear points", id="cal-reset", n_clicks=0, className="btn-ghost", style={"marginLeft": "10px"}),
            html.Span(id="cal-status", className="status-line"),
        ]),
        dcc.Store(id="cal-cam-pts", data=[]),
        dcc.Store(id="cal-ortho-pts", data=[]),
    ])

    app.layout = html.Div(className="app", children=[
        topbar,
        dbc.Container(fluid=True, className="content", children=[
            dbc.Tabs(id="tabs", active_tab="dash", className="cow-tabs", children=[
                dbc.Tab(dashboard_page, label="Dashboard", tab_id="dash"),
                dbc.Tab(calibration_page, label="Calibration", tab_id="calib"),
            ]),
        ]),
    ])

    _register_callbacks(app, config)
    return app


def _register_callbacks(app: Dash, config: Config) -> None:
    db_path = config.paths.db_path
    ortho = config.paths.orthophoto
    calib_path = config.paths.calibration

    def _cam_ref_frame(cam):
        if not cam:
            return None
        con = db.connect(db_path, read_only=True)
        ref = db.reference_frame(con, cam)
        con.close()
        return ref

    # ---------------- Overview / KPIs / trends ----------------
    @app.callback(
        Output("ov-cards", "children"), Output("ov-counts", "figure"), Output("ov-posture", "figure"),
        Input("ov-cam", "value"), Input("ov-trunc", "value"),
    )
    def _overview(cam, trunc):
        con = db.connect(db_path, read_only=True)
        k = db.kpi_summary(con)
        cards = [
            _kpi(k["detections"], "detections"),
            _kpi(k["valid_frames"], "valid frames"),
            _kpi(k["cows_per_frame"], "cows / valid frame", "alt"),
            _kpi(f"{k['pct_lying']}%", "lying"),
            _kpi(f"{k['pct_localized']}%", "localized"),
        ]
        if not cam:
            con.close()
            return cards, _theme(go.Figure()), _theme(go.Figure())
        counts = db.counts_over_time(con, cam, trunc)
        posture = db.posture_over_time(con, cam, trunc)
        con.close()
        f1 = px.area(counts, x="t", y="cows_per_frame", markers=True)
        f1.update_traces(line_color="#34d399", fillcolor="rgba(52,211,153,.15)")
        f2 = px.bar(posture, x="t", y="n", color="posture", barmode="stack")
        return cards, _theme(f1, "Mean cows per valid frame"), _theme(f2, "Posture over time")

    # ---------------- Segmentation ----------------
    @app.callback(Output("seg-slider", "max"), Output("seg-slider", "value"), Input("ov-cam", "value"))
    def _seg_bounds(cam):
        if not cam:
            return 0, 0
        con = db.connect(db_path, read_only=True)
        frames = db.frames_df(con, cam)
        con.close()
        return max(0, len(frames) - 1), 0

    @app.callback(
        Output("seg-img", "src"), Output("seg-caption", "children"),
        Input("ov-cam", "value"), Input("seg-slider", "value"),
    )
    def _seg_image(cam, idx):
        if not cam:
            return no_update, "no camera"
        con = db.connect(db_path, read_only=True)
        frames = db.frames_df(con, cam)
        con.close()
        if frames.empty or idx >= len(frames):
            return no_update, "no processed frames — run `segment` first"
        row = frames.iloc[int(idx)]
        path = row["overlay_path"] or row["frame_path"]
        if not path or not Path(path).exists():
            return no_update, "overlay missing"
        return _data_uri(path), f"{cam} · frame {int(row['frame_idx'])} · {row['ts']}"

    # ---------------- Heatmap ----------------
    @app.callback(Output("heat-fig", "figure"), Input("tabs", "active_tab"), Input("cal-status", "children"))
    def _heatmap(tab, _cal):
        con = db.connect(db_path, read_only=True)
        df = con.execute(
            "SELECT world_x, world_y FROM detections WHERE world_x IS NOT NULL AND world_y IS NOT NULL"
        ).df()
        con.close()
        fig = _bg_image_figure(ortho, "", 600)
        if df.empty:
            fig.update_layout(title=dict(text="No localized detections yet — calibrate, then run `cownting localize`",
                                         font=dict(color="#8ba0b6")))
            if not (ortho and Path(ortho).exists()):
                fig.update_yaxes(autorange="reversed")
            return fig
        fig.add_trace(go.Histogram2dContour(
            x=df["world_x"], y=df["world_y"], colorscale="Turbo", opacity=0.7,
            contours=dict(coloring="heatmap"), showscale=True,
        ))
        if not (ortho and Path(ortho).exists()):
            fig.update_yaxes(autorange="reversed")
        return fig

    # ---------------- Calibration nudge ----------------
    @app.callback(
        Output("calib-nudge", "className"), Output("calib-nudge-text", "children"),
        Input("ov-cam", "value"), Input("cal-status", "children"),
    )
    def _nudge(cam, _status):
        if load_all(calib_path).get(cam or ""):
            return "nudge-hidden", ""
        return "nudge", [f"⚠ {cam or 'This camera'} isn't calibrated — the heatmap stays empty until you set it up. "]

    @app.callback(Output("tabs", "active_tab"), Input("go-calib", "n_clicks"), prevent_initial_call=True)
    def _go_calib(n):
        return "calib"

    # ---------------- Calibration points ----------------
    @app.callback(
        Output("cal-cam-pts", "data"), Output("cal-ortho-pts", "data"),
        Input("cal-cam-fig", "clickData"), Input("cal-ortho-fig", "clickData"),
        Input("cal-reset", "n_clicks"), Input("cal-undo", "n_clicks"), Input("cal-cam", "value"),
        State("cal-cam-pts", "data"), State("cal-ortho-pts", "data"),
        prevent_initial_call=True,
    )
    def _collect(cam_click, ortho_click, reset, undo, cam, cam_pts, ortho_pts):
        from dash import ctx

        trig = ctx.triggered_id
        if trig == "cal-reset":
            return [], []
        if trig == "cal-cam":
            # reload saved (full-res) points -> convert to display px for the downscaled figures
            saved = load_all(calib_path).get(cam or "", {})
            sc, so = _disp_scale(_cam_ref_frame(cam)), _disp_scale(ortho)
            return ([[x * sc, y * sc] for x, y in saved.get("cam_points", [])],
                    [[x * so, y * so] for x, y in saved.get("ortho_points", [])])
        cam_pts = list(cam_pts or [])
        ortho_pts = list(ortho_pts or [])
        if trig == "cal-undo":
            if len(cam_pts) >= len(ortho_pts) and cam_pts:
                cam_pts.pop()
            elif ortho_pts:
                ortho_pts.pop()
        elif trig == "cal-cam-fig" and cam_click:
            p = cam_click["points"][0]
            cam_pts.append([p["x"], p["y"]])
        elif trig == "cal-ortho-fig" and ortho_click:
            p = ortho_click["points"][0]
            ortho_pts.append([p["x"], p["y"]])
        return cam_pts, ortho_pts

    @app.callback(
        Output("cal-cam-fig", "figure"), Output("cal-ortho-fig", "figure"),
        Input("cal-cam", "value"), Input("cal-cam-pts", "data"), Input("cal-ortho-pts", "data"),
    )
    def _calib_figs(cam, cam_pts, ortho_pts):
        cam_pts, ortho_pts = cam_pts or [], ortho_pts or []
        pairs = min(len(cam_pts), len(ortho_pts))
        return (
            _clickable_figure(_cam_ref_frame(cam), cam_pts,
                              f"Camera frame — {len(cam_pts)} pts (click ground-level features)"),
            _clickable_figure(ortho, ortho_pts,
                              f"Orthophoto — {len(ortho_pts)} pts · {pairs} matched pairs"),
        )

    @app.callback(
        Output("cal-saved-info", "children"), Output("cal-saved-info", "className"),
        Input("cal-cam", "value"), Input("cal-status", "children"),
    )
    def _saved_info(cam, _status):
        saved = load_all(calib_path).get(cam or "", {})
        if not saved:
            return f"○ {cam or '—'} is not calibrated yet — add ≥4 matched pairs below.", "saved-info"
        return (f"✓ {cam} calibrated — {saved.get('n_points', '?')} pts, "
                f"reproj {saved.get('reproj_error', float('nan')):.1f} px, saved {saved.get('saved_at', '?')}. "
                f"Points loaded below; recalibrate only if the camera moved.", "saved-info ok")

    @app.callback(
        Output("cal-status", "children"),
        Input("cal-save", "n_clicks"),
        State("cal-cam", "value"), State("cal-cam-pts", "data"), State("cal-ortho-pts", "data"),
        prevent_initial_call=True,
    )
    def _save(n, cam, cam_pts, ortho_pts):
        cam_pts = cam_pts or []
        ortho_pts = ortho_pts or []
        if len(cam_pts) < 4 or len(cam_pts) != len(ortho_pts):
            return f"Need ≥4 matched pairs (have {len(cam_pts)} cam / {len(ortho_pts)} ortho)."
        # convert display px -> full-res px before solving (H must map full-res cam -> full-res ortho)
        sc, so = _disp_scale(_cam_ref_frame(cam)), _disp_scale(ortho)
        cam_full = [[x / sc, y / sc] for x, y in cam_pts]
        ortho_full = [[x / so, y / so] for x, y in ortho_pts]
        try:
            H, err = compute_homography(cam_full, ortho_full)
        except Exception as exc:  # noqa: BLE001
            return f"Failed: {exc}"
        save_homography(calib_path, cam, H, err, ortho, cam_full, ortho_full)
        return f"Saved {cam}: {len(cam_pts)} pairs, reproj {err:.1f} px. Now run `cownting localize`."


def run_dashboard(config: Config, host: str = "127.0.0.1", port: int = 8050, debug: bool = False) -> None:
    app = build_app(config)
    app.run(host=host, port=port, debug=debug)
