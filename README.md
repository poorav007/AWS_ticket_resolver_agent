# Ticket Resolver

An autonomous IT incident-resolution agent for AWS ECS.

> Understand → Investigate → Diagnose → **Approve** → Remediate → Verify → Resolve

TrueForge (or OpenCode) runs the agent. The agent reaches AWS **only** through a
local MCP server that acts as a controlled bridge. There is no direct
model-to-boto3 path, and no arbitrary-command tool.

```
User
  ↓
TrueForge  ── agent loop, human approval checkpoints, chat UI
  ↓  MCP protocol
Ticket Resolver MCP server   ← the only thing that touches AWS
  ↓  boto3 (default credential chain)
AWS ECS / CloudWatch
```

---

## Quick start

```bash
# 1. Virtualenv (Python 3.13 — the system 3.15 is a beta and lacks wheels)
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 2. Prove the whole lifecycle works
.venv/bin/python scripts/reset_demo.py --tickets
.venv/bin/python scripts/test_workflow.py        # expect 41/41 checks passed

# 3a. Use it from OpenCode (stdio transport, configured in opencode.json)
#     -> restart OpenCode so it spawns the MCP server

# 3b. Or use it from TrueForge (HTTP transport)
./scripts/serve_for_trueforge.sh
```

No AWS account or credentials are needed to run the demo — see
[Backends](#backends).

---

## URLs and commands

| Use case | Command | URL / endpoint |
|---|---|---|
| OpenCode local MCP server (configured in `opencode.json`) | Restart OpenCode after setup | No URL needed; OpenCode launches `.venv/bin/python mcp-server/server.py` locally |
| TrueForge via local HTTP | `./scripts/serve_for_trueforge.sh` | `http://127.0.0.1:8080/mcp` |
| TrueForge via public HTTPS tunnel | `./scripts/serve_with_tunnel.sh` | Printed at runtime as `https://<random>.trycloudflare.com/mcp` |
| Manual HTTP server start | `TICKET_RESOLVER_TRANSPORT=streamable-http TICKET_RESOLVER_PORT=8080 .venv/bin/python mcp-server/server.py` | `http://127.0.0.1:8080/mcp` |
| OpenCode config schema | Already referenced in `opencode.json` | `https://opencode.ai/config.json` |

Typical flow:

```bash
# Setup
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# Reset demo data
.venv/bin/python scripts/reset_demo.py --tickets

# Run workflow test
.venv/bin/python scripts/test_workflow.py

# Start local HTTP MCP server for TrueForge
./scripts/serve_for_trueforge.sh

# Or start with a public HTTPS tunnel for hosted TrueForge
./scripts/serve_with_tunnel.sh
```

---

## Layout

```
.
├── mcp-server/
│   ├── server.py             # MCP tools; the approval gate lives here
│   ├── config.py             # env-driven settings, no secrets
│   ├── models.py             # typed domain models
│   ├── errors.py             # {"success": ...} envelopes + redaction
│   ├── tickets.py            # local JSON ticket store
│   ├── runbooks.py           # runbook loading + symptom matching
│   └── backends/
│       ├── base.py           # AWSBackend interface
│       ├── boto3_backend.py  # real AWS
│       └── sim.py            # simulated AWS account
├── tickets/                  # INC-1001.json, INC-1002.json
├── runbooks/                 # api-500, high-cpu, database-timeout, service-unhealthy, dynamodb-throttling
├── scripts/
│   ├── test_workflow.py      # end-to-end lifecycle test
│   ├── reset_demo.py         # replay the incident
│   ├── serve_for_trueforge.sh
│   └── serve_with_tunnel.sh  # server + public HTTPS tunnel for TrueForge
├── AGENTS.md                 # agent instructions (loaded by OpenCode)
└── opencode.json
```

---

## The tools

Investigation tools are annotated `read_only_hint=True`; anything that can
change AWS is `destructive_hint=True`. The split is machine-readable, not just
a naming convention.

| Tool | Kind | Purpose |
|---|---|---|
| `describe_backend` | read | Is this real AWS or simulated? |
| `get_ticket` | read | Ticket by id |
| `list_tickets` | read | Available ticket ids |
| `get_ecs_services` | read | Services in a cluster |
| `get_ecs_service_health` | read | Counts, rollout state, events, `issues` |
| `get_ecs_deployments` | read | Deployment history for correlation |
| `list_log_groups` | read | Discover log group names |
| `get_recent_logs` | read | Recent events, filterable, length-capped |
| `get_cloudwatch_metrics` | read | `cpu`/`memory`/`errors`/`requests`/`latency`/`dynamodb_throttled` |
| `get_runbook` | read | Runbooks matched to a ticket |
| `propose_remediation` | **write** | Records a plan; mutates nothing |
| `execute_remediation` | **destructive** | Runs an *approved* plan |
| `verify_service` | read | Multi-signal proof the incident is over |
| `verify_dynamodb` | read | DynamoDB throttling proof after remediation |
| `update_ticket` | write | Status + resolution record |

Every tool returns `{"success": true, "data": {...}}` or
`{"success": false, "error": "...", "details": "..."}`. A missing cluster, an
unknown log group, or absent AWS credentials are **results**, never crashes.

---

## The approval gate

Two independent layers, either of which alone blocks a disruptive action:

1. **Harness checkpoint** — TrueForge pauses the turn and asks the human.
2. **Server-side gate** — `execute_remediation` refuses unless it is given a
   `proposal_id` that exists, is `PENDING_APPROVAL`, and a non-empty
   `approved_by`. There is no code path that reaches `forceNewDeployment`
   without one. A model cannot talk its way past this.

`propose_remediation` performs no AWS mutation. It validates the target,
requires a rationale of real substance, and returns a token. Only then, with a
named human approver, can execution proceed. A second execution of the same
proposal is refused. Supports both `forceNewDeployment` (ECS) and
`update_dynamodb_table` (DynamoDB).

## Why verification is separate

`forceNewDeployment` returning HTTP 200 means AWS *accepted a request*. It says
nothing about whether customers are being served. So `verify_service`
independently re-observes five signals:

| Check | Passes when |
|---|---|
| `tasksRunning` | `runningCount == desiredCount` and > 0 |
| `deploymentRolledOut` | PRIMARY deployment `rolloutState == COMPLETED` |
| `noNewErrors` | zero ERROR log events **after** the remediation timestamp |
| `errorRateRecovered` | `ErrorCount5xx` back to ~0 in recent buckets |
| `cpuWithinLimits` | only when `max_cpu_percent` is supplied |

Only `verified: true` permits `update_ticket(status="RESOLVED")`.

Note the third check is anchored to `since`, not to a sliding window. A
"last 5 minutes" query still contains pre-remediation errors — that is how
CloudWatch actually behaves — so the honest question is "any errors *since* we
fixed it?".

---

## Backends

Selected by `TICKET_RESOLVER_BACKEND`:

| Value | Behaviour |
|---|---|
| `auto` *(default)* | Real AWS if credentials resolve, else simulated |
| `boto3` | Real AWS; fails loudly without credentials |
| `sim` | Simulated account only |

The simulated account reproduces two demo scenarios:

- **INC-1001**: `payment-api` ships a build missing `GATEWAY_API_KEY`, tasks crash-loop, CloudWatch fills with 500s, and the deployment trips the ECS circuit breaker.
- **INC-1002**: `payments-transactions` DynamoDB table has insufficient provisioned capacity, returning `ProvisionedThroughputExceededException` and causing HTTP 500 errors.

State lives in `state/simulated-aws.json` and persists across restarts, so *before* and *after* remediation are genuinely different observations. The running server notices external changes to that file, so `reset_demo.py` works mid-session.

**The agent is instructed to label simulated evidence as synthetic.** Point it at real AWS when you have credentials.

### Real AWS

Credentials are never hardcoded. The boto3 default chain applies: env vars,
`~/.aws/credentials`, SSO, instance profile, or ECS task role.

```bash
aws sts get-caller-identity        # should print your ARN
export TICKET_RESOLVER_BACKEND=boto3
export AWS_REGION=us-east-1
```

### Least-privilege IAM

Attach to the role the agent assumes. No `AdministratorAccess`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOnlyInvestigation",
      "Effect": "Allow",
      "Action": [
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:DescribeTasks",
        "ecs:ListClusters",
        "logs:DescribeLogGroups",
        "logs:FilterLogEvents",
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "dynamodb:DescribeTable",
        "dynamodb:ListTables"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ControlledRemediation",
      "Effect": "Allow",
      "Action": [
        "ecs:UpdateService",
        "dynamodb:UpdateTable"
      ],
      "Resource": [
        "arn:aws:ecs:REGION:ACCOUNT:service/hackathon-cluster/*",
        "arn:aws:dynamodb:REGION:ACCOUNT:table/payments-transactions"
      ]
    }
  ]
}
```

`ecs:UpdateService` and `dynamodb:UpdateTable` are the only write permissions, each scoped to the specific resource. There is no delete anywhere in the codebase.

---

## Configuration

All optional; defaults work out of the box.

| Variable | Default | Purpose |
|---|---|---|
| `TICKET_RESOLVER_BACKEND` | `auto` | `auto` / `boto3` / `sim` |
| `TICKET_RESOLVER_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `TICKET_RESOLVER_HOST` / `_PORT` | `127.0.0.1` / `8080` | HTTP bind address |
| `TICKET_RESOLVER_CLUSTER` | `hackathon-cluster` | Default cluster |
| `TICKET_RESOLVER_LOG_GROUP` | `/ecs/payment-api` | Fallback log group |
| `TICKET_RESOLVER_MAX_LOG_EVENTS` | `50` | Hard cap per log call |
| `TICKET_RESOLVER_ALLOW_REMEDIATION` | `true` | Kill switch for writes |
| `TICKET_RESOLVER_TICKETS_DIR` | `./tickets` | Ticket location |
| `TICKET_RESOLVER_RUNBOOKS_DIR` | `./runbooks` | Runbook location |

