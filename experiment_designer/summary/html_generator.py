"""
HTML Report Generator - Generate self-contained HTML reports.

Uses Jinja2 templates with embedded CSS and base64-encoded charts.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .models import ExperimentSummary, ExperimentComparison


# Inline template for when Jinja2 is not available
FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Experiment Summary: {name}</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 1200px; margin: 0 auto; padding: 2rem; background: #f8fafc; }}
        .header {{ background: #2563eb; color: white; padding: 2rem; border-radius: 8px; margin-bottom: 2rem; }}
        .card {{ background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 1.5rem; margin-bottom: 1.5rem; }}
        h2 {{ color: #1e293b; border-bottom: 2px solid #2563eb; padding-bottom: 0.5rem; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #e2e8f0; }}
        .badge {{ display: inline-block; padding: 0.25rem 0.75rem; border-radius: 999px; font-size: 0.875rem; background: #2563eb; color: white; margin: 0.25rem; }}
        .chart img {{ max-width: 100%; height: auto; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 1rem; }}
    </style>
</head>
<body>
    <header class="header">
        <h1>{name}</h1>
        <p>{description}</p>
        <div>
            <span class="badge">{crawler_count} Crawlers</span>
            <span class="badge">{total_jobs} Jobs</span>
            <span class="badge">Source: {source}</span>
        </div>
    </header>
    <section class="card">
        <h2>Summary</h2>
        <pre>{text_summary}</pre>
    </section>
    {charts_html}
    <footer style="text-align: center; color: #64748b; margin-top: 2rem;">
        Generated: {timestamp}
    </footer>
</body>
</html>"""


REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Experiment Summary: {{ summary.name }}</title>
    <style>
        :root {
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
            --success: #22c55e;
            --warning: #f59e0b;
            --error: #ef4444;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }

        .header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            padding: 2rem;
            border-radius: 12px;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }

        .header h1 {
            font-size: 1.875rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
        }

        .header p {
            opacity: 0.9;
            max-width: 800px;
        }

        .badges {
            margin-top: 1rem;
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }

        .badge {
            display: inline-block;
            padding: 0.375rem 0.75rem;
            border-radius: 999px;
            font-size: 0.875rem;
            font-weight: 500;
            background: rgba(255, 255, 255, 0.2);
        }

        .card {
            background: var(--card-bg);
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }

        h2 {
            color: var(--text);
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid var(--primary);
        }

        h3 {
            color: var(--text);
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 1.5rem;
        }

        .grid-2 {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1rem;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }

        th, td {
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }

        th {
            font-weight: 600;
            color: var(--text-muted);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        tr:hover {
            background: var(--bg);
        }

        .chart {
            text-align: center;
        }

        .chart img {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
        }

        .chart h3 {
            margin-bottom: 1rem;
            color: var(--text-muted);
        }

        .stat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 1rem;
        }

        .stat-box {
            background: var(--bg);
            padding: 1rem;
            border-radius: 8px;
            text-align: center;
        }

        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            color: var(--primary);
        }

        .stat-label {
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .tag {
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 500;
            background: var(--bg);
            color: var(--text-muted);
            margin-right: 0.25rem;
        }

        .tag-factor {
            background: #dbeafe;
            color: #1d4ed8;
        }

        .tag-param {
            background: #dcfce7;
            color: #166534;
        }

        footer {
            text-align: center;
            color: var(--text-muted);
            margin-top: 2rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border);
            font-size: 0.875rem;
        }

        .validation-error {
            background: #fef2f2;
            color: #dc2626;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            margin-bottom: 0.5rem;
        }

        .validation-warning {
            background: #fffbeb;
            color: #d97706;
            padding: 0.5rem 1rem;
            border-radius: 8px;
            margin-bottom: 0.5rem;
        }

        @media (max-width: 768px) {
            body { padding: 1rem; }
            .header { padding: 1.5rem; }
            .grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <header class="header">
        <h1>{{ summary.name }}</h1>
        {% if summary.description %}
        <p>{{ summary.description }}</p>
        {% endif %}
        <div class="badges">
            <span class="badge">{{ summary.crawler_count }} Crawlers</span>
            <span class="badge">{{ summary.total_jobs }} Jobs</span>
            <span class="badge">{{ summary.total_queues }} Queues</span>
            <span class="badge">Source: {{ summary.source.value }}</span>
            {% if summary.experiment_id %}
            <span class="badge">ID: {{ summary.experiment_id }}</span>
            {% endif %}
        </div>
    </header>

    <!-- Summary Stats -->
    <section class="card">
        <h2>Overview</h2>
        <div class="stat-grid">
            <div class="stat-box">
                <div class="stat-value">{{ summary.crawler_count }}</div>
                <div class="stat-label">Crawlers</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary.total_jobs }}</div>
                <div class="stat-label">Total Jobs</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary.total_queues }}</div>
                <div class="stat-label">Queues</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary._format_duration(summary.estimated_duration_seconds) }}</div>
                <div class="stat-label">Est. Duration</div>
            </div>
        </div>
    </section>

    <!-- Design Information -->
    <section class="card">
        <h2>Design Overview</h2>
        <div class="grid-2">
            <div>
                <table>
                    <tr><td><strong>Design Type</strong></td><td>{{ summary.design_type or 'Static' }}</td></tr>
                    {% if summary.has_design %}
                    <tr><td><strong>Formula</strong></td><td>{{ summary.design_formula or 'N/A' }}</td></tr>
                    <tr><td><strong>Replications</strong></td><td>{{ summary.design_replications }}</td></tr>
                    {% if summary.design_seed is not none %}
                    <tr><td><strong>Seed</strong></td><td>{{ summary.design_seed }}</td></tr>
                    {% endif %}
                    {% endif %}
                </table>
            </div>
            <div>
                <table>
                    <tr><td><strong>Templates</strong></td><td>{{ summary.template_names|join(', ') or 'None' }}</td></tr>
                    <tr><td><strong>Data Pools</strong></td><td>{{ summary.data_pool_names|join(', ') or 'None' }}</td></tr>
                    <tr><td><strong>Version</strong></td><td>{{ summary.version or 'N/A' }}</td></tr>
                </table>
            </div>
        </div>

        {% if summary.design_factors %}
        <h3 style="margin-top: 1.5rem;">Factors</h3>
        <table>
            <thead>
                <tr>
                    <th>Factor</th>
                    <th>Levels</th>
                    <th>Count</th>
                    <th>Maps To</th>
                </tr>
            </thead>
            <tbody>
                {% for factor in summary.design_factors %}
                <tr>
                    <td><span class="tag tag-factor">{{ factor.name }}</span></td>
                    <td>{{ factor.levels|join(', ') }}</td>
                    <td>{{ factor.level_count }}</td>
                    <td>{{ factor.maps_to|join(', ') or '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% endif %}

        {% if summary.design_parameters %}
        <h3 style="margin-top: 1.5rem;">Parameters</h3>
        <table>
            <thead>
                <tr>
                    <th>Parameter</th>
                    <th>Distribution</th>
                    <th>Configuration</th>
                </tr>
            </thead>
            <tbody>
                {% for name, param in summary.design_parameters.items() %}
                <tr>
                    <td><span class="tag tag-param">{{ name }}</span></td>
                    <td>{{ param.distribution_type }}</td>
                    <td>
                        {% if param.distribution_type == 'fixed' %}
                        value={{ param.config.get('value', 'N/A') }}
                        {% elif param.distribution_type == 'uniform' %}
                        min={{ param.config.get('min') }}, max={{ param.config.get('max') }}
                        {% elif param.distribution_type == 'normal' %}
                        mean={{ param.config.get('mean') }}, std={{ param.config.get('std') }}
                        {% elif param.distribution_type == 'discrete' %}
                        {{ param.config.get('values', {}).items()|list|length }} values
                        {% elif param.distribution_type == 'gumbel' %}
                        mu={{ param.config.get('mu') }}, beta={{ param.config.get('beta') }}
                        {% else %}
                        {{ param.config }}
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% endif %}
    </section>

    {% if charts %}
    <!-- Visualizations -->
    <section class="card">
        <h2>Visualizations</h2>
        <div class="grid">
            {% for name, chart_b64 in charts.items() %}
            <div class="chart">
                <h3>{{ name|replace('_', ' ')|title }}</h3>
                <img src="{{ chart_b64 }}" alt="{{ name }}">
            </div>
            {% endfor %}
        </div>
    </section>
    {% endif %}

    <!-- Job Analysis -->
    <section class="card">
        <h2>Job Analysis</h2>
        <table>
            <thead>
                <tr>
                    <th>Job Type</th>
                    <th>Count</th>
                    <th>Percentage</th>
                </tr>
            </thead>
            <tbody>
                {% for job_type, count in summary.jobs_by_type.items()|sort(attribute='1', reverse=true) %}
                <tr>
                    <td>{{ job_type }}</td>
                    <td>{{ count }}</td>
                    <td>{{ '%.1f'|format(count / summary.total_jobs * 100 if summary.total_jobs > 0 else 0) }}%</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </section>

    {% if summary.execution_stats %}
    <!-- Execution Status -->
    <section class="card">
        <h2>Execution Status</h2>
        <div class="stat-grid">
            <div class="stat-box">
                <div class="stat-value">{{ '%.1f'|format(summary.execution_stats.completion_rate * 100) }}%</div>
                <div class="stat-label">Completion</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary.execution_stats.total_queues_processed }}</div>
                <div class="stat-label">Processed</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary.execution_stats.total_queues_pending }}</div>
                <div class="stat-label">Pending</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{{ summary.execution_stats.total_queues_processing }}</div>
                <div class="stat-label">Processing</div>
            </div>
        </div>
    </section>
    {% endif %}

    {% if not summary.is_valid or summary.errors or summary.warnings %}
    <!-- Validation -->
    <section class="card">
        <h2>Validation</h2>
        {% for error in summary.errors %}
        <div class="validation-error">ERROR: {{ error }}</div>
        {% endfor %}
        {% for warning in summary.warnings %}
        <div class="validation-warning">WARNING: {{ warning }}</div>
        {% endfor %}
    </section>
    {% endif %}

    <footer>
        Generated: {{ timestamp }} | Source: {{ summary.source_file or 'API' }}
        {% if summary.source_file %}| File: {{ summary.source_file }}{% endif %}
    </footer>
