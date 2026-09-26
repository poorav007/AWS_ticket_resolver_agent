# Production AWS Setup for Ticket Resolver

This guide helps you prepare the project for a real-world AWS deployment before enabling execution.

---

## 1. Production rollout order

Use this order to reduce risk:

1. Enable **read-only AWS tools** first
2. Enable **read-only database tools**
3. Add **approval workflow**
4. Add **one narrow write tool**
5. Add **verification checks**
6. Enable production execution gradually

Do not start with broad write access.

---

## 2. Pre-execution checklist

Before running against real AWS, confirm all of the following:

- [ ] AWS account and region are finalized
- [ ] MCP server uses a dedicated IAM role
- [ ] IAM permissions follow least privilege
- [ ] Database access uses a restricted DB user
- [ ] Secrets are stored in AWS Secrets Manager or SSM Parameter Store
- [ ] Read-only investigation tools are tested
- [ ] Write tools are narrow and validated
- [ ] Human approval is required for risky actions
- [ ] All tool calls are audit-logged
- [ ] Post-remediation verification is implemented
- [ ] Staging environment has been tested successfully
- [ ] Production starts in read-only mode before write mode

---

## 3. What to allow the agent to do

Define a strict scope.

### Good scope
- Read ECS service health
- Read CloudWatch logs and metrics
- Read ticket state
- Read safe database diagnostics
- Restart only a specific ECS service after approval
- Update only approved ticket fields or recovery flags

### Bad scope
- Run arbitrary AWS CLI commands
- Run arbitrary SQL queries
- Update any database row in any table
- Full admin access to the AWS account

---

## 4. Recommended MCP tool categories

### Read-only tools
- `describe_backend`
- `get_ticket`
- `get_ecs_services`
- `get_ecs_service_health`
- `get_ecs_deployments`
- `get_recent_logs`
- `get_cloudwatch_metrics`
- `get_order_status`
- `get_payment_record`

### Write tools
- `propose_remediation`
- `execute_remediation`
- `update_ticket_status`
- `retry_failed_payment`
- `mark_record_for_recovery`

Every write tool should require validation, audit logging, and usually approval.

---

## 5. Sample least-privilege IAM policy

Replace `REGION`, `ACCOUNT_ID`, secret ARN, cluster name, and service name with your real values.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcsReadOnly",
      "Effect": "Allow",
      "Action": [
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:ListClusters"
      ],
      "Resource": "*"
    },
    {
      "Sid": "LogsReadOnly",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:FilterLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MetricsReadOnly",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ControlledEcsRemediation",
      "Effect": "Allow",
      "Action": [
        "ecs:UpdateService"
      ],
      "Resource": "arn:aws:ecs:REGION:ACCOUNT_ID:service/hackathon-cluster/payment-api"
    },
    {
      "Sid": "ReadDbSecret",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:REGION:ACCOUNT_ID:secret:ticket-resolver/db-*"
    }
  ]
}
```

### IAM notes
- Avoid `AdministratorAccess`
- Avoid wide write access on `*`
- Limit remediation to only the intended ECS service
- If multiple services are allowed, scope to only those exact ARNs

---

## 6. Database permission model

Use separate DB identities where possible.

### Read-only DB user
Allow only:
- `SELECT` on incident, order, or payment tables needed for diagnosis

### Write DB user
Allow only:
- `UPDATE` on approved columns only
- `INSERT` into audit/remediation tables if needed

Do not allow:
- `DROP`
- `ALTER`
- unrestricted `DELETE`
- unrestricted `UPDATE`
- arbitrary SQL execution from model input

---

## 7. Safe MCP database tool design

Do not expose a tool like this:

```text
execute_sql(query)
```

Instead expose narrow tools like:

- `get_order_status(order_id)`
- `retry_payment_record(payment_id, approved_by)`
- `mark_incident_recovery_started(ticket_id, approved_by)`

### Example safe pattern

```python
def retry_payment_record(payment_id: str, approved_by: str, ticket_id: str) -> dict:
    if not payment_id:
        return {"success": False, "error": "Missing payment_id"}

    if not approved_by:
        return {"success": False, "error": "Missing approved_by"}

    if not ticket_id:
        return {"success": False, "error": "Missing ticket_id"}

    allowed_prefix = "pay_"
    if not payment_id.startswith(allowed_prefix):
        return {"success": False, "error": "Invalid payment id"}

    # Lookup exact row first
    # Validate current state before update
    # Apply one targeted update only
    # Write audit record with approved_by and ticket_id
    # Return before/after status

    return {
        "success": True,
        "data": {
            "payment_id": payment_id,
            "ticket_id": ticket_id,
            "approved_by": approved_by,
            "status": "queued_for_retry"
        }
    }
```

### Validation rules for write tools
- require ticket ID
- require approver identity for risky actions
- restrict allowed resource IDs/patterns
- read current state before updating
- update only one known record at a time
- write an audit trail
- reject bulk updates

---

## 8. Approval workflow

For real production use, the write flow should be:

1. Investigate
2. Diagnose
3. Propose remediation
4. Wait for explicit human approval
5. Execute remediation
6. Verify service recovery
7. Mark ticket resolved

Minimum approval record should contain:
- ticket ID
- proposal ID
- approver name
- action approved
- timestamp
- rationale

---

## 9. Verification before resolution

Never treat API acceptance as success.

Verify at least:
- ECS running count equals desired count
- deployment rollout completed
- no new application errors after remediation
- error metric returns to normal
- latency or CPU returns to acceptable range
- database/application state matches expected result

Only after successful verification should the ticket be resolved.

---

## 10. Logging and audit requirements

Log every important action:

- tool name
- timestamp
- ticket ID
- target resource
- approver identity
- request parameters
- before state
- after state
- execution outcome
- verification outcome

This is important for debugging, compliance, and incident review.

---

## 11. Recommended production architecture

```text
User
  -> Agent
     -> MCP Server
        -> AWS APIs (ECS, CloudWatch, Secrets Manager)
        -> Database (restricted DB user)
```

The agent should never directly access AWS credentials or raw database admin access.

---

## 12. Final recommendation

Start small in production:

1. enable read-only AWS tools
2. enable read-only DB tools
3. confirm logs, metrics, and diagnostics are correct
4. add approval-gated remediation
5. add one very narrow DB write tool
6. expand only after successful staging validation

This is the safest path for real-world deployment.