To demo the gate, set `TICKET_RESOLVER_ALLOW_REMEDIATION=false`; every
`execute_remediation` call then fails no matter what the model says.

---

## TrueForge integration

TrueForge registers MCP servers **by URL** under *Settings → Connectors*; it
does not spawn local stdio processes.

### Preferred: Local HTTP server (same machine)

If TrueForge is running on the same machine as the MCP server, use the
local HTTP transport:

```bash
./scripts/serve_for_trueforge.sh
```

Then in TrueForge: *Settings → Connectors → Add MCP Server*
- **URL:** `http://127.0.0.1:8080/mcp`
- **Auth:** No auth

> If TrueForge's connector rejects `127.0.0.1` with `Outbound URL blocked`,
> you are running TrueForge in a hosted/remote environment (not the same
> machine). See the tunnel option below.

### Optional: Public HTTPS tunnel (remote TrueForge)

If TrueForge is hosted remotely and rejects loopback/private addresses,
you can expose the server via a public HTTPS tunnel. This requires `cloudflared`
and a working outbound network connection. **This path is NOT required for
development or local demos.**

```bash
brew install cloudflared      # once
./scripts/serve_with_tunnel.sh
```

That starts the MCP server, opens a public HTTPS tunnel, and prints a URL
similar to `https://<random>.trycloudflare.com/mcp`.

