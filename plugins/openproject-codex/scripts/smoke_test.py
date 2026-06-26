#!/usr/bin/env python3
"""Read-only smoke test for the OpenProject Codex plugin."""

from __future__ import annotations

import importlib.util
import json
import os
import time
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
    if os.environ.get("OPENPROJECT_SMOKE_WRITE") == "1":
        project = os.environ.get("OPENPROJECT_DEFAULT_PROJECT") or "pod-initiative"
        timestamp = str(int(time.time()))
        temp_board = module.openproject_create_board(project=project, name=f"Codex Smoke Board {timestamp}")
        temp_wiki = module.openproject_create_wiki_page(
            project=project,
            title=f"codex-smoke-{timestamp}",
            content="Created by the OpenProject Codex smoke test.",
        )
        temp_meeting = module.openproject_create_meeting(
            project=project,
            title=f"Codex Smoke Meeting {timestamp}",
            start_date="2026-06-27",
            start_time="10:00",
            duration_hours="1",
            location="Codex smoke test",
        )
        upload_path = Path("/tmp/openproject-codex-smoke.txt")
        upload_path.write_text("openproject codex smoke attachment\n", encoding="utf-8")
        meeting_attachment = module.openproject_upload_attachment(
            container_type="meeting",
            container_id=temp_meeting["id"],
            file_path=str(upload_path),
        )
        wiki_page = module.openproject_get_wiki_page_by_slug(project=project, page_slug=temp_wiki["slug"])
        wiki_attachment = module.openproject_upload_attachment(
            container_type="wiki_page",
            container_id=wiki_page["id"],
            file_path=str(upload_path),
        )
        bulk_results: dict[str, Any] = {"skipped": True}
        if os.environ.get("OPENPROJECT_SMOKE_WORK_PACKAGE_BULK") == "1":
            me = module.openproject_get_user("me")
            custom_option_href = os.environ.get("OPENPROJECT_SMOKE_CUSTOM_OPTION_HREF", "/api/v3/custom_options/21")
            work_package_a = module.openproject_create_work_package(
                subject=f"Codex Smoke WP A {timestamp}",
                project=project,
                link_overrides={"customField4": [custom_option_href]},
                notify=False,
            )
            work_package_b = module.openproject_create_work_package(
                subject=f"Codex Smoke WP B {timestamp}",
                project=project,
                link_overrides={"customField4": [custom_option_href]},
                notify=False,
            )
            work_package_ids = [work_package_a["id"], work_package_b["id"]]
            bulk_results = {
                "skipped": False,
                "createdWorkPackages": work_package_ids,
                "bulkUpdate": module.openproject_bulk_update_work_packages(
                    work_package_ids=work_package_ids,
                    note="Codex smoke bulk update",
                    notify=False,
                    stop_on_error=True,
                ),
                "bulkWatchAdd": module.openproject_bulk_manage_watchers(
                    work_package_ids=work_package_ids,
                    user_id=me["id"],
                    action="add",
                    stop_on_error=True,
                ),
                "bulkWatchRemove": module.openproject_bulk_manage_watchers(
                    work_package_ids=work_package_ids,
                    user_id=me["id"],
                    action="remove",
                    stop_on_error=True,
                ),
                "bulkComment": module.openproject_bulk_add_comment(
                    work_package_ids=work_package_ids,
                    comment="Codex smoke bulk comment",
                    notify=False,
                    stop_on_error=True,
                ),
            }
        module.openproject_delete_attachment(meeting_attachment["id"])
        module.openproject_delete_attachment(wiki_attachment["id"])
        module.openproject_delete_meeting(project=project, meeting_id=temp_meeting["id"])
        module.openproject_delete_wiki_page(project=project, page_slug=temp_wiki["slug"])
        module.openproject_delete_board(project=project, board_id=temp_board["id"])
        results["live_write_smoke"] = {
            "board": temp_board,
            "wiki": temp_wiki,
            "meeting": temp_meeting,
            "meetingAttachment": meeting_attachment,
            "wikiAttachment": wiki_attachment,
            "workPackageBulk": bulk_results,
        }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
