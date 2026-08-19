"""
Parameter Distributions - Generate random values from various probability distributions.

Supports:
- Uniform distribution (continuous between min and max)
- Discrete distribution (categorical with proportions)
- Normal distribution (Gaussian with mean and std)
- Gumbel distribution (for error terms)
- Fixed value (constant)

All distributions support reproducible randomization via random.Random seed.
"""

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class Distribution(ABC):
    """Abstract base class for probability distributions."""

    @abstractmethod
    def sample(self, rng: random.Random) -> Any:
        """
        Sample a value from the distribution.

        Args:
            rng: Random number generator instance

        Returns:
            Sampled value
        """
        pass

    @abstractmethod
    def describe(self) -> str:
        """Return human-readable description of the distribution."""
        pass


@dataclass
class UniformDistribution(Distribution):
    """
    Uniform distribution over a continuous range.

    Parameters:
        min: Minimum value (inclusive)
        max: Maximum value (inclusive)

    Example YAML:
        pi:
          distribution: uniform
          min: -1
          max: 1
    """

    min: float
    max: float

    def sample(self, rng: random.Random) -> float:
        return rng.uniform(self.min, self.max)

    def describe(self) -> str:
        return f"Uniform({self.min}, {self.max})"


@dataclass
class DiscreteDistribution(Distribution):
    """
    Discrete distribution with specified proportions.

    Parameters:
        values: Dict mapping value to proportion (must sum to ~1.0)

    Example YAML:
        kappa:
          distribution: discrete
          values:
            0: 0.25   # 1/4 of samples
            1: 0.25   # 1/4 of samples
            2: 0.50   # 2/4 of samples
    """

    values: dict[Any, float]

    def __post_init__(self):
        """Validate and normalize proportions."""
        total = sum(self.values.values())
        if abs(total - 1.0) > 0.01:
            # Normalize proportions
            self.values = {k: v / total for k, v in self.values.items()}

    def sample(self, rng: random.Random) -> Any:
        r = rng.random()
        cumulative = 0.0
        for value, proportion in self.values.items():
            cumulative += proportion
            if r <= cumulative:
                return value
        # Return last value if we fall through (due to floating point)
        return list(self.values.keys())[-1]

    def describe(self) -> str:
        items = ", ".join(f"{k}: {v:.2f}" for k, v in self.values.items())
        return f"Discrete({{{items}}})"


@dataclass
class NormalDistribution(Distribution):
    """
    Normal (Gaussian) distribution.

    Parameters:
        mean: Mean of the distribution
        std: Standard deviation

    Example YAML:
        alpha:
          distribution: normal
          mean: 0.35
          std: 0.1
    """

    mean: float
    std: float

    def sample(self, rng: random.Random) -> float:
        return rng.gauss(self.mean, self.std)

    def describe(self) -> str:
        return f"Normal({self.mean}, {self.std})"


@dataclass
class GumbelDistribution(Distribution):
    """
    Gumbel distribution (Type I extreme value distribution).

    Used for error terms in discrete choice models.

    Parameters:
        mu: Location parameter
        beta: Scale parameter

    Example YAML:
        epsilon:
          distribution: gumbel
          mu: -0.4557735
          beta: 0.0793006

    Note: The Gumbel distribution uses the inverse CDF method:
        X = mu - beta * ln(-ln(U)) where U ~ Uniform(0, 1)
    """

    mu: float
    beta: float

    def sample(self, rng: random.Random) -> float:
        u = rng.random()
        # Avoid log(0)
        while u == 0:
            u = rng.random()
        return self.mu - self.beta * math.log(-math.log(u))

    def describe(self) -> str:
        return f"Gumbel({self.mu}, {self.beta})"


@dataclass
class TruncatedNormalDistribution(Distribution):
    """
    Truncated normal distribution (bounded Gaussian).

    Parameters:
        mean: Mean of the underlying normal distribution
        std: Standard deviation
        min: Minimum value (lower bound)
        max: Maximum value (upper bound)

    Example YAML:
        beta:
          distribution: truncated_normal
          mean: 0.25
          std: 0.1
          min: 0
          max: 0.5
    """

    mean: float
    std: float
    min: float
    max: float

    def sample(self, rng: random.Random) -> float:
        # Simple rejection sampling
        for _ in range(1000):
            value = rng.gauss(self.mean, self.std)
            if self.min <= value <= self.max:
                return value
        # Fallback: clamp to bounds
        return max(self.min, min(self.max, self.mean))

    def describe(self) -> str:
        return f"TruncatedNormal({self.mean}, {self.std}, [{self.min}, {self.max}])"