</body>
</html>"""


COMPARISON_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Experiment Comparison</title>
    <style>
        /* Same styles as report template */
        :root { --primary: #2563eb; --bg: #f8fafc; --card-bg: #ffffff; --text: #1e293b; --text-muted: #64748b; --border: #e2e8f0; }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: system-ui, sans-serif; max-width: 1600px; margin: 0 auto; padding: 2rem; background: var(--bg); }
        .header { background: linear-gradient(135deg, #7c3aed 0%, #5b21b6 100%); color: white; padding: 2rem; border-radius: 12px; margin-bottom: 2rem; }
        .card { background: var(--card-bg); border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); padding: 1.5rem; margin-bottom: 1.5rem; }
        h2 { color: var(--text); font-size: 1.25rem; margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 2px solid var(--primary); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
        .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }
        .diff-positive { color: #22c55e; }
        .diff-negative { color: #ef4444; }
        footer { text-align: center; color: var(--text-muted); margin-top: 2rem; }
    </style>
</head>
<body>
    <header class="header">
        <h1>Experiment Comparison</h1>
        <p>{{ comparison.summary_a.name }} vs {{ comparison.summary_b.name }}</p>
    </header>

    <section class="card">
        <h2>Summary Differences</h2>
        <table>
            <thead>
                <tr><th>Metric</th><th>Experiment A</th><th>Experiment B</th><th>Difference</th></tr>
            </thead>
            <tbody>
                <tr>
                    <td>Crawler Count</td>
                    <td>{{ comparison.summary_a.crawler_count }}</td>
                    <td>{{ comparison.summary_b.crawler_count }}</td>
                    <td class="{{ 'diff-positive' if comparison.crawler_count_diff > 0 else 'diff-negative' if comparison.crawler_count_diff < 0 else '' }}">
                        {{ '%+d'|format(comparison.crawler_count_diff) }}
                    </td>
                </tr>
                <tr>
                    <td>Total Jobs</td>
                    <td>{{ comparison.summary_a.total_jobs }}</td>
                    <td>{{ comparison.summary_b.total_jobs }}</td>
                    <td class="{{ 'diff-positive' if comparison.job_count_diff > 0 else 'diff-negative' if comparison.job_count_diff < 0 else '' }}">
                        {{ '%+d'|format(comparison.job_count_diff) }}
                    </td>
                </tr>
                <tr>
                    <td>Total Queues</td>
                    <td>{{ comparison.summary_a.total_queues }}</td>
                    <td>{{ comparison.summary_b.total_queues }}</td>
                    <td class="{{ 'diff-positive' if comparison.queue_count_diff > 0 else 'diff-negative' if comparison.queue_count_diff < 0 else '' }}">
                        {{ '%+d'|format(comparison.queue_count_diff) }}
                    </td>
                </tr>
            </tbody>
        </table>
    </section>

    {% if comparison.differences %}
    <section class="card">
        <h2>Detailed Differences ({{ comparison.differences|length }})</h2>
        <table>
            <thead>
                <tr><th>Category</th><th>Field</th><th>Value A</th><th>Value B</th></tr>
            </thead>
            <tbody>
                {% for diff in comparison.differences[:30] %}
                <tr>
                    <td>{{ diff.category }}</td>
                    <td>{{ diff.field }}</td>
                    <td>{{ diff.value_a }}</td>
                    <td>{{ diff.value_b }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% if comparison.differences|length > 30 %}
        <p style="color: var(--text-muted); margin-top: 1rem;">... and {{ comparison.differences|length - 30 }} more differences</p>
        {% endif %}
    </section>
    {% endif %}

    {% if charts %}
    <section class="card">
        <h2>Comparison Charts</h2>
        <div class="comparison-grid">
            {% for name, chart_b64 in charts.items() %}
            <div style="text-align: center;">
                <h3 style="margin-bottom: 1rem; color: var(--text-muted);">{{ name|replace('_', ' ')|title }}</h3>
                <img src="{{ chart_b64 }}" alt="{{ name }}" style="max-width: 100%; border-radius: 8px;">
            </div>
            {% endfor %}
        </div>
    </section>
    {% endif %}

    <footer>Generated: {{ timestamp }}</footer>
</body>
</html>"""


