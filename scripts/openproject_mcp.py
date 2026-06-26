#!/usr/bin/env python3
"""MCP server for OpenProject Codex."""

from __future__ import annotations

import json
import os
from typing import Any
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP


BASE_URL = os.environ.get("OPENPROJECT_BASE_URL", "").rstrip("/")
DEFAULT_PROJECT = os.environ.get("OPENPROJECT_DEFAULT_PROJECT", "").strip() or None
API_ROOT = f"{BASE_URL}/api/v3" if BASE_URL else ""
USER_AGENT = "openproject-codex-plugin/0.1.0"

mcp = FastMCP(
    "openproject_codex",
    instructions=(
        "Tools for working with the OpenProject API from Codex. "
        "Use these tools to inspect projects, list work packages, create tasks, "
        "update tasks, and add comments without using the OpenProject UI."
    ),
)


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("OPENPROJECT_API_TOKEN") or _read_secret_from_env_file("OPENPROJECT_API_TOKEN_FILE")
    basic_token = os.environ.get("OPENPROJECT_BASIC_API_TOKEN") or _read_secret_from_env_file(
        "OPENPROJECT_BASIC_API_TOKEN_FILE"
    )
    if token:
        return {"Authorization": f"Bearer {token}"}
    if basic_token:
        import base64

        encoded = base64.b64encode(f"apikey:{basic_token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    raise RuntimeError(
        "Missing OpenProject credentials. Set OPENPROJECT_API_TOKEN "
        "(preferred), OPENPROJECT_API_TOKEN_FILE, OPENPROJECT_BASIC_API_TOKEN, "
        "or OPENPROJECT_BASIC_API_TOKEN_FILE."
    )


def _read_secret_from_env_file(env_var: str) -> str | None:
    secret_path = os.environ.get(env_var)
    if not secret_path:
        return None
    path = Path(secret_path).expanduser()
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/hal+json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **_auth_headers(),
    }


def _client() -> httpx.Client:
    if not BASE_URL:
        raise RuntimeError("Missing OPENPROJECT_BASE_URL. Set it to your OpenProject instance URL.")
    return httpx.Client(headers=_headers(), timeout=30.0, follow_redirects=True)


def _api_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.get(f"{API_ROOT}{path}", params=params)
    return _decode_response(response)