@dataclass
class BetaDistribution(Distribution):
    """
    Beta distribution (bounded continuous distribution).

    Useful for generating proportions/probabilities.

    Parameters:
        alpha: Shape parameter alpha > 0
        beta: Shape parameter beta > 0
        min: Optional minimum value (default 0)
        max: Optional maximum value (default 1)

    Example YAML:
        engagement:
          distribution: beta
          alpha: 2
          beta: 5
    """

    alpha: float
    beta: float
    min: float = 0.0
    max: float = 1.0

    def sample(self, rng: random.Random) -> float:
        value = rng.betavariate(self.alpha, self.beta)
        # Scale to [min, max] range
        return self.min + value * (self.max - self.min)

    def describe(self) -> str:
        return f"Beta({self.alpha}, {self.beta})"


@dataclass
class FixedValue(Distribution):
    """
    Fixed (constant) value - not really a distribution.

    Parameters:
        value: The constant value to return

    Example YAML:
        tau:
          value: 1
    """

    value: Any

    def sample(self, rng: random.Random) -> Any:
        return self.value

    def describe(self) -> str:
        return f"Fixed({self.value})"


class DistributionFactory:
    """
    Factory for creating Distribution instances from configuration dicts.

    Example usage:
        config = {
            "distribution": "uniform",
            "min": -1,
            "max": 1
        }
        dist = DistributionFactory.create(config)
        value = dist.sample(rng)
    """

    @staticmethod
    def create(config: dict[str, Any] | Any) -> Distribution:
        """
        Create a Distribution from a configuration dict.

        Args:
            config: Configuration dict with 'distribution' key, or a simple value

        Returns:
            Distribution instance

        Raises:
            ValueError: If distribution type is unknown or config is invalid
        """
        # Handle simple values (not a dict)
        if not isinstance(config, dict):
            return FixedValue(value=config)

        # Handle fixed value shorthand
        if "value" in config and "distribution" not in config:
            return FixedValue(value=config["value"])

        dist_type = config.get("distribution", "fixed")

        if dist_type == "uniform":
            return UniformDistribution(min=float(config["min"]), max=float(config["max"]))

        elif dist_type == "discrete":
            values = config.get("values", {})
            # Convert string keys to appropriate types (int, float, etc.)
            # Keys stay heterogeneous on purpose: numeric-looking keys become
            # int/float, everything else keeps its original (str) form.
            converted: dict[int | float | str, float] = {}
            for k, v in values.items():
                try:
                    # Try to convert key to int or float
                    if isinstance(k, str):
                        if "." in k:
                            converted[float(k)] = float(v)
                        else:
                            try:
                                converted[int(k)] = float(v)
                            except ValueError:
                                converted[k] = float(v)
                    else:
                        converted[k] = float(v)
                except ValueError:
                    converted[k] = float(v)
            return DiscreteDistribution(values=converted)

        elif dist_type == "normal":
            return NormalDistribution(mean=float(config["mean"]), std=float(config["std"]))

        elif dist_type == "gumbel":
            return GumbelDistribution(mu=float(config["mu"]), beta=float(config["beta"]))

        elif dist_type == "truncated_normal":
            return TruncatedNormalDistribution(
                mean=float(config["mean"]),
                std=float(config["std"]),
                min=float(config["min"]),
                max=float(config["max"]),
            )

        elif dist_type == "beta":
            return BetaDistribution(
                alpha=float(config["alpha"]),
                beta=float(config["beta"]),
                min=float(config.get("min", 0)),
                max=float(config.get("max", 1)),
            )

        elif dist_type == "fixed":
            return FixedValue(value=config.get("value", 0))

        else:
            raise ValueError(f"Unknown distribution type: {dist_type}")

    @staticmethod
    def list_types() -> list[str]:
        """List all available distribution types."""
        return ["uniform", "discrete", "normal", "gumbel", "truncated_normal", "beta", "fixed"]

    @staticmethod
    def get_schema(dist_type: str) -> dict[str, Any]:
        """
        Get the parameter schema for a distribution type.

        Args:
            dist_type: Distribution type name

        Returns:
            Dict describing required and optional parameters
        """
        schemas = {
            "uniform": {
                "required": ["min", "max"],
                "optional": [],
                "description": "Continuous uniform distribution",
                "example": {"distribution": "uniform", "min": -1, "max": 1},
            },
            "discrete": {
                "required": ["values"],
                "optional": [],
                "description": "Discrete distribution with proportions",
                "example": {"distribution": "discrete", "values": {0: 0.25, 1: 0.75}},
            },
            "normal": {
                "required": ["mean", "std"],
                "optional": [],
                "description": "Gaussian distribution",
                "example": {"distribution": "normal", "mean": 0, "std": 1},
            },
            "gumbel": {
                "required": ["mu", "beta"],
                "optional": [],
                "description": "Gumbel (Type I extreme value) distribution",
                "example": {"distribution": "gumbel", "mu": 0, "beta": 1},
            },
            "truncated_normal": {
                "required": ["mean", "std", "min", "max"],
                "optional": [],
                "description": "Bounded Gaussian distribution",
                "example": {
                    "distribution": "truncated_normal",
                    "mean": 0.5,
                    "std": 0.2,
                    "min": 0,
                    "max": 1,
                },
            },
            "beta": {
                "required": ["alpha", "beta"],
                "optional": ["min", "max"],
                "description": "Beta distribution (bounded continuous)",
                "example": {"distribution": "beta", "alpha": 2, "beta": 5},
            },
            "fixed": {
                "required": ["value"],
                "optional": [],
                "description": "Constant value",
                "example": {"value": 1},
            },
        }
        return schemas.get(dist_type, {})


