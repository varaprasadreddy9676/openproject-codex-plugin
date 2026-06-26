#!/usr/bin/env python3
"""Read-only smoke test for the OpenProject Codex plugin."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_PATH = SCRIPT_DIR / "openproject_mcp.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("openproject_codex_mcp", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            raise RuntimeError(
                "Missing Python dependency 'mcp'. Install project dependencies first, "
                "for example with 'python3 -m pip install -e .'"
            ) from exc
        raise
    return module


def main() -> None:
    module = _load_module()
    results = {
        "connection": module.openproject_connection_status(),
        "projects": module.openproject_list_projects(page_size=3),
        "roles": module.openproject_list_roles()[:5],
        "users": module.openproject_list_users(page_size=5),
        "groups": module.openproject_list_groups(page_size=5),
        "my_assigned_work": module.openproject_my_work(kind="assigned", page_size=5),
    }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
