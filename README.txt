Ticket Resolver
================

Overview
--------
Ticket Resolver is an autonomous incident-resolution agent for AWS ECS services.
It investigates incidents, proposes a safe remediation, waits for human approval,
executes the remediation, verifies recovery, and then resolves the ticket.

Workflow
--------
Understand -> Investigate -> Diagnose -> Approve -> Remediate -> Verify -> Resolve

Main Features
-------------
- Reads incident tickets from local JSON files
- Investigates ECS service health, deployments, logs, and metrics
- Uses runbooks for guided diagnosis
- Requires human approval before disruptive remediation
- Verifies recovery before marking a ticket as resolved
- Supports both simulated AWS and real AWS via boto3

Project Structure
-----------------
- mcp-server/           MCP server implementation and tools
- mcp-server/backends/  AWS backends (simulated and boto3)
- runbooks/             Troubleshooting runbooks
- tickets/              Sample incident tickets
- scripts/              Helper scripts for testing and demo setup
- state/                Simulated backend and proposal state
- AGENTS.md             Agent operating instructions
- opencode.json         OpenCode MCP configuration

Requirements
------------
- Python 3.13 recommended
- pip
- Optional: AWS credentials for real AWS mode
- Optional: cloudflared for public HTTPS tunnel

Install
-------
1. Create a virtual environment:
   python3.13 -m venv .venv

2. Install dependencies:
   .venv/bin/python -m pip install -r requirements.txt

Main Commands
-------------
Reset demo tickets:
   .venv/bin/python scripts/reset_demo.py --tickets

Run workflow test:
   .venv/bin/python scripts/test_workflow.py

Start MCP server locally for OpenCode:
   .venv/bin/python mcp-server/server.py

Start HTTP MCP server for TrueForge:
   ./scripts/serve_for_trueforge.sh

Start HTTP MCP server with public tunnel:
   ./scripts/serve_with_tunnel.sh

Useful URLs
-----------
- Local MCP endpoint (OpenCode stdio):
  configured in opencode.json — no URL needed

- Local HTTP endpoint (TrueForge same machine):
  http://127.0.0.1:8080/mcp
  Start with: ./scripts/serve_for_trueforge.sh

- Public tunnel endpoint (remote TrueForge only, requires cloudflared + network):
  https://<random>.trycloudflare.com/mcp
  Start with: ./scripts/serve_with_tunnel.sh

- OpenCode config schema:
  https://opencode.ai/config.json

Backends
--------
The backend is controlled by TICKET_RESOLVER_BACKEND:

- auto   = use real AWS if credentials exist, otherwise simulated mode
- boto3  = force real AWS
- sim    = force simulated AWS

Example:
   export TICKET_RESOLVER_BACKEND=sim

GitHub Upload Note
------------------
If you are uploading this project to GitHub, include this README.txt for a quick
plain-text explanation, and keep README.md for the full formatted documentation.

Safety Notes
------------
- Remediation requires explicit human approval.
- Verification is required before resolution.
- Do not expose AWS credentials.
- Prefer simulated mode for demo/testing if you do not have AWS access.

Suggested Demo Flow
-------------------
1. Install dependencies
2. Reset demo tickets
3. Run the workflow test
4. Start the server
5. Connect from OpenCode or TrueForge
6. Investigate and resolve ticket INC-1001

Files to Mention on GitHub
--------------------------
- README.md   Full documentation
- README.txt  Plain text quick guide
- AGENTS.md   Agent rules and workflow
