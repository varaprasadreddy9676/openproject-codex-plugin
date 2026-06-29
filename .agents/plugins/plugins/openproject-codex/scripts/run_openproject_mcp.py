#!/usr/bin/env python3
"""Launch the bundled OpenProject MCP server without external bootstrap."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    os.execve(
        sys.executable,
        [sys.executable, str(root / "scripts" / "openproject_mcp.py")],
        os.environ.copy(),
    )


if __name__ == "__main__":
    main()
