#!/usr/bin/env python3
"""Bootstrap the OpenProject MCP server in a local virtualenv."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".plugin-venv"
PYTHON = VENV_DIR / "bin" / "python3"
PIP = [str(PYTHON), "-m", "pip"]
REQUIRED_PACKAGES = [
    "httpx>=0.28.0",
    "matplotlib>=3.9.0",
    "mcp>=1.13.0",
]


def _venv_ready() -> bool:
    return PYTHON.exists()


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, cwd=ROOT)


def _ensure_venv() -> None:
    if not _venv_ready():
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True, cwd=ROOT)
        _run(PIP + ["install", "--upgrade", "pip"])
    marker = VENV_DIR / ".deps-ready"
    if not marker.exists():
        _run(PIP + ["install", *REQUIRED_PACKAGES])
        marker.write_text("ok\n", encoding="utf-8")


def main() -> None:
    _ensure_venv()
    env = os.environ.copy()
    os.execve(
        str(PYTHON),
        [str(PYTHON), str(ROOT / "scripts" / "openproject_mcp.py")],
        env,
    )


if __name__ == "__main__":
    main()
