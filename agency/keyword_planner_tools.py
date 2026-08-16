"""Google Ads Keyword Planner tools.

Wraps the KeywordPlanIdeaService endpoints:

- :generateKeywordIdeas
- :generateKeywordHistoricalMetrics
- :generateForecastMetrics (KeywordPlanService)

And a geo-target lookup helper so callers can translate country/region
names into the criterion IDs the Keyword Planner requires.

All tools are read-only against Google Ads and consume no budget: the
Keyword Planner endpoints are query-only.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, request, search
from .config import ensure_customer_allowed, ensure_tool_allowed
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_NETWORKS = frozenset({"GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"})
_KEYWORD_ANNOTATIONS = frozenset({"UNSPECIFIED", "UNKNOWN", "KEYWORD_CONCEPT"})
_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})
_MAX_SEED_KEYWORDS = 20
_MAX_HIST_KEYWORDS = 10_000


def _split_list(value: Any) -> list[str]:
    """Accept either a list or a comma-separated string of tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError("value must be a list or a comma-separated string")
    return [str(item).strip() for item in parts if str(item).strip()]


def _language_resource(language_id: str | int) -> str:
    digits = re.sub(r"\D", "", str(language_id))
    if not digits:
        raise ValueError("language_id must be a numeric language constant id (e.g. 1003 for Spanish)")
    return f"languageConstants/{int(digits)}"


def _geo_resources(geo_target_ids: Any) -> list[str]:
    tokens = _split_list(geo_target_ids)
    if not tokens:
        raise ValueError("geo_target_ids must include at least one geo target constant id")
    resources: list[str] = []
    for token in tokens:
        digits = re.sub(r"\D", "", token)
        if not digits:
            raise ValueError(f"geo_target_id {token!r} must be numeric")
        resources.append(f"geoTargetConstants/{int(digits)}")
    return resources


def _validate_network(network: str) -> str:
    value = (network or "").strip().upper()
    if value not in _NETWORKS:
        raise ValueError(
            f"network must be one of {sorted(_NETWORKS)} (got {network!r})"
        )
    return value


def _validate_annotations(annotations: Any) -> list[str]:
    tokens = [token.upper() for token in _split_list(annotations)]
    for token in tokens:
        if token not in _KEYWORD_ANNOTATIONS:
            raise ValueError(
                f"keyword_annotations entry {token!r} must be one of {sorted(_KEYWORD_ANNOTATIONS)}"
            )
    return tokens


def _clamp_page_size(page_size: int, ceiling: int) -> int:
    try:
        value = int(page_size)
    except (TypeError, ValueError) as error:
        raise ValueError("page_size must be an integer") from error
    if value < 1:
        raise ValueError("page_size must be >= 1")
    return min(value, ceiling)


def _yearmonth(value: str | None, *, field: str) -> dict[str, int] | None:
    if value in (None, ""):
        return None
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", str(value).strip())
    if not match:
        raise ValueError(f"{field} must be in YYYY-MM format")
    return {"year": int(match.group(1)), "month": _MONTH_ENUM[int(match.group(2))]}


# The API expects the MonthOfYear enum name, not a raw integer.
_MONTH_ENUM = {
    1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
    5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
    9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER",
}


def _historical_options(
    include_average_cpc: bool,
    year_month_start: Optional[str],
    year_month_end: Optional[str],
) -> dict[str, Any]:
    options: dict[str, Any] = {"includeAverageCpc": bool(include_average_cpc)}
    start = _yearmonth(year_month_start, field="year_month_start")
    end = _yearmonth(year_month_end, field="year_month_end")
    if start or end:
        if not (start and end):
            raise ValueError("year_month_start and year_month_end must be provided together")
        options["yearMonthRange"] = {"start": start, "end": end}
    return options


def _seed(keywords: Any, page_url: Optional[str]) -> dict[str, Any]:
    seed_keywords = _split_list(keywords)
    if len(seed_keywords) > _MAX_SEED_KEYWORDS:
        raise ValueError(f"keywords accepts up to {_MAX_SEED_KEYWORDS} seed terms per request")
    url = (page_url or "").strip() or None
    if not seed_keywords and not url:
        raise ValueError("Provide at least one of: keywords, page_url")
    if seed_keywords and url:
        return {"keywordAndUrlSeed": {"url": url, "keywords": seed_keywords}}
    if seed_keywords:
        return {"keywordSeed": {"keywords": seed_keywords}}
    return {"urlSeed": {"url": url}}


