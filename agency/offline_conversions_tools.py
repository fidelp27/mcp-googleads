"""Offline conversion uploads for Google Ads Smart Bidding.

Three tools that cover the `ConversionUploadService` +
`ConversionAdjustmentUploadService` surface:

- upload_click_conversions
- upload_call_conversions
- upload_conversion_adjustments

Contact info in Enhanced Conversions for Leads payloads is hashed
SHA-256 client-side (helpers shared with customer_match_tools). Consent
is mandatory on every call for click / call uploads and rejected as
UNSPECIFIED to prevent silent Smart Bidding degradation.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, request
from .config import (
    client_display_name,
    ensure_customer_allowed,
    ensure_tool_allowed,
    get_settings,
)
from .customer_match_tools import (
    _build_user_identifiers,
    _normalize_consent,
)
from .governance import (
    audited_tool,
    create_approval,
    record_audit,
    update_approval,
    validate_approval,
)


WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

_MAX_CONVERSIONS_PER_CALL = 2000
_CLICK_ID_FIELDS = ("gclid", "gbraid", "wbraid")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}([+-]\d{2}:?\d{2}|Z)?$"
)


def _positive_int(value, field: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(text)


def _conversion_action_resource(customer_id: str, action_id: str) -> str:
    return (
        f"customers/{customer_id}/conversionActions/"
        f"{_positive_int(action_id, 'conversion_action_id')}"
    )


def _validate_datetime(value: str, field: str) -> str:
    text = str(value).strip()
    if not _DATETIME_RE.match(text):
        raise ValueError(
            f"{field} must be 'YYYY-MM-DD HH:MM:SS' with optional timezone offset "
            f"(±HH:MM or Z); got {value!r}"
        )
    return text


def _validate_currency(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    clean = str(code).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", clean):
        raise ValueError(f"currency_code must be a 3-letter ISO code (got {code!r})")
    return clean


def _optional_positive_float(value, field: str) -> Optional[float]:
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if val < 0:
        raise ValueError(f"{field} must be non-negative")
    return val


def _build_click_conversion(customer_id: str, record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("each click conversion must be a dict")
    action_id = record.get("conversion_action_id")
    if not action_id:
        raise ValueError("conversion_action_id is required")
    entry: dict[str, Any] = {
        "conversionAction": _conversion_action_resource(customer_id, action_id),
        "conversionDateTime": _validate_datetime(
            record.get("conversion_date_time"), "conversion_date_time"
        ),
    }
    value = _optional_positive_float(record.get("conversion_value"), "conversion_value")
    if value is not None:
        entry["conversionValue"] = value
    currency = _validate_currency(record.get("currency_code"))
    if currency:
        entry["currencyCode"] = currency

    click_id_used = False
    for click_field in _CLICK_ID_FIELDS:
        if record.get(click_field):
            entry[click_field] = str(record[click_field]).strip()
            click_id_used = True

    identifiers_source = record.get("user_identifiers")
    identifiers: list[dict[str, Any]] = []
    if identifiers_source:
        if not isinstance(identifiers_source, list):
            raise ValueError("user_identifiers must be a list of member dicts")
        for member in identifiers_source:
            identifiers.extend(_build_user_identifiers(member))
        entry["userIdentifiers"] = identifiers

    if not click_id_used and not identifiers:
        raise ValueError(
            "each click conversion requires a click ID (gclid/gbraid/wbraid) "
            "or user_identifiers for Enhanced Conversions for Leads"
        )
    consent = record.get("consent")
    entry["consent"] = _normalize_consent(consent)

    order_id = record.get("order_id")
    if order_id:
        entry["orderId"] = str(order_id)
    custom_vars = record.get("custom_variables")
    if custom_vars:
        if not isinstance(custom_vars, list):
            raise ValueError("custom_variables must be a list of dicts")
        entry["customVariables"] = [
            {
                "conversionCustomVariable": (
                    f"customers/{customer_id}/conversionCustomVariables/"
                    f"{_positive_int(cv.get('conversion_custom_variable_id'), 'conversion_custom_variable_id')}"
                ),
                "value": str(cv.get("value", "")),
            }
            for cv in custom_vars
        ]
    return entry


def _build_call_conversion(customer_id: str, record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("each call conversion must be a dict")
    action_id = record.get("conversion_action_id")
    if not action_id:
        raise ValueError("conversion_action_id is required")
    caller_id = record.get("caller_id")
    if not caller_id:
        raise ValueError("caller_id is required (E.164, will not be hashed by the API)")
    entry: dict[str, Any] = {
        "conversionAction": _conversion_action_resource(customer_id, action_id),
        "callerId": str(caller_id).strip(),
        "callStartDateTime": _validate_datetime(
            record.get("call_start_date_time"), "call_start_date_time"
        ),
        "conversionDateTime": _validate_datetime(
            record.get("conversion_date_time"), "conversion_date_time"
        ),
    }
    value = _optional_positive_float(record.get("conversion_value"), "conversion_value")
    if value is not None:
        entry["conversionValue"] = value
    currency = _validate_currency(record.get("currency_code"))
    if currency:
        entry["currencyCode"] = currency
    entry["consent"] = _normalize_consent(record.get("consent"))
    return entry


def _build_conversion_adjustment(
    customer_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("each adjustment must be a dict")
    action_id = record.get("conversion_action_id")
    if not action_id:
        raise ValueError("conversion_action_id is required")
    adjustment_type = str(record.get("adjustment_type", "")).strip().upper()
    if adjustment_type not in {"RETRACTION", "RESTATEMENT", "ENHANCEMENT"}:
        raise ValueError(
            "adjustment_type must be RETRACTION, RESTATEMENT or ENHANCEMENT"
        )
    entry: dict[str, Any] = {
        "conversionAction": _conversion_action_resource(customer_id, action_id),
        "adjustmentDateTime": _validate_datetime(
            record.get("adjustment_date_time"), "adjustment_date_time"
        ),
        "adjustmentType": adjustment_type,
    }
    order_id = record.get("order_id")
    gclid_time = record.get("gclid_date_time_pair")
    if order_id:
        entry["orderId"] = str(order_id)
    elif gclid_time and isinstance(gclid_time, dict):
        entry["gclidDateTimePair"] = {
            "gclid": str(gclid_time["gclid"]).strip(),
            "conversionDateTime": _validate_datetime(
                gclid_time.get("conversion_date_time"),
                "gclid_date_time_pair.conversion_date_time",
            ),
        }
    else:
        raise ValueError(
            "adjustment must include order_id OR gclid_date_time_pair={'gclid','conversion_date_time'}"
        )
    if adjustment_type == "RESTATEMENT":
        value = _optional_positive_float(
            (record.get("restatement_value") or {}).get("adjusted_value"),
            "restatement_value.adjusted_value",
        )
        if value is None:
            raise ValueError(
                "RESTATEMENT requires restatement_value.adjusted_value"
            )
        currency = _validate_currency(
            (record.get("restatement_value") or {}).get("currency_code")
        )
        payload = {"adjustedValue": value}
        if currency:
            payload["currencyCode"] = currency
        entry["restatementValue"] = payload
    if adjustment_type == "ENHANCEMENT":
        identifiers_source = record.get("user_identifiers")
        if not identifiers_source:
            raise ValueError(
                "ENHANCEMENT requires user_identifiers (email/phone/address_info)"
            )
        identifiers: list[dict[str, Any]] = []
        for member in identifiers_source:
            identifiers.extend(_build_user_identifiers(member))
        entry["userIdentifiers"] = identifiers
    return entry


def _build_operations(
    customer_id: str,
    records: list[dict[str, Any]],
    builder,
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty list")
    if len(records) > _MAX_CONVERSIONS_PER_CALL:
        raise ValueError(
            f"batch is limited to {_MAX_CONVERSIONS_PER_CALL} records per call"
        )
    return [builder(customer_id, r) for r in records]


def register_offline_conversions_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def upload_click_conversions(
        customer_id: str = Field(description="Google Ads customer ID"),
        conversions: list[dict[str, Any]] = Field(
            description=(
                "List of click conversion records. Each has: "
                "conversion_action_id, conversion_date_time ('YYYY-MM-DD HH:MM:SS±HH:MM'), "
                "conversion_value?, currency_code?, one of gclid/gbraid/wbraid OR "
                "user_identifiers (Enhanced Conversions for Leads), consent, "
                "order_id?, custom_variables?."
            )
        ),
        validate_only: bool = Field(
            default=False,
            description="Ask Google to validate the batch without applying it",
        ),
        partial_failure: bool = Field(
            default=True,
            description="Return per-row errors instead of failing the batch",
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Upload click conversions to Google Ads (Smart Bidding fuel)."""
        ensure_tool_allowed("upload_click_conversions", write=True)
        cid = ensure_customer_allowed(customer_id)
        operations = _build_operations(cid, conversions, _build_click_conversion)
        return _dispatch_upload(
            tool_name="upload_click_conversions",
            customer_id=cid,
            endpoint=f"customers/{cid}:uploadClickConversions",
            body={
                "conversions": operations,
                "partialFailure": bool(partial_failure),
                "validateOnly": bool(validate_only),
            },
            payload={
                "operation": "upload_click_conversions",
                "record_count": len(conversions),
                "validate_only": bool(validate_only),
            },
            record_count=len(conversions),
            verb="UPLOAD",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def upload_call_conversions(
        customer_id: str = Field(description="Google Ads customer ID"),
        conversions: list[dict[str, Any]] = Field(
            description=(
                "List of call conversion records. Each has: "
                "conversion_action_id, caller_id (E.164), "
                "call_start_date_time, conversion_date_time, "
                "conversion_value?, currency_code?, consent."
            )
        ),
        validate_only: bool = Field(default=False),
        partial_failure: bool = Field(default=True),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Upload call conversions to Google Ads."""
        ensure_tool_allowed("upload_call_conversions", write=True)
        cid = ensure_customer_allowed(customer_id)
        operations = _build_operations(cid, conversions, _build_call_conversion)
        return _dispatch_upload(
            tool_name="upload_call_conversions",
            customer_id=cid,
            endpoint=f"customers/{cid}:uploadCallConversions",
            body={
                "callConversions": operations,
                "partialFailure": bool(partial_failure),
                "validateOnly": bool(validate_only),
            },
            payload={
                "operation": "upload_call_conversions",
                "record_count": len(conversions),
                "validate_only": bool(validate_only),
            },
            record_count=len(conversions),
            verb="UPLOAD",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def upload_conversion_adjustments(
        customer_id: str = Field(description="Google Ads customer ID"),
        adjustments: list[dict[str, Any]] = Field(
            description=(
                "List of adjustments. Each has: conversion_action_id, "
                "adjustment_date_time, adjustment_type "
                "(RETRACTION|RESTATEMENT|ENHANCEMENT), and either order_id "
                "OR gclid_date_time_pair={'gclid','conversion_date_time'}. "
                "RESTATEMENT requires restatement_value={'adjusted_value','currency_code?'}; "
                "ENHANCEMENT requires user_identifiers."
            )
        ),
        validate_only: bool = Field(default=False),
        partial_failure: bool = Field(default=True),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Upload retroactive value adjustments, retractions or Enhanced Conversions."""
        ensure_tool_allowed("upload_conversion_adjustments", write=True)
        cid = ensure_customer_allowed(customer_id)
        operations = _build_operations(
            cid, adjustments, _build_conversion_adjustment
        )
        return _dispatch_upload(
            tool_name="upload_conversion_adjustments",
            customer_id=cid,
            endpoint=f"customers/{cid}:uploadConversionAdjustments",
            body={
                "conversionAdjustments": operations,
                "partialFailure": bool(partial_failure),
                "validateOnly": bool(validate_only),
            },
            payload={
                "operation": "upload_conversion_adjustments",
                "record_count": len(adjustments),
                "validate_only": bool(validate_only),
            },
            record_count=len(adjustments),
            verb="ADJUST",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )


def _dispatch_upload(
    *,
    tool_name: str,
    customer_id: str,
    endpoint: str,
    body: dict[str, Any],
    payload: dict[str, Any],
    record_count: int,
    verb: str,
    dry_run: bool,
    approval_id: Optional[str],
    confirmation_text: Optional[str],
) -> str:
    settings = get_settings()
    if not dry_run and settings.read_only_mode:
        raise PermissionError("READ_ONLY_MODE=true blocks offline conversion uploads")

    expected_confirmation = f"{verb} {record_count} RECORDS"
    if dry_run:
        approval = create_approval(
            tool_name,
            customer_id,
            payload,
            expected_confirmation=expected_confirmation,
        )
        record_audit(
            tool_name,
            customer_id,
            payload,
            "validated",
            {"note": "dry-run validates payload locally; no API call fired"},
            dry_run=True,
            operation_id=approval["approval_id"],
        )
        return format_json(
            {
                "status": "validated",
                "executed": False,
                "approval_id": approval["approval_id"],
                "expires_at": approval["expires_at"],
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "tool": tool_name,
                "expected_confirmation": expected_confirmation,
                "record_count": record_count,
                "next_step": (
                    f"Call {tool_name} again with the same arguments, dry_run=false, "
                    f"this approval_id and confirmation_text='{expected_confirmation}'."
                ),
            }
        )

    if not approval_id:
        raise PermissionError("approval_id is required when dry_run=false")
    validate_approval(
        approval_id,
        tool_name,
        customer_id,
        payload,
        confirmation_text,
    )
    try:
        response = request(
            "POST",
            endpoint,
            customer_id=customer_id,
            body=body,
        )
        partial_failure_error = response.get("partialFailureError")
        results = response.get("results", [])
        audit_id = record_audit(
            tool_name,
            customer_id,
            payload,
            "executed",
            {
                "results_count": len(results),
                "partial_failure_error": partial_failure_error,
            },
            dry_run=False,
            operation_id=approval_id,
        )
        update_approval(approval_id, status="executed", audit_id=audit_id)
        return format_json(
            {
                "status": "completed" if not partial_failure_error else "partial",
                "executed": True,
                "approval_id": approval_id,
                "audit_id": audit_id,
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "record_count": record_count,
                "results_count": len(results),
                "partial_failure_error": partial_failure_error,
                "results": results,
            }
        )
    except Exception as error:
        record_audit(
            tool_name,
            customer_id,
            payload,
            "error",
            str(error),
            dry_run=False,
            operation_id=approval_id,
        )
        raise
