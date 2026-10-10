"""MCP 2026-07-28 server exposing HotDocs Advance operations."""

from __future__ import annotations

import asyncio
import base64
import copy
import os
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette

from hotdocs_mcp import __version__
from hotdocs_mcp.client import HotDocsClient, HotDocsError, HotDocsProtocolError, JsonValue
from hotdocs_mcp.paths import path_id
from hotdocs_mcp.runtime import LazyClient
from hotdocs_mcp.runtime import SafeMCPServer as MCPServer
from hotdocs_mcp.urls import destination_https

MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
STREAMABLE_HTTP_TRANSPORT = "streamable-http"

WorkItemId = Annotated[str, Field(min_length=1, description="The HotDocs work item identifier.")]
VersionId = Annotated[str, Field(min_length=1, description="The HotDocs version identifier.")]
DocumentId = Annotated[str, Field(min_length=1, description="The HotDocs document identifier.")]
ListLimit = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_LIST_LIMIT,
        description="Maximum items to return locally, from 1 through 100.",
    ),
]
Confirmation = Annotated[
    bool,
    Field(description="Set to true only after reviewing and approving this write operation."),
]


def _decode_json(response: Any, method: str, path: str) -> JsonValue:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HotDocsProtocolError(
            f"HotDocs Advance returned non-JSON content for {method} {path}: "
            f"content_type={response.headers.get('content-type', '')!r}, "
            f"raw_body={response.text!r}"
        ) from exc


def _response_result(response: Any, method: str, path: str) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "data": _decode_json(response, method, path),
    }


def _collection_result(response: Any, method: str, path: str, limit: int) -> dict[str, Any]:
    payload = _decode_json(response, method, path)
    limited = copy.deepcopy(payload)
    returned: int | None = None
    truncated: bool | None = None
    returned_by_field: dict[str, int] = {}

    if isinstance(limited, list):
        returned = min(len(limited), limit)
        truncated = len(limited) > limit
        limited = limited[:limit]
    elif isinstance(limited, dict):
        for key, value in limited.items():
            if isinstance(value, list):
                returned_by_field[key] = min(len(value), limit)
                limited[key] = value[:limit]
        if len(returned_by_field) == 1:
            returned = next(iter(returned_by_field.values()))
            source_key = next(iter(returned_by_field))
            original = payload[source_key] if isinstance(payload, dict) else []
            truncated = len(original) > limit

    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "data": limited,
        "collection": {
            "limit": limit,
            "returned": returned,
            "returned_by_field": returned_by_field,
            "truncated": truncated,
            "automatic_pagination": False,
        },
    }


def _require_confirmation(confirm: bool, action: str) -> None:
    if not confirm:
        raise HotDocsError(
            f"Confirmation required: set confirm=true only after reviewing the {action} operation."
        )


def _require_xsd_attestation(answer_xml: str | None, answer_xml_is_xsd_validated: bool) -> None:
    if answer_xml is not None and not answer_xml_is_xsd_validated:
        raise HotDocsError(
            "HotDocs Answer XML must be validated against the published Answer Set XSD; "
            "set answer_xml_is_xsd_validated=true only after that validation succeeds."
        )


