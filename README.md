# OpenProject Codex Plugin

This plugin lets Codex work directly with any OpenProject instance through the OpenProject API and, where the public API is still thin, through authenticated OpenProject UI workflows.

## Current first-class tool coverage

- connection and project inspection
- project create, update, delete
- users and groups
- roles and memberships
- project members
- project versions
- project categories
- query create, update, delete, run
- work package listing and search
- my assigned or authored work
- work package detail and raw payload inspection
- work package create, update, delete
- work package comments/activity
- work package relations
- work package watchers
- binary attachment upload plus attachment metadata and deletion
- work package file links
- time entry list, create, update, delete
- document list, fetch, update
- news list, fetch, create, update, delete
- boards list, create, delete
- wiki page list, fetch by slug, create, update, delete
- meeting list, fetch by id, create, delete
- bulk work package updates, comments, deletes, and watcher changes
- bulk work package actions by saved query
- bulk project membership changes for users and groups
- assignee workload reports
- saved-query burndown snapshots
- overdue dashboards by assignee or status
- project health export to HTML or PNG
- generic authenticated OpenProject API calls

## Why this exists

OpenProject exposes an official `/mcp` endpoint, but it is currently read-only. This plugin keeps read and write workflows inside Codex by using the authenticated OpenProject API directly.

## Required environment

- `OPENPROJECT_API_TOKEN` preferred
- `OPENPROJECT_API_TOKEN_FILE` recommended for local secret-file storage
- `OPENPROJECT_BASIC_API_TOKEN` fallback
- `OPENPROJECT_BASIC_API_TOKEN_FILE` fallback secret-file path
- `OPENPROJECT_BASE_URL` required
- `OPENPROJECT_DEFAULT_PROJECT` optional
- `OPENPROJECT_UI_USERNAME` and `OPENPROJECT_UI_PASSWORD` required for boards/wiki/meetings UI-backed tools
- `OPENPROJECT_UI_USERNAME_FILE` and `OPENPROJECT_UI_PASSWORD_FILE` supported for secret-file storage

## Fallback pattern

If a required OpenProject feature does not yet have a dedicated tool, use `openproject_call_api` with the matching `/api/v3/...` endpoint rather than switching back to the browser UI.

## Local verification

Example `.mcp.json`:

```bash
{
  "mcpServers": {
    "openproject_codex": {
      "command": "python3",
      "args": ["./scripts/openproject_mcp.py"],
      "cwd": ".",
      "env": {
        "OPENPROJECT_BASE_URL": "https://your-openproject.example.com",
        "OPENPROJECT_DEFAULT_PROJECT": "",
        "OPENPROJECT_API_TOKEN_FILE": "~/.codex/secrets/openproject-api-token"
      }
    }
  }
}
```

Install Python dependencies:

```bash
python3 -m pip install -e .
```

Local verification:

```bash
python3 ./scripts/smoke_test.py
```

This performs read-only checks against connection status, projects, roles, users, groups, and your assigned work.

To exercise the live write paths against a disposable test set:

```bash
OPENPROJECT_SMOKE_WRITE=1 python3 ./scripts/smoke_test.py
```

That write smoke test creates and removes a temporary board, wiki page, meeting, and wiki/meeting attachments.

Optional bulk work-package smoke:

```bash
OPENPROJECT_SMOKE_WRITE=1 OPENPROJECT_SMOKE_WORK_PACKAGE_BULK=1 python3 ./scripts/smoke_test.py
```

Use `OPENPROJECT_SMOKE_CUSTOM_OPTION_HREF` when your instance requires a specific custom-option link for new work packages.

## Reporting examples

- `openproject_report_assignee_workload(project="pod-initiative")`
- `openproject_report_burndown(query_id=123)`
- `openproject_dashboard_overdue_by_team(project="pod-initiative")`
- `openproject_export_project_health(project="pod-initiative", file_format="html")`
- `openproject_export_project_health(project="pod-initiative", file_format="png")`

## Known limits

- Board card and column manipulation is still not wrapped as a first-class tool surface; board creation and deletion are covered.
- Meeting editing beyond create/delete is still limited by the currently exposed instance workflows.
- If an instance enforces required custom fields on work packages, use `link_overrides` and `field_overrides` on the work package tools to supply those extra values.
