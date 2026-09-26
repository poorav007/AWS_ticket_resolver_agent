#!/usr/bin/env python3
"""End-to-end test of the Ticket Resolver MCP server.

Runs the full incident lifecycle against the server over a real MCP stdio
connection: investigate -> propose -> (approval gate) -> execute -> verify.

    .venv/bin/python scripts/test_workflow.py

Exits non-zero if any stage fails, so it doubles as a smoke test in CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-server"))

os.environ.setdefault("TICKET_RESOLVER_BACKEND", "sim")

from mcp import Client, StdioServerParameters  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((PASS if condition else FAIL, name, detail))
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def as_dict(result) -> dict:
    """Normalise an MCP tool result into a plain dict."""
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured.get("result", structured)
    raise AssertionError(f"no usable content in result: {result!r}")


async def main() -> int:
    params = StdioServerParameters(
        command=str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        args=[str(PROJECT_ROOT / "mcp-server" / "server.py")],
        env={**os.environ, "TICKET_RESOLVER_BACKEND": "sim"},
    )

    async with Client(params) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print(f"\nConnected. {len(names)} tools exposed:\n  {', '.join(names)}\n")

        check("tools exposed", len(names) >= 11, f"{len(names)} tools")
        for required in (
            "get_ticket", "get_ecs_services", "get_ecs_service_health",
            "get_ecs_deployments", "get_recent_logs", "get_cloudwatch_metrics",
            "propose_remediation", "execute_remediation", "verify_service",
            "verify_dynamodb", "update_ticket",
        ):
            check(f"tool {required}", required in names)

        # -- read/write annotations ---------------------------------
        ann = {t.name: t.annotations for t in tools.tools}
        check(
            "investigation tools marked read-only",
            all(
                getattr(ann.get(n), "read_only_hint", None) is True
                for n in ("get_ecs_service_health", "get_recent_logs", "verify_service")
            ),
        )
        check(
            "remediation tools marked destructive",
            all(
                getattr(ann.get(n), "destructive_hint", None) is True
                for n in ("execute_remediation", "propose_remediation")
            ),
        )

        # -- 1. backend ---------------------------------------------
        print("\n1. Backend")
        backend = as_dict(await client.call_tool("describe_backend", {}))
        check("backend reachable", backend.get("success") is True, backend.get("data", {}).get("backend", ""))

        # -- 2. ticket ----------------------------------------------
        print("\n2. Ticket")
        ticket = as_dict(await client.call_tool("get_ticket", {"ticket_id": "INC-1001"}))
        check("ticket fetched", ticket.get("success") is True)
        data = ticket.get("data", {})
        check("ticket targets payment-api", data.get("service") == "payment-api", str(data.get("service")))

        missing = as_dict(await client.call_tool("get_ticket", {"ticket_id": "INC-9999"}))
        check("missing ticket handled", missing.get("success") is False, missing.get("error", ""))

        # -- 3. investigate -----------------------------------------
        print("\n3. Investigation")
        services = as_dict(await client.call_tool("get_ecs_services", {"cluster": "hackathon-cluster"}))
        check("services listed", "payment-api" in services.get("data", {}).get("services", []))

        bad_cluster = as_dict(await client.call_tool("get_ecs_services", {"cluster": "does-not-exist"}))
        check("bad cluster handled", bad_cluster.get("success") is False, bad_cluster.get("error", ""))

        health = as_dict(await client.call_tool(
            "get_ecs_service_health", {"cluster": "hackathon-cluster", "service": "payment-api"}))
        check("service is DEGRADED", health.get("data", {}).get("verdict") == "DEGRADED",
              health.get("data", {}).get("verdict", ""))
        check("issues detected", len(health.get("data", {}).get("issues", [])) > 0,
              "; ".join(health.get("data", {}).get("issues", []))[:90])

        deps = as_dict(await client.call_tool(
            "get_ecs_deployments", {"cluster": "hackathon-cluster", "service": "payment-api"}))
        check("deployment blocked", deps.get("data", {}).get("rolloutBlocked") is True)

        logs = as_dict(await client.call_tool(
            "get_recent_logs",
            {"log_group": "/ecs/payment-api", "minutes": 30, "limit": 20, "filter_pattern": "ERROR"}))
        check("error logs found", logs.get("data", {}).get("errorEvents", 0) > 0,
              f"{logs.get('data', {}).get('errorEvents')} events")

        bad_logs = as_dict(await client.call_tool(
            "get_recent_logs", {"log_group": "/ecs/nope", "minutes": 5}))
        check("missing log group handled", bad_logs.get("success") is False)

        metrics = as_dict(await client.call_tool(
            "get_cloudwatch_metrics", {"metric": "errors", "service": "payment-api", "minutes": 30}))
        check("5xx metric elevated", (metrics.get("data", {}).get("latest") or 0) > 0,
              f"latest={metrics.get('data', {}).get('latest')}")

        bad_metric = as_dict(await client.call_tool(
            "get_cloudwatch_metrics", {"metric": "nonsense", "service": "payment-api"}))
        check("bad metric rejected", bad_metric.get("success") is False)

        runbook = as_dict(await client.call_tool("get_runbook", {"ticket_id": "INC-1001"}))
        slugs = [m["slug"] for m in runbook.get("data", {}).get("matches", [])]
        check("runbook matched", "api-500" in slugs, ", ".join(slugs))

        # -- 4. approval gate ---------------------------------------
        print("\n4. Approval gate")
        no_approval = as_dict(await client.call_tool(
            "execute_remediation", {"proposal_id": "prop-doesnotexist", "approved_by": "tester"}))
        check("unknown proposal refused", no_approval.get("success") is False)

        proposal = as_dict(await client.call_tool("propose_remediation", {
            "ticket_id": "INC-1001",
            "action": "force_new_deployment",
            "cluster": "hackathon-cluster",
            "service": "payment-api",
            "rationale": "Latest deployment tripped the circuit breaker and logs show ConfigError: GATEWAY_API_KEY is not set.",
            "expected_impact": "Brief interruption while 2 tasks are replaced (~1 minute).",
            "evidence": ["[ERROR] ConfigError: GATEWAY_API_KEY is not set or invalid"],
        }))
        check("proposal created", proposal.get("success") is True)
        pid = proposal.get("data", {}).get("proposalId", "")
        check("proposal pending approval",
              proposal.get("data", {}).get("status") == "PENDING_APPROVAL", pid)

        ticket_state = as_dict(await client.call_tool("get_ticket", {"ticket_id": "INC-1001"}))
        check("ticket awaiting approval",
              ticket_state.get("data", {}).get("status") == "AWAITING_APPROVAL",
              ticket_state.get("data", {}).get("status", ""))

        # -- 5. verify BEFORE remediation (must fail) ----------------
        print("\n5. Pre-remediation verification (must NOT verify)")
        early = as_dict(await client.call_tool("verify_service", {
            "cluster": "hackathon-cluster", "service": "payment-api", "minutes": 10}))
        check("pre-remediation NOT verified", early.get("data", {}).get("verified") is False,
              ", ".join(early.get("data", {}).get("failedChecks", [])))

        # -- 6. execute ----------------------------------------------
        print("\n6. Execute approved remediation")
        executed = as_dict(await client.call_tool(
            "execute_remediation", {"proposal_id": pid, "approved_by": "oncall@example.com"}))
        check("remediation executed", executed.get("success") is True,
              str(executed.get("data", {}).get("result", {}).get("deploymentId", ""))[:40])
        check("verification flagged required",
              executed.get("data", {}).get("verificationRequired") is True)

        replay = as_dict(await client.call_tool(
            "execute_remediation", {"proposal_id": pid, "approved_by": "oncall@example.com"}))
        check("double execution refused", replay.get("success") is False, replay.get("error", ""))

        # -- 7. verify -----------------------------------------------
        print("\n7. Post-remediation verification")
        after = as_dict(await client.call_tool("verify_service", {
            "cluster": "hackathon-cluster", "service": "payment-api",
            "since": executed.get("data", {}).get("executedAt"), "minutes": 10}))
        data = after.get("data", {})
        check("post-remediation VERIFIED", data.get("verified") is True,
              ", ".join(data.get("failedChecks", [])))
        for entry in data.get("checks", []):
            print(f"        - {entry['check']}: {entry['detail'][:80]}")

        # -- 8. resolve ---------------------------------------------
        print("\n8. Resolution")
        resolved = as_dict(await client.call_tool("update_ticket", {
            "ticket_id": "INC-1001", "status": "RESOLVED",
            "note": "Rolled payment-api onto a corrected task definition via ecs:UpdateService.",
            "resolution": {
                "rootCause": "Latest deployment shipped without GATEWAY_API_KEY, so payment-gateway returned 401 and the API returned 500.",
                "remediation": "force_new_deployment",
                "verifiedBy": "verify_service",
            },
        }))
        check("ticket resolved", resolved.get("data", {}).get("status") == "RESOLVED",
              resolved.get("data", {}).get("status", ""))

        # -- 8b. DynamoDB scenario (INC-1002) -----------------------
        print("\n8b. DynamoDB throttling scenario (INC-1002)")
        dyn_ticket = as_dict(await client.call_tool("get_ticket", {"ticket_id": "INC-1002"}))
        check("DynamoDB ticket fetched", dyn_ticket.get("success") is True)
        check("DynamoDB ticket targets payments-transactions",
              dyn_ticket.get("data", {}).get("dynamodbTable") == "payments-transactions",
              str(dyn_ticket.get("data", {}).get("dynamodbTable")))

        # Verify throttled metric is elevated before remediation
        dyn_metrics = as_dict(await client.call_tool(
            "get_cloudwatch_metrics",
            {"metric": "dynamodb_throttled", "service": "payments-transactions", "minutes": 30}))
        check("DynamoDB throttled metric elevated",
              (dyn_metrics.get("data", {}).get("latest") or 0) > 0,
              f"latest={dyn_metrics.get('data', {}).get('latest')}")

        # Propose DynamoDB capacity increase
        dyn_proposal = as_dict(await client.call_tool("propose_remediation", {
            "ticket_id": "INC-1002",
            "action": "update_dynamodb_table",
            "cluster": "hackathon-cluster",
            "service": "payment-api",
            "table_name": "payments-transactions",
            "rationale": "ThrottledRequests metric at 847 exceeds provisioned capacity. Increasing read and write capacity to 100.",
            "expected_impact": "No downtime; capacity update applies in seconds (~1 minute to propagate).",
            "evidence": ["ThrottledRequests latest=847, baseline=12"],
        }))
        check("DynamoDB proposal created", dyn_proposal.get("success") is True)
        dyn_pid = dyn_proposal.get("data", {}).get("proposalId", "")
        check("DynamoDB proposal pending approval",
              dyn_proposal.get("data", {}).get("status") == "PENDING_APPROVAL", dyn_pid)

        # Verify BEFORE remediation (must NOT verify)
        dyn_no_verify = as_dict(await client.call_tool("verify_dynamodb", {
            "table": "payments-transactions", "minutes": 10}))
        check("pre-remediation DynamoDB NOT verified",
              dyn_no_verify.get("data", {}).get("verified") is False,
              ", ".join(dyn_no_verify.get("data", {}).get("failedChecks", [])))

        # Execute approved DynamoDB remediation
        dyn_executed = as_dict(await client.call_tool(
            "execute_remediation", {"proposal_id": dyn_pid, "approved_by": "oncall@example.com"}))
        check("DynamoDB remediation executed", dyn_executed.get("success") is True,
              str(dyn_executed.get("data", {}).get("newReadCapacity", "")) + "/" +
              str(dyn_executed.get("data", {}).get("newWriteCapacity", "")))
        check("DynamoDB verification flagged required",
              dyn_executed.get("data", {}).get("verificationRequired") is True)

        # Verify AFTER remediation (must verify)
        dyn_after = as_dict(await client.call_tool("verify_dynamodb", {
            "table": "payments-transactions", "since": dyn_executed.get("data", {}).get("executedAt"),
            "minutes": 10}))
        check("post-remediation DynamoDB VERIFIED",
              dyn_after.get("data", {}).get("verified") is True,
              ", ".join(dyn_after.get("data", {}).get("failedChecks", [])))

        # Resolve DynamoDB ticket
        dyn_resolved = as_dict(await client.call_tool("update_ticket", {
            "ticket_id": "INC-1002", "status": "RESOLVED",
            "note": "Increased payments-transactions DynamoDB provisioned capacity to 100 read / 100 write.",
            "resolution": {
                "rootCause": "DynamoDB table payments-transactions had insufficient provisioned capacity (5 RCU / 5 WCU), causing ProvisionedThroughputExceededException.",
                "remediation": "update_dynamodb_table",
                "newReadCapacity": 100,
                "newWriteCapacity": 100,
                "verifiedBy": "verify_dynamodb",
            },
        }))
        check("DynamoDB ticket resolved",
              dyn_resolved.get("data", {}).get("status") == "RESOLVED",
              dyn_resolved.get("data", {}).get("status", ""))

        # -- 9. regressions -----------------------------------------
        # These lock in bugs found during live testing.
        print("\n9. Regression guards")

        # (a) Post-remediation the service must read HEALTHY. A tripped circuit
        #     breaker on a *superseded* deployment is history, not a live fault.
        final_health = as_dict(await client.call_tool(
            "get_ecs_service_health", {"cluster": "hackathon-cluster", "service": "payment-api"}))
        fh = final_health.get("data", {})
        check("post-remediation verdict is HEALTHY", fh.get("verdict") == "HEALTHY",
              f"verdict={fh.get('verdict')} issues={fh.get('issues')}")

        # (b) Long metric windows must be down-sampled to protect agent context.
        capped = as_dict(await client.call_tool("get_cloudwatch_metrics", {
            "metric": "errors", "service": "payment-api", "minutes": 60, "max_points": 20}))
        cd = capped.get("data", {})
        check("metric series down-sampled", len(cd.get("points", [])) <= 21,
              f"{len(cd.get('points', []))} points returned")
        check("summary reflects full window", cd.get("sampleCount", 0) > len(cd.get("points", [])),
              f"sampleCount={cd.get('sampleCount')}")

        # (c) reset_demo.py must take effect on an ALREADY RUNNING server, which
        #     requires the simulated backend to notice the state file changed.
        state_file = PROJECT_ROOT / "state" / "simulated-aws.json"
        backup = state_file.read_text() if state_file.exists() else None
        if backup is not None:
            state_file.unlink()
            after_reset = as_dict(await client.call_tool(
                "get_ecs_service_health", {"cluster": "hackathon-cluster", "service": "payment-api"}))
            ar = after_reset.get("data", {})
            check("running server picks up reset", ar.get("verdict") == "DEGRADED",
                  f"verdict={ar.get('verdict')} "
                  f"running={ar.get('running_count')}/{ar.get('desired_count')}")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(backup)

    # -- summary ---------------------------------------------------------
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'=' * 60}")
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailures:")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
