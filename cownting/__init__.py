"""Cownting — offline cow / solar-field analysis pipeline.

Stage 1 (perception): video -> instance segmentation -> facts + KPIs.
Stage 2 (spatial):     orthophoto homography -> world positions -> heatmap.

Identity / pose / shade layers are scaffolded but flag-gated off.
"""

__version__ = "0.1.0"
