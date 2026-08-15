"""Google Ads REST client with retries, pagination and local rate limiting."""

from __future__ import annotations

import json
import random
import time
from collections import defaultdict, deque
from threading import RLock
from typing import Any

import requests

from .config import ensure_customer_allowed, get_settings


_rate_lock = RLock()
_request_times: dict[str, deque[float]] = defaultdict(deque)


def _server_module():
    import google_ads_server

    return google_ads_server


def _headers(login_customer_id: str | None = None) -> dict[str, str]:
    server = _server_module()
    headers = server.get_headers(server.get_credentials())
    if login_customer_id:
        headers["login-customer-id"] = ensure_customer_allowed(login_customer_id)
    return headers


def _rate_limit(customer_id: str) -> None:
    limit = get_settings().rate_limit_per_minute
    now = time.monotonic()
    with _rate_lock:
        bucket = _request_times[customer_id]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= limit:
            wait_seconds = min(30.0, max(0.0, 60 - (now - bucket[0])))
        else:
            wait_seconds = 0.0
    if wait_seconds:
        time.sleep(wait_seconds)
    with _rate_lock:
        _request_times[customer_id].append(time.monotonic())


def request(
    method: str,
    path: str,
    *,
    customer_id: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    retries: int = 3,
) -> dict[str, Any]:
    customer_id = ensure_customer_allowed(customer_id)
    _rate_limit(customer_id)
    server = _server_module()
    url = f"https://googleads.googleapis.com/{server.API_VERSION}/{path.lstrip('/')}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.request(
                method,
                url,
                headers=_headers(),
                json=body,
                params=params,
                timeout=server.HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                retry_after = float(response.headers.get("Retry-After", 0) or 0)
                time.sleep(max(retry_after, (2**attempt) + random.random()))
                continue
            if not response.ok:
                request_id = response.headers.get("request-id")
                detail = response.text[:4000]
                raise RuntimeError(
                    f"Google Ads API {response.status_code}; request_id={request_id}; response={detail}"
                )
            if not response.content:
                return {}
            return response.json()
        except (requests.RequestException, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
            if attempt >= retries - 1 or isinstance(error, RuntimeError):
                raise
            time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"Google Ads request failed: {last_error}")


def search(customer_id: str, query: str) -> list[dict[str, Any]]:
    customer_id = ensure_customer_allowed(customer_id)
    results: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        # The REST googleAds:search endpoint fixes page size at 10000 rows and
        # rejects an explicit pageSize with PAGE_SIZE_NOT_SUPPORTED.
        body: dict[str, Any] = {"query": query}
        if page_token:
            body["pageToken"] = page_token
        response = request(
            "POST",
            f"customers/{customer_id}/googleAds:search",
            customer_id=customer_id,
            body=body,
        )
        results.extend(response.get("results", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return results


def mutate(
    customer_id: str,
    service: str,
    operations: list[dict[str, Any]],
    *,
    validate_only: bool,
    partial_failure: bool = False,
) -> dict[str, Any]:
    customer_id = ensure_customer_allowed(customer_id)
    body = {
        "operations": operations,
        "validateOnly": validate_only,
        "partialFailure": partial_failure,
        "responseContentType": "MUTABLE_RESOURCE",
    }
    return request(
        "POST",
        f"customers/{customer_id}/{service}:mutate",
        customer_id=customer_id,
        body=body,
    )


def batch_mutate(
    customer_id: str,
    mutate_operations: list[dict[str, Any]],
    *,
    validate_only: bool,
) -> dict[str, Any]:
    customer_id = ensure_customer_allowed(customer_id)
    return request(
        "POST",
        f"customers/{customer_id}/googleAds:mutate",
        customer_id=customer_id,
        body={
            "mutateOperations": mutate_operations,
            "validateOnly": validate_only,
            "responseContentType": "MUTABLE_RESOURCE",
        },
    )


def format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
