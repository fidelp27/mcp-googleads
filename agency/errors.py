"""Structured Google Ads API errors.

`agency.api.request` used to raise a bare RuntimeError with the raw
response text, forcing callers to grep the message for context. This
module parses Google Ads' error envelope and raises a typed exception
so tools can react to specific error codes without string matching.

Serialization is stable so tools can put the exception straight into
the audit log or the tool response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


_DOCS_ROOT = "https://developers.google.com/google-ads/api/reference/rpc"
_ERROR_ENUM_MAP = {
    "authorizationError": "AuthorizationErrorEnum.AuthorizationError",
    "authenticationError": "AuthenticationErrorEnum.AuthenticationError",
    "queryError": "QueryErrorEnum.QueryError",
    "requestError": "RequestErrorEnum.RequestError",
    "quotaError": "QuotaErrorEnum.QuotaError",
    "internalError": "InternalErrorEnum.InternalError",
    "resourceCountLimitExceededError": (
        "ResourceCountLimitExceededErrorEnum.ResourceCountLimitExceededError"
    ),
    "keywordPlanError": "KeywordPlanErrorEnum.KeywordPlanError",
    "keywordPlanIdeaError": "KeywordPlanIdeaErrorEnum.KeywordPlanIdeaError",
    "conversionUploadError": "ConversionUploadErrorEnum.ConversionUploadError",
    "conversionAdjustmentUploadError": (
        "ConversionAdjustmentUploadErrorEnum.ConversionAdjustmentUploadError"
    ),
    "offlineUserDataJobError": (
        "OfflineUserDataJobErrorEnum.OfflineUserDataJobError"
    ),
    "customerMatchError": "CustomerMatchErrorEnum.CustomerMatchError",
    "policyValidationError": "PolicyValidationErrorEnum.PolicyValidationError",
    "policyFindingError": "PolicyFindingErrorEnum.PolicyFindingError",
    "campaignError": "CampaignErrorEnum.CampaignError",
    "adGroupError": "AdGroupErrorEnum.AdGroupError",
    "adError": "AdErrorEnum.AdError",
    "keywordError": "KeywordErrorEnum.KeywordError",
    "biddingError": "BiddingErrorEnum.BiddingError",
    "campaignBudgetError": "CampaignBudgetErrorEnum.CampaignBudgetError",
    "assetError": "AssetErrorEnum.AssetError",
    "userListError": "UserListErrorEnum.UserListError",
    "experimentError": "ExperimentErrorEnum.ExperimentError",
    "changeStatusError": "ChangeStatusErrorEnum.ChangeStatusError",
}


@dataclass
class GoogleAdsErrorItem:
    """One field-level error from GoogleAdsFailure.errors[]."""

    error_code_category: Optional[str] = None
    error_code_value: Optional[str] = None
    message: Optional[str] = None
    location_field: Optional[str] = None
    trigger_value: Optional[Any] = None
    docs_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code_category": self.error_code_category,
            "error_code_value": self.error_code_value,
            "message": self.message,
            "location_field": self.location_field,
            "trigger_value": self.trigger_value,
            "docs_url": self.docs_url,
        }


class GoogleAdsError(RuntimeError):
    """Structured Google Ads API error raised by agency.api.request.

    The class keeps a str() representation stable for backward
    compatibility (older callers still do `except RuntimeError`) but
    exposes structured fields for callers that want to react to a
    specific error code.
    """

    def __init__(
        self,
        *,
        http_status: int,
        api_version: str,
        request_id: Optional[str] = None,
        status: Optional[str] = None,
        message: Optional[str] = None,
        errors: Optional[list[GoogleAdsErrorItem]] = None,
        raw: Optional[str] = None,
    ) -> None:
        self.http_status = http_status
        self.api_version = api_version
        self.request_id = request_id
        self.status = status
        self.message = message
        self.errors: list[GoogleAdsErrorItem] = list(errors or [])
        self.raw = raw
        summary = _build_summary(
            http_status=http_status,
            request_id=request_id,
            status=status,
            message=message,
            errors=self.errors,
        )
        super().__init__(summary)

    @classmethod
    def from_response(
        cls,
        *,
        http_status: int,
        api_version: str,
        body: str,
    ) -> "GoogleAdsError":
        parsed = _safe_load_json(body)
        request_id: Optional[str] = None
        status: Optional[str] = None
        message: Optional[str] = None
        errors: list[GoogleAdsErrorItem] = []

        error_root = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error_root, dict):
            message = error_root.get("message") or None
            status = error_root.get("status") or None
            for detail in error_root.get("details") or []:
                if not isinstance(detail, dict):
                    continue
                request_id = detail.get("requestId") or request_id
                nested = detail.get("errors") or []
                for item in nested:
                    if not isinstance(item, dict):
                        continue
                    parsed_code = _parse_error_code(item.get("errorCode"))
                    location = _parse_location(item.get("location"))
                    errors.append(
                        GoogleAdsErrorItem(
                            error_code_category=parsed_code[0],
                            error_code_value=parsed_code[1],
                            message=item.get("message"),
                            location_field=location,
                            trigger_value=(item.get("trigger") or {}).get("stringValue"),
                            docs_url=_docs_url(api_version, parsed_code[0]),
                        )
                    )
        return cls(
            http_status=http_status,
            api_version=api_version,
            request_id=request_id,
            status=status,
            message=message,
            errors=errors,
            raw=body,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "http_status": self.http_status,
            "api_version": self.api_version,
            "request_id": self.request_id,
            "status": self.status,
            "message": self.message,
            "errors": [e.to_dict() for e in self.errors],
        }

    def has_code(self, category: str, value: Optional[str] = None) -> bool:
        for err in self.errors:
            if err.error_code_category != category:
                continue
            if value is None or err.error_code_value == value:
                return True
        return False


def _parse_error_code(error_code: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(error_code, dict) or not error_code:
        return (None, None)
    for category, value in error_code.items():
        if value is None:
            continue
        return (category, str(value))
    return (None, None)


def _parse_location(location: Any) -> Optional[str]:
    if not isinstance(location, dict):
        return None
    field_paths = location.get("fieldPathElements") or []
    parts: list[str] = []
    for element in field_paths:
        if not isinstance(element, dict):
            continue
        name = element.get("fieldName")
        index = element.get("index")
        if name is None:
            continue
        if index is None:
            parts.append(str(name))
        else:
            parts.append(f"{name}[{index}]")
    return ".".join(parts) if parts else None


def _docs_url(api_version: str, category: Optional[str]) -> Optional[str]:
    if not category:
        return None
    enum_path = _ERROR_ENUM_MAP.get(category)
    if not enum_path:
        return None
    return f"{_DOCS_ROOT}/{api_version}/{enum_path}"


def _build_summary(
    *,
    http_status: int,
    request_id: Optional[str],
    status: Optional[str],
    message: Optional[str],
    errors: list[GoogleAdsErrorItem],
) -> str:
    parts: list[str] = [f"Google Ads API {http_status}"]
    if request_id:
        parts.append(f"request_id={request_id}")
    if status:
        parts.append(f"status={status}")
    if message:
        parts.append(f"message={message[:200]}")
    if errors:
        codes = ", ".join(
            f"{e.error_code_category or 'unknown'}:{e.error_code_value or '?'}"
            for e in errors[:5]
        )
        parts.append(f"codes=[{codes}]")
    return "; ".join(parts)


def _safe_load_json(body: str) -> Any:
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {}