In TrueForge: *Settings → Connectors → Add MCP Server* → paste the URL,
auth **No auth**. Then create the agent, attach the `ticket-resolver-aws`
server, and use [`AGENTS.md`](AGENTS.md) as the agent instructions. In the
agent's tool-approval settings, mark `execute_remediation` and
`propose_remediation` as requiring approval.

> ⚠️ The tunnel exposes the server to the public internet while it runs. The URL
> is random and unguessable, but anyone holding it can invoke the tools,
> including the approval-gated remediation. Run it only for the demo, and stop
> it afterwards with Ctrl-C.

---

## Demo script

1. `./scripts/reset_demo.py --tickets`
2. Start TrueForge, open the Ticket Resolver agent.
3. *"Resolve INC-1001."*
4. Agent investigates, cites evidence, and proposes a remediation — then stops.
5. **You approve** (or reject, or ask for more evidence).
6. Agent executes, verifies, and reports the outcome.
7. Show `cat tickets/INC-1001.json` — the resolution record is written to disk.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No tools in OpenCode | Restart OpenCode so it respawns the MCP server |
| Tools behave differently than the code you just edited | The stdio server is long-lived; restart it |
| `No AWS credentials available` | Expected with no creds — use `TICKET_RESOLVER_BACKEND=sim` |
| `ModuleNotFoundError: mcp.server.fastmcp` | You copied v1 code. v2 renamed it to `MCPServer` |
| Agent reports `ModuleNotFoundError` for `boto3` | Use `.venv/bin/python`, not the system `python3` |
| Demo already looks healthy | `scripts/reset_demo.py` |
