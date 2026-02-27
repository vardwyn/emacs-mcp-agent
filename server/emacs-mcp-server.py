#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import difflib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_SPECS = (
    {
        "name": "emacs.health",
        "description": "Readiness check for Python MCP server + Emacs bridge.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.get_project_root",
        "description": "Get the project root path used by the MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.get_selection",
        "description": "Get current editor selection.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.submit_diff",
        "description": "Submit a small, single-file diff for review in Emacs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "description": {"type": "string"},
                "diff": {"type": "string"},
            },
            "required": ["path", "description", "diff"],
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.submit_apply_patch",
        "description": "Submit an apply_patch-format patch for one file; server converts it to unified diff for Emacs review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "description": {"type": "string"},
                "patch": {"type": "string"},
            },
            "required": ["path", "description", "patch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.feedback_list",
        "description": "List unread per-file feedback items.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.feedback_get",
        "description": "Get one unread feedback item and mark it consumed.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
)
TOOL_NAMES = tuple(spec["name"] for spec in TOOL_SPECS)
SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"
TOOLS_CALL_BASE_PARAM_KEYS = frozenset({"name", "arguments"})
TOOLS_CALL_META_PARAM_KEYS = frozenset({"_meta"})
TOOLS_CALL_TASK_PARAM_KEYS = frozenset({"task"})
BRIDGE_CONNECT_TIMEOUT_S = 1.0
BRIDGE_READ_TIMEOUT_S = 5.0
# Size limit semantics:
# - *_MAX_REQUEST_LINE_BYTES and BRIDGE_MAX_LINE_BYTES are raw transport line bytes.
# - *_MAX_*_BYTES for payload fields are UTF-8 encoded byte sizes.
BRIDGE_MAX_LINE_BYTES = 1024 * 1024
MCP_MAX_REQUEST_LINE_BYTES = 1024 * 1024
SUBMIT_MAX_DIFF_BYTES = 256 * 1024
SUBMIT_MAX_PATCH_BYTES = 256 * 1024
SUBMIT_MAX_DESCRIPTION_BYTES = 256 * 1024
SELECTION_MAX_TEXT_BYTES = 256 * 1024
SELECTION_MAX_FILE_BYTES = 16 * 1024


# JSON response templates
def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_ok(
    request_id: Any, structured_content: dict[str, Any], text: str = "ok"
) -> dict[str, Any]:
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured_content,
        },
    )


def _tool_err(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return _rpc_result(
        request_id,
        {
            "isError": True,
            "content": [{"type": "text", "text": message}],
            "structuredContent": {"error": {"code": code, "message": message}},
        },
    )


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _is_valid_request_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int, float))


def _is_valid_progress_token(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int, float))


@dataclass(frozen=True)
class ServerPaths:
    """Filesystem paths used by the server."""

    project_root: Path
    state_dir: Path
    active_dir: Path
    active_index_path: Path
    before_dir: Path
    feedback_dir: Path
    feedback_inbox_dir: Path
    feedback_pending_dir: Path

    socket_path: Path


@dataclass
class ToolCallContext:
    """Context object passed to tool handlers."""

    tool_name: str
    arguments: dict[str, Any]

class ServerError(Exception):
    """Base class for server-level errors."""