class HTMLGenerator:
    """Generate self-contained HTML reports from experiment summaries."""

    def __init__(
        self,
        summary: ExperimentSummary,
        charts: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize the HTML generator.

        Args:
            summary: The experiment summary to generate HTML for
            charts: Dictionary of chart names to base64-encoded PNGs
        """
        self.summary = summary
        self.charts = charts or {}

    def generate(self) -> str:
        """Generate the HTML report."""
        try:
            from jinja2 import Template
            template = Template(REPORT_TEMPLATE)
            return template.render(
                summary=self.summary,
                charts=self.charts,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except ImportError:
            # Fallback without Jinja2
            return self._generate_fallback()

    def _generate_fallback(self) -> str:
        """Generate HTML without Jinja2."""
        charts_html = ""
        for name, chart_b64 in self.charts.items():
            charts_html += f"""
            <section class="card">
                <h2>{name.replace('_', ' ').title()}</h2>
                <div class="chart"><img src="{chart_b64}" alt="{name}"></div>
            </section>
            """

        return FALLBACK_TEMPLATE.format(
            name=self.summary.name,
            description=self.summary.description or "",
            crawler_count=self.summary.crawler_count,
            total_jobs=self.summary.total_jobs,
            source=self.summary.source.value,
            text_summary=self.summary.to_text(),
            charts_html=charts_html,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )


class ComparisonHTMLGenerator:
    """Generate HTML reports for experiment comparisons."""

    def __init__(
        self,
        comparison: ExperimentComparison,
        charts: Optional[Dict[str, str]] = None,
    ):
        self.comparison = comparison
        self.charts = charts or {}

    def generate(self) -> str:
        """Generate the comparison HTML report."""
        try:
            from jinja2 import Template
            template = Template(COMPARISON_TEMPLATE)
            return template.render(
                comparison=self.comparison,
                charts=self.charts,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except ImportError:
            # Simple fallback
            return f"""<!DOCTYPE html>
<html><head><title>Comparison</title></head>
<body>
<h1>Experiment Comparison</h1>
<pre>{self.comparison.to_text()}</pre>
</body>
</html>"""
