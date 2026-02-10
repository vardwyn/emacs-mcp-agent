#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_SPECS = (
    {
        "name": "emacs.ping",
        "description": "Quick connectivity check.",
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
        "name": "emacs.patch_init",
        "description": "Initialize patch staging area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}},
                "create": {"type": "array", "items": {"type": "string"}},
                "delete": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.patch_add_files",
        "description": "Add files to patch staging area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}},
                "create": {"type": "array", "items": {"type": "string"}},
                "delete": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.patch_discard",
        "description": "Discard current staged patch.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.patch_submit_for_review",
        "description": "Submit staged patch for review.",
        "inputSchema": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "emacs.wait_patch_result",
        "description": "Wait for final patch review result.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)
TOOL_NAMES = tuple(spec["name"] for spec in TOOL_SPECS)
MCP_PROTOCOL_VERSION = "2025-11-25"


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


@dataclass(frozen=True)
class ServerPaths:
    """Filesystem paths used by the server."""

    project_root: Path

    stage_dir: Path

    state_base_dir: Path

    staging_state_dir: Path
    staging_manifest_path: Path

    review_state_dir: Path
    review_manifest_path: Path
    review_before_dir: Path

    result_dir: Path
    current_result_path: Path

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


class EmacsRpcClient:
    """Unix-socket JSON bridge to Emacs."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a bridge method on the Emacs side."""
        raise NotImplementedError


class EmacsMcpServer:
    """Main MCP server implementation over stdio."""

    def __init__(self, paths: ServerPaths, emacs_client: EmacsRpcClient) -> None:
        self.paths = paths
        self.emacs_client = emacs_client
        self.is_initialized = False

    def run(self) -> int:
        """Read MCP requests from stdin and write responses to stdout."""
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = _rpc_error(None, -32700, "Parse error")
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            try:
                response = self.handle_rpc_request(request)
            except Exception:
                request_id = request.get("id") if isinstance(request, dict) else None
                response = _rpc_error(request_id, -32603, "Internal error")

            # JSON-RPC notifications (requests without "id") must not receive responses.
            if response is None:
                continue

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

        return 0

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
            "emacs.get_selection": self._validate_empty_arguments,
            "emacs.patch_discard": self._validate_empty_arguments,
            "emacs.wait_patch_result": self._validate_empty_arguments,
            "emacs.patch_submit_for_review": self._validate_submit_arguments,
            "emacs.patch_init": self._validate_patch_file_set_arguments,
            "emacs.patch_add_files": self._validate_patch_file_set_arguments,
        }
        validator = validators.get(tool_name)
        assert validator is not None, f"Unhandled tool in validate_tool_arguments: {tool_name}"
        return validator(tool_name, arguments)

    def _validate_empty_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_no_keys(arguments, tool_name)
        return {}

    def _validate_submit_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_allowed_keys(arguments, {"description"}, tool_name)
        description = arguments.get("description")
        if not isinstance(description, str) or not description:
            raise ToolError(
                "invalid_arguments",
                "Invalid arguments for emacs.patch_submit_for_review: "
                "'description' must be a non-empty string",
            )
        return {"description": description}

    def _validate_patch_file_set_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_allowed_keys(arguments, {"files", "create", "delete"}, tool_name)

        files = self._expect_string_list(arguments, "files")
        create = self._expect_string_list(arguments, "create")
        delete = self._expect_string_list(arguments, "delete")

        for path in files:
            self._validate_repo_rel_path_token(path, tool_name, "files")
        for path in create:
            self._validate_repo_rel_path_token(path, tool_name, "create")
        for path in delete:
            self._validate_repo_rel_path_token(path, tool_name, "delete")

        self._ensure_no_duplicates(files, create, delete, tool_name)
        return {"files": files, "create": create, "delete": delete}

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

    def _expect_string_list(self, arguments: dict[str, Any], key: str) -> list[str]:
        value = arguments.get(key, [])
        if not isinstance(value, list):
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments: '{key}' must be a list of strings",
            )
        for item in value:
            if not isinstance(item, str):
                raise ToolError(
                    "invalid_arguments",
                    f"Invalid arguments: '{key}' must be a list of strings",
                )
        return value

    def _validate_repo_rel_path_token(self, path: str, tool_name: str, key: str) -> None:
        if not path:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: '{key}' contains empty path",
            )
        if "\x00" in path:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: path contains NUL byte",
            )
        if os.path.isabs(path):
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: path must be repo-relative: {path}",
            )
        path_obj = Path(path)
        if ".." in path_obj.parts:
            raise ToolError(
                "invalid_arguments",
                f"Invalid arguments for {tool_name}: path traversal is not allowed: {path}",
            )

    def _ensure_no_duplicates(
        self, files: list[str], create: list[str], delete: list[str], tool_name: str
    ) -> None:
        seen: set[str] = set()
        for path in [*files, *create, *delete]:
            if path in seen:
                raise ToolError(
                    "invalid_arguments",
                    f"Invalid arguments for {tool_name}: duplicate path: {path}",
                )
            seen.add(path)

    def dispatch_tool(self, ctx: ToolCallContext) -> dict[str, Any]:
        """Route tool calls to concrete handlers."""
        handlers: dict[str, Any] = {
            "emacs.ping": self.tool_ping,
        }
        handler = handlers.get(ctx.tool_name)
        assert handler is not None, f"Unknown tool: {ctx.tool_name}"
        return handler(ctx.arguments)

    # Tool handlers
    def tool_ping(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def tool_get_selection(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_patch_init(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_patch_add_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_patch_discard(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_patch_submit_for_review(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def tool_wait_patch_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# Helpers
def build_paths() -> ServerPaths:
    """Construct default path layout from cwd and XDG cache."""

    def _realpath(path: Path) -> Path:
        return Path(os.path.realpath(str(path.expanduser())))

    project_root = _realpath(Path.cwd())
    stage_dir = _realpath(Path("/tmp/emacs-mcp/stage"))

    xdg_cache_root = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache"))
    state_base_dir = _realpath(xdg_cache_root / "emacs-mcp")

    state_dir = _realpath(state_base_dir / "state")
    staging_state_dir = _realpath(state_dir / "staging")
    staging_manifest_path = _realpath(staging_state_dir / "manifest.json")

    review_state_dir = _realpath(state_dir / "review")
    review_manifest_path = _realpath(review_state_dir / "manifest.json")
    review_before_dir = _realpath(review_state_dir / "before")

    result_dir = _realpath(state_base_dir / "patch-results")
    current_result_path = _realpath(result_dir / "current.json")

    socket_path = _realpath(state_base_dir / "emacs-mcp.sock")

    return ServerPaths(
        project_root=project_root,
        stage_dir=stage_dir,
        state_base_dir=state_base_dir,
        staging_state_dir=staging_state_dir,
        staging_manifest_path=staging_manifest_path,
        review_state_dir=review_state_dir,
        review_manifest_path=review_manifest_path,
        review_before_dir=review_before_dir,
        result_dir=result_dir,
        current_result_path=current_result_path,
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
