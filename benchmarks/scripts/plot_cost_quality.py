#!/usr/bin/env python3
"""Render a cost-vs-quality scatter (with a Pareto frontier) from run artifacts.

Reads the scored benchmark runs under ``swe-benchmark-data/`` and plots one point
per model: mean cost per task on the x-axis, mean task score (the same 0-100
scores shown in the README leaderboard) on the y-axis. Non-dominated models --
those where no other model is both cheaper and higher-scoring -- are connected by
a highlighted frontier line, so the cost/quality trade-off is read at a glance.

Each model's numbers come straight from its per-task ``metrics.json``
(``total_cost_usd``) and ``eval.json`` (``task_score``); a task that produced no
score (a model failure) counts as 0, matching the leaderboard's 5-task mean.

Note: for self-hosted vLLM runs ``total_cost_usd`` is a token-based *estimate*
(the served model has no per-token bill), so the x-axis is labelled as estimated.

Usage:
    uv run scripts/plot_cost_quality.py
    uv run scripts/plot_cost_quality.py --repo mcp-gateway-registry --dark
    uv run scripts/plot_cost_quality.py --data-dir ../swe-benchmark-data --out chart.png
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: render to file, never a display
import matplotlib.pyplot as plt  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent
_BENCHMARKS_DIR = _SCRIPTS_DIR.parent
_REPO_ROOT = _BENCHMARKS_DIR.parent
DEFAULT_DATA_DIR = _BENCHMARKS_DIR / "swe-benchmark-data"
# Default to the tracked docs/images path so the committed chart the README
# embeds stays in sync when this is re-run. (swe-benchmark-data is gitignored.)
DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "images" / "cost-quality.png"
METRICS_FILENAME = "metrics.json"
EVAL_FILENAME = "eval.json"

# Palette (from the dataviz skill's validated reference instance). Marks are a
# recessive dark neutral; the frontier is the warm accent. Text wears ink tokens.
_THEME = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "muted": "#52514e",
        "grid": "#e6e5e2",
        "dot": "#33322f",
        "accent": "#eb6834",
        "label_bg": "#ffffff",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "muted": "#c3c2b7",
        "grid": "#333330",
        "dot": "#d7d6cf",
        "accent": "#d95926",
        "label_bg": "#26262410",
    },
}


@dataclass
class ModelPoint:
    """One model's aggregate for the scatter."""

    model: str
    mean_cost: float
    mean_score: float
    n_tasks: int
    n_scored: int


