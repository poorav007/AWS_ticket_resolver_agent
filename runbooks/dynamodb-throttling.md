---
title: DynamoDB provisioned throughput exceeded
slug: dynamodb-throttling
symptoms: [ProvisionedThroughputExceededException, throttled, dynamodb, "too many requests", "read capacity", "write capacity"]
services: [payment-api, order-api, any service using DynamoDB]
---

# DynamoDB Provisioned Throughput Exceeded

## Symptoms

- `ProvisionedThroughputExceededException` in application logs
- HTTP 500 errors from the service writing to DynamoDB
- CloudWatch `ThrottledRequests` metric elevated
- `ConsumedReadCapacityUnits` / `ConsumedWriteCapacityUnits` at or above provisioned limit
- Error rate spikes while CPU looks normal on the calling service

## Investigation

1. **Confirm the table is throttling** — `get_cloudwatch_metrics` for
   `AWS/DynamoDB` / `ThrottledRequests`. A non-zero value confirms throttling.
2. **Check consumed vs provisioned capacity** — query
   `ConsumedReadCapacityUnits` and `ConsumedWriteCapacityUnits` and
   compare against `ProvisionedReadCapacityUnits` / `ProvisionedWriteCapacityUnits`.
3. **Check the deploy history** — a new deployment that changed the
   query pattern or increased write volume will explain it.
4. **Check the calling service logs** — `get_recent_logs` filtered
   to `ProvisionedThroughputExceededException`.
5. **Distinguish from a real outage** — if the table itself is
   unavailable (not just throttling), that requires a different fix.

## Possible causes

- Traffic spike exceeding provisioned capacity
- Hot partition (single partition receiving disproportionate traffic)
- A recent deployment introduced a higher-volume query or write pattern
- Batch jobs or background workers suddenly writing to the table
- Capacity not scaled after a traffic pattern change

## Possible remediation

- **Increase provisioned read capacity** — raise `ProvisionedReadCapacityUnits`
- **Increase provisioned write capacity** — raise `ProvisionedWriteCapacityUnits`
- **Enable autoscaling** — if not already enabled, configure target tracking
- **Investigate hot partitions** — if a single key receives too much traffic

Disruptive — requires explicit human approval.

## Verification

1. `ThrottledRequests` back to 0 or near-baseline.
2. No `ProvisionedThroughputExceededException` in recent logs after the remediation timestamp.
3. `ConsumedReadCapacityUnits` and `ConsumedWriteCapacityUnits` below the new provisioned limits.
4. Application error rate (`ErrorCount5xx`) back to ~0.
5. `runningCount == desiredCount`, `rolloutState == COMPLETED` for the calling service.
