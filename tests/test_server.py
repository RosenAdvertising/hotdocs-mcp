"""MCP schema, protocol, tool round-trip, and error-surfacing tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from mcp import Client

from hotdocs_mcp.client import HotDocsClient
from hotdocs_mcp.config import Settings
from hotdocs_mcp.server import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, create_server


@dataclass(frozen=True)
class ToolCase:
    name: str
    arguments: dict[str, Any]
    method: str
    path: str
    response: httpx.Response


NEW_WORK_ITEM_ID = "11111111-1111-4111-8111-111111111111"
NEW_VERSION_ID = "22222222-2222-4222-8222-222222222222"

TOOL_CASES = (
    ToolCase(
        "list_work_items",
        {"limit": 1, "status_filter": "status eq 'Completed'"},
        "GET",
        "/WorkItems",
        httpx.Response(200, json=[{"id": "wi-1"}, {"id": "wi-2"}]),
    ),
    ToolCase(
        "list_work_group_items",
        {"work_group_id": "wg-1", "limit": 1},
        "GET",
        "/WorkGroups/wg-1/WorkItems",
        httpx.Response(200, json={"items": [{"id": "wi-1"}]}),
    ),
    ToolCase(
        "list_template_packages",
        {"limit": 1},
        "GET",
        "/TemplatePackages",
        httpx.Response(200, json=[{"id": "tp-1"}]),
    ),
    ToolCase(
        "get_template_variable_data",
        {"template_package_id": "tp-1", "version_id": "tv-1"},
        "GET",
        "/TemplatePackages/tp-1/Versions/tv-1/VariableData",
        httpx.Response(
            200,
            json={"title": "Example Document", "variables": [{"name": "Example Client"}]},
        ),
    ),
    ToolCase(
        "get_work_item_answers",
        {"work_item_id": "wi-1", "answer_format": "xml"},
        "GET",
        "/WorkItems/wi-1/AnswerSet/Content",
        httpx.Response(200, text="<AnswerSet />", headers={"Content-Type": "application/xml"}),
    ),
    ToolCase(
        "list_work_item_documents",
        {"work_item_id": "wi-1", "limit": 1},
        "GET",
        "/WorkItems/wi-1/Documents",
        httpx.Response(200, json=[{"id": "doc-1"}]),
    ),
    ToolCase(
        "list_work_item_version_documents",
        {"work_item_id": "wi-1", "version_id": "v-1", "limit": 1},
        "GET",
        "/WorkItems/wi-1/Versions/v-1/Documents",
        httpx.Response(200, json=[{"id": "doc-1"}]),
    ),
    ToolCase(
        "get_work_item_document",
        {"work_item_id": "wi-1", "document_id": "doc-1"},
        "GET",
        "/WorkItems/wi-1/Documents/doc-1",
        httpx.Response(
            200,
            json={
                "id": "doc-1",
                "assemblyResults": {
                    "allReferencedVariablesAnswered": False,
                    "unansweredVariables": ["Example Client"],
                },
            },
        ),
    ),
    ToolCase(
        "download_work_item_document",
        {"work_item_id": "wi-1", "document_id": "doc-1"},
        "GET",
        "/WorkItems/wi-1/Documents/doc-1/Content",
        httpx.Response(
            200,
            content=b"mock document bytes",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="example-document.pdf"',
            },
        ),
    ),
    ToolCase(
        "create_work_item",
        {
            "work_item_id": NEW_WORK_ITEM_ID,
            "name": "Example estate plan",
            "work_group_id": "wg-1",
            "template_package_ids": ["tp-1", "tp-2"],
            "confirm": True,
        },
        "PUT",
        f"/WorkItems/{NEW_WORK_ITEM_ID}",
        httpx.Response(200),
    ),
    ToolCase(
        "create_work_item_version",
        {
            "work_item_id": "wi-1",
            "version_id": NEW_VERSION_ID,
            "template_package_id": "tp-1",
            "confirm": True,
        },
        "PUT",
        f"/WorkItems/wi-1/Versions/{NEW_VERSION_ID}",
        httpx.Response(200, json={"versionId": NEW_VERSION_ID, "assemblySessionId": "as-1"}),
    ),
    ToolCase(
        "patch_work_item_answers",
        {
            "work_item_id": "wi-1",
            "answer_xml": "<AnswerSet />",
            "answer_xml_is_xsd_validated": True,
            "confirm": True,
        },
        "PATCH",
        "/WorkItems/wi-1/AnswerSet/Content",
        httpx.Response(200, json={"unresolvedImageReferences": []}),
    ),
    ToolCase(
        "complete_work_item_version",
        {"work_item_id": "wi-1", "version_id": "v-1", "confirm": True},
        "PUT",
        "/WorkItems/wi-1/Versions/v-1/Complete",
        httpx.Response(202, json={"status": "Accepted"}),
    ),
)


@pytest.mark.parametrize("case", TOOL_CASES, ids=lambda case: case.name)
@respx.mock
async def test_each_tool_makes_its_documented_mocked_round_trip(
    case: ToolCase,
    settings: Settings,
) -> None:
    auth = respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    route = respx.request(case.method, f"{settings.api_base_url.rstrip('/')}{case.path}").mock(
        return_value=case.response
    )
    hotdocs = HotDocsClient(settings, max_retries=0)
    server = create_server(hotdocs)

    try:
        async with Client(server, raise_exceptions=True) as mcp_client:
            result = await mcp_client.call_tool(case.name, case.arguments)
    finally:
        await hotdocs.aclose()

    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    assert auth.call_count == 1
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer token"

    if case.name == "list_work_items":
        assert dict(request.url.params) == {"filter": "status eq 'Completed'"}
        assert result.structured_content["data"] == [{"id": "wi-1"}]
        assert result.structured_content["collection"]["truncated"] is True
    elif case.name == "download_work_item_document":
        assert result.structured_content["content_base64"] == "bW9jayBkb2N1bWVudCBieXRlcw=="
    elif case.name == "create_work_item":
        assert json.loads(request.content) == {
            "name": "Example estate plan",
            "workGroupId": "wg-1",
            "templatePackageIds": ["tp-1", "tp-2"],
            "isPrivateToOwner": False,
            "useDefaultAnswers": True,
        }
    elif case.name == "patch_work_item_answers":
        assert request.headers["Content-Type"] == "application/xml"
        assert request.content == b"<AnswerSet />"


async def test_tool_catalog_has_described_typed_parameters_and_structured_outputs(
    settings: Settings,
) -> None:
    hotdocs = HotDocsClient(settings)
    server = create_server(hotdocs)
    try:
        tools = await server.list_tools()
    finally:
        await hotdocs.aclose()

    assert len(tools) == 13
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", tool.name) for tool in tools)
    for tool in tools:
        assert tool.description
        assert "\n" not in tool.description
        assert tool.output_schema is not None
        for schema in tool.input_schema.get("properties", {}).values():
            assert schema.get("description"), f"{tool.name} has an undescribed parameter"

    by_name = {tool.name: tool for tool in tools}
    for name in (
        "list_work_items",
        "list_work_group_items",
        "list_template_packages",
        "list_work_item_documents",
        "list_work_item_version_documents",
    ):
        limit_schema = by_name[name].input_schema["properties"]["limit"]
        assert limit_schema["default"] == DEFAULT_LIST_LIMIT
        assert limit_schema["maximum"] == MAX_LIST_LIMIT

    for name in (
        "create_work_item",
        "create_work_item_version",
        "patch_work_item_answers",
        "complete_work_item_version",
    ):
        assert "confirm" in by_name[name].input_schema["required"]


async def test_server_negotiates_mcp_protocol_revision_2026_07_28(settings: Settings) -> None:
    hotdocs = HotDocsClient(settings)
    server = create_server(hotdocs)
    try:
        async with Client(server) as mcp_client:
            assert mcp_client.protocol_version == "2026-07-28"
    finally:
        await hotdocs.aclose()


@respx.mock
async def test_write_confirmation_stops_request_before_authentication(settings: Settings) -> None:
    hotdocs = HotDocsClient(settings, max_retries=0)
    server = create_server(hotdocs)
    try:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "create_work_item_version",
                {
                    "work_item_id": "wi-1",
                    "version_id": NEW_VERSION_ID,
                    "template_package_id": "tp-1",
                    "confirm": False,
                },
            )
    finally:
        await hotdocs.aclose()

    assert result.is_error is True
    assert "Confirmation required" in result.content[0].text
    assert len(respx.calls) == 0


@respx.mock
async def test_vendor_error_is_redacted_in_mcp_tool_error(settings: Settings) -> None:
    respx.post(settings.auth_url).mock(return_value=httpx.Response(200, text="token"))
    respx.get(f"{settings.api_base_url}WorkItems").mock(
        return_value=httpx.Response(
            409,
            text="assembly session is locked",
            headers={"Content-Type": "text/plain", "X-Correlation-Id": "corr-9"},
        )
    )
    hotdocs = HotDocsClient(settings, max_retries=0)
    server = create_server(hotdocs)
    try:
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool("list_work_items", {})
    finally:
        await hotdocs.aclose()

    assert result.is_error is True
    error_text = result.content[0].text
    assert "assembly session is locked" not in error_text
    assert "corr-9" not in error_text
    assert "Check inputs" in error_text
