"""Priority guarded write operations for Phase 4 (module `operations`, hidden in READ_ONLY_MODE).

All mutations flow through `agency.mutations.execute_mutation` with:
- mandatory `dry_run=True` (`validateOnly=True`) preview & diff,
- exact confirmation text for high-impact changes,
- optimistic locking (`state_verifier`) to reject stale approvals when live state drifted,
- captured `previous_state` for supported reversions.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import search
from .config import ensure_customer_allowed
from .governance import audited_tool
from .mutations import execute_mutation


WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

_MAX_BATCH_ITEMS = 100
_VALID_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})
_VALID_PLACEMENT_LEVELS = frozenset({"CAMPAIGN", "ACCOUNT"})
_VALID_PLACEMENT_TYPES = frozenset({"PLACEMENT", "MOBILE_APPLICATION"})


def _pos_int(value: str | int, field: str) -> str:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return text


def _enum_token(value: str, field: str) -> str:
    clean = (value or "").strip().upper()
    if not clean or not clean.replace("_", "").isalnum():
        raise ValueError(f"{field} must be a valid uppercase enum token")
    return clean


def register_operations_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_campaign_conversion_goal(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        campaign_id: Annotated[str, Field(description="Target campaign ID")],
        category: Annotated[str, Field(description="Conversion action category enum (e.g. PURCHASE, LEAD)")],
        origin: Annotated[str, Field(description="Conversion origin enum (e.g. WEBSITE, GOOGLE_ANALYTICS_4)")],
        biddable: Annotated[bool, Field(description="True to optimize campaign bids for this goal; False to exclude")],
        dry_run: Annotated[bool, Field(description="True for validateOnly preview; False to execute with approval_id")] = True,
        approval_id: Annotated[Optional[str], Field(description="Approval ID from dry_run=true")] = None,
        confirmation_text: Annotated[Optional[str], Field(description="Exact confirmation string returned by preview")] = None,
    ) -> str:
        """Preview or toggle a campaign-level conversion goal (`biddable`) with optimistic locking."""
        cid = ensure_customer_allowed(customer_id)
        camp_id = _pos_int(campaign_id, "campaign_id")
        cat = _enum_token(category, "category")
        orig = _enum_token(origin, "origin")
        resource_name = f"customers/{cid}/campaignConversionGoals/{camp_id}~{cat}~{orig}"

        def fetch_live() -> dict[str, Any]:
            rows = search(
                cid,
                f"""
                SELECT campaign_conversion_goal.resource_name,
                       campaign_conversion_goal.category,
                       campaign_conversion_goal.origin,
                       campaign_conversion_goal.biddable
                FROM campaign_conversion_goal
                WHERE campaign_conversion_goal.resource_name = '{resource_name}'
                """,
            )
            if not rows:
                raise ValueError(f"Campaign conversion goal not found: {resource_name}")
            goal = rows[0].get("campaignConversionGoal") or {}
            return {
                "resourceName": goal.get("resourceName") or resource_name,
                "category": goal.get("category") or cat,
                "origin": goal.get("origin") or orig,
                "biddable": bool(goal.get("biddable")),
            }

        previous_state = fetch_live() if dry_run else None
        operations = [
            {
                "update": {
                    "resourceName": resource_name,
                    "biddable": bool(biddable),
                },
                "updateMask": "biddable",
            }
        ]
        expected_confirmation = f"{camp_id}:{cat}:{orig}:biddable={str(bool(biddable)).lower()}"
        return execute_mutation(
            tool_name="update_campaign_conversion_goal",
            customer_id=cid,
            service="campaignConversionGoals",
            operations=operations,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            expected_confirmation=expected_confirmation,
            previous_state=previous_state,
            state_verifier=fetch_live,
            stale_state_fields=("biddable",),
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def set_conversion_action_primary(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        conversion_action_id: Annotated[str, Field(description="Conversion action ID")],
        primary_for_goal: Annotated[bool, Field(description="True for primary (biddable); False for secondary (observation)")],
        dry_run: Annotated[bool, Field(description="True for validateOnly preview; False to execute with approval_id")] = True,
        approval_id: Annotated[Optional[str], Field(description="Approval ID from dry_run=true")] = None,
        confirmation_text: Annotated[Optional[str], Field(description="Exact conversion action name required for confirmation")] = None,
    ) -> str:
        """Preview or update `primary_for_goal` on a conversion action with exact-name confirmation and optimistic locking."""
        cid = ensure_customer_allowed(customer_id)
        action_id = _pos_int(conversion_action_id, "conversion_action_id")
        resource_name = f"customers/{cid}/conversionActions/{action_id}"

        def fetch_live() -> dict[str, Any]:
            rows = search(
                cid,
                f"""
                SELECT conversion_action.resource_name,
                       conversion_action.name,
                       conversion_action.status,
                       conversion_action.category,
                       conversion_action.primary_for_goal
                FROM conversion_action
                WHERE conversion_action.resource_name = '{resource_name}'
                """,
            )
            if not rows:
                raise ValueError(f"Conversion action not found: {resource_name}")
            act = rows[0].get("conversionAction") or {}
            return {
                "resourceName": act.get("resourceName") or resource_name,
                "name": act.get("name") or action_id,
                "status": act.get("status"),
                "category": act.get("category"),
                "primaryForGoal": bool(act.get("primaryForGoal")),
            }

        live = fetch_live()
        operations = [
            {
                "update": {
                    "resourceName": resource_name,
                    "primaryForGoal": bool(primary_for_goal),
                },
                "updateMask": "primary_for_goal",
            }
        ]
        return execute_mutation(
            tool_name="set_conversion_action_primary",
            customer_id=cid,
            service="conversionActions",
            operations=operations,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            expected_confirmation=str(live["name"]),
            previous_state=live if dry_run else None,
            state_verifier=fetch_live,
            stale_state_fields=("primaryForGoal", "status"),
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def exclude_placements(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        placements: Annotated[list[str], Field(description="List of placement URLs or mobile app IDs to exclude (max 100)")],
        level: Annotated[str, Field(description="Exclusion scope: 'CAMPAIGN' or 'ACCOUNT'")] = "CAMPAIGN",
        campaign_id: Annotated[Optional[str], Field(description="Required when level='CAMPAIGN'")] = None,
        placement_type: Annotated[str, Field(description="'PLACEMENT' (websites/URLs) or 'MOBILE_APPLICATION' (app IDs)")] = "PLACEMENT",
        dry_run: Annotated[bool, Field(description="True for validateOnly preview; False to execute with approval_id")] = True,
        approval_id: Annotated[Optional[str], Field(description="Approval ID from dry_run=true")] = None,
        confirmation_text: Annotated[Optional[str], Field(description="Exact confirmation string when requested")] = None,
    ) -> str:
        """Exclude websites or mobile apps at campaign or account level (Display, PMax, Demand Gen brand safety)."""
        cid = ensure_customer_allowed(customer_id)
        scope = (level or "").strip().upper()
        if scope not in _VALID_PLACEMENT_LEVELS:
            raise ValueError(f"level must be one of {sorted(_VALID_PLACEMENT_LEVELS)}")
        ptype = (placement_type or "").strip().upper()
        if ptype not in _VALID_PLACEMENT_TYPES:
            raise ValueError(f"placement_type must be one of {sorted(_VALID_PLACEMENT_TYPES)}")
        cleaned = list(dict.fromkeys(str(item).strip() for item in placements if str(item).strip()))
        if not cleaned:
            raise ValueError("placements must contain at least one non-empty value")
        if len(cleaned) > _MAX_BATCH_ITEMS:
            raise ValueError(f"placements is limited to {_MAX_BATCH_ITEMS} entries per call")

        operations: list[dict[str, Any]] = []
        if scope == "CAMPAIGN":
            camp_id = _pos_int(campaign_id or "", "campaign_id")
            camp_resource = f"customers/{cid}/campaigns/{camp_id}"
            service = "campaignCriteria"
            for item in cleaned:
                criterion: dict[str, Any] = {"campaign": camp_resource, "negative": True}
                if ptype == "MOBILE_APPLICATION":
                    criterion["mobileApplication"] = {"appId": item}
                else:
                    criterion["placement"] = {"url": item}
                operations.append({"create": criterion})
            expected_confirmation = f"EXCLUDE {len(cleaned)} {ptype} CAMPAIGN {camp_id}"
        else:
            service = "customerNegativeCriteria"
            for item in cleaned:
                criterion = {}
                if ptype == "MOBILE_APPLICATION":
                    criterion["mobileApplication"] = {"appId": item}
                else:
                    criterion["placement"] = {"url": item}
                operations.append({"create": criterion})
            expected_confirmation = f"EXCLUDE {len(cleaned)} {ptype} ACCOUNT {cid}"

        return execute_mutation(
            tool_name="exclude_placements",
            customer_id=cid,
            service=service,
            operations=operations,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            expected_confirmation=expected_confirmation,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_account_negative_keywords(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        keywords: Annotated[list[str], Field(description="Account-level negative keywords to add (max 100)")],
        match_type: Annotated[str, Field(description="Match type: 'EXACT', 'PHRASE', or 'BROAD'")] = "EXACT",
        dry_run: Annotated[bool, Field(description="True for validateOnly preview; False to execute with approval_id")] = True,
        approval_id: Annotated[Optional[str], Field(description="Approval ID from dry_run=true")] = None,
        confirmation_text: Annotated[Optional[str], Field(description="Exact confirmation string returned by preview")] = None,
    ) -> str:
        """Add account-level negative keywords (`customerNegativeCriteria`) applying across Search, Shopping, and PMax."""
        cid = ensure_customer_allowed(customer_id)
        mt = (match_type or "").strip().upper()
        if mt not in _VALID_MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(_VALID_MATCH_TYPES)}")
        cleaned = list(dict.fromkeys(str(kw).strip() for kw in keywords if str(kw).strip()))
        if not cleaned:
            raise ValueError("keywords must contain at least one non-empty string")
        if len(cleaned) > _MAX_BATCH_ITEMS:
            raise ValueError(f"keywords is limited to {_MAX_BATCH_ITEMS} entries per call")

        operations = [
            {"create": {"negativeKeyword": {"text": kw, "matchType": mt}}}
            for kw in cleaned
        ]
        return execute_mutation(
            tool_name="add_account_negative_keywords",
            customer_id=cid,
            service="customerNegativeCriteria",
            operations=operations,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            expected_confirmation=f"ACCOUNT NEGATIVES {len(cleaned)} {mt} {cid}",
        )
