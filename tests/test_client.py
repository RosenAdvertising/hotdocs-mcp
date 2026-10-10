"""Offline tests for authentication, retries, errors, and pagination."""

from __future__ import annotations

import re
from typing import Any, cast

import httpx
import pytest
import respx

from hotdocs_mcp.client import HotDocsAPIError, HotDocsClient, JsonValue
from hotdocs_mcp.config import Settings


@respx.mock
async def test_happy_path_authenticates_and_sends_bearer_token(settings: Settings) -> None:
    auth = respx.post(settings.auth_url).mock(
        return_value=httpx.Response(
            200,
            text="mock-access-token",
            headers={"Content-Type": "text/plain"},
        )
    )
    endpoint = respx.get(f"{settings.api_base_url}WorkItems").mock(
        return_value=httpx.Response(200, json=[{"id": "wi-1"}])
    )

    async with HotDocsClient(settings) as client:
        result = await client.request_json("GET", "/WorkItems")

    assert result == [{"id": "wi-1"}]
    assert auth.call_count == 1
    assert auth.calls[0].request.read() == (
        b'{"ClientName":"api-client","PrincipalName":"service-principal",'
        b'"PrincipalPassword":"test-password"}'
    )
    assert endpoint.calls[0].request.headers["Authorization"] == "Bearer mock-access-token"


@respx.mock
async def test_auth_failure_preserves_vendor_response(settings: Settings) -> None:
    respx.post(settings.auth_url).mock(
        return_value=httpx.Response(
            401,
            text="service principal disabled",
            headers={"Content-Type": "text/plain", "X-Vendor-Trace": "trace-42"},
        )
    )

    async with HotDocsClient(settings, max_retries=0) as client:
        with pytest.raises(HotDocsAPIError) as caught:
            await client.authenticate()

    error = caught.value
    assert error.status_code == 401
    assert error.raw_body == "service principal disabled"
    assert "trace-42" in str(error)
    assert "service principal disabled" in str(error)


@respx.mock
async def test_429_retries_using_retry_after(settings: Settings) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    endpoint = respx.get(f"{settings.api_base_url}WorkItems").mock(
        side_effect=[
            httpx.Response(429, text="slow down", headers={"Retry-After": "0"}),
            httpx.Response(200, json=[{"id": "wi-1"}]),
        ]
    )

    async with HotDocsClient(settings, max_retries=1, sleep=record_sleep) as client:
        result = await client.request_json("GET", "/WorkItems")

    assert result == [{"id": "wi-1"}]
    assert endpoint.call_count == 2
    assert delays == [0.0]


@respx.mock
async def test_5xx_retries_with_exponential_backoff(settings: Settings) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    endpoint = respx.get(f"{settings.api_base_url}TemplatePackages").mock(
        side_effect=[
            httpx.Response(503, text="temporarily unavailable"),
            httpx.Response(200, json=[]),
        ]
    )

    async with HotDocsClient(
        settings,
        max_retries=1,
        backoff_base_seconds=0.5,
        sleep=record_sleep,
    ) as client:
        result = await client.request_json("GET", "/TemplatePackages")

    assert result == []
    assert endpoint.call_count == 2
    assert delays == [0.5]


@respx.mock
async def test_expired_token_is_replaced_through_documented_login(settings: Settings) -> None:
    auth = respx.post(settings.auth_url).mock(
        side_effect=[
            httpx.Response(200, text="expired-token"),
            httpx.Response(200, text="replacement-token"),
        ]
    )
    endpoint = respx.get(f"{settings.api_base_url}WorkItems").mock(
        side_effect=[
            httpx.Response(401, text="token expired"),
            httpx.Response(200, json=[{"id": "wi-1"}]),
        ]
    )

    async with HotDocsClient(settings, max_retries=0) as client:
        result = await client.request_json("GET", "/WorkItems")

    assert result == [{"id": "wi-1"}]
    assert auth.call_count == 2
    assert endpoint.call_count == 2
    assert endpoint.calls[0].request.headers["Authorization"] == "Bearer expired-token"
    assert endpoint.calls[1].request.headers["Authorization"] == "Bearer replacement-token"


@respx.mock
async def test_schema_neutral_pagination_follows_verified_links_and_caps_limit(
    settings: Settings,
) -> None:
    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    pages = respx.get(
        url__regex=re.compile(rf"{re.escape(settings.api_base_url)}WorkItems(?:\?p=2)?$")
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": [{"id": "wi-1"}, {"id": "wi-2"}],
                    "continuation": "/WorkItems?p=2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": [{"id": "wi-3"}, {"id": "wi-4"}],
                    "continuation": None,
                },
            ),
        ]
    )

    def items(payload: JsonValue) -> list[dict[str, Any]]:
        envelope = cast(dict[str, Any], payload)
        return cast(list[dict[str, Any]], envelope["records"])

    def next_path(payload: JsonValue) -> str | None:
        envelope = cast(dict[str, Any], payload)
        return cast(str | None, envelope["continuation"])

    async with HotDocsClient(settings) as client:
        result = await client.paginate(
            "/WorkItems",
            limit=3,
            extract_items=items,
            extract_next_path=next_path,
        )

    assert [item["id"] for item in result] == ["wi-1", "wi-2", "wi-3"]
    assert pages.call_count == 2
    assert dict(pages.calls[0].request.url.params) == {}
    assert dict(pages.calls[1].request.url.params) == {"p": "2"}
