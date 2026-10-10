"""Exercise real MCP tool calls with private error sentinels and no network."""

import os

import httpx
import pytest
from mcp import Client

from hotdocs_mcp import runtime, server, setup

SENTINEL = "private-vendor-detail-DO-NOT-EXPOSE"


class FailingClient:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def aclose(self):
        pass

    def __getattr__(self, _name):
        async def fail(*_args, **_kwargs):
            raise self.error

        return fail


def app_with(monkeypatch, client):
    return server.create_server(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 503])
async def test_vendor_error_is_actionable_and_private(monkeypatch, caplog, status):
    error = runtime.HotDocsAPIError(httpx.Response(status, text=SENTINEL))
    app = app_with(monkeypatch, FailingClient(error))
    async with Client(app) as client:
        result = await client.call_tool("list_work_items", {})
    assert result.is_error
    text = result.content[0].text
    assert SENTINEL not in text + caplog.text
    assert "Error executing tool" not in text
    assert any(word in text.lower() for word in ("check", "wait"))
    if status == 403:
        assert "permissions" in text
    if status == 503:
        assert "write completed" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError(SENTINEL),
        httpx.ReadTimeout(SENTINEL),
        ValueError(SENTINEL),
        RuntimeError(SENTINEL),
    ],
)
async def test_non_http_failure_does_not_leak(monkeypatch, caplog, error):
    app = app_with(monkeypatch, FailingClient(error))
    async with Client(app) as client:
        result = await client.call_tool("list_work_items", {})
    assert result.is_error
    assert SENTINEL not in result.content[0].text + caplog.text
    assert "check" in result.content[0].text.lower()


def clear_configuration(monkeypatch):
    for key in list(os.environ):
        if key.startswith(
            (
                "VLEX_",
                "DOCKETWISE_",
                "HOTDOCS_",
                "WORDPRESS_",
                "YOAST_",
                "WHITESPARK_",
                "LEX_MACHINA_",
                "ELITE3E_",
                "NEXTDOOR_",
            )
        ):
            monkeypatch.delenv(key)


@pytest.mark.asyncio
async def test_missing_configuration_tool_error(monkeypatch, caplog):
    clear_configuration(monkeypatch)
    app = app_with(monkeypatch, runtime.LazyClient())
    async with Client(app) as client:
        result = await client.call_tool("list_work_items", {})
    assert result.is_error
    assert "hotdocs-mcp-setup" in result.content[0].text
    assert "Traceback" not in caplog.text


def test_setup_and_verify_do_not_claim_vendor_authentication(monkeypatch, capsys):
    clear_configuration(monkeypatch)
    assert setup.setup() == 0
    assert setup.verify() == 2
    output = capsys.readouterr()
    assert "hotdocs-mcp-setup" in output.err
    assert "Traceback" not in output.err


def test_verify_valid_local_configuration(monkeypatch, capsys):
    monkeypatch.setattr(setup.Settings, "from_env", lambda: object())
    assert setup.verify() == 0
    assert "Vendor authentication has not been tested" in capsys.readouterr().out
