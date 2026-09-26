"""Structured, agent-friendly result envelopes and error handling.

Every MCP tool returns a JSON-serialisable dict shaped like:

    {"success": true,  "data": {...}}
    {"success": false, "error": "<short human message>", "details": "<why>"}

The contract the tools rely on:
  * AWS ClientError / BotoCoreError are always caught and converted.
  * A missing AWS resource is a *result*, never an exception, so the MCP
    server process never dies because a cluster does not exist.
  * Credential-looking strings are never echoed back.
"""

from __future__ import annotations

import re
from typing import Any

# Substrings that indicate a botocore message may embed sensitive material.
_SENSITIVE_HINTS = (
    "aws_secret_access_key",
    "aws_access_key_id",
    "aws_session_token",
    "authorization",
    "x-amz-security-token",
    "bearer ",
)

_SECRET_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),          # access key IDs
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+"),
    re.compile(r"(?i)aws_session_token\s*[=:]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
)


def redact(text: str) -> str:
    """Strip anything that looks like a credential from a string."""
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    """Build a success envelope."""
    payload: dict[str, Any] = {"success": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


def fail(error: str, details: str | None = None, **extra: Any) -> dict[str, Any]:
    """Build a failure envelope.

    `error` stays short and human-readable (it is what the agent reads first);
    `details` carries the diagnostic context.
    """
    payload: dict[str, Any] = {
        "success": False,
        "error": redact(str(error)),
    }
    payload["details"] = redact(details) if details else "No further details available."
    payload.update(extra)
    return payload


def tool_error(exc: Exception, context: str) -> dict[str, Any]:
    """Convert an arbitrary exception into a failure envelope.

    Uses botocore's own exception classes when available so we can produce
    genuinely useful messages (e.g. ClusterNotFoundException) instead of a
    generic "something went wrong".
    """
    try:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )
    except ImportError:  # pragma: no cover - boto3 is a hard dependency
        BotoCoreError = ClientError = None  # type: ignore[assignment]
        NoCredentialsError = PartialCredentialsError = ()  # type: ignore[assignment]

    if ClientError is not None and isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "AWSError")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        return fail(
            f"AWS API error during {context}: {code}",
            message,
            awsErrorCode=code,
        )

    if NoCredentialsError and isinstance(exc, NoCredentialsError):
        return fail(
            "No AWS credentials available",
            "boto3 could not locate credentials. Configure them via the AWS CLI, "
            "environment variables, or an instance/task role, or run with "
            "TICKET_RESOLVER_BACKEND=sim to use the simulated account.",
            awsErrorCode="NoCredentialsError",
        )

    if PartialCredentialsError and isinstance(exc, PartialCredentialsError):
        return fail(
            "Incomplete AWS credentials",
            "Only part of the credential chain resolved. Set both the access key "
            "ID and secret, or clear them and use a role/profile.",
            awsErrorCode="PartialCredentialsError",
        )

    if BotoCoreError is not None and isinstance(exc, BotoCoreError):
        return fail(
            f"AWS transport error during {context}",
            str(exc),
            awsErrorCode=type(exc).__name__,
        )

    return fail(
        f"Unexpected error during {context}",
        f"{type(exc).__name__}: {exc}",
    )
