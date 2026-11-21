"""Delivery diagnostics, budget pacing, landing pages, demographics, and asset/call performance.

Read-only tools for the `diagnostics` module (included in `leads`, `ecommerce`,
`agency`, and `full` presets):
- `diagnose_delivery`: combines account status, billing/account budgets,
  campaign serving reasons, and ad/asset/keyword policy disapprovals into
  `confirmed_causes`, `hypotheses`, and `insufficient_data`.
- `get_budget_pacing`: month-to-date spend, daily run-rate, end-of-month
  projection, and variance vs. monthly target budget in the account time zone.
- `get_landing_page_performance`: landing page URL metrics, mobile-friendly
  click rate, and speed score.
- `get_demographics_performance`: age, gender, income, and parental status
  breakdowns.
- `get_assets_and_calls_performance`: extension/asset performance & policy
  approvals (`campaign_asset`) plus call reporting (`call_view`, zero PII).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .analysis_profiles import load_profile
from .api import format_json, search, search as _api_search, search_with_meta
from .config import client_display_name, ensure_customer_allowed, ensure_tool_allowed
from .dates import account_today, coverage_metadata, resolve_date_range
from .errors import GoogleAdsError
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _search_bounded(customer_id: str, query: str) -> tuple[list[dict[str, Any]], bool]:
    """Run bounded search returning `(rows, truncated)` while preserving `diagnostics_tools.search` monkeypatches."""
    if search is not _api_search:
        return search(customer_id, query), False
    meta = search_with_meta(customer_id, query)
    return meta["rows"], bool(meta["truncated"])


def _safe_search(customer_id: str, query: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        return search(customer_id, query), None
    except GoogleAdsError as exc:
        return [], f"Optional diagnostic query unavailable ({exc.status or exc.http_status}): {exc.message or exc}"


def _safe_search_bounded(customer_id: str, query: str) -> tuple[list[dict[str, Any]], bool, str | None]:
    try:
        rows, truncated = _search_bounded(customer_id, query)
        return rows, truncated, None
    except GoogleAdsError as exc:
        return (
            [],
            False,
            f"Optional diagnostic query unavailable ({exc.status or exc.http_status}): {exc.message or exc}",
        )


def _clean_campaign_id(campaign_id: Optional[str]) -> str | None:
    if campaign_id is None:
        return None
    text = str(campaign_id).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("campaign_id must be a positive integer")
    return text


def _account_meta(customer_id: str) -> tuple[str, str | None, dict[str, Any]]:
    rows = search(
        customer_id,
        """
        SELECT customer.id, customer.descriptive_name, customer.status,
               customer.currency_code, customer.time_zone
        FROM customer
        LIMIT 1
        """,
    )
    cust = (rows[0].get("customer") or {}) if rows else {}
    currency = str(cust.get("currencyCode") or "UNKNOWN").upper()
    tz = cust.get("timeZone")
    return currency, str(tz) if tz else None, cust


# ---------- Pure summarizers ----------


def _summarize_delivery_diagnosis(
    *,
    customer_id: str,
    customer_obj: dict[str, Any],
    billing_rows: list[dict[str, Any]],
    account_budget_rows: list[dict[str, Any]],
    campaign_rows: list[dict[str, Any]],
    ad_policy_rows: list[dict[str, Any]],
    asset_policy_rows: list[dict[str, Any]],
    keyword_policy_rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    query_warnings: list[str],
    truncated_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Separate delivery findings into confirmed_causes, hypotheses, and insufficient_data."""
    confirmed: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []
    truncated_set = set(truncated_queries or [])

    # 1. Account status
    acct_status = str(customer_obj.get("status") or "UNKNOWN").upper()
    if acct_status not in {"ENABLED", "UNKNOWN"}:
        confirmed.append(
            {
                "severity": "critical",
                "code": "ACCOUNT_NOT_ENABLED",
                "scope": "account",
                "message": f"Account status is '{acct_status}'; ads cannot serve until the account is ENABLED.",
            }
        )

    # 2. Billing setup & account budgets
    approved_billing = [
        r.get("billingSetup", {})
        for r in billing_rows
        if str((r.get("billingSetup") or {}).get("status") or "").upper() == "APPROVED"
    ]
    billing_truncation_recorded = False
    if billing_rows and not approved_billing:
        statuses = sorted(
            {str((r.get("billingSetup") or {}).get("status") or "UNKNOWN") for r in billing_rows}
        )
        if "billing_setup" in truncated_set:
            billing_truncation_recorded = True
            insufficient.append(
                {
                    "code": "BILLING_SETUP_TRUNCATED",
                    "scope": "billing",
                    "message": (
                        f"Visible billing_setup rows are not APPROVED (statuses: {', '.join(statuses)}), "
                        "but the billing_setup query was truncated by GOOGLE_ADS_MAX_SEARCH_ROWS. "
                        "An APPROVED billing setup may exist on an omitted page; cannot confirm billing blockage."
                    ),
                }
            )
        else:
            confirmed.append(
                {
                    "severity": "critical",
                    "code": "NO_APPROVED_BILLING_SETUP",
                    "scope": "billing",
                    "message": f"No APPROVED billing setup found (statuses: {', '.join(statuses)}). Ads will not serve.",
                }
            )
    elif not billing_rows:
        insufficient.append(
            {
                "code": "BILLING_SETUP_UNAVAILABLE",
                "scope": "billing",
                "message": "No billing_setup rows returned (may require billing read permission or be a test account).",
            }
        )

    for row in account_budget_rows:
        ab = row.get("accountBudget", {}) or {}
        status = str(ab.get("status") or "").upper()
        limit_micros = int(ab.get("approvedSpendingLimitMicros") or 0)
        served_micros = int(ab.get("amountServedMicros") or 0)
        if status == "APPROVED" and limit_micros > 0 and served_micros >= limit_micros:
            confirmed.append(
                {
                    "severity": "critical",
                    "code": "ACCOUNT_BUDGET_EXHAUSTED",
                    "scope": "billing",
                    "message": (
                        f"Account budget '{ab.get('name') or ab.get('id')}' has spent "
                        f"{served_micros / 1_000_000:,.2f} / {limit_micros / 1_000_000:,.2f} "
                        f"{coverage['currency_code']}."
                    ),
                }
            )

    # 3. Policy disapprovals (ads, assets, keywords)
    disapproved_ads: list[dict[str, Any]] = []
    limited_ads: list[dict[str, Any]] = []
    for row in ad_policy_rows:
        aga = row.get("adGroupAd", {}) or {}
        policy = aga.get("policySummary", {}) or {}
        approval = str(policy.get("approvalStatus") or "").upper()
        if approval == "DISAPPROVED":
            topics = [
                entry.get("topic")
                for entry in (policy.get("policyTopicEntries") or [])
                if isinstance(entry, dict) and entry.get("topic")
            ]
            disapproved_ads.append(
                {
                    "campaign_id": str((row.get("campaign") or {}).get("id") or ""),
                    "campaign_name": (row.get("campaign") or {}).get("name"),
                    "ad_group_id": str((row.get("adGroup") or {}).get("id") or ""),
                    "ad_id": str((aga.get("ad") or {}).get("id") or ""),
                    "approval_status": approval,
                    "policy_topics": topics,
                }
            )
        elif approval in {"APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"}:
            limited_ads.append(
                {
                    "campaign_id": str((row.get("campaign") or {}).get("id") or ""),
                    "ad_id": str((aga.get("ad") or {}).get("id") or ""),
                    "approval_status": approval,
                }
            )

    if disapproved_ads:
        confirmed.append(
            {
                "severity": "high",
                "code": "DISAPPROVED_ADS",
                "scope": "policy",
                "count": len(disapproved_ads),
                "message": f"{len(disapproved_ads)} enabled ad(s) are DISAPPROVED by Google Ads policy.",
                "examples": disapproved_ads[:10],
            }
        )
    if limited_ads:
        hypotheses.append(
            {
                "severity": "warning",
                "code": "LIMITED_POLICY_ADS",
                "scope": "policy",
                "count": len(limited_ads),
                "message": f"{len(limited_ads)} ad(s) have restricted eligibility (APPROVED_LIMITED / AREA_OF_INTEREST_ONLY), which can cap reach.",
            }
        )

    disapproved_assets: list[dict[str, Any]] = []
    for row in asset_policy_rows:
        ca = row.get("campaignAsset", {}) or {}
        asset = row.get("asset", {}) or {}
        policy = asset.get("policySummary", {}) or {}
        approval = str(policy.get("approvalStatus") or "").upper()
        if approval == "DISAPPROVED":
            disapproved_assets.append(
                {
                    "campaign_id": str((row.get("campaign") or {}).get("id") or ""),
                    "asset_id": str(asset.get("id") or ""),
                    "asset_name": asset.get("name"),
                    "field_type": ca.get("fieldType"),
                }
            )
    if disapproved_assets:
        confirmed.append(
            {
                "severity": "warning",
                "code": "DISAPPROVED_ASSETS",
                "scope": "policy",
                "count": len(disapproved_assets),
                "message": f"{len(disapproved_assets)} linked campaign asset(s)/extension(s) are DISAPPROVED.",
                "examples": disapproved_assets[:10],
            }
        )

    disapproved_keywords: list[dict[str, Any]] = []
    for row in keyword_policy_rows:
        crit = row.get("adGroupCriterion", {}) or {}
        approval = str(crit.get("approvalStatus") or "").upper()
        if approval == "DISAPPROVED":
            disapproved_keywords.append(
                {
                    "campaign_id": str((row.get("campaign") or {}).get("id") or ""),
                    "keyword": (crit.get("keyword") or {}).get("text"),
                    "approval_status": approval,
                }
            )
    if disapproved_keywords:
        confirmed.append(
            {
                "severity": "warning",
                "code": "DISAPPROVED_KEYWORDS",
                "scope": "policy",
                "count": len(disapproved_keywords),
                "message": f"{len(disapproved_keywords)} enabled keyword(s) are DISAPPROVED.",
                "examples": disapproved_keywords[:10],
            }
        )

    # 4. Campaign serving status, budgets, rank/impression share
    campaigns_summary: list[dict[str, Any]] = []
    if not campaign_rows:
        insufficient.append(
            {
                "code": "NO_CAMPAIGNS_MATCHED",
                "scope": "campaigns",
                "message": "No non-removed campaigns matched the filter in the selected date window.",
            }
        )

    for row in campaign_rows:
        camp = row.get("campaign", {}) or {}
        budget = row.get("campaignBudget", {}) or {}
        m = row.get("metrics", {}) or {}
        cid_ = str(camp.get("id") or "")
        cname = camp.get("name") or cid_
        status = str(camp.get("status") or "").upper()
        primary_status = str(camp.get("primaryStatus") or "UNKNOWN").upper()
        reasons = [str(r) for r in (camp.get("primaryStatusReasons") or [])]
        impressions = int(m.get("impressions") or 0)
        clicks = int(m.get("clicks") or 0)
        cost_micros = int(m.get("costMicros") or 0)
        budget_micros = int(budget.get("amountMicros") or 0)
        budget_lost_is = float(m.get("searchBudgetLostImpressionShare") or 0)
        rank_lost_is = float(m.get("searchRankLostImpressionShare") or 0)

        campaigns_summary.append(
            {
                "campaign_id": cid_,
                "campaign_name": cname,
                "status": status,
                "primary_status": primary_status,
                "primary_status_reasons": reasons,
                "daily_budget_micros": budget_micros,
                "impressions": impressions,
                "clicks": clicks,
                "cost_micros": cost_micros,
                "search_budget_lost_is": budget_lost_is,
                "search_rank_lost_is": rank_lost_is,
            }
        )

        if status == "PAUSED":
            confirmed.append(
                {
                    "severity": "info",
                    "code": "CAMPAIGN_PAUSED",
                    "scope": "campaign",
                    "campaign_id": cid_,
                    "message": f"Campaign '{cname}' is PAUSED.",
                }
            )
            continue

        if primary_status in {"NOT_ELIGIBLE", "ENDED", "PENDING"}:
            confirmed.append(
                {
                    "severity": "critical",
                    "code": "CAMPAIGN_NOT_ELIGIBLE",
                    "scope": "campaign",
                    "campaign_id": cid_,
                    "message": f"Campaign '{cname}' has primary_status={primary_status} (reasons: {reasons or ['unspecified']}).",
                }
            )
        if any("BUDGET" in r for r in reasons) or budget_lost_is >= 0.2:
            confirmed.append(
                {
                    "severity": "high",
                    "code": "BUDGET_CONSTRAINED",
                    "scope": "campaign",
                    "campaign_id": cid_,
                    "message": (
                        f"Campaign '{cname}' is constrained by budget "
                        f"(reasons={reasons}, budget_lost_is={round(budget_lost_is * 100, 1)}%)."
                    ),
                }
            )
        if rank_lost_is >= 0.4:
            hypotheses.append(
                {
                    "severity": "warning",
                    "code": "HIGH_RANK_LOST_IMPRESSION_SHARE",
                    "scope": "campaign",
                    "campaign_id": cid_,
                    "message": (
                        f"Campaign '{cname}' loses {round(rank_lost_is * 100, 1)}% Search Impression Share to Ad Rank. "
                        "Inspect bids/target CPA/ROAS, Quality Score, and RSA relevance."
                    ),
                }
            )
        if status == "ENABLED" and impressions == 0 and primary_status not in {"NOT_ELIGIBLE", "ENDED"}:
            hypotheses.append(
                {
                    "severity": "high",
                    "code": "ZERO_IMPRESSIONS_ENABLED_CAMPAIGN",
                    "scope": "campaign",
                    "campaign_id": cid_,
                    "message": (
                        f"Enabled campaign '{cname}' has 0 impressions in the window. "
                        "Check geo targeting, ad schedules, negative keyword conflicts, restrictive bids, or low search volume."
                    ),
                }
            )

    for warn in query_warnings:
        insufficient.append(
            {
                "code": "DIAGNOSTIC_QUERY_UNAVAILABLE",
                "scope": "api",
                "message": warn,
            }
        )

    for qname in sorted(truncated_set):
        if qname == "billing_setup" and billing_truncation_recorded:
            continue
        insufficient.append(
            {
                "code": "DIAGNOSTIC_QUERY_TRUNCATED",
                "scope": qname,
                "message": (
                    f"Query '{qname}' reached GOOGLE_ADS_MAX_SEARCH_ROWS; absence-based conclusions "
                    "for this resource cannot be confirmed."
                ),
            }
        )

    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "account_status": acct_status,
        **coverage,
        "confirmed_causes": confirmed,
        "hypotheses": hypotheses,
        "insufficient_data": insufficient,
        "billing": {
            "approved_setups": len(approved_billing),
            "total_setups": len(billing_rows),
            "account_budgets_checked": len(account_budget_rows),
        },
        "policy": {
            "disapproved_ads": len(disapproved_ads),
            "limited_ads": len(limited_ads),
            "disapproved_assets": len(disapproved_assets),
            "disapproved_keywords": len(disapproved_keywords),
        },
        "campaigns": campaigns_summary,
        "summary": {
            "confirmed_causes_count": len(confirmed),
            "hypotheses_count": len(hypotheses),
            "insufficient_data_count": len(insufficient),
            "campaigns_evaluated": len(campaigns_summary),
        },
    }


