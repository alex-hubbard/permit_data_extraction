"""Choropleth of ECHO active-major coverage by state (manuscript Figure 2).

Reads ``data/processed/analysis/coverage_vs_echo_majors.csv`` and draws the
share of EPA ECHO active major facilities matched by at least one dataset
facility, per state. Labels are read from the coverage table so the map
cannot drift from the data.

Usage::

    python scripts/make_echo_coverage_map.py                 # reports/figures/echo_coverage_map.png (titled)
    python scripts/make_echo_coverage_map.py --out X.png --no-title --pdf   # manuscript-style
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_CSV = ROOT / "data/processed/analysis/coverage_vs_echo_majors.csv"
DEFAULT_OUT = ROOT / "reports/figures/echo_coverage_map.png"

INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
# one-hue sequential ramp, light -> dark
SCALE = [[0.0, "#F2F6FD"], [0.25, "#B7D3F6"], [0.5, "#6DA7EC"],
         [0.75, "#2A78D6"], [1.0, "#0D366B"]]
# coverage tiers used in Section 3.4 of the manuscript
TIERS = [("Sparse", "<30%", 0, 30), ("Partial", "30–70%", 30, 70),
         ("Well covered", "≥70%", 70, 100)]

# callouts: big states + the zero-coverage holes (label text filled from data)
CALLOUTS = {
    "LA": (30.9, -92.3), "TX": (31.2, -99.3), "CA": (37.2, -119.5),
    "IN": (39.9, -86.3), "OH": (40.2, -82.8), "PA": (40.9, -77.7),
    "KS": (38.5, -98.4), "NE": (41.5, -99.7),
}


def load_coverage(path: Path = COVERAGE_CSV) -> pd.DataFrame:
    cov = pd.read_csv(path)
    cov["pct"] = cov["coverage"] * 100
    return cov


def build_map(cov: pd.DataFrame, title: bool = True) -> go.Figure:
    national_m = int(cov["matched_facilities"].sum())
    national_t = int(cov["echo_active_major_facilities"].sum())

    fig = go.Figure(go.Choropleth(
        locations=cov["state"], locationmode="USA-states", z=cov["pct"],
        zmin=0, zmax=100, colorscale=SCALE,
        marker_line_color="white", marker_line_width=0.8,
        colorbar=dict(
            title=dict(text="Coverage<br> ", font=dict(size=13, color=INK2)),
            tickmode="array", tickvals=[0, 30, 70, 100],
            ticktext=["0%", "30%", "70%", "100%"], ticklen=8,
            tickcolor=MUTED, thickness=14, len=0.75, y=0.5, x=1.0,
            tickfont=dict(size=12, color=INK2), outlinewidth=0),
        customdata=cov[["matched_facilities", "echo_active_major_facilities"]],
        hovertemplate="%{location}: %{z:.0f}%% (%{customdata[0]}/%{customdata[1]})<extra></extra>",
    ))

    for st_, (lat, lon) in CALLOUTS.items():
        pct = cov.loc[cov.state == st_, "pct"].iloc[0]
        label = f"{st_} {pct:.0f}%" if pct == 0 else f"{pct:.0f}%"
        fig.add_trace(go.Scattergeo(
            lat=[lat], lon=[lon], mode="text", text=[label],
            textfont=dict(size=13, color="white" if pct > 55 else INK,
                          family="Arial"),
            showlegend=False, hoverinfo="skip"))

    # tier labels beside the colour bar (bar spans paper y 0.125-0.875)
    y0, span = 0.125, 0.75
    for name, rng, lo, hi in TIERS:
        fig.add_annotation(
            xref="paper", yref="paper", x=1.045, y=y0 + span * (lo + hi) / 200,
            text=f"<b>{name}</b><br>{rng}", showarrow=False, align="left",
            xanchor="left", font=dict(size=12, color=INK2))

    fig.update_geos(scope="usa", bgcolor="white", lakecolor="white",
                    showlakes=True, subunitcolor="white")
    layout = dict(
        width=1640, height=840, margin=dict(l=10, r=240, t=88 if title else 20, b=40),
        paper_bgcolor="white", font=dict(family="Arial"),
    )
    if title:
        layout["title"] = dict(
            text=(f"<b>Share of EPA ECHO active major facilities in our database, by state</b>"
                  f"<br><sup>National: {national_m:,} of {national_t:,} facilities matched "
                  f"({national_m/national_t:.0%}) · white = no usable portal (KS, NE, DE, DC)"
                  f" · PR (2%) not shown</sup>"),
            font=dict(size=26, color=INK), x=0.02, xanchor="left")
    else:
        fig.add_annotation(
            xref="paper", yref="paper", x=0.0, y=-0.02, xanchor="left", yanchor="top",
            showarrow=False, font=dict(size=12, color=MUTED), align="left",
            text=(f"National: {national_m:,} of {national_t:,} ECHO active major facilities "
                  f"matched ({national_m/national_t:.1%}). White = no matched facilities "
                  f"(KS, NE, DE, DC). Puerto Rico (1 of 44) not shown."))
    fig.update_layout(**layout)
    return fig


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output PNG path")
    ap.add_argument("--no-title", action="store_true", help="omit the in-figure title (manuscript style)")
    ap.add_argument("--pdf", action="store_true", help="also write a PDF next to the PNG")
    args = ap.parse_args()

    fig = build_map(load_coverage(), title=not args.no_title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(str(args.out), scale=2)
    print(f"map written -> {args.out}")
    if args.pdf:
        pdf = args.out.with_suffix(".pdf")
        fig.write_image(str(pdf))
        print(f"map written -> {pdf}")


if __name__ == "__main__":
    main()
