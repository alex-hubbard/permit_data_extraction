"""Build the three manuscript figures into docs/docs/figures/.

* Figure 1 - pipeline overview (drawn with matplotlib; no data input).
* Figure 2 - ECHO coverage choropleth (delegates to make_echo_coverage_map.build_map).
* Figure 3 - representativeness against ASM / CBP / NAICS benchmarks, three panels.

Figure 3 reads the benchmark tables written by scripts/benchmark_coverage_vs_echo.py:
    data/processed/analysis/asm_representativeness_state.csv
    data/processed/analysis/cbp_representativeness_county.csv
    data/processed/analysis/coverage_by_naics.csv

Usage::

    python scripts/make_manuscript_figures.py            # all three
    python scripts/make_manuscript_figures.py --only 3   # one figure
    python scripts/make_manuscript_figures.py --out-dir some/dir
"""
from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from make_echo_coverage_map import build_map, load_coverage  # noqa: E402

ANALYSIS = ROOT / "data/processed/analysis"
DEFAULT_OUT = ROOT / "docs/docs/figures"

# ---- palette (one blue hue for magnitude; gray for de-emphasis) -------------
SURFACE = "#ffffff"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE = "#2a78d6"                       # slot 1 / step 450
BLUE_100, BLUE_150, BLUE_600 = "#cde2fb", "#b7d3f6", "#184f95"
ORDINAL_4 = ["#86b6ef", "#5598e7", "#256abf", "#104281"]   # steps 250/350/500/650

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 7, "axes.titlesize": 7.5, "axes.labelsize": 7,
    "xtick.labelsize": 6.5, "ytick.labelsize": 6.5, "legend.fontsize": 6.5,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.6,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.labelcolor": INK2,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5, "xtick.major.width": 0.6,
    "ytick.major.width": 0.6, "text.color": INK,
    "savefig.facecolor": SURFACE, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "pdf.fonttype": 42, "ps.fonttype": 42,
})


def _save(fig: plt.Figure, out_dir: Path, stem: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        p = out_dir / f"{stem}.{ext}"
        fig.savefig(p, dpi=300, bbox_inches="tight", pad_inches=0.04)
        print(f"wrote {p}")
    plt.close(fig)


def _style_axes(ax, grid_axis: str = "y") -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.5, zorder=0)
    ax.set_axisbelow(True)


def _panel_letter(ax, letter: str, x: float = -0.18) -> None:
    ax.text(x, 1.16, letter, transform=ax.transAxes, fontsize=9,
            fontweight="bold", va="bottom", ha="left", color=INK)


# =============================================================================
# Figure 1 - pipeline overview
# =============================================================================
STAGES = [
    ("Public permit sources", None,
     "EPA permit hub · about 40 state and local agency portals (static HTML pages to "
     "JavaScript applications) · state document indexes queried per facility for "
     "data-center permits"),
    ("1  Acquisition", "Section 2.1",
     "Agentic crawler (Selenium page rendering, optional LLM scoring of ambiguous links, "
     "direct HTTP fetch with browser-driven fallback) · 55 state-specific download "
     "scripts · EPA permit hub downloader"),
    ("2  Text extraction", "Section 2.2",
     "Native parse with PyPDF2, kept when it averages at least 30 characters per page · "
     "otherwise OCR fallback: pages rendered at 200 DPI with Poppler and read with "
     "Tesseract, five pages at a time"),
    ("3  LLM extraction", "Section 2.3",
     "One structured-output call per document against an enforced JSON schema · long "
     "documents split into chunks of at most 80,000 characters, cut-off chunks halved and "
     "retried, chunk results merged additively · model recorded on every record"),
    ("4  Post-processing", "Consolidation · Section 2.4",
     "Parse operating and run hours · normalize fuel types, ZIP codes and dates · infer "
     "capacity from unit descriptions · facility-level deduplication · industry code "
     "resolution with provenance (Section 5.2) · automated QC (Section 4.3)"),
]
ARTIFACTS = [  # sits on the arrow leaving stage i (index into STAGES)
    None,
    "PDF corpus · more than 118,000 permit documents",
    "One text file per PDF · filename is the stable key joining PDF, text and records",
    "One JSON object per document · general permit fields + array of emission units",
    "Release table · 1,621,542 records from 74,017 documents · 52 fields · CSV and Excel",
]


