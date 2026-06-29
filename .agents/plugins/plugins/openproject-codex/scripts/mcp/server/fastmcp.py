"""Tiny FastMCP-compatible shim for stdio tool serving."""

from __future__ import annotations

import inspect
import json
import sys
import types
from typing import Any, get_args, get_origin


class FastMCP:
    def __init__(self, name: str, instructions: str | None = None) -> None:
        self.name = name
        self.instructions = instructions or ""
        self._tools: dict[str, dict[str, Any]] = {}

    def tool(self):
        def decorator(func):
            self._tools[func.__name__] = {
                "name": func.__name__,
                "description": inspect.getdoc(func) or "",
                "inputSchema": _schema_for_callable(func),
                "handler": func,
            }
            return func

        return decorator

    def run(self) -> None:
        while True:
            message = _read_message()
            if message is None:
                break
            response = self._handle_request(message)
            if response is not None:
                _write_message(response)

    def _handle_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return _success_response(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": "local-shim"},
                    "instructions": self.instructions,
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _success_response(request_id, {})
        if method == "tools/list":
            return _success_response(
                request_id,
                {
                    "tools": [
                        {
                            "name": spec["name"],
                            "description": spec["description"],
                            "inputSchema": spec["inputSchema"],
                        }
                        for spec in self._tools.values()
                    ]
                },
            )
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            spec = self._tools.get(tool_name)
            if spec is None:
                return _success_response(request_id, _tool_result({"error": f"Unknown tool '{tool_name}'."}, is_error=True))
            try:
                payload = spec["handler"](**arguments)
                return _success_response(request_id, _tool_result(payload))
            except Exception as exc:
                return _success_response(
                    request_id,
                    _tool_result({"error": str(exc)}, is_error=True),
                )
        return _error_response(request_id, -32601, f"Unsupported method: {method}")


def _schema_for_callable(func) -> dict[str, Any]:
    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in sig.parameters.items():
        if parameter.kind not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
            continue
        annotation = parameter.annotation
        properties[name] = _annotation_to_schema(annotation)
        if parameter.default is inspect._empty:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect._empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    if origin is None:
        if annotation is str:
            return {"type": "string"}
        if annotation in {int, float}:
            return {"type": "number"}
        if annotation is bool:
            return {"type": "boolean"}
        if annotation is dict or annotation is Any:
            return {"type": "object"}
        if annotation is list:
            return {"type": "array"}
        return {"type": "string"}
    if origin in {list, tuple, set}:
        args = get_args(annotation)
        item_schema = _annotation_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin is dict:
        return {"type": "object"}
    if origin is Any:
        return {"type": "object"}
    if origin in {types.UnionType}:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _annotation_to_schema(args[0])
        return {"type": "string"}
    if str(origin).endswith("Union"):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _annotation_to_schema(args[0])
        return {"type": "string"}
    return {"type": "string"}


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    if is_error:
        result["isError"] = True
    return result


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\n", b"\r\n"}:
            break
        header = line.decode("utf-8").strip()
        if ":" in header:
            key, value = header.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _success_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": payload}
