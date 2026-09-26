"""Local ticket store.

Tickets are plain JSON files in ``tickets/``. Keeping them on disk (rather than
in memory) means the agent's status updates and resolution notes survive MCP
server restarts, and a human can read or edit them directly.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import Ticket

log = logging.getLogger("ticket-resolver.tickets")

# Ticket IDs are used to build a filename, so constrain them tightly.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

VALID_STATUSES = {"OPEN", "INVESTIGATING", "AWAITING_APPROVAL", "REMEDIATING", "RESOLVED"}


class TicketStore:
    """Reads and writes tickets as JSON files."""

    def __init__(self, tickets_dir: Path) -> None:
        self._dir = Path(tickets_dir)

    # -- reads ------------------------------------------------------------
    def list_ids(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.json"))

    def get(self, ticket_id: str) -> Ticket:
        """Load one ticket by ID (e.g. ``INC-1001``)."""
        if not ticket_id or not _SAFE_ID.match(ticket_id):
            raise ValueError(
                f"Invalid ticket id {ticket_id!r}. Expected something like INC-1001."
            )

        path = self._dir / f"{ticket_id}.json"
        if not path.exists():
            available = ", ".join(self.list_ids()) or "none"
            raise FileNotFoundError(
                f"Ticket {ticket_id!r} not found. Available tickets: {available}"
            )

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ticket file {path.name} is not valid JSON: {exc}") from exc

        return Ticket.from_dict(raw, ticket_id=ticket_id)

    # -- writes -----------------------------------------------------------
    def update_status(
        self,
        ticket_id: str,
        status: str,
        resolution: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> Ticket:
        """Update a ticket's status, optionally attaching a resolution record."""
        status = status.upper()
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}. Valid: {', '.join(sorted(VALID_STATUSES))}"
            )

        ticket = self.get(ticket_id)  # validates id + existence
        path = self._dir / f"{ticket.id}.json"
        raw = json.loads(path.read_text())

        raw["status"] = status
        raw["updatedAt"] = datetime.now(timezone.utc).isoformat()

        if note:
            history = raw.setdefault("history", [])
            history.append(
                {
                    "at": raw["updatedAt"],
                    "status": status,
                    "note": note,
                }
            )

        if resolution is not None:
            merged = dict(raw.get("resolution") or {})
            merged.update(resolution)
            raw["resolution"] = merged

        path.write_text(json.dumps(raw, indent=2) + "\n")
        log.info("ticket %s -> %s", ticket.id, status)
        return Ticket.from_dict(raw, ticket_id=ticket.id)
