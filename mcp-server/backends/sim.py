"""Simulated AWS backend.

Models a small ECS + CloudWatch account that reproduces the INC-1001 demo
scenario: ``payment-api`` rolled out a build with a broken gateway credential,
so tasks are crash-looping, CloudWatch is full of HTTP 500s, and the latest
deployment has tripped the ECS deployment circuit breaker.

State lives in a JSON file under ``state/`` so it survives MCP server restarts.
That matters: verification is only meaningful if "before remediation" and
"after remediation" are two different, persistent observations.

Time is relative, not hardcoded, so the demo always looks fresh:

    ... healthy traffic ... | incident starts | error storm | [remediation] | healthy
"""

from __future__ import annotations

import json
import logging
import random
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backends.base import AWSBackend, RemediationNotPermitted, ResourceNotFound
from models import Deployment, LogEvent, MetricPoint, MetricSeries, ServiceHealth

log = logging.getLogger("ticket-resolver.sim")

SERVICE = "payment-api"
CLUSTER = "hackathon-cluster"
TASK_DEF_OLD = "arn:aws:ecs:us-east-1:000000000000:task-definition/payment-api:42"
TASK_DEF_BAD = "arn:aws:ecs:us-east-1:000000000000:task-definition/payment-api:43"
TASK_DEF_FIXED = "arn:aws:ecs:us-east-1:000000000000:task-definition/payment-api:44"
LOG_GROUP = "/ecs/payment-api"

# How long ago (at state creation) the bad deploy shipped and broke the service.
_BAD_DEPLOY_AGE_MIN = 24
_INCIDENT_AGE_MIN = 22
# ECS gives up on a failing deployment after roughly this long.
_ROLLOUT_STUCK_MIN = 12

# App namespace metrics the agent can query alongside standard ECS metrics.
APP_NAMESPACE = "TicketResolver/App"

_HEALTHY_PATHS = (
    "GET /api/v1/payments/charge 200 138ms",
    "GET /api/v1/payments/charge 200 151ms",
    "GET /api/v1/payments/refunds 200 96ms",
    "POST /api/v1/payments/charge 200 187ms",
    "GET /health 200 3ms",
)

# The actual root cause, told through the logs.
_ERROR_CYCLE = (
    "ERROR payment-api: Unhandled exception in PaymentGateway.charge() "
    "-> HTTP 500 (transaction rolled back)",
    "ERROR payment-api: ConfigError: GATEWAY_API_KEY is not set or invalid "
    "(task role has no secrets:GetSecretValue for payment-gateway/prod)",
    "ERROR payment-api: payment-gateway client init failed: 401 Unauthorized "
    "from https://gateway.internal/v1/charge",
    "ERROR payment-api: Traceback (most recent call last):\n"
    "  File \"/app/payments/gateway.py\", line 88, in charge\n"
    "    resp = self._client.authorize(payload)\n"
    "  File \"/app/payments/gateway.py\", line 41, in authorize\n"
    "    raise GatewayError(r.status_code, r.text)\n"
    "payment_gateway.errors.GatewayError: 401 Unauthorized",
    "WARN payment-api: circuit breaker open for payment-gateway "
    "(5 consecutive failures)",
    "ERROR payment-api: health check failed: dependency payment-gateway UNHEALTHY",
)