def _summarize_budget_pacing(
    *,
    customer_id: str,
    currency_code: str,
    time_zone: str,
    today_local: date,
    mtd_cost_micros: float,
    last_7d_cost_micros: float,
    last_7d_days: int,
    active_daily_budget_micros: float,
    monthly_target_budget: float | None,
    target_source: str,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Calculate MTD spend, daily pacing rate, end-of-month projection, and status."""
    days_in_month = calendar.monthrange(today_local.year, today_local.month)[1]
    complete_days_elapsed = max(0, today_local.day - 1)
    days_remaining = days_in_month - complete_days_elapsed

    mtd_spend = mtd_cost_micros / 1_000_000
    avg_daily_mtd = (mtd_spend / complete_days_elapsed) if complete_days_elapsed > 0 else 0.0
    avg_daily_7d = (last_7d_cost_micros / 1_000_000 / last_7d_days) if last_7d_days > 0 else avg_daily_mtd

    # Project remaining days using recent 7-day pace when available, else MTD pace
    effective_daily_pace = avg_daily_7d if last_7d_days > 0 else avg_daily_mtd
    projected_eom_spend = round(mtd_spend + effective_daily_pace * days_remaining, 2)

    if monthly_target_budget is not None and monthly_target_budget > 0:
        target_monthly = float(monthly_target_budget)
        source = target_source
    else:
        target_monthly = round((active_daily_budget_micros / 1_000_000) * days_in_month, 2)
        source = "inferred_from_active_daily_budgets"

    expected_mtd_spend = round(
        target_monthly * (complete_days_elapsed / days_in_month), 2
    ) if target_monthly > 0 else 0.0
    remaining_budget = round(target_monthly - mtd_spend, 2) if target_monthly > 0 else None
    required_daily_spend = (
        round(max(0.0, remaining_budget) / days_remaining, 2)
        if remaining_budget is not None and days_remaining > 0
        else None
    )
    projected_variance_pct = (
        round((projected_eom_spend - target_monthly) / target_monthly * 100, 2)
        if target_monthly > 0
        else None
    )

    if projected_variance_pct is None:
        pacing_status = "no_target"
    elif projected_variance_pct > 10.0:
        pacing_status = "overspending"
    elif projected_variance_pct < -10.0:
        pacing_status = "underspending"
    else:
        pacing_status = "on_track"

    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "campaign_id": campaign_id,
        "currency_code": currency_code,
        "time_zone": time_zone,
        "queried_at_utc": datetime.now(timezone.utc).isoformat(),
        "month": f"{today_local.year:04d}-{today_local.month:02d}",
        "calendar": {
            "days_in_month": days_in_month,
            "complete_days_elapsed": complete_days_elapsed,
            "days_remaining": days_remaining,
            "month_progress_pct": round(complete_days_elapsed / days_in_month * 100, 1),
        },
        "target": {
            "monthly_target_budget": target_monthly,
            "source": source,
            "expected_mtd_spend": expected_mtd_spend,
            "configured_daily_budgets_sum": round(active_daily_budget_micros / 1_000_000, 2),
        },
        "actuals": {
            "mtd_spend": round(mtd_spend, 2),
            "avg_daily_spend_mtd": round(avg_daily_mtd, 2),
            "avg_daily_spend_last_7d": round(avg_daily_7d, 2),
            "remaining_budget": remaining_budget,
            "required_daily_spend_to_hit_target": required_daily_spend,
        },
        "projection": {
            "projected_eom_spend": projected_eom_spend,
            "projected_variance_pct": projected_variance_pct,
            "pacing_status": pacing_status,
        },
    }


def register_diagnostics_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def diagnose_delivery(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        campaign_id: Annotated[Optional[str], Field(description="Optional campaign ID filter")] = None,
        days: Annotated[int, Field(description="Lookback window (1 to 90 days)")] = 14,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
    ) -> str:
        """Diagnose why campaigns are not delivering or losing reach across billing, policy, budget, and rank."""
        ensure_tool_allowed("diagnose_delivery")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, cust_obj = _account_meta(cid)
        window = resolve_date_range(
            days=min(max(1, days), 90),
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        warnings: list[str] = []
        truncated_queries: list[str] = []

        billing_rows, billing_truncated, warn = _safe_search_bounded(
            cid,
            "SELECT billing_setup.id, billing_setup.status, billing_setup.payments_account FROM billing_setup",
        )
        if billing_truncated:
            truncated_queries.append("billing_setup")
        if warn:
            warnings.append(warn)

        account_budget_rows, budget_truncated, warn = _safe_search_bounded(
            cid,
            """
            SELECT account_budget.id, account_budget.name, account_budget.status,
                   account_budget.approved_spending_limit_micros,
                   account_budget.amount_served_micros
            FROM account_budget
            """,
        )
        if budget_truncated:
            truncated_queries.append("account_budget")
        if warn:
            warnings.append(warn)

        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        campaign_rows, campaigns_truncated = _search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.primary_status, campaign.primary_status_reasons,
                   campaign_budget.amount_micros,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.search_budget_lost_impression_share,
                   metrics.search_rank_lost_impression_share
            FROM campaign
            WHERE {window.gaql_clause}
              AND campaign.status != 'REMOVED'{camp_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        if campaigns_truncated:
            truncated_queries.append("campaign")

        ad_policy_rows, ad_policy_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign.name, ad_group.id,
                   ad_group_ad.ad.id, ad_group_ad.status,
                   ad_group_ad.policy_summary.approval_status,
                   ad_group_ad.policy_summary.policy_topic_entries
            FROM ad_group_ad
            WHERE ad_group_ad.status = 'ENABLED'
              AND campaign.status != 'REMOVED'{camp_filter}
            """,
        )
        if ad_policy_truncated:
            truncated_queries.append("ad_group_ad")
        if warn:
            warnings.append(warn)

        asset_policy_rows, asset_policy_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign_asset.field_type,
                   asset.id, asset.name,
                   asset.policy_summary.approval_status
            FROM campaign_asset
            WHERE campaign_asset.status = 'ENABLED'{camp_filter}
            """,
        )
        if asset_policy_truncated:
            truncated_queries.append("campaign_asset")
        if warn:
            warnings.append(warn)

        keyword_policy_rows, kw_policy_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id, ad_group_criterion.keyword.text,
                   ad_group_criterion.approval_status
            FROM keyword_view
            WHERE ad_group_criterion.status = 'ENABLED'{camp_filter}
            """,
        )
        if kw_policy_truncated:
            truncated_queries.append("keyword_view")
        if warn:
            warnings.append(warn)

        any_truncated = bool(truncated_queries)
        report = _summarize_delivery_diagnosis(
            customer_id=cid,
            customer_obj=cust_obj,
            billing_rows=billing_rows,
            account_budget_rows=account_budget_rows,
            campaign_rows=campaign_rows,
            ad_policy_rows=ad_policy_rows,
            asset_policy_rows=asset_policy_rows,
            keyword_policy_rows=keyword_policy_rows,
            coverage=coverage,
            query_warnings=warnings,
            truncated_queries=truncated_queries,
        )
        report["truncated"] = any_truncated
        report["truncated_queries"] = sorted(set(truncated_queries))
        report["coverage_complete"] = (not bool(warnings)) and (not any_truncated)
        if any_truncated:
            report["truncation_warning"] = (
                f"One or more delivery diagnostic queries reached GOOGLE_ADS_MAX_SEARCH_ROWS "
                f"({', '.join(report['truncated_queries'])}); policy or campaign findings may be incomplete."
            )
        return format_json(report)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_budget_pacing(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        monthly_target_budget: Annotated[
            Optional[float],
            Field(description="Optional monthly target budget in account currency units (not micros)"),
        ] = None,
        profile_id: Annotated[
            Optional[str],
            Field(description="Optional analysis profile ID to read monthly_target_budget from"),
        ] = None,
        campaign_id: Annotated[Optional[str], Field(description="Optional campaign ID filter")] = None,
    ) -> str:
        """Compute month-to-date spend, daily run-rate, projected end-of-month spend, and pacing variance."""
        ensure_tool_allowed("get_budget_pacing")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        today_local, effective_tz, _ = account_today(time_zone)

        target_value = monthly_target_budget
        target_source = "explicit_argument"
        if target_value is None and profile_id:
            prof = load_profile(profile_id)
            raw_target = prof.get("parameters", {}).get("monthly_target_budget")
            if raw_target is not None:
                target_value = float(raw_target)
                target_source = f"profile:{prof['id']}"

        # Complete MTD days (day 1 through yesterday in account timezone)
        mtd_cost_micros = 0.0
        mtd_truncated = False
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        if today_local.day > 1:
            mtd_start = date(today_local.year, today_local.month, 1)
            mtd_end = today_local - timedelta(days=1)
            resource = "campaign" if clean_camp else "customer"
            mtd_rows, mtd_truncated = _search_bounded(
                cid,
                f"""
                SELECT metrics.cost_micros
                FROM {resource}
                WHERE segments.date BETWEEN '{mtd_start.isoformat()}' AND '{mtd_end.isoformat()}'{camp_filter}
                """,
            )
            mtd_cost_micros = sum(
                float((r.get("metrics") or {}).get("costMicros") or 0) for r in mtd_rows
            )

        last_7d_win = resolve_date_range(days=7, time_zone=effective_tz, complete_days_only=True)
        resource = "campaign" if clean_camp else "customer"
        rows_7d, last_7d_truncated = _search_bounded(
            cid,
            f"""
            SELECT metrics.cost_micros
            FROM {resource}
            WHERE {last_7d_win.gaql_clause}{camp_filter}
            """,
        )
        cost_7d_micros = sum(
            float((r.get("metrics") or {}).get("costMicros") or 0) for r in rows_7d
        )

        budget_rows, budgets_truncated = _search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.status = 'ENABLED'{camp_filter}
            """,
        )
        daily_budgets_sum = sum(
            float((r.get("campaignBudget") or {}).get("amountMicros") or 0) for r in budget_rows
        )

        any_truncated = bool(mtd_truncated or last_7d_truncated or budgets_truncated)
        report = _summarize_budget_pacing(
            customer_id=cid,
            currency_code=currency_code,
            time_zone=effective_tz,
            today_local=today_local,
            mtd_cost_micros=mtd_cost_micros,
            last_7d_cost_micros=cost_7d_micros,
            last_7d_days=7,
            active_daily_budget_micros=daily_budgets_sum,
            monthly_target_budget=target_value,
            target_source=target_source,
            campaign_id=clean_camp,
        )
        report["truncated"] = any_truncated
        report["totals_are_partial"] = any_truncated
        if any_truncated:
            report["truncation_warning"] = (
                "One or more budget pacing queries reached GOOGLE_ADS_MAX_SEARCH_ROWS; "
                "MTD/7d spend or configured daily budget totals may be partial."
            )
        return format_json(report)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_landing_page_performance(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
        campaign_id: Annotated[Optional[str], Field(description="Optional campaign ID filter")] = None,
    ) -> str:
        """Return landing page URL performance, mobile-friendly click rate, and speed score."""
        ensure_tool_allowed("get_landing_page_performance")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)

        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        meta = search_with_meta(
            cid,
            f"""
            SELECT landing_page_view.unexpanded_final_url,
                   campaign.id, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value,
                   metrics.mobile_friendly_clicks_percentage,
                   metrics.speed_score
            FROM landing_page_view
            WHERE {window.gaql_clause}{camp_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        return format_json(
            {
                "customer_id": cid,
                "tool": "get_landing_page_performance",
                **coverage,
                "returned_rows": meta["returned_rows"],
                "truncated": meta["truncated"],
                "results": meta["rows"],
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_demographics_performance(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        dimension: Annotated[
            str,
            Field(description="Dimension: 'age', 'gender', 'income', 'parental_status', or 'all'"),
        ] = "age",
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
        campaign_id: Annotated[Optional[str], Field(description="Optional campaign ID filter")] = None,
    ) -> str:
        """Return performance broken down by age range, gender, household income, or parental status."""
        ensure_tool_allowed("get_demographics_performance")
        cid = ensure_customer_allowed(customer_id)
        dim = (dimension or "age").strip().lower()
        views = {
            "age": ("age_range_view", "ad_group_criterion.age_range.type"),
            "gender": ("gender_view", "ad_group_criterion.gender.type"),
            "income": ("income_range_view", "ad_group_criterion.income_range.type"),
            "parental_status": ("parental_status_view", "ad_group_criterion.parental_status.type"),
        }
        if dim != "all" and dim not in views:
            raise ValueError(f"dimension must be one of {sorted(views)} or 'all'")

        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""

        targets = list(views.keys()) if dim == "all" else [dim]
        breakdowns: dict[str, Any] = {}
        for key in targets:
            view_name, field_name = views[key]
            meta = search_with_meta(
                cid,
                f"""
                SELECT {field_name}, campaign.id, campaign.name,
                       metrics.impressions, metrics.clicks, metrics.ctr,
                       metrics.cost_micros, metrics.conversions, metrics.conversions_value
                FROM {view_name}
                WHERE {window.gaql_clause}{camp_filter}
                ORDER BY metrics.cost_micros DESC
                """,
            )
            breakdowns[key] = {
                "returned_rows": meta["returned_rows"],
                "truncated": meta["truncated"],
                "results": meta["rows"],
            }

        return format_json(
            {
                "customer_id": cid,
                "tool": "get_demographics_performance",
                "dimension": dim,
                **coverage,
                "breakdowns": breakdowns,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_assets_and_calls_performance(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
        campaign_id: Annotated[Optional[str], Field(description="Optional campaign ID filter")] = None,
    ) -> str:
        """Return campaign asset/extension performance and approval status plus aggregate call_view metrics (zero PII)."""
        ensure_tool_allowed("get_assets_and_calls_performance")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""

        assets_meta = search_with_meta(
            cid,
            f"""
            SELECT campaign.id, campaign.name,
                   campaign_asset.field_type, campaign_asset.status,
                   asset.id, asset.name, asset.type,
                   asset.policy_summary.approval_status,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM campaign_asset
            WHERE {window.gaql_clause}
              AND campaign_asset.status != 'REMOVED'{camp_filter}
            ORDER BY metrics.impressions DESC
            """,
        )

        warnings: list[str] = []
        call_rows, calls_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign.name,
                   call_view.call_status, call_view.type,
                   call_view.call_duration_seconds,
                   call_view.call_tracking_display_location
            FROM call_view
            WHERE {window.gaql_clause}{camp_filter}
            """,
        )
        if warn:
            warnings.append(warn)

        any_truncated = bool(assets_meta["truncated"] or calls_truncated)
        return format_json(
            {
                "customer_id": cid,
                "tool": "get_assets_and_calls_performance",
                **coverage,
                "truncated": any_truncated,
                "totals_are_partial": any_truncated,
                "coverage_complete": (not bool(warnings)) and (not any_truncated),
                "assets": {
                    "returned_rows": assets_meta["returned_rows"],
                    "truncated": assets_meta["truncated"],
                    "results": assets_meta["rows"],
                },
                "calls": {
                    "total_calls": len(call_rows),
                    "truncated": calls_truncated,
                    "results": call_rows,
                },
                "warnings": warnings,
            }
        )
