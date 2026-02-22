#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import sys
import difflib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_SPECS = (
    {
        "name": "emacs.ping",
        "description": "Health check for Python MCP server + Emacs bridge.",
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
MCP_PROTOCOL_VERSION = "2025-11-25"
BRIDGE_CONNECT_TIMEOUT_S = 1.0
BRIDGE_READ_TIMEOUT_S = 5.0
# Size limit semantics:
# - *_MAX_REQUEST_LINE_BYTES and BRIDGE_MAX_LINE_BYTES are raw transport line bytes.
# - *_MAX_*_BYTES for payload fields are UTF-8 encoded byte sizes.
BRIDGE_MAX_LINE_BYTES = 1024 * 1024
MCP_MAX_REQUEST_LINE_BYTES = 1024 * 1024
SUBMIT_MAX_DIFF_BYTES = 256 * 1024
SUBMIT_MAX_DESCRIPTION_BYTES = 16 * 1024
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

        request_id = request.get("id")
        has_id = "id" in request

        jsonrpc = request.get("jsonrpc")
        if jsonrpc is not None and jsonrpc != "2.0":
            return _rpc_error(request_id if has_id else None, -32600, "Invalid Request")

        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _rpc_error(request_id if has_id else None, -32600, "Invalid Request")

        params = request.get("params", {})
        if params is None:
            params = {}

        # JSON-RPC notifications (requests without "id") must not receive responses.
        if not has_id:
            return None

        if method == "initialize":
            if not self.is_initialized:
                return self.handle_initialize(request_id=request_id, params=params)
            else:
                return _rpc_error(request_id if has_id else None, -32600, "Already initialized")

        if not self.is_initialized:
            return _rpc_error(request_id if has_id else None, -32602, "Server not initialized")

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
        if requested is not None:
            if not isinstance(requested, str):
                return _rpc_error(
                    request_id, -32602, "Invalid protocolVersion: expected string"
                )
            if requested != MCP_PROTOCOL_VERSION:
                return _rpc_error(
                    request_id,
                    -32602,
                    (
                        f"Unsupported protocolVersion: {requested}. "
                        f"Supported: {MCP_PROTOCOL_VERSION}"
                    ),
                )

        self.is_initialized = True
        return _rpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
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
            return _tool_err(request_id, "invalid_arguments", "Invalid params: expected object")
        unknown_keys = sorted(set(params.keys()) - {"name", "arguments"})
        if unknown_keys:
            return _tool_err(
                request_id,
                "invalid_arguments",
                f"Invalid params: unexpected keys: {', '.join(unknown_keys)}",
            )

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return _tool_err(request_id, "invalid_arguments", "Missing or invalid tool name")
        if tool_name not in TOOL_NAMES:
            message = f"Unknown tool: {tool_name}"
            return _tool_err(request_id, "unknown_tool", message)

        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _tool_err(
                request_id, "invalid_arguments", "Invalid arguments: expected object"
            )

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
            "emacs.ping": self._validate_empty_arguments,
            "emacs.get_project_root": self._validate_empty_arguments,
            "emacs.get_selection": self._validate_empty_arguments,
            "emacs.feedback_list": self._validate_empty_arguments,
            "emacs.submit_diff": self._validate_submit_diff_arguments,
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
                processed_path = inbox_path.with_suffix(f"{inbox_path.suffix}.processed")
                try:
                    os.replace(inbox_path, processed_path)
                except OSError as quarantine_exc:
                    try:
                        pending_path.unlink()
                    except OSError:
                        pass
                    raise ToolError(
                        "io_error",
                        (
                            f"Failed to clear processed feedback inbox event {inbox_path}: {exc}; "
                            f"failed to quarantine event: {quarantine_exc}"
                        ),
                    )

            if isinstance(active_entry, dict):
                self._clear_active_file(rel_path, active_index)
                self._save_active_index(active_index)
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

    def _normalize_submission_diff(self, rel_path: str, diff_text: str) -> str:
        path_for_header = Path(rel_path).as_posix()
        prelude_prefixes = (
            "diff --git ",
            "index ",
            "--- ",
            "+++ ",
            "new file mode ",
            "deleted file mode ",
            "old mode ",
            "new mode ",
            "similarity index ",
            "dissimilarity index ",
            "rename from ",
            "rename to ",
            "Binary files ",
        )

        lines = diff_text.splitlines()
        body_lines: list[str] = []
        in_body = False
        for line in lines:
            if not in_body:
                if line.startswith("@@ "):
                    in_body = True
                    body_lines.append(line)
                    continue
                if line.startswith(prelude_prefixes) or line == "":
                    continue
                in_body = True
                body_lines.append(line)
                continue
            body_lines.append(line)

        if not body_lines:
            body_lines = lines

        canonical_header = [
            f"diff --git a/{path_for_header} b/{path_for_header}",
            f"--- a/{path_for_header}",
            f"+++ b/{path_for_header}",
        ]
        normalized_lines = canonical_header + body_lines
        return "\n".join(normalized_lines).rstrip("\n") + "\n"

    def dispatch_tool(self, ctx: ToolCallContext) -> dict[str, Any]:
        """Route tool calls to concrete handlers."""
        handlers: dict[str, Any] = {
            "emacs.ping": self.tool_ping,
            "emacs.get_project_root": self.tool_get_project_root,
            "emacs.get_selection": self.tool_get_selection,
            "emacs.submit_diff": self.tool_submit_diff,
            "emacs.feedback_list": self.tool_feedback_list,
            "emacs.feedback_get": self.tool_feedback_get,
        }
        handler = handlers.get(ctx.tool_name)
        assert handler is not None, f"Unknown tool: {ctx.tool_name}"
        return handler(ctx.arguments)

    # Tool handlers
    def tool_ping(self, arguments: dict[str, Any]) -> dict[str, Any]:
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
        self._ensure_submit_state_dirs()
        active_index = self._load_active_index()
        created_active_entry = self._create_before_snapshot_if_needed(path, abs_path, active_index)

        normalized_diff = self._normalize_submission_diff(path, diff)
        self.emacs_client.call(
            "emacs.append_submission",
            {"path": path, "description": description, "diff": normalized_diff},
        )

        if created_active_entry:
            self._save_active_index(active_index)
        return {"ok": True}

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
