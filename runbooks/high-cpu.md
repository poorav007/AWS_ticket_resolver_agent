---
title: High CPU utilization
slug: high-cpu
symptoms: [cpu, "high cpu", "cpu utilization", "cpu spike", "throttling", "slow", "latency"]
services: [payment-api, order-api, notification-worker, "any compute service"]
---

# High CPU utilization

## Symptoms

- `CPUUtilization` sustained above ~85%
- Elevated `LatencyP95` and request timeouts
- ECS CPU throttling reported for the task
- Increased 500s that resolve to handler timeouts
- Alert fires on average or maximum CPU

## Investigation

1. **Quantify it** — `get_cloudwatch_metrics` for `CPUUtilization` over at
   least 30 minutes. Note the `peak` versus `latest`: a `peak` far above
   `latest` means a spike that has already subsided, not a sustained problem.
2. **Check the shape** — a step change that lines up with a deployment points
   to a hot loop introduced by that release. A gradual ramp suggests a leak or
   growing workload.
3. **Correlate deployments** — `get_ecs_deployments`. Did CPU rise right after
   the latest rollout?
4. **Check memory too** — high CPU plus climbing memory suggests a leak or
   runaway loop rather than legitimate load.
5. **Check downstream latency** — if `LatencyP95` rose alongside CPU, the CPU
   may be *symptomatic* of retries against a slow dependency rather than the
   root cause.
6. **Read the logs** — `get_recent_logs` filtered to `WARN` and `ERROR` for
   retry storms, timeouts, or GC pressure messages.

## Reading the evidence

| Observation | Likely cause |
|---|---|
| CPU step change at deploy time | Inefficient code or accidental infinite loop |
| CPU high *and* dependency timeouts high | CPU is a symptom; fix the dependency |
| CPU slowly climbing over hours/days | Memory/CPU leak, unbounded cache |
| CPU high only at peak traffic | Legitimate load — scale out instead |
| CPU high with memory at the limit | Thrashing; raise task memory |

## Possible causes

- Inefficient or looping code in a recent release
- Retry storm against a slow dependency
- Memory/handle leak
- Legitimate traffic growth exceeding capacity
- Noisy neighbour on shared infrastructure

## Possible remediation

- Roll back to the last known-good task definition.
- Scale out task count (does **not** fix a per-task defect, but relieves
  legitimate saturation).
- Raise CPU/memory limits for the task.
- Fix the downstream dependency that is causing retries.

Disruptive — requires explicit human approval.

## Verification

1. `CPUUtilization` `latest` is back below the alert threshold, and staying
   there rather than oscillating.
2. `LatencyP95` recovered.
3. 5xx rate returned to baseline.
4. `runningCount == desiredCount` with `rolloutState == COMPLETED`.
5. No ERROR logs after the remediation timestamp.
