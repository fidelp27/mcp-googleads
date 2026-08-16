"""Customer Match membership management.

Backs the OfflineUserDataJobService pipeline: create a job, add member
operations, run the job. Handles SHA-256 hashing of contact info before
transport and enforces explicit per-call consent — Customer Match data
never leaves the process without both.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, request, search
from .config import (
    client_display_name,
    ensure_customer_allowed,
    ensure_tool_allowed,
    get_settings,
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
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_CONSENT_VALUES = frozenset({"GRANTED", "DENIED", "UNSPECIFIED"})
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_PHONE_STRIP_RE = re.compile(r"[\s\-().]")
_MAX_MEMBERS_PER_CALL = 100_000
_ADD_OPS_BATCH_SIZE = 10_000


def _looks_hashed(value: str) -> bool:
    return bool(_HASH_RE.fullmatch(value.strip().lower()))


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    """Google-Ads spec normalization: trim + lowercase + strip Gmail dots."""
    normalized = email.strip().lower()
    if "@" not in normalized:
        raise ValueError(f"'{email}' is not a valid email address")
    local, _, domain = normalized.partition("@")
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def _normalize_phone(phone: str) -> str:
    """Google-Ads spec: E.164 (with leading +), digits only."""
    cleaned = _PHONE_STRIP_RE.sub("", phone.strip())
    if not cleaned:
        raise ValueError("phone must contain digits")
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned.lstrip("+")
    digits = cleaned[1:]
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        raise ValueError(
            f"phone '{phone}' must be in E.164 format (leading + and 6-15 digits)"
        )
    return cleaned


def _hash_email(value: str) -> str:
    if _looks_hashed(value):
        return value.strip().lower()
    return _sha256_hex(_normalize_email(value))


def _hash_phone(value: str) -> str:
    if _looks_hashed(value):
        return value.strip().lower()
    return _sha256_hex(_normalize_phone(value))


def _build_user_identifiers(member: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one member dict into 1+ userIdentifier payload entries."""
    identifiers: list[dict[str, Any]] = []
    if "email" in member and member["email"]:
        identifiers.append({"hashedEmail": _hash_email(str(member["email"]))})
    if "hashed_email" in member and member["hashed_email"]:
        identifiers.append(
            {"hashedEmail": _hash_email(str(member["hashed_email"]))}
        )
    if "phone" in member and member["phone"]:
        identifiers.append({"hashedPhoneNumber": _hash_phone(str(member["phone"]))})
    if "hashed_phone" in member and member["hashed_phone"]:
        identifiers.append(
            {"hashedPhoneNumber": _hash_phone(str(member["hashed_phone"]))}
        )
    if "mobile_id" in member and member["mobile_id"]:
        identifiers.append({"mobileId": str(member["mobile_id"]).strip()})
    if "third_party_user_id" in member and member["third_party_user_id"]:
        identifiers.append(
            {"thirdPartyUserId": str(member["third_party_user_id"]).strip()}
        )
    if "address_info" in member and isinstance(member["address_info"], dict):
        addr = member["address_info"]
        first = addr.get("first_name")
        last = addr.get("last_name")
        country = addr.get("country_code")
        postal = addr.get("postal_code")
        if not (first and last and country and postal):
            raise ValueError(
                "address_info requires first_name, last_name, country_code, postal_code"
            )
        identifiers.append(
            {
                "addressInfo": {
                    "hashedFirstName": _sha256_hex(str(first).strip().lower()),
                    "hashedLastName": _sha256_hex(str(last).strip().lower()),
                    "countryCode": str(country).strip().upper(),
                    "postalCode": str(postal).strip(),
                }
            }
        )
    if not identifiers:
        raise ValueError(
            "member requires at least one of: email, hashed_email, phone, "
            "hashed_phone, mobile_id, third_party_user_id, address_info"
        )
    return identifiers


def _normalize_consent(consent: dict[str, Any] | None) -> dict[str, str]:
    if not consent or not isinstance(consent, dict):
        raise PermissionError(
            "consent is required; pass {'ad_user_data': GRANTED|DENIED, "
            "'ad_personalization': GRANTED|DENIED}"
        )
    aud = str(consent.get("ad_user_data", "")).strip().upper()
    pers = str(consent.get("ad_personalization", "")).strip().upper()
    if aud not in _CONSENT_VALUES or pers not in _CONSENT_VALUES:
        raise ValueError(
            f"consent values must be in {sorted(_CONSENT_VALUES)}; got {consent}"
        )
    if aud == "UNSPECIFIED" or pers == "UNSPECIFIED":
        raise PermissionError(
            "consent UNSPECIFIED is not accepted here; require an explicit "
            "GRANTED or DENIED from the data source."
        )
    return {"adUserData": aud, "adPersonalization": pers}


