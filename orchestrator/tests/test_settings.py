"""
Tests for orchestrator configuration settings.

Run with: pytest orchestrator/tests/test_settings.py -v
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.config.settings import Settings, get_settings


class TestSettings:
    """Tests for Settings class."""

    def test_default_values(self):
        """Test that settings have sensible defaults."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()

            assert settings.env == "development"
            assert settings.pm_api_path == "../primemover_api"
            assert settings.pm_runner_path == "../primemover_runner"
            assert settings.pm_api_http_port == 8090
            assert settings.pm_api_mysql_port == 3307
            assert settings.grafana_port == 3000
            assert settings.loki_port == 3100

    def test_env_override(self):
        """Test that environment variables override defaults."""
        env_vars = {
            "ENV": "production",
            "PM_API_PATH": "/custom/api/path",
            "PM_API_HTTP_PORT": "9090",
            "GRAFANA_PORT": "4000",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()

            assert settings.env == "production"
            assert settings.pm_api_path == "/custom/api/path"
            assert settings.pm_api_http_port == 9090
            assert settings.grafana_port == 4000

    def test_aws_credentials_optional(self):
        """Test that AWS credentials are optional (None or empty string)."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()

            # AWS credentials can be None or empty string when not set
            assert not settings.aws_access_key_id  # Falsy (None or "")
            assert not settings.aws_secret_access_key  # Falsy (None or "")
            assert settings.aws_region == "eu-central-1"

    def test_aws_credentials_from_env(self):
        """Test that AWS credentials can be set from environment."""
        env_vars = {
            "AWS_ACCESS_KEY_ID": "AKIATEST",
            "AWS_SECRET_ACCESS_KEY": "secret123",
            "AWS_DEFAULT_REGION": "us-west-2",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()

            assert settings.aws_access_key_id == "AKIATEST"
            assert settings.aws_secret_access_key == "secret123"
            assert settings.aws_region == "us-west-2"

    def test_get_api_path_resolves(self):
        """Test that get_api_path returns resolved path."""
        with patch.dict(os.environ, {"PM_API_PATH": "../test_api"}, clear=True):
            settings = Settings()
            path = settings.get_api_path()

            assert isinstance(path, Path)
            assert path.is_absolute()

    def test_get_runner_path_resolves(self):
        """Test that get_runner_path returns resolved path."""
        with patch.dict(os.environ, {"PM_RUNNER_PATH": "../test_runner"}, clear=True):
            settings = Settings()
            path = settings.get_runner_path()

            assert isinstance(path, Path)
            assert path.is_absolute()

    def test_monitoring_urls_default(self):
        """Test default monitoring URLs."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()

            assert settings.grafana_url == "http://localhost:3000"
            assert settings.loki_url == "http://localhost:3100"
            assert settings.prometheus_url == "http://localhost:9090"
            assert settings.tempo_url == "http://localhost:3200"

    def test_grafana_credentials_default(self):
        """Test default Grafana credentials."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)

            assert settings.grafana_admin_user == "admin"
            # No default password for security - must be set via env
            assert settings.grafana_admin_password is None

    def test_lambda_config_defaults(self):
        """Test default Lambda configuration."""
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()

            assert settings.lambda_function_name == "bot-decision-yt"
            assert settings.lambda_log_group == "/aws/lambda/bot-decision-yt"


class TestGetSettings:
    """Tests for get_settings cached function."""

    def test_returns_settings_instance(self):
        """Test that get_settings returns a Settings instance."""
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_caching(self):
        """Test that get_settings returns cached instance."""
        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2

    def test_cache_can_be_cleared(self):
        """Test that settings cache can be cleared."""
        settings1 = get_settings()

        get_settings.cache_clear()

        # After clearing cache, a new instance is created
        settings2 = get_settings()
        # Note: They may be equal but are different instances
        # In practice, this test verifies no errors occur
        assert isinstance(settings2, Settings)