def figure1(out_dir: Path) -> None:
    W, H = 7.2, 4.9
    fig = plt.figure(figsize=(W, H))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    x0, x1 = 0.15, W - 0.15           # box extent
    title_w = 1.85                    # title column width inside the box
    box_h, src_h, gap, pill_h = 0.58, 0.48, 0.38, 0.26
    y = H - 0.12                      # running top edge

    def box(top, h, fill, edge, title, sub, body, title_color=INK, body_color=INK):
        ax.add_patch(FancyBboxPatch((x0, top - h), x1 - x0, h,
                                    boxstyle="round,pad=0,rounding_size=0.06",
                                    fc=fill, ec=edge, lw=0.7, zorder=2))
        ax.text(x0 + 0.16, top - h / 2 + (0.07 if sub else 0), title, fontsize=8,
                fontweight="bold", va="center", ha="left", color=title_color, zorder=3)
        if sub:
            ax.text(x0 + 0.16, top - h / 2 - 0.12, sub, fontsize=6.5, va="center",
                    ha="left", color=INK2, zorder=3)
        wrapped = "\n".join(textwrap.wrap(body, 96))
        ax.text(x0 + title_w + 0.05, top - h / 2, wrapped, fontsize=6.8, va="center",
                ha="left", color=body_color, linespacing=1.35, zorder=3)
        return top - h

    def arrow(y_from, y_to, label=None, final=False):
        xm = (x0 + x1) / 2
        if final:  # the release pill is the terminus: stop the arrow at its top edge
            y_to = (y_from + y_to) / 2 + pill_h / 2
        ax.add_patch(FancyArrowPatch((xm, y_from), (xm, y_to), arrowstyle="-|>",
                                     mutation_scale=9, lw=0.9, color=MUTED, zorder=1,
                                     shrinkA=0, shrinkB=0))
        if label:
            ym = (y_from + y_to) / 2 if not final else y_to - pill_h / 2
            pw = 0.058 * len(label) + 0.3
            ax.add_patch(FancyBboxPatch((xm - pw / 2, ym - pill_h / 2), pw, pill_h,
                                        boxstyle="round,pad=0,rounding_size=0.13",
                                        fc=BLUE_600 if final else SURFACE,
                                        ec=BLUE_600 if final else AXIS, lw=0.7, zorder=4))
            ax.text(xm, ym, label, fontsize=6.8, va="center", ha="center", zorder=5,
                    color=SURFACE if final else INK2,
                    fontweight="bold" if final else "normal")

    # sources box
    t, sub, body = STAGES[0]
    y = box(y, src_h, SURFACE, AXIS, t, sub, body)
    for i, (t, sub, body) in enumerate(STAGES[1:], start=1):
        arrow(y, y - gap, ARTIFACTS[i - 1] if i > 1 else None)
        y -= gap
        y = box(y, box_h, BLUE_100, BLUE_150, t, sub, body)
    # final artifact: the release table
    arrow(y, y - gap - 0.10, ARTIFACTS[-1], final=True)

    _save(fig, out_dir, "fig1_pipeline_overview")


