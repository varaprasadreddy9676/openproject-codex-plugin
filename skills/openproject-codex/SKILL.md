---
name: openproject-codex
description: Use the OpenProject Codex plugin to inspect and manage work in Codex through the OpenProject API.
---

# OpenProject Codex

Use this plugin when the user wants to work with OpenProject from Codex instead of using the OpenProject UI.

The plugin is intended to cover common project-management actions directly and then fall back to the generic authenticated API tool for any OpenProject module that is not yet represented by a dedicated MCP tool.

## Behavior

- Prefer the MCP tools from this plugin for project, team, membership, and work package operations.
- Default to the configured `OPENPROJECT_DEFAULT_PROJECT` when present, unless the user explicitly asks for another project.
- Use `openproject_connection_status` first if credentials or connectivity are uncertain.
- Use `openproject_list_types`, `openproject_list_statuses`, `openproject_list_priorities`, and `openproject_list_project_assignees` before write operations when a value needs to be discovered.
- Use dedicated tools first for projects, users, groups, memberships, versions, categories, queries, work packages, relations, watchers, comments, documents, news, time entries, file links, and bulk task operations.
- Prefer `openproject_my_work` for “my items” or “items assigned to me” requests.
- Prefer the query tools when the user refers to saved views or wants bulk actions across a query result set.
- Prefer the bulk tools for repeated edits instead of issuing many single-item calls one by one.
- If the user needs an OpenProject action that does not yet have a dedicated tool, use `openproject_call_api` against the relevant `/api/v3/...` endpoint instead of sending the user back to OpenProject UI.
- The official OpenProject `/mcp` endpoint is read-only, so write operations should continue to use this plugin's API tools.
- Boards are not currently covered by a stable API in this plugin; wiki and meetings are read-oriented here unless the target instance exposes more through custom endpoints.

## Required configuration

- `OPENPROJECT_API_TOKEN` is the preferred credential.
- `OPENPROJECT_API_TOKEN_FILE` is supported for local secret-file storage and is the recommended Codex desktop setup.
- `OPENPROJECT_BASIC_API_TOKEN` is supported as a fallback for legacy Basic auth with the `apikey` user.
- `OPENPROJECT_BASIC_API_TOKEN_FILE` is supported for the same legacy Basic-auth fallback.
- `OPENPROJECT_BASE_URL` is required.
- `OPENPROJECT_DEFAULT_PROJECT` is optional.

## Typical flow

1. Check connection status.
2. Inspect projects, users, groups, memberships, versions, categories, or work packages.
3. Create, update, relate, watch, comment on, assign, or delete work through the MCP tools.
4. Use the bulk tools when the same change must be applied to many items.
5. Use `openproject_call_api` for unsupported modules or advanced API workflows.
