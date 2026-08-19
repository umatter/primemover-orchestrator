"""
Experiment Visualizer - Generate matplotlib charts for experiment summaries.

Creates base64-encoded PNG images for embedding in HTML reports.
"""

import base64
import io
from typing import Any

from .models import ExperimentSummary, NumericStats


# Lazy import matplotlib to avoid import errors if not installed
def _import_matplotlib():
    """Import matplotlib with appropriate backend."""
    import matplotlib

    matplotlib.use("Agg")  # Non-GUI backend for server-side rendering
    import matplotlib.pyplot as plt
    import numpy as np

    return plt, np


class ExperimentVisualizer:
    """Generate matplotlib visualizations for experiment summaries."""

    def __init__(self, summary: ExperimentSummary, style: str = "default"):
        """
        Initialize the visualizer.

        Args:
            summary: The experiment summary to visualize
            style: Matplotlib style to use (default, seaborn, etc.)
        """
        self.summary = summary
        self.style = style
        self._setup_style()

    def _setup_style(self):
        """Configure matplotlib style."""
        plt, np = _import_matplotlib()

        # Try to use seaborn style if available
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            try:
                plt.style.use("seaborn-whitegrid")
            except OSError:
                pass  # Use default

        plt.rcParams["figure.figsize"] = (8, 5)
        plt.rcParams["font.size"] = 10
        plt.rcParams["figure.dpi"] = 100

    def generate_all(self) -> dict[str, str]:
        """
        Generate all applicable charts as base64 PNG strings.

        Returns:
            Dictionary mapping chart names to base64-encoded PNG strings
        """
        charts = {}

        # Parameter distribution charts
        if self.summary.statistics:
            stats = self.summary.statistics

            if stats.pi_stats and stats.pi_stats.count > 0:
                charts["pi_distribution"] = self._plot_numeric_distribution(
                    stats.pi_stats, "Pi (π)", "Political Orientation Distribution"
                )

            if stats.alpha_stats and stats.alpha_stats.count > 0:
                charts["alpha_distribution"] = self._plot_numeric_distribution(
                    stats.alpha_stats, "Alpha (α)", "Preference Strength Distribution"
                )

            if stats.kappa_distribution:
                charts["kappa_distribution"] = self._plot_categorical_distribution(
                    stats.kappa_distribution, "Kappa (κ)", "Information Processing Distribution"
                )

            if stats.browser_distribution:
                charts["browser_distribution"] = self._plot_bar_chart(
                    stats.browser_distribution, "Browser Distribution"
                )

            if stats.location_distribution and len(stats.location_distribution) <= 15:
                charts["location_distribution"] = self._plot_bar_chart(
                    stats.location_distribution, "Location Distribution", rotate_labels=True
                )

            if stats.factor_level_counts:
                charts["factor_allocation"] = self._plot_factor_allocation(
                    stats.factor_level_counts
                )

        # Job type distribution
        if self.summary.jobs_by_type:
            charts["job_types"] = self._plot_pie_chart(
                self.summary.jobs_by_type, "Job Type Distribution"
            )

        return charts

    def _plot_numeric_distribution(self, stats: NumericStats, xlabel: str, title: str) -> str:
        """Plot histogram with optional KDE overlay."""
        plt, np = _import_matplotlib()

        fig, ax = plt.subplots(figsize=(8, 5))

        if stats.values:
            # Plot histogram
            n, bins, patches = ax.hist(
                stats.values,
                bins=min(20, len(stats.values) // 2 + 1),
                density=True,
                alpha=0.7,
                color="steelblue",
                edgecolor="white",
            )

            # Add KDE if enough values
            if len(stats.values) > 10:
                try:
                    from scipy.stats import gaussian_kde

                    kde = gaussian_kde(stats.values, bw_method="scott")
                    x_range = np.linspace(stats.min_value, stats.max_value, 100)
                    ax.plot(x_range, kde(x_range), "r-", linewidth=2, label="KDE")
                except ImportError:
                    pass  # scipy not available

            # Add mean line
            ax.axvline(
                stats.mean,
                color="orange",
                linestyle="--",
                linewidth=2,
                label=f"Mean: {stats.mean:.3f}",
            )

            # Add standard deviation band
            if stats.std > 0:
                ax.axvspan(
                    stats.mean - stats.std,
                    stats.mean + stats.std,
                    alpha=0.2,
                    color="orange",
                    label=f"±1 SD: {stats.std:.3f}",
                )

        ax.set_xlabel(xlabel)
        ax.set_ylabel("Density")
        ax.set_title(title)
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _plot_categorical_distribution(self, data: dict[Any, int], xlabel: str, title: str) -> str:
        """Plot bar chart for categorical/discrete data."""
        plt, np = _import_matplotlib()

        fig, ax = plt.subplots(figsize=(8, 5))

        labels = [str(k) for k in sorted(data.keys())]
        values = [data[k] for k in sorted(data.keys())]
        total = sum(values)

        colors = plt.cm.Blues(np.linspace(0.4, 0.8, len(labels)))

        bars = ax.bar(labels, values, color=colors, edgecolor="white")

        # Add percentage labels
        for bar, value in zip(bars, values, strict=False):
            height = bar.get_height()
            pct = (value / total * 100) if total > 0 else 0
            ax.annotate(
                f"{pct:.1f}%",
                xy=(bar.get_x() + bar.get_width() / 2, height),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
            )

        ax.set_xlabel(xlabel)
        ax.set_ylabel("Count")
        ax.set_title(title)
        ax.grid(True, alpha=0.3, axis="y")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _plot_pie_chart(self, data: dict[str, int], title: str) -> str:
        """Plot pie chart for categorical data."""
        plt, np = _import_matplotlib()

        fig, ax = plt.subplots(figsize=(8, 6))

        # Sort by value descending
        sorted_items = sorted(data.items(), key=lambda x: -x[1])
        labels = [item[0] for item in sorted_items]
        values = [item[1] for item in sorted_items]

        # Use a nice color palette
        colors = plt.cm.Set3(np.linspace(0, 1, len(labels)))

        wedges, texts, autotexts = ax.pie(
            values, labels=labels, autopct="%1.1f%%", colors=colors, pctdistance=0.8, startangle=90
        )

        # Make percentage text more readable
        for autotext in autotexts:
            autotext.set_fontsize(9)

        ax.set_title(title)

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _plot_bar_chart(self, data: dict[str, int], title: str, rotate_labels: bool = False) -> str:
        """Plot bar chart for distribution data."""
        plt, np = _import_matplotlib()

        fig, ax = plt.subplots(figsize=(8, 5))

        # Sort by value descending
        sorted_items = sorted(data.items(), key=lambda x: -x[1])
        labels = [item[0] for item in sorted_items]
        values = [item[1] for item in sorted_items]

        colors = plt.cm.Blues(np.linspace(0.4, 0.8, len(labels)))

        ax.bar(range(len(labels)), values, color=colors, edgecolor="white")

        ax.set_xticks(range(len(labels)))
        if rotate_labels:
            ax.set_xticklabels(labels, rotation=45, ha="right")
        else:
            ax.set_xticklabels(labels)

        ax.set_ylabel("Count")
        ax.set_title(title)
        ax.grid(True, alpha=0.3, axis="y")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    def _plot_factor_allocation(self, factor_level_counts: dict[str, dict[str, int]]) -> str:
        """Plot factor level allocation as grouped bar chart."""
        plt, np = _import_matplotlib()

        fig, ax = plt.subplots(figsize=(10, 6))

        # Prepare data
        factor_names = list(factor_level_counts.keys())
        num_factors = len(factor_names)

        if num_factors == 0:
            ax.text(0.5, 0.5, "No factor data", ha="center", va="center")
            return self._fig_to_base64(fig)

        # Calculate positions
        x_positions = []
        x_labels = []
        heights = []
        colors = []

        pos = 0
        color_idx = 0
        color_map = plt.cm.Set2(np.linspace(0, 1, num_factors))

        for factor_name in factor_names:
            levels = factor_level_counts[factor_name]
            for level, count in sorted(levels.items()):
                x_positions.append(pos)
                x_labels.append(f"{level}")
                heights.append(count)
                colors.append(color_map[color_idx])
                pos += 1
            pos += 0.5  # Gap between factors
            color_idx += 1

        ax.bar(x_positions, heights, color=colors, edgecolor="white")

        ax.set_xticks(x_positions)
        ax.set_xticklabels(x_labels, rotation=45, ha="right")
        ax.set_ylabel("Crawler Count")
        ax.set_title("Factor Level Allocation")
        ax.grid(True, alpha=0.3, axis="y")

        # Add legend for factors
        import matplotlib.patches as mpatches

        legend_patches = [
            mpatches.Patch(color=color_map[i], label=name) for i, name in enumerate(factor_names)
        ]
        ax.legend(handles=legend_patches, loc="upper right")

        plt.tight_layout()
        return self._fig_to_base64(fig)

    @staticmethod
    def _fig_to_base64(fig) -> str:
        """Convert matplotlib figure to base64 PNG string."""
        plt, _ = _import_matplotlib()

        buffer = io.BytesIO()
        fig.savefig(
            buffer, format="png", dpi=150, bbox_inches="tight", facecolor="white", edgecolor="none"
        )
        buffer.seek(0)
        b64 = base64.b64encode(buffer.read()).decode("utf-8")
        plt.close(fig)
        return f"data:image/png;base64,{b64}"


def create_comparison_charts(
    summary_a: ExperimentSummary, summary_b: ExperimentSummary
) -> dict[str, str]:
    """
    Create comparison charts for two experiments.

    Returns overlaid distributions and difference charts.
    """
    plt, np = _import_matplotlib()

    charts = {}

    # Compare pi distributions if available
    stats_a = summary_a.statistics
    stats_b = summary_b.statistics

    if stats_a and stats_b:
        if stats_a.pi_stats and stats_b.pi_stats:
            charts["pi_comparison"] = _plot_distribution_comparison(
                stats_a.pi_stats.values,
                stats_b.pi_stats.values,
                "Pi (π)",
                "Political Orientation Comparison",
                summary_a.name,
                summary_b.name,
            )

    return charts


def _plot_distribution_comparison(
    values_a: list[float],
    values_b: list[float],
    xlabel: str,
    title: str,
    label_a: str,
    label_b: str,
) -> str:
    """Plot overlaid histograms for comparison."""
    plt, np = _import_matplotlib()

    fig, ax = plt.subplots(figsize=(10, 6))

    # Determine common bin edges
    all_values = values_a + values_b
    bins = np.linspace(min(all_values), max(all_values), 20)

    ax.hist(
        values_a,
        bins=bins,
        density=True,
        alpha=0.5,
        color="steelblue",
        label=f"{label_a[:20]}..." if len(label_a) > 20 else label_a,
    )
    ax.hist(
        values_b,
        bins=bins,
        density=True,
        alpha=0.5,
        color="coral",
        label=f"{label_b[:20]}..." if len(label_b) > 20 else label_b,
    )

    # Add mean lines
    mean_a = np.mean(values_a)
    mean_b = np.mean(values_b)
    ax.axvline(
        mean_a, color="steelblue", linestyle="--", linewidth=2, label=f"Mean A: {mean_a:.3f}"
    )
    ax.axvline(mean_b, color="coral", linestyle="--", linewidth=2, label=f"Mean B: {mean_b:.3f}")

    ax.set_xlabel(xlabel)
    ax.set_ylabel("Density")
    ax.set_title(title)
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(
        buffer, format="png", dpi=150, bbox_inches="tight", facecolor="white", edgecolor="none"
    )
    buffer.seek(0)
    b64 = base64.b64encode(buffer.read()).decode("utf-8")
    plt.close(fig)
    return f"data:image/png;base64,{b64}"