def _api_post(
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _client() as client:
        response = client.post(f"{API_ROOT}{path}", params=params, json=body or {})
    return _decode_response(response)


def _api_patch(
    path: str,
    *,
    body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _client() as client:
        response = client.patch(f"{API_ROOT}{path}", params=params, json=body)
    return _decode_response(response)


def _api_delete(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as client:
        response = client.delete(f"{API_ROOT}{path}", params=params)
    return _decode_response(response)


def _api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    normalized_method = method.upper()
    normalized_path = path if path.startswith("/") else f"/{path}"
    if normalized_path.startswith("/api/v3/"):
        normalized_path = normalized_path.removeprefix("/api/v3")
    with _client() as client:
        response = client.request(
            normalized_method,
            f"{API_ROOT}{normalized_path}",
            params=params,
            json=body,
        )
    return _decode_response(response)


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}

    if response.is_success:
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    message = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("errorIdentifier")
    raise RuntimeError(f"OpenProject API error {response.status_code}: {message or response.text}")


def _collection_elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("_embedded", {}).get("elements", [])


def _link_href(payload: dict[str, Any], key: str) -> str | None:
    href = payload.get("_links", {}).get(key, {}).get("href")
    if isinstance(href, str) and href:
        return href
    return None


def _formattable(raw: str | None) -> dict[str, str] | None:
    if raw is None:
        return None
    return {"format": "markdown", "raw": raw}


def _normalize_project_ref(project: str | int | None) -> str:
    value = project or DEFAULT_PROJECT
    if value is None:
        raise RuntimeError(
            "No project was provided and OPENPROJECT_DEFAULT_PROJECT is not configured."
        )
    return str(value)


def _resolve_project(project: str | int | None) -> dict[str, Any]:
    ref = _normalize_project_ref(project)
    try:
        return _api_get(f"/projects/{ref}")
    except Exception:
        filters = json.dumps([{"identifier": {"operator": "=", "values": [ref]}}])
        payload = _api_get("/projects", params={"filters": filters, "pageSize": 100})
        for candidate in _collection_elements(payload):
            if str(candidate.get("identifier")) == ref or candidate.get("name") == ref:
                return candidate
    raise RuntimeError(f"Could not resolve OpenProject project '{ref}'.")


def _resource_collection(path: str, *, page_size: int = 100) -> list[dict[str, Any]]:
    payload = _api_get(path, params={"pageSize": page_size})
    return _collection_elements(payload)


def _find_named_resource(path: str, name: str, *, title_keys: tuple[str, ...] = ("name",)) -> dict[str, Any]:
    lowered = name.strip().lower()
    for item in _resource_collection(path):
        for key in title_keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip().lower() == lowered:
                return item
    raise RuntimeError(f"Could not find resource '{name}' at {path}.")


def _work_package_summary(payload: dict[str, Any]) -> dict[str, Any]:
    links = payload.get("_links", {})
    return {
        "id": payload.get("id"),
        "subject": payload.get("subject"),
        "type": links.get("type", {}).get("title"),
        "status": links.get("status", {}).get("title"),
        "priority": links.get("priority", {}).get("title"),
        "project": links.get("project", {}).get("title"),
        "assignee": links.get("assignee", {}).get("title"),
        "author": links.get("author", {}).get("title"),
        "startDate": payload.get("startDate"),
        "dueDate": payload.get("dueDate"),
        "percentageDone": payload.get("percentageDone"),
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "lockVersion": payload.get("lockVersion"),
        "href": _link_href(payload, "self"),
    }


def _set_link(payload_links: dict[str, Any], key: str, href: str | None) -> None:
    if href is None:
        payload_links[key] = None
        return
    payload_links[key] = {"href": href}


def _resource_href(path: str, name: str, *, title_keys: tuple[str, ...] = ("name",)) -> str:
    resource = _find_named_resource(path, name, title_keys=title_keys)
    href = _link_href(resource, "self")
    if not href:
        raise RuntimeError(f"Resource '{name}' at {path} is missing a self link.")
    return href


def _project_id(project: str | int | None) -> int:
    return int(_resolve_project(project)["id"])


def _normalize_href(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        if href.startswith(BASE_URL):
            return href[len(BASE_URL) :]
        raise RuntimeError(f"External href is not allowed: {href}")
    return href


def _filter_by_names(key: str, names: list[str] | None) -> list[dict[str, Any]]:
    if not names:
        return []
    return [{key: {"operator": "=", "values": names}}]


def _filter_by_ids(key: str, ids: list[int] | None) -> list[dict[str, Any]]:
    if not ids:
        return []
    return [{key: {"operator": "=", "values": [str(int(value)) for value in ids]}}]


def _title_and_href(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "href": _link_href(payload, "self"),
    }


def _role_href(role_name: str) -> str:
    return _resource_href("/roles", role_name)


def _resolve_role(role_name: str) -> dict[str, Any]:
    return _find_named_resource("/roles", role_name)


def _normalize_search(value: str) -> str:
    return value.strip().lower()


def _user_matches(item: dict[str, Any], query: str) -> bool:
    needle = _normalize_search(query)
    haystacks = [
        item.get("name"),
        item.get("login"),
        item.get("firstName"),
        item.get("lastName"),
        item.get("email"),
    ]
    return any(isinstance(value, str) and needle in value.lower() for value in haystacks)


def _group_matches(item: dict[str, Any], query: str) -> bool:
    needle = _normalize_search(query)
    return needle in str(item.get("name", "")).lower()


def _project_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "identifier": payload.get("identifier"),
        "name": payload.get("name"),
        "active": payload.get("active"),
        "public": payload.get("public"),
        "description": payload.get("description"),
        "statusExplanation": payload.get("statusExplanation"),
        "href": _link_href(payload, "self"),
    }


def _role_links(role_names: list[str]) -> list[dict[str, str]]:
    return [{"href": _role_href(role_name)} for role_name in role_names]


def _membership_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "project": payload.get("_links", {}).get("project", {}).get("title"),
        "principal": payload.get("_links", {}).get("principal", {}).get("title"),
        "principalHref": payload.get("_links", {}).get("principal", {}).get("href"),
        "roles": [role.get("title") for role in payload.get("_links", {}).get("roles", []) if isinstance(role, dict)],
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "href": _link_href(payload, "self"),
    }


def _watcher_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "login": payload.get("login"),
        "email": payload.get("email"),
        "href": _link_href(payload, "self"),
    }


