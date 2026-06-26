# OpenProject Codex Plugin

This plugin lets Codex work directly with any OpenProject instance through the OpenProject API, without relying on the OpenProject web UI for day-to-day project work.

## Current first-class tool coverage

- connection and project inspection
- project create, update, delete
- users and groups
- roles and memberships
- project members
- project versions
- project categories
- work package listing and search
- my assigned or authored work
- work package detail and raw payload inspection
- work package create, update, delete
- work package comments/activity
- work package relations
- work package watchers
- bulk work package updates, comments, deletes, and watcher changes
- bulk project membership changes for users and groups
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

Local verification:

```bash
python3 ./scripts/smoke_test.py
```

This performs read-only checks against connection status, projects, roles, users, groups, and your assigned work.
