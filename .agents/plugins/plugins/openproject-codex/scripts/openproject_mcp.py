#!/usr/bin/env python3
"""MCP server for OpenProject Codex."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
import json
import mimetypes
import os
import re
from typing import Any
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.server.fastmcp import FastMCP


USER_AGENT = "openproject-codex-plugin/0.1.0"
CONFIG_DIR = Path(os.environ.get("OPENPROJECT_CODEX_CONFIG_DIR", "~/.codex/openproject-codex")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.json"
PLACEHOLDER_BASE_URLS = {
    "",
    "https://your-openproject.example.com",
    "http://your-openproject.example.com",
}

BASE_URL = ""
DEFAULT_PROJECT: str | None = None
API_ROOT = ""

mcp = FastMCP(
    "openproject_codex",
    instructions=(
        "Tools for working with the OpenProject API from Codex. "
        "Use these tools to inspect projects, list work packages, create tasks, "
        "update tasks, and add comments without using the OpenProject UI."
    ),
)


def _normalize_base_url(value: str | None) -> str:
    normalized = (value or "").strip().rstrip("/")
    if normalized in PLACEHOLDER_BASE_URLS:
        return ""
    return normalized


def _normalize_optional(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _load_saved_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _env_or_saved_value(env_key: str, saved: dict[str, Any], *, normalizer=None) -> Any:
    env_value = os.environ.get(env_key)
    value = env_value if env_value is not None else saved.get(env_key)
    if normalizer is not None:
        return normalizer(value)
    return value


def _refresh_runtime_config() -> dict[str, Any]:
    global BASE_URL, DEFAULT_PROJECT, API_ROOT
    saved = _load_saved_config()
    BASE_URL = _env_or_saved_value("OPENPROJECT_BASE_URL", saved, normalizer=_normalize_base_url)
    DEFAULT_PROJECT = _env_or_saved_value("OPENPROJECT_DEFAULT_PROJECT", saved, normalizer=_normalize_optional)
    API_ROOT = f"{BASE_URL}/api/v3" if BASE_URL else ""
    return {
        "OPENPROJECT_BASE_URL": BASE_URL,
        "OPENPROJECT_DEFAULT_PROJECT": DEFAULT_PROJECT,
        "OPENPROJECT_API_TOKEN": _env_or_saved_value("OPENPROJECT_API_TOKEN", saved, normalizer=_normalize_optional),
        "OPENPROJECT_BASIC_API_TOKEN": _env_or_saved_value(
            "OPENPROJECT_BASIC_API_TOKEN",
            saved,
            normalizer=_normalize_optional,
        ),
        "OPENPROJECT_UI_USERNAME": _env_or_saved_value("OPENPROJECT_UI_USERNAME", saved, normalizer=_normalize_optional),
        "OPENPROJECT_UI_PASSWORD": _env_or_saved_value("OPENPROJECT_UI_PASSWORD", saved, normalizer=_normalize_optional),
    }


def _persist_config(updates: dict[str, Any]) -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = _load_saved_config()
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    CONFIG_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return current


def _redact(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _configuration_state() -> dict[str, Any]:
    runtime = _refresh_runtime_config()
    saved = _load_saved_config()
    api_token = runtime.get("OPENPROJECT_API_TOKEN")
    basic_token = runtime.get("OPENPROJECT_BASIC_API_TOKEN")
    missing: list[str] = []
    if not BASE_URL:
        missing.append("base_url")
    if not api_token and not basic_token and not _read_secret_from_env_file("OPENPROJECT_API_TOKEN_FILE") and not _read_secret_from_env_file("OPENPROJECT_BASIC_API_TOKEN_FILE"):
        missing.append("api_token")
    has_ui_creds = bool(
        runtime.get("OPENPROJECT_UI_USERNAME")
        and runtime.get("OPENPROJECT_UI_PASSWORD")
    ) or bool(
        _read_secret_from_env_file("OPENPROJECT_UI_USERNAME_FILE")
        and _read_secret_from_env_file("OPENPROJECT_UI_PASSWORD_FILE")
    )
    return {
        "configured": not missing,
        "missing": missing,
        "base_url": BASE_URL or None,
        "default_project": DEFAULT_PROJECT,
        "auth_mode": "bearer" if api_token else "basic" if basic_token else None,
        "has_ui_credentials": has_ui_creds,
        "saved_config_path": str(CONFIG_PATH),
        "saved_keys": sorted(saved.keys()),
        "token_preview": _redact(api_token or basic_token),
    }


def _setup_help_message(state: dict[str, Any] | None = None) -> str:
    current = state or _configuration_state()
    missing = current.get("missing", [])
    missing_text = ", ".join(missing) if missing else "none"
    return (
        "OpenProject Codex is not configured yet. Missing: "
        f"{missing_text}. Run openproject_setup_connection with your OpenProject base_url "
        "and api_token. Optional fields: default_project, ui_username, ui_password."
    )


def _auth_headers() -> dict[str, str]:
    runtime = _refresh_runtime_config()
    token = runtime.get("OPENPROJECT_API_TOKEN") or _read_secret_from_env_file("OPENPROJECT_API_TOKEN_FILE")
    basic_token = runtime.get("OPENPROJECT_BASIC_API_TOKEN") or _read_secret_from_env_file(
        "OPENPROJECT_BASIC_API_TOKEN_FILE"
    )
    if token:
        return {"Authorization": f"Bearer {token}"}
    if basic_token:
        import base64

        encoded = base64.b64encode(f"apikey:{basic_token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    raise RuntimeError(
        "Missing OpenProject credentials. "
        f"{_setup_help_message()}"
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


def _ui_auth() -> tuple[str, str]:
    runtime = _refresh_runtime_config()
    username = runtime.get("OPENPROJECT_UI_USERNAME") or _read_secret_from_env_file("OPENPROJECT_UI_USERNAME_FILE")
    password = runtime.get("OPENPROJECT_UI_PASSWORD") or _read_secret_from_env_file("OPENPROJECT_UI_PASSWORD_FILE")
    if username and password:
        return username, password
    raise RuntimeError(
        "Missing OpenProject UI credentials. Run openproject_setup_connection with "
        "ui_username and ui_password to enable UI-backed boards, wiki, and meeting tools."
    )


def _ui_session() -> httpx.Client:
    username, password = _ui_auth()
    client = httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/vnd.turbo-stream.html,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=30.0,
        follow_redirects=True,
    )
    login_path = f"{BASE_URL}/login"
    login_page = client.get(login_path)
    token = _extract_authenticity_token(login_page.text)
    response = client.post(
        login_path,
        data={"username": username, "password": password, "authenticity_token": token},
        headers={"Referer": login_path},
    )
    if 'data-logged-in="true"' not in response.text and "/logout" not in response.text:
        client.close()
        raise RuntimeError("OpenProject UI login failed. Check OPENPROJECT_UI_USERNAME and OPENPROJECT_UI_PASSWORD.")
    return client


def _client() -> httpx.Client:
    _refresh_runtime_config()
    if not BASE_URL:
        raise RuntimeError(_setup_help_message())
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


def _optional_href(path: str | None) -> dict[str, str] | None:
    if not path:
        return None
    return {"href": path}


def _project_ui_identifier(project: str | int | None) -> str:
    project_obj = _resolve_project(project)
    return str(project_obj.get("identifier") or project_obj.get("id"))


def _project_ui_path(project: str | int | None) -> str:
    return f"/projects/{_project_ui_identifier(project)}"


def _extract_authenticity_token(html: str) -> str:
    for pattern in (
        r'name="authenticity_token" value="([^"]+)"',
        r'name="csrf-token" content="([^"]+)"',
    ):
        match = re.search(pattern, html)
        if match:
            return unescape(match.group(1))
    raise RuntimeError("Could not find an authenticity token in the OpenProject UI response.")


def _clean_html_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(without_tags).split())


def _extract_hidden_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in re.findall(
        r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
        html,
        flags=re.I,
    ):
        fields[unescape(name)] = unescape(value)
    return fields


def _merge_link_overrides(payload_links: dict[str, Any], link_overrides: dict[str, Any] | None) -> None:
    if not link_overrides:
        return
    for key, value in link_overrides.items():
        if value is None:
            payload_links[key] = None
        elif isinstance(value, list):
            payload_links[key] = [{"href": _normalize_href(str(item))} for item in value]
        else:
            payload_links[key] = {"href": _normalize_href(str(value))}


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


def _query_columns_links(column_ids: list[str] | None) -> list[dict[str, str]] | None:
    if not column_ids:
        return None
    return [{"href": f"/api/v3/queries/columns/{column_id}"} for column_id in column_ids]


def _query_sort_links(sort_ids: list[str] | None) -> list[dict[str, str]] | None:
    if not sort_ids:
        return None
    return [{"href": f"/api/v3/queries/sort_bys/{sort_id}"} for sort_id in sort_ids]


def _query_highlight_links(column_ids: list[str] | None) -> list[dict[str, str]] | None:
    if not column_ids:
        return None
    return [{"href": f"/api/v3/queries/columns/{column_id}"} for column_id in column_ids]


def _query_group_link(group_id: str | None) -> dict[str, str] | None:
    if not group_id:
        return None
    return {"href": f"/api/v3/queries/group_bys/{group_id}"}


def _query_body(
    *,
    name: str | None = None,
    project: str | int | None = None,
    public: bool | None = None,
    include_subprojects: bool | None = None,
    sums: bool | None = None,
    show_hierarchies: bool | None = None,
    filters: list[dict[str, Any]] | None = None,
    column_ids: list[str] | None = None,
    sort_ids: list[str] | None = None,
    group_by: str | None = None,
    highlighted_attribute_ids: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if public is not None:
        body["public"] = bool(public)
    if include_subprojects is not None:
        body["includeSubprojects"] = bool(include_subprojects)
    if sums is not None:
        body["sums"] = bool(sums)
    if show_hierarchies is not None:
        body["showHierarchies"] = bool(show_hierarchies)
    if filters is not None:
        body["filters"] = filters
    links: dict[str, Any] = {}
    if project is not None:
        links["project"] = {"href": _link_href(_resolve_project(project), "self")}
    columns = _query_columns_links(column_ids)
    if columns is not None:
        links["columns"] = columns
    sorts = _query_sort_links(sort_ids)
    if sorts is not None:
        links["sortBy"] = sorts
    group = _query_group_link(group_by)
    if group is not None:
        links["groupBy"] = group
    highlights = _query_highlight_links(highlighted_attribute_ids)
    if highlights is not None:
        links["highlightedAttributes"] = highlights
    if links:
        body["_links"] = links
    return body


def _attachment_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "fileName": payload.get("fileName"),
        "fileSize": payload.get("fileSize"),
        "contentType": payload.get("contentType"),
        "description": payload.get("description"),
        "createdAt": payload.get("createdAt"),
        "href": _link_href(payload, "self"),
        "downloadHref": payload.get("_links", {}).get("downloadLocation", {}).get("href"),
        "container": payload.get("_links", {}).get("container", {}).get("title"),
    }


def _file_link_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "storageName": payload.get("storageName"),
        "mimeType": payload.get("mimeType"),
        "size": payload.get("size"),
        "href": _link_href(payload, "self"),
        "originId": payload.get("originId"),
        "location": payload.get("_links", {}).get("origin", {}).get("href")
        or payload.get("_links", {}).get("storageUrl", {}).get("href"),
    }


def _time_entry_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "spentOn": payload.get("spentOn"),
        "hours": payload.get("hours"),
        "ongoing": payload.get("ongoing"),
        "comment": payload.get("comment"),
        "project": payload.get("_links", {}).get("project", {}).get("title"),
        "entity": payload.get("_links", {}).get("entity", {}).get("title"),
        "activity": payload.get("_links", {}).get("activity", {}).get("title"),
        "user": payload.get("_links", {}).get("user", {}).get("title"),
        "href": _link_href(payload, "self"),
    }


def _news_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "summary": payload.get("summary"),
        "description": payload.get("description"),
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "project": payload.get("_links", {}).get("project", {}).get("title"),
        "author": payload.get("_links", {}).get("author", {}).get("title"),
        "href": _link_href(payload, "self"),
    }


def _document_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "description": payload.get("description"),
        "createdAt": payload.get("createdAt"),
        "updatedAt": payload.get("updatedAt"),
        "project": payload.get("_links", {}).get("project", {}).get("title"),
        "href": _link_href(payload, "self"),
    }


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _next_page_params(payload: dict[str, Any]) -> dict[str, Any] | None:
    href = payload.get("_links", {}).get("nextByOffset", {}).get("href")
    if not isinstance(href, str) or not href:
        return None
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    result: dict[str, Any] = {}
    for key, values in query.items():
        if not values:
            continue
        value = values[-1]
        if key in {"offset", "pageSize"}:
            try:
                result[key] = int(value)
                continue
            except ValueError:
                pass
        result[key] = value
    return result or None


def _fetch_all_collection(path: str, *, params: dict[str, Any] | None = None, page_size: int = 100) -> list[dict[str, Any]]:
    normalized_path = path
    if normalized_path.startswith("/api/v3/"):
        normalized_path = normalized_path.removeprefix("/api/v3")
    request_params = dict(params or {})
    request_params.setdefault("pageSize", max(1, min(page_size, 200)))
    request_params.setdefault("offset", 1)
    items: list[dict[str, Any]] = []
    while True:
        payload = _api_get(normalized_path, params=request_params)
        items.extend(_collection_elements(payload))
        next_params = _next_page_params(payload)
        if next_params is None:
            break
        request_params = next_params
    return items


def _fetch_all_work_packages(
    project: str | int | None = None,
    *,
    filters: list[dict[str, Any]] | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if filters:
        params["filters"] = json.dumps(filters)
    if project is None:
        return _fetch_all_collection("/work_packages", params=params, page_size=page_size)
    project_obj = _resolve_project(project)
    return _fetch_all_collection(f"/projects/{project_obj['id']}/work_packages", params=params, page_size=page_size)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "report"


def _report_output_path(prefix: str, file_format: str, output_path: str | None = None) -> Path:
    if output_path:
        return Path(output_path).expanduser().resolve()
    return Path("/tmp") / f"{_slugify(prefix)}.{file_format}"


def _svg_bar_chart(title: str, items: list[tuple[str, float]], *, color: str = "#1A67A3", width: int = 860) -> str:
    max_value = max((value for _, value in items), default=0.0)
    row_height = 28
    left_pad = 220
    chart_width = width - left_pad - 80
    height = 90 + row_height * max(1, len(items))
    rows: list[str] = []
    for index, (label, value) in enumerate(items):
        y = 50 + index * row_height
        bar_width = 0 if max_value <= 0 else (value / max_value) * chart_width
        rows.append(
            f'<text x="12" y="{y + 14}" font-size="12" fill="#1f2937">{label}</text>'
            f'<rect x="{left_pad}" y="{y}" width="{bar_width:.2f}" height="18" fill="{color}" rx="4" />'
            f'<text x="{left_pad + bar_width + 8:.2f}" y="{y + 14}" font-size="12" fill="#111827">{value:.2f}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#ffffff" />'
        f'<text x="12" y="28" font-size="20" font-weight="700" fill="#111827">{title}</text>'
        + "".join(rows)
        + "</svg>"
    )


def _write_html_report(title: str, sections: list[dict[str, Any]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for section in sections:
        table_html = ""
        rows = section.get("rows")
        if rows:
            headers = list(rows[0].keys())
            table_html = (
                "<table><thead><tr>"
                + "".join(f"<th>{header}</th>" for header in headers)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>" + "".join(f"<td>{row.get(header, '')}</td>" for header in headers) + "</tr>"
                    for row in rows
                )
                + "</tbody></table>"
            )
        blocks.append(
            f"<section><h2>{section['title']}</h2>"
            + (f"<p>{section['summary']}</p>" if section.get("summary") else "")
            + (section.get("svg", "") or "")
            + table_html
            + "</section>"
        )
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8' />"
        f"<title>{title}</title>"
        "<style>body{font-family:Helvetica,Arial,sans-serif;margin:24px;color:#111827;background:#f8fafc}"
        "h1{margin:0 0 24px}section{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin:0 0 20px}"
        "table{border-collapse:collapse;width:100%;margin-top:16px}th,td{border:1px solid #e5e7eb;padding:8px;text-align:left;font-size:14px}"
        "th{background:#f1f5f9}svg{max-width:100%;height:auto;display:block;margin-top:16px}</style></head><body>"
        f"<h1>{title}</h1>{''.join(blocks)}</body></html>"
    )
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _write_png_report(title: str, items: list[tuple[str, float]], output_path: Path) -> Path:
    try:
        import matplotlib.pyplot as plt
    except ModuleNotFoundError as exc:
        raise RuntimeError("PNG export requires matplotlib. Install project dependencies first.") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    labels = [label for label, _ in items] or ["No data"]
    values = [value for _, value in items] or [0]
    fig_height = max(4, 0.45 * len(labels) + 1.5)
    fig, ax = plt.subplots(figsize=(12, fig_height))
    ax.barh(labels, values, color="#1A67A3")
    ax.set_title(title)
    ax.invert_yaxis()
    ax.set_xlabel("Count")
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)
    return output_path


@mcp.tool()
def openproject_setup_connection(
    base_url: str,
    api_token: str,
    default_project: str | None = None,
    ui_username: str | None = None,
    ui_password: str | None = None,
    verify_connection: bool = True,
) -> dict[str, Any]:
    """Save OpenProject credentials locally so the plugin can be used immediately from Codex chat."""
    updates: dict[str, Any] = {
        "OPENPROJECT_BASE_URL": _normalize_base_url(base_url),
        "OPENPROJECT_API_TOKEN": _normalize_optional(api_token),
    }
    if default_project is not None:
        updates["OPENPROJECT_DEFAULT_PROJECT"] = _normalize_optional(default_project)
    if ui_username is not None:
        updates["OPENPROJECT_UI_USERNAME"] = _normalize_optional(ui_username)
    if ui_password is not None:
        updates["OPENPROJECT_UI_PASSWORD"] = _normalize_optional(ui_password)
    if not updates["OPENPROJECT_BASE_URL"]:
        raise RuntimeError("base_url is required and must be a real OpenProject URL.")
    if not updates["OPENPROJECT_API_TOKEN"]:
        raise RuntimeError("api_token is required.")
    _persist_config(updates)
    status = openproject_connection_status(verify_api=verify_connection, verify_ui=False)
    status["message"] = "OpenProject connection saved."
    return status


@mcp.tool()
def openproject_clear_saved_connection(clear_ui_credentials: bool = True) -> dict[str, Any]:
    """Remove locally saved OpenProject credentials from the plugin config file."""
    updates: dict[str, Any] = {
        "OPENPROJECT_BASE_URL": None,
        "OPENPROJECT_API_TOKEN": None,
        "OPENPROJECT_BASIC_API_TOKEN": None,
        "OPENPROJECT_DEFAULT_PROJECT": None,
    }
    if clear_ui_credentials:
        updates["OPENPROJECT_UI_USERNAME"] = None
        updates["OPENPROJECT_UI_PASSWORD"] = None
    _persist_config(updates)
    return {
        "cleared": True,
        "config_path": str(CONFIG_PATH),
        "connection": openproject_connection_status(verify_api=False, verify_ui=False),
    }


@mcp.tool()
def openproject_connection_status(verify_api: bool = True, verify_ui: bool = False) -> dict[str, Any]:
    """Return plugin configuration state and optionally verify API or UI connectivity."""
    state = _configuration_state()
    response: dict[str, Any] = {
        "configured": state["configured"],
        "missing": state["missing"],
        "base_url": state["base_url"],
        "api_root": API_ROOT or None,
        "default_project": state["default_project"],
        "auth_mode": state["auth_mode"],
        "has_ui_credentials": state["has_ui_credentials"],
        "saved_config_path": state["saved_config_path"],
        "saved_keys": state["saved_keys"],
        "token_preview": state["token_preview"],
        "setup_required": not state["configured"],
        "setup_message": _setup_help_message(state) if not state["configured"] else None,
    }
    if not state["configured"]:
        return response
    if verify_api:
        try:
            payload = _api_get("/")
            response["api_connection"] = {"ok": True, "instance": payload}
        except Exception as exc:
            response["api_connection"] = {"ok": False, "error": str(exc)}
    if verify_ui:
        try:
            with _ui_session() as client:
                response["ui_connection"] = {"ok": True, "base_url": BASE_URL, "status_code": 200 if client else 0}
        except Exception as exc:
            response["ui_connection"] = {"ok": False, "error": str(exc)}
    return response


@mcp.tool()
def openproject_test_connection(test_ui: bool = False) -> dict[str, Any]:
    """Verify the currently saved OpenProject connection without changing configuration."""
    return openproject_connection_status(verify_api=True, verify_ui=test_ui)


@mcp.tool()
def openproject_whoami() -> dict[str, Any]:
    """Return the authenticated OpenProject user after configuration is complete."""
    return openproject_get_user("me")


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
def openproject_create_query(
    name: str,
    project: str | int | None = None,
    public: bool = False,
    include_subprojects: bool = True,
    sums: bool = False,
    show_hierarchies: bool = True,
    filters: list[dict[str, Any]] | None = None,
    column_ids: list[str] | None = None,
    sort_ids: list[str] | None = None,
    group_by: str | None = None,
    highlighted_attribute_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a saved query."""
    body = _query_body(
        name=name,
        project=project,
        public=public,
        include_subprojects=include_subprojects,
        sums=sums,
        show_hierarchies=show_hierarchies,
        filters=filters,
        column_ids=column_ids,
        sort_ids=sort_ids,
        group_by=group_by,
        highlighted_attribute_ids=highlighted_attribute_ids,
    )
    return _api_post("/queries", body=body)