# =============================================================================
# Figure 2 - ECHO coverage map
# =============================================================================
def figure2(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    fig = build_map(load_coverage(), title=False)
    for ext in ("png", "pdf"):
        p = out_dir / f"fig2_echo_coverage_map.{ext}"
        fig.write_image(str(p), scale=2 if ext == "png" else 1)
        print(f"wrote {p}")


# =============================================================================
# Figure 3 - representativeness against economic benchmarks
# =============================================================================
SHORT_SECTOR = {
    "Mining/Oil & Gas": "Mining, oil & gas", "Admin/Waste Mgmt": "Admin. & waste",
    "Wholesale Trade": "Wholesale trade", "Public Administration": "Public admin.",
}
LABEL_STATES = ["CA", "TX", "OH", "IN", "LA", "NJ", "KS", "NE", "OR", "AZ", "DE"]
LABEL_OFFSETS = {  # (dx, dy) in points; tuned by eye for the current data
    "CA": (5, -5), "TX": (5, 4), "OH": (3, -21), "IN": (5, 2), "LA": (-5, 3),
    "NJ": (5, -2), "KS": (5, -3), "NE": (5, 3), "OR": (5, -2),
    "AZ": (5, -3), "DE": (-5, 0),
}
LEADER_STATES = {"OH"}   # labels moved into open space, joined by a hairline


def _county_quartiles(cbp: pd.DataFrame) -> pd.DataFrame:
    """Same quartile rule as benchmark_coverage_vs_echo.py (rank-based qcut)."""
    has = cbp.dropna(subset=["cbp_mfg_est"]).copy()
    has["q"] = pd.qcut(has["cbp_mfg_est"].rank(method="first"), 4,
                       labels=["Q1", "Q2", "Q3", "Q4"])
    g = has.groupby("q", observed=True).agg(
        echo=("echo_active_major_facilities", "sum"),
        matched=("matched_facilities", "sum"), n=("fips", "size"))
    g["coverage"] = g["matched"] / g["echo"] * 100
    rho = has["coverage"].corr(has["cbp_mfg_est"], method="spearman")
    return g, rho


def figure3(out_dir: Path) -> None:
    asm = pd.read_csv(ANALYSIS / "asm_representativeness_state.csv").dropna(subset=["asm_valadd"])
    cbp = pd.read_csv(ANALYSIS / "cbp_representativeness_county.csv", dtype={"fips": str})
    nai = pd.read_csv(ANALYSIS / "coverage_by_naics.csv")

    fig, axes = plt.subplots(1, 3, figsize=(7.5, 2.75),
                             gridspec_kw=dict(width_ratios=[1.3, 1.0, 1.15], wspace=0.95))

    # ---- (a) state coverage vs manufacturing value added ---------------------
    ax = axes[0]
    x = asm["asm_valadd"] / 1e6          # ASM VALADD is in $1,000 -> billions
    yv = asm["coverage"] * 100
    rho = asm["coverage"].corr(asm["asm_valadd"], method="spearman")
    for lvl in (30, 70):
        ax.axhline(lvl, color=AXIS, lw=0.5, zorder=0)
    ax.scatter(x, yv, s=16, color=BLUE, edgecolor=SURFACE, linewidth=0.6, zorder=3)
    for st in LABEL_STATES:
        r = asm[asm.state == st]
        if r.empty:
            continue
        dx, dy = LABEL_OFFSETS[st]
        ax.annotate(st, (r["asm_valadd"].iloc[0] / 1e6, r["coverage"].iloc[0] * 100),
                    xytext=(dx, dy), textcoords="offset points", fontsize=6,
                    color=INK2, ha="left" if dx > 0 else "right", va="center",
                    arrowprops=(dict(arrowstyle="-", lw=0.5, color=MUTED,
                                     shrinkA=0, shrinkB=2.5)
                                if st in LEADER_STATES else None))
    ax.set_xscale("log")
    ax.set_xlim(1.5, 800)
    ax.set_xticks([2, 10, 50, 250]); ax.set_xticklabels(["$2B", "$10B", "$50B", "$250B"])
    ax.set_ylim(-8, 103); ax.set_yticks([0, 30, 70, 100])
    ax.set_yticklabels(["0%", "30%", "70%", "100%"])
    ax.set_xlabel("Manufacturing value added, 2021 (ASM)")
    ax.set_ylabel("Share of ECHO major facilities matched")
    for lvl, lab in ((15, "sparse"), (50, "partial"), (85, "well covered")):
        ax.text(1.02, lvl / 100, lab, transform=ax.transAxes, fontsize=5.8,
                color=MUTED, va="center", ha="left", rotation=90)
    ax.set_title(f"Spearman ρ = {rho:+.2f} · {len(asm) - 1} states + DC", loc="right",
                 fontsize=6.3, color=INK2, pad=6)
    _style_axes(ax, "y")
    _panel_letter(ax, "a", x=-0.30)

    # ---- (b) coverage by county manufacturing-establishment quartile ---------
    ax = axes[1]
    g, rho_c = _county_quartiles(cbp)
    xs = np.arange(4)
    ax.bar(xs, g["coverage"], width=0.56, color=ORDINAL_4, zorder=3)
    for i, v in enumerate(g["coverage"]):
        ax.text(i, v + 2, f"{v:.1f}", ha="center", va="bottom", fontsize=6, color=INK2)
    ax.set_xticks(xs)
    ax.set_xticklabels(["Q1\nfewest", "Q2", "Q3", "Q4\nmost"])
    ax.set_ylim(0, 100); ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("County quartile by manufacturing\nestablishments, 2022 (CBP)")
    ax.set_title(f"Spearman ρ = {rho_c:+.2f} · {int(g['n'].sum()):,} counties", loc="right",
                 fontsize=6.3, color=INK2, pad=6)
    _style_axes(ax, "y")
    _panel_letter(ax, "b", x=-0.30)

    # ---- (c) representation ratio by two-digit NAICS sector ------------------
    ax = axes[2]
    sec = nai.dropna(subset=["naics2"]).copy()
    sec["naics2"] = sec["naics2"].astype(int).astype(str)
    sec = sec[(sec["sector"] != "Unknown/blank") & (sec["share_of_echo_universe"] >= 0.01)]
    sec = sec.sort_values("share_of_echo_universe", ascending=True)
    big = sec["share_of_echo_universe"] >= 0.05
    labels = [f"{SHORT_SECTOR.get(s, s)} ({n})" for s, n in zip(sec["sector"], sec["naics2"])]
    ys = np.arange(len(sec))
    colors = [BLUE if b else MUTED for b in big]
    ax.barh(ys, sec["representation_ratio"], height=0.56, color=colors, zorder=3)
    ax.axvline(1.0, color=INK2, lw=0.6, zorder=4)
    for yi, (v, b) in enumerate(zip(sec["representation_ratio"], big)):
        if b:
            ax.text(v + 0.03, yi, f"{v:.2f}", va="center", ha="left", fontsize=6.2, color=INK2)
    ax.set_yticks(ys); ax.set_yticklabels(labels)
    ax.set_xlim(0, 1.5); ax.set_xticks([0, 0.5, 1.0, 1.5])
    ax.set_xlabel("Representation ratio\n(matched share ÷ ECHO share)")
    ax.tick_params(axis="y", length=0)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=BLUE, label="Sector ≥ 5% of ECHO universe"),
                       Patch(color=MUTED, label="Sector 1–5% of ECHO universe")],
              loc="lower left", bbox_to_anchor=(0.0, 1.0), frameon=False,
              handlelength=1.0, handleheight=0.8, borderaxespad=0.0, labelspacing=0.3)
    _style_axes(ax, "x")
    _panel_letter(ax, "c", x=-0.62)

    _save(fig, out_dir, "fig3_representativeness")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build manuscript figures.")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--only", type=int, choices=[1, 2, 3], help="build one figure only")
    args = ap.parse_args()
    builders = {1: figure1, 2: figure2, 3: figure3}
    for k in ([args.only] if args.only else [1, 2, 3]):
        builders[k](args.out_dir)


if __name__ == "__main__":
    main()
