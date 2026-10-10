"""Destination policy exercised through MCP Client, with synthetic HTTP only."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import Client
from test_security_boundaries import BAD_URLS

from hotdocs_mcp.client import HotDocsClient
from hotdocs_mcp.server import create_server
from hotdocs_mcp.urls import URL_FIELDS

SETTING = "HOTDOCS_ALLOWED_DESTINATION_HOSTS"
COMPLETE = {"work_item_id": "work-1", "version_id": "version-1", "confirm": True}
BAD_DESTINATIONS = [
    "https://unlisted.example/callback",
    "https://127.0.0.1.sslip.io/callback",
    "https://redirect.example/redirect?url=http://169.254.169.254/latest/meta-data/",
    "https://trusted.example.attacker.example/callback",
    "https://nottrusted.example/callback",
    "https://127.0.0.1./callback",
    "https://127.1./callback",
    "https://2130706433./callback",
    "HTTPS://LOCALHOST/callback",
    "https://example.test:/callback",
    "https://example.test?/callback#",
]


@pytest.fixture
def vendor(monkeypatch):
    monkeypatch.setenv(SETTING, "callbacks.example,.trusted.example")
    return AsyncMock(request=AsyncMock(return_value=httpx.Response(200, json={"ok": True})))


@pytest.mark.parametrize("url", BAD_URLS + BAD_DESTINATIONS)
async def test_rejected_destination_zero_requests(vendor, url):
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool(
            "complete_work_item_version", {**COMPLETE, "callback_url": url}
        )
    assert result.is_error
    assert "public HTTPS" in result.content[0].text or SETTING in result.content[0].text
    assert url not in result.content[0].text
    vendor.request.assert_not_awaited()


@pytest.mark.parametrize(
    "setting",
    [
        None,
        "",
        " , ",
        "https://callbacks.example",
        "*.example",
        "callbacks.example:443",
        "callbacks.example/path",
        ".",
        "user@callbacks.example",
    ],
)
async def test_empty_or_invalid_configuration_zero_requests(monkeypatch, vendor, setting):
    if setting is None:
        monkeypatch.delenv(SETTING)
    else:
        monkeypatch.setenv(SETTING, setting)
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool(
            "complete_work_item_version",
            {**COMPLETE, "callback_url": "https://callbacks.example/done"},
        )
    assert result.is_error
    assert SETTING in result.content[0].text
    vendor.request.assert_not_awaited()


@pytest.mark.parametrize(
    ("setting", "url"),
    [
        ("callbacks.example", "https://callbacks.example/done"),
        (" CALLBACKS.EXAMPLE. , other.example", "HTTPS://CALLBACKS.EXAMPLE./done?job=1"),
        (".trusted.example", "https://trusted.example/done"),
        (".trusted.example", "https://sub.deep.trusted.example/done"),
        ("bücher.example", "https://xn--bcher-kva.example/done"),
        (".BÜCHER.EXAMPLE.", "https://sub.xn--bcher-kva.example/done"),
        ("8.8.8.8", "https://8.8.8.8/done"),
        ("2606:4700:4700::1111", "https://[2606:4700:4700::1111]/done"),
    ],
)
async def test_approved_hosts_forwarded_unchanged(monkeypatch, vendor, setting, url):
    monkeypatch.setenv(SETTING, setting)
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool(
            "complete_work_item_version", {**COMPLETE, "callback_url": url}
        )
    assert not result.is_error
    vendor.request.assert_awaited_once()
    assert vendor.request.call_args.kwargs["json_body"] == {"callbackUrl": url}


async def test_exact_entry_does_not_grant_subdomains(monkeypatch, vendor):
    monkeypatch.setenv(SETTING, "callbacks.example")
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool(
            "complete_work_item_version",
            {**COMPLETE, "callback_url": "https://sub.callbacks.example/done"},
        )
    assert result.is_error
    vendor.request.assert_not_awaited()


async def test_model_cannot_supply_allowlist(monkeypatch, vendor):
    monkeypatch.delenv(SETTING)
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool(
            "complete_work_item_version",
            {
                **COMPLETE,
                "callback_url": "https://callbacks.example/done",
                SETTING: "callbacks.example",
                "allowed_destination_hosts": "callbacks.example",
            },
        )
    assert result.is_error
    assert SETTING in result.content[0].text
    vendor.request.assert_not_awaited()


async def test_optional_callback_unset_still_completes(monkeypatch, vendor):
    monkeypatch.delenv(SETTING)
    async with Client(create_server(vendor)) as client:
        result = await client.call_tool("complete_work_item_version", COMPLETE)
    assert not result.is_error
    vendor.request.assert_awaited_once()


@pytest.mark.parametrize(
    "key", [*sorted(URL_FIELDS), "Callback-URL", "target_url", "baseUrlPattern"]
)
@pytest.mark.parametrize("encoded", [False, True])
async def test_freeform_destinations_cannot_bypass_boundary(vendor, key, encoded):
    # The registered production tools currently have no free-form payload field.
    # Exercise the same SafeMCPServer boundary with an explicit test payload tool.
    app = create_server(vendor)

    @app.tool()
    async def inspect_payload(payload: dict | None = None, payload_json: str | None = None) -> dict:
        await vendor.request("POST", "/test", json_body=payload or payload_json)
        return {}

    payload = {"nested": [{key: "https://unlisted.example/done"}]}
    args = {"payload_json": json.dumps(payload)} if encoded else {"payload": payload}
    async with Client(app) as client:
        result = await client.call_tool("inspect_payload", args)
    assert result.is_error
    assert SETTING in result.content[0].text
    vendor.request.assert_not_awaited()


@pytest.mark.parametrize(
    "description",
    ['{"url":"http://localhost/notes"}', '{"furlong":7}', '[{"callback_url":"http://localhost"}]'],
)
async def test_reviewer_description_survives_real_client(monkeypatch, settings, description):
    monkeypatch.delenv(SETTING, raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        if "/auth/" in str(request.url):
            return httpx.Response(200, text="synthetic-test-token")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        vendor = HotDocsClient(settings, http_client=http)
        async with Client(create_server(vendor)) as client:
            result = await client.call_tool(
                "create_work_item",
                {
                    "work_item_id": "11111111-1111-4111-8111-111111111111",
                    "name": "furlong",
                    "work_group_id": "group-1",
                    "template_package_ids": ["template-1"],
                    "confirm": True,
                    "description": description,
                },
            )
    assert not result.is_error, result
    body = json.loads(requests[-1].content)
    assert body["description"] == description


async def test_reviewer_furlong_and_opaque_nested_text(vendor):
    app = create_server(vendor)

    @app.tool()
    async def inspect_payload(payload: dict) -> dict:
        return payload

    payload = {"furlong": 7, "description": '{"url":"http://localhost/notes"}', "curl_options": 3}
    async with Client(app) as client:
        result = await client.call_tool("inspect_payload", {"payload": payload})
    assert not result.is_error
    assert "furlong" in result.content[0].text


async def test_real_client_rejects_unlisted_before_authentication(monkeypatch, settings):
    monkeypatch.setenv(SETTING, "callbacks.example")
    sent = []
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sent.append(req))) as http,
        Client(create_server(HotDocsClient(settings, http_client=http))) as client,
    ):
        result = await client.call_tool(
            "complete_work_item_version",
            {**COMPLETE, "callback_url": "https://unlisted.example/done"},
        )
    assert result.is_error
    assert sent == []