def _read_json(path: Path) -> dict | None:
    """Return the parsed JSON object at ``path``, or None if absent/invalid."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _task_score(eval_data: dict | None) -> float | None:
    """Extract ``task_score`` from an eval.json object, or None when missing."""
    if not eval_data:
        return None
    score = eval_data.get("task_score")
    return float(score) if isinstance(score, (int, float)) else None


def _aggregate_model(model_repo_dir: Path) -> ModelPoint | None:
    """Aggregate one model's per-task cost and score under a repo directory.

    A task folder must have ``metrics.json`` (for cost) to count. A missing or
    unscored ``eval.json`` counts as a 0 score -- a model failure still consumed
    cost and still counts against the mean, matching the README leaderboard.

    Args:
        model_repo_dir: ``<data-dir>/<model>/<repo>`` directory.

    Returns:
        The model's aggregate, or None if it has no scorable task folders.
    """
    model = model_repo_dir.parent.name
    costs: list[float] = []
    scores: list[float] = []
    n_scored = 0
    for task_dir in sorted(p for p in model_repo_dir.iterdir() if p.is_dir()):
        metrics = _read_json(task_dir / METRICS_FILENAME)
        if metrics is None:
            continue
        cost = metrics.get("total_cost_usd")
        costs.append(float(cost) if isinstance(cost, (int, float)) else 0.0)
        score = _task_score(_read_json(task_dir / EVAL_FILENAME))
        if score is not None:
            n_scored += 1
        scores.append(score if score is not None else 0.0)
    if not scores:
        return None
    return ModelPoint(
        model=model,
        mean_cost=sum(costs) / len(costs),
        mean_score=sum(scores) / len(scores),
        n_tasks=len(scores),
        n_scored=n_scored,
    )


def _collect_points(data_dir: Path, repo: str) -> list[ModelPoint]:
    """Collect one ModelPoint per model that has runs for ``repo``.

    Args:
        data_dir: The ``swe-benchmark-data`` root.
        repo: The dataset repo subfolder to aggregate (e.g. mcp-gateway-registry).

    Returns:
        Model aggregates sorted by descending mean score.

    Raises:
        SystemExit: If no model has scorable runs for the repo.
    """
    points: list[ModelPoint] = []
    for model_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        repo_dir = model_dir / repo
        if not repo_dir.is_dir():
            continue
        point = _aggregate_model(repo_dir)
        if point is None:
            continue
        # A model that never produced a scored task (e.g. one that could not be
        # served at a usable context window on this node) is "not viable", not a
        # $0 / 0% data point -- excluding it keeps it off the frontier. Log the
        # skip so the omission is explicit, never silent.
        if point.n_scored == 0:
            logger.warning(
                "  excluding %s: no scored tasks (not a viable run to plot)",
                point.model,
            )
            continue
        points.append(point)
    if not points:
        raise SystemExit(
            f"no scorable runs found under {data_dir} for repo '{repo}'. "
            "Run the benchmark and judge first."
        )
    return sorted(points, key=lambda p: p.mean_score, reverse=True)


def _pareto_frontier(points: list[ModelPoint]) -> list[ModelPoint]:
    """Return the non-dominated points: cheapest-and-best trade-off curve.

    A point dominates another when it is both no more expensive and no
    lower-scoring, and strictly better on at least one axis. The frontier is the
    set of points nothing dominates, ordered by ascending cost for drawing.

    Args:
        points: All model aggregates.

    Returns:
        The frontier points, ordered by ascending mean cost.
    """
    frontier: list[ModelPoint] = []
    for candidate in points:
        dominated = any(
            other is not candidate
            and other.mean_cost <= candidate.mean_cost
            and other.mean_score >= candidate.mean_score
            and (
                other.mean_cost < candidate.mean_cost
                or other.mean_score > candidate.mean_score
            )
            for other in points
        )
        if not dominated:
            frontier.append(candidate)
    return sorted(frontier, key=lambda p: p.mean_cost)


def _label(point: ModelPoint) -> str:
    """Build a point label, flagging partial runs (a model failure)."""
    if point.n_scored < point.n_tasks:
        return f"{point.model} ({point.n_scored}/{point.n_tasks} scored)"
    return point.model


def _plot(
    points: list[ModelPoint],
    frontier: list[ModelPoint],
    *,
    mode: str,
    title: str,
    cost_label: str,
    output: Path,
) -> None:
    """Render the scatter with its frontier and save to ``output``.

    Args:
        points: All model aggregates.
        frontier: The non-dominated subset (ascending cost).
        mode: "light" or "dark" theme.
        title: Chart title.
        cost_label: X-axis label (cost provenance is caller's responsibility).
        output: Destination image path.
    """
    theme = _THEME[mode]
    fig, ax = plt.subplots(figsize=(11, 7), dpi=150)
    fig.patch.set_facecolor(theme["surface"])
    ax.set_facecolor(theme["surface"])

    # Frontier: a recessive accent line under the marks, filled to the baseline.
    if len(frontier) >= 2:
        fx = [p.mean_cost for p in frontier]
        fy = [p.mean_score for p in frontier]
        ax.plot(
            fx,
            fy,
            color=theme["accent"],
            linewidth=2,
            linestyle="--",
            marker="o",
            markersize=9,
            zorder=2,
            label="Cost/quality frontier",
        )
        ax.fill_between(
            fx,
            fy,
            min(p.mean_score for p in points) - 5,
            color=theme["accent"],
            alpha=0.06,
            zorder=1,
        )

    # All models as dark neutral dots; frontier points already drawn in accent.
    frontier_ids = {id(p) for p in frontier}
    for point in points:
        on_frontier = id(point) in frontier_ids
        ax.scatter(
            point.mean_cost,
            point.mean_score,
            s=90,
            color=theme["accent"] if on_frontier else theme["dot"],
            edgecolors=theme["surface"],
            linewidths=1.5,
            zorder=3,
        )
        ax.annotate(
            _label(point),
            (point.mean_cost, point.mean_score),
            textcoords="offset points",
            xytext=(10, 6),
            fontsize=9,
            color=theme["ink"],
            zorder=4,
        )

    ax.set_xlabel(cost_label, fontsize=11, color=theme["ink"], labelpad=10)
    ax.set_ylabel(
        "Mean task score (0-100)", fontsize=11, color=theme["ink"], labelpad=10
    )
    ax.set_title(title, fontsize=13, color=theme["ink"], pad=16, loc="left")

    ax.grid(True, color=theme["grid"], linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(theme["grid"])
    ax.tick_params(colors=theme["muted"], labelsize=9)

    # Headroom so labels near the axis edges do not clip.
    xs = [p.mean_cost for p in points]
    ys = [p.mean_score for p in points]
    xpad = max((max(xs) - min(xs)) * 0.12, 1.0)
    ypad = max((max(ys) - min(ys)) * 0.12, 3.0)
    ax.set_xlim(max(0.0, min(xs) - xpad), max(xs) + xpad * 2.2)
    ax.set_ylim(max(0.0, min(ys) - ypad), min(100.0, max(ys) + ypad))

    if len(frontier) >= 2:
        legend = ax.legend(loc="lower right", frameon=False, fontsize=9)
        for text in legend.get_texts():
            text.set_color(theme["muted"])

    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, facecolor=theme["surface"], bbox_inches="tight")
    plt.close(fig)
    logger.info(
        "wrote %s (%d models, %d on frontier)", output, len(points), len(frontier)
    )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Plot a cost-vs-quality scatter with a Pareto frontier from "
        "benchmark run artifacts.",
        epilog="Example:\n"
        "  uv run scripts/plot_cost_quality.py --repo mcp-gateway-registry\n"
        "  uv run scripts/plot_cost_quality.py --dark --out chart-dark.png",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"swe-benchmark-data root (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--repo",
        default="mcp-gateway-registry",
        help="Dataset repo subfolder to aggregate (default: mcp-gateway-registry)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Output image path (default: {DEFAULT_OUTPUT}, or -dark suffix in dark mode)",
    )
    parser.add_argument(
        "--dark", action="store_true", help="Render the dark-mode theme"
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Override the chart title",
    )
    parser.add_argument(
        "--cost-label",
        default="Estimated cost per task (mean $, token-based for self-hosted)",
        help="X-axis label; make cost provenance explicit",
    )
    return parser.parse_args()


def main() -> None:
    """Aggregate the artifacts and render the cost-quality chart."""
    args = _parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    if not data_dir.is_dir():
        raise SystemExit(f"data dir not found: {data_dir}")

    mode = "dark" if args.dark else "light"
    output = args.out or (
        DEFAULT_OUTPUT.with_name("cost-quality-dark.png")
        if args.dark
        else DEFAULT_OUTPUT
    )
    title = args.title or f"Cost vs. quality -- {args.repo}"

    points = _collect_points(data_dir, args.repo)
    for point in points:
        logger.info(
            "  %-32s score=%.2f cost=$%.2f (%d/%d scored)",
            point.model,
            point.mean_score,
            point.mean_cost,
            point.n_scored,
            point.n_tasks,
        )
    frontier = _pareto_frontier(points)
    _plot(
        points,
        frontier,
        mode=mode,
        title=title,
        cost_label=args.cost_label,
        output=output,
    )


if __name__ == "__main__":
    main()
