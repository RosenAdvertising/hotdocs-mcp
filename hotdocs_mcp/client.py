"""Thin asynchronous HTTP client for HotDocs Advance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from hotdocs_mcp.config import Settings
from hotdocs_mcp.urls import validate_url_arguments

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
ItemT = TypeVar("ItemT")
SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HotDocsError(ToolError):
    """Base class for errors that should be visible to an MCP caller."""


class HotDocsAPIError(HotDocsError):
    """An HTTP error with the vendor response preserved without schema assumptions."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.headers = dict(response.headers)
        self.content_type = response.headers.get("content-type", "")
        self.raw_body = response.text
        detail = {
            "status": self.status_code,
            "headers": self.headers,
            "content_type": self.content_type,
            "raw_body": self.raw_body,
        }
        super().__init__(f"HotDocs Advance API error: {json.dumps(detail, sort_keys=True)}")


class HotDocsProtocolError(HotDocsError):
    """A successful vendor response did not match the documented content contract."""


class HotDocsTransportError(HotDocsError):
    """A request could not reach HotDocs Advance."""


class HotDocsClient:
    """Authenticate and make retried calls to the HotDocs Advance REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_http_client = http_client is None
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._sleep = sleep
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> HotDocsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally owned HTTP client."""
        if self._owns_http_client:
            await self._http.aclose()

    async def authenticate(self) -> str:
        """Exchange ResourceOwner service-principal credentials for a bearer token."""
        if self._token is not None:
            return self._token

        async with self._token_lock:
            if self._token is not None:
                return self._token
            response = await self._send_with_retries(
                "POST",
                self.settings.auth_url,
                json={
                    "ClientName": self.settings.client_name,
                    "PrincipalName": self.settings.principal_name,
                    "PrincipalPassword": self.settings.principal_password,
                },
                headers={"Accept": "text/plain"},
                # Sign-in only issues a bearer token and changes no records, so it may be resent.
                idempotent=True,
            )
            self._raise_for_vendor_error(response)
            token = response.text.strip()
            if not token:
                raise HotDocsProtocolError(
                    "HotDocs Advance authentication returned 200 but the text token was empty."
                )
            self._token = token
            return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: JsonValue | None = None,
        content: str | bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one authenticated API request and preserve vendor failures."""
        validate_url_arguments(json_body)
        validate_url_arguments(params)
        token = await self.authenticate()
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)
        url = self._api_url(path)
        response = await self._send_with_retries(
            method,
            url,
            params=params,
            json=json_body,
            content=content,
            headers=request_headers,
        )
        if response.status_code == 401:
            await self._invalidate_token(token)
            replacement_token = await self.authenticate()
            request_headers["Authorization"] = f"Bearer {replacement_token}"
            response = await self._send_with_retries(
                method,
                url,
                params=params,
                json=json_body,
                content=content,
                headers=request_headers,
            )
        self._raise_for_vendor_error(response)
        return response

    async def request_json(self, method: str, path: str, **kwargs: Any) -> JsonValue:
        """Send an API request and decode a JSON body, allowing documented empty responses."""
        response = await self.request(method, path, **kwargs)
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

    async def paginate(
        self,
        path: str,
        *,
        limit: int,
        extract_items: Callable[[JsonValue], Sequence[ItemT]],
        extract_next_path: Callable[[JsonValue], str | None],
        params: Mapping[str, str] | None = None,
    ) -> list[ItemT]:
        """Follow tenant-verified page links without assuming vendor parameter or envelope names."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        items: list[ItemT] = []
        next_path: str | None = path
        next_params = params
        while next_path is not None and len(items) < limit:
            payload = await self.request_json("GET", next_path, params=next_params)
            items.extend(extract_items(payload)[: limit - len(items)])
            next_path = extract_next_path(payload)
            next_params = None
        return items

    def _api_url(self, path: str) -> str:
        relative = path.lstrip("/")
        return f"{self.settings.api_base_url}{relative}"

    async def _invalidate_token(self, rejected_token: str) -> None:
        async with self._token_lock:
            if self._token == rejected_token:
                self._token = None

    async def _send_with_retries(
        self, method: str, url: str, *, idempotent: bool = False, **kwargs: Any
    ) -> httpx.Response:
        # Only reads (and calls the code marks idempotent) are resent: a write that got a 5xx
        # may already have been applied, so resending it could apply it twice.
        can_retry = idempotent or method.upper() in SAFE_RETRY_METHODS
        max_retries = self._max_retries if can_retry else 0
        for attempt in range(max_retries + 1):
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                raise HotDocsTransportError(
                    f"Could not reach HotDocs Advance for {method} {url}: {exc}"
                ) from exc

            retryable = response.status_code == 429 or 500 <= response.status_code <= 599
            if not retryable or attempt == max_retries:
                return response
            await self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("retry loop exhausted unexpectedly")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    now = parsedate_to_datetime(response.headers.get("date", ""))
                    return max(0.0, (retry_at - now).total_seconds())
                except (TypeError, ValueError):
                    pass
        return self._backoff_base_seconds * (2**attempt)

    @staticmethod
    def _raise_for_vendor_error(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise HotDocsAPIError(response)
