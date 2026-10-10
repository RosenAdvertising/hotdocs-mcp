"""Offline raw-wire regressions adapted from the supplied Actionstep reference."""

from __future__ import annotations

import json
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from hotdocs_mcp import __version__, server

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"


def _modern_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    request_id: int = 1,
) -> tuple[dict[str, str], dict[str, Any]]:
    request_params = dict(params or {})
    request_params["_meta"] = {
        PROTOCOL_VERSION_META_KEY: protocol_version,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "hotdocs-spec-test", "version": "0"},
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": protocol_version,
        "mcp-method": method,
    }
    if method in ("tools/call", "prompts/get"):
        headers["mcp-name"] = str(request_params["name"])
    elif method == "resources/read":
        headers["mcp-name"] = str(request_params["uri"])
    return headers, {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


async def _post_modern(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    header_overrides: dict[str, str] | None = None,
) -> httpx.Response:
    app = server.mcp.streamable_http_app(host="127.0.0.1", stateless_http=True, json_response=True)
    headers, body = _modern_request(method, params, protocol_version=protocol_version)
    if header_overrides:
        headers.update(header_overrides)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client,
    ):
        return await client.post("/mcp", headers=headers, json=body)


def _result(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        payload = next(payload for payload in payloads if "result" in payload)
    else:
        payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    return payload["result"]


def test_sdk_pins_the_2026_revision() -> None:
    assert version("mcp") == "2.3.0"
    assert LATEST_PROTOCOL_VERSION == PROTOCOL_VERSION
    assert MODERN_PROTOCOL_VERSIONS == (PROTOCOL_VERSION,)


async def test_modern_discovery_is_sessionless_and_declares_sdk_capabilities() -> None:
    response = await _post_modern("server/discover")
    result = _result(response)
    assert "mcp-session-id" not in response.headers
    assert result["supportedVersions"] == [PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 0
    assert result["cacheScope"] == "private"
    assert result["capabilities"] == {
        "prompts": {"listChanged": True},
        "resources": {"listChanged": True, "subscribe": True},
        "tools": {"listChanged": True},
    }
    assert "extensions" not in result["capabilities"]
    info = result["_meta"][SERVER_INFO_META_KEY]
    assert info["name"] == "HotDocs Advance"
    assert info["title"] == "HotDocs Advance"
    assert info["version"] == __version__


async def test_client_negotiates_modern_and_keeps_legacy_compatibility() -> None:
    async with Client(server.mcp, cache=None) as modern:
        assert modern.protocol_version == PROTOCOL_VERSION
    async with Client(server.mcp, mode="legacy", cache=None) as legacy:
        assert legacy.protocol_version == LEGACY_PROTOCOL_VERSION


async def test_cacheable_results_are_complete_private_and_deterministic() -> None:
    methods = (
        "tools/list",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    )
    first, second, prompts, resources, templates = [
        _result(await _post_modern(method)) for method in methods
    ]
    for result in (first, second, prompts, resources, templates):
        assert result["resultType"] == "complete"
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"
    assert first["tools"] == second["tools"]
    assert len(first["tools"]) == 13
    assert all(tool["inputSchema"]["type"] == "object" for tool in first["tools"])
    assert prompts["prompts"] == []
    assert resources["resources"] == []
    assert templates["resourceTemplates"] == []


async def test_missing_resource_returns_invalid_params() -> None:
    response = await _post_modern("resources/read", {"uri": "hotdocs://does-not-exist"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


async def test_unknown_tool_remains_an_is_error_result() -> None:
    response = await _post_modern("tools/call", {"name": "does_not_exist", "arguments": {}})
    result = _result(response)
    assert result["isError"] is True
    assert result["resultType"] == "complete"


@pytest.mark.parametrize(
    ("method", "params", "overrides"),
    [
        ("tools/list", None, {"mcp-method": "resources/list"}),
        (
            "tools/call",
            {"name": "list_work_items", "arguments": {}},
            {"mcp-name": "list_template_packages"},
        ),
    ],
)
async def test_modern_http_enforces_routing_headers(method, params, overrides) -> None:
    response = await _post_modern(method, params, header_overrides=overrides)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


async def test_modern_http_rejects_unsupported_versions() -> None:
    response = await _post_modern("tools/list", protocol_version="2099-01-01")
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32022,
        "message": "Unsupported protocol version",
        "data": {"supported": [PROTOCOL_VERSION], "requested": "2099-01-01"},
    }


async def test_modern_http_rejects_unknown_methods() -> None:
    response = await _post_modern("example/unknown")
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": -32601,
        "message": "Method not found",
        "data": "example/unknown",
    }
