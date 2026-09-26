---
title: Service unhealthy / tasks not healthy
slug: service-unhealthy
symptoms: [unhealthy, "not healthy", "health check failed", "circuit breaker", "deployment failed", "tasks not starting"]
services: [payment-api, order-api, notification-worker, "any ecs service"]
---

# Service unhealthy / tasks not healthy

## Symptoms

- ECS service `runningCount` below `desiredCount`
- Tasks start and immediately stop
- `rolloutState: FAILED`, or `IN_PROGRESS` with the deployment circuit breaker
  tripped
- Load balancer health checks failing
- `service <name> has started N tasks` events with no matching steady state

## Investigation

1. **Get the service health snapshot** — `get_ecs_service_health`. The
   `issues` field summarises shortfalls and failed deployments.
2. **Read the service events** — the `events` list explains *why* a deployment
   stopped progressing ("was not able to come up due to errors", "task has
   started, but is not reporting healthy").
3. **Check the deployment** — `get_ecs_deployments`. A tripped circuit breaker
   means ECS has already retried repeatedly and given up.
4. **Read the logs** — `get_recent_logs` filtered to `ERROR` and then `WARN`.
   Look for the *container's* view of failing: missing env vars, failed
   dependency init, OOMKilled, failed bind.
5. **Compare task definitions** — a new task definition alongside a newly
   failed deployment is strong evidence of a bad release.

## Reading ECS events

| Event message | Meaning |
|---|---|
| `deployment ... was not able to come up due to errors` | Circuit breaker tripped; tasks are failing |
| `task has started, but is not reporting healthy` | Container starts, health check fails |
| `has started N tasks` with no steady-state event | Tasks still converging, or flapping |
| `service ... has reached a steady state` | Rollout completed successfully |

## Possible causes

- Bad release (code or configuration) — most common
- Health check timeout too aggressive for a slow-starting service
- Missing secret, env var, or IAM permission for the task role
- Resource limits too small (CPU throttling, OOM kill)
- Port/protocol mismatch between task and load balancer target group

## Possible remediation

- Force a new deployment onto a known-good task definition.
- Restart the service to clear crash-looping tasks.
- Correct the configuration/secret, then redeploy.
- Raise health check timeout or task memory limits.

Disruptive — requires explicit human approval.

## Verification

1. `runningCount == desiredCount` and `pendingCount == 0`.
2. Active deployment `rolloutState == COMPLETED`.
3. A `reach a steady state` event is present.
4. No ERROR logs after the remediation timestamp.
5. Service health endpoint reports healthy.
