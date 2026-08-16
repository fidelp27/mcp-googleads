"""Tests for the GoogleAdsError parser + typed exception."""
from __future__ import annotations

import json

import pytest

from agency.errors import GoogleAdsError, GoogleAdsErrorItem, _parse_error_code, _parse_location


API_VERSION = "v25"


def _wrap(error: dict) -> str:
    return json.dumps({"error": error})


# ---------- from_response happy path ----------


def test_from_response_parses_permission_denied_metric_access():
    body = _wrap(
        {
            "code": 403,
            "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "requestId": "req-abc",
                    "errors": [
                        {
                            "errorCode": {"authorizationError": "METRIC_ACCESS_DENIED"},
                            "message": "The developer doesn't have access to metrics: 'auction_insight_search_impression_share'",
                        }
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=403, api_version=API_VERSION, body=body
    )
    assert err.http_status == 403
    assert err.status == "PERMISSION_DENIED"
    assert err.request_id == "req-abc"
    assert err.message.startswith("The caller does not have permission")
    assert len(err.errors) == 1
    item = err.errors[0]
    assert item.error_code_category == "authorizationError"
    assert item.error_code_value == "METRIC_ACCESS_DENIED"
    assert item.docs_url == (
        "https://developers.google.com/google-ads/api/reference/rpc/v25/"
        "AuthorizationErrorEnum.AuthorizationError"
    )


def test_from_response_parses_invalid_argument_with_location():
    body = _wrap(
        {
            "code": 400,
            "message": "Request contains an invalid argument.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "requestId": "req-xyz",
                    "errors": [
                        {
                            "errorCode": {"queryError": "UNRECOGNIZED_FIELD"},
                            "message": "Unrecognized field: 'metrics.video_views'",
                            "location": {
                                "fieldPathElements": [
                                    {"fieldName": "query"}
                                ]
                            },
                        }
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    assert err.errors[0].location_field == "query"
    assert err.errors[0].error_code_value == "UNRECOGNIZED_FIELD"


def test_from_response_handles_multiple_errors_per_failure():
    body = _wrap(
        {
            "code": 400,
            "message": "multiple",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "requestId": "r1",
                    "errors": [
                        {
                            "errorCode": {"keywordPlanError": "INVALID_KEYWORD"},
                            "message": "kw invalid",
                        },
                        {
                            "errorCode": {"quotaError": "RESOURCE_EXHAUSTED"},
                            "message": "too many",
                        },
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    assert len(err.errors) == 2
    assert err.has_code("keywordPlanError")
    assert err.has_code("keywordPlanError", "INVALID_KEYWORD")
    assert not err.has_code("keywordPlanError", "SOMETHING_ELSE")
    assert err.has_code("quotaError")


def test_from_response_docs_url_none_for_unknown_category():
    body = _wrap(
        {
            "code": 400,
            "message": "unknown",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": [
                        {
                            "errorCode": {"mysteryError": "MYSTERY"},
                            "message": "unknown category",
                        }
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    assert err.errors[0].docs_url is None
    assert err.errors[0].error_code_category == "mysteryError"


# ---------- fault-tolerant parsing ----------


def test_from_response_handles_non_json_body():
    err = GoogleAdsError.from_response(
        http_status=500, api_version=API_VERSION, body="<html>Server error</html>"
    )
    assert err.http_status == 500
    assert err.errors == []
    assert err.raw.startswith("<html>")


def test_from_response_handles_empty_body():
    err = GoogleAdsError.from_response(
        http_status=502, api_version=API_VERSION, body=""
    )
    assert err.http_status == 502
    assert err.message is None
    assert err.errors == []


def test_from_response_handles_missing_details():
    body = _wrap({"code": 400, "message": "just a message"})
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    assert err.message == "just a message"
    assert err.errors == []


def test_from_response_handles_malformed_detail_item():
    body = _wrap(
        {
            "code": 400,
            "message": "half broken",
            "details": [
                "not-a-dict",
                {"errors": ["also not a dict", {"errorCode": None, "message": "ok"}]},
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    assert len(err.errors) == 1
    assert err.errors[0].error_code_category is None


# ---------- summary / str() ----------


def test_summary_includes_key_fields():
    body = _wrap(
        {
            "code": 400,
            "message": "hello",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "requestId": "abc",
                    "errors": [
                        {
                            "errorCode": {"queryError": "UNRECOGNIZED_FIELD"},
                            "message": "bad",
                        }
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    summary = str(err)
    assert "400" in summary
    assert "abc" in summary
    assert "INVALID_ARGUMENT" in summary
    assert "queryError:UNRECOGNIZED_FIELD" in summary


def test_googleads_error_is_runtime_error_for_backward_compat():
    err = GoogleAdsError(http_status=500, api_version="v25")
    assert isinstance(err, RuntimeError)


# ---------- to_dict serialization ----------


def test_to_dict_stable_shape():
    body = _wrap(
        {
            "code": 400,
            "message": "test",
            "details": [
                {
                    "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
                    "errors": [
                        {
                            "errorCode": {"queryError": "UNRECOGNIZED_FIELD"},
                            "message": "bad",
                        }
                    ],
                }
            ],
        }
    )
    err = GoogleAdsError.from_response(
        http_status=400, api_version=API_VERSION, body=body
    )
    d = err.to_dict()
    assert set(d.keys()) == {
        "http_status",
        "api_version",
        "request_id",
        "status",
        "message",
        "errors",
    }
    assert d["errors"][0]["error_code_category"] == "queryError"


# ---------- helper functions ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"queryError": "UNRECOGNIZED_FIELD"}, ("queryError", "UNRECOGNIZED_FIELD")),
        ({"authorizationError": "USER_PERMISSION_DENIED"}, ("authorizationError", "USER_PERMISSION_DENIED")),
        ({}, (None, None)),
        (None, (None, None)),
        ("not-a-dict", (None, None)),
        ({"foo": None, "bar": "baz"}, ("bar", "baz")),
    ],
)
def test_parse_error_code(raw, expected):
    assert _parse_error_code(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("not-a-dict", None),
        ({}, None),
        (
            {"fieldPathElements": [{"fieldName": "operations", "index": 0}, {"fieldName": "create"}]},
            "operations[0].create",
        ),
        (
            {"fieldPathElements": [{"fieldName": "query"}]},
            "query",
        ),
    ],
)
def test_parse_location(raw, expected):
    assert _parse_location(raw) == expected


# ---------- backwards compatibility with existing except RuntimeError ----------


def test_can_still_catch_as_runtime_error():
    err = GoogleAdsError(http_status=500, api_version="v25")
    try:
        raise err
    except RuntimeError as caught:
        assert caught is err
