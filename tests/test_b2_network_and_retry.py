"""Network failures get a specific message, and writes are sent once (no retry after a 5xx)."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp import Client

from hotdocs_mcp.client import HotDocsClient
from hotdocs_mcp.config import Settings
from hotdocs_mcp.server import create_server

SENTINEL = "private-network-detail-DO-NOT-EXPOSE"
NETWORK_MESSAGE = "HotDocs request failed because the connection timed out or was unavailable."
WORK_ITEM_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"


async def _no_wait(_delay: float) -> None:
    return None


async def _call(settings: Settings, tool: str, arguments: dict[str, object]):
    async with (
        HotDocsClient(settings, sleep=_no_wait) as hotdocs,
        Client(create_server(hotdocs)) as client,
    ):
        return await client.call_tool(tool, arguments)


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError(SENTINEL), httpx.ReadTimeout(SENTINEL), httpx.ConnectTimeout(SENTINEL)],
    ids=["connect-error", "read-timeout", "connect-timeout"],
)
@respx.mock
async def test_network_failure_on_api_call_gets_network_message(
    settings: Settings, error: httpx.RequestError
) -> None:
    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    respx.get(f"{settings.api_base_url}WorkItems").mock(side_effect=error)

    result = await _call(settings, "list_work_items", {})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith(NETWORK_MESSAGE)
    assert "could not be completed" not in text
    assert SENTINEL not in text


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError(SENTINEL), httpx.ReadTimeout(SENTINEL)],
    ids=["connect-error", "read-timeout"],
)
@respx.mock
async def test_network_failure_on_sign_in_gets_network_message(
    settings: Settings, error: httpx.RequestError
) -> None:
    respx.post(settings.auth_url).mock(side_effect=error)

    result = await _call(settings, "list_work_items", {})

    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith(NETWORK_MESSAGE)
    assert SENTINEL not in text


WRITE_CASES = [
    pytest.param(
        "create_work_item",
        {
            "work_item_id": WORK_ITEM_ID,
            "name": "Engagement letter",
            "work_group_id": "wg-1",
            "template_package_ids": ["tp-1"],
            "confirm": True,
        },
        "PUT",
        f"WorkItems/{WORK_ITEM_ID}",
        id="create_work_item",
    ),
    pytest.param(
        "create_work_item_version",
        {
            "work_item_id": "wi-1",
            "version_id": VERSION_ID,
            "template_package_id": "tp-1",
            "confirm": True,
        },
        "PUT",
        f"WorkItems/wi-1/Versions/{VERSION_ID}",
        id="create_work_item_version",
    ),
    pytest.param(
        "patch_work_item_answers",
        {
            "work_item_id": "wi-1",
            "answer_xml": "<AnswerSet/>",
            "answer_xml_is_xsd_validated": True,
            "confirm": True,
        },
        "PATCH",
        "WorkItems/wi-1/AnswerSet/Content",
        id="patch_work_item_answers",
    ),
    pytest.param(
        "complete_work_item_version",
        {"work_item_id": "wi-1", "version_id": VERSION_ID, "confirm": True},
        "PUT",
        f"WorkItems/wi-1/Versions/{VERSION_ID}/Complete",
        id="complete_work_item_version",
    ),
]


@pytest.mark.parametrize("status", [500, 503, 429])
@pytest.mark.parametrize(("tool", "arguments", "method", "path"), WRITE_CASES)
@respx.mock
async def test_write_tool_is_sent_once_on_retryable_status(
    settings: Settings,
    tool: str,
    arguments: dict[str, object],
    method: str,
    path: str,
    status: int,
) -> None:
    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    route = respx.route(method=method, url=f"{settings.api_base_url}{path}").mock(
        return_value=httpx.Response(status, headers={"Retry-After": "0"}, text="vendor failed")
    )

    result = await _call(settings, tool, arguments)

    assert result.is_error is True
    assert route.call_count == 1


@respx.mock
async def test_get_still_retries_5xx_then_succeeds(settings: Settings) -> None:
    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    route = respx.get(f"{settings.api_base_url}WorkItems").mock(
        side_effect=[
            httpx.Response(503, text="temporarily unavailable"),
            httpx.Response(200, json=[{"id": "wi-1"}]),
        ]
    )

    result = await _call(settings, "list_work_items", {})

    assert result.is_error is False
    assert route.call_count == 2


@respx.mock
async def test_sign_in_post_is_still_retried_after_5xx(settings: Settings) -> None:
    auth = respx.post(settings.auth_url).mock(
        side_effect=[httpx.Response(502, text="bad gateway"), httpx.Response(200, text="token")]
    )
    respx.get(f"{settings.api_base_url}WorkItems").mock(
        return_value=httpx.Response(200, json=[{"id": "wi-1"}])
    )

    result = await _call(settings, "list_work_items", {})

    assert result.is_error is False
    assert auth.call_count == 2
