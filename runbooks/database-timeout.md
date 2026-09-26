---
title: Database / datastore connection timeout
slug: database-timeout
symptoms: [timeout, "connection timeout", "database", "db", "sql", "connection refused", "too many connections", "deadlock"]
services: [payment-api, order-api, "any service with a datastore"]
---

# Database / datastore connection timeout

## Symptoms

- `TimeoutError`, `OperationalError`, or connection-pool exhaustion errors
- Requests hang then fail with 500 or 504
- Error rate spikes while CPU looks normal
- Logs show connect timeouts, not application exceptions
- `LatencyP95` rises sharply; `RequestCount` may be flat or falling

## Investigation

1. **Confirm the logs point at the datastore** — `get_recent_logs` filtered
   to `ERROR`. Look for the driver/ORM error text, not a generic 500.
2. **Distinguish timeout from refusal** — a *timeout* means the datastore is
   reachable but slow/saturated (or the network path is broken). A *refused*
   connection means nothing is listening: wrong host/port, or the datastore is
   down.
3. **Check pool exhaustion** — errors mentioning "too many connections",
   "pool exhausted", or "timeout waiting for connection" indicate the pool is
   sized too small or connections are leaking.
4. **Check CPU on the calling service** — high CPU plus DB timeouts usually
   means the app is the bottleneck, not the database.
5. **Check latency, not just errors** — `get_cloudwatch_metrics` for
   `LatencyP95`. A latency cliff with a flat request count is the classic
   datastore-saturation signature.
6. **Check the deploy history** — a new connection-pool setting, a changed
   query, or a new migration in the latest deployment will explain it.

## Reading the evidence

| Observation | Likely cause |
|---|---|
| Connect timeout, DB CPU unknown | Network path, security group, or DB overloaded |
| "too many connections" | Pool too large for the DB's limit, or a leak |
| "pool exhausted" | Connections not returned; leak or slow queries |
| Timeouts immediately after a deploy | Bad pool config or an accidental N+1 query |
| Deadlock detected | Concurrent write contention; needs a schema/query fix |

## Possible causes

- Connection pool exhausted or misconfigured (often by a recent deploy)
- Datastore saturated (CPU, IOPS, max connections)
- Long-running query or missing index introduced recently
- Network/ACL change blocking the path
- Datastore failover in progress

## Possible remediation

- Roll back to the last known-good task definition (fixes pool/query
  regressions).
- Restart the service to reset leaked connections.
- Raise pool limits or task resources.
- For genuine saturation, scale the datastore — note that this is *outside*
  this agent's current toolset and must be escalated to a human.

Disruptive — requires explicit human approval. If the root cause is the
datastore itself, say so and escalate rather than repeatedly restarting the
calling service.

## Verification

1. No connection-timeout errors after the remediation timestamp.
2. `LatencyP95` back to baseline.
3. `runningCount == desiredCount`, `rolloutState == COMPLETED`.
4. `ErrorCount5xx` at ~0 in the most recent buckets.
5. Request throughput recovered to its pre-incident level.
