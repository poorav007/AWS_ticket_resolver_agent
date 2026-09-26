"""Backend package for the Ticket Resolver MCP server.

Exposes :func:`get_backend`, which resolves the configured backend and falls
back to the simulated account when real credentials are unavailable.
"""

from __future__ import annotations

from backends.base import (
    AWSBackend,
    RemediationNotPermitted,
    ResourceNotFound,
    metric_key,
)

__all__ = [
    "AWSBackend",
    "RemediationNotPermitted",
    "ResourceNotFound",
    "metric_key",
    "get_backend",
]


def get_backend(config) -> AWSBackend:
    """Instantiate the backend selected by ``config.backend``.

    Modes:
        auto   - real AWS if credentials resolve, otherwise simulated
        boto3  - real AWS; raises if credentials are missing
        sim    - simulated account
    """
    mode = (config.backend or "auto").strip().lower()

    if mode == "sim":
        from backends.sim import SimulatedBackend

        return SimulatedBackend(config)

    if mode == "boto3":
        from backends.boto3_backend import Boto3Backend

        return Boto3Backend(config)

    if mode == "auto":
        from backends.boto3_backend import Boto3Backend
        from backends.sim import SimulatedBackend

        probe = Boto3Backend(config)
        reachable, message = probe.healthcheck()
        if reachable:
            return probe
        sim = SimulatedBackend(config)
        sim.fallback_reason = message
        return sim

    raise ValueError(
        f"Unknown TICKET_RESOLVER_BACKEND={config.backend!r}. "
        "Expected one of: auto, boto3, sim."
    )