def register_keyword_planner_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_keyword_ideas(
        customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes)"),
        keywords: Optional[str] = Field(
            default=None,
            description=(
                "Seed keywords, up to 20. Accept a Python list or a comma-separated string. "
                "Provide either keywords, page_url, or both."
            ),
        ),
        page_url: Optional[str] = Field(
            default=None,
            description="Seed URL. Use with or without keywords.",
        ),
        geo_target_ids: str = Field(
            description=(
                "Geo target constant IDs, comma-separated or as a list. "
                "Examples: 2032=Argentina, 2484=Mexico, 2724=Spain, 2840=United States."
            ),
        ),
        language_id: str = Field(
            default="1003",
            description="Language constant ID. Default 1003 (Spanish). English is 1000.",
        ),
        network: str = Field(
            default="GOOGLE_SEARCH",
            description="GOOGLE_SEARCH or GOOGLE_SEARCH_AND_PARTNERS.",
        ),
        include_adult_keywords: bool = Field(default=False),
        keyword_annotations: Optional[str] = Field(
            default=None,
            description="Optional annotations. Use KEYWORD_CONCEPT to get theme groupings.",
        ),
        page_size: int = Field(default=100, description="Results per page (max 1000)."),
    ) -> str:
        """Suggest new keywords from seeds with search volume, competition and top-of-page bids."""
        ensure_tool_allowed("generate_keyword_ideas")
        cid = ensure_customer_allowed(customer_id)
        body: dict[str, Any] = {
            "language": _language_resource(language_id),
            "geoTargetConstants": _geo_resources(geo_target_ids),
            "keywordPlanNetwork": _validate_network(network),
            "includeAdultKeywords": bool(include_adult_keywords),
            "pageSize": _clamp_page_size(page_size, 1000),
        }
        annotations = _validate_annotations(keyword_annotations)
        if annotations:
            body["keywordAnnotation"] = annotations
        body.update(_seed(keywords, page_url))

        results: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            if page_token:
                body["pageToken"] = page_token
            response = request(
                "POST",
                f"customers/{cid}:generateKeywordIdeas",
                customer_id=cid,
                body=body,
            )
            results.extend(response.get("results", []))
            page_token = response.get("nextPageToken")
            if not page_token or len(results) >= body["pageSize"]:
                break

        return format_json({
            "customer_id": cid,
            "tool": "generate_keyword_ideas",
            "fresh": True,
            "language": body["language"],
            "geo_target_constants": body["geoTargetConstants"],
            "keyword_plan_network": body["keywordPlanNetwork"],
            "seed": {k: v for k, v in body.items() if k.endswith("Seed")},
            "count": len(results),
            "results": results,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_keyword_historical_metrics(
        customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes)"),
        keywords: str = Field(
            description="Exact keywords to look up. List or comma-separated string.",
        ),
        geo_target_ids: str = Field(
            description="Geo target constant IDs, comma-separated or as a list.",
        ),
        language_id: str = Field(default="1003"),
        network: str = Field(default="GOOGLE_SEARCH"),
        include_average_cpc: bool = Field(default=True),
        year_month_start: Optional[str] = Field(
            default=None,
            description="Optional YYYY-MM start. If set, year_month_end is required.",
        ),
        year_month_end: Optional[str] = Field(
            default=None,
            description="Optional YYYY-MM end. If set, year_month_start is required.",
        ),
    ) -> str:
        """Return historical monthly search volume and competition for an exact keyword list."""
        ensure_tool_allowed("generate_keyword_historical_metrics")
        cid = ensure_customer_allowed(customer_id)
        keyword_list = _split_list(keywords)
        if not keyword_list:
            raise ValueError("keywords must include at least one term")
        if len(keyword_list) > _MAX_HIST_KEYWORDS:
            raise ValueError(
                f"generate_keyword_historical_metrics accepts up to {_MAX_HIST_KEYWORDS} keywords per call"
            )
        body: dict[str, Any] = {
            "keywords": keyword_list,
            "language": _language_resource(language_id),
            "geoTargetConstants": _geo_resources(geo_target_ids),
            "keywordPlanNetwork": _validate_network(network),
            "historicalMetricsOptions": _historical_options(
                include_average_cpc, year_month_start, year_month_end
            ),
        }
        response = request(
            "POST",
            f"customers/{cid}:generateKeywordHistoricalMetrics",
            customer_id=cid,
            body=body,
        )
        results = response.get("results", [])
        return format_json({
            "customer_id": cid,
            "tool": "generate_keyword_historical_metrics",
            "fresh": True,
            "language": body["language"],
            "geo_target_constants": body["geoTargetConstants"],
            "keyword_plan_network": body["keywordPlanNetwork"],
            "historical_options": body["historicalMetricsOptions"],
            "count": len(results),
            "results": results,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_keyword_forecast(
        customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes)"),
        keywords: str = Field(
            description="Keywords to forecast. List or comma-separated string.",
        ),
        cpc_bid_micros: int = Field(
            description="Max CPC bid in micros (1 unit = 1_000_000 micros)."
        ),
        daily_budget_micros: int = Field(
            description="Daily budget in micros. Applied to the forecasted plan.",
        ),
        geo_target_ids: str = Field(
            description="Geo target constant IDs, comma-separated or as a list.",
        ),
        language_id: str = Field(default="1003"),
        network: str = Field(default="GOOGLE_SEARCH"),
        match_type: str = Field(
            default="PHRASE",
            description="Match type for every forecasted keyword: EXACT, PHRASE or BROAD.",
        ),
    ) -> str:
        """Forecast clicks, impressions, cost and conversions for a keyword set at a given bid + budget."""
        ensure_tool_allowed("generate_keyword_forecast")
        cid = ensure_customer_allowed(customer_id)
        keyword_list = _split_list(keywords)
        if not keyword_list:
            raise ValueError("keywords must include at least one term")
        if int(cpc_bid_micros) <= 0:
            raise ValueError("cpc_bid_micros must be positive")
        if int(daily_budget_micros) <= 0:
            raise ValueError("daily_budget_micros must be positive")
        match = (match_type or "").strip().upper()
        if match not in _MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(_MATCH_TYPES)}")

        campaign = {
            "languageConstants": [_language_resource(language_id)],
            "geoTargetConstants": _geo_resources(geo_target_ids),
            "biddingStrategy": {
                "manualCpcBiddingStrategy": {
                    "maxCpcBidMicros": str(int(cpc_bid_micros)),
                    "dailyBudgetMicros": str(int(daily_budget_micros)),
                }
            },
            "adGroups": [
                {
                    "keywords": [
                        {"text": kw, "matchType": match}
                        for kw in keyword_list
                    ],
                }
            ],
        }
        today = date.today()
        forecast_start = today + timedelta(days=1)
        forecast_end = forecast_start + timedelta(days=29)
        body = {
            "campaign": campaign,
            "forecastPeriod": {
                "startDate": forecast_start.isoformat(),
                "endDate": forecast_end.isoformat(),
            },
        }
        response = request(
            "POST",
            f"customers/{cid}:generateKeywordForecastMetrics",
            customer_id=cid,
            body=body,
        )
        return format_json({
            "customer_id": cid,
            "tool": "generate_keyword_forecast",
            "fresh": True,
            "keyword_count": len(keyword_list),
            "cpc_bid_micros": int(cpc_bid_micros),
            "daily_budget_micros": int(daily_budget_micros),
            "match_type": match,
            "response": response,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def find_geo_target_constants(
        customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes)"),
        query: Optional[str] = Field(
            default=None,
            description="Substring to match against geo_target_constant.name (case-insensitive).",
        ),
        country_code: Optional[str] = Field(
            default=None,
            description="ISO-3166 alpha-2 country code to filter by (e.g. AR, MX, ES, US).",
        ),
        target_type: Optional[str] = Field(
            default=None,
            description="Optional target type filter (Country, Region, City, ...).",
        ),
        limit: int = Field(default=25, description="Maximum rows to return (1-500)."),
    ) -> str:
        """Look up geo_target_constant IDs by name or country. Use the returned IDs in the Keyword Planner tools."""
        ensure_tool_allowed("find_geo_target_constants")
        cid = ensure_customer_allowed(customer_id)
        capped = max(1, min(500, int(limit)))

        filters: list[str] = ["geo_target_constant.status = 'ENABLED'"]
        if query:
            cleaned = query.strip()
            if "'" in cleaned or "\\" in cleaned:
                raise ValueError("query contains invalid characters")
            filters.append(f"geo_target_constant.name LIKE '%{cleaned}%'")
        if country_code:
            code = country_code.strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", code):
                raise ValueError("country_code must be 2 letters (ISO-3166 alpha-2)")
            filters.append(f"geo_target_constant.country_code = '{code}'")
        if target_type:
            cleaned_type = target_type.strip()
            if not re.fullmatch(r"[A-Za-z ]+", cleaned_type):
                raise ValueError("target_type must be alphabetic")
            filters.append(f"geo_target_constant.target_type = '{cleaned_type}'")

        gaql = f"""
            SELECT geo_target_constant.id,
                   geo_target_constant.name,
                   geo_target_constant.canonical_name,
                   geo_target_constant.country_code,
                   geo_target_constant.target_type,
                   geo_target_constant.status
            FROM geo_target_constant
            WHERE {' AND '.join(filters)}
            ORDER BY geo_target_constant.name
            LIMIT {capped}
        """
        return format_json({
            "customer_id": cid,
            "tool": "find_geo_target_constants",
            "fresh": True,
            "results": search(cid, gaql),
        })
