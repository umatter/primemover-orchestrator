"""
Tests for component base classes and health checks.

Run with: pytest orchestrator/tests/test_components.py -v
"""

from pathlib import Path

from orchestrator.components.base import (
    BaseComponent,
    ComponentHealth,
    ComponentStatus,
)


class TestComponentStatus:
    """Tests for ComponentStatus enum."""

    def test_status_values(self):
        """Test all status values exist."""
        assert ComponentStatus.RUNNING.value == "running"
        assert ComponentStatus.STOPPED.value == "stopped"
        assert ComponentStatus.STARTING.value == "starting"
        assert ComponentStatus.STOPPING.value == "stopping"
        assert ComponentStatus.ERROR.value == "error"
        assert ComponentStatus.UNKNOWN.value == "unknown"

    def test_status_from_string(self):
        """Test creating status from string value."""
        status = ComponentStatus("running")
        assert status == ComponentStatus.RUNNING


class TestComponentHealth:
    """Tests for ComponentHealth dataclass."""

    def test_healthy_status(self, healthy_component_health):
        """Test is_healthy returns True for RUNNING status."""
        assert healthy_component_health.is_healthy is True

    def test_stopped_not_healthy(self, stopped_component_health):
        """Test is_healthy returns False for STOPPED status."""
        assert stopped_component_health.is_healthy is False

    def test_error_not_healthy(self, error_component_health):
        """Test is_healthy returns False for ERROR status."""
        assert error_component_health.is_healthy is False

    def test_to_dict(self, healthy_component_health):
        """Test conversion to dictionary."""
        result = healthy_component_health.to_dict()

        assert result["status"] == "running"
        assert result["message"] == "Component is running"
        assert result["details"] == {"uptime": "1h 30m"}
        assert result["url"] == "http://localhost:8090"

    def test_to_dict_with_none_url(self, stopped_component_health):
        """Test to_dict handles None URL."""
        result = stopped_component_health.to_dict()

        assert result["url"] is None

    def test_default_values(self):
        """Test default values for ComponentHealth."""
        health = ComponentHealth(status=ComponentStatus.UNKNOWN)

        assert health.message == ""
        assert health.details == {}
        assert health.url is None


class ConcreteComponent(BaseComponent):
    """Concrete implementation of BaseComponent for testing."""

    def __init__(self, name: str, path: Path = None):
        super().__init__(name, path)
        self._running = False

    def start(self) -> ComponentHealth:
        self._running = True
        return ComponentHealth(
            status=ComponentStatus.RUNNING,
            message="Started successfully",
        )

    def stop(self, remove_volumes: bool = False) -> ComponentHealth:
        self._running = False
        return ComponentHealth(
            status=ComponentStatus.STOPPED,
            message="Stopped successfully",
        )

    def status(self) -> ComponentHealth:
        if self._running:
            return ComponentHealth(
                status=ComponentStatus.RUNNING,
                message="Running",
            )
        return ComponentHealth(
            status=ComponentStatus.STOPPED,
            message="Not running",
        )

    def is_running(self) -> bool:
        return self._running


class TestBaseComponent:
    """Tests for BaseComponent abstract class."""

    def test_init_with_name(self):
        """Test initialization with just name."""
        component = ConcreteComponent("test-component")

        assert component.name == "test-component"
        assert component.path is None

    def test_init_with_path(self, tmp_path):
        """Test initialization with name and path."""
        component = ConcreteComponent("test-component", tmp_path)

        assert component.name == "test-component"
        assert component.path == tmp_path

    def test_start(self):
        """Test start method."""
        component = ConcreteComponent("test")
        result = component.start()

        assert result.status == ComponentStatus.RUNNING
        assert component.is_running() is True

    def test_stop(self):
        """Test stop method."""
        component = ConcreteComponent("test")
        component.start()
        result = component.stop()

        assert result.status == ComponentStatus.STOPPED
        assert component.is_running() is False

    def test_restart(self):
        """Test restart method (default implementation)."""
        component = ConcreteComponent("test")
        component.start()

        result = component.restart()

        assert result.status == ComponentStatus.RUNNING
        assert component.is_running() is True

    def test_status_when_running(self):
        """Test status when component is running."""
        component = ConcreteComponent("test")
        component.start()

        result = component.status()

        assert result.status == ComponentStatus.RUNNING

    def test_status_when_stopped(self):
        """Test status when component is stopped."""
        component = ConcreteComponent("test")

        result = component.status()

        assert result.status == ComponentStatus.STOPPED

    def test_validate_with_existing_path(self, tmp_path):
        """Test validate with existing path."""
        component = ConcreteComponent("test", tmp_path)

        is_valid, errors = component.validate()

        assert is_valid is True
        assert errors == []

    def test_validate_with_nonexistent_path(self):
        """Test validate with non-existent path."""
        component = ConcreteComponent("test", Path("/nonexistent/path"))

        is_valid, errors = component.validate()

        assert is_valid is False
        assert len(errors) == 1
        assert "does not exist" in errors[0]

    def test_validate_without_path(self):
        """Test validate when path is None."""
        component = ConcreteComponent("test")

        is_valid, errors = component.validate()

        assert is_valid is True
        assert errors == []

    def test_repr(self, tmp_path):
        """Test string representation."""
        component = ConcreteComponent("test-component", tmp_path)

        repr_str = repr(component)

        assert "ConcreteComponent" in repr_str
        assert "test-component" in repr_str


class TestComponentIntegration:
    """Integration tests for component lifecycle."""

    def test_full_lifecycle(self):
        """Test complete component lifecycle."""
        component = ConcreteComponent("integration-test")

        # Initially stopped
        assert component.is_running() is False

        # Start
        start_result = component.start()
        assert start_result.is_healthy is True
        assert component.is_running() is True

        # Check status
        status_result = component.status()
        assert status_result.status == ComponentStatus.RUNNING

        # Restart
        restart_result = component.restart()
        assert restart_result.is_healthy is True
        assert component.is_running() is True

        # Stop
        stop_result = component.stop()
        assert stop_result.status == ComponentStatus.STOPPED
        assert component.is_running() is False

    def test_multiple_starts(self):
        """Test starting an already running component."""
        component = ConcreteComponent("test")

        component.start()
        result = component.start()  # Start again

        assert result.status == ComponentStatus.RUNNING
        assert component.is_running() is True

    def test_multiple_stops(self):
        """Test stopping an already stopped component."""
        component = ConcreteComponent("test")

        result = component.stop()  # Stop without starting

        assert result.status == ComponentStatus.STOPPED
        assert component.is_running() is False