def _build_operations(
    members: list[dict[str, Any]], op_type: str, consent: dict[str, str]
) -> list[dict[str, Any]]:
    if not isinstance(members, list) or not members:
        raise ValueError("members must be a non-empty list")
    if len(members) > _MAX_MEMBERS_PER_CALL:
        raise ValueError(
            f"members is limited to {_MAX_MEMBERS_PER_CALL} entries per call"
        )
    ops: list[dict[str, Any]] = []
    for idx, member in enumerate(members):
        if not isinstance(member, dict):
            raise ValueError(f"members[{idx}] must be a dict")
        identifiers = _build_user_identifiers(member)
        ops.append(
            {
                op_type: {
                    "userIdentifiers": identifiers,
                    "consent": consent,
                }
            }
        )
    return ops


def _chunks(items: list[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _user_list_resource(customer_id: str, user_list_id: str) -> str:
    if not str(user_list_id).isdigit() or int(user_list_id) <= 0:
        raise ValueError("user_list_id must be a positive integer")
    return f"customers/{customer_id}/userLists/{int(user_list_id)}"


def _run_job_pipeline(
    *,
    customer_id: str,
    user_list_id: str,
    operations: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    """Create OfflineUserDataJob, add ops in chunks, run. Returns the job resource + summary."""
    if dry_run:
        # Nothing to do server-side for dry-run: we validate members locally by
        # the fact that _build_operations already ran. The job would be a real
        # POST create; we skip it to avoid leaving orphan jobs on the account.
        return {
            "dry_run": True,
            "operations_count": len(operations),
            "batches": (len(operations) + _ADD_OPS_BATCH_SIZE - 1) // _ADD_OPS_BATCH_SIZE,
            "user_list": _user_list_resource(customer_id, user_list_id),
        }

    create_body = {
        "job": {
            "type": "CUSTOMER_MATCH_USER_LIST",
            "customerMatchUserListMetadata": {
                "userList": _user_list_resource(customer_id, user_list_id),
                "consent": {
                    "adUserData": operations[0][
                        next(iter(operations[0]))
                    ]["consent"]["adUserData"],
                    "adPersonalization": operations[0][
                        next(iter(operations[0]))
                    ]["consent"]["adPersonalization"],
                },
            },
        }
    }
    created = request(
        "POST",
        f"customers/{customer_id}/offlineUserDataJobs:create",
        customer_id=customer_id,
        body=create_body,
    )
    job_resource = created.get("resourceName")
    if not job_resource:
        raise RuntimeError(
            f"createJob did not return a resourceName; got {created}"
        )

    added_batches = []
    for batch in _chunks(operations, _ADD_OPS_BATCH_SIZE):
        add_resp = request(
            "POST",
            f"{job_resource}:addOperations",
            customer_id=customer_id,
            body={"operations": batch, "enablePartialFailure": True},
        )
        added_batches.append(
            {
                "batch_size": len(batch),
                "partial_failure_error": add_resp.get("partialFailureError"),
            }
        )

    run_resp = request(
        "POST",
        f"{job_resource}:run",
        customer_id=customer_id,
        body={},
    )
    return {
        "job_resource_name": job_resource,
        "run_response": run_resp,
        "batches": added_batches,
        "operations_count": len(operations),
    }


def register_customer_match_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_customer_match_members(
        customer_id: str = Field(description="Google Ads customer ID"),
        user_list_id: str = Field(description="Customer Match user list ID"),
        members: list[dict[str, Any]] = Field(
            description=(
                "List of members. Each member is a dict with one or more of: "
                "email, hashed_email, phone, hashed_phone, mobile_id, "
                "third_party_user_id, address_info={first_name,last_name,country_code,postal_code}. "
                "Plain email/phone are SHA-256 hashed before transport; already-hashed values "
                "are passed through unchanged."
            )
        ),
        consent: dict[str, str] = Field(
            description=(
                "Explicit per-call consent: "
                "{'ad_user_data': GRANTED|DENIED, 'ad_personalization': GRANTED|DENIED}. "
                "UNSPECIFIED is rejected."
            )
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Add members to a Customer Match list via OfflineUserDataJobService.

        Contact info is hashed (SHA-256, hex, lowercase) client-side; only
        hashes leave the process. `consent` is required on every call and
        UNSPECIFIED values are rejected. The pipeline is: create job -> add
        operations in 10k batches -> run. Returns the job resource name so
        callers can poll status with `get_offline_user_data_job_status`.
        """
        ensure_tool_allowed("add_customer_match_members", write=True)
        cid = ensure_customer_allowed(customer_id)
        normalized_consent = _normalize_consent(consent)
        operations = _build_operations(members, "create", normalized_consent)
        return _dispatch_membership_operation(
            tool_name="add_customer_match_members",
            customer_id=cid,
            user_list_id=user_list_id,
            operations=operations,
            member_count=len(members),
            op_verb="add",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def remove_customer_match_members(
        customer_id: str = Field(description="Google Ads customer ID"),
        user_list_id: str = Field(description="Customer Match user list ID"),
        members: list[dict[str, Any]] = Field(
            description="Same member format as add_customer_match_members."
        ),
        consent: dict[str, str] = Field(
            description="Same consent format as add_customer_match_members."
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Remove members from a Customer Match list via OfflineUserDataJobService."""
        ensure_tool_allowed("remove_customer_match_members", write=True)
        cid = ensure_customer_allowed(customer_id)
        normalized_consent = _normalize_consent(consent)
        operations = _build_operations(members, "remove", normalized_consent)
        return _dispatch_membership_operation(
            tool_name="remove_customer_match_members",
            customer_id=cid,
            user_list_id=user_list_id,
            operations=operations,
            member_count=len(members),
            op_verb="remove",
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_customer_match_list_stats(
        customer_id: str = Field(description="Google Ads customer ID"),
        user_list_id: str = Field(description="Customer Match user list ID"),
    ) -> str:
        """Return size, membership status and eligibility for a Customer Match list."""
        ensure_tool_allowed("get_customer_match_list_stats")
        cid = ensure_customer_allowed(customer_id)
        resource = _user_list_resource(cid, user_list_id)
        rows = search(
            cid,
            f"""
            SELECT user_list.id, user_list.name, user_list.type,
                   user_list.membership_status, user_list.membership_life_span,
                   user_list.size_for_display, user_list.size_for_search,
                   user_list.size_range_for_display, user_list.size_range_for_search,
                   user_list.eligible_for_display, user_list.eligible_for_search,
                   user_list.description
            FROM user_list
            WHERE user_list.resource_name = '{resource}'
            """,
        )
        if not rows:
            return format_json(
                {
                    "customer_id": cid,
                    "tool": "get_customer_match_list_stats",
                    "fresh": True,
                    "error": f"user_list {user_list_id} not found",
                }
            )
        return format_json(
            {
                "customer_id": cid,
                "tool": "get_customer_match_list_stats",
                "fresh": True,
                "user_list": rows[0].get("userList", {}),
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_offline_user_data_job_status(
        customer_id: str = Field(description="Google Ads customer ID"),
        job_id: str = Field(description="OfflineUserDataJob ID"),
    ) -> str:
        """Return the current status of an OfflineUserDataJob (PENDING/RUNNING/SUCCESS/FAILED)."""
        ensure_tool_allowed("get_offline_user_data_job_status")
        cid = ensure_customer_allowed(customer_id)
        if not str(job_id).isdigit() or int(job_id) <= 0:
            raise ValueError("job_id must be a positive integer")
        resource = f"customers/{cid}/offlineUserDataJobs/{int(job_id)}"
        rows = search(
            cid,
            f"""
            SELECT offline_user_data_job.id, offline_user_data_job.type,
                   offline_user_data_job.status, offline_user_data_job.failure_reason
            FROM offline_user_data_job
            WHERE offline_user_data_job.resource_name = '{resource}'
            """,
        )
        if not rows:
            return format_json(
                {
                    "customer_id": cid,
                    "tool": "get_offline_user_data_job_status",
                    "fresh": True,
                    "error": f"offline_user_data_job {job_id} not found",
                }
            )
        return format_json(
            {
                "customer_id": cid,
                "tool": "get_offline_user_data_job_status",
                "fresh": True,
                "job": rows[0].get("offlineUserDataJob", {}),
            }
        )


def _dispatch_membership_operation(
    *,
    tool_name: str,
    customer_id: str,
    user_list_id: str,
    operations: list[dict[str, Any]],
    member_count: int,
    op_verb: str,
    dry_run: bool,
    approval_id: Optional[str],
    confirmation_text: Optional[str],
) -> str:
    settings = get_settings()
    if settings.read_only_mode and not dry_run:
        raise PermissionError("READ_ONLY_MODE=true blocks Customer Match execution")

    payload = {
        "user_list_id": str(user_list_id),
        "member_count": member_count,
        "operation": op_verb,
    }
    expected_confirmation = f"{op_verb.upper()} {member_count} MEMBERS"

    if dry_run:
        preview_summary = _run_job_pipeline(
            customer_id=customer_id,
            user_list_id=user_list_id,
            operations=operations,
            dry_run=True,
        )
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
            preview_summary,
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
                "preview": preview_summary,
                "next_step": (
                    f"Call {tool_name} again with the same arguments, "
                    f"dry_run=false, this approval_id and "
                    f"confirmation_text='{expected_confirmation}'."
                ),
            }
        )

    if not approval_id:
        raise PermissionError("approval_id is required when dry_run=false")
    approval = validate_approval(
        approval_id,
        tool_name,
        customer_id,
        payload,
        confirmation_text,
    )
    try:
        result = _run_job_pipeline(
            customer_id=customer_id,
            user_list_id=user_list_id,
            operations=operations,
            dry_run=False,
        )
        audit_id = record_audit(
            tool_name,
            customer_id,
            payload,
            "executed",
            result,
            dry_run=False,
            operation_id=approval_id,
        )
        update_approval(approval_id, status="executed", audit_id=audit_id)
        return format_json(
            {
                "status": "executed",
                "executed": True,
                "approval_id": approval_id,
                "audit_id": audit_id,
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "result": result,
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
