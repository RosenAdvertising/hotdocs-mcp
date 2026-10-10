"""Stateless HTTP serving, environment configuration, and cancellation regressions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import Mock

import httpx
import pytest
import respx
from mcp import Client
from mcp.server.mcpserver import Context
from test_spec_2026_07_28 import (
    PROTOCOL_VERSION,
    SERVER_INFO_META_KEY,
    _modern_request,
    _result,
)

from hotdocs_mcp import __version__, server
from hotdocs_mcp.config import Settings
from hotdocs_mcp.runtime import LazyClient


@pytest.fixture(autouse=True)
def clean_transport_environment(monkeypatch):
    for name in (
        "HOTDOCS_MCP_TRANSPORT",
        "HOTDOCS_MCP_HOST",
        "PORT",
        "HOTDOCS_MCP_ALLOWED_HOSTS",
        "HOTDOCS_MCP_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)


@asynccontextmanager
async def _http_client():
    app = server.create_serve_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client,
    ):
        yield client


async def test_http_catalog_matches_stdio() -> None:
    async with Client(server.create_server(), mode="legacy", cache=None) as stdio:
        stdio_tools = (await stdio.list_tools()).tools
    headers, body = _modern_request("tools/list")
    async with _http_client() as client:
        response = await client.post("/mcp", headers=headers, json=body)
    tools = _result(response)["tools"]
    assert {tool["name"]: tool["inputSchema"] for tool in tools} == {
        tool.name: tool.input_schema for tool in stdio_tools
    }


async def test_legacy_http_catalog_stays_available() -> None:
    async with _http_client() as client:
        response = await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert len(_result(response)["tools"]) == 13
    assert "mcp-session-id" not in response.headers


@respx.mock
async def test_http_read_tool_uses_environment_credentials_and_vendor_mocks(
    monkeypatch, settings: Settings
) -> None:
    for name, value in (
        ("HOTDOCS_TENANCY_MONIKER", settings.tenancy_moniker),
        ("HOTDOCS_CLIENT_NAME", settings.client_name),
        ("HOTDOCS_PRINCIPAL_NAME", settings.principal_name),
        ("HOTDOCS_PRINCIPAL_PASSWORD", settings.principal_password),
    ):
        monkeypatch.setenv(name, value)
    auth = respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    route = respx.get(f"{settings.api_base_url}WorkItems").mock(
        return_value=httpx.Response(200, json=[{"id": "wi-1"}, {"id": "wi-2"}])
    )
    lazy = LazyClient()
    monkeypatch.setattr(server, "mcp", server.create_server(lazy), raising=False)
    headers, body = _modern_request("tools/call", {"name": "list_work_items", "arguments": {}})
    headers.update(
        {"authorization": "Bearer request-credential", "hotdocs-client-name": "request-client"}
    )
    try:
        async with _http_client() as client:
            first, second = await asyncio.gather(
                client.post("/mcp", headers=headers, json=body),
                client.post("/mcp", headers=headers, json=body),
            )
    finally:
        await lazy.aclose()
    for response in (first, second):
        result = _result(response)
        assert result["isError"] is False
        assert result["resultType"] == "complete"
        assert result["structuredContent"] == {
            "status": 200,
            "content_type": "application/json",
            "data": [{"id": "wi-1"}, {"id": "wi-2"}],
            "collection": {
                "limit": 50,
                "returned": 2,
                "returned_by_field": {},
                "truncated": False,
                "automatic_pagination": False,
            },
        }
        assert "mcp-session-id" not in response.headers
    assert auth.call_count == 1
    assert route.call_count == 2
    assert json.loads(auth.calls[0].request.content) == {
        "ClientName": settings.client_name,
        "PrincipalName": settings.principal_name,
        "PrincipalPassword": settings.principal_password,
    }
    assert all(call.request.headers["authorization"] == "Bearer token" for call in route.calls)


async def test_stateless_requests_have_fresh_context_and_one_lifespan(monkeypatch) -> None:
    counts = {"entered": 0, "exited": 0}
    contexts = []
    probe = server.create_server()

    @asynccontextmanager
    async def lifespan(_):
        counts["entered"] += 1
        try:
            yield {"marker": "shared-lifespan"}
        finally:
            counts["exited"] += 1

    probe._lowlevel_server.lifespan = lifespan

    @probe.tool()
    async def request_context(ctx: Context) -> dict[str, str]:
        contexts.append(ctx.request_context)
        return {
            "marker": ctx.request_context.lifespan_context["marker"],
            "client": ctx.headers["x-test-client"],
        }

    monkeypatch.setattr(server, "mcp", probe, raising=False)
    headers, body = _modern_request("tools/call", {"name": "request_context", "arguments": {}})
    async with _http_client() as client:
        responses = await asyncio.gather(
            client.post(
                "/mcp",
                headers={**headers, "x-test-client": "first", "mcp-session-id": "unissued"},
                json=body,
            ),
            client.post("/mcp", headers={**headers, "x-test-client": "second"}, json=body),
        )
        assert counts == {"entered": 1, "exited": 0}
    assert counts == {"entered": 1, "exited": 1}
    assert contexts[0] is not contexts[1]
    assert contexts[0].lifespan_context is contexts[1].lifespan_context
    for response, name in zip(responses, ("first", "second"), strict=True):
        assert "mcp-session-id" not in response.headers
        assert _result(response)["structuredContent"] == {
            "marker": "shared-lifespan",
            "client": name,
        }


def test_default_transport_preserves_stdio_main(monkeypatch) -> None:
    fake_server = Mock()
    factory = Mock(return_value=fake_server)
    monkeypatch.setattr(server, "create_server", factory)
    assert server._requested_transport() == "stdio"
    assert server.main() == 0
    fake_server.run.assert_called_once_with(transport="stdio")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "stdio"),
        ("  ", "stdio"),
        (" STDIO ", "stdio"),
        (" Streamable-HTTP ", "streamable-http"),
    ],
)
def test_transport_environment_is_normalized(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_TRANSPORT", value)
    assert server._requested_transport() == expected


def test_bogus_transport_exits_with_both_options(monkeypatch) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_TRANSPORT", "bogus")
    with pytest.raises(SystemExit, match=r"HOTDOCS_MCP_TRANSPORT.*stdio.*streamable-http"):
        server.main()


def test_http_main_selects_the_http_server(monkeypatch) -> None:
    calls = []

    async def serve():
        calls.append("http")

    monkeypatch.setenv("HOTDOCS_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "_serve_streamable_http", serve, raising=False)
    assert server.main() == 0
    assert calls == ["http"]


def test_default_host_and_port_and_overrides(monkeypatch) -> None:
    assert server._host() == "127.0.0.1"
    assert server._port() == 8080
    monkeypatch.setenv("HOTDOCS_MCP_HOST", " 0.0.0.0 ")
    monkeypatch.setenv("PORT", " 9080 ")
    assert server._host() == "0.0.0.0"
    assert server._port() == 9080


def test_empty_host_defaults_to_loopback(monkeypatch) -> None:
    for value in ("", " \t "):
        monkeypatch.setenv("HOTDOCS_MCP_HOST", value)
        assert server._host() == "127.0.0.1"


def test_invalid_port_exits_clearly(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "invalid")
    with pytest.raises(SystemExit, match="PORT must be an integer"):
        server._port()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_uses_sdk_security(monkeypatch, host) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_HOST", host)
    assert server._transport_security() is None


def test_localhost_spelling_is_non_loopback_without_allowed_hosts(monkeypatch) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_HOST", "LOCALHOST")
    monkeypatch.delenv("HOTDOCS_MCP_ALLOWED_HOSTS", raising=False)
    assert server._host() == "LOCALHOST"
    with pytest.raises(SystemExit) as excinfo:
        server.create_serve_app()
    assert "HOTDOCS_MCP_ALLOWED_HOSTS" in str(excinfo.value)


@pytest.mark.parametrize("allowed_hosts", [None, "", " , "])
def test_non_loopback_requires_allowed_hosts(monkeypatch, allowed_hosts) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_HOST", "0.0.0.0")
    if allowed_hosts is not None:
        monkeypatch.setenv("HOTDOCS_MCP_ALLOWED_HOSTS", allowed_hosts)
    with pytest.raises(SystemExit, match="HOTDOCS_MCP_ALLOWED_HOSTS"):
        server.create_serve_app()


async def test_http_rejects_bad_host_and_origin(monkeypatch) -> None:
    monkeypatch.setenv("HOTDOCS_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("HOTDOCS_MCP_ALLOWED_HOSTS", " 127.0.0.1:8080, mcp.example.com, ")
    monkeypatch.setenv("HOTDOCS_MCP_ALLOWED_ORIGINS", " https://client.example.com, ")
    headers, body = _modern_request("tools/list")
    async with _http_client() as client:
        bad_host = await client.post(
            "/mcp", headers={**headers, "host": "attacker.example.com"}, json=body
        )
        bad_origin = await client.post(
            "/mcp", headers={**headers, "origin": "https://attacker.example.com"}, json=body
        )
        good_origin = await client.post(
            "/mcp", headers={**headers, "origin": "https://client.example.com"}, json=body
        )
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
    assert good_origin.status_code == 200


async def test_loopback_rejects_bad_host_and_origin() -> None:
    headers, body = _modern_request("tools/list")
    async with _http_client() as client:
        bad_host = await client.post(
            "/mcp", headers={**headers, "host": "attacker.example.com"}, json=body
        )
        bad_origin = await client.post(
            "/mcp", headers={**headers, "origin": "https://attacker.example.com"}, json=body
        )
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_modern_get_and_delete_are_refused(method) -> None:
    async with _http_client() as client:
        response = await client.request(
            method, "/mcp", headers={"mcp-protocol-version": PROTOCOL_VERSION}
        )
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


async def test_http_discovery_has_current_protocol_and_package_version() -> None:
    headers, body = _modern_request("server/discover")
    async with _http_client() as client:
        response = await client.post("/mcp", headers=headers, json=body)
    result = _result(response)
    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert result["_meta"][SERVER_INFO_META_KEY]["version"] == __version__
    assert "mcp-session-id" not in response.headers


def test_server_import_survives_missing_distribution() -> None:
    import subprocess
    import sys

    script = """