def create_server(client: HotDocsClient | LazyClient | None = None) -> MCPServer:
    """Create the MCP application and register its HotDocs Advance tools."""
    client = client if client is not None else LazyClient()
    server = MCPServer("HotDocs Advance", title="HotDocs Advance", version=__version__)

    @server.tool()
    async def list_work_items(
        limit: ListLimit = DEFAULT_LIST_LIMIT,
        status_filter: Annotated[
            str | None,
            Field(description="Optional HotDocs status filter expression."),
        ] = None,
    ) -> dict[str, Any]:
        """Find work items you are allowed to see."""
        params = {"filter": status_filter} if status_filter else None
        path = "/WorkItems"
        response = await client.request("GET", path, params=params)
        return _collection_result(response, "GET", path, limit)

    @server.tool()
    async def list_work_group_items(
        work_group_id: Annotated[
            str,
            Field(min_length=1, description="The work group identifier."),
        ],
        limit: ListLimit = DEFAULT_LIST_LIMIT,
        status_filter: Annotated[
            str | None,
            Field(description="Optional HotDocs status filter expression."),
        ] = None,
    ) -> dict[str, Any]:
        """List the work items held in one work group."""
        path = f"/WorkGroups/{path_id(work_group_id, 'work_group_id')}/WorkItems"
        params = {"filter": status_filter} if status_filter else None
        response = await client.request("GET", path, params=params)
        return _collection_result(response, "GET", path, limit)

    @server.tool()
    async def list_template_packages(limit: ListLimit = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
        """List the templates available for creating work items."""
        path = "/TemplatePackages"
        response = await client.request("GET", path)
        return _collection_result(response, "GET", path, limit)

    @server.tool()
    async def get_template_variable_data(
        template_package_id: Annotated[
            str,
            Field(min_length=1, description="The template package identifier."),
        ],
        version_id: VersionId,
    ) -> dict[str, Any]:
        """Show the questions and data fields used by a template version."""
        path = (
            f"/TemplatePackages/{path_id(template_package_id, 'template_package_id')}"
            f"/Versions/{path_id(version_id, 'version_id')}/VariableData"
        )
        response = await client.request("GET", path)
        return _response_result(response, "GET", path)

    @server.tool()
    async def get_work_item_answers(
        work_item_id: WorkItemId,
        answer_format: Annotated[
            Literal["xml", "json"],
            Field(description="Return the answer set as XML or JSON."),
        ] = "xml",
    ) -> dict[str, Any]:
        """Retrieve the saved answers for a work item."""
        path = f"/WorkItems/{path_id(work_item_id, 'work_item_id')}/AnswerSet/Content"
        accept = "application/xml" if answer_format == "xml" else "application/json"
        response = await client.request("GET", path, headers={"Accept": accept})
        data: JsonValue = (
            _decode_json(response, "GET", path) if answer_format == "json" else response.text
        )
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "format": answer_format,
            "data": data,
        }

    @server.tool()
    async def list_work_item_documents(
        work_item_id: WorkItemId,
        limit: ListLimit = DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        """List documents from a work item's latest completed version."""
        path = f"/WorkItems/{path_id(work_item_id, 'work_item_id')}/Documents"
        response = await client.request("GET", path)
        return _collection_result(response, "GET", path, limit)

    @server.tool()
    async def list_work_item_version_documents(
        work_item_id: WorkItemId,
        version_id: VersionId,
        limit: ListLimit = DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        """List documents from one historical work item version."""
        path = (
            f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
            f"/Versions/{path_id(version_id, 'version_id')}/Documents"
        )
        response = await client.request("GET", path)
        return _collection_result(response, "GET", path, limit)

    @server.tool()
    async def get_work_item_document(
        work_item_id: WorkItemId,
        document_id: DocumentId,
    ) -> dict[str, Any]:
        """Show one document and any questions that were left unanswered."""
        path = (
            f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
            f"/Documents/{path_id(document_id, 'document_id')}"
        )
        response = await client.request("GET", path)
        return _response_result(response, "GET", path)

    @server.tool()
    async def download_work_item_document(
        work_item_id: WorkItemId,
        document_id: DocumentId,
    ) -> dict[str, Any]:
        """Download an assembled document in a JSON-safe format."""
        path = (
            f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
            f"/Documents/{path_id(document_id, 'document_id')}/Content"
        )
        response = await client.request("GET", path, headers={"Accept": "*/*"})
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "content_disposition": response.headers.get("content-disposition"),
            "size_bytes": len(response.content),
            "encoding": "base64",
            "content_base64": base64.b64encode(response.content).decode("ascii"),
        }

    @server.tool()
    async def create_work_item(
        work_item_id: Annotated[UUID, Field(description="A new caller-generated work item GUID.")],
        name: Annotated[str, Field(min_length=1, description="The work item name.")],
        work_group_id: Annotated[
            str,
            Field(min_length=1, description="The work group identifier."),
        ],
        template_package_ids: Annotated[
            list[str],
            Field(min_length=1, description="One or more template package identifiers."),
        ],
        confirm: Confirmation,
        description: Annotated[
            str | None,
            Field(description="Optional work item description."),
        ] = None,
        answer_xml: Annotated[
            str | None,
            Field(description="Optional HotDocs Answer XML to pre-populate the work item."),
        ] = None,
        answer_xml_is_xsd_validated: Annotated[
            bool,
            Field(description="Confirm that answer_xml passed the published Answer Set XSD."),
        ] = False,
        is_private_to_owner: Annotated[
            bool,
            Field(description="Whether only the owner can access the work item."),
        ] = False,
        use_default_answers: Annotated[
            bool,
            Field(description="Whether templates should contribute their default answers."),
        ] = True,
    ) -> dict[str, Any]:
        """Create a new work item after explicit approval."""
        _require_confirmation(confirm, "create work item")
        _require_xsd_attestation(answer_xml, answer_xml_is_xsd_validated)
        path = f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
        body: dict[str, Any] = {
            "name": name,
            "workGroupId": work_group_id,
            "templatePackageIds": template_package_ids,
            "isPrivateToOwner": is_private_to_owner,
            "useDefaultAnswers": use_default_answers,
        }
        if description is not None:
            body["description"] = description
        if answer_xml is not None:
            body["answerXml"] = answer_xml
        response = await client.request("PUT", path, json_body=body)
        return _response_result(response, "PUT", path)

    @server.tool()
    async def create_work_item_version(
        work_item_id: WorkItemId,
        version_id: Annotated[UUID, Field(description="A new caller-generated version GUID.")],
        template_package_id: Annotated[
            str,
            Field(min_length=1, description="The template package to assemble."),
        ],
        confirm: Confirmation,
    ) -> dict[str, Any]:
        """Start a new assembly version after explicit approval."""
        _require_confirmation(confirm, "create work item version")
        path = (
            f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
            f"/Versions/{path_id(version_id, 'version_id')}"
        )
        response = await client.request(
            "PUT", path, json_body={"templatePackageId": template_package_id}
        )
        return _response_result(response, "PUT", path)

    @server.tool()
    async def patch_work_item_answers(
        work_item_id: WorkItemId,
        answer_xml: Annotated[
            str,
            Field(
                min_length=1,
                description="HotDocs Answer XML containing the answers to overlay.",
            ),
        ],
        answer_xml_is_xsd_validated: Annotated[
            bool,
            Field(description="Confirm that answer_xml passed the published Answer Set XSD."),
        ],
        confirm: Confirmation,
    ) -> dict[str, Any]:
        """Overlay selected saved answers after explicit approval."""
        _require_confirmation(confirm, "patch work item answers")
        _require_xsd_attestation(answer_xml, answer_xml_is_xsd_validated)
        path = f"/WorkItems/{path_id(work_item_id, 'work_item_id')}/AnswerSet/Content"
        response = await client.request(
            "PATCH",
            path,
            content=answer_xml,
            headers={"Content-Type": "application/xml", "Accept": "application/json"},
        )
        return _response_result(response, "PATCH", path)

    @server.tool()
    async def complete_work_item_version(
        work_item_id: WorkItemId,
        version_id: VersionId,
        confirm: Confirmation,
        callback_url: Annotated[
            str | None,
            Field(description="Optional HTTPS URL notified when asynchronous completion finishes."),
        ] = None,
        callback_secret: Annotated[
            str | None,
            Field(
                description="Optional callback secret sent with the completion request.",
                json_schema_extra={"writeOnly": True},
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Finish assembly and produce documents after explicit approval."""
        _require_confirmation(confirm, "complete work item version")
        if callback_secret is not None and callback_url is None:
            raise HotDocsError("callback_url is required when callback_secret is provided.")
        path = (
            f"/WorkItems/{path_id(work_item_id, 'work_item_id')}"
            f"/Versions/{path_id(version_id, 'version_id')}/Complete"
        )
        body: dict[str, Any] = {}
        if callback_url is not None:
            destination_https(callback_url)
            body["callbackUrl"] = callback_url
        if callback_secret is not None:
            body["callbackSecret"] = callback_secret
        response = await client.request("PUT", path, json_body=body or None)
        return _response_result(response, "PUT", path)

    return server


mcp = create_server()


def _requested_transport() -> str:
    return os.environ.get("HOTDOCS_MCP_TRANSPORT", "stdio").strip().lower() or "stdio"


def _host() -> str:
    return os.environ.get("HOTDOCS_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _port() -> int:
    raw = os.environ.get("PORT", "8080").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"PORT must be an integer, got {raw!r}") from exc


def _transport_security() -> TransportSecuritySettings | None:
    if _host() in ("127.0.0.1", "localhost", "::1"):
        return None
    allowed_hosts = [
        host.strip()
        for host in os.environ.get("HOTDOCS_MCP_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    ]
    if not allowed_hosts:
        raise SystemExit(
            "HOTDOCS_MCP_ALLOWED_HOSTS must be set to comma-separated allowed Host headers "
            "when HOTDOCS_MCP_HOST is not a loopback host."
        )
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get("HOTDOCS_MCP_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def create_serve_app() -> Starlette:
    """Build the stateless MCP endpoint with SDK Host and Origin validation."""
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        host=_host(),
        stateless_http=True,
        transport_security=_transport_security(),
    )


async def _serve_streamable_http() -> None:
    import uvicorn

    config = uvicorn.Config(
        create_serve_app(),
        host=_host(),
        port=_port(),
        log_level=mcp.settings.log_level.lower(),
        access_log=False,
    )
    await uvicorn.Server(config).serve()


def main() -> int:
    """Select the transport while deferring vendor configuration until tool use."""
    transport = _requested_transport()
    if transport == "stdio":
        client = LazyClient()
        try:
            create_server(client).run(transport="stdio")
        finally:
            asyncio.run(client.aclose())
        return 0
    if transport == STREAMABLE_HTTP_TRANSPORT:
        asyncio.run(_serve_streamable_http())
        return 0
    raise SystemExit(
        "Unsupported HOTDOCS_MCP_TRANSPORT "
        f"{transport!r}; expected 'stdio' or '{STREAMABLE_HTTP_TRANSPORT}'."
    )


if __name__ == "__main__":
    raise SystemExit(main())
