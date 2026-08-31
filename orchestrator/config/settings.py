"""
Orchestrator configuration using Pydantic Settings.

Configuration is loaded from environment variables and .env file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Orchestrator configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment
    env: str = Field(default="development", alias="ENV")

    # ==========================================================================
    # Component Paths
    # ==========================================================================
    pm_api_path: str = Field(
        default="../primemover_api",
        alias="PM_API_PATH",
        description="Path to primemover_api repository",
    )
    pm_runner_path: str = Field(
        default="../primemover_runner",
        alias="PM_RUNNER_PATH",
        description="Path to primemover_runner repository",
    )
    pm_gui_path: str = Field(
        default="../primemover_gui",
        alias="PM_GUI_PATH",
        description="Path to primemover_gui repository (embedded in API)",
    )

    # ==========================================================================
    # Component Ports
    # ==========================================================================
    pm_api_http_port: int = Field(
        default=8090,
        alias="PM_API_HTTP_PORT",
        description="API HTTP port (Apache)",
    )
    pm_api_mysql_port: int = Field(
        default=3307,
        alias="PM_API_MYSQL_PORT",
        description="API MySQL port",
    )
    pm_api_redis_port: int = Field(
        default=8889,
        alias="PM_API_REDIS_PORT",
        description="API Redis port",
    )

    # Monitoring ports
    grafana_port: int = Field(default=3000, alias="GRAFANA_PORT")
    loki_port: int = Field(default=3100, alias="LOKI_PORT")
    prometheus_port: int = Field(default=9090, alias="PROMETHEUS_PORT")
    tempo_port: int = Field(default=3200, alias="TEMPO_PORT")

    # ==========================================================================
    # Runner Configuration
    # ==========================================================================
    runner_java_opts: str = Field(
        default="-Xmx2g -Xms512m",
        alias="RUNNER_JAVA_OPTS",
        description="JVM options for runner",
    )
    runner_spring_profile: str = Field(
        default="local",
        alias="RUNNER_SPRING_PROFILE",
        description="Spring profile for runner (local, integration, production)",
    )

    # ==========================================================================
    # Monitoring URLs (derived from ports for backwards compatibility)
    # ==========================================================================
    grafana_url: str = Field(default="http://localhost:3000", alias="GRAFANA_URL")
    grafana_admin_user: str = Field(default="admin", alias="GRAFANA_ADMIN_USER")
    # SECURITY: No default password - should be set via environment variable
    grafana_admin_password: str | None = Field(default=None, alias="GRAFANA_ADMIN_PASSWORD")

    # ==========================================================================
    # API Stack Service Credentials (for health checks)
    # ==========================================================================
    # SECURITY: No default passwords - must be set via environment variables
    pm_mysql_password: str | None = Field(default=None, alias="PM_MYSQL_PASSWORD")
    pm_redis_password: str | None = Field(default=None, alias="PM_REDIS_PASSWORD")

    loki_url: str = Field(default="http://localhost:3100", alias="LOKI_URL")
    prometheus_url: str = Field(default="http://localhost:9090", alias="PROMETHEUS_URL")
    tempo_url: str = Field(default="http://localhost:3200", alias="TEMPO_URL")

    # ==========================================================================
    # Log Paths
    # ==========================================================================
    pm_api_log_path: str = Field(
        default="../primemover_api/storage/logs",
        alias="PM_API_LOG_PATH",
    )
    pm_runner_log_path: str = Field(
        default="../primemover_runner/primemover-runner/log",
        alias="PM_RUNNER_LOG_PATH",
    )
    pm_runner_har_path: str = Field(
        default="../primemover_runner/primemover-runner/out/har",
        alias="PM_RUNNER_HAR_PATH",
    )

    # ==========================================================================
    # Job data object store (MinIO)
    # ==========================================================================
    # The runner stopped writing job data to local disk in primemover_runner
    # cadb15e (storage.local.enabled=false) and writes only to the object store
    # its config still calls "awsS3" -- which is not AWS: awsS3.endpoint points
    # at s3.qamelab.org, the MinIO on this box. Nothing followed it, so every
    # HAR-backed reader here silently returned nothing. These settings let the
    # aggregator read job data where it actually lives; leave the credentials
    # unset and it falls back to pm_runner_har_path.
    jobdata_s3_endpoint: str = Field(
        default="https://s3.qamelab.org",
        alias="JOBDATA_S3_ENDPOINT",
    )
    jobdata_s3_bucket: str = Field(default="primemoverrunner", alias="JOBDATA_S3_BUCKET")
    jobdata_s3_access_key: str | None = Field(default=None, alias="JOBDATA_S3_ACCESS_KEY")
    jobdata_s3_secret_key: str | None = Field(default=None, alias="JOBDATA_S3_SECRET_KEY")
    # Job data is spread over two flat prefixes, not a directory per job.
    jobdata_s3_prefixes: str = Field(
        default="reports/,finalsource/",
        alias="JOBDATA_S3_PREFIXES",
    )

    # ==========================================================================
    # PrimeMover API Credentials
    # ==========================================================================
    pm_api_base_url: str = Field(
        default="http://localhost:8090/api/v1",
        alias="PM_API_BASE_URL",
    )
    # SECURITY: No default credentials - must be set via environment variables
    pm_api_email: str | None = Field(default=None, alias="PM_API_EMAIL")
    pm_api_password: str | None = Field(default=None, alias="PM_API_PASSWORD")

    # ==========================================================================
    # E2E residential proxy
    # ==========================================================================
    # The browser-automation e2e scenarios need a working proxy: Multilogin
    # validates a profile's proxy on start by fetching proxy.multilogin.com
    # through it, and refuses to launch the browser if that fails.
    #
    # The scenario JSONs ship with PROXY_*_PLACEHOLDER rather than real values,
    # so credentials are never committed. They are substituted from here at
    # upload time. Leave unset to run only the non-proxy paths (--chrome).
    e2e_proxy_host: str | None = Field(default=None, alias="E2E_PROXY_HOST")
    e2e_proxy_port: int | None = Field(default=None, alias="E2E_PROXY_PORT")
    e2e_proxy_username: str | None = Field(default=None, alias="E2E_PROXY_USERNAME")
    e2e_proxy_password: str | None = Field(default=None, alias="E2E_PROXY_PASSWORD")

    @property
    def e2e_proxy_configured(self) -> bool:
        """Whether a usable e2e proxy has been supplied."""
        return bool(self.e2e_proxy_host and self.e2e_proxy_username and self.e2e_proxy_password)

    # ==========================================================================
    # AWS (for CloudWatch Lambda logs)
    # ==========================================================================
    aws_region: str = Field(default="eu-central-1", alias="AWS_DEFAULT_REGION")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    lambda_function_name: str = Field(default="bot-decision-yt", alias="LAMBDA_FUNCTION_NAME")
    lambda_log_group: str = Field(
        default="/aws/lambda/bot-decision-yt",
        alias="LAMBDA_LOG_GROUP",
    )

    # ==========================================================================
    # Helper Methods
    # ==========================================================================
    def get_api_path(self) -> Path:
        """Get resolved API path."""
        return Path(self.pm_api_path).resolve()

    def get_runner_path(self) -> Path:
        """Get resolved Runner path."""
        return Path(self.pm_runner_path).resolve()

    def get_orchestrator_path(self) -> Path:
        """Get orchestrator path (current directory)."""
        return Path(__file__).parent.parent.parent.resolve()


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