import importlib.metadata
import mcp.server  # httpx2 reads its own metadata while the SDK imports

def missing(distribution):
    raise importlib.metadata.PackageNotFoundError(distribution)

importlib.metadata.version = missing
import hotdocs_mcp.server
print(hotdocs_mcp.server.mcp.name)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "HotDocs Advance"


async def test_uvicorn_uses_configured_host_port_and_no_access_log(monkeypatch) -> None:
    import uvicorn

    monkeypatch.setenv("PORT", "9080")
    configurations = []

    class FakeServer:
        def __init__(self, config):
            configurations.append(config)

        async def serve(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    await server._serve_streamable_http()
    assert len(configurations) == 1
    assert configurations[0].host == "127.0.0.1"
    assert configurations[0].port == 9080
    assert configurations[0].access_log is False


async def test_http_disconnect_cancels_the_running_tool(monkeypatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class PendingClient:
        async def request(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(server, "mcp", server.create_server(PendingClient()), raising=False)
    app = server.create_serve_app()
    headers, body = _modern_request("tools/call", {"name": "list_work_items", "arguments": {}})
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body).encode()})
    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.encode(), value.encode()) for key, value in headers.items()]
        + [(b"host", b"127.0.0.1:8080")],
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 10000),
    }
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(scope, incoming.get, send))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, timeout=5)
            assert cancelled.is_set()
            assert not any(message.get("body") for message in messages)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
