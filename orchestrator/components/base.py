"""
Base classes for PrimeMover component management.

Provides abstract base class and common types for managing
different PrimeMover components (API, Runner, Monitoring).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class ComponentStatus(Enum):
    """Status of a managed component."""

    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Health check result for a component."""

    status: ComponentStatus
    message: str = ""
    details: dict = field(default_factory=dict)
    url: Optional[str] = None

    @property
    def is_healthy(self) -> bool:
        """Check if component is healthy (running)."""
        return self.status == ComponentStatus.RUNNING

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "url": self.url,
        }


class BaseComponent(ABC):
    """
    Abstract base class for PrimeMover components.

    Each component type (API, Runner, Monitoring) extends this
    class to provide specific lifecycle management.
    """

    def __init__(self, name: str, path: Optional[Path] = None):
        """
        Initialize component.

        Args:
            name: Human-readable component name
            path: Path to component directory (if applicable)
        """
        self.name = name
        self.path = path

    @abstractmethod
    def start(self) -> ComponentHealth:
        """
        Start the component.

        Returns:
            ComponentHealth with status after start attempt
        """
        pass

    @abstractmethod
    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        """
        Stop the component.

        Args:
            remove_volumes: If True, also remove persistent volumes

        Returns:
            ComponentHealth with status after stop attempt
        """
        pass

    @abstractmethod
    def status(self) -> ComponentHealth:
        """
        Get current component status.

        Returns:
            ComponentHealth with current status and details
        """
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """
        Quick check if component is running.

        Returns:
            True if component is running, False otherwise
        """
        pass

    def restart(self) -> ComponentHealth:
        """
        Restart the component.

        Default implementation stops then starts.
        Subclasses may override for more efficient restart.

        Returns:
            ComponentHealth with status after restart
        """
        self.stop()
        return self.start()

    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate component configuration and prerequisites.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if self.path is not None and not self.path.exists():
            errors.append(f"Component path does not exist: {self.path}")

        return len(errors) == 0, errors

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, path={self.path})"
