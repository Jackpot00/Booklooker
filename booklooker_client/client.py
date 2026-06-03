"""Client utilities for the Booklooker REST API v2.0.

The official API returns JSON envelopes shaped as:

    {"status": "OK", "returnValue": ...}

This module keeps the raw response available while exposing normalized keys:
``ok``, ``status``, ``data`` and ``error``.
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Generator, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests import Response, Session
from requests.exceptions import RequestException

DEFAULT_BASE_URL = "https://api.booklooker.de/2.0"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT = 100
DEFAULT_RATE_PERIOD = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_RETRY_AFTER = 60.0
TOKEN_ERROR_CODES = {"TOKEN_EXPIRED", "TOKEN_MISSING", "TOKEN_UNKNOWN"}
RETRY_API_CODES = {"QUOTA_EXCEEDED", "SERVER_DOWN", "TEMPORARILY_BLOCKED"}
RETRY_HTTP_STATUSES = {429, 500, 502, 503, 504}


class BooklookerError(Exception):
    """Base exception for Booklooker client errors."""


class BooklookerHTTPError(BooklookerError):
    """Raised when an HTTP request fails permanently."""

    def __init__(self, message: str, response: Response | None = None) -> None:
        super().__init__(message)
        self.response = response


class BooklookerAPIError(BooklookerError):
    """Raised when the Booklooker API returns a NOK envelope and requested so."""

    def __init__(self, message: str, payload: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass
class RateLimiter:
    """Simple sliding-window rate limiter.

    Booklooker documents a global limit of 100 REST requests per minute. Search
    has an additional quota; pass a stricter limiter when using that endpoint if
    your account requires it.
    """

    max_calls: int = DEFAULT_RATE_LIMIT
    period: float = DEFAULT_RATE_PERIOD
    sleep: Any = time.sleep
    now: Any = time.monotonic

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if self.period <= 0:
            raise ValueError("period must be greater than 0")
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        """Block until another request can be made."""

        while True:
            current = self.now()
            while self._calls and current - self._calls[0] >= self.period:
                self._calls.popleft()

            if len(self._calls) < self.max_calls:
                self._calls.append(current)
                return

            wait_for = self.period - (current - self._calls[0])
            if wait_for > 0:
                self.sleep(wait_for)


class BooklookerClient:
    """Requests-based client for the Booklooker REST API v2.0."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        session: Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        rate_limiter: RateLimiter | None = None,
        rate_limit_per_minute: int | None = DEFAULT_RATE_LIMIT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        raise_on_api_error: bool = False,
    ) -> None:
        self.api_key = api_key or os.getenv("BOOKLOOKER_API_KEY")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.raise_on_api_error = raise_on_api_error
        self.rate_limiter = rate_limiter
        if self.rate_limiter is None and rate_limit_per_minute:
            self.rate_limiter = RateLimiter(max_calls=rate_limit_per_minute)

    def authenticate(self, api_key: str | None = None) -> dict[str, Any]:
        """Authenticate via API key and store the returned REST token."""

        key = api_key or self.api_key
        if not key:
            raise ValueError(
                "api_key is required or BOOKLOOKER_API_KEY must be set"
            )

        result = self._request(
            "POST",
            "/authenticate",
            params={"apiKey": key},
            authenticated=False,
        )
        if result["ok"]:
            self.token = str(result["data"])
        return result

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Call an arbitrary Booklooker endpoint and return normalized JSON."""

        return self._request(
            method,
            endpoint,
            params=params,
            data=data,
            json=json,
            files=files,
            headers=headers,
            authenticated=authenticated,
        )

    def iter_pages(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        items_key: str | None = None,
        page_param: str = "page",
        per_page_param: str = "limit",
        start_page: int = 1,
        per_page: int | None = None,
        max_pages: int | None = None,
        stop_when_empty: bool = True,
        authenticated: bool = True,
    ) -> Generator[dict[str, Any], None, None]:
        """Yield normalized page responses for endpoints with page parameters.

        Booklooker's documented REST endpoints mostly return bounded result sets
        rather than a standard pagination envelope. This helper is intentionally
        configurable so callers can use it with any endpoint/parameter pair that
        Booklooker adds or enables for their account.
        """

        page = start_page
        pages_seen = 0
        base_params = dict(params or {})

        while max_pages is None or pages_seen < max_pages:
            request_params = dict(base_params)
            request_params[page_param] = page
            if per_page is not None:
                request_params[per_page_param] = per_page

            payload = self.request(
                method,
                endpoint,
                params=request_params,
                authenticated=authenticated,
            )
            yield payload

            pages_seen += 1
            data = self._extract_items(payload["data"], items_key)
            if stop_when_empty and not data:
                break
            if per_page is not None and isinstance(data, list) and len(data) < per_page:
                break
            page += 1

    def iter_items(self, *args: Any, items_key: str | None = None, **kwargs: Any) -> Generator[Any, None, None]:
        """Yield items across paginated responses."""

        for page in self.iter_pages(*args, items_key=items_key, **kwargs):
            data = self._extract_items(page["data"], items_key)
            if isinstance(data, list):
                yield from data
            elif data is not None:
                yield data

    def get_article_list(
        self,
        *,
        field: str = "orderNo",
        show_price: bool = False,
        show_stock: bool = False,
        media_type: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "field": field,
            "showPrice": int(show_price),
            "showStock": int(show_stock),
        }
        if media_type is not None:
            params["mediaType"] = media_type
        return self.request("GET", "/article_list", params=params)

    def get_article_status(self, order_no: str) -> dict[str, Any]:
        return self.request("GET", "/article_status", params={"orderNo": order_no})

    def delete_article(self, order_no: str) -> dict[str, Any]:
        return self.request("DELETE", "/article", params={"orderNo": order_no})

    def delete_image(
        self, order_no: str, *, position: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"orderNo": order_no}
        if position is not None:
            params["position"] = position
        return self.request("DELETE", "/image", params=params)

    def search(self, **params: Any) -> dict[str, Any]:
        """Search Booklooker offers.

        Common parameters include ``medium``, ``title``, ``author``, ``isbn``,
        ``ean``, ``limit``, ``sortOrder`` and ``sortDir``.
        """

        return self.request("GET", "/search", params=params)

    def import_file(
        self,
        file_content: bytes | Iterable[bytes],
        *,
        data_type: int,
        file_type: str = "article",
        media_type: int = 0,
        format_id: int | None = None,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fileType": file_type,
            "dataType": data_type,
            "mediaType": media_type,
        }
        if format_id is not None:
            params["formatID"] = format_id
        if encoding is not None:
            params["encoding"] = encoding
        return self.request(
            "POST",
            "/file_import",
            params=params,
            data=file_content,
            headers={"Content-Type": "application/octet-stream"},
        )

    def get_file_status(
        self, filename: str, *, show_errors: bool = False
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            "/file_status",
            params={"filename": filename, "showErrors": int(show_errors)},
        )

    def get_import_status(self) -> dict[str, Any]:
        return self.request("GET", "/import_status")

    def get_orders(
        self,
        *,
        order_id: int | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if order_id is not None:
            params["orderId"] = order_id
        if date is not None:
            params["date"] = date
        if date_from is not None:
            params["dateFrom"] = date_from
        if date_to is not None:
            params["dateTo"] = date_to
        return self.request("GET", "/order", params=params)

    def cancel_order(self, order_id: int) -> dict[str, Any]:
        return self.request("PUT", "/order_cancel", params={"orderId": order_id})

    def cancel_order_item(
        self, order_item_id: int, *, media_type: int
    ) -> dict[str, Any]:
        return self.request(
            "PUT",
            "/order_item_cancel",
            params={"orderItemId": order_item_id, "mediaType": media_type},
        )

    def send_order_message(self, order_id: int, message: str) -> dict[str, Any]:
        return self.request(
            "PUT",
            "/order_message",
            params={"orderId": order_id, "message": message},
        )

    def set_order_status(
        self,
        order_id: int,
        status: str,
        *,
        delivery_tracking_no: str | None = None,
        delivery_service: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"orderId": order_id, "status": status}
        if delivery_tracking_no is not None:
            params["deliveryTrackingNo"] = delivery_tracking_no
        if delivery_service is not None:
            params["deliveryService"] = delivery_service
        return self.request("PUT", "/order_status", params=params)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        _reauthenticated: bool = False,
    ) -> dict[str, Any]:
        if authenticated:
            self._ensure_token()

        request_params = self._clean_params(params)
        if authenticated:
            request_params["token"] = self.token

        response_payload: dict[str, Any] | None = None
        last_error: RequestException | None = None

        for attempt in range(self.max_retries + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()

            try:
                response = self.session.request(
                    method=method.upper(),
                    url=self._url(endpoint),
                    params=request_params,
                    data=data,
                    json=json,
                    files=files,
                    headers=dict(headers or {}),
                    timeout=self.timeout,
                )
            except RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise BooklookerHTTPError(
                        f"Booklooker request failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleep_before_retry(attempt)
                continue

            if self._should_retry_http(response.status_code) and attempt < self.max_retries:
                self._sleep_before_retry(attempt, response)
                continue

            if response.status_code >= 400:
                raise BooklookerHTTPError(
                    f"Booklooker returned HTTP {response.status_code}", response=response
                )

            response_payload = self._decode_response(response)
            normalized = self.normalize(response_payload)
            if self._should_retry_api(normalized) and attempt < self.max_retries:
                self._sleep_before_retry(attempt, response)
                continue

            if (
                authenticated
                and not _reauthenticated
                and normalized["error"] in TOKEN_ERROR_CODES
            ):
                self.authenticate()
                return self._request(
                    method,
                    endpoint,
                    params=params,
                    data=data,
                    json=json,
                    files=files,
                    headers=headers,
                    authenticated=authenticated,
                    _reauthenticated=True,
                )

            if self.raise_on_api_error and not normalized["ok"]:
                raise BooklookerAPIError(
                    f"Booklooker API returned {normalized['error']}", normalized
                )
            return normalized

        if last_error is not None:
            raise BooklookerHTTPError(str(last_error)) from last_error
        if response_payload is None:
            raise BooklookerHTTPError("Booklooker request failed")
        return self.normalize(response_payload)

    def _ensure_token(self) -> None:
        if not self.token:
            self.authenticate()

    def _url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def _clean_params(
        self, params: Mapping[str, Any] | None
    ) -> MutableMapping[str, Any]:
        return {key: value for key, value in dict(params or {}).items() if value is not None}

    def _decode_response(self, response: Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BooklookerHTTPError(
                "Booklooker returned a non-JSON response", response=response
            ) from exc

        if not isinstance(payload, dict):
            raise BooklookerHTTPError(
                "Booklooker returned a JSON response that is not an object",
                response=response,
            )
        return payload

    def _should_retry_http(self, status_code: int) -> bool:
        return status_code in RETRY_HTTP_STATUSES

    def _should_retry_api(self, normalized: Mapping[str, Any]) -> bool:
        return normalized.get("error") in RETRY_API_CODES

    def _sleep_before_retry(
        self, attempt: int, response: Response | None = None
    ) -> None:
        retry_after = self._retry_after(response)
        if response is not None:
            response.close()
        if retry_after is not None:
            time.sleep(retry_after)
            return
        time.sleep(self.backoff_factor * (2**attempt))

    def _retry_after(self, response: Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return min(DEFAULT_MAX_RETRY_AFTER, max(0.0, float(value)))
        except ValueError:
            return None

    def normalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize a Booklooker envelope into predictable JSON."""

        status = str(payload.get("status", "")).upper()
        data = payload.get("returnValue", payload.get("return_value"))
        normalized_data = self._normalize_value(data)
        ok = status == "OK"
        error = None if ok else self._normalize_error(normalized_data)
        return {
            "ok": ok,
            "status": status,
            "data": normalized_data,
            "error": error,
            "raw": self._normalize_value(dict(payload)),
        }

    def _normalize_error(self, value: Any) -> str:
        if value is None:
            return "UNKNOWN_ERROR"
        if isinstance(value, str):
            return value
        return str(value)

    def _normalize_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {self._to_snake_case(str(key)): self._normalize_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [self._normalize_value(item) for item in value]
        return value

    def _extract_items(self, data: Any, items_key: str | None) -> Any:
        if items_key is None:
            return data
        current = data
        for part in items_key.split("."):
            if not isinstance(current, Mapping):
                return None
            current = current.get(part)
        return current

    def _to_snake_case(self, value: str) -> str:
        value = value.replace("-", "_")
        value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        return value.lower()