def _batch_result(
    action: str,
    inputs: list[dict[str, Any]],
    handler: Any,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    success_count = 0
    error_count = 0
    for item in inputs:
        try:
            output = handler(item)
            results.append({"ok": True, "input": item, "result": output})
            success_count += 1
        except Exception as exc:
            results.append({"ok": False, "input": item, "error": str(exc)})
            error_count += 1
            if stop_on_error:
                break
    return {
        "action": action,
        "total": len(inputs),
        "successCount": success_count,
        "errorCount": error_count,
        "results": results,
    }


@mcp.tool()
def openproject_connection_status() -> dict[str, Any]:
    """Return plugin configuration and verify API connectivity using the authenticated account."""
    payload = _api_get("/")
    return {
        "base_url": BASE_URL,
        "api_root": API_ROOT,
        "default_project": DEFAULT_PROJECT,
        "authenticated": True,
        "instance": payload,
    }


@mcp.tool()
def openproject_call_api(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    """Call any OpenProject API v3 endpoint through the configured authenticated session."""
    return _api_request(method, path, params=query, body=body)


@mcp.tool()
def openproject_list_projects(search: str | None = None, page_size: int = 20, offset: int = 1) -> dict[str, Any]:
    """List visible OpenProject projects/workspaces."""
    params: dict[str, Any] = {"pageSize": max(1, min(page_size, 100)), "offset": max(1, offset)}
    if search:
        params["filters"] = json.dumps(
            [{"nameAndIdentifier": {"operator": "**", "values": [search]}}]
        )
    payload = _api_get("/projects", params=params)
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "projects": [
            {
                "id": item.get("id"),
                "identifier": item.get("identifier"),
                "name": item.get("name"),
                "active": item.get("active"),
                "statusExplanation": item.get("statusExplanation"),
                "href": _link_href(item, "self"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_get_project(project: str | int | None = None) -> dict[str, Any]:
    """Fetch a project by identifier, numeric id, or configured default project."""
    project_obj = _resolve_project(project)
    summary = _project_summary(project_obj)
    summary["links"] = project_obj.get("_links", {})
    return summary


@mcp.tool()
def openproject_create_project(
    name: str,
    identifier: str,
    description: str | None = None,
    public: bool | None = None,
    active: bool | None = None,
    parent_project: str | int | None = None,
) -> dict[str, Any]:
    """Create an OpenProject project."""
    body: dict[str, Any] = {"name": name, "identifier": identifier}
    if description is not None:
        body["description"] = _formattable(description)
    if public is not None:
        body["public"] = bool(public)
    if active is not None:
        body["active"] = bool(active)
    if parent_project is not None:
        body.setdefault("_links", {})["parent"] = {"href": _link_href(_resolve_project(parent_project), "self")}
    created = _api_post("/projects", body=body)
    return _project_summary(created)


@mcp.tool()
def openproject_update_project(
    project: str | int,
    name: str | None = None,
    identifier: str | None = None,
    description: str | None = None,
    public: bool | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """Update an OpenProject project."""
    project_obj = _resolve_project(project)
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if identifier is not None:
        body["identifier"] = identifier
    if description is not None:
        body["description"] = _formattable(description)
    if public is not None:
        body["public"] = bool(public)
    if active is not None:
        body["active"] = bool(active)
    updated = _api_patch(f"/projects/{project_obj['id']}", body=body)
    return _project_summary(updated)


@mcp.tool()
def openproject_delete_project(project: str | int) -> dict[str, Any]:
    """Delete an OpenProject project."""
    project_obj = _resolve_project(project)
    result = _api_delete(f"/projects/{project_obj['id']}")
    return {"deleted": True, "project": _project_summary(project_obj), "result": result}


@mcp.tool()
def openproject_list_roles() -> list[dict[str, Any]]:
    """List project roles available in the instance."""
    return [_title_and_href(item) for item in _resource_collection("/roles")]


@mcp.tool()
def openproject_list_users(search: str | None = None, page_size: int = 100, offset: int = 1) -> dict[str, Any]:
    """List users and optionally filter them client-side by name, login, or email."""
    payload = _api_get(
        "/users",
        params={"pageSize": max(1, min(page_size, 200)), "offset": max(1, offset)},
    )
    users = _collection_elements(payload)
    if search:
        users = [item for item in users if _user_matches(item, search)]
    return {
        "total": payload.get("total"),
        "count": len(users),
        "users": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "login": item.get("login"),
                "email": item.get("email"),
                "status": item.get("status"),
                "href": _link_href(item, "self"),
            }
            for item in users
        ],
    }


@mcp.tool()
def openproject_get_user(user_id: int | str) -> dict[str, Any]:
    """Fetch a user by numeric id or `me`."""
    payload = _api_get(f"/users/{user_id}")
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "login": payload.get("login"),
        "firstName": payload.get("firstName"),
        "lastName": payload.get("lastName"),
        "email": payload.get("email"),
        "status": payload.get("status"),
        "href": _link_href(payload, "self"),
        "links": payload.get("_links", {}),
    }


@mcp.tool()
def openproject_list_groups(search: str | None = None, page_size: int = 100, offset: int = 1) -> dict[str, Any]:
    """List groups and optionally filter them by name."""
    payload = _api_get(
        "/groups",
        params={"pageSize": max(1, min(page_size, 200)), "offset": max(1, offset)},
    )
    groups = _collection_elements(payload)
    if search:
        groups = [item for item in groups if _group_matches(item, search)]
    return {
        "total": payload.get("total"),
        "count": len(groups),
        "groups": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "members": [
                    {"href": member.get("href"), "title": member.get("title")}
                    for member in item.get("_links", {}).get("members", [])
                    if isinstance(member, dict)
                ],
                "href": _link_href(item, "self"),
            }
            for item in groups
        ],
    }


@mcp.tool()
def openproject_get_group(group_id: int) -> dict[str, Any]:
    """Fetch a group by numeric id."""
    payload = _api_get(f"/groups/{int(group_id)}")
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "members": [
            {"href": member.get("href"), "title": member.get("title")}
            for member in payload.get("_links", {}).get("members", [])
            if isinstance(member, dict)
        ],
        "href": _link_href(payload, "self"),
        "links": payload.get("_links", {}),
    }


