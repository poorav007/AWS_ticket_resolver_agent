#!/usr/bin/env python3
"""Ticket Resolver MCP server.

Bridges the Ticket Resolver agent to AWS through a narrow, explicitly-scoped
tool surface. Design rules enforced here:

* Read-only investigation tools are annotated ``read_only_hint=True``.
* Any state-changing tool is annotated ``destructive_hint=True`` and refuses to
  run without a matching, explicitly-approved proposal. The approval gate lives
  in this process as well as in the agent harness, so a misbehaving model
  cannot reach a disruptive AWS call on its own.
* Every tool returns a ``{"success": bool, ...}`` envelope. A missing AWS
  resource is a result, not a crash.
* Credentials are never accepted, logged, or returned by any tool.

Run locally for OpenCode:
    .venv/bin/python mcp-server/server.py

Run as a remote streamable-HTTP server for TrueForge:
    TICKET_RESOLVER_TRANSPORT=streamable-http TICKET_RESOLVER_PORT=8080 \\
        .venv/bin/python mcp-server/server.py
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make sibling modules importable regardless of the caller's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from backends import get_backend  # noqa: E402
from backends.base import RemediationNotPermitted, ResourceNotFound  # noqa: E402
from config import CONFIG  # noqa: E402
from errors import fail, ok, tool_error  # noqa: E402
from runbooks import RunbookLibrary  # noqa: E402
from tickets import TicketStore  # noqa: E402

# stdout is reserved for the MCP stdio protocol - logs must go to stderr.
logging.basicConfig(
    level=os.environ.get("TICKET_RESOLVER_LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ticket-resolver")

# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
_tickets = TicketStore(CONFIG.tickets_dir)
_runbooks = RunbookLibrary(CONFIG.runbooks_dir)
_backend = get_backend(CONFIG)

APP_NAMESPACE = "TicketResolver/App"
ECS_NAMESPACE = "AWS/ECS"
DYNAMODB_NAMESPACE = "AWS/DynamoDB"

# Catalog of metric shortcuts so the agent can ask for "error rate" without
# memorising CloudWatch namespace/metric/dimension triples.
METRIC_CATALOG: dict[str, dict[str, Any]] = {
    "cpu": {
        "namespace": ECS_NAMESPACE,
        "metric": "CPUUtilization",
        "unit": "Percent",
        "dimensions": lambda cluster, service: {
            "ClusterName": cluster,
            "ServiceName": service,
        },
    },
    "memory": {
        "namespace": ECS_NAMESPACE,
        "metric": "MemoryUtilization",
        "unit": "Percent",
        "dimensions": lambda cluster, service: {
            "ClusterName": cluster,
            "ServiceName": service,
        },
    },
    "errors": {
        "namespace": APP_NAMESPACE,
        "metric": "ErrorCount5xx",
        "unit": "Count",
        "dimensions": lambda cluster, service: {"ServiceName": service},
    },
    "requests": {
        "namespace": APP_NAMESPACE,
        "metric": "RequestCount",
        "unit": "Count",
        "dimensions": lambda cluster, service: {"ServiceName": service},
    },
    "latency": {
        "namespace": APP_NAMESPACE,
        "metric": "LatencyP95",
        "unit": "Milliseconds",
        "dimensions": lambda cluster, service: {"ServiceName": service},
    },
    "dynamodb_throttled": {
        "namespace": DYNAMODB_NAMESPACE,
        "metric": "ThrottledRequests",
        "unit": "Count",
        "dimensions": lambda cluster, service: {"TableName": service},
    },
}

SERVER_INSTRUCTIONS = """\
Ticket Resolver investigates and resolves incidents on AWS ECS.

Investigation order:
  get_ticket -> get_ecs_service_health -> get_ecs_deployments ->
  get_recent_logs -> get_cloudwatch_metrics -> get_runbook

Ground every conclusion in tool output. Separate observed facts from
hypotheses, and say so when evidence is insufficient.

Remediation is a separate, approval-gated phase:
  1. Call propose_remediation and present the plan to the human.
  2. Stop and wait for explicit human approval.
  3. Only after approval, call execute_remediation with the proposal id and
     the approver's identity.
  4. Then call verify_service. A successful AWS API call is NOT resolution.

