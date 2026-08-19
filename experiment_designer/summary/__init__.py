"""
Experiment Summary Module

Provides unified summary generation for YAML specs, JSON specs, and API experiments.
Includes visualization with matplotlib and HTML report generation.
"""

from .analyzer import ExperimentAnalyzer
from .comparator import compare_summaries
from .generator import SummaryGenerator
from .html_generator import ComparisonHTMLGenerator, HTMLGenerator
from .models import (
    CrawlerSummary,
    DataSource,
    ExecutionStats,
    ExperimentComparison,
    ExperimentSummary,
    FactorSummary,
    NumericStats,
    ParameterSummary,
    StatisticalSummary,
)
from .visualizer import ExperimentVisualizer

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