@mcp.tool()
def openproject_list_statuses() -> list[dict[str, Any]]:
    """List all work package statuses available in the instance."""
    return [
        {"id": item.get("id"), "name": item.get("name"), "href": _link_href(item, "self")}
        for item in _resource_collection("/statuses")
    ]


@mcp.tool()
def openproject_list_types() -> list[dict[str, Any]]:
    """List all work package types available in the instance."""
    return [
        {"id": item.get("id"), "name": item.get("name"), "href": _link_href(item, "self")}
        for item in _resource_collection("/types")
    ]


@mcp.tool()
def openproject_list_priorities() -> list[dict[str, Any]]:
    """List all work package priorities available in the instance."""
    return [
        {"id": item.get("id"), "name": item.get("name"), "href": _link_href(item, "self")}
        for item in _resource_collection("/priorities")
    ]


@mcp.tool()
def openproject_list_project_assignees(
    project: str | int | None = None, page_size: int = 100
) -> list[dict[str, Any]]:
    """List assignees available for a project."""
    project_obj = _resolve_project(project)
    project_id = project_obj["id"]
    payload = _api_get(
        f"/projects/{project_id}/available_assignees",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "login": item.get("login"),
            "href": _link_href(item, "self"),
        }
        for item in _collection_elements(payload)
    ]


