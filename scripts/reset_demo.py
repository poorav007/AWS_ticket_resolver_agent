#!/usr/bin/env python3
"""Reset the simulated AWS account back to the broken pre-demo state.

The demo is only convincing if the incident can be replayed. Run this between
demo attempts:

    python scripts/reset_demo.py

It deletes the simulated AWS state so the next backend start recreates the
incident (bad deployment, circuit breaker, 500s) with fresh timestamps.

Tickets are left alone; use --tickets to reset those too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-server"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickets",
        action="store_true",
        help="Also reset ticket files to OPEN with no resolution recorded.",
    )
    args = parser.parse_args()

    from config import CONFIG

    # Remove the state file so the sim rebuilds its timeline on next start.
    state_file = CONFIG.state_dir / "simulated-aws.json"
    if state_file.exists():
        state_file.unlink()
        print(f"removed simulated AWS state: {state_file}")
    else:
        print(f"no simulated AWS state to remove ({state_file})")

    if args.tickets:
        import json

        for path in sorted(CONFIG.tickets_dir.glob("*.json")):
            data = json.loads(path.read_text())
            data["status"] = "OPEN"
            data.pop("resolution", None)
            path.write_text(json.dumps(data, indent=2) + "\n")
            print(f"reset ticket: {path.name} -> OPEN")

    print("\nDemo state reset. The next MCP server start recreates the incident.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
