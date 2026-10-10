"""Reviewer SSRF probes, with no DNS or vendor requests."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hotdocs_mcp.urls import public_https, validate_url_arguments

BAD_URLS = [
    "http://127.0.0.1/private",
    "http://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1/private",
    "https://127.1/private",
    "https://2130706433/private",
    "https://0x7f000001/private",
    "https://017700000001/private",
    "https://0177.0.0.1/private",
    "https://0x7f.0.0.1/private",
    "https://169.254.169.254/private",
    "https://2852039166/private",
    "https://0xa9fea9fe/private",
    "https://0251.0376.0251.0376/private",
    "https://[::1]/private",
    "https://[::ffff:127.0.0.1]/private",
    "https://[::ffff:7f00:1]/private",
    "https://[fe80::1]/private",
    "https://[fe80::1%25en0]/private",
    "https://[fc00::1]/private",
    "https://0.0.0.0/private",
    "https://10.0.0.1/private",
    "https://192.168.1.1/private",
    "https://100.64.0.1/private",
    "https://192.0.2.1/private",
    "https://224.0.0.1/private",
    "https://[ff02::1]/private",
    "https://localhost/private",
    "https://LOCALHOST./private",
    "https://app.localhost/private",
    "https://app.local/private",
    "https://metadata.google.internal/private",
    "https://%31%32%37.0.0.1/private",
    "https://127。0。0。1/private",
    "https://user:pass@example.test/private",
    "https://example.test:99999/private",
    "https://example.test\\@127.0.0.1/private",
    "https://example.test/#fragment",
    "https://example.test\n/path",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_non_public_url_review_probes(url):
    with pytest.raises(ToolError, match="public HTTPS"):
        public_https(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/callback?job=1",
        "https://8.8.8.8/page",
        "https://[2606:4700:4700::1111]/page",
    ],
)
def test_public_https_accepted(url):
    public_https(url)


@pytest.mark.parametrize(
    "key",
    [
        "url",
        "callbackUrl",
        "callback_url",
        "URL",
        "target_uri",
        "redirectUrls",
        "urlToFetch",
        "url_list",
        "href",
        "callback",
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_nested_url_cannot_bypass(key, encoded):
    import json

    value = {"payload": [{key: "https://2130706433/private"}]}
    if encoded:
        value = {"payload_json": json.dumps(value)}
    with pytest.raises(ToolError, match="public HTTPS"):
        validate_url_arguments(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_callback_probe_never_forwarded(url):
    from unittest.mock import AsyncMock

    from hotdocs_mcp.server import create_server

    client = AsyncMock()
    server = create_server(client)
    with pytest.raises(ToolError, match="public HTTPS"):
        await server.call_tool(
            "complete_work_item_version",
            {
                "work_item_id": "work-1",
                "version_id": "version-1",
                "confirm": True,
                "callback_url": url,
            },
        )
    client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_json_url_boundary(settings):
    import httpx

    from hotdocs_mcp.client import HotDocsClient

    sent = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: sent.append(req))
    ) as http:
        client = HotDocsClient(settings, http_client=http)
        with pytest.raises(ToolError, match="public HTTPS"):
            await client.request(
                "PUT",
                "/WorkItems/work/Versions/ver/Complete",
                json_body={"nested": {"callbackUrl": "https://0xa9fea9fe/"}},
            )
    assert sent == []