@mcp.tool()
def openproject_update_query(
    query_id: int,
    name: str | None = None,
    project: str | int | None = None,
    public: bool | None = None,
    include_subprojects: bool | None = None,
    sums: bool | None = None,
    show_hierarchies: bool | None = None,
    filters: list[dict[str, Any]] | None = None,
    column_ids: list[str] | None = None,
    sort_ids: list[str] | None = None,
    group_by: str | None = None,
    highlighted_attribute_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Update a saved query."""
    body = _query_body(
        name=name,
        project=project,
        public=public,
        include_subprojects=include_subprojects,
        sums=sums,
        show_hierarchies=show_hierarchies,
        filters=filters,
        column_ids=column_ids,
        sort_ids=sort_ids,
        group_by=group_by,
        highlighted_attribute_ids=highlighted_attribute_ids,
    )
    return _api_patch(f"/queries/{int(query_id)}", body=body)


@mcp.tool()
def openproject_delete_query(query_id: int) -> dict[str, Any]:
    """Delete a saved query."""
    result = _api_delete(f"/queries/{int(query_id)}")
    return {"deleted": True, "queryId": int(query_id), "result": result}


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
def openproject_list_work_package_attachments(work_package_id: int, page_size: int = 100) -> dict[str, Any]:
    """List attachments for a work package."""
    payload = _api_get(
        f"/work_packages/{int(work_package_id)}/attachments",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "attachments": [_attachment_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_list_work_package_file_links(work_package_id: int, page_size: int = 100) -> dict[str, Any]:
    """List file links for a work package."""
    payload = _api_get(
        f"/work_packages/{int(work_package_id)}/file_links",
        params={"pageSize": max(1, min(page_size, 200))},
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "fileLinks": [_file_link_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_create_work_package_file_links(
    work_package_id: int,
    file_links: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create file links for a work package using the OpenProject storage/file-link model."""
    body = {"_type": "Collection", "_embedded": {"elements": file_links}}
    payload = _api_post(f"/work_packages/{int(work_package_id)}/file_links", body=body)
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "fileLinks": [_file_link_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_get_attachment(attachment_id: int) -> dict[str, Any]:
    """Fetch attachment metadata."""
    return _attachment_summary(_api_get(f"/attachments/{int(attachment_id)}"))


@mcp.tool()
def openproject_delete_attachment(attachment_id: int) -> dict[str, Any]:
    """Delete an attachment."""
    result = _api_delete(f"/attachments/{int(attachment_id)}")
    return {"deleted": True, "attachmentId": int(attachment_id), "result": result}


@mcp.tool()
def openproject_get_file_link(file_link_id: int) -> dict[str, Any]:
    """Fetch a file link."""
    return _file_link_summary(_api_get(f"/file_links/{int(file_link_id)}"))


@mcp.tool()
def openproject_delete_file_link(file_link_id: int) -> dict[str, Any]:
    """Delete a file link."""
    result = _api_delete(f"/file_links/{int(file_link_id)}")
    return {"deleted": True, "fileLinkId": int(file_link_id), "result": result}


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
    field_overrides: dict[str, Any] | None = None,
    link_overrides: dict[str, Any] | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Create a work package in OpenProject."""
    project_obj = _resolve_project(project)
    payload: dict[str, Any] = {
        "subject": subject,
        "_links": {
            "project": {"href": _link_href(project_obj, "self")},
            "type": {"href": _resource_href("/types", type_name)},
        },
    }
    if description is not None:
        payload["description"] = _formattable(description)
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
    _merge_link_overrides(payload["_links"], link_overrides)
    if field_overrides:
        payload.update(field_overrides)

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
    field_overrides: dict[str, Any] | None = None,
    link_overrides: dict[str, Any] | None = None,
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
    _merge_link_overrides(links, link_overrides)
    if field_overrides:
        payload.update(field_overrides)

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
def openproject_list_time_entries(
    project: str | int | None = None,
    work_package_id: int | None = None,
    user_id: int | str | None = None,
    page_size: int = 50,
    offset: int = 1,
) -> dict[str, Any]:
    """List time entries with optional project, work package, or user filters."""
    filters: list[dict[str, Any]] = []
    if project is not None:
        filters.append({"project": {"operator": "=", "values": [str(_project_id(project))]}})
    if work_package_id is not None:
        filters.append({"entity_id": {"operator": "=", "values": [str(int(work_package_id))]}})
        filters.append({"entity_type": {"operator": "=", "values": ["WorkPackage"]}})
    if user_id is not None:
        filters.append({"user": {"operator": "=", "values": [str(user_id)]}})
    payload = _api_get(
        "/time_entries",
        params={
            "pageSize": max(1, min(page_size, 200)),
            "offset": max(1, offset),
            "filters": json.dumps(filters),
        },
    )
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "timeEntries": [_time_entry_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_get_time_entry(time_entry_id: int) -> dict[str, Any]:
    """Fetch a time entry."""
    return _time_entry_summary(_api_get(f"/time_entries/{int(time_entry_id)}"))


@mcp.tool()
def openproject_create_time_entry(
    spent_on: str,
    hours: str,
    project: str | int,
    work_package_id: int,
    comment: str | None = None,
    ongoing: bool = False,
    activity_id: int | None = None,
    user_id: int | str | None = None,
) -> dict[str, Any]:
    """Create a time entry for a work package."""
    body: dict[str, Any] = {
        "spentOn": spent_on,
        "hours": hours,
        "ongoing": bool(ongoing),
        "_links": {
            "project": {"href": _link_href(_resolve_project(project), "self")},
            "entity": {"href": f"/api/v3/work_packages/{int(work_package_id)}"},
        },
    }
    if comment is not None:
        body["comment"] = _formattable(comment)
    if activity_id is not None:
        body["_links"]["activity"] = {"href": f"/api/v3/time_entries/activity/{int(activity_id)}"}
    if user_id is not None:
        body["_links"]["user"] = {"href": f"/api/v3/users/{user_id}"}
    return _time_entry_summary(_api_post("/time_entries", body=body))


@mcp.tool()
def openproject_update_time_entry(
    time_entry_id: int,
    spent_on: str | None = None,
    hours: str | None = None,
    comment: str | None = None,
    ongoing: bool | None = None,
    activity_id: int | None = None,
) -> dict[str, Any]:
    """Update a time entry."""
    body: dict[str, Any] = {}
    if spent_on is not None:
        body["spentOn"] = spent_on
    if hours is not None:
        body["hours"] = hours
    if comment is not None:
        body["comment"] = _formattable(comment)
    if ongoing is not None:
        body["ongoing"] = bool(ongoing)
    if activity_id is not None:
        body.setdefault("_links", {})["activity"] = {"href": f"/api/v3/time_entries/activity/{int(activity_id)}"}
    return _time_entry_summary(_api_patch(f"/time_entries/{int(time_entry_id)}", body=body))


@mcp.tool()
def openproject_delete_time_entry(time_entry_id: int) -> dict[str, Any]:
    """Delete a time entry."""
    result = _api_delete(f"/time_entries/{int(time_entry_id)}")
    return {"deleted": True, "timeEntryId": int(time_entry_id), "result": result}


@mcp.tool()
def openproject_list_documents(page_size: int = 50, offset: int = 1) -> dict[str, Any]:
    """List documents."""
    payload = _api_get("/documents", params={"pageSize": max(1, min(page_size, 200)), "offset": max(1, offset)})
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "documents": [_document_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_get_document(document_id: int) -> dict[str, Any]:
    """Fetch a document."""
    payload = _api_get(f"/documents/{int(document_id)}")
    result = _document_summary(payload)
    attachments = payload.get("_embedded", {}).get("attachments", {})
    if attachments:
        result["attachments"] = [_attachment_summary(item) for item in attachments.get("_embedded", {}).get("elements", [])]
    return result


@mcp.tool()
def openproject_update_document(document_id: int, title: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Update a document."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = {"raw": description}
    return _document_summary(_api_patch(f"/documents/{int(document_id)}", body=body))


@mcp.tool()
def openproject_list_news(page_size: int = 50, offset: int = 1) -> dict[str, Any]:
    """List news items."""
    payload = _api_get("/news", params={"pageSize": max(1, min(page_size, 200)), "offset": max(1, offset)})
    return {
        "total": payload.get("total"),
        "count": payload.get("count"),
        "news": [_news_summary(item) for item in _collection_elements(payload)],
    }


@mcp.tool()
def openproject_get_news(news_id: int) -> dict[str, Any]:
    """Fetch a news item."""
    return _news_summary(_api_get(f"/news/{int(news_id)}"))


@mcp.tool()
def openproject_create_news(
    project: str | int,
    title: str,
    summary: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create a news item."""
    body: dict[str, Any] = {
        "title": title,
        "_links": {"project": {"href": _link_href(_resolve_project(project), "self")}},
    }
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = _formattable(description)
    return _news_summary(_api_post("/news", body=body))


@mcp.tool()
def openproject_update_news(
    news_id: int,
    title: str | None = None,
    summary: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update a news item."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = _formattable(description)
    return _news_summary(_api_patch(f"/news/{int(news_id)}", body=body))


@mcp.tool()
def openproject_delete_news(news_id: int) -> dict[str, Any]:
    """Delete a news item."""
    result = _api_delete(f"/news/{int(news_id)}")
    return {"deleted": True, "newsId": int(news_id), "result": result}


@mcp.tool()
def openproject_get_wiki_page(page_id: str) -> dict[str, Any]:
    """Fetch a wiki page by id."""
    return _api_get(f"/wiki_pages/{page_id}")


@mcp.tool()
def openproject_get_meeting(meeting_id: int) -> dict[str, Any]:
    """Fetch a meeting page by id."""
    return _api_get(f"/meetings/{int(meeting_id)}")


@mcp.tool()
def openproject_upload_attachment(
    container_type: str,
    container_id: int,
    file_path: str,
    file_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Upload a binary attachment to a work package, wiki page, meeting, or activity."""
    normalized_type = container_type.strip().lower()
    path_map = {
        "work_package": f"/work_packages/{int(container_id)}/attachments",
        "wiki_page": f"/wiki_pages/{int(container_id)}/attachments",
        "meeting": f"/meetings/{int(container_id)}/attachments",
        "activity": f"/activities/{int(container_id)}/attachments",
    }
    api_path = path_map.get(normalized_type)
    if api_path is None:
        raise RuntimeError("container_type must be one of: work_package, wiki_page, meeting, activity.")
    attachment_path = Path(file_path).expanduser()
    if not attachment_path.exists() or not attachment_path.is_file():
        raise RuntimeError(f"Attachment file does not exist: {attachment_path}")
    upload_name = file_name or attachment_path.name
    metadata: dict[str, Any] = {"fileName": upload_name}
    if description is not None:
        metadata["description"] = description
    mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
    with httpx.Client(
        headers={"Accept": "application/hal+json", "User-Agent": USER_AGENT, **_auth_headers()},
        timeout=30.0,
        follow_redirects=True,
    ) as client, attachment_path.open("rb") as handle:
        response = client.post(
            f"{API_ROOT}{api_path}",
            files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "file": (upload_name, handle, mime_type),
            },
        )
    return _attachment_summary(_decode_response(response))


@mcp.tool()
def openproject_list_boards(project: str | int | None = None) -> dict[str, Any]:
    """List boards visible in a project using the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        response = client.get(f"{BASE_URL}{project_path}/boards")
    finally:
        client.close()
    rows = list(
        re.finditer(
            r'<a href="/projects/[^"]+/boards/(?P<id>\d+)"[^>]*>(?P<name>[^<]+)</a>.*?data-test-selector="board-remove-(?P=id)"',
            response.text,
            flags=re.S,
        )
    )
    return {
        "count": len(rows),
        "boards": [
            {
                "id": int(match.group("id")),
                "name": _clean_html_text(match.group("name")),
                "href": f"{BASE_URL}/projects/{_project_ui_identifier(project)}/boards/{match.group('id')}",
            }
            for match in rows
        ],
    }


@mcp.tool()
def openproject_create_board(project: str | int | None, name: str, kind: str = "basic") -> dict[str, Any]:
    """Create a board through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        new_page = client.get(f"{BASE_URL}{project_path}/boards/new")
        token = _extract_authenticity_token(new_page.text)
        response = client.post(
            f"{BASE_URL}{project_path}/boards",
            data={
                "authenticity_token": token,
                "boards_grid[name]": name,
                "boards_grid[attribute]": kind,
                "button": "",
            },
            headers={"Referer": str(new_page.url)},
            follow_redirects=False,
        )
    finally:
        client.close()
    location = response.headers.get("location", "")
    match = re.search(r"/boards/(\d+)", location)
    return {
        "id": int(match.group(1)) if match else None,
        "name": name,
        "kind": kind,
        "href": location if location.startswith("http") else f"{BASE_URL}{location}",
    }


@mcp.tool()
def openproject_delete_board(project: str | int | None, board_id: int) -> dict[str, Any]:
    """Delete a board through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        index_page = client.get(f"{BASE_URL}{project_path}/boards")
        token = _extract_authenticity_token(index_page.text)
        response = client.post(
            f"{BASE_URL}{project_path}/boards/{int(board_id)}",
            data={"_method": "delete", "authenticity_token": token},
            headers={"Referer": str(index_page.url)},
        )
    finally:
        client.close()
    return {"deleted": response.status_code < 400, "boardId": int(board_id)}


@mcp.tool()
def openproject_list_wiki_pages(project: str | int | None = None) -> dict[str, Any]:
    """List wiki pages in a project using the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        response = client.get(f"{BASE_URL}{project_path}/wiki/index")
    finally:
        client.close()
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = rf'href="(/projects/{re.escape(_project_ui_identifier(project))}/wiki/([^"/?#]+))"'
    for href, slug in re.findall(pattern, response.text):
        if slug in {"index", "new"} or slug in seen:
            continue
        seen.add(slug)
        pages.append({"slug": slug, "href": f"{BASE_URL}{href}"})
    return {"count": len(pages), "pages": pages}


@mcp.tool()
def openproject_get_wiki_page_by_slug(project: str | int | None, page_slug: str) -> dict[str, Any]:
    """Fetch wiki page details by project slug path instead of API id."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        response = client.get(f"{BASE_URL}{project_path}/wiki/{page_slug}")
    finally:
        client.close()
    page_id_match = re.search(r"/api/v3/wiki_pages/(\d+)", response.text)
    title_match = re.search(r"<title>(.*?)</title>", response.text, flags=re.S)
    return {
        "id": int(page_id_match.group(1)) if page_id_match else None,
        "slug": page_slug,
        "title": _clean_html_text(title_match.group(1)) if title_match else page_slug,
        "href": str(response.url),
    }


@mcp.tool()
def openproject_create_wiki_page(project: str | int | None, title: str, content: str) -> dict[str, Any]:
    """Create a wiki page through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        new_page = client.get(f"{BASE_URL}{project_path}/wiki")
        token = _extract_authenticity_token(new_page.text)
        response = client.post(
            f"{BASE_URL}{project_path}/wiki/new",
            data={"authenticity_token": token, "page[title]": title, "page[text]": content},
            headers={"Referer": str(new_page.url)},
            follow_redirects=False,
        )
    finally:
        client.close()
    location = response.headers.get("location", "")
    return {
        "title": title,
        "slug": location.rstrip("/").split("/")[-1] if location else None,
        "href": location if location.startswith("http") else f"{BASE_URL}{location}",
    }


@mcp.tool()
def openproject_update_wiki_page(
    project: str | int | None,
    page_slug: str,
    title: str | None = None,
    content: str | None = None,
    journal_notes: str | None = None,
) -> dict[str, Any]:
    """Update a wiki page through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        edit_page = client.get(f"{BASE_URL}{project_path}/wiki/{page_slug}/edit")
        hidden = _extract_hidden_fields(edit_page.text)
        current_title_match = re.search(r'name="page\[title\]"[^>]*value="([^"]*)"', edit_page.text)
        current_text_match = re.search(r'<textarea[^>]*name="page\[text\]"[^>]*>(.*?)</textarea>', edit_page.text, flags=re.S)
        response = client.post(
            f"{BASE_URL}{project_path}/wiki/{page_slug}",
            data={
                "_method": "put",
                "authenticity_token": hidden.get("authenticity_token") or _extract_authenticity_token(edit_page.text),
                "page[lock_version]": hidden.get("page[lock_version]", "0"),
                "page[parent_id]": hidden.get("page[parent_id]", ""),
                "page[title]": title if title is not None else unescape(current_title_match.group(1)) if current_title_match else page_slug,
                "page[text]": content if content is not None else unescape(current_text_match.group(1)) if current_text_match else "",
                "page[journal_notes]": journal_notes or "",
                "button": "Save",
            },
            headers={"Referer": str(edit_page.url)},
            follow_redirects=False,
        )
    finally:
        client.close()
    location = response.headers.get("location", "")
    return {
        "slug": location.rstrip("/").split("/")[-1] if location else page_slug,
        "title": title,
        "href": location if location.startswith("http") else f"{BASE_URL}{location}" if location else None,
    }


@mcp.tool()
def openproject_delete_wiki_page(project: str | int | None, page_slug: str) -> dict[str, Any]:
    """Delete a wiki page through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        page = client.get(f"{BASE_URL}{project_path}/wiki/{page_slug}")
        token = _extract_authenticity_token(page.text)
        response = client.post(
            f"{BASE_URL}{project_path}/wiki/{page_slug}",
            data={"_method": "delete", "authenticity_token": token},
            headers={"Referer": str(page.url)},
        )
    finally:
        client.close()
    return {"deleted": response.status_code < 400, "pageSlug": page_slug}


@mcp.tool()
def openproject_list_meetings(project: str | int | None = None, upcoming: bool = True) -> dict[str, Any]:
    """List project meetings using the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        response = client.get(f"{BASE_URL}{project_path}/meetings", params={"upcoming": str(bool(upcoming)).lower()})
    finally:
        client.close()
    meetings = []
    for match in re.finditer(
        r'<a href="/projects/[^"]+/meetings/(?P<id>\d+)"[^>]*>(?P<title>[^<]+)</a>.*?class="op-border-box-grid__row-item start_time[^"]*">\s*(?P<start>[^<]+)\s*</div>.*?class="op-border-box-grid__row-item duration[^"]*">\s*(?P<duration>.*?)\s*</div>.*?class="op-border-box-grid__row-item location[^"]*">\s*(?P<location>.*?)\s*</div>',
        response.text,
        flags=re.S,
    ):
        meetings.append(
            {
                "id": int(match.group("id")),
                "title": _clean_html_text(match.group("title")),
                "start": _clean_html_text(match.group("start")),
                "duration": _clean_html_text(match.group("duration")),
                "location": _clean_html_text(match.group("location")),
                "href": f"{BASE_URL}{project_path}/meetings/{match.group('id')}",
            }
        )
    return {"count": len(meetings), "upcoming": bool(upcoming), "meetings": meetings}


@mcp.tool()
def openproject_create_meeting(
    project: str | int | None,
    title: str,
    start_date: str,
    start_time: str,
    duration_hours: str,
    location: str | None = None,
) -> dict[str, Any]:
    """Create a one-time meeting through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        new_dialog = client.get(f"{BASE_URL}{project_path}/meetings/new_dialog", headers={"Accept": "*/*"})
        token = _extract_authenticity_token(new_dialog.text)
        response = client.post(
            f"{BASE_URL}{project_path}/meetings",
            data={
                "authenticity_token": token,
                "meeting[title]": title,
                "meeting[location]": location or "",
                "meeting[start_date]": start_date,
                "meeting[start_time_hour]": start_time,
                "meeting[duration]": duration_hours,
            },
            headers={"Referer": f"{BASE_URL}{project_path}/meetings"},
            follow_redirects=False,
        )
    finally:
        client.close()
    location_header = response.headers.get("location", "")
    match = re.search(r"/meetings/(\d+)", location_header)
    return {
        "id": int(match.group(1)) if match else None,
        "title": title,
        "startDate": start_date,
        "startTime": start_time,
        "durationHours": duration_hours,
        "location": location or "",
        "href": location_header if location_header.startswith("http") else f"{BASE_URL}{location_header}",
    }


@mcp.tool()
def openproject_delete_meeting(project: str | int | None, meeting_id: int) -> dict[str, Any]:
    """Delete a meeting through the OpenProject UI workflow."""
    project_path = _project_ui_path(project)
    client = _ui_session()
    try:
        dialog = client.get(
            f"{BASE_URL}{project_path}/meetings/{int(meeting_id)}/delete_dialog",
            headers={"Accept": "*/*"},
        )
        hidden = _extract_hidden_fields(dialog.text)
        response = client.post(
            f"{BASE_URL}{project_path}/meetings/{int(meeting_id)}",
            data={
                "_method": hidden.get("_method", "delete"),
                "authenticity_token": hidden.get("authenticity_token") or _extract_authenticity_token(dialog.text),
            },
            headers={"Referer": str(dialog.url)},
        )
    finally:
        client.close()
    return {"deleted": response.status_code < 400, "meetingId": int(meeting_id)}


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
    field_overrides: dict[str, Any] | None = None,
    link_overrides: dict[str, Any] | None = None,
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
            field_overrides=field_overrides,
            link_overrides=link_overrides,
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


def _query_work_package_ids(query_id: int, page_size: int = 200) -> list[int]:
    work_packages = _query_work_packages(query_id=query_id, page_size=page_size)
    return [int(item["id"]) for item in work_packages if item.get("id") is not None]


def _query_work_packages(query_id: int, page_size: int = 100) -> list[dict[str, Any]]:
    query_payload = _api_get(f"/queries/{int(query_id)}")
    results_href = query_payload.get("_links", {}).get("results", {}).get("href")
    if not results_href:
        raise RuntimeError(f"Query {query_id} does not expose a results link.")
    return _fetch_all_collection(_normalize_href(results_href), page_size=page_size)


@mcp.tool()
def openproject_bulk_update_by_query(
    query_id: int,
    subject: str | None = None,
    description: str | None = None,
    assignee_id: int | None = None,
    status_name: str | None = None,
    priority_name: str | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    percentage_done: int | None = None,
    note: str | None = None,
    field_overrides: dict[str, Any] | None = None,
    link_overrides: dict[str, Any] | None = None,
    notify: bool = True,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Apply the same work-package update to every item returned by a saved query."""
    work_package_ids = _query_work_package_ids(query_id)
    return openproject_bulk_update_work_packages(
        work_package_ids=work_package_ids,
        subject=subject,
        description=description,
        assignee_id=assignee_id,
        status_name=status_name,
        priority_name=priority_name,
        start_date=start_date,
        due_date=due_date,
        percentage_done=percentage_done,
        note=note,
        field_overrides=field_overrides,
        link_overrides=link_overrides,
        notify=notify,
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_comment_by_query(
    query_id: int,
    comment: str,
    notify: bool = True,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Add the same comment to every work package returned by a saved query."""
    work_package_ids = _query_work_package_ids(query_id)
    return openproject_bulk_add_comment(
        work_package_ids=work_package_ids,
        comment=comment,
        notify=notify,
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_watch_by_query(
    query_id: int,
    user_id: int,
    action: str = "add",
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Add or remove the same watcher across all work packages returned by a saved query."""
    work_package_ids = _query_work_package_ids(query_id)
    return openproject_bulk_manage_watchers(
        work_package_ids=work_package_ids,
        user_id=user_id,
        action=action,
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_bulk_delete_by_query(
    query_id: int,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Delete every work package returned by a saved query."""
    work_package_ids = _query_work_package_ids(query_id)
    return openproject_bulk_delete_work_packages(
        work_package_ids=work_package_ids,
        stop_on_error=stop_on_error,
    )


@mcp.tool()
def openproject_report_assignee_workload(
    project: str | int | None = None,
    status_names: list[str] | None = None,
    include_unassigned: bool = True,
) -> dict[str, Any]:
    """Aggregate visible work packages into an assignee workload report."""
    filters = _filter_by_names("status", status_names)
    work_packages = _fetch_all_work_packages(project=project, filters=filters or None)
    buckets: dict[str, dict[str, Any]] = {}
    for item in work_packages:
        summary = _work_package_summary(item)
        assignee = summary.get("assignee") or "Unassigned"
        if assignee == "Unassigned" and not include_unassigned:
            continue
        bucket = buckets.setdefault(
            assignee,
            {
                "assignee": assignee,
                "total": 0,
                "overdue": 0,
                "statuses": Counter(),
                "types": Counter(),
            },
        )
        bucket["total"] += 1
        bucket["statuses"][summary.get("status") or "Unknown"] += 1
        bucket["types"][summary.get("type") or "Unknown"] += 1
        due_date = _parse_iso_date(summary.get("dueDate"))
        if due_date and due_date < date.today() and summary.get("status") not in {"Closed", "Resolved", "Done"}:
            bucket["overdue"] += 1
    rows = sorted(
        (
            {
                "assignee": value["assignee"],
                "total": value["total"],
                "overdue": value["overdue"],
                "topStatus": value["statuses"].most_common(1)[0][0] if value["statuses"] else None,
                "topType": value["types"].most_common(1)[0][0] if value["types"] else None,
            }
            for value in buckets.values()
        ),
        key=lambda item: (-int(item["total"]), str(item["assignee"])),
    )
    return {
        "project": _normalize_project_ref(project) if project is not None else DEFAULT_PROJECT,
        "totalWorkPackages": len(work_packages),
        "assignees": rows,
    }


@mcp.tool()
def openproject_report_burndown(
    query_id: int,
    days: int = 14,
) -> dict[str, Any]:
    """Generate a simple snapshot burndown-style projection for a saved query."""
    query_payload = _api_get(f"/queries/{int(query_id)}")
    work_packages = [_work_package_summary(item) for item in _query_work_packages(query_id=query_id, page_size=100)]
    today = date.today()
    remaining_total = len(work_packages)
    completed = 0
    due_buckets: Counter[str] = Counter()
    overdue = 0
    for item in work_packages:
        status = item.get("status") or ""
        if status.lower() in {"closed", "resolved", "done"}:
            completed += 1
        due_date = _parse_iso_date(item.get("dueDate"))
        if due_date is None:
            due_buckets["No due date"] += 1
        elif due_date < today:
            overdue += 1
            due_buckets["Overdue"] += 1
        elif due_date <= today + timedelta(days=7):
            due_buckets["Due in 7 days"] += 1
        else:
            due_buckets["Due later"] += 1
    ideal_points = []
    actual_points = []
    for offset in range(max(1, days) + 1):
        point_date = today + timedelta(days=offset)
        ideal_remaining = max(0.0, remaining_total * (1 - (offset / max(1, days))))
        actual_remaining = sum(
            1
            for item in work_packages
            if (_parse_iso_date(item.get("dueDate")) or (today + timedelta(days=days + 1))) >= point_date
        )
        ideal_points.append({"date": point_date.isoformat(), "remaining": round(ideal_remaining, 2)})
        actual_points.append({"date": point_date.isoformat(), "remaining": actual_remaining})
    return {
        "query": {
            "id": query_payload.get("id"),
            "name": query_payload.get("name"),
            "href": _link_href(query_payload, "self"),
        },
        "scopeTotal": remaining_total,
        "completedSnapshot": completed,
        "openSnapshot": remaining_total - completed,
        "overdueSnapshot": overdue,
        "dueBuckets": dict(due_buckets),
        "idealSeries": ideal_points,
        "actualSeries": actual_points,
    }


@mcp.tool()
def openproject_dashboard_overdue_by_team(
    project: str | int | None = None,
    team_mode: str = "assignee",
) -> dict[str, Any]:
    """Build an overdue dashboard grouped by assignee or status."""
    filters = [{"dueDate": {"operator": "<t-", "values": ["0"]}}]
    work_packages = _fetch_all_work_packages(project=project, filters=filters)
    rows: dict[str, dict[str, Any]] = {}
    for item in work_packages:
        summary = _work_package_summary(item)
        if (summary.get("status") or "").lower() in {"closed", "resolved", "done"}:
            continue
        if team_mode == "status":
            team_key = summary.get("status") or "Unknown"
        else:
            team_key = summary.get("assignee") or "Unassigned"
        bucket = rows.setdefault(
            team_key,
            {"team": team_key, "total": 0, "priorities": Counter(), "oldestDueDate": None, "sampleItems": []},
        )
        bucket["total"] += 1
        bucket["priorities"][summary.get("priority") or "Unknown"] += 1
        due_date = summary.get("dueDate")
        if due_date and (bucket["oldestDueDate"] is None or due_date < bucket["oldestDueDate"]):
            bucket["oldestDueDate"] = due_date
        if len(bucket["sampleItems"]) < 5:
            bucket["sampleItems"].append({"id": summary["id"], "subject": summary["subject"], "dueDate": due_date})
    ordered = sorted(rows.values(), key=lambda item: (-int(item["total"]), str(item["team"])))
    for item in ordered:
        item["topPriority"] = item["priorities"].most_common(1)[0][0] if item["priorities"] else None
        item["priorities"] = dict(item["priorities"])
    return {
        "project": _normalize_project_ref(project) if project is not None else DEFAULT_PROJECT,
        "groupedBy": team_mode,
        "teams": ordered,
    }


@mcp.tool()
def openproject_export_project_health(
    project: str | int | None = None,
    file_format: str = "html",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Export a project health dashboard as HTML or PNG."""
    work_packages = _fetch_all_work_packages(project=project)
    today = date.today()
    status_counter: Counter[str] = Counter()
    assignee_overdue: Counter[str] = Counter()
    due_bucket_counter: Counter[str] = Counter()
    recent_updates: list[dict[str, Any]] = []
    for item in work_packages:
        summary = _work_package_summary(item)
        status_counter[summary.get("status") or "Unknown"] += 1
        due_date = _parse_iso_date(summary.get("dueDate"))
        if due_date is None:
            due_bucket_counter["No due date"] += 1
        elif due_date < today:
            due_bucket_counter["Overdue"] += 1
            assignee_overdue[summary.get("assignee") or "Unassigned"] += 1
        elif due_date <= today + timedelta(days=7):
            due_bucket_counter["Due in 7 days"] += 1
        else:
            due_bucket_counter["Due later"] += 1
        recent_updates.append(
            {
                "id": summary.get("id"),
                "subject": summary.get("subject"),
                "status": summary.get("status"),
                "assignee": summary.get("assignee"),
                "updatedAt": summary.get("updatedAt"),
            }
        )
    recent_updates.sort(key=lambda item: _parse_iso_datetime(item.get("updatedAt")) or datetime.min.replace(tzinfo=UTC), reverse=True)
    status_items = status_counter.most_common()
    overdue_items = assignee_overdue.most_common()
    due_items = due_bucket_counter.most_common()
    normalized_format = file_format.strip().lower()
    report_title = f"OpenProject health report: {_normalize_project_ref(project)}"
    output = _report_output_path(f"{report_title}-{normalized_format}", normalized_format, output_path=output_path)
    if normalized_format == "html":
        _write_html_report(
            report_title,
            [
                {
                    "title": "Status distribution",
                    "summary": f"{len(work_packages)} visible work packages in scope.",
                    "svg": _svg_bar_chart("Status distribution", [(label, float(value)) for label, value in status_items]),
                    "rows": [{"status": label, "count": value} for label, value in status_items],
                },
                {
                    "title": "Overdue by assignee",
                    "summary": "Only open overdue items are counted here.",
                    "svg": _svg_bar_chart("Overdue by assignee", [(label, float(value)) for label, value in overdue_items]),
                    "rows": [{"assignee": label, "overdue": value} for label, value in overdue_items],
                },
                {
                    "title": "Due-date buckets",
                    "svg": _svg_bar_chart("Due-date buckets", [(label, float(value)) for label, value in due_items], color="#0f766e"),
                    "rows": [{"bucket": label, "count": value} for label, value in due_items],
                },
                {
                    "title": "Recent updates",
                    "rows": recent_updates[:15],
                },
            ],
            output,
        )
    elif normalized_format == "png":
        _write_png_report(report_title, [(label, float(value)) for label, value in status_items], output)
    else:
        raise RuntimeError("file_format must be 'html' or 'png'.")
    return {
        "project": _normalize_project_ref(project) if project is not None else DEFAULT_PROJECT,
        "fileFormat": normalized_format,
        "path": str(output),
        "totalWorkPackages": len(work_packages),
        "statusDistribution": dict(status_counter),
        "overdueByAssignee": dict(assignee_overdue),
        "dueBuckets": dict(due_bucket_counter),
    }


if __name__ == "__main__":
    mcp.run()
