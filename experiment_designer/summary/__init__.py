"""
Experiment Summary Module

Provides unified summary generation for YAML specs, JSON specs, and API experiments.
Includes visualization with matplotlib and HTML report generation.
"""

from .models import (
    DataSource,
    ExperimentSummary,
    CrawlerSummary,
    StatisticalSummary,
    NumericStats,
    FactorSummary,
    ParameterSummary,
    ExecutionStats,
    ExperimentComparison,
)
from .generator import SummaryGenerator
from .analyzer import ExperimentAnalyzer
from .visualizer import ExperimentVisualizer
from .html_generator import HTMLGenerator, ComparisonHTMLGenerator
from .comparator import compare_summaries

__all__ = [
    # Models
    "DataSource",
    "ExperimentSummary",
    "CrawlerSummary",
    "StatisticalSummary",
    "NumericStats",
    "FactorSummary",
    "ParameterSummary",
    "ExecutionStats",
    "ExperimentComparison",
    # Classes
    "SummaryGenerator",
    "ExperimentAnalyzer",
    "ExperimentVisualizer",
    "HTMLGenerator",
    "ComparisonHTMLGenerator",
    # Functions
    "compare_summaries",
]
