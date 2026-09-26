"""Real AWS backend built on boto3.

Credentials are never hardcoded or read from config: clients are built with no
explicit keys, so boto3's default credential chain applies (env vars, shared
config/credentials files, SSO, instance profile, ECS task role, ...).

Clients are created lazily and cached per-region, because constructing a
boto3 client requires a credential-chain lookup that should not happen at
import time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from backends.base import AWSBackend, ResourceNotFound
from models import Deployment, LogEvent, MetricSeries, ServiceHealth

log = logging.getLogger("ticket-resolver.boto3")

# Short, bounded timeouts so a slow AWS API call cannot wedge the MCP server.
_CLIENT_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 3, "mode": "standard"},
)

# Events are the single most useful signal for "why is this service degraded".
_MAX_EVENTS = 10


class Boto3Backend(AWSBackend):
    name = "boto3"

    def __init__(self, config) -> None:
        self._config = config
        self._region = config.region
        self._clients: dict[str, Any] = {}

    # -- plumbing ---------------------------------------------------------
    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = boto3.client(
                service,
                region_name=self._region,
                config=_CLIENT_CONFIG,
                # No hardcoded credentials: default chain only.
            )
        return self._clients[service]

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "region": self._region,
            "source": "real AWS via boto3 default credential chain",
        }

    def healthcheck(self) -> tuple[bool, str]:
        try:
            identity = self.client("sts").get_caller_identity()
        except Exception as exc:  # noqa: BLE001 - never crash the server
            return False, f"{type(exc).__name__}: {exc}"
        return True, (
            f"Authenticated as {identity.get('Arn', 'unknown')} "
            f"in account {identity.get('Account', 'unknown')}"
        )

    # -- ECS --------------------------------------------------------------
    def list_ecs_services(self, cluster: str) -> list[str]:
        if not cluster:
            raise ValueError("cluster is required")

        try:
            paginator = self.client("ecs").get_paginator("list_services")
            names: list[str] = []
            for page in paginator.paginate(cluster=cluster):
                names.extend(page.get("serviceArns", []) or [])
        except ClientError as exc:
            raise self._translate(exc, f"cluster {cluster!r}") from exc

        # list_services returns ARNs; agents and runbooks think in names.
        return [arn.rsplit("/", 1)[-1] for arn in names]

    def describe_ecs_service(self, cluster: str, service: str) -> ServiceHealth:
        if not cluster or not service:
            raise ValueError("cluster and service are both required")

        ecs = self.client("ecs")
        try:
            response = ecs.describe_services(cluster=cluster, services=[service])
        except ClientError as exc:
            raise self._translate(exc, f"service {service!r} in cluster {cluster!r}") from exc

        services = response.get("services") or []
        if not services:
            # ECS returns 200 + empty list for a name that does not exist.
            raise ResourceNotFound(
                f"No ECS service named {service!r} exists in cluster {cluster!r}."
            )

        failures = response.get("failures") or []
        if failures:
            raise ResourceNotFound(
                f"ECS could not describe {service!r} in {cluster!r}: "
                + "; ".join(
                    f"{f.get('arn', 'service')} - {f.get('reason', 'unknown reason')}"
                    for f in failures
                )
            )

        events = [
            {
                "id": e.get("id"),
                "createdAt": e.get("createdAt").isoformat()
                if isinstance(e.get("createdAt"), datetime)
                else e.get("createdAt"),
                "message": e.get("message"),
            }
            for e in (services[0].get("events") or [])[:_MAX_EVENTS]
        ]

        return ServiceHealth.from_aws(
            cluster=cluster, service_name=service, raw=services[0], events=events
        )

    def force_new_deployment(self, cluster: str, service: str) -> dict[str, Any]:
        if not cluster or not service:
            raise ValueError("cluster and service are both required")

        # Confirm the target exists first so a typo produces a clear message
        # rather than an opaque ECS exception.
        before = self.describe_ecs_service(cluster, service)
        try:
            response = self.client("ecs").update_service(
                cluster=cluster,
                service=service,
                forceNewDeployment=True,
            )
        except ClientError as exc:
            raise self._translate(
                exc, f"service {service!r} in cluster {cluster!r}"
            ) from exc

        service_result = response.get("service") or {}
        new_deployment = next(
            (d for d in service_result.get("deployments", []) if d.get("status") == "PRIMARY"),
            None,
        )

        return {
            "backend": self.name,
            "cluster": cluster,
            "service": service,
            "action": "forceNewDeployment",
            "disruptive": True,
            "acceptedAt": datetime.now(timezone.utc).isoformat(),
            # Deployment model does not carry task_definition; use the
            # service-level task definition captured before remediation.
            "previousTaskDefinition": before.task_definition,
            "currentTaskDefinition": service_result.get("taskDefinition"),
            "deploymentId": (new_deployment or {}).get("id"),
            "desiredCount": service_result.get("desiredCount"),
            "runningCount": service_result.get("runningCount"),
            "note": (
                "ECS accepted the request. This does NOT mean the incident is "
                "resolved - verify service health, rollout state, and error rate."
            ),
        }

    # -- CloudWatch Logs --------------------------------------------------
    def list_log_groups(self, prefix: str = "") -> list[str]:
        names: list[str] = []
        kwargs: dict[str, Any] = {}
        if prefix:
            kwargs["logGroupNamePrefix"] = prefix

        try:
            paginator = self.client("logs").get_paginator("describe_log_groups")
            for page in paginator.paginate(**kwargs):
                for group in page.get("logGroups", []) or []:
                    name = group.get("logGroupName")
                    if name:
                        names.append(name)
        except ClientError as exc:
            raise self._translate(exc, f"log groups with prefix {prefix!r}") from exc
        return names

    def filter_log_events(
        self,
        log_group: str,
        minutes: int,
        limit: int,
        filter_pattern: str | None = None,
    ) -> list[LogEvent]:
        if not log_group:
            raise ValueError("log_group is required")

        logs = self.client("logs")
        start = datetime.now(timezone.utc) - timedelta(minutes=max(1, minutes))

        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": int(start.timestamp() * 1000),
            "limit": max(1, min(int(limit), 10_000)),
        }
        if filter_pattern:
            kwargs["filterPattern"] = filter_pattern

        try:
            response = logs.filter_log_events(**kwargs)
        except ClientError as exc:
            raise self._translate(exc, f"log group {log_group!r}") from exc

        if not (response.get("events") or response.get("searchedLogStreams")):
            # Distinguish "group does not exist" from "group has no matching
            # events", which mean very different things to an agent.
            try:
                logs.describe_log_groups(logGroupNamePrefix=log_group)
                exists = any(
                    g.get("logGroupName") == log_group
                    for g in logs.describe_log_groups(logGroupNamePrefix=log_group).get(
                        "logGroups", []
                    )
                )
            except ClientError:
                exists = False
            if not exists:
                raise ResourceNotFound(
                    f"Log group {log_group!r} does not exist. "
                    "Use list_log_groups to discover valid names."
                )

        events = [LogEvent.from_aws(e) for e in response.get("events", []) or []]
        # CloudWatch returns oldest-first; agents want the newest events.
        events.sort(key=lambda e: e.timestamp or "", reverse=True)
        return events[: max(1, int(limit))]

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
        if not namespace or not metric_name:
            raise ValueError("namespace and metric_name are required")

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=max(1, minutes))
        dimension_list = [{"Name": k, "Value": v} for k, v in (dimensions or {}).items()]

        try:
            response = self.client("cloudwatch").get_metric_data(
                MetricDataQueries=[
                    {
                        "Id": "m0",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": namespace,
                                "MetricName": metric_name,
                                "Dimensions": dimension_list,
                            },
                            "Period": max(60, int(period)),
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    }
                ],
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampDescending",
            )
        except ClientError as exc:
            raise self._translate(
                exc, f"metric {namespace}/{metric_name}"
            ) from exc

        results = response.get("MetricDataResults") or []
        if not results:
            raise ResourceNotFound(
                f"No data returned for {namespace}/{metric_name} with dimensions "
                f"{dimensions or '{}'} over the last {minutes} minutes. The metric "
                "may not exist for this resource or the window may be too small."
            )

        result = results[0]
        from models import MetricPoint

        # get_metric_data returns timestamps and values as parallel arrays,
        # unlike get_metric_statistics which returns datapoint dicts.
        timestamps = result.get("Timestamps", []) or []
        values = result.get("Values", []) or []
        points: list[MetricPoint] = []
        for ts, val in zip(timestamps, values):
            points.append(
                MetricPoint(
                    timestamp=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    average=float(val) if val is not None else None,
                    maximum=float(val) if val is not None else None,
                    minimum=float(val) if val is not None else None,
                    sample_count=1,
                    unit=result.get("Unit", "None"),
                )
            )
        points.sort(key=lambda p: p.timestamp or "")

        return MetricSeries(
            namespace=namespace,
            metric_name=metric_name,
            dimensions=dimensions or {},
            unit=result.get("Unit", "None"),
            points=points,
        )

    # -- DynamoDB ---------------------------------------------------------
    def describe_dynamodb_table(self, table_name: str) -> dict[str, Any]:
        if not table_name:
            raise ValueError("table_name is required")
        dynamodb = self.client("dynamodb")
        try:
            response = dynamodb.describe_table(TableName=table_name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                raise ResourceNotFound(
                    f"DynamoDB table {table_name!r} not found."
                ) from exc
            raise self._translate(exc, f"describe DynamoDB table {table_name!r}") from exc
        table = response.get("Table", {})
        return {
            "tableName": table_name,
            "tableStatus": table.get("TableStatus", "UNKNOWN"),
            "provisionedReadCapacity": table.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0),
            "provisionedWriteCapacity": table.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0),
        }

    def update_dynamodb_table(self, table_name: str, read_capacity: int, write_capacity: int) -> dict[str, Any]:
        if not table_name:
            raise ValueError("table_name is required")
        if read_capacity < 1 or write_capacity < 1:
            raise ValueError("read_capacity and write_capacity must be positive integers.")

        dynamodb = self.client("dynamodb")
        try:
            response = dynamodb.update_table(
                TableName=table_name,
                ProvisionedThroughput={
                    "ReadCapacityUnits": read_capacity,
                    "WriteCapacityUnits": write_capacity,
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                raise ResourceNotFound(
                    f"DynamoDB table {table_name!r} not found."
                ) from exc
            raise self._translate(exc, f"update DynamoDB table {table_name!r}") from exc

        table = response.get("Table", {})
        return {
            "backend": self.name,
            "table": table_name,
            "action": "update_dynamodb_table",
            "disruptive": True,
            "acceptedAt": datetime.now(timezone.utc).isoformat(),
            "newReadCapacity": read_capacity,
            "newWriteCapacity": write_capacity,
            "tableStatus": table.get("TableStatus", "UNKNOWN"),
            "note": (
                "DynamoDB accepted the capacity update. This does NOT mean "
                "the incident is resolved - verify ThrottledRequests and "
                "application error rate."
            ),
        }

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _translate(exc: ClientError, target: str) -> Exception:
        """Turn AWS 'not found' errors into ResourceNotFound, keep others real."""
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if code in {
            "ClusterNotFoundException",
            "ServiceNotFoundException",
            "ResourceNotFoundException",
            "ResourceNotFound",
            "InvalidParameterException",
        }:
            return ResourceNotFound(message or f"{target} not found.")
        log.warning("AWS call failed for %s: %s - %s", target, code, message)
        return exc


__all__ = ["Boto3Backend", "Deployment"]