class ToolError(ServerError):
    """Tool execution error with stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PathValidationError(ServerError):
    """Internal path validation error with a stable kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class EmacsRpcClient:
    """Unix-socket JSON bridge to Emacs."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a bridge method on the Emacs side."""
        assert isinstance(method, str) and method, "Bridge method must be a non-empty string"
        if params is None:
            params = {}
        assert isinstance(params, dict), "Bridge params must be an object"

        request_id = self._next_id
        self._next_id += 1

        request = {"id": request_id, "method": method, "params": params}
        request_line = json.dumps(request, separators=(",", ":")) + "\n"

        response_line: bytes
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client_socket:
                client_socket.settimeout(BRIDGE_CONNECT_TIMEOUT_S)
                client_socket.connect(str(self.socket_path))
                client_socket.settimeout(BRIDGE_READ_TIMEOUT_S)
                client_socket.sendall(request_line.encode("utf-8"))
                response_line = self._readline_with_size_limit(client_socket, BRIDGE_MAX_LINE_BYTES)
        except socket.timeout:
            raise ToolError("emacs_error", "Timed out waiting for Emacs bridge response")
        except (FileNotFoundError, ConnectionRefusedError):
            raise ToolError("emacs_unreachable", "Emacs bridge socket is unavailable")
        except OSError as exc:
            raise ToolError("emacs_unreachable", f"Failed to reach Emacs bridge: {exc}")

        try:
            response = json.loads(response_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ToolError("invalid_response", "Emacs bridge returned invalid JSON")

        if not isinstance(response, dict):
            raise ToolError("invalid_response", "Emacs bridge response must be an object")

        jsonrpc = response.get("jsonrpc")
        if jsonrpc is not None and jsonrpc != "2.0":
            raise ToolError("invalid_response", "Emacs bridge response has invalid jsonrpc version")

        if response.get("id") != request_id:
            raise ToolError("invalid_response", "Emacs bridge response id mismatch")

        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise ToolError(
                "invalid_response",
                "Emacs bridge response must contain exactly one of result/error",
            )

        if has_error:
            error_value = response["error"]
            if isinstance(error_value, dict):
                code = error_value.get("code")
                message = error_value.get("message")
                code_part = f"{code}" if code is not None else "unknown"
                message_part = (
                    str(message) if isinstance(message, str) and message else "Unknown error"
                )
            else:
                code_part = "unknown"
                message_part = "Malformed error payload"
            raise ToolError("emacs_error", f"Emacs error {code_part}: {message_part}")

        result = response.get("result")
        if not isinstance(result, dict):
            raise ToolError("invalid_response", "Emacs bridge result must be an object")
        return result

    def _readline_with_size_limit(self, client_socket: socket.socket, max_length: int) -> bytes:
        data = bytearray()
        while True:
            chunk = client_socket.recv(4096)
            if not chunk:
                raise ToolError("invalid_response", "Emacs bridge closed without a newline response")
            data.extend(chunk)
            if len(data) > max_length:
                raise ToolError("invalid_response", "Emacs bridge response exceeded size limit")
            newline_index = data.find(b"\n")
            if newline_index >= 0:
                return bytes(data[:newline_index])


class EmacsMcpServer:
    """Main MCP server implementation over stdio."""

    ACTIVE_INDEX_SCHEMA_VERSION = 1

    def __init__(self, paths: ServerPaths, emacs_client: EmacsRpcClient) -> None:
        self.paths = paths
        self.emacs_client = emacs_client
        self.is_initialized = False
        self.client_initialized = False
        self.negotiated_protocol_version: str | None = None
        self._ensure_private_dir(self.paths.socket_path.parent)

    def run(self) -> int:
        """Read MCP requests from stdin and write responses to stdout."""
        while True:
            raw_line = sys.stdin.buffer.readline(MCP_MAX_REQUEST_LINE_BYTES + 1)
            if not raw_line:
                break

            if len(raw_line) > MCP_MAX_REQUEST_LINE_BYTES:
                self._write_response(_rpc_error(None, -32600, "Invalid Request: request line too large"))
                return 1

            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                self._write_response(_rpc_error(None, -32700, "Parse error"))
                continue

            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write_response(_rpc_error(None, -32700, "Parse error"))
                continue

            try:
                response = self.handle_rpc_request(request)
            except Exception:
                request_id = request.get("id") if isinstance(request, dict) else None
                response = _rpc_error(request_id, -32603, "Internal error")

            # JSON-RPC notifications (requests without "id") must not receive responses.
            if response is None:
                continue

            self._write_response(response)

        return 0

    def _write_response(self, response: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def handle_rpc_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch a single JSON-RPC request."""
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "Invalid Request")

        has_id = "id" in request
        request_id = request.get("id") if has_id else None

        jsonrpc = request.get("jsonrpc")
        if jsonrpc != "2.0":
            return _rpc_error(request_id if has_id else None, -32600, "Invalid Request")

        method = request.get("method")
        if not isinstance(method, str) or not method:
            return None if not has_id else _rpc_error(request_id, -32600, "Invalid Request")

        if has_id and not _is_valid_request_id(request_id):
            return _rpc_error(None, -32600, "Invalid Request")

        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if not has_id else _rpc_error(request_id, -32602, "Invalid params: expected object")

        # JSON-RPC notifications (requests without "id") must not receive responses.
        if not has_id:
            if method == "notifications/initialized":
                self.client_initialized = True
            return None

        if method == "ping":
            return _rpc_result(request_id, {})

        if method == "initialize":
            if not self.is_initialized:
                return self.handle_initialize(request_id=request_id, params=params)
            else:
                return _rpc_error(request_id, -32600, "Already initialized")

        if not self.is_initialized:
            return _rpc_error(request_id, -32602, "Server not initialized")

        if method == "tools/list":
            return self.handle_tools_list(request_id=request_id)
        if method == "tools/call":
            return self.handle_tools_call(request_id=request_id, params=params)
        if method == "shutdown":
            return _rpc_result(request_id, {})

        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    def handle_initialize(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Handle MCP initialize."""
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params: expected object")

        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            return _rpc_error(request_id, -32602, "Invalid params: protocolVersion must be a string")

        capabilities = params.get("capabilities")
        if not isinstance(capabilities, dict):
            return _rpc_error(request_id, -32602, "Invalid params: capabilities must be an object")

        client_info = params.get("clientInfo")
        if not isinstance(client_info, dict):
            return _rpc_error(request_id, -32602, "Invalid params: clientInfo must be an object")
        client_name = client_info.get("name")
        client_version = client_info.get("version")
        if not isinstance(client_name, str) or not client_name:
            return _rpc_error(request_id, -32602, "Invalid params: clientInfo.name must be a string")
        if not isinstance(client_version, str) or not client_version:
            return _rpc_error(
                request_id, -32602, "Invalid params: clientInfo.version must be a string"
            )

        protocol_version = (
            requested if requested in SUPPORTED_MCP_PROTOCOL_VERSIONS else DEFAULT_MCP_PROTOCOL_VERSION
        )

        self.is_initialized = True
        self.client_initialized = False
        self.negotiated_protocol_version = protocol_version
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "emacs-mcp-server", "version": "0.1.0"},
            },
        )

    def handle_tools_list(self, request_id: Any) -> dict[str, Any]:
        """Handle tools/list."""
        tools = [
            {
                "name": spec["name"],
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
            for spec in TOOL_SPECS
        ]
        return _rpc_result(request_id, {"tools": tools})

    def handle_tools_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Handle tools/call."""
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params: expected object")

        allowed_keys = set(TOOLS_CALL_BASE_PARAM_KEYS)
        allowed_keys.update(TOOLS_CALL_META_PARAM_KEYS)
        if self.negotiated_protocol_version == "2025-11-25":
            allowed_keys.update(TOOLS_CALL_TASK_PARAM_KEYS)
        unknown_keys = sorted(set(params.keys()) - allowed_keys)
        if unknown_keys:
            return _rpc_error(
                request_id, -32602, f"Invalid params: unexpected keys: {', '.join(unknown_keys)}"
            )

        if "_meta" in params:
            metadata = params.get("_meta")
            if not isinstance(metadata, dict):
                return _rpc_error(request_id, -32602, "Invalid params: _meta must be an object")
            progress_token = metadata.get("progressToken")
            if "progressToken" in metadata and not _is_valid_progress_token(progress_token):
                return _rpc_error(
                    request_id,
                    -32602,
                    "Invalid params: _meta.progressToken must be a string or number",
                )

        if "task" in params:
            task_metadata = params.get("task")
            if not isinstance(task_metadata, dict):
                return _rpc_error(request_id, -32602, "Invalid params: task must be an object")
            ttl = task_metadata.get("ttl")
            if "ttl" in task_metadata and (
                not isinstance(ttl, (int, float)) or isinstance(ttl, bool)
            ):
                return _rpc_error(request_id, -32602, "Invalid params: task.ttl must be a number")

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return _rpc_error(request_id, -32602, "Invalid params: missing or invalid tool name")
        if tool_name not in TOOL_NAMES:
            return _rpc_error(request_id, -32601, f"Unknown tool: {tool_name}")

        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _rpc_error(request_id, -32602, "Invalid params: arguments must be an object")

        try:
            validated_arguments = self.validate_tool_arguments(tool_name, arguments)
            payload = self.dispatch_tool(
                ToolCallContext(tool_name=tool_name, arguments=validated_arguments)
            )
            return _tool_ok(request_id, payload)
        except ToolError as exc:
            return _tool_err(request_id, exc.code, exc.message)

    def validate_tool_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        validators: dict[str, Any] = {
            "emacs.health": self._validate_empty_arguments,
            "emacs.get_project_root": self._validate_empty_arguments,
            "emacs.get_selection": self._validate_empty_arguments,
            "emacs.feedback_list": self._validate_empty_arguments,
            "emacs.submit_diff": self._validate_submit_diff_arguments,
            "emacs.submit_apply_patch": self._validate_submit_apply_patch_arguments,
            "emacs.feedback_get": self._validate_feedback_get_arguments,
        }
        validator = validators.get(tool_name)
        assert validator is not None, f"Unhandled tool in validate_tool_arguments: {tool_name}"
        return validator(tool_name, arguments)

    def _validate_empty_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_no_keys(arguments, tool_name)
        return {}

    def _validate_submit_diff_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_allowed_keys(arguments, {"path", "description", "diff"}, tool_name)
        path = arguments.get("path")
        description = arguments.get("description")
        diff = arguments.get("diff")

        if not isinstance(path, str) or not path:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'path' must be a non-empty string",
            )
        self._validate_path_from_tool(path, tool_name, "path")

        if not isinstance(description, str) or not description:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'description' must be a non-empty string",
            )
        if _utf8_len(description) > SUBMIT_MAX_DESCRIPTION_BYTES:
            raise ToolError(
                "invalid_arguments",
                (
                    f"Invalid arguments for {tool_name}: 'description' is too large "
                    f"(max {SUBMIT_MAX_DESCRIPTION_BYTES} bytes)"
                ),
            )

        if not isinstance(diff, str) or not diff:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'diff' must be a non-empty string",
            )
        if _utf8_len(diff) > SUBMIT_MAX_DIFF_BYTES:
            raise ToolError(
                "invalid_arguments",
                (
                    f"Invalid arguments for {tool_name}: 'diff' is too large "
                    f"(max {SUBMIT_MAX_DIFF_BYTES} bytes)"
                ),
            )

        return {"path": path, "description": description, "diff": diff}

    def _validate_submit_apply_patch_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_allowed_keys(arguments, {"path", "description", "patch"}, tool_name)
        path = arguments.get("path")
        description = arguments.get("description")
        patch = arguments.get("patch")

        if not isinstance(path, str) or not path:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'path' must be a non-empty string",
            )
        self._validate_path_from_tool(path, tool_name, "path")

        if not isinstance(description, str) or not description:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'description' must be a non-empty string",
            )
        if _utf8_len(description) > SUBMIT_MAX_DESCRIPTION_BYTES:
            raise ToolError(
                "invalid_arguments",
                (
                    f"Invalid arguments for {tool_name}: 'description' is too large "
                    f"(max {SUBMIT_MAX_DESCRIPTION_BYTES} bytes)"
                ),
            )

        if not isinstance(patch, str) or not patch:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'patch' must be a non-empty string",
            )
        if _utf8_len(patch) > SUBMIT_MAX_PATCH_BYTES:
            raise ToolError(
                "invalid_arguments",
                (
                    f"Invalid arguments for {tool_name}: 'patch' is too large "
                    f"(max {SUBMIT_MAX_PATCH_BYTES} bytes)"
                ),
            )

        return {"path": path, "description": description, "patch": patch}

    def _validate_feedback_get_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_allowed_keys(arguments, {"id"}, tool_name)
        feedback_id = arguments.get("id")
        if not isinstance(feedback_id, int) or isinstance(feedback_id, bool):
            # bool is a subclass of int in python!!
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'id' must be an integer",
            )
        if feedback_id <= 0:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: 'id' must be a positive integer",
            )
        return {"id": feedback_id}

    def _validate_selection_point(self, key: str, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ToolError("invalid_response", f"Invalid emacs.get_selection '{key}': expected object")
        unknown_keys = sorted(set(value.keys()) - {"line", "col", "pos"})
        if unknown_keys:
            raise ToolError(
                "invalid_response",
                f"Invalid emacs.get_selection '{key}': unexpected keys: {', '.join(unknown_keys)}",
            )

        line = value.get("line")
        col = value.get("col")
        pos = value.get("pos")
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            raise ToolError("invalid_response", f"Invalid emacs.get_selection '{key}.line'")
        if not isinstance(col, int) or isinstance(col, bool) or col < 0:
            raise ToolError("invalid_response", f"Invalid emacs.get_selection '{key}.col'")
        if not isinstance(pos, int) or isinstance(pos, bool) or pos < 0:
            raise ToolError("invalid_response", f"Invalid emacs.get_selection '{key}.pos'")
        return {"line": line, "col": col, "pos": pos}

    def _validate_get_selection_result(self, result: dict[str, Any]) -> dict[str, Any]:
        ok = result.get("ok")
        if not isinstance(ok, bool):
            raise ToolError("invalid_response", "Invalid emacs.get_selection result: missing boolean 'ok'")

        if ok:
            unknown_keys = sorted(set(result.keys()) - {"ok", "file", "start", "end", "text"})
            if unknown_keys:
                raise ToolError(
                    "invalid_response",
                    (
                        "Invalid emacs.get_selection result: unexpected keys: "
                        f"{', '.join(unknown_keys)}"
                    ),
                )

            file_path = result.get("file")
            text = result.get("text")
            if not isinstance(file_path, str) or not file_path:
                raise ToolError("invalid_response", "Invalid emacs.get_selection result: invalid 'file'")
            if _utf8_len(file_path) > SELECTION_MAX_FILE_BYTES:
                raise ToolError(
                    "invalid_response",
                    (
                        "Invalid emacs.get_selection result: 'file' is too large "
                        f"(max {SELECTION_MAX_FILE_BYTES} bytes)"
                    ),
                )
            try:
                self._validate_repo_path(file_path)
            except PathValidationError as exc:
                raise ToolError(
                    "invalid_response",
                    f"Invalid emacs.get_selection result: invalid 'file' path ({exc.message})",
                )

            if not isinstance(text, str):
                raise ToolError("invalid_response", "Invalid emacs.get_selection result: invalid 'text'")
            if _utf8_len(text) > SELECTION_MAX_TEXT_BYTES:
                raise ToolError(
                    "invalid_response",
                    (
                        "Invalid emacs.get_selection result: 'text' is too large "
                        f"(max {SELECTION_MAX_TEXT_BYTES} bytes)"
                    ),
                )

            start = self._validate_selection_point("start", result.get("start"))
            end = self._validate_selection_point("end", result.get("end"))
            return {"ok": True, "file": file_path, "start": start, "end": end, "text": text}

        unknown_keys = sorted(set(result.keys()) - {"ok", "reason"})
        if unknown_keys:
            raise ToolError(
                "invalid_response",
                (
                    "Invalid emacs.get_selection result: unexpected keys: "
                    f"{', '.join(unknown_keys)}"
                ),
            )
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ToolError("invalid_response", "Invalid emacs.get_selection result: invalid 'reason'")
        return {"ok": False, "reason": reason}

    def _validate_bridge_project_root_result(self, result: Any) -> str:
        if not isinstance(result, dict):
            raise ToolError("invalid_response", "Invalid emacs.get_project_root result: expected object")

        unknown_keys = sorted(set(result.keys()) - {"ok", "project_root"})
        if unknown_keys:
            raise ToolError(
                "invalid_response",
                (
                    "Invalid emacs.get_project_root result: unexpected keys: "
                    f"{', '.join(unknown_keys)}"
                ),
            )

        ok = result.get("ok")
        if not isinstance(ok, bool):
            raise ToolError(
                "invalid_response",
                "Invalid emacs.get_project_root result: missing boolean 'ok'",
            )
        if not ok:
            raise ToolError("not_ready", "emacs-mcp not ready: emacs root provider returned ok=false")

        bridge_root = result.get("project_root")
        if not isinstance(bridge_root, str) or not bridge_root:
            raise ToolError(
                "invalid_response",
                "Invalid emacs.get_project_root result: invalid 'project_root'",
            )

        canonical_bridge_root = os.path.realpath(bridge_root)
        if not canonical_bridge_root:
            raise ToolError(
                "invalid_response",
                "Invalid emacs.get_project_root result: empty canonical project root",
            )
        return canonical_bridge_root


    def _validate_no_keys(self, arguments: dict[str, Any], tool_name: str) -> None:
        if arguments:
            keys = ", ".join(sorted(arguments.keys()))
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: unexpected keys: {keys}",
            )

    def _validate_allowed_keys(
        self, arguments: dict[str, Any], allowed_keys: set[str], tool_name: str
    ) -> None:
        unknown = sorted(set(arguments.keys()) - allowed_keys)
        if unknown:
            keys = ", ".join(unknown)
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: unexpected keys: {keys}",
            )

    def _validate_repo_path(self, path: str) -> Path:
        if not path:
            raise PathValidationError("invalid_token", "contains empty path")
        if "\x00" in path:
            raise PathValidationError("invalid_token", "contains NUL byte")
        if os.path.isabs(path):
            raise PathValidationError("invalid_token", "must be repo-relative")
        if ".." in Path(path).parts:
            raise PathValidationError("invalid_token", "path traversal is not allowed")

        candidate = self.paths.project_root / path
        resolved = Path(os.path.realpath(str(candidate)))
        try:
            resolved.relative_to(self.paths.project_root)
        except ValueError:
            raise PathValidationError("outside_project_root", "escapes project root")
        return resolved

    def _validate_path_from_tool(self, path: str, tool_name: str, key: str) -> Path:
        try:
            return self._validate_repo_path(path)
        except PathValidationError as exc:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: '{key}' {exc.message}: {path}",
            )

    def _validate_path_from_index(self, path: str) -> Path:
        try:
            return self._validate_repo_path(path)
        except PathValidationError as exc:
            raise ToolError("state_corrupt", f"Active index has invalid path: {path} ({exc.message})")

    def _ensure_private_dir(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to prepare private directory {path}: {exc}")

    def _set_private_file_mode(self, path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to set private file mode for {path}: {exc}")

    def _fsync_parent_dir(self, target_path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            dir_fd = os.open(str(target_path.parent), flags)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to open directory for sync {target_path.parent}: {exc}")
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to sync directory {target_path.parent}: {exc}")
        finally:
            os.close(dir_fd)

    def _ensure_submit_state_dirs(self) -> None:
        self._ensure_private_dir(self.paths.state_dir)
        self._ensure_private_dir(self.paths.active_dir)
        self._ensure_private_dir(self.paths.before_dir)

    def _ensure_feedback_dirs(self) -> None:
        self._ensure_private_dir(self.paths.feedback_dir)
        self._ensure_private_dir(self.paths.feedback_inbox_dir)
        self._ensure_private_dir(self.paths.feedback_pending_dir)

    def _load_active_index(self) -> dict[str, Any]:
        if not self.paths.active_index_path.exists():
            return {"schema_version": self.ACTIVE_INDEX_SCHEMA_VERSION, "active_files": {}}

        try:
            raw_text = self.paths.active_index_path.read_text(encoding="utf-8")
            raw_value = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError("state_corrupt", f"Failed to load active index: {exc}")

        if not isinstance(raw_value, dict):
            raise ToolError("state_corrupt", "Active index is not an object")

        schema_version = raw_value.get("schema_version")
        active_files = raw_value.get("active_files")
        if schema_version != self.ACTIVE_INDEX_SCHEMA_VERSION:
            raise ToolError(
                "state_corrupt",
                (
                    f"Unsupported active index schema: {schema_version}; "
                    f"expected {self.ACTIVE_INDEX_SCHEMA_VERSION}"
                ),
            )
        if not isinstance(active_files, dict):
            raise ToolError("state_corrupt", "Active index 'active_files' must be an object")

        normalized_active_files: dict[str, dict[str, str]] = {}
        for rel_path, meta in active_files.items():
            if not isinstance(rel_path, str):
                raise ToolError("state_corrupt", "Active index path key must be a string")
            self._validate_path_from_index(rel_path)
            if not isinstance(meta, dict):
                raise ToolError("state_corrupt", f"Active index entry must be an object: {rel_path}")
            before_kind = meta.get("before_kind")
            if before_kind not in {"present", "missing"}:
                raise ToolError(
                    "state_corrupt",
                    (
                        f"Active index entry has invalid before_kind for {rel_path}: "
                        f"{before_kind}"
                    ),
                )
            normalized_active_files[rel_path] = {"before_kind": before_kind}

        return {
            "schema_version": self.ACTIVE_INDEX_SCHEMA_VERSION,
            "active_files": normalized_active_files,
        }

    def _save_active_index(self, index: dict[str, Any]) -> None:
        self._write_json_atomic(self.paths.active_index_path, index)

    def _atomic_write_bytes(self, target_path: Path, payload: bytes) -> None:
        self._ensure_private_dir(target_path.parent)
        temp_fd = -1
        temp_path: Path | None = None
        try:
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.tmp-",
                dir=str(target_path.parent),
            )
            temp_path = Path(temp_name)
            os.chmod(temp_path, 0o600)
            with os.fdopen(temp_fd, "wb") as temp_file:
                temp_fd = -1
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, target_path)
            self._set_private_file_mode(target_path)
            self._fsync_parent_dir(target_path)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to write state file {target_path}: {exc}")
        finally:
            if temp_fd >= 0:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _atomic_write_text(self, target_path: Path, payload: str) -> None:
        self._atomic_write_bytes(target_path, payload.encode("utf-8"))

    def _write_json_atomic(self, target_path: Path, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        self._atomic_write_text(target_path, body)

    def _before_snapshot_path(self, rel_path: str) -> Path:
        return self.paths.before_dir / rel_path

    def _is_file_active(self, rel_path: str, index: dict[str, Any]) -> bool:
        active_files = index.get("active_files", {})
        assert isinstance(active_files, dict), "active_files must be a dict"
        return rel_path in active_files

    def _create_before_snapshot_if_needed(
        self, rel_path: str, abs_path: Path, index: dict[str, Any]
    ) -> bool:
        if self._is_file_active(rel_path, index):
            return False

        active_files = index["active_files"]
        assert isinstance(active_files, dict), "active_files must be a dict"

        before_snapshot_path = self._before_snapshot_path(rel_path)
        try:
            if abs_path.exists():
                if not abs_path.is_file():
                    raise ToolError(
                        "invalid_arguments",
                        f"Invalid path for emacs.submit_diff: not a regular file: {rel_path}",
                    )
                self._atomic_write_bytes(before_snapshot_path, abs_path.read_bytes())
                active_files[rel_path] = {"before_kind": "present"}
            else:
                active_files[rel_path] = {"before_kind": "missing"}
        except OSError as exc:
            raise ToolError(
                "io_error",
                f"Failed to create BEFORE snapshot for {rel_path}: {exc}",
            )
        return True

    def _clear_active_file(self, rel_path: str, index: dict[str, Any]) -> None:
        active_files = index.get("active_files", {})
        assert isinstance(active_files, dict), "active_files must be a dict"
        active_files.pop(rel_path, None)

    def _cleanup_before_snapshot(self, rel_path: str) -> None:
        before_snapshot_path = self._before_snapshot_path(rel_path)
        try:
            if before_snapshot_path.exists():
                before_snapshot_path.unlink()
        except OSError as exc:
            raise ToolError("io_error", f"Failed to remove BEFORE snapshot for {rel_path}: {exc}")

        parent = before_snapshot_path.parent
        while parent != self.paths.before_dir and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _rollback_submit_diff_active_entry(self, rel_path: str, index: dict[str, Any]) -> None:
        """Best-effort rollback when Emacs append fails after creating active state."""
        try:
            self._clear_active_file(rel_path, index)
        except Exception:
            return

        index_rolled_back = False
        try:
            self._save_active_index(index)
            index_rolled_back = True
        except Exception:
            pass

        if index_rolled_back:
            try:
                self._cleanup_before_snapshot(rel_path)
            except Exception:
                pass

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            raise ToolError("io_error", f"Failed to read file {path}: {exc}")

    def _read_before_state(self, rel_path: str, before_kind: str) -> tuple[str, bool]:
        if before_kind == "missing":
            return ("", False)
        before_snapshot_path = self._before_snapshot_path(rel_path)
        if not before_snapshot_path.exists():
            raise ToolError(
                "state_corrupt",
                f"Missing BEFORE snapshot for active file: {rel_path}",
            )
        if not before_snapshot_path.is_file():
            raise ToolError(
                "state_corrupt",
                f"BEFORE snapshot is not a file: {before_snapshot_path}",
            )
        return (self._read_text_file(before_snapshot_path), True)

    def _read_after_state(self, abs_path: Path, rel_path: str) -> tuple[str, bool]:
        if not abs_path.exists():
            return ("", False)
        if not abs_path.is_file():
            raise ToolError(
                "state_corrupt",
                f"Current file path is not a regular file: {rel_path}",
            )
        return (self._read_text_file(abs_path), True)

    def _generate_applied_diff(
        self,
        rel_path: str,
        before_text: str,
        before_exists: bool,
        after_text: str,
        after_exists: bool,
    ) -> str:
        if before_exists == after_exists and before_text == after_text:
            return ""

        path_for_header = Path(rel_path).as_posix()
        fromfile = f"a/{path_for_header}" if before_exists else "/dev/null"
        tofile = f"b/{path_for_header}" if after_exists else "/dev/null"

        body_lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=fromfile,
                tofile=tofile,
                lineterm="",
            )
        )
        if not body_lines:
            return ""

        header = f"diff --git a/{path_for_header} b/{path_for_header}\n"
        return header + "\n".join(body_lines).rstrip("\n") + "\n"

    def _feedback_next_id_path(self) -> Path:
        return self.paths.feedback_dir / "next_id.txt"

    def _allocate_feedback_id(self) -> int:
        next_id_path = self._feedback_next_id_path()
        next_id_value = 1

        if next_id_path.exists():
            try:
                raw_text = next_id_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ToolError("io_error", f"Failed to read feedback ID state: {exc}")
            try:
                next_id_value = int(raw_text)
            except ValueError:
                raise ToolError("state_corrupt", f"Invalid feedback ID state value: {raw_text!r}")
            if not isinstance(next_id_value, int) or next_id_value <= 0:
                raise ToolError(
                    "state_corrupt",
                    f"Invalid feedback next ID value: {next_id_value}",
                )
        else:
            max_pending_id = 0
            for pending_path in self._list_pending_paths():
                pending_id = int(pending_path.stem)
                if pending_id > max_pending_id:
                    max_pending_id = pending_id
            next_id_value = max_pending_id + 1
            if not isinstance(next_id_value, int) or next_id_value <= 0:
                raise ToolError(
                    "state_corrupt",
                    f"Invalid computed feedback next ID: {next_id_value}",
                )

        allocated_id = next_id_value
        self._atomic_write_text(next_id_path, f"{allocated_id + 1}\n")
        return allocated_id

    def _pending_item_path(self, feedback_id: int) -> Path:
        assert isinstance(feedback_id, int) and not isinstance(feedback_id, bool) and feedback_id > 0, (
            "feedback id must be a positive integer"
        )
        return self.paths.feedback_pending_dir / f"{feedback_id}.json"

    def _load_finalize_event(self, inbox_path: Path) -> dict[str, Any]:
        try:
            raw_text = inbox_path.read_text(encoding="utf-8")
            raw_value = json.loads(raw_text)
        except OSError as exc:
            raise ToolError("io_error", f"Failed to read feedback inbox event {inbox_path}: {exc}")
        except json.JSONDecodeError as exc:
            raise ToolError("state_corrupt", f"Invalid JSON in feedback inbox event {inbox_path}: {exc}")
        if not isinstance(raw_value, dict):
            raise ToolError("state_corrupt", f"Feedback inbox event is not an object: {inbox_path}")
        return raw_value

    def _extract_finalize_event(self, inbox_path: Path) -> tuple[str, str]:
        event = self._load_finalize_event(inbox_path)

        schema_version = event.get("schema_version")
        if schema_version != 1:
            raise ToolError(
                "state_corrupt",
                f"Unsupported feedback inbox event schema in {inbox_path}: {schema_version}",
            )

        rel_path = event.get("path")
        if not isinstance(rel_path, str) or not rel_path:
            raise ToolError("state_corrupt", f"Invalid feedback inbox event path in {inbox_path}")

        user_message = event.get("user_message", "")
        if user_message is None:
            user_message = ""
        if not isinstance(user_message, str):
            raise ToolError(
                "state_corrupt",
                f"Invalid feedback inbox event user_message in {inbox_path}",
            )
        return (rel_path, user_message)

    def _process_feedback_inbox(self) -> None:
        self._ensure_feedback_dirs()
        self._ensure_submit_state_dirs()

        active_index = self._load_active_index()

        inbox_files = sorted(
            path
            for path in self.paths.feedback_inbox_dir.iterdir()
            if path.is_file() and path.suffix == ".json"
        )
        for inbox_path in inbox_files:
            rel_path, user_message = self._extract_finalize_event(inbox_path)
            abs_path = self._validate_path_from_index(rel_path)
            before_snapshot_path = self._before_snapshot_path(rel_path)

            applied_diff = ""
            active_files = active_index.get("active_files", {})
            assert isinstance(active_files, dict), "active_files must be a dict"
            active_entry = active_files.get(rel_path)
            if isinstance(active_entry, dict):
                before_kind = active_entry.get("before_kind")
                if before_kind not in {"present", "missing"}:
                    raise ToolError(
                        "state_corrupt",
                        f"Active index entry has invalid before_kind for {rel_path}: {before_kind}",
                    )
                before_text, before_exists = self._read_before_state(rel_path, before_kind)
                after_text, after_exists = self._read_after_state(abs_path, rel_path)
                applied_diff = self._generate_applied_diff(
                    rel_path=rel_path,
                    before_text=before_text,
                    before_exists=before_exists,
                    after_text=after_text,
                    after_exists=after_exists,
                )
                self._clear_active_file(rel_path, active_index)
                self._save_active_index(active_index)
            else:
                try:
                    has_before_snapshot = before_snapshot_path.exists()
                except OSError as exc:
                    raise ToolError(
                        "io_error",
                        f"Failed to inspect BEFORE snapshot for {rel_path}: {exc}",
                    )
                if has_before_snapshot:
                    raise ToolError(
                        "state_corrupt",
                        f"Found BEFORE snapshot without active index entry for {rel_path}",
                    )

            feedback_id = self._allocate_feedback_id()
            pending_path = self._pending_item_path(feedback_id)
            pending_item = {
                "schema_version": 1,
                "id": feedback_id,
                "path": rel_path,
                "applied_diff": applied_diff,
                "user_message": user_message,
            }
            self._write_json_atomic(pending_path, pending_item)

            try:
                inbox_path.unlink()
            except OSError as exc:
                raise ToolError(
                    "io_error",
                    f"Failed to clear processed feedback inbox event {inbox_path}: {exc}",
                )

            if isinstance(active_entry, dict):
                self._cleanup_before_snapshot(rel_path)

    def _load_pending_item(self, pending_path: Path) -> dict[str, Any]:
        try:
            raw_text = pending_path.read_text(encoding="utf-8")
            raw_value = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError("state_corrupt", f"Invalid pending feedback file {pending_path}: {exc}")

        if not isinstance(raw_value, dict):
            raise ToolError("state_corrupt", f"Pending feedback is not an object: {pending_path}")
        if raw_value.get("schema_version") != 1:
            raise ToolError(
                "state_corrupt",
                f"Unsupported pending feedback schema in {pending_path}",
            )

        feedback_id = raw_value.get("id")
        rel_path = raw_value.get("path")
        applied_diff = raw_value.get("applied_diff")
        user_message = raw_value.get("user_message")
        if (
            not isinstance(feedback_id, int)
            or isinstance(feedback_id, bool)
            or feedback_id <= 0
        ):
            raise ToolError("state_corrupt", f"Invalid feedback id in {pending_path}")
        if not isinstance(rel_path, str):
            raise ToolError("state_corrupt", f"Invalid feedback path in {pending_path}")
        self._validate_path_from_index(rel_path)
        if not isinstance(applied_diff, str):
            raise ToolError("state_corrupt", f"Invalid applied_diff in {pending_path}")
        if user_message is None:
            user_message = ""
        if not isinstance(user_message, str):
            raise ToolError("state_corrupt", f"Invalid user_message in {pending_path}")

        return {
            "schema_version": 1,
            "id": feedback_id,
            "path": rel_path,
            "applied_diff": applied_diff,
            "user_message": user_message,
        }

    def _list_pending_paths(self) -> list[Path]:
        self._ensure_feedback_dirs()
        pending_paths = [
            path
            for path in self.paths.feedback_pending_dir.iterdir()
            if path.is_file() and path.suffix == ".json"
        ]
        for path in pending_paths:
            assert path.stem.isdigit() and int(path.stem) > 0, (
                f"Pending feedback filename must be a positive integer id: {path.name}"
            )
        return sorted(pending_paths, key=lambda path: int(path.stem))

    def _raise_invalid_submit_diff(self, detail: str) -> None:
        raise ToolError(
            "invalid_arguments",
            f"Invalid arguments for emacs.submit_diff: invalid diff: {detail}",
        )

    def _generate_diff_from_apply_patch(
        self,
        rel_path: str,
        abs_path: Path,
        patch_text: str,
    ) -> str:
        def invalid(detail: str) -> None:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for emacs.submit_apply_patch: invalid patch: {detail}",
            )

        def parse_header(line: str) -> tuple[str, str] | None:
            prefixes = (
                ("update", "*** Update File: "),
                ("add", "*** Add File: "),
                ("delete", "*** Delete File: "),
            )
            for operation, prefix in prefixes:
                if line.startswith(prefix):
                    return (operation, line[len(prefix) :])
            return None

        normalized_text = patch_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not normalized_text:
            invalid("empty patch text")

        lines = normalized_text.split("\n")
        if lines[0] != "*** Begin Patch":
            invalid("missing '*** Begin Patch' header")
        if lines[-1] != "*** End Patch":
            invalid("missing '*** End Patch' footer")

        body_lines = lines[1:-1]
        if not body_lines:
            invalid("patch body is empty")

        header = parse_header(body_lines[0])
        if header is None:
            invalid("expected one of: Update File, Add File, Delete File")
        operation, patch_path = header
        if not patch_path:
            invalid("patch file path is empty")

        canonical_target = Path(rel_path).as_posix()
        canonical_patch_path = Path(patch_path).as_posix()
        if canonical_patch_path != canonical_target:
            invalid(f"patch file path does not match 'path' argument: {patch_path!r}")

        for line in body_lines[1:]:
            if parse_header(line) is not None:
                invalid("multiple file sections are not supported")

        before_text: str
        before_exists: bool
        after_text: str
        after_exists: bool

        if operation == "add":
            if abs_path.exists():
                invalid("add patch target already exists on disk")
            add_lines = body_lines[1:]
            if not add_lines:
                invalid("add patch has no content")
            for line in add_lines:
                if not line.startswith("+"):
                    invalid(f"invalid add patch line (expected '+'): {line!r}")
            before_text = ""
            before_exists = False
            after_text = "\n".join(line[1:] for line in add_lines) + "\n"
            after_exists = True
        elif operation == "delete":
            if len(body_lines) != 1:
                invalid("delete patch must not include change lines")
            before_text, before_exists = self._read_after_state(abs_path, rel_path)
            if not before_exists:
                invalid("delete patch target does not exist on disk")
            after_text = ""
            after_exists = False
        else:
            before_text, before_exists = self._read_after_state(abs_path, rel_path)
            if not before_exists:
                invalid("update patch target does not exist on disk")

            update_lines = body_lines[1:]
            if not update_lines:
                invalid("update patch has no body")
            if update_lines[0].startswith("*** Move to: "):
                invalid("move/rename directives are not supported")

            chunks: list[list[str]] = []
            current_chunk: list[str] | None = None
            saw_change_line = False
            for line in update_lines:
                if line.startswith("@@"):
                    current_chunk = []
                    chunks.append(current_chunk)
                    continue
                if line == "*** End of File":
                    continue
                if line.startswith("*** "):
                    invalid(f"unsupported update directive: {line!r}")
                if not line or line[0] not in {" ", "+", "-"}:
                    invalid(f"invalid update patch line (expected @@/+/-/space): {line!r}")
                if current_chunk is None:
                    invalid("update patch is missing '@@' before change lines")
                current_chunk.append(line)
                saw_change_line = True

            if not chunks:
                invalid("update patch is missing '@@' sections")
            if not saw_change_line:
                invalid("update patch does not contain any change lines")

            source_lines = before_text.splitlines()
            had_trailing_newline = before_text.endswith("\n")
            working_lines = list(source_lines)
            cursor = 0

            def find_match_index(needle: list[str], start_index: int) -> int:
                if not needle:
                    return max(0, min(start_index, len(working_lines)))

                max_start = len(working_lines) - len(needle)
                if max_start < 0:
                    invalid(f"update chunk does not match target file ({rel_path})")

                forward_matches = [
                    index
                    for index in range(max(0, start_index), max_start + 1)
                    if working_lines[index : index + len(needle)] == needle
                ]
                if len(forward_matches) == 1:
                    return forward_matches[0]
                if len(forward_matches) > 1:
                    invalid(f"update chunk is ambiguous in target file ({rel_path})")

                all_matches = [
                    index
                    for index in range(0, max_start + 1)
                    if working_lines[index : index + len(needle)] == needle
                ]
                if len(all_matches) == 1:
                    return all_matches[0]
                if len(all_matches) > 1:
                    invalid(f"update chunk is ambiguous in target file ({rel_path})")

                invalid(f"update chunk context not found in target file ({rel_path})")

            for chunk in chunks:
                before_chunk = [line[1:] for line in chunk if line[0] in {" ", "-"}]
                after_chunk = [line[1:] for line in chunk if line[0] in {" ", "+"}]
                match_index = find_match_index(before_chunk, cursor)
                working_lines[match_index : match_index + len(before_chunk)] = after_chunk
                cursor = match_index + len(after_chunk)

            if not working_lines:
                after_text = ""
            else:
                after_text = "\n".join(working_lines)
                if had_trailing_newline or not source_lines:
                    after_text += "\n"
            after_exists = True

        generated_diff = self._generate_applied_diff(
            rel_path,
            before_text,
            before_exists,
            after_text,
            after_exists,
        )
        if not generated_diff:
            invalid("patch produces no file changes")
        if _utf8_len(generated_diff) > SUBMIT_MAX_DIFF_BYTES:
            invalid(f"generated diff is too large (max {SUBMIT_MAX_DIFF_BYTES} bytes)")
        return generated_diff

    def _extract_unified_file_header_path(self, line: str, prefix: str) -> str:
        assert line.startswith(prefix), f"Expected line to start with {prefix!r}"
        path_with_suffix = line[len(prefix) :]
        if "\t" in path_with_suffix:
            return path_with_suffix.split("\t", 1)[0]
        return path_with_suffix

    def _submission_header_matches_target(self, header_path: str, rel_path: str) -> bool:
        canonical_rel_path = Path(rel_path).as_posix()
        return header_path in {
            canonical_rel_path,
            f"a/{canonical_rel_path}",
            f"b/{canonical_rel_path}",
        }

    def _normalize_submission_diff(self, rel_path: str, diff_text: str) -> str:
        path_for_header = Path(rel_path).as_posix()
        normalized_text = diff_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not normalized_text:
            self._raise_invalid_submit_diff("empty diff text")

        lines = normalized_text.split("\n")
        first_hunk_index = next(
            (index for index, line in enumerate(lines) if line.startswith("@@ ")),
            None,
        )
        if first_hunk_index is None:
            self._raise_invalid_submit_diff("expected at least one unified hunk header (`@@ ... @@`)")

        if sum(1 for line in lines if line.startswith("diff --git ")) > 1:
            self._raise_invalid_submit_diff("multiple file sections are not supported")

        prelude_prefixes = (
            "index ",
            "new file mode ",
            "deleted file mode ",
            "old mode ",
            "new mode ",
            "similarity index ",
            "dissimilarity index ",
        )

        source_header: str | None = None
        target_header: str | None = None
        for line in lines[:first_hunk_index]:
            if line == "":
                continue
            if line.startswith("```") or line.startswith("~~~"):
                self._raise_invalid_submit_diff("markdown fences are not supported; submit raw unified diff")
            if line.startswith("rename from ") or line.startswith("rename to "):
                self._raise_invalid_submit_diff("rename patches are not supported")
            if line.startswith("Binary files "):
                self._raise_invalid_submit_diff("binary patches are not supported")
            if line.startswith("diff --git "):
                continue
            if line.startswith("--- "):
                if source_header is not None:
                    self._raise_invalid_submit_diff("multiple `---` file headers are not supported")
                source_header = self._extract_unified_file_header_path(line, "--- ")
                continue
            if line.startswith("+++ "):
                if target_header is not None:
                    self._raise_invalid_submit_diff("multiple `+++` file headers are not supported")
                target_header = self._extract_unified_file_header_path(line, "+++ ")
                continue
            if line.startswith(prelude_prefixes):
                continue
            self._raise_invalid_submit_diff(f"unexpected prelude line before first hunk: {line!r}")

        if (source_header is None) != (target_header is None):
            self._raise_invalid_submit_diff("both `---` and `+++` file headers must be provided together")

        operation = "modify"
        if source_header is not None and target_header is not None:
            source_is_dev_null = source_header == "/dev/null"
            target_is_dev_null = target_header == "/dev/null"
            if source_is_dev_null and target_is_dev_null:
                self._raise_invalid_submit_diff("invalid file headers: both sides are /dev/null")

            if source_is_dev_null:
                if not self._submission_header_matches_target(target_header, rel_path):
                    self._raise_invalid_submit_diff(
                        "target file header does not match `path` argument for file creation"
                    )
                operation = "create"
            elif target_is_dev_null:
                if not self._submission_header_matches_target(source_header, rel_path):
                    self._raise_invalid_submit_diff(
                        "source file header does not match `path` argument for file deletion"
                    )
                operation = "delete"
            else:
                if not self._submission_header_matches_target(source_header, rel_path):
                    self._raise_invalid_submit_diff(
                        "source file header does not match `path` argument"
                    )
                if not self._submission_header_matches_target(target_header, rel_path):
                    self._raise_invalid_submit_diff(
                        "target file header does not match `path` argument"
                    )

        if operation == "create":
            source_line = "--- /dev/null"
            target_line = f"+++ b/{path_for_header}"
        elif operation == "delete":
            source_line = f"--- a/{path_for_header}"
            target_line = "+++ /dev/null"
        else:
            source_line = f"--- a/{path_for_header}"
            target_line = f"+++ b/{path_for_header}"

        hunk_lines = lines[first_hunk_index:]
        canonical_lines = [
            f"diff --git a/{path_for_header} b/{path_for_header}",
            source_line,
            target_line,
        ]
        canonical_lines.extend(hunk_lines)
        return "\n".join(canonical_lines).rstrip("\n") + "\n"

    def _validate_submission_diff_with_git(self, diff_text: str) -> None:
        try:
            process = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    "--recount",
                    "--whitespace=nowarn",
                    "--unidiff-zero",
                    "-",
                ],
                cwd=str(self.paths.project_root),
                input=diff_text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ToolError("io_error", f"Failed to run git apply --check: {exc}")

        if process.returncode == 0:
            return

        stderr_text = process.stderr.decode("utf-8", errors="replace")
        stdout_text = process.stdout.decode("utf-8", errors="replace")
        detail_source = stderr_text if stderr_text.strip() else stdout_text
        detail = "git apply --check failed"
        for line in detail_source.splitlines():
            stripped = line.strip()
            if stripped:
                detail = stripped
                break
        self._raise_invalid_submit_diff(detail)

    def dispatch_tool(self, ctx: ToolCallContext) -> dict[str, Any]:
        """Route tool calls to concrete handlers."""
        handlers: dict[str, Any] = {
            "emacs.health": self.tool_health,
            "emacs.get_project_root": self.tool_get_project_root,
            "emacs.get_selection": self.tool_get_selection,
            "emacs.submit_apply_patch": self.tool_submit_apply_patch,
            "emacs.submit_diff": self.tool_submit_diff,
            "emacs.feedback_list": self.tool_feedback_list,
            "emacs.feedback_get": self.tool_feedback_get,
        }
        handler = handlers.get(ctx.tool_name)
        assert handler is not None, f"Unknown tool: {ctx.tool_name}"
        return handler(ctx.arguments)

    # Tool handlers
    def tool_health(self, arguments: dict[str, Any]) -> dict[str, Any]:
        python_project_root = os.path.realpath(str(self.paths.project_root))
        try:
            bridge_result = self.emacs_client.call("emacs.get_project_root", {})
            emacs_project_root = self._validate_bridge_project_root_result(bridge_result)
        except ToolError as exc:
            if exc.code in {"emacs_unreachable", "invalid_response", "root_mismatch", "not_ready"}:
                raise
            raise ToolError("not_ready", f"emacs-mcp not ready: {exc.message}")

        if python_project_root != emacs_project_root:
            raise ToolError(
                "root_mismatch",
                (
                    "Python and Emacs project roots differ "
                    f"(python={python_project_root}, emacs={emacs_project_root})"
                ),
            )

        return {
            "ok": True,
            "status": "ready",
            "python_project_root": python_project_root,
            "emacs_project_root": emacs_project_root,
        }

    def tool_get_project_root(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "project_root": str(self.paths.project_root)}

    def tool_get_selection(self, arguments: dict[str, Any]) -> dict[str, Any]:
        selection = self.emacs_client.call("emacs.get_selection", {})
        return self._validate_get_selection_result(selection)

    def tool_submit_diff(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        description = arguments.get("description")
        diff = arguments.get("diff")
        assert isinstance(path, str), "Validated argument 'path' must be a string"
        assert isinstance(description, str), "Validated argument 'description' must be a string"
        assert isinstance(diff, str), "Validated argument 'diff' must be a string"

        abs_path = self._validate_path_from_tool(path, "emacs.submit_diff", "path")
        normalized_diff = self._normalize_submission_diff(path, diff)
        self._validate_submission_diff_with_git(normalized_diff)

        self._ensure_submit_state_dirs()
        active_index = self._load_active_index()
        created_active_entry = self._create_before_snapshot_if_needed(path, abs_path, active_index)
        if created_active_entry:
            self._save_active_index(active_index)

        try:
            self.emacs_client.call(
                "emacs.append_submission",
                {"path": path, "description": description, "diff": normalized_diff},
            )
        except ToolError as exc:
            if created_active_entry:
                self._rollback_submit_diff_active_entry(path, active_index)
            invalid_params_prefix = "Emacs error invalid_params: "
            if exc.code == "emacs_error" and exc.message.startswith(invalid_params_prefix):
                detail = exc.message[len(invalid_params_prefix) :].strip()
                self._raise_invalid_submit_diff(detail or "invalid unified hunk structure")
            raise
        except Exception:
            if created_active_entry:
                self._rollback_submit_diff_active_entry(path, active_index)
            raise
        return {"ok": True}

    def tool_submit_apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        description = arguments.get("description")
        patch = arguments.get("patch")
        assert isinstance(path, str), "Validated argument 'path' must be a string"
        assert isinstance(description, str), "Validated argument 'description' must be a string"
        assert isinstance(patch, str), "Validated argument 'patch' must be a string"

        abs_path = self._validate_path_from_tool(path, "emacs.submit_apply_patch", "path")
        generated_diff = self._generate_diff_from_apply_patch(path, abs_path, patch)
        return self.tool_submit_diff(
            {"path": path, "description": description, "diff": generated_diff}
        )

    def tool_feedback_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._process_feedback_inbox()
        items: list[dict[str, Any]] = []
        for pending_path in self._list_pending_paths():
            item = self._load_pending_item(pending_path)
            items.append({"id": item["id"], "path": item["path"]})
        return {"ok": True, "items": items}

    def tool_feedback_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        feedback_id = arguments.get("id")
        assert isinstance(feedback_id, int) and not isinstance(feedback_id, bool), (
            "Validated argument 'id' must be an integer"
        )

        self._process_feedback_inbox()
        pending_path = self._pending_item_path(feedback_id)
        if not pending_path.exists():
            raise ToolError("not_found", f"Feedback item not found: {feedback_id}")
        item = self._load_pending_item(pending_path)
        try:
            pending_path.unlink()
        except OSError as exc:
            raise ToolError("io_error", f"Failed to consume feedback item {feedback_id}: {exc}")
        return {
            "ok": True,
            "path": item["path"],
            "applied_diff": item["applied_diff"],
            "user_message": item["user_message"],
        }


# Helpers
def build_paths() -> ServerPaths:
    """Construct default path layout from cwd and XDG cache."""

    def _realpath(path: Path) -> Path:
        return Path(os.path.realpath(str(path.expanduser())))

    project_root = _realpath(Path.cwd())
    xdg_cache_home = _realpath(Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")))
    cache_base_dir = _realpath(xdg_cache_home / "emacs-mcp")

    state_dir = _realpath(cache_base_dir / "state")
    active_dir = _realpath(state_dir / "active")
    active_index_path = _realpath(active_dir / "index.json")
    before_dir = _realpath(active_dir / "before")

    feedback_dir = _realpath(cache_base_dir / "feedback")
    feedback_inbox_dir = _realpath(feedback_dir / "inbox")
    feedback_pending_dir = _realpath(feedback_dir / "pending")

    socket_path = _realpath(cache_base_dir / "emacs-mcp.sock")

    return ServerPaths(
        project_root=project_root,
        state_dir=state_dir,
        active_dir=active_dir,
        active_index_path=active_index_path,
        before_dir=before_dir,
        feedback_dir=feedback_dir,
        feedback_inbox_dir=feedback_inbox_dir,
        feedback_pending_dir=feedback_pending_dir,
        socket_path=socket_path,
    )

def build_server() -> EmacsMcpServer:
    """Wire paths + bridge client into the server object."""
    paths = build_paths()
    emacs_client = EmacsRpcClient(socket_path=paths.socket_path)
    return EmacsMcpServer(paths=paths, emacs_client=emacs_client)

def main() -> int:
    """Program entrypoint."""
    server = build_server()
    return server.run()


if __name__ == "__main__":
    raise SystemExit(main())