Never call execute_remediation without a human-approved proposal.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Proposal store (the approval gate's state)
# --------------------------------------------------------------------------
def _proposal_path() -> Path:
    CONFIG.ensure_state_dir()
    return CONFIG.state_dir / "remediation-proposals.json"


def _load_proposals() -> dict[str, Any]:
    path = _proposal_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Proposal store unreadable, refusing to run remediation: %s", exc)
        return {}


def _save_proposals(store: dict[str, Any]) -> None:
    path = _proposal_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(path)


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------
server = MCPServer(
    name="ticket-resolver-aws",
    version="0.1.0",
    instructions=SERVER_INSTRUCTIONS,
)

READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "open_world_hint": True}
WRITE = {"read_only_hint": False, "destructive_hint": True, "idempotent_hint": False}
LOCAL_WRITE = {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": True}


# ==========================================================================
# Investigation tools (read-only)
# ==========================================================================
@server.tool(
    name="describe_backend",
    title="Describe the active AWS backend",
    annotations=ToolAnnotations(**READ_ONLY),
)
def describe_backend() -> dict[str, Any]:
    """Report which AWS backend is active.

    Call this before presenting evidence. If the backend is ``simulated`` the
    findings are synthetic and must be labelled as such - never present
    simulated output as real production evidence.
    """
    reachable, message = _backend.healthcheck()
    info = _backend.describe()
    return ok({**info, "reachable": reachable, "healthMessage": message})


@server.tool(
    name="get_ticket",
    title="Get incident ticket",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_ticket(ticket_id: str) -> dict[str, Any]:
    """Fetch an incident ticket by id, for example ``INC-1001``.

    Returns the title, severity, affected service and cluster, the reported
    symptoms, and the current ticket status.
    """
    if not ticket_id:
        return fail("Missing ticket_id", "Call get_ticket with a ticket id such as INC-1001.")
    try:
        ticket = _tickets.get(ticket_id)
    except FileNotFoundError as exc:
        return fail("Ticket not found", str(exc))
    except ValueError as exc:
        return fail("Invalid ticket", str(exc))
    except OSError as exc:
        return tool_error(exc, f"reading ticket {ticket_id!r}")

    payload = ticket.to_dict()
    payload["availableTickets"] = _tickets.list_ids()
    return ok(payload)


@server.tool(
    name="list_tickets",
    title="List incident tickets",
    annotations=ToolAnnotations(**READ_ONLY),
)
def list_tickets() -> dict[str, Any]:
    """List all ticket ids available in the local ticket store."""
    return ok({"tickets": _tickets.list_ids()})


@server.tool(
    name="get_ecs_services",
    title="List ECS services in a cluster",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_ecs_services(cluster: str) -> dict[str, Any]:
    """List the ECS service names running in ``cluster``.

    Use this to confirm a cluster exists and to discover the exact service
    names available before investigating one of them.
    """
    if not cluster:
        return fail("Missing cluster", "Provide the ECS cluster name, e.g. hackathon-cluster.")
    try:
        services = _backend.list_ecs_services(cluster)
    except ResourceNotFound as exc:
        return fail("Cluster not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"listing services in {cluster!r}")

    return ok(
        {
            "cluster": cluster,
            "services": services,
            "count": len(services),
            "empty": not services,
            "note": None if services else "Cluster exists but has no ECS services.",
        }
    )


@server.tool(
    name="get_ecs_service_health",
    title="Get ECS service health",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_ecs_service_health(
    cluster: str, service: str, include_events: bool = True
) -> dict[str, Any]:
    """Get a health snapshot for one ECS service.

    Reports status, desired/running/pending task counts, the service's
    deployments with their rollout state, recent service events, and a list of
    human-readable ``issues`` explaining any degradation.
    """
    if not cluster or not service:
        return fail(
            "Missing input",
            "Both cluster and service are required, e.g. cluster=hackathon-cluster, service=payment-api.",
        )
    try:
        health = _backend.describe_ecs_service(cluster, service)
    except ResourceNotFound as exc:
        return fail("Service not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"describing {service!r} in {cluster!r}")

    payload = health.to_dict()
    if not include_events:
        payload.pop("events", None)

    # Surface a clear degraded/healthy verdict so the agent does not have to
    # infer it from counts.
    payload["verdict"] = "HEALTHY" if health.stable and not health.issues else "DEGRADED"
    payload["investigatedAt"] = _iso(_now())
    return ok(payload)


@server.tool(
    name="get_ecs_deployments",
    title="Get recent ECS deployments",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_ecs_deployments(cluster: str, service: str) -> dict[str, Any]:
    """List recent deployments for an ECS service, newest first.

    Compare each deployment's ``createdAt`` against the incident start time. A
    deployment created shortly before symptoms began is the prime suspect for
    a regression.
    """
    if not cluster or not service:
        return fail("Missing input", "Both cluster and service are required.")
    try:
        health = _backend.describe_ecs_service(cluster, service)
    except ResourceNotFound as exc:
        return fail("Service not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"listing deployments for {service!r}")

    deployments = sorted(
        health.deployments, key=lambda d: d.created_at or "", reverse=True
    )
    primary = next((d for d in deployments if d.status == "PRIMARY"), None)

    return ok(
        {
            "cluster": cluster,
            "service": service,
            "deployments": [d.to_dict() for d in deployments],
            "count": len(deployments),
            "activeDeployment": primary.to_dict() if primary else None,
            "rolloutComplete": bool(
                primary and (primary.rollout_state or "").upper() == "COMPLETED"
            ),
            "rolloutBlocked": bool(
                primary
                and (primary.rollout_state or "").upper() in {"FAILED", "IN_PROGRESS"}
            ),
            "taskDefinition": health.task_definition,
        }
    )


@server.tool(
    name="list_log_groups",
    title="List CloudWatch log groups",
    annotations=ToolAnnotations(**READ_ONLY),
)
def list_log_groups(prefix: str = "") -> dict[str, Any]:
    """List CloudWatch log groups, optionally filtered by ``prefix``.

    Use this to discover the correct log group name before calling
    ``get_recent_logs``.
    """
    try:
        groups = _backend.list_log_groups(prefix)
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, "listing log groups")
    return ok({"prefix": prefix, "logGroups": groups, "count": len(groups)})


@server.tool(
    name="get_recent_logs",
    title="Get recent CloudWatch logs",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_recent_logs(
    log_group: str,
    minutes: int = 30,
    limit: int = 50,
    filter_pattern: str | None = None,
) -> dict[str, Any]:
    """Fetch recent log events from a CloudWatch log group, newest first.

    Args:
        log_group: Log group name, e.g. ``/ecs/payment-api``.
        minutes: How far back to look.
        limit: Maximum events to return (hard-capped to protect agent context).
        filter_pattern: Optional filter. ``ERROR``, ``WARN``, ``INFO``, or a
            substring such as ``"ConfigError"``.

    A short window plus a narrow filter is the most token-efficient way to
    gather evidence: prefer ``minutes=15, filter_pattern="ERROR"`` over
    dumping the whole window.
    """
    if not log_group:
        return fail("Missing log_group", "Provide a log group name, e.g. /ecs/payment-api.")
    if minutes < 1 or minutes > 1440:
        return fail("Invalid minutes", "minutes must be between 1 and 1440.")
    capped = max(1, min(int(limit), CONFIG.max_log_events))
    if int(limit) > CONFIG.max_log_events:
        log.info("get_recent_logs limit clamped from %s to %s", limit, CONFIG.max_log_events)

    try:
        events = _backend.filter_log_events(
            log_group=log_group,
            minutes=int(minutes),
            limit=capped,
            filter_pattern=filter_pattern or None,
        )
    except ResourceNotFound as exc:
        return fail("Log group not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"reading logs from {log_group!r}")

    error_count = sum(1 for e in events if e.message.startswith("[ERROR]"))
    return ok(
        {
            "logGroup": log_group,
            "windowMinutes": int(minutes),
            "filterPattern": filter_pattern,
            "returned": len(events),
            "truncated": len(events) >= capped,
            "errorEvents": error_count,
            "events": [e.to_dict() for e in events],
        }
    )


@server.tool(
    name="get_cloudwatch_metrics",
    title="Get CloudWatch metrics",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_cloudwatch_metrics(
    metric: str = "errors",
    cluster: str = "",
    service: str = "",
    minutes: int = 30,
    max_points: int = 20,
) -> dict[str, Any]:
    """Fetch a CloudWatch metric time series for an ECS service.

    Args:
        metric: One of the shortcuts ``cpu``, ``memory``, ``errors``,
            ``requests``, ``latency``.
        cluster: ECS cluster name (required for ``cpu``/``memory``).
        service: ECS service name.
        minutes: Window length, 1-1440.
        max_points: Cap on returned data points. The series is down-sampled
            (keeping the newest point and the peak) so a long window does not
            flood your context. Set 0 to return every point. The ``latest``,
            ``peak`` and ``first`` summaries always reflect the full window.

    Returns the scalar summary (``latest``, ``peak``, ``first``) plus a
    down-sampled point list. Read the summary fields for reasoning; use the
    points only when you need the shape of the curve.
    """
    if minutes < 1 or minutes > 1440:
        return fail("Invalid minutes", "minutes must be between 1 and 1440.")

    key = (metric or "").strip().lower()
    if key not in METRIC_CATALOG:
        return fail(
            "Unknown metric",
            f"metric must be one of: {', '.join(sorted(METRIC_CATALOG))}.",
            availableMetrics=sorted(METRIC_CATALOG),
        )

    spec = METRIC_CATALOG[key]
    if not service:
        return fail(
            "Missing service",
            f"The {key!r} metric requires a service name, e.g. service=payment-api.",
        )
    if key in {"cpu", "memory"} and not cluster:
        cluster = cluster or CONFIG.default_cluster

    dimensions = spec["dimensions"](cluster, service)
    try:
        series = _backend.get_metric_series(
            namespace=spec["namespace"],
            metric_name=spec["metric"],
            dimensions=dimensions,
            minutes=int(minutes),
        )
    except ResourceNotFound as exc:
        return fail("Metric data not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"fetching {key!r} metric for {service!r}")

    payload = series.to_dict(max_points=int(max_points) or None)
    payload["metricShortcut"] = key
    return ok(payload)


@server.tool(
    name="get_runbook",
    title="Get a runbook",
    annotations=ToolAnnotations(**READ_ONLY),
)
def get_runbook(
    slug: str = "", ticket_id: str = "", include_content: bool = True
) -> dict[str, Any]:
    """Fetch runbooks for a known issue type.

    Provide ``slug`` (e.g. ``api-500``) for a specific runbook, or ``ticket_id``
    to get the runbooks that best match that ticket's symptoms. Ground the
    diagnosis in the returned runbook rather than free-form reasoning.
    """
    try:
        if slug:
            book = _runbooks.get(slug)
            return ok(book.to_dict(include_body=include_content))

        if ticket_id:
            ticket = _tickets.get(ticket_id)
            matches = _runbooks.search(
                text=f"{ticket.title} {ticket.description}",
                symptoms=ticket.symptoms,
                limit=3,
            )
            if ticket.runbook:
                try:  # ensure the ticket's declared runbook is included
                    declared = _runbooks.get(ticket.runbook)
                    if all(m.slug != declared.slug for m in matches):
                        matches.insert(0, declared)
                except KeyError:
                    log.warning("Ticket %s references unknown runbook %s", ticket_id, ticket.runbook)
            return ok(
                {
                    "ticketId": ticket.id,
                    "matches": [m.to_dict(include_body=include_content) for m in matches],
                    "count": len(matches),
                }
            )

        return fail(
            "Nothing requested",
            "Provide either slug=<runbook> or ticket_id=<id>.",
            availableRunbooks=_runbooks.list_slugs(),
        )
    except KeyError as exc:
        return fail("Runbook not found", str(exc).strip("'"))
    except FileNotFoundError as exc:
        return fail("Ticket not found", str(exc))
    except ValueError as exc:
        return fail("Invalid input", str(exc))
    except OSError as exc:
        return tool_error(exc, "reading runbooks")


# ==========================================================================
# Remediation tools (write / disruptive)
# ==========================================================================
@server.tool(
    name="propose_remediation",
    title="Propose a remediation for human approval",
    annotations=ToolAnnotations(**WRITE),
)
def propose_remediation(
    ticket_id: str,
    action: str,
    cluster: str,
    service: str,
    rationale: str,
    expected_impact: str = "",
    evidence: list[str] | None = None,
    table_name: str = "",
) -> dict[str, Any]:
    """Record a remediation proposal and return an approval token.

    This performs **no** AWS mutation. It creates a ``PENDING_APPROVAL`` record
    so that a later ``execute_remediation`` call has something explicit to match
    against.

    Present the rationale and expected impact to the human and wait for explicit
    approval before calling ``execute_remediation``.

    Args:
        ticket_id: Ticket this remediation belongs to.
        action: ``force_new_deployment`` (ECS) or ``update_dynamodb_table`` (DynamoDB).
        cluster: Target ECS cluster.
        service: Target ECS service (or DynamoDB table name when action is DynamoDB).
        rationale: Why this action will fix the incident. Cite evidence.
        expected_impact: What will be disrupted and for roughly how long.
        evidence: Verbatim supporting observations (log lines, metric values).
        table_name: Required for DynamoDB actions; the name of the table to update.
    """
    supported = {"force_new_deployment", "update_dynamodb_table"}
    if action not in supported:
        return fail(
            "Unsupported action",
            f"action must be one of: {', '.join(sorted(supported))}.",
            supportedActions=sorted(supported),
        )
    if not rationale or len(rationale.strip()) < 10:
        return fail(
            "Rationale required",
            "Provide a rationale of at least 10 characters citing the evidence "
            "that supports this action.",
        )
    if action == "update_dynamodb_table" and not table_name:
        return fail(
            "Table name required",
            "table_name is required when action is 'update_dynamodb_table'.",
        )

    try:
        ticket = _tickets.get(ticket_id)
    except (FileNotFoundError, ValueError) as exc:
        return fail("Ticket not found", str(exc))

    # Validate the target now so approval is never granted for a typo.
    try:
        if action == "update_dynamodb_table":
            _backend.describe_dynamodb_table(table_name)  # read-only probe
        else:
            _backend.describe_ecs_service(cluster, service)
    except ResourceNotFound as exc:
        return fail("Target not found", str(exc))
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc, f"validating target {service!r}")

    proposal_id = f"prop-{secrets.token_hex(6)}"
    store = _load_proposals()
    store[proposal_id] = {
        "proposalId": proposal_id,
        "ticketId": ticket.id,
        "action": action,
        "cluster": cluster,
        "service": service,
        "table_name": table_name,
        "rationale": rationale,
        "expectedImpact": expected_impact,
        "evidence": list(evidence or []),
        "status": "PENDING_APPROVAL",
        "createdAt": _iso(_now()),
        "approvedBy": None,
        "approvedAt": None,
        "executedAt": None,
        "result": None,
    }
    _save_proposals(store)

    try:
        _tickets.update_status(
            ticket.id, "AWAITING_APPROVAL",
            note=f"Remediation proposed: {action} ({proposal_id})",
        )
    except (OSError, ValueError) as exc:
        log.warning("Could not update ticket status: %s", exc)

    return ok(
        {
            **store[proposal_id],
            "requiresHumanApproval": True,
            "nextStep": (
                "Present this plan to the human and wait for explicit approval. "
                "Then call execute_remediation(proposal_id, approved_by)."
            ),
        }
    )


@server.tool(
    name="approve_remediation",
    title="Record explicit human approval for a remediation proposal",
    annotations=ToolAnnotations(**WRITE),
)
def approve_remediation(proposal_id: str, approved_by: str, note: str = "") -> dict[str, Any]:
    """Mark a proposal as HUMAN_APPROVED.

    This is a server-side approval checkpoint. ``execute_remediation`` refuses
    to run unless this approval has been recorded first.
    """
    if not proposal_id:
        return fail("Missing proposal_id", "Call propose_remediation first.")
    if not approved_by or not approved_by.strip():
        return fail(
            "Approval required",
            "approved_by must name the human approver. Never fabricate an approver.",
        )

    store = _load_proposals()
    proposal = store.get(proposal_id)
    if proposal is None:
        return fail("Proposal not found", f"No remediation proposal with id {proposal_id!r}.")

    if proposal.get("status") == "EXECUTED":
        return fail("Already executed", f"Proposal {proposal_id} has already been executed.")
    if proposal.get("status") == "APPROVED":
        return ok({**proposal, "alreadyApproved": True})
    if proposal.get("status") != "PENDING_APPROVAL":
        return fail(
            "Proposal not approvable",
            f"Proposal {proposal_id} has status {proposal.get('status')!r}; expected PENDING_APPROVAL.",
            proposal=proposal,
        )

    approved_at = _now().isoformat()
    proposal.update(
        {
            "status": "APPROVED",
            "approvedBy": approved_by.strip(),
            "approvedAt": approved_at,
            "approvalNote": note or None,
        }
    )
    store[proposal_id] = proposal
    _save_proposals(store)

    try:
        _tickets.update_status(
            proposal["ticketId"],
            "AWAITING_APPROVAL",
            note=f"Proposal {proposal_id} explicitly approved by {approved_by}",
        )
    except (OSError, ValueError) as exc:
        log.warning("Could not update ticket status: %s", exc)

    return ok(
        {
            **proposal,
            "nextStep": "Now call execute_remediation with the same proposal_id.",
        }
    )


@server.tool(
    name="execute_remediation",
    title="Execute an approved remediation (DISRUPTIVE)",
    annotations=ToolAnnotations(**WRITE),
)
def execute_remediation(proposal_id: str, approved_by: str) -> dict[str, Any]:
    """Execute a previously approved remediation. **This disrupts production.**

    Hard requirements, all enforced in this process:
      * ``proposal_id`` must reference an existing proposal.
      * The proposal must be ``PENDING_APPROVAL``.
      * ``approved_by`` must be a non-empty human identifier.
      * Remediation must be enabled server-side.

    A successful call means AWS *accepted* the request. It does **not** mean the
    incident is resolved - you must call ``verify_service`` afterwards.
    """
    if not CONFIG.allow_remediation:
        return fail(
            "Remediation disabled",
            "TICKET_RESOLVER_ALLOW_REMEDIATION is false on this server.",
        )
    if not proposal_id:
        return fail("Missing proposal_id", "Call propose_remediation first.")
    if not approved_by or not approved_by.strip():
        return fail(
            "Approval required",
            "approved_by must name the human who approved this action. "
            "Never fabricate an approver.",
        )

    store = _load_proposals()
    proposal = store.get(proposal_id)
    if proposal is None:
        return fail(
            "Proposal not found",
            f"No remediation proposal with id {proposal_id!r}. "
            "Call propose_remediation to create one.",
        )
    if proposal["status"] == "EXECUTED":
        return fail(
            "Already executed",
            f"Proposal {proposal_id} was already executed at {proposal.get('executedAt')}.",
            proposal=proposal,
        )
    if proposal["status"] == "PENDING_APPROVAL":
        return fail(
            "Not yet human-approved",
            "Call approve_remediation(proposal_id, approved_by) first. "
            "Execution is blocked until explicit approval is recorded server-side.",
            proposal=proposal,
        )
    if proposal["status"] != "APPROVED":
        return fail(
            "Proposal not approvable",
            f"Proposal {proposal_id} has status {proposal['status']!r}; "
            "expected APPROVED.",
            proposal=proposal,
        )

    recorded_approver = (proposal.get("approvedBy") or "").strip()
    if recorded_approver and approved_by.strip() != recorded_approver:
        return fail(
            "Approver mismatch",
            f"Proposal was approved by {recorded_approver!r}; execute_remediation must use the same approved_by.",
            proposal=proposal,
        )

    cluster, service = proposal["cluster"], proposal["service"]
    action = proposal["action"]
    table_name = proposal.get("table_name", "")

    try:
        if action == "update_dynamodb_table":
            read_cap = int(proposal.get("newReadCapacity", 100))
            write_cap = int(proposal.get("newWriteCapacity", 100))
            result = _backend.update_dynamodb_table(table_name, read_cap, write_cap)
        else:
            result = _backend.force_new_deployment(cluster, service)
    except RemediationNotPermitted as exc:
        return fail("Remediation not permitted", str(exc))
    except ResourceNotFound as exc:
        return fail("Target not found", str(exc))
    except Exception as exc:  # noqa: BLE001
        proposal["status"] = "FAILED"
        proposal["executedAt"] = _iso(_now())
        proposal["error"] = f"{type(exc).__name__}: {exc}"
        store[proposal_id] = proposal
        _save_proposals(store)
        return tool_error(exc, f"executing {action} on {service!r}")

    executed_at = _now()
    proposal.update(
        {
            "status": "EXECUTED",
            "approvedBy": approved_by.strip(),
            "approvedAt": executed_at.isoformat(),
            "executedAt": executed_at.isoformat(),
            "result": result,
        }
    )
    store[proposal_id] = proposal
    _save_proposals(store)

    try:
        _tickets.update_status(
            proposal["ticketId"], "REMEDIATING",
            note=f"Remediation {proposal_id} executed by {approved_by}",
        )
    except (OSError, ValueError) as exc:
        log.warning("Could not update ticket status: %s", exc)

    verify_tool = "verify_service"
    verify_target = {"cluster": cluster, "service": service, "since": result.get("acceptedAt")}
    if action == "update_dynamodb_table":
        verify_tool = "verify_dynamodb"
        verify_target = {"table": table_name, "since": result.get("acceptedAt")}

    return ok(
        {
            **proposal,
            "verificationRequired": True,
            "verifyWith": verify_tool,
            **verify_target,
            "warning": (
                "AWS accepted the request. The incident is NOT yet resolved. "
                "Call verify_service (or verify_dynamodb) and only report "
                "resolution if it verifies."
            ),
        }
    )


@server.tool(
    name="verify_service",
    title="Verify whether a remediation actually fixed the incident",
    annotations=ToolAnnotations(**READ_ONLY),
)
def verify_service(
    cluster: str,
    service: str,
    since: str = "",
    minutes: int = 10,
    max_error_count: int = 0,
    max_cpu_percent: float | None = None,
) -> dict[str, Any]:
    """Check multiple independent signals to decide whether the incident is over.

    This is deliberately *not* satisfied by the AWS API having accepted a
    remediation. It independently re-observes:

      1. ``tasksRunning``     - runningCount == desiredCount
      2. ``deploymentRolledOut`` - active deployment rolloutState == COMPLETED
      3. ``noNewErrors``      - zero ERROR log events *after* ``since``
      4. ``errorRateRecovered``  - ErrorCount5xx back to ~0 in recent buckets
      5. ``cpuWithinLimits``  - only checked when ``max_cpu_percent`` is given

    Args:
        cluster: ECS cluster name.
        service: ECS service name.
        since: ISO timestamp marking the start of the remediation. Defaults to
            the most recent executed proposal for this service, else
            ``now - minutes``.
        minutes: Window used for the metric check.
        max_error_count: Allowed ERROR events after ``since``. Default 0.
        max_cpu_percent: Optional CPU ceiling; omit to skip that check.

    Note: a recent-window log query still contains pre-remediation errors, which
    is why check 3 counts only errors *newer than* ``since``.
    """
    if not cluster or not service:
        return fail("Missing input", "Both cluster and service are required.")

    # -- resolve the remediation boundary --------------------------------
    boundary = _parse_iso(since)
    boundary_source = "caller"
    if boundary is None:
        store = _load_proposals()
        candidates = [
            p for p in store.values()
            if p.get("cluster") == cluster
            and p.get("service") == service
            and p.get("status") == "EXECUTED"
            and p.get("action") == "force_new_deployment"
        ]
        if candidates:
            latest = max(candidates, key=lambda p: p.get("executedAt") or "")
            boundary = _parse_iso(latest.get("executedAt"))
            boundary_source = f"proposal {latest.get('proposalId')}"
    if boundary is None:
        boundary = _now() - timedelta(minutes=minutes)
        boundary_source = f"fallback now-{minutes}m"

    checks: list[dict[str, Any]] = []

    # -- 1 & 2: ECS state ------------------------------------------------
    health = None
    try:
        health = _backend.describe_ecs_service(cluster, service)
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {
                "check": "serviceReachable",
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
    if health is not None:
        checks.append(
            {
                "check": "tasksRunning",
                "passed": health.running_count == health.desired_count
                and health.running_count > 0,
                "detail": f"running={health.running_count} desired={health.desired_count} "
                f"pending={health.pending_count}",
            }
        )
        primary = next((d for d in health.deployments if d.status == "PRIMARY"), None)
        rollout = (primary.rollout_state if primary else None) or "UNKNOWN"
        checks.append(
            {
                "check": "deploymentRolledOut",
                "passed": rollout.upper() == "COMPLETED",
                "detail": f"deployment={primary.id if primary else 'none'} "
                f"rolloutState={rollout}"
                + (f" reason={primary.rollout_state_reason}" if primary and primary.rollout_state_reason else ""),
            }
        )

    # -- 3: no errors since the boundary ---------------------------------
    log_group = CONFIG.default_log_group
    try:
        ticket_id = None
        for path in sorted(CONFIG.tickets_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if raw.get("service") == service and raw.get("cluster") == cluster:
                log_group = raw.get("logGroup") or log_group
                ticket_id = raw.get("id") or ticket_id
                break

        events = _backend.filter_log_events(
            log_group=log_group,
            minutes=max(1, minutes),
            limit=CONFIG.max_log_events,
            filter_pattern="ERROR",
        )
        new_errors = []
        for event in events:
            ts = _parse_iso(event.timestamp)
            if ts and ts >= boundary:
                new_errors.append(event)
        checks.append(
            {
                "check": "noNewErrors",
                "passed": len(new_errors) <= max_error_count,
                "detail": f"{len(new_errors)} ERROR event(s) after {boundary_source} "
                f"(limit {max_error_count}); {len(events) - len(new_errors)} "
                f"pre-remediation error(s) in window ignored",
                "sampleMessages": [e.message[:200] for e in new_errors[:3]],
            }
        )
    except ResourceNotFound as exc:
        checks.append(
            {"check": "noNewErrors", "passed": False, "detail": f"log group missing: {exc}"}
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"check": "noNewErrors", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
        )

    # -- 4: error rate ---------------------------------------------------
    try:
        series = _backend.get_metric_series(
            namespace=APP_NAMESPACE,
            metric_name="ErrorCount5xx",
            dimensions={"ServiceName": service},
            minutes=max(1, minutes),
        )
        latest = series.latest
        peak = series.peak
        passed = latest is not None and latest <= max(1.0, float(max_error_count))
        checks.append(
            {
                "check": "errorRateRecovered",
                "passed": passed,
                "detail": f"ErrorCount5xx latest={latest} peak={peak} "
                f"over last {minutes}m (unit={series.unit})",
            }
        )
    except ResourceNotFound as exc:
        checks.append(
            {
                "check": "errorRateRecovered",
                "passed": False,
                "detail": f"no metric data: {exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"check": "errorRateRecovered", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
        )

    # -- 5: optional CPU ceiling ----------------------------------------
    if max_cpu_percent is not None:
        try:
            cpu = _backend.get_metric_series(
                namespace=ECS_NAMESPACE,
                metric_name="CPUUtilization",
                dimensions={"ClusterName": cluster, "ServiceName": service},
                minutes=max(1, minutes),
            )
            checks.append(
                {
                    "check": "cpuWithinLimits",
                    "passed": cpu.latest is not None and cpu.latest <= max_cpu_percent,
                    "detail": f"CPUUtilization latest={cpu.latest} peak={cpu.peak} "
                    f"(limit {max_cpu_percent})",
                }
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {"check": "cpuWithinLimits", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
            )

    failed = [c["check"] for c in checks if not c["passed"]]
    verified = not failed and bool(checks)

    return ok(
        {
            "cluster": cluster,
            "service": service,
            "verified": verified,
            "verdict": "VERIFIED" if verified else "NOT_VERIFIED",
            "sinceBoundary": _iso(boundary),
            "sinceSource": boundary_source,
            "checksRun": len(checks),
            "checksPassed": len(checks) - len(failed),
            "failedChecks": failed,
            "checks": checks,
            "summary": (
                f"All {len(checks)} verification signals passed. The incident is resolved."
                if verified
                else f"{len(failed)} of {len(checks)} signals failed: {', '.join(failed)}. "
                "The incident is NOT resolved."
            ),
            "guidance": (
                "You may now mark the ticket RESOLVED."
                if verified
                else "Do not report resolution. Investigate the failed signals and "
                "either wait for a rollout to converge or escalate to a human."
            ),
        }
    )


@server.tool(
    name="update_ticket",
    title="Update a ticket's status and resolution notes",
    annotations=ToolAnnotations(**LOCAL_WRITE),
)
def update_ticket(
    ticket_id: str,
    status: str,
    note: str = "",
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a ticket's status and attach a resolution record.

    Only pass ``status="RESOLVED"`` after ``verify_service`` has returned
    ``verified: true``. Attaching a resolution record with ``rootCause`` and
    ``evidence`` makes the outcome auditable.
    """
    try:
        ticket = _tickets.update_status(
            ticket_id, status, resolution=resolution, note=note or None
        )
    except FileNotFoundError as exc:
        return fail("Ticket not found", str(exc))
    except ValueError as exc:
        return fail("Invalid update", str(exc))
    except OSError as exc:
        return tool_error(exc, f"updating ticket {ticket_id!r}")

    return ok(ticket.to_dict())


# -- DynamoDB verification ---------------------------------------------------
@server.tool(
    name="verify_dynamodb",
    title="Verify whether a DynamoDB remediation actually fixed the throttling",
    annotations=ToolAnnotations(**READ_ONLY),
)
def verify_dynamodb(
    table: str,
    since: str = "",
    minutes: int = 10,
    max_throttled: int = 5,
) -> dict[str, Any]:
    """Check whether DynamoDB throttling has stopped after remediation.

    This independently re-observes:
      1. ``throttledRequests`` - zero (or near-zero) ThrottledRequests after ``since``
      2. ``tableExists``      - the table is still reachable

    Args:
        table: DynamoDB table name.
        since: ISO timestamp marking the start of the remediation.
        minutes: Window used for the metric check.
        max_throttled: Allowed ThrottledRequests after ``since``. Default 5.
    """
    if not table:
        return fail("Missing input", "table is required.")

    checks: list[dict[str, Any]] = []

    # -- 1: throttled requests after boundary -----------------------------
    try:
        series = _backend.get_metric_series(
            namespace=DYNAMODB_NAMESPACE,
            metric_name="ThrottledRequests",
            dimensions={"TableName": table},
            minutes=max(1, minutes),
        )
        latest = series.latest
        effective_latest = 0.0 if latest is None else float(latest)
        checks.append(
            {
                "check": "throttledRequests",
                "passed": effective_latest <= max_throttled,
                "detail": (
                    f"ThrottledRequests latest={latest} "
                    f"(effective={effective_latest}, limit {max_throttled})"
                ),
            }
        )
    except ResourceNotFound as exc:
        checks.append(
            {"check": "throttledRequests", "passed": False, "detail": f"no metric data: {exc}"}
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"check": "throttledRequests", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
        )

    # -- 2: table still reachable -----------------------------------------
    try:
        _backend.describe_dynamodb_table(table)  # read-only probe
        checks.append(
            {"check": "tableExists", "passed": True, "detail": f"table {table!r} is reachable"}
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {"check": "tableExists", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
        )

    failed = [c["check"] for c in checks if not c["passed"]]
    verified = not failed and bool(checks)

    return ok(
        {
            "table": table,
            "verified": verified,
            "verdict": "VERIFIED" if verified else "NOT_VERIFIED",
            "sinceBoundary": _parse_iso(since) if since else None,
            "checksRun": len(checks),
            "checksPassed": len(checks) - len(failed),
            "failedChecks": failed,
            "checks": checks,
            "summary": (
                f"All {len(checks)} verification signals passed. DynamoDB throttling is resolved."
                if verified
                else f"{len(failed)} of {len(checks)} signals failed: {', '.join(failed)}. "
                "The incident is NOT resolved."
            ),
            "guidance": (
                "You may now mark the ticket RESOLVED."
                if verified
                else "Do not report resolution. Investigate the failed signals and "
                "either wait for the capacity update to propagate or escalate to a human."
            ),
        }
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    transport = os.environ.get("TICKET_RESOLVER_TRANSPORT", "stdio").strip().lower()

    if transport in {"streamable-http", "http"}:
        import anyio

        host = os.environ.get("TICKET_RESOLVER_HOST", "127.0.0.1")
        port = int(os.environ.get("TICKET_RESOLVER_PORT", "8080"))
        log.info("starting streamable-http MCP server on %s:%s/mcp", host, port)
        anyio.run(
            lambda: server.run_streamable_http_async(host=host, port=port)
        )
        return 0

    log.info("starting stdio MCP server (backend=%s)", _backend.name)
    log.info("tickets=%s runbooks=%s", CONFIG.tickets_dir, CONFIG.runbooks_dir)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