@mcp.tool()
def openproject_list_project_members(project: str | int | None = None, page_size: int = 100) -> dict[str, Any]:
    """List project memberships visible to the current user."""
    project_id = _project_id(project)
    payload = _api_get(
        f"/projects/{project_id}/memberships",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "memberships": [
            {
                "id": item.get("id"),
                "principal": item.get("_links", {}).get("principal", {}).get("title"),
                "roles": [
                    role.get("title")
                    for role in item.get("_embedded", {}).get("roles", {}).get("elements", [])
                    if isinstance(role, dict)
                ],
                "href": _link_href(item, "self"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_list_project_versions(project: str | int | None = None, page_size: int = 100) -> dict[str, Any]:
    """List versions/milestones available in a project."""
    project_id = _project_id(project)
    payload = _api_get(
        f"/projects/{project_id}/versions",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "versions": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "status": item.get("status"),
                "sharing": item.get("sharing"),
                "startDate": item.get("startDate"),
                "endDate": item.get("endDate"),
                "href": _link_href(item, "self"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_list_project_categories(project: str | int | None = None, page_size: int = 100) -> dict[str, Any]:
    """List work package categories in a project."""
    project_id = _project_id(project)
    payload = _api_get(
        f"/projects/{project_id}/categories",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "categories": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "assignedTo": item.get("_links", {}).get("assignedTo", {}).get("title"),
                "href": _link_href(item, "self"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_list_memberships(
    project: str | int | None = None,
    principal_id: int | None = None,
    page_size: int = 100,
    offset: int = 1,
) -> dict[str, Any]:
    """List memberships, optionally filtered by project and principal id."""
    filters: list[dict[str, Any]] = []
    if project is not None:
        filters.append({"project": {"operator": "=", "values": [str(_project_id(project))]}})
    if principal_id is not None:
        filters.append({"principal": {"operator": "=", "values": [str(int(principal_id))]}})
    payload = _api_get(
        "/memberships",
        params={
            "pageSize": max(1, min(page_size, 200)),
            "offset": max(1, offset),
            "filters": json.dumps(filters),
        },
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "memberships": [_membership_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_get_membership(membership_id: int) -> dict[str, Any]:
    """Fetch a membership by id."""
    return _membership_summary(_api_get(f"/memberships/{int(membership_id)}"))


@mcp.tool()
def openproject_create_membership(
    project: str | int,
    role_names: list[str],
    user_id: int | None = None,
    group_id: int | None = None,
) -> dict[str, Any]:
    """Add a user or group to a project with one or more roles."""
    if (user_id is None) == (group_id is None):
        raise RuntimeError("Provide exactly one of user_id or group_id.")
    principal_href = f"/api/v3/users/{int(user_id)}" if user_id is not None else f"/api/v3/groups/{int(group_id)}"
    body = {
        "_links": {
            "project": {"href": _link_href(_resolve_project(project), "self")},
            "principal": {"href": principal_href},
            "roles": _role_links(role_names),
        }
    }
    payload = _api_post("/memberships", body=body)
    return _membership_summary(payload)


@mcp.tool()
def openproject_update_membership(membership_id: int, role_names: list[str]) -> dict[str, Any]:
    """Replace the roles assigned to a membership."""
    payload = _api_patch(
        f"/memberships/{int(membership_id)}",
        body={"_links": {"roles": _role_links(role_names)}},
    )
    return _membership_summary(payload)


@mcp.tool()
def openproject_delete_membership(membership_id: int) -> dict[str, Any]:
    """Delete a project membership."""
    result = _api_delete(f"/memberships/{int(membership_id)}")
    return {"deleted": True, "membershipId": int(membership_id), "result": result}


@mcp.tool()
def openproject_list_queries(project: str | int | None = None, page_size: int = 100) -> dict[str, Any]:
    """List saved work package queries globally or within a project."""
    params = {"pageSize": max(1, min(page_size, 200))}
    if project is None:
        payload = _api_get("/queries", params=params)
    else:
        payload = _api_get(f"/projects/{_project_id(project)}/queries", params=params)
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "queries": [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "public": item.get("public"),
                "href": _link_href(item, "self"),
                "resultsHref": item.get("_links", {}).get("results", {}).get("href"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_get_query(query_id: int) -> dict[str, Any]:
    """Fetch a saved query definition."""
    return _api_get(f"/queries/{int(query_id)}")


@mcp.tool()
def openproject_run_query(query_id: int, page_size: int = 50, offset: int = 1) -> dict[str, Any]:
    """Run a saved query and return its work package results."""
    query_payload = _api_get(f"/queries/{int(query_id)}")
    results_href = query_payload.get("_links", {}).get("results", {}).get("href")
    if not results_href:
        raise RuntimeError(f"Query {query_id} does not expose a results link.")
    payload = _api_get(_normalize_href(results_href), params={"pageSize": max(1, min(page_size, 100)), "offset": max(1, offset)})
    return {
        "query": {
            "id": query_payload.get("id"),
            "name": query_payload.get("name"),
            "href": _link_href(query_payload, "self"),
        },
        "total": payload.get("total"),
        "count": payload.get("count"),
        "work_packages": [_work_package_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_my_work(
    project: str | int | None = None,
    kind: str = "assigned",
    open_only: bool = True,
    page_size: int = 20,
    offset: int = 1,
) -> dict[str, Any]:
    """List work packages for the current user, such as assigned or authored items."""
    normalized_kind = kind.strip().lower()
    if normalized_kind not in {"assigned", "authored"}:
        raise RuntimeError("kind must be 'assigned' or 'authored'.")
    filters: list[dict[str, Any]] = []
    if normalized_kind == "assigned":
        filters.append({"assignee": {"operator": "=", "values": ["me"]}})
    else:
        filters.append({"author": {"operator": "=", "values": ["me"]}})
    if open_only:
        filters.append({"status": {"operator": "o", "values": []}})
    return openproject_list_work_packages(
        project=project,
        filters=filters,
        page_size=page_size,
        offset=offset,
    )


@mcp.tool()
def openproject_list_work_packages(
    project: str | int | None = None,
    filters: list[dict[str, Any]] | None = None,
    page_size: int = 20,
    offset: int = 1,
) -> dict[str, Any]:
    """List work packages, optionally scoped to a project and OpenProject filter objects."""
    params: dict[str, Any] = {"pageSize": max(1, min(page_size, 100)), "offset": max(1, offset)}
    if filters:
        params["filters"] = json.dumps(filters)
    if project is None:
        payload = _api_get("/work_packages", params=params)
    else:
        project_obj = _resolve_project(project)
        payload = _api_get(f"/projects/{project_obj['id']}/work_packages", params=params)
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "work_packages": [_work_package_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_search_work_packages(
    query: str | None = None,
    project: str | int | None = None,
    status_names: list[str] | None = None,
    assignee_ids: list[int] | None = None,
    type_names: list[str] | None = None,
    priority_names: list[str] | None = None,
    page_size: int = 20,
    offset: int = 1,
) -> dict[str, Any]:
    """Search work packages using common UI-style filters without hand-building raw filter JSON."""
    filters: list[dict[str, Any]] = []
    if query:
        filters.append({"subjectOrId": {"operator": "**", "values": [query]}})
    filters.extend(_filter_by_names("status", status_names))
    filters.extend(_filter_by_ids("assignee", assignee_ids))
    filters.extend(_filter_by_names("type", type_names))
    filters.extend(_filter_by_names("priority", priority_names))
    return openproject_list_work_packages(
        project=project,
        filters=filters or None,
        page_size=page_size,
        offset=offset,
    )


@mcp.tool()
def openproject_get_work_package(work_package_id: int) -> dict[str, Any]:
    """Fetch a full work package including description and links."""
    payload = _api_get(f"/work_packages/{int(work_package_id)}")
    summary = _work_package_summary(payload)
    summary["description"] = payload.get("description", {})
    summary["links"] = payload.get("_links", {})
    return summary


@mcp.tool()
def openproject_get_work_package_raw(work_package_id: int) -> dict[str, Any]:
    """Fetch the raw work package payload including embedded resources and all links."""
    return _api_get(f"/work_packages/{int(work_package_id)}")


@mcp.tool()
def openproject_list_work_package_activities(work_package_id: int, page_size: int = 20) -> dict[str, Any]:
    """List comments/activity entries for a work package."""
    payload = _api_get(
        f"/work_packages/{int(work_package_id)}/activities",
        params={"pageSize": max(1, min(page_size, 100))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "activities": [
            {
                "id": item.get("id"),
                "user": item.get("_links", {}).get("user", {}).get("title"),
                "createdAt": item.get("createdAt"),
                "updatedAt": item.get("updatedAt"),
                "comment": item.get("comment", {}),
                "details": item.get("details", []),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_list_work_package_relations(work_package_id: int, page_size: int = 100) -> dict[str, Any]:
    """List relations for a work package."""
    payload = _api_get(
        f"/work_packages/{int(work_package_id)}/relations",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "relations": [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "reverseType": item.get("reverseType"),
                "description": item.get("description"),
                "lag": item.get("lag"),
                "from": item.get("_links", {}).get("from", {}).get("title"),
                "to": item.get("_links", {}).get("to", {}).get("title"),
                "href": _link_href(item, "self"),
            }
            for item in _collection_elements(payload)
        ],
    }


@mcp.tool()
def openproject_list_work_package_watchers(work_package_id: int, page_size: int = 100) -> dict[str, Any]:
    """List watchers for a work package."""
    payload = _api_get(
        f"/work_packages/{int(work_package_id)}/watchers",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "watchers": [_watcher_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_add_watcher(work_package_id: int, user_id: int) -> dict[str, Any]:
    """Add a watcher to a work package."""
    payload = _api_post(
        f"/work_packages/{int(work_package_id)}/watchers",
        body={"user": {"href": f"/api/v3/users/{int(user_id)}"}},
    )
    return _watcher_summary(payload)


@mcp.tool()
def openproject_remove_watcher(work_package_id: int, user_id: int) -> dict[str, Any]:
    """Remove a watcher from a work package."""
    result = _api_delete(f"/work_packages/{int(work_package_id)}/watchers/{int(user_id)}")
    return {
        "deleted": True,
        "workPackageId": int(work_package_id),
        "userId": int(user_id),
        "result": result,
    }


@mcp.tool()
def openproject_create_work_package(
    subject: str,
    project: str | int | None = None,
    description: str | None = None,
    type_name: str = "Task",
    assignee_id: int | None = None,
    status_name: str | None = None,
    priority_name: str | None = None,
    parent_id: int | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Create a work package in OpenProject."""
    project_obj = _resolve_project(project)
    payload: dict[str, Any] = {
        "subject": subject,
        "description": _formattable(description),
        "_links": {
            "project": {"href": _link_href(project_obj, "self")},
            "type": {"href": _resource_href("/types", type_name)},
        },
    }
    if start_date is not None:
        payload["startDate"] = start_date
    if due_date is not None:
        payload["dueDate"] = due_date
    if status_name is not None:
        payload["_links"]["status"] = {"href": _resource_href("/statuses", status_name)}
    if priority_name is not None:
        payload["_links"]["priority"] = {"href": _resource_href("/priorities", priority_name)}
    if assignee_id is not None:
        payload["_links"]["assignee"] = {"href": f"/api/v3/users/{int(assignee_id)}"}
    if parent_id is not None:
        payload["_links"]["parent"] = {"href": f"/api/v3/work_packages/{int(parent_id)}"}

    created = _api_post("/work_packages", body=payload, params={"notify": str(bool(notify)).lower()})
    return _work_package_summary(created)


@mcp.tool()
def openproject_create_work_package_relation(
    from_work_package_id: int,
    to_work_package_id: int,
    relation_type: str,
    lag: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a relation between two work packages."""
    body: dict[str, Any] = {
        "_links": {"to": {"href": f"/api/v3/work_packages/{int(to_work_package_id)}"}},
        "type": relation_type,
    }
    if lag is not None:
        body["lag"] = int(lag)
    if description is not None:
        body["description"] = description
    payload = _api_post(f"/work_packages/{int(from_work_package_id)}/relations", body=body)
    return {
        "id": payload.get("id"),
        "type": payload.get("type"),
        "reverseType": payload.get("reverseType"),
        "description": payload.get("description"),
        "lag": payload.get("lag"),
        "from": payload.get("_links", {}).get("from", {}).get("title"),
        "to": payload.get("_links", {}).get("to", {}).get("title"),
        "href": _link_href(payload, "self"),
    }


@mcp.tool()
def openproject_update_work_package(
    work_package_id: int,
    subject: str | None = None,
    description: str | None = None,
    assignee_id: int | None = None,
    status_name: str | None = None,
    priority_name: str | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    percentage_done: int | None = None,
    note: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Update an existing work package using optimistic locking."""
    current = _api_get(f"/work_packages/{int(work_package_id)}")
    payload: dict[str, Any] = {"lockVersion": current.get("lockVersion")}
    links: dict[str, Any] = {}

    if subject is not None:
        payload["subject"] = subject
    if description is not None:
        payload["description"] = _formattable(description)
    if start_date is not None:
        payload["startDate"] = start_date
    if due_date is not None:
        payload["dueDate"] = due_date
    if percentage_done is not None:
        payload["percentageDone"] = int(percentage_done)
    if note is not None:
        payload["comment"] = _formattable(note)
    if status_name is not None:
        links["status"] = {"href": _resource_href("/statuses", status_name)}
    if priority_name is not None:
        links["priority"] = {"href": _resource_href("/priorities", priority_name)}
    if assignee_id is not None:
        links["assignee"] = {"href": f"/api/v3/users/{int(assignee_id)}"}

    if links:
        payload["_links"] = links

    updated = _api_patch(
        f"/work_packages/{int(work_package_id)}",
        body=payload,
        params={"notify": str(bool(notify)).lower()},
    )
    return _work_package_summary(updated)


@mcp.tool()
def openproject_delete_work_package(work_package_id: int) -> dict[str, Any]:
    """Delete a work package."""
    result = _api_delete(f"/work_packages/{int(work_package_id)}")
    return {"deleted": True, "work_package_id": int(work_package_id), "result": result}


@mcp.tool()
def openproject_add_comment(work_package_id: int, comment: str, notify: bool = True) -> dict[str, Any]:
    """Add a comment/activity entry to a work package."""
    payload = _api_post(
        f"/work_packages/{int(work_package_id)}/activities",
        body={"comment": _formattable(comment)},
        params={"notify": str(bool(notify)).lower()},
    )
    return {
        "id": payload.get("id"),
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "comment": payload.get("comment", {}),
        "user": payload.get("_links", {}).get("user", {}).get("title"),
        "workPackage": payload.get("_links", {}).get("workPackage", {}).get("href"),
    }


@mcp.tool()
def openproject_delete_resource(href_or_path: str) -> dict[str, Any]:
    """Delete any API resource by its API path or instance-local href."""
    normalized_path = _normalize_href(href_or_path)
    result = _api_request("DELETE", normalized_path)
    return {"deleted": True, "path": normalized_path, "result": result}


@mcp.tool()
def openproject_bulk_update_work_packages(
    work_package_ids: list[int],
    subject: str | None = None,
    description: str | None = None,
    assignee_id: int | None = None,
    status_name: str | None = None,
    priority_name: str | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    percentage_done: int | None = None,
    note: str | None = None,
    notify: bool = True,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Apply the same update to many work packages with per-item success/error reporting."""
    inputs = [{"work_package_id": int(work_package_id)} for work_package_id in work_package_ids]
    return _batch_result(
        "bulk_update_work_packages",
        inputs,
        lambda item: openproject_update_work_package(
            work_package_id=item["work_package_id"],
            subject=subject,
            description=description,
            assignee_id=assignee_id,
            status_name=status_name,
            priority_name=priority_name,
            start_date=start_date,
            due_date=due_date,
            percentage_done=percentage_done,
            note=note,
            notify=notify,
        ),
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_add_comment(
    work_package_ids: list[int],
    comment: str,
    notify: bool = True,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Add the same comment to many work packages."""
    inputs = [{"work_package_id": int(work_package_id)} for work_package_id in work_package_ids]
    return _batch_result(
        "bulk_add_comment",
        inputs,
        lambda item: openproject_add_comment(item["work_package_id"], comment=comment, notify=notify),
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_delete_work_packages(
    work_package_ids: list[int],
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Delete many work packages with per-item results."""
    inputs = [{"work_package_id": int(work_package_id)} for work_package_id in work_package_ids]
    return _batch_result(
        "bulk_delete_work_packages",
        inputs,
        lambda item: openproject_delete_work_package(item["work_package_id"]),
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_manage_watchers(
    work_package_ids: list[int],
    user_id: int,
    action: str = "add",
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Add or remove the same watcher across many work packages."""
    normalized_action = action.strip().lower()
    if normalized_action not in {"add", "remove"}:
        raise RuntimeError("action must be 'add' or 'remove'.")
    inputs = [{"work_package_id": int(work_package_id), "user_id": int(user_id)} for work_package_id in work_package_ids]
    handler = (
        (lambda item: openproject_add_watcher(item["work_package_id"], item["user_id"]))
        if normalized_action == "add"
        else (lambda item: openproject_remove_watcher(item["work_package_id"], item["user_id"]))
    )
    return _batch_result(
        f"bulk_{normalized_action}_watchers",
        inputs,
        handler,
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_create_memberships(
    project: str | int,
    role_names: list[str],
    user_ids: list[int] | None = None,
    group_ids: list[int] | None = None,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Add many users and or groups to a project with the same roles."""
    inputs: list[dict[str, Any]] = []
    for user_id in user_ids or []:
        inputs.append({"user_id": int(user_id)})
    for group_id in group_ids or []:
        inputs.append({"group_id": int(group_id)})
    return _batch_result(
        "bulk_create_memberships",
        inputs,
        lambda item: openproject_create_membership(
            project=project,
            role_names=role_names,
            user_id=item.get("user_id"),
            group_id=item.get("group_id"),
        ),
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_update_memberships(
    membership_ids: list[int],
    role_names: list[str],
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Replace roles for many memberships."""
    inputs = [{"membership_id": int(membership_id)} for membership_id in membership_ids]
    return _batch_result(
        "bulk_update_memberships",
        inputs,
        lambda item: openproject_update_membership(item["membership_id"], role_names=role_names),
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_delete_memberships(
    membership_ids: list[int],
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Delete many memberships."""
    inputs = [{"membership_id": int(membership_id)} for membership_id in membership_ids]
    return _batch_result(
        "bulk_delete_memberships",
        inputs,
        lambda item: openproject_delete_membership(item["membership_id"]),
        stop_on_error=stop_on_error,
    )


if __name__ == "__main__":
    mcp.run()
