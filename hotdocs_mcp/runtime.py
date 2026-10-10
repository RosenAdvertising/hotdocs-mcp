"""Lazy client creation and a safe boundary for MCP tool failures."""

from __future__ import annotations

from typing import Any, get_args

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata

from hotdocs_mcp.client import HotDocsAPIError, HotDocsClient, HotDocsError
from hotdocs_mcp.config import ConfigError, Settings
from hotdocs_mcp.paths import validate_path_arguments
from hotdocs_mcp.urls import validate_url_arguments

CONFIGURATION_HELP = (
    "hotdocs is not configured or its configuration is invalid. "
    "Set HOTDOCS_TENANCY_MONIKER, HOTDOCS_CLIENT_NAME, HOTDOCS_PRINCIPAL_NAME, "
    "HOTDOCS_PRINCIPAL_PASSWORD. "
    "Run hotdocs-mcp-setup for instructions, then restart the MCP server."
)


class LazyClient:
    """Create a client only on tool use, never during initialize or tools/list."""

    def __init__(self) -> None:
        self._client: HotDocsClient | None = None

    def __getattr__(self, name: str) -> Any:
        if self._client is None:
            self._client = HotDocsClient(Settings.from_env())
        return getattr(self._client, name)

    async def __aenter__(self) -> LazyClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()
            self._client = None


def safe_error(exc: Exception) -> ToolError:
    """Map error types/status codes; never interpolate exception or vendor text."""
    if isinstance(exc, ConfigError):
        return ToolError(CONFIGURATION_HELP)
    if isinstance(exc, HotDocsAPIError):
        status = exc.status_code
        if status == 401:
            message = "Authentication failed. Check credentials with hotdocs-mcp-setup."
        elif status == 403:
            message = "Access denied. Check account permissions and API scopes for this operation."
        elif status == 404:
            message = "Resource not found. Check the record ID and account access."
        elif status == 429:
            message = "Rate limit reached. Wait before retrying; check any write result first."
        elif isinstance(status, int) and 500 <= status <= 599:
            message = "Service unavailable. Check whether any write completed before retrying."
        else:
            message = "Request rejected or response invalid. Check inputs and API configuration."
        return ToolError(message)
    if isinstance(exc, httpx.RequestError):
        return ToolError(
            "Connection failed; the operation outcome may be unknown. "
            "Check connectivity and whether any write completed before retrying."
        )
    if type(exc) in (ToolError, HotDocsError):
        return exc
    if isinstance(exc, ValueError):
        return ToolError(
            "Invalid tool input or response. Check the tool schema and supplied values."
        )
    return ToolError(
        "The operation could not be completed. Check configuration and service availability; "
        "check whether any write completed before retrying."
    )


class TextPreservingMetadata(FuncMetadata):
    """Keep JSON-looking text intact, including nullable strings, during SDK coercion."""

    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        text_keys = set()
        for name, field in self.arg_model.model_fields.items():
            if field.annotation is str or str in get_args(field.annotation):
                text_keys.add(name)
                if field.alias:
                    text_keys.add(field.alias)
        text = {key: value for key, value in data.items() if key in text_keys}
        other = {key: value for key, value in data.items() if key not in text_keys}
        return super().pre_parse_json(other) | text


class SafeMCPServer(MCPServer):
    """Translate failures before the SDK logs or returns their private details."""

    def add_tool(self, *args: Any, **kwargs: Any) -> None:
        super().add_tool(*args, **kwargs)
        # Per-server metadata only; signatures and serialized tool schemas stay intact.
        for tool in self._tool_manager.list_tools():
            if not isinstance(tool.fn_metadata, TextPreservingMetadata):
                tool.fn_metadata = TextPreservingMetadata(**tool.fn_metadata.model_dump())

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        validate_path_arguments(name, arguments)
        validate_url_arguments(arguments)
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            raise safe_error(exc.__cause__ or exc) from None
        except ToolError as exc:
            # The SDK wraps deliberate ToolErrors; classify the original typed failure.
            raise safe_error(exc.__cause__ or exc) from None
        except Exception as exc:
            raise safe_error(exc) from None
