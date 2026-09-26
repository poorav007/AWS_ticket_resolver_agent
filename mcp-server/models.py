"""Typed domain models for the Ticket Resolver MCP server.

These dataclasses are the contract between the AWS backends and the tools.
Keeping them separate from both sides means a boto3 response shape change or a
change in the simulated state file only affects its own backend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _iso(value: Any) -> str | None:
    """Normalise a datetime (or ISO string, or None) to an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    """Convert a dataclass (or anything dataclass-able) to a plain dict."""
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}


# --------------------------------------------------------------------------
# ECS
# --------------------------------------------------------------------------
@dataclass
class Deployment:
    """One ECS deployment of a service."""

    id: str
    status: str
    rollout_state: str | None
    rollout_state_reason: str | None
    desired_count: int
    running_count: int
    pending_count: int
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_aws(cls, raw: dict[str, Any]) -> "Deployment":
        return cls(
            id=raw.get("id", "unknown"),
            status=raw.get("status", "UNKNOWN"),
            rollout_state=raw.get("rolloutState"),
            rollout_state_reason=raw.get("rolloutStateReason"),
            desired_count=int(raw.get("desiredCount", 0) or 0),
            running_count=int(raw.get("runningCount", 0) or 0),
            pending_count=int(raw.get("pendingCount", 0) or 0),
            created_at=_iso(raw.get("createdAt")),
            updated_at=_iso(raw.get("updatedAt")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceHealth:
    """Health snapshot of an ECS service, as an operator cares about it."""

    cluster: str
    service: str
    status: str
    desired_count: int
    running_count: int
    pending_count: int
    launch_type: str | None
    task_definition: str | None
    platform_version: str | None
    # Set when the service exists but is not fully converged.
    stable: bool
    # Human-readable reasons the service is degraded, if any.
    issues: list[str] = field(default_factory=list)
    deployments: list[Deployment] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_aws(
        cls,
        cluster: str,
        service_name: str,
        raw: dict[str, Any],
        events: list[dict[str, Any]] | None = None,
    ) -> "ServiceHealth":
        deployments = [
            Deployment.from_aws(d) for d in raw.get("deployments", []) or []
        ]

        desired = int(raw.get("desiredCount", 0) or 0)
        running = int(raw.get("runningCount", 0) or 0)
        pending = int(raw.get("pendingCount", 0) or 0)

        issues: list[str] = []
        if running < desired:
            issues.append(
                f"Only {running} of {desired} desired tasks are running "
                f"({pending} pending)."
            )

        # A deployment is "unhealthy" if ECS itself says so, or if it is stuck
        # mid-rollout past a short window. Only the PRIMARY deployment is
        # authoritative: a tripped circuit breaker on a superseded ACTIVE
        # deployment is history, not a current fault.
        for dep in deployments:
            if dep.status != "PRIMARY":
                continue
            state = (dep.rollout_state or "").upper()
            if state == "FAILED":
                issues.append(
                    f"Deployment {dep.id} FAILED: {dep.rollout_state_reason or 'no reason given'}"
                )
            elif state == "IN_PROGRESS" and dep.rollout_state_reason and "circuit breaker" in dep.rollout_state_reason.lower():
                issues.append(
                    f"Deployment {dep.id} tripped the ECS deployment circuit breaker: "
                    f"{dep.rollout_state_reason}"
                )

        return cls(
            cluster=cluster,
            service=raw.get("serviceName", service_name),
            status=raw.get("status", "UNKNOWN"),
            desired_count=desired,
            running_count=running,
            pending_count=pending,
            launch_type=raw.get("launchType"),
            task_definition=raw.get("taskDefinition"),
            platform_version=raw.get("platformVersion"),
            stable=(running == desired and running > 0),
            issues=issues,
            deployments=deployments,
            events=events or [],
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["deployments"] = [d.to_dict() for d in self.deployments]
        return payload


# --------------------------------------------------------------------------
# CloudWatch Logs
# --------------------------------------------------------------------------
@dataclass
class LogEvent:
    """A single CloudWatch log event."""

    timestamp: str | None
    message: str
    log_stream: str | None = None
    ingestion_time: str | None = None

    @classmethod
    def from_aws(cls, raw: dict[str, Any]) -> "LogEvent":
        return cls(
            timestamp=_iso(raw.get("timestamp")),
            message=raw.get("message", ""),
            log_stream=raw.get("logStreamName"),
            ingestion_time=_iso(raw.get("ingestionTime")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# CloudWatch Metrics
# --------------------------------------------------------------------------
@dataclass
class MetricPoint:
    timestamp: str | None
    average: float | None
    maximum: float | None
    minimum: float | None
    sample_count: float | None
    unit: str | None = None

    @classmethod
    def from_aws(cls, raw: dict[str, Any]) -> "MetricPoint":
        return cls(
            timestamp=_iso(raw.get("timestamp")),
            average=raw.get("Average"),
            maximum=raw.get("Maximum"),
            minimum=raw.get("Minimum"),
            sample_count=raw.get("SampleCount"),
            unit=raw.get("Unit"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MetricSeries:
    """A named metric time series with a short human summary."""

    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    unit: str
    points: list[MetricPoint] = field(default_factory=list)

    @property
    def latest(self) -> float | None:
        for point in reversed(self.points):
            if point.average is not None:
                return point.average
        return None

    @property
    def peak(self) -> float | None:
        values = [p.maximum if p.maximum is not None else p.average for p in self.points]
        values = [v for v in values if v is not None]
        return max(values) if values else None

    def to_dict(self, max_points: int | None = None) -> dict[str, Any]:
        """Serialise the series.

        ``max_points`` down-samples the point list (always keeping the newest
        point and the peak) so a long window cannot flood an agent's context.
        The scalar summary fields are computed from the *full* series, so
        down-sampling never changes the reported peak or latest value.
        """
        points = self.points
        if max_points is not None and 0 < max_points < len(points):
            last = len(points) - 1
            peak = self.peak

            # Reserve slots for the two points that must never be dropped: the
            # newest, and the peak. Everything else is filled by even spacing,
            # so the result is a hard cap rather than "roughly max_points".
            protected: set[int] = {last}
            if peak is not None:
                for i, p in enumerate(points):
                    if (p.maximum if p.maximum is not None else p.average) == peak:
                        protected.add(i)
                        break

            budget = max(1, max_points - len(protected))
            candidates = [i for i in range(len(points)) if i not in protected]
            if candidates:
                step = len(candidates) / budget
                for j in range(budget):
                    protected.add(candidates[int(j * step)])

            points = [p for i, p in enumerate(points) if i in protected]

        return {
            "namespace": self.namespace,
            "metricName": self.metric_name,
            "dimensions": self.dimensions,
            "unit": self.unit,
            "latest": self.latest,
            "peak": self.peak,
            "first": self.points[0].average if self.points else None,
            "sampleCount": len(self.points),
            "returnedPoints": len(points),
            "points": [p.to_dict() for p in points],
        }


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------
@dataclass
class Ticket:
    """An incident ticket loaded from a local JSON file."""

    id: str
    title: str
    severity: str
    service: str
    cluster: str
    status: str
    description: str = ""
    log_group: str | None = None
    symptoms: list[str] = field(default_factory=list)
    runbook: str | None = None
    reported_at: str | None = None
    resolution: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], ticket_id: str | None = None) -> "Ticket":
        return cls(
            id=str(raw.get("id") or ticket_id or "UNKNOWN"),
            title=raw.get("title", "Untitled ticket"),
            severity=raw.get("severity", "UNSET"),
            service=raw.get("service", ""),
            cluster=raw.get("cluster", ""),
            status=raw.get("status", "OPEN"),
            description=raw.get("description", ""),
            log_group=raw.get("logGroup") or raw.get("log_group"),
            symptoms=list(raw.get("symptoms", []) or []),
            runbook=raw.get("runbook"),
            reported_at=_iso(raw.get("reportedAt") or raw.get("reported_at")),
            resolution=raw.get("resolution"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise using camelCase, matching the rest of the tool payloads."""
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "service": self.service,
            "cluster": self.cluster,
            "status": self.status,
            "description": self.description,
            "logGroup": self.log_group,
            "symptoms": self.symptoms,
            "runbook": self.runbook,
            "reportedAt": self.reported_at,
            "resolution": self.resolution,
        }