# Emitted once remediation lands, so verification has positive evidence.
_RECOVERY_LINES = (
    "INFO payment-api: reloaded configuration, GATEWAY_API_KEY present",
    "INFO payment-api: payment-gateway client init succeeded (HTTP 200)",
    "INFO payment-api: circuit breaker closed for payment-gateway",
    "INFO payment-api: health check passed: all dependencies HEALTHY",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SimulatedBackend(AWSBackend):
    name = "simulated"

    def __init__(self, config) -> None:
        self._config = config
        self.fallback_reason: str | None = None
        self._path = Path(config.state_dir) / "simulated-aws.json"
        self._state = self._load_or_create()
        self._stamp = self._current_stamp()

    def _current_stamp(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _sync(self) -> None:
        """Reload state if the file changed underneath us.

        The MCP server is long-lived, so it can easily outlive a
        ``reset_demo.py`` run. Without this the server would keep serving
        pre-reset state and the demo could not be replayed.
        """
        stamp = self._current_stamp()
        if stamp is None:
            if self._state.get("remediatedAt"):
                log.info("Sim state file vanished; recreating the incident.")
                self._state = self._default_state()
                self._save(self._state)
        elif stamp != self._stamp:
            log.info("Sim state file changed on disk; reloading.")
            self._state = self._load_or_create()
        self._stamp = self._current_stamp()

    # -- persistence ------------------------------------------------------
    def _default_state(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "createdAt": _iso(now),
            "cluster": CLUSTER,
            "remediatedAt": None,
            "remediationCount": 0,
            "deployments": {
                "bad": {
                    "id": "ecs-svc/9182736450",
                    "status": "PRIMARY",
                    "rolloutState": "IN_PROGRESS",
                    "rolloutStateReason": "ECS deployment circuit breaker.",
                    "desiredCount": 2,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "createdAt": _iso(now - timedelta(minutes=_BAD_DEPLOY_AGE_MIN)),
                    "updatedAt": _iso(now - timedelta(minutes=_ROLLOUT_STUCK_MIN)),
                    "taskDefinition": TASK_DEF_BAD,
                },
                "good": {
                    "id": "ecs-svc/6450918273",
                    "status": "ACTIVE",
                    "rolloutState": "COMPLETED",
                    "rolloutStateReason": None,
                    "desiredCount": 2,
                    "runningCount": 2,
                    "pendingCount": 0,
                    "createdAt": _iso(now - timedelta(minutes=60 * 26)),
                    "updatedAt": _iso(now - timedelta(minutes=_BAD_DEPLOY_AGE_MIN - 1)),
                    "taskDefinition": TASK_DEF_OLD,
                },
            },
        }

    def _load_or_create(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Sim state unreadable, recreating: %s", exc)
        state = self._default_state()
        self._save(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self._config.ensure_state_dir()
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self._path)
        self._stamp = self._current_stamp()

    # -- introspection ----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": self.name,
            "region": self._config.region,
            "source": "simulated AWS account (no real AWS calls are made)",
            "stateFile": str(self._path),
            "cluster": self._state["cluster"],
        }
        if self.fallback_reason:
            info["fallbackReason"] = self.fallback_reason
            info["warning"] = (
                "Simulated backend active because real AWS credentials were not "
                "available. Evidence returned by this backend is synthetic."
            )
        return info

    def healthcheck(self) -> tuple[bool, str]:
        return True, f"Simulated AWS state at {self._path}"

    # -- timeline helpers -------------------------------------------------
    @property
    def _incident_start(self) -> datetime:
        return _parse(self._state["deployments"]["bad"]["createdAt"]) or (
            datetime.now(timezone.utc) - timedelta(minutes=_INCIDENT_AGE_MIN)
        )

    @property
    def _remediated_at(self) -> datetime | None:
        return _parse(self._state.get("remediatedAt"))

    @staticmethod
    def _minute(dt: datetime) -> datetime:
        return dt.replace(second=0, microsecond=0)

    def _is_healthy(self, when: datetime) -> bool:
        """Was the service healthy at ``when``?

        Compared at 1-minute granularity to match how CloudWatch buckets data:
        a remediation at 08:12:08 belongs to the 08:12 bucket, so that bucket is
        treated as post-remediation rather than lingering as "still broken".
        """
        if self._minute(when) < self._minute(self._incident_start):
            return True
        remediated = self._remediated_at
        return remediated is not None and self._minute(when) >= self._minute(remediated)

    # -- ECS --------------------------------------------------------------
    def list_ecs_services(self, cluster: str) -> list[str]:
        self._sync()
        if not cluster:
            raise ValueError("cluster is required")
        if cluster != self._state["cluster"]:
            raise ResourceNotFound(
                f"Cluster {cluster!r} not found. "
                f"The simulated account contains only {self._state['cluster']!r}."
            )
        return [SERVICE]

    def describe_ecs_service(self, cluster: str, service: str) -> ServiceHealth:
        self._sync()
        if not cluster or not service:
            raise ValueError("cluster and service are both required")
        self.list_ecs_services(cluster)  # validates the cluster

        if service != SERVICE:
            raise ResourceNotFound(
                f"No ECS service named {service!r} exists in cluster {cluster!r}. "
                f"Available: [{', '.join(self.list_ecs_services(cluster))}]"
            )

        now = datetime.now(timezone.utc)
        healthy = self._is_healthy(now)
        deployments = self._state["deployments"]

        if healthy and "fixed" in deployments:
            running, pending = 2, 0
            # Once remediated, the fixed deployment is PRIMARY/COMPLETED and the
            # previously-stuck bad deployment drops to ACTIVE (still there, no
            # longer in charge).
            active = [
                deployments["fixed"],
                {**deployments["bad"], "status": "ACTIVE"},
                deployments["good"],
            ]
        else:
            running, pending = 1, 0
            active = [deployments["bad"], deployments["good"]]

        raw = {
            "serviceName": service,
            "status": "ACTIVE",
            "desiredCount": 2,
            "runningCount": running,
            "pendingCount": pending,
            "launchType": "FARGATE",
            "taskDefinition": active[0]["taskDefinition"],
            "platformVersion": "LATEST",
            "deployments": [dict(d, runningCount=running, pendingCount=pending) for d in active],
        }

        events = self._service_events(healthy)
        return ServiceHealth.from_aws(cluster, service, raw, events=events)

    def _service_events(self, healthy: bool) -> list[dict[str, Any]]:
        if healthy:
            return [
                {
                    "id": "sim-4",
                    "createdAt": _iso(datetime.now(timezone.utc) - timedelta(minutes=2)),
                    "message": (
                        f"service {SERVICE} deployment ecs-svc/5501123344 "
                        "reach a steady state."
                    ),
                },
                {
                    "id": "sim-3",
                    "createdAt": _iso(datetime.now(timezone.utc) - timedelta(minutes=3)),
                    "message": f"service {SERVICE} has started 2 tasks.",
                },
            ]
        return [
            {
                "id": "sim-2",
                "createdAt": _iso(datetime.now(timezone.utc) - timedelta(minutes=_ROLLOUT_STUCK_MIN)),
                "message": (
                    f"service {SERVICE} deployment ecs-svc/9182736450 was not able to "
                    "come up due to errors. The deployment circuit breaker was triggered."
                ),
            },
            {
                "id": "sim-1",
                "createdAt": _iso(
                    datetime.now(timezone.utc)
                    - timedelta(minutes=_ROLLOUT_STUCK_MIN - 4)
                ),
                "message": (
                    f"service {SERVICE}: task has started, but is not reporting healthy."
                ),
            },
        ]

    def force_new_deployment(self, cluster: str, service: str) -> dict[str, Any]:
        self._sync()
        if not self._config.allow_remediation:
            raise RemediationNotPermitted(
                "Remediation is disabled. Set TICKET_RESOLVER_ALLOW_REMEDIATION=true "
                "to enable it."
            )
        if not cluster or not service:
            raise ValueError("cluster and service are both required")
        self.describe_ecs_service(cluster, service)  # validates existence

        now = datetime.now(timezone.utc)
        new_id = f"ecs-svc/{random.Random(int(now.timestamp())).randint(10**9, 10**10 - 1)}"

        # The new deployment supersedes the stuck one and restores capacity.
        self._state["deployments"]["fixed"] = {
            "id": new_id,
            "status": "PRIMARY",
            "rolloutState": "COMPLETED",
            "rolloutStateReason": None,
            "desiredCount": 2,
            "runningCount": 2,
            "pendingCount": 0,
            "createdAt": _iso(now),
            "updatedAt": _iso(now),
            "taskDefinition": TASK_DEF_FIXED,
        }
        self._state["deployments"]["good"]["status"] = "ACTIVE"
        self._state["remediatedAt"] = _iso(now)
        self._state["remediationCount"] = int(self._state.get("remediationCount", 0)) + 1
        self._save(self._state)

        return {
            "backend": self.name,
            "cluster": cluster,
            "service": service,
            "action": "forceNewDeployment",
            "disruptive": True,
            "acceptedAt": _iso(now),
            "previousTaskDefinition": TASK_DEF_BAD,
            "currentTaskDefinition": TASK_DEF_FIXED,
            "deploymentId": new_id,
            "desiredCount": 2,
            "runningCount": 2,
            "note": (
                "ECS accepted the request. This does NOT mean the incident is "
                "resolved - verify service health, rollout state, and error rate."
            ),
        }

    # -- CloudWatch Logs --------------------------------------------------
    def list_log_groups(self, prefix: str = "") -> list[str]:
        self._sync()
        groups = [LOG_GROUP, "/ecs/order-api", "/ecs/notification-worker"]
        return [g for g in groups if g.startswith(prefix)] if prefix else groups

    def filter_log_events(
        self,
        log_group: str,
        minutes: int,
        limit: int,
        filter_pattern: str | None = None,
    ) -> list[LogEvent]:
        self._sync()
        if not log_group:
            raise ValueError("log_group is required")
        if log_group not in self.list_log_groups():
            raise ResourceNotFound(
                f"Log group {log_group!r} does not exist. "
                f"Available: {', '.join(self.list_log_groups())}"
            )

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=max(1, minutes))
        events = self._build_timeline(now, window_start)
        events.sort(key=lambda e: e.timestamp or "", reverse=True)
        events = events[: max(1, int(limit))]

        if filter_pattern:
            events = [e for e in events if _matches(e.message, filter_pattern)]
        return events

    def _build_timeline(self, now: datetime, window_start: datetime) -> list[LogEvent]:
        """Generate the log timeline for the requested window.

        The grid is aligned to whole minutes so log events and metric buckets
        share boundaries. Without that, a sub-second offset in the stored
        incident start would let a stray post-remediation error slip through.
        """
        events: list[LogEvent] = []
        incident = self._minute(self._incident_start)
        remediated = self._minute(self._remediated_at) if self._remediated_at else None

        # Deterministic jitter so repeated calls are stable within a run.
        rng = random.Random(4242)
        stream = f"{SERVICE}/{self._state.get('createdAt', '')}/payments"

        def add(when: datetime, message: str) -> None:
            if when < window_start or when > now:
                return
            # Derive the level from the message itself so the simulated stream
            # is self-consistent with what filter patterns match against.
            level = "INFO"
            for candidate in ("ERROR", "WARN", "INFO"):
                if message.startswith(candidate):
                    level = candidate
                    break
            events.append(
                LogEvent(
                    timestamp=_iso(when),
                    message=f"[{level}] {message}",
                    log_stream=stream,
                    ingestion_time=_iso(when + timedelta(seconds=1)),
                )
            )

        # --- healthy baseline, leading up to the bad deploy ---------------
        cursor = self._minute(window_start)
        while cursor < incident:
            add(
                cursor,
                rng.choice(_HEALTHY_PATHS) + f" requestId={rng.randrange(10**6, 10**7)}",
            )
            cursor += timedelta(minutes=2)

        # --- incident window (strictly before remediation) -----------------
        incident_end = remediated if remediated is not None else self._minute(now)
        cursor = incident
        step = 0
        while cursor < incident_end:
            add(
                cursor,
                _ERROR_CYCLE[step % len(_ERROR_CYCLE)]
                + f" requestId={rng.randrange(10**6, 10**7)}",
            )
            cursor += timedelta(minutes=1)
            step += 1

        # --- post-remediation recovery ------------------------------------
        if remediated is not None and remediated >= self._minute(window_start):
            exact = self._remediated_at or remediated
            for offset, line in enumerate(_RECOVERY_LINES):
                add(exact + timedelta(seconds=20 * offset), line)
            tail = self._minute(exact) + timedelta(minutes=1)
            while tail <= now:
                add(
                    tail,
                    rng.choice(_HEALTHY_PATHS) + f" requestId={rng.randrange(10**6, 10**7)}",
                )
                tail += timedelta(minutes=2)

        return events

    # -- CloudWatch Metrics ----------------------------------------------
    def get_metric_series(
        self,
        namespace: str,
        metric_name: str,
        dimensions: dict[str, str],
        minutes: int,
        period: int = 60,
        stat: str = "Average",
    ) -> MetricSeries:
        self._sync()
        if not namespace or not metric_name:
            raise ValueError("namespace and metric_name are required")

        profiles = self._metric_profiles()
        key = (namespace, metric_name)
        if key not in profiles:
            raise ResourceNotFound(
                f"Metric {namespace}/{metric_name} does not exist in the simulated "
                f"account. Available: "
                + ", ".join(f"{n}/{m}" for n, m in sorted(profiles))
            )

        unit, healthy_value, broken_value = profiles[key]

        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=max(1, minutes))
        points: list[MetricPoint] = []

        cursor = start.replace(second=0, microsecond=0)
        while cursor <= now:
            healthy = self._is_healthy(cursor)
            base = healthy_value if healthy else broken_value
            # Stable wobble (crc32, not hash()) so repeated calls - and
            # repeated server restarts - return identical telemetry.
            jitter = (zlib.crc32(f"{metric_name}:{cursor.isoformat()}".encode()) % 11 - 5) / 100.0
            value = round(base * (1 + jitter), 4)
            points.append(
                MetricPoint(
                    timestamp=_iso(cursor),
                    average=value,
                    maximum=round(value * 1.12, 4),
                    minimum=round(value * 0.9, 4),
                    sample_count=1,
                    unit=unit,
                )
            )
            cursor += timedelta(minutes=max(1, period // 60))

        return MetricSeries(
            namespace=namespace,
            metric_name=metric_name,
            dimensions=dimensions or {},
            unit=unit,
            points=points,
        )

    @staticmethod
    def _metric_profiles() -> dict[tuple[str, str], tuple[str, float, float]]:
        """metric -> (unit, healthy value, broken value)."""
        return {
            ("AWS/ECS", "CPUUtilization"): ("Percent", 31.0, 87.5),
            ("AWS/ECS", "MemoryUtilization"): ("Percent", 48.0, 63.0),
            (APP_NAMESPACE, "RequestCount"): ("Count", 120.0, 118.0),
            (APP_NAMESPACE, "ErrorCount5xx"): ("Count", 0.0, 96.0),
            (APP_NAMESPACE, "LatencyP95"): ("Milliseconds", 210.0, 1180.0),
        }


def _matches(message: str, pattern: str) -> bool:
    """Very small subset of CloudWatch filter patterns.

    Supports plain substrings plus ``ERROR``/``WARN``/``INFO`` level matching,
    which covers what the agent actually needs without pulling in a regex
    engine with different semantics to CloudWatch's.
    """
    p = pattern.strip()
    if p.upper() in {"ERROR", "WARN", "WARNING", "INFO"}:
        return f"[{p.upper().replace('WARNING', 'WARN')}]" in message
    if p.startswith("?ERROR") or p.startswith("?WARN") or p.startswith("?INFO"):
        level = p[1:].upper().replace("WARNING", "WARN")
        if f"[{level}]" not in message:
            return False
        p = p[1 + len(level):]
    return p.lower() in message.lower() if p else True