class ParameterSampler:
    """
    Sample values for multiple parameters from their distributions.

    Example usage:
        sampler = ParameterSampler(seed=42)
        sampler.add_parameter("pi", {"distribution": "uniform", "min": -1, "max": 1})
        sampler.add_parameter("kappa", {"distribution": "discrete", "values": {0: 0.25, 1: 0.75}})

        # Sample values for one crawler
        values = sampler.sample()
        # {"pi": 0.352, "kappa": 1}

        # Sample for multiple crawlers
        all_values = sampler.sample_batch(100)
        # [{"pi": 0.352, "kappa": 1}, {"pi": -0.841, "kappa": 0}, ...]
    """

    def __init__(self, seed: int | None = None):
        """
        Initialize parameter sampler.

        Args:
            seed: Random seed for reproducibility
        """
        self.rng = random.Random(seed)
        self.parameters: dict[str, Distribution] = {}
        self.errors: list[str] = []

    def add_parameter(self, name: str, config: dict[str, Any] | Any) -> None:
        """
        Add a parameter with its distribution.

        Args:
            name: Parameter name
            config: Distribution configuration dict or simple value
        """
        try:
            dist = DistributionFactory.create(config)
            self.parameters[name] = dist
        except Exception as e:
            self.errors.append(f"Error creating distribution for '{name}': {e}")

    def add_parameters(self, params: dict[str, Any]) -> None:
        """
        Add multiple parameters at once.

        Args:
            params: Dict mapping parameter names to distribution configs
        """
        for name, config in params.items():
            self.add_parameter(name, config)

    def sample(self) -> dict[str, Any]:
        """
        Sample values for all parameters.

        Returns:
            Dict mapping parameter names to sampled values
        """
        return {name: dist.sample(self.rng) for name, dist in self.parameters.items()}

    def sample_batch(self, count: int) -> list[dict[str, Any]]:
        """
        Sample values for multiple crawlers/entities.

        Args:
            count: Number of samples to generate

        Returns:
            List of dicts, each containing parameter values
        """
        return [self.sample() for _ in range(count)]

    def describe(self) -> dict[str, str]:
        """
        Get descriptions of all parameter distributions.

        Returns:
            Dict mapping parameter names to distribution descriptions
        """
        return {name: dist.describe() for name, dist in self.parameters.items()}
