"""Configuration for the Ticket Resolver MCP server.

All settings come from environment variables with sane defaults so the server
can run with zero configuration. Nothing secret is stored here: AWS credentials
are resolved exclusively through boto3's default credential chain.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# mcp-server/config.py -> project root is the parent directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    # --- Backend selection -------------------------------------------------
    # "auto"    -> use real AWS when credentials resolve, else fall back to sim
    # "boto3"   -> always use real AWS (fail loudly if creds are missing)
    # "sim"     -> always use the built-in simulated account
    backend: str = field(default_factory=lambda: os.environ.get(
        "TICKET_RESOLVER_BACKEND", "auto"
    ).strip().lower())

    region: str = field(default_factory=lambda: os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    ))

    # --- Local data --------------------------------------------------------
    tickets_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("TICKET_RESOLVER_TICKETS_DIR", str(PROJECT_ROOT / "tickets"))
    ).expanduser())

    runbooks_dir: Path = field(default_factory=lambda: Path(
        os.environ.get(
            "TICKET_RESOLVER_RUNBOOKS_DIR", str(PROJECT_ROOT / "runbooks")
        )
    ).expanduser())

    # Mutable state for the simulated backend (and ticket status updates).
    state_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("TICKET_RESOLVER_STATE_DIR", str(PROJECT_ROOT / "state"))
    ).expanduser())

    # --- Demo / default targets -------------------------------------------
    default_cluster: str = field(default_factory=lambda: os.environ.get(
        "TICKET_RESOLVER_CLUSTER", "hackathon-cluster"
    ))

    default_log_group: str = field(default_factory=lambda: os.environ.get(
        "TICKET_RESOLVER_LOG_GROUP", "/ecs/payment-api"
    ))

    # --- Guard rails -------------------------------------------------------
    # Cap on how many log events a single tool call may return, so the agent's
    # context window is never flooded by a chatty log group.
    max_log_events: int = field(
        default_factory=lambda: _env_int("TICKET_RESOLVER_MAX_LOG_EVENTS", 50))

    # Remediations are refused unless explicitly enabled. Keep this True during
    # local development; TrueForge enforces the human-approval checkpoint on
    # top of this, so the two layers are independent.
    allow_remediation: bool = field(
        default_factory=lambda: _env_bool("TICKET_RESOLVER_ALLOW_REMEDIATION", True))

    def ensure_state_dir(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir


CONFIG = Config()
