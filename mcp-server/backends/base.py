"""Backend interface for AWS access.

Two implementations exist:

  * ``Boto3Backend`` - real AWS via boto3 (default credential chain).
  * ``SimulatedBackend`` - a deterministic in-memory AWS account used for
    local development and demos when no credentials are present.

Everything above this layer (the MCP tools, and therefore the agent) is written
against this interface only. Swapping backends is a configuration change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from models import LogEvent, MetricSeries, ServiceHealth


class AWSBackend(ABC):
    """Abstract AWS capability surface required by the Ticket Resolver tools."""

    #: Short identifier surfaced to the agent, e.g. "boto3" or "simulated".
    name: str = "abstract"

    # -- introspection ----------------------------------------------------
    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Return a short description of this backend for the agent."""

    @abstractmethod
    def healthcheck(self) -> tuple[bool, str]:
        """Return ``(reachable, message)`` without raising."""

    # -- ECS --------------------------------------------------------------
    @abstractmethod
    def list_ecs_services(self, cluster: str) -> list[str]:
        """List service names in ``cluster``.

        Raises:
            ResourceNotFound: if the cluster does not exist.
        """

    @abstractmethod
    def describe_ecs_service(self, cluster: str, service: str) -> ServiceHealth:
        """Return a health snapshot for one service.

        Raises:
            ResourceNotFound: if the cluster or service does not exist.
        """

    @abstractmethod
    def force_new_deployment(self, cluster: str, service: str) -> dict[str, Any]:
        """Roll a service onto a fresh deployment.

        This is a *disruptive* operation and must only be reached through the
        approval-gated remediation tool.

        Raises:
            ResourceNotFound: if the cluster or service does not exist.
        """

    # -- CloudWatch Logs --------------------------------------------------
    @abstractmethod
    def list_log_groups(self, prefix: str = "") -> list[str]:
        """List log group names, optionally filtered by prefix."""

    @abstractmethod
    def filter_log_events(
        self,
        log_group: str,
        minutes: int,
        limit: int,
        filter_pattern: str | None = None,
    ) -> list[LogEvent]:
        """Return recent log events, newest first.

        Raises:
            ResourceNotFound: if the log group does not exist.
        """

    # -- CloudWatch Metrics ----------------------------------------------
    @abstractmethod
    def get_metric_series(
        self,
        namespace: str,
        metric_name: str,
        dimensions: dict[str, str],
        minutes: int,
        period: int = 60,
        stat: str = "Average",
    ) -> MetricSeries:
        """Return a time series for one metric.

        Raises:
            ResourceNotFound: if the metric does not exist.
        """


class ResourceNotFound(Exception):
    """A requested AWS resource does not exist.

    Tools translate this into a structured ``success: false`` result rather
    than letting it escape and kill the MCP server.
    """


class RemediationNotPermitted(Exception):
    """A write operation was attempted while remediation is disabled."""


def metric_key(namespace: str, metric_name: str, dimensions: dict[str, str]) -> str:
    """Stable cache key for a metric series."""
    dim = ",".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
    return f"{namespace}|{metric_name}|{dim}"
