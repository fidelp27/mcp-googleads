"""Built-in synthetic demo dataset for zero-credential MCP exploration (--demo)."""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any


DEMO_ACCOUNTS: dict[str, dict[str, Any]] = {
    "1111111111": {
        "id": "1111111111",
        "name": "Nordic Gear Ecommerce (Demo)",
        "currency_code": "EUR",
        "time_zone": "Europe/Madrid",
        "industry": "Ecommerce Outdoor",
        "manager": False,
        "test_account": False,
        "accepted_customer_data_terms": True,
        "enhanced_conversions_for_leads_enabled": False,
        "conversion_tracking_status": "CONVERSION_TRACKING_MANAGED_BY_SELF",
    },
    "2222222222": {
        "id": "2222222222",
        "name": "Apex B2B Leads (Demo)",
        "currency_code": "USD",
        "time_zone": "America/New_York",
        "industry": "B2B SaaS",
        "manager": False,
        "test_account": False,
        "accepted_customer_data_terms": False,
        "enhanced_conversions_for_leads_enabled": False,
        "conversion_tracking_status": "CONVERSION_TRACKING_MANAGED_BY_SELF",
    },
}

DEMO_CUSTOMER_IDS: tuple[str, ...] = tuple(DEMO_ACCOUNTS.keys())


def is_demo_mode() -> bool:
    return os.getenv("GOOGLE_ADS_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def demo_clients_config() -> dict[str, dict[str, Any]]:
    return {
        cid: {
            "name": meta["name"],
            "industry": meta["industry"],
            "currency": meta["currency_code"],
        }
        for cid, meta in DEMO_ACCOUNTS.items()
    }


def _extract_resource(query: str) -> str:
    match = re.search(r"\bFROM\s+([a-zA-Z0-9_]+)", query, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _extract_campaign_id(query: str) -> str | None:
    match = re.search(r"campaign\.id\s*=\s*(\d+)", query, re.IGNORECASE)
    return match.group(1) if match else None


def _customer_rows(customer_id: str, query: str = "") -> list[dict[str, Any]]:
    meta = DEMO_ACCOUNTS[customer_id]
    select_part = query.upper().split("FROM", 1)[0]
    if "SEGMENTS.WEEK" in select_part:
        base_end = date.today() - timedelta(days=1)
        return [
            {
                "segments": {"week": (base_end - timedelta(days=14)).isoformat()},
                "metrics": {
                    "impressions": "32000",
                    "clicks": "1250",
                    "costMicros": "780000000",
                    "conversions": 32.0,
                    "conversionsValue": 3950.0,
                    "conversionsByConversionDate": 34.0,
                    "conversionsValueByConversionDate": 4180.0,
                },
            },
            {
                "segments": {"week": (base_end - timedelta(days=7)).isoformat()},
                "metrics": {
                    "impressions": "35500",
                    "clicks": "1420",
                    "costMicros": "840000000",
                    "conversions": 38.0,
                    "conversionsValue": 4620.0,
                    "conversionsByConversionDate": 39.5,
                    "conversionsValueByConversionDate": 4810.0,
                },
            },
        ]
    if "SEGMENTS.DATE" in select_part and "CUSTOMER.CURRENCY_CODE" not in select_part:
        base_end = date.today() - timedelta(days=1)
        rows = []
        for offset in range(6, -1, -1):
            day = (base_end - timedelta(days=offset)).isoformat()
            factor = 1.0 + (0.05 * (6 - offset))
            if customer_id == "1111111111":
                rows.append(
                    {
                        "segments": {"date": day},
                        "metrics": {
                            "impressions": str(int(4500 * factor)),
                            "clicks": str(int(185 * factor)),
                            "costMicros": str(int(110_000_000 * factor)),
                            "conversions": round(4.5 * factor, 2),
                            "conversionsValue": round(560.0 * factor, 2),
                            "conversionsByConversionDate": round(4.8 * factor, 2),
                            "conversionsValueByConversionDate": round(590.0 * factor, 2),
                        },
                    }
                )
            else:
                rows.append(
                    {
                        "segments": {"date": day},
                        "metrics": {
                            "impressions": str(int(2100 * factor)),
                            "clicks": str(int(70 * factor)),
                            "costMicros": str(int(135_000_000 * factor)),
                            "conversions": round(1.4 * factor, 2),
                            "conversionsValue": round(280.0 * factor, 2),
                            "conversionsByConversionDate": round(1.6 * factor, 2),
                            "conversionsValueByConversionDate": round(320.0 * factor, 2),
                        },
                    }
                )
        return rows
    if customer_id == "1111111111":
        metrics = {
            "impressions": "142000",
            "clicks": "5840",
            "ctr": 0.0411,
            "costMicros": "3450000000",
            "conversions": 148.0,
            "conversionsValue": 18240.0,
            "allConversions": 590.0,
            "allConversionsValue": 21500.0,
        }
    else:
        metrics = {
            "impressions": "64000",
            "clicks": "2150",
            "ctr": 0.0336,
            "costMicros": "4200000000",
            "conversions": 46.0,
            "conversionsValue": 9200.0,
            "allConversions": 185.0,
            "allConversionsValue": 11400.0,
        }
    return [
        {
            "customer": {
                "id": meta["id"],
                "descriptiveName": meta["name"],
                "status": "ENABLED",
                "currencyCode": meta["currency_code"],
                "timeZone": meta["time_zone"],
                "manager": meta["manager"],
                "testAccount": meta["test_account"],
                "conversionTrackingSetting": {
                    "conversionTrackingId": f"900{meta['id'][:6]}",
                    "conversionTrackingStatus": meta["conversion_tracking_status"],
                    "googleAdsConversionCustomer": f"customers/{meta['id']}",
                    "acceptedCustomerDataTerms": meta["accepted_customer_data_terms"],
                    "enhancedConversionsForLeadsEnabled": meta[
                        "enhanced_conversions_for_leads_enabled"
                    ],
                },
            },
            "metrics": metrics,
        }
    ]


def _campaign_rows(customer_id: str, query: str) -> list[dict[str, Any]]:
    q_upper = query.upper()
    yesterday = "DURING YESTERDAY" in q_upper
    scale = 1 / 20 if yesterday else 1.0

    if customer_id == "1111111111":
        raw = [
            {
                "campaign": {
                    "id": "101",
                    "name": "ES | Search | Brand",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "biddingStrategyType": "TARGET_ROAS",
                    "primaryStatus": "ELIGIBLE",
                    "primaryStatusReasons": [],
                },
                "campaignBudget": {
                    "id": "501",
                    "name": "Brand Daily",
                    "amountMicros": "40000000",
                    "status": "ENABLED",
                },
                "metrics": {
                    "impressions": str(int(36000 * scale)),
                    "clicks": str(int(2880 * scale)),
                    "ctr": 0.08,
                    "averageCpc": 250000,
                    "costMicros": str(int(720000000 * scale)),
                    "conversions": round(72.0 * scale, 2),
                    "conversionsValue": round(9360.0 * scale, 2),
                    "allConversions": round(210.0 * scale, 2),
                    "allConversionsValue": round(10500.0 * scale, 2),
                    "costPerConversion": 10000000,
                    "searchImpressionShare": 0.89,
                    "searchBudgetLostImpressionShare": 0.02,
                    "searchRankLostImpressionShare": 0.09,
                },
            },
            {
                "campaign": {
                    "id": "102",
                    "name": "ES | PMax | Core Catalog",
                    "status": "ENABLED",
                    "advertisingChannelType": "PERFORMANCE_MAX",
                    "biddingStrategyType": "MAXIMIZE_CONVERSION_VALUE",
                    "primaryStatus": "LIMITED",
                    "primaryStatusReasons": ["BUDGET_CONSTRAINED"],
                },
                "campaignBudget": {
                    "id": "502",
                    "name": "PMax Daily",
                    "amountMicros": "75000000",
                    "status": "ENABLED",
                },
                "metrics": {
                    "impressions": str(int(78000 * scale)),
                    "clicks": str(int(2180 * scale)),
                    "ctr": 0.0279,
                    "averageCpc": 917431,
                    "costMicros": str(int(2000000000 * scale)),
                    "conversions": round(64.0 * scale, 2),
                    "conversionsValue": round(7680.0 * scale, 2),
                    "allConversions": round(295.0 * scale, 2),
                    "allConversionsValue": round(9200.0 * scale, 2),
                    "costPerConversion": 31250000,
                    "searchImpressionShare": 0.61,
                    "searchBudgetLostImpressionShare": 0.28,
                    "searchRankLostImpressionShare": 0.11,
                },
            },
            {
                "campaign": {
                    "id": "103",
                    "name": "ES | Search | Non-Brand Jackets",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "primaryStatus": "ELIGIBLE",
                    "primaryStatusReasons": [],
                },
                "campaignBudget": {
                    "id": "503",
                    "name": "Non-Brand Daily",
                    "amountMicros": "35000000",
                    "status": "ENABLED",
                },
                "metrics": {
                    "impressions": str(int(28000 * scale)),
                    "clicks": str(int(780 * scale)),
                    "ctr": 0.0278,
                    "averageCpc": 935897,
                    "costMicros": str(int(730000000 * scale)),
                    "conversions": round(12.0 * scale, 2),
                    "conversionsValue": round(1200.0 * scale, 2),
                    "allConversions": round(85.0 * scale, 2),
                    "allConversionsValue": round(1800.0 * scale, 2),
                    "costPerConversion": 60833333,
                    "searchImpressionShare": 0.44,
                    "searchBudgetLostImpressionShare": 0.12,
                    "searchRankLostImpressionShare": 0.44,
                },
            },
        ]
    else:
        raw = [
            {
                "campaign": {
                    "id": "201",
                    "name": "US | Search | High-Intent CRM",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "biddingStrategyType": "TARGET_CPA",
                    "primaryStatus": "LIMITED",
                    "primaryStatusReasons": ["BUDGET_CONSTRAINED"],
                },
                "campaignBudget": {
                    "id": "601",
                    "name": "Core Leads Daily",
                    "amountMicros": "110000000",
                    "status": "ENABLED",
                },
                "metrics": {
                    "impressions": str(int(42000 * scale)),
                    "clicks": str(int(1620 * scale)),
                    "ctr": 0.0386,
                    "averageCpc": 1790123,
                    "costMicros": str(int(2900000000 * scale)),
                    "conversions": round(46.0 * scale, 2),
                    "conversionsValue": round(9200.0 * scale, 2),
                    "allConversions": round(140.0 * scale, 2),
                    "allConversionsValue": round(10600.0 * scale, 2),
                    "costPerConversion": 63043478,
                    "searchImpressionShare": 0.52,
                    "searchBudgetLostImpressionShare": 0.34,
                    "searchRankLostImpressionShare": 0.14,
                },
            },
            {
                "campaign": {
                    "id": "202",
                    "name": "US | Search | Competitors Broad",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                    "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
                    "primaryStatus": "ELIGIBLE",
                    "primaryStatusReasons": [],
                },
                "campaignBudget": {
                    "id": "602",
                    "name": "Competitors Daily",
                    "amountMicros": "60000000",
                    "status": "ENABLED",
                },
                "metrics": {
                    "impressions": str(int(22000 * scale)),
                    "clicks": str(int(530 * scale)),
                    "ctr": 0.0241,
                    "averageCpc": 2452830,
                    "costMicros": str(int(1300000000 * scale)),
                    "conversions": 0.0,
                    "conversionsValue": 0.0,
                    "allConversions": round(45.0 * scale, 2),
                    "allConversionsValue": round(800.0 * scale, 2),
                    "costPerConversion": 0,
                    "searchImpressionShare": 0.31,
                    "searchBudgetLostImpressionShare": 0.05,
                    "searchRankLostImpressionShare": 0.64,
                },
            },
        ]

    if "SEGMENTS.CONVERSION_ACTION" in q_upper:
        today_str = (date.today() - timedelta(days=1)).isoformat()
        if customer_id == "1111111111":
            return [
                {
                    "campaign": raw[0]["campaign"],
                    "segments": {
                        "date": today_str,
                        "device": "MOBILE",
                        "conversionAction": "customers/1111111111/conversionActions/1001",
                        "conversionActionName": "Purchase (Web)",
                        "conversionActionCategory": "PURCHASE",
                    },
                    "metrics": {
                        "conversions": 148.0,
                        "allConversions": 152.0,
                        "conversionsValue": 18240.0,
                        "allConversionsValue": 18600.0,
                        "costPerConversion": 23310810,
                    },
                },
                {
                    "campaign": raw[1]["campaign"],
                    "segments": {
                        "date": today_str,
                        "device": "DESKTOP",
                        "conversionAction": "customers/1111111111/conversionActions/1002",
                        "conversionActionName": "Add to Cart (Secondary)",
                        "conversionActionCategory": "ADD_TO_CART",
                    },
                    "metrics": {
                        "conversions": 0.0,
                        "allConversions": 438.0,
                        "conversionsValue": 0.0,
                        "allConversionsValue": 2900.0,
                        "costPerConversion": 0,
                    },
                },
            ]
        return [
            {
                "campaign": raw[0]["campaign"],
                "segments": {
                    "date": today_str,
                    "device": "DESKTOP",
                    "conversionAction": "customers/2222222222/conversionActions/2001",
                    "conversionActionName": "Demo Request Lead",
                    "conversionActionCategory": "LEAD",
                },
                "metrics": {
                    "conversions": 46.0,
                    "allConversions": 48.0,
                    "conversionsValue": 9200.0,
                    "allConversionsValue": 9600.0,
                    "costPerConversion": 63043478,
                },
            },
            {
                "campaign": raw[1]["campaign"],
                "segments": {
                    "date": today_str,
                    "device": "MOBILE",
                    "conversionAction": "customers/2222222222/conversionActions/2002",
                    "conversionActionName": "Pricing Page View (Secondary)",
                    "conversionActionCategory": "PAGE_VIEW",
                },
                "metrics": {
                    "conversions": 0.0,
                    "allConversions": 137.0,
                    "conversionsValue": 0.0,
                    "allConversionsValue": 1800.0,
                    "costPerConversion": 0,
                },
            },
        ]

    cid_filter = _extract_campaign_id(query)
    if cid_filter:
        raw = [row for row in raw if row["campaign"]["id"] == cid_filter]
    return raw


def _search_term_rows(customer_id: str, query: str) -> list[dict[str, Any]]:
    if customer_id == "1111111111":
        rows = [
            {
                "searchTermView": {"searchTerm": "chaqueta impermeable gratis", "status": "NONE"},
                "campaign": {"id": "103", "name": "ES | Search | Non-Brand Jackets"},
                "adGroup": {"id": "1031", "name": "Waterproof Jackets"},
                "metrics": {"impressions": "920", "clicks": "84", "costMicros": "148000000", "conversions": 0.0, "conversionsValue": 0.0},
            },
            {
                "searchTermView": {"searchTerm": "empleo nordic gear madrid", "status": "NONE"},
                "campaign": {"id": "101", "name": "ES | Search | Brand"},
                "adGroup": {"id": "1011", "name": "Brand Exact"},
                "metrics": {"impressions": "410", "clicks": "39", "costMicros": "62000000", "conversions": 0.0, "conversionsValue": 0.0},
            },
            {
                "searchTermView": {"searchTerm": "nordic gear chaqueta goretex", "status": "ADDED"},
                "campaign": {"id": "101", "name": "ES | Search | Brand"},
                "adGroup": {"id": "1011", "name": "Brand Exact"},
                "metrics": {"impressions": "3100", "clicks": "420", "costMicros": "110000000", "conversions": 28.0, "conversionsValue": 3640.0},
            },
        ]
    else:
        rows = [
            {
                "searchTermView": {"searchTerm": "free open source crm download", "status": "NONE"},
                "campaign": {"id": "202", "name": "US | Search | Competitors Broad"},
                "adGroup": {"id": "2021", "name": "Competitors"},
                "metrics": {"impressions": "1850", "clicks": "145", "costMicros": "390000000", "conversions": 0.0, "conversionsValue": 0.0},
            },
            {
                "searchTermView": {"searchTerm": "crm internship remote", "status": "NONE"},
                "campaign": {"id": "202", "name": "US | Search | Competitors Broad"},
                "adGroup": {"id": "2021", "name": "Competitors"},
                "metrics": {"impressions": "640", "clicks": "58", "costMicros": "165000000", "conversions": 0.0, "conversionsValue": 0.0},
            },
            {
                "searchTermView": {"searchTerm": "enterprise b2b crm pricing", "status": "ADDED"},
                "campaign": {"id": "201", "name": "US | Search | High-Intent CRM"},
                "adGroup": {"id": "2011", "name": "High-Intent Exact"},
                "metrics": {"impressions": "2400", "clicks": "260", "costMicros": "480000000", "conversions": 18.0, "conversionsValue": 3600.0},
            },
        ]
    if "METRICS.CONVERSIONS = 0" in query.upper():
        rows = [row for row in rows if float(row["metrics"]["conversions"]) == 0.0]
    cid_filter = _extract_campaign_id(query)
    if cid_filter:
        rows = [row for row in rows if row["campaign"]["id"] == cid_filter]
    return rows


def _keyword_rows(customer_id: str) -> list[dict[str, Any]]:
    if customer_id == "1111111111":
        return [
            {
                "campaign": {"id": "101", "name": "ES | Search | Brand"},
                "adGroup": {"id": "1011", "name": "Brand Exact"},
                "adGroupCriterion": {
                    "criterionId": "7001",
                    "keyword": {"text": "nordic gear", "matchType": "EXACT"},
                    "qualityInfo": {
                        "qualityScore": 9,
                        "creativeQualityScore": "ABOVE_AVERAGE",
                        "postClickQualityScore": "ABOVE_AVERAGE",
                        "searchPredictedCtr": "ABOVE_AVERAGE",
                    },
                    "effectiveCpcBidMicros": "350000",
                },
                "metrics": {
                    "impressions": "18500",
                    "clicks": "1650",
                    "ctr": 0.089,
                    "averageCpc": 240000,
                    "costMicros": "396000000",
                    "conversions": 44.0,
                    "searchImpressionShare": 0.92,
                    "searchTopImpressionShare": 0.85,
                    "searchAbsoluteTopImpressionShare": 0.68,
                },
            },
            {
                "campaign": {"id": "103", "name": "ES | Search | Non-Brand Jackets"},
                "adGroup": {"id": "1031", "name": "Waterproof Jackets"},
                "adGroupCriterion": {
                    "criterionId": "7002",
                    "keyword": {"text": "chaqueta montaña impermeable", "matchType": "PHRASE"},
                    "qualityInfo": {"qualityScore": 6},
                    "effectiveCpcBidMicros": "1100000",
                },
                "metrics": {
                    "impressions": "14200",
                    "clicks": "410",
                    "ctr": 0.0288,
                    "averageCpc": 920000,
                    "costMicros": "377200000",
                    "conversions": 9.0,
                    "searchImpressionShare": 0.48,
                    "searchTopImpressionShare": 0.34,
                    "searchAbsoluteTopImpressionShare": 0.12,
                },
            },
        ]
    return [
        {
            "campaign": {"id": "201", "name": "US | Search | High-Intent CRM"},
            "adGroup": {"id": "2011", "name": "High-Intent Exact"},
            "adGroupCriterion": {
                "criterionId": "8001",
                "keyword": {"text": "b2b crm software", "matchType": "EXACT"},
                "qualityInfo": {"qualityScore": 8},
                "effectiveCpcBidMicros": "2100000",
            },
            "metrics": {
                "impressions": "16400",
                "clicks": "720",
                "ctr": 0.0439,
                "averageCpc": 1820000,
                "costMicros": "1310400000",
                "conversions": 26.0,
                "searchImpressionShare": 0.58,
                "searchTopImpressionShare": 0.44,
                "searchAbsoluteTopImpressionShare": 0.21,
            },
        },
    ]


def _conversion_action_rows(customer_id: str) -> list[dict[str, Any]]:
    if customer_id == "1111111111":
        return [
            {
                "conversionAction": {
                    "resourceName": "customers/1111111111/conversionActions/1001",
                    "id": "1001",
                    "name": "Purchase (Web)",
                    "category": "PURCHASE",
                    "type": "WEBPAGE",
                    "origin": "WEBSITE",
                    "status": "ENABLED",
                    "primaryForGoal": True,
                    "includeInConversionsMetric": True,
                    "countingType": "MANY_PER_CLICK",
                    "valueSettings": {"defaultValue": 120.0, "defaultCurrencyCode": "EUR"},
                    "attributionModelSettings": {
                        "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                        "dataDrivenModelStatus": "AVAILABLE",
                    },
                    "clickThroughLookbackWindowDays": 30,
                    "viewThroughLookbackWindowDays": 1,
                }
            },
            {
                "conversionAction": {
                    "resourceName": "customers/1111111111/conversionActions/1002",
                    "id": "1002",
                    "name": "Add to Cart (Secondary)",
                    "category": "ADD_TO_CART",
                    "type": "WEBPAGE",
                    "origin": "WEBSITE",
                    "status": "ENABLED",
                    "primaryForGoal": False,
                    "includeInConversionsMetric": False,
                    "countingType": "MANY_PER_CLICK",
                    "valueSettings": {"defaultValue": 0.0, "defaultCurrencyCode": "EUR"},
                    "attributionModelSettings": {
                        "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                        "dataDrivenModelStatus": "AVAILABLE",
                    },
                    "clickThroughLookbackWindowDays": 30,
                    "viewThroughLookbackWindowDays": 1,
                }
            },
            {
                "conversionAction": {
                    "resourceName": "customers/2222222222/conversionActions/2003",
                    "id": "2003",
                    "name": "Qualified SQL (CRM Offline)",
                    "category": "QUALIFIED_LEAD",
                    "type": "UPLOAD_CLICKS",
                    "origin": "CALL_FROM_ADS",
                    "status": "ENABLED",
                    "primaryForGoal": False,
                    "includeInConversionsMetric": False,
                    "countingType": "ONE_PER_CLICK",
                    "valueSettings": {"defaultValue": 500.0, "defaultCurrencyCode": "USD"},
                    "attributionModelSettings": {
                        "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                        "dataDrivenModelStatus": "AVAILABLE",
                    },
                    "clickThroughLookbackWindowDays": 90,
                    "viewThroughLookbackWindowDays": 1,
                }
            },
        ]
    return [
        {
            "conversionAction": {
                "resourceName": "customers/2222222222/conversionActions/2001",
                "id": "2001",
                "name": "Demo Request Lead",
                "category": "LEAD",
                "type": "WEBPAGE",
                "origin": "WEBSITE",
                "status": "ENABLED",
                "primaryForGoal": True,
                "includeInConversionsMetric": True,
                "countingType": "ONE_PER_CLICK",
                "valueSettings": {"defaultValue": 200.0, "defaultCurrencyCode": "USD"},
                "attributionModelSettings": {
                    "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                    "dataDrivenModelStatus": "AVAILABLE",
                },
                "clickThroughLookbackWindowDays": 30,
                "viewThroughLookbackWindowDays": 1,
            }
        },
        {
            "conversionAction": {
                "resourceName": "customers/2222222222/conversionActions/2002",
                "id": "2002",
                "name": "Pricing Page View (Secondary)",
                "category": "PAGE_VIEW",
                "type": "WEBPAGE",
                "origin": "WEBSITE",
                "status": "ENABLED",
                "primaryForGoal": False,
                "includeInConversionsMetric": False,
                "countingType": "ONE_PER_CLICK",
                "valueSettings": {"defaultValue": 0.0, "defaultCurrencyCode": "USD"},
                "attributionModelSettings": {
                    "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_LAST_CLICK",
                    "dataDrivenModelStatus": "STALE",
                },
                "clickThroughLookbackWindowDays": 30,
                "viewThroughLookbackWindowDays": 1,
            }
        },
    ]


def _ad_rows(customer_id: str) -> list[dict[str, Any]]:
    if customer_id == "1111111111":
        return [
            {
                "campaign": {"id": "101", "name": "ES | Search | Brand"},
                "adGroup": {"id": "1011", "name": "Brand Exact"},
                "adGroupAd": {
                    "status": "ENABLED",
                    "adStrength": "EXCELLENT",
                    "ad": {
                        "id": "9001",
                        "type": "RESPONSIVE_SEARCH_AD",
                        "finalUrls": ["https://example.com/nordic-gear"],
                        "responsiveSearchAd": {
                            "headlines": [
                                {"text": "Nordic Gear Oficial", "assetPerformanceLabel": "BEST", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                                {"text": "Chaquetas Técnicas de Montaña", "assetPerformanceLabel": "GOOD", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                                {"text": "Envío Gratis en 24h", "assetPerformanceLabel": "GOOD", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                            ],
                            "descriptions": [
                                {"text": "Equipamiento técnico impermeable para montaña con 5 años de garantía.", "assetPerformanceLabel": "BEST", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                                {"text": "Compra directo en la tienda oficial con devolución gratuita.", "assetPerformanceLabel": "GOOD", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                            ],
                            "path1": "outdoor",
                            "path2": "oficial",
                        },
                    },
                },
                "metrics": {"impressions": "19500", "clicks": "1710", "costMicros": "420000000", "conversions": 48.0},
            }
        ]
    return [
        {
            "campaign": {"id": "201", "name": "US | Search | High-Intent CRM"},
            "adGroup": {"id": "2011", "name": "High-Intent Exact"},
            "adGroupAd": {
                "status": "ENABLED",
                "adStrength": "AVERAGE",
                "ad": {
                    "id": "9002",
                    "type": "RESPONSIVE_SEARCH_AD",
                    "finalUrls": ["https://example.com/apex-crm"],
                    "responsiveSearchAd": {
                        "headlines": [
                            {"text": "Apex B2B CRM", "assetPerformanceLabel": "GOOD", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                            {"text": "Close Deals Faster", "assetPerformanceLabel": "LOW", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                        ],
                        "descriptions": [
                            {"text": "Built for modern B2B revenue teams. Book a live demo today.", "assetPerformanceLabel": "GOOD", "policySummaryInfo": {"approvalStatus": "APPROVED"}},
                        ],
                        "path1": "crm",
                        "path2": "demo",
                    },
                },
            },
            "metrics": {"impressions": "21000", "clicks": "890", "costMicros": "1580000000", "conversions": 29.0},
        }
    ]


def demo_search(customer_id: str, query: str) -> list[dict[str, Any]]:
    """Return deterministic synthetic GAQL rows for demo accounts."""
    normalized = "".join(ch for ch in str(customer_id) if ch.isdigit())
    if normalized not in DEMO_ACCOUNTS:
        raise PermissionError(
            f"Demo mode only includes accounts {', '.join(DEMO_CUSTOMER_IDS)}; got {normalized}"
        )
    resource = _extract_resource(query)
    if resource == "customer":
        return _customer_rows(normalized, query)
    if resource == "campaign":
        return _campaign_rows(normalized, query)
    if resource == "search_term_view":
        return _search_term_rows(normalized, query)
    if resource == "keyword_view":
        return _keyword_rows(normalized)
    if resource == "conversion_action":
        return _conversion_action_rows(normalized)
    if resource == "ad_group_ad":
        return _ad_rows(normalized)
    if resource == "ad_group":
        return [
            {
                "campaign": row["campaign"],
                "adGroup": row["adGroup"],
                "metrics": row["metrics"],
            }
            for row in _ad_rows(normalized)
        ]
    if resource == "recommendation":
        return [
            {
                "recommendation": {
                    "resourceName": f"customers/{normalized}/recommendations/demo-rec-1",
                    "type": "CAMPAIGN_BUDGET",
                    "dismissed": False,
                }
            }
        ]
    if resource == "bidding_strategy":
        return [
            {
                "biddingStrategy": {
                    "id": "3001",
                    "name": "Target ROAS Core" if normalized == "1111111111" else "Target CPA Leads",
                    "type": "TARGET_ROAS" if normalized == "1111111111" else "TARGET_CPA",
                    "status": "ENABLED",
                    "campaignCount": "2",
                    "effectiveCurrencyCode": DEMO_ACCOUNTS[normalized]["currency_code"],
                }
            }
        ]
    if resource == "customer_conversion_goal":
        if normalized == "1111111111":
            return [
                {"customerConversionGoal": {"category": "PURCHASE", "origin": "WEBSITE", "biddable": True}},
                {"customerConversionGoal": {"category": "ADD_TO_CART", "origin": "WEBSITE", "biddable": False}},
            ]
        return [
            {"customerConversionGoal": {"category": "LEAD", "origin": "WEBSITE", "biddable": True}},
            {"customerConversionGoal": {"category": "PAGE_VIEW", "origin": "WEBSITE", "biddable": False}},
        ]
    if resource == "campaign_conversion_goal":
        return []
    if resource == "custom_conversion_goal":
        if normalized == "2222222222":
            return [
                {
                    "customConversionGoal": {
                        "resourceName": "customers/2222222222/customConversionGoals/5001",
                        "id": "5001",
                        "name": "SQL Pipeline Goal",
                        "status": "ENABLED",
                        "conversionActions": [
                            "customers/2222222222/conversionActions/2001",
                            "customers/2222222222/conversionActions/2003",
                        ],
                    }
                }
            ]
        return []
    if resource == "conversion_goal_campaign_config":
        if normalized == "2222222222":
            return [
                {
                    "campaign": {"id": "201"},
                    "conversionGoalCampaignConfig": {
                        "goalConfigLevel": "CAMPAIGN",
                        "customConversionGoal": "customers/2222222222/customConversionGoals/5001",
                    },
                },
                {
                    "campaign": {"id": "202"},
                    "conversionGoalCampaignConfig": {
                        "goalConfigLevel": "CUSTOMER",
                    },
                },
            ]
        return []
    if resource == "offline_conversion_upload_client_summary":
        if normalized == "2222222222":
            return [
                {
                    "offlineConversionUploadClientSummary": {
                        "client": "GOOGLE_ADS_API",
                        "status": "NEEDS_ATTENTION",
                        "totalEventCount": "120",
                        "successfulEventCount": "102",
                        "pendingEventCount": "4",
                        "lastUploadDateTime": "2026-11-19 18:30:00",
                        "alerts": [
                            {"error": "EXPIRED_EVENT", "errorPercentage": 0.1167},
                        ],
                    }
                }
            ]
        return []
    if resource == "offline_conversion_upload_conversion_action_summary":
        if normalized == "2222222222":
            return [
                {
                    "offlineConversionUploadConversionActionSummary": {
                        "conversionActionId": "2003",
                        "conversionActionName": "Qualified SQL (CRM Offline)",
                        "client": "GOOGLE_ADS_API",
                        "status": "NEEDS_ATTENTION",
                        "totalEventCount": "120",
                        "successfulEventCount": "102",
                        "pendingEventCount": "4",
                        "lastUploadDateTime": "2026-11-19 18:30:00",
                        "alerts": [
                            {"error": "EXPIRED_EVENT", "errorPercentage": 0.1167},
                        ],
                    }
                }
            ]
        return []
    if resource == "billing_setup":
        return [
            {
                "billingSetup": {
                    "id": "9100",
                    "status": "APPROVED",
                    "paymentsAccount": f"customers/{normalized}/paymentsAccounts/1000-2000-3000-4000",
                }
            }
        ]
    if resource == "account_budget":
        return [
            {
                "accountBudget": {
                    "id": "9200",
                    "name": "Annual Master Budget",
                    "status": "APPROVED",
                    "approvedSpendingLimitMicros": "500000000000",
                    "amountServedMicros": "120000000000",
                }
            }
        ]
    if resource == "landing_page_view":
        url = (
            "https://example.com/nordic-gear/jackets"
            if normalized == "1111111111"
            else "https://example.com/apex-crm/demo"
        )
        return [
            {
                "landingPageView": {"unexpandedFinalUrl": url},
                "campaign": {"id": "101" if normalized == "1111111111" else "201", "name": "Core"},
                "metrics": {
                    "impressions": "18000",
                    "clicks": "1250",
                    "ctr": 0.0694,
                    "costMicros": "620000000",
                    "conversions": 42.0,
                    "conversionsValue": 5400.0,
                    "mobileFriendlyClicksPercentage": 0.96,
                    "speedScore": 8,
                },
            }
        ]
    if resource == "age_range_view":
        return [
            {
                "adGroupCriterion": {"ageRange": {"type": "AGE_RANGE_25_34"}},
                "campaign": {"id": "101", "name": "Core"},
                "metrics": {"impressions": "12000", "clicks": "680", "ctr": 0.0567, "costMicros": "320000000", "conversions": 22.0, "conversionsValue": 2900.0},
            },
            {
                "adGroupCriterion": {"ageRange": {"type": "AGE_RANGE_35_44"}},
                "campaign": {"id": "101", "name": "Core"},
                "metrics": {"impressions": "10500", "clicks": "540", "ctr": 0.0514, "costMicros": "280000000", "conversions": 18.0, "conversionsValue": 2400.0},
            },
        ]
    if resource == "gender_view":
        return [
            {
                "adGroupCriterion": {"gender": {"type": "FEMALE"}},
                "campaign": {"id": "101", "name": "Core"},
                "metrics": {"impressions": "11500", "clicks": "610", "ctr": 0.053, "costMicros": "295000000", "conversions": 21.0, "conversionsValue": 2700.0},
            },
            {
                "adGroupCriterion": {"gender": {"type": "MALE"}},
                "campaign": {"id": "101", "name": "Core"},
                "metrics": {"impressions": "11000", "clicks": "610", "ctr": 0.0554, "costMicros": "305000000", "conversions": 19.0, "conversionsValue": 2600.0},
            },
        ]
    if resource in {"income_range_view", "parental_status_view"}:
        return []
    if resource == "campaign_asset":
        return [
            {
                "campaign": {"id": "101" if normalized == "1111111111" else "201", "name": "Core"},
                "campaignAsset": {"fieldType": "SITELINK", "status": "ENABLED"},
                "asset": {
                    "id": "8101",
                    "name": "Pricing Sitelink",
                    "type": "SITELINK",
                    "policySummary": {"approvalStatus": "APPROVED"},
                },
                "metrics": {"impressions": "9500", "clicks": "410", "costMicros": "140000000", "conversions": 12.0, "conversionsValue": 1600.0},
            },
            {
                "campaign": {"id": "103" if normalized == "1111111111" else "202", "name": "Expansion"},
                "campaignAsset": {"fieldType": "CALLOUT", "status": "ENABLED"},
                "asset": {
                    "id": "8102",
                    "name": "Promo Callout",
                    "type": "CALLOUT",
                    "policySummary": {"approvalStatus": "DISAPPROVED"},
                },
                "metrics": {"impressions": "0", "clicks": "0", "costMicros": "0", "conversions": 0.0, "conversionsValue": 0.0},
            },
        ]
    if resource == "call_view":
        return [
            {
                "campaign": {"id": "201", "name": "US | Search | High-Intent CRM"},
                "callView": {
                    "callStatus": "RECEIVED",
                    "type": "HIGH_END_MOBILE_SEARCH",
                    "callDurationSeconds": "145",
                    "callTrackingDisplayLocation": "AD",
                },
            }
        ]
    if resource == "asset_group":
        if normalized == "1111111111":
            return [
                {
                    "campaign": {"id": "102", "name": "ES | PMax | Core Catalog"},
                    "assetGroup": {
                        "id": "4001",
                        "name": "Winter Jackets",
                        "status": "ENABLED",
                        "adStrength": "GOOD",
                        "primaryStatus": "ELIGIBLE",
                        "primaryStatusReasons": [],
                    },
                    "metrics": {
                        "impressions": "52000",
                        "clicks": "1640",
                        "costMicros": "1450000000",
                        "conversions": 58.0,
                        "conversionsValue": 7100.0,
                    },
                },
                {
                    "campaign": {"id": "102", "name": "ES | PMax | Core Catalog"},
                    "assetGroup": {
                        "id": "4002",
                        "name": "Accessories Clearance",
                        "status": "ENABLED",
                        "adStrength": "POOR",
                        "primaryStatus": "LIMITED",
                        "primaryStatusReasons": ["ASSET_GROUP_LIMITED"],
                    },
                    "metrics": {
                        "impressions": "14000",
                        "clicks": "390",
                        "costMicros": "320000000",
                        "conversions": 0.0,
                        "conversionsValue": 0.0,
                    },
                },
            ]
        return []
    if resource == "asset_group_asset":
        if normalized == "1111111111":
            return [
                {
                    "assetGroup": {"id": "4001"},
                    "assetGroupAsset": {
                        "fieldType": "HEADLINE",
                        "status": "ENABLED",
                        "primaryStatus": "ELIGIBLE",
                        "primaryStatusReasons": [],
                        "policySummary": {"approvalStatus": "APPROVED"},
                    },
                    "asset": {"id": "5101", "name": "GoreTex Headline", "type": "TEXT"},
                },
                {
                    "assetGroup": {"id": "4002"},
                    "assetGroupAsset": {
                        "fieldType": "MARKETING_IMAGE",
                        "status": "ENABLED",
                        "primaryStatus": "NOT_ELIGIBLE",
                        "primaryStatusReasons": ["ASSET_DISAPPROVED"],
                        "policySummary": {"approvalStatus": "DISAPPROVED"},
                    },
                    "asset": {"id": "5102", "name": "Old Banner", "type": "IMAGE"},
                },
            ]
        return []
    if resource == "asset_group_signal":
        if normalized == "1111111111":
            return [
                {
                    "assetGroup": {"id": "4001"},
                    "assetGroupSignal": {"searchTheme": {"text": "chaqueta montaña impermeable"}},
                }
            ]
        return []
    if resource == "asset_group_listing_group_filter":
        if normalized == "1111111111":
            return [{"assetGroup": {"id": "4001"}, "assetGroupListingGroupFilter": {"type": "UNIT_INCLUDED", "listingSource": "SHOPPING"}}]
        return []
    if resource == "campaign_search_term_insight":
        if normalized == "1111111111":
            return [
                {
                    "campaign": {"id": "102"},
                    "campaignSearchTermInsight": {
                        "campaignId": "102",
                        "categoryLabel": "chaquetas impermeables",
                    },
                    "metrics": {"impressions": "21000", "clicks": "840", "conversions": 34.0, "conversionsValue": 4300.0},
                }
            ]
        return []
    if resource == "performance_max_placement_view":
        if normalized == "1111111111":
            return [
                {
                    "performanceMaxPlacementView": {
                        "displayName": "Mobile App: casual-puzzle-game",
                        "placement": "mobileapp::2-com.example.puzzle",
                        "placementType": "MOBILE_APPLICATION",
                    },
                    "metrics": {"impressions": "18000"},
                },
                {
                    "performanceMaxPlacementView": {
                        "displayName": "outdoor-magazine.example.com",
                        "placement": "outdoor-magazine.example.com",
                        "placementType": "WEBSITE",
                    },
                    "metrics": {"impressions": "12000"},
                },
            ]
        return []
    if resource == "shopping_performance_view":
        if normalized == "1111111111":
            return [
                {
                    "segments": {"productItemId": "SKU-ALPINE-01", "productTitle": "Alpine Pro Jacket", "productBrand": "Nordic Gear", "productTypeL1": "Jackets"},
                    "metrics": {"impressions": "22000", "clicks": "920", "costMicros": "640000000", "conversions": 36.0, "conversionsValue": 5400.0},
                },
                {
                    "segments": {"productItemId": "SKU-SOCK-99", "productTitle": "Basic Cotton Socks", "productBrand": "Nordic Gear", "productTypeL1": "Accessories"},
                    "metrics": {"impressions": "11500", "clicks": "340", "costMicros": "210000000", "conversions": 0.0, "conversionsValue": 0.0},
                },
            ]
        return []
    if resource == "detail_placement_view":
        return [
            {
                "campaign": {"id": "102" if normalized == "1111111111" else "202", "name": "Awareness"},
                "detailPlacementView": {
                    "displayName": "Mobile App: Flashlight Free",
                    "placement": "mobileapp::2-com.example.flashlight",
                    "placementType": "MOBILE_APPLICATION",
                    "targetUrl": "https://play.google.com/store/apps/details?id=com.example.flashlight",
                },
                "metrics": {"impressions": "15000", "clicks": "180", "costMicros": "95000000", "conversions": 0.0, "conversionsValue": 0.0},
            },
            {
                "campaign": {"id": "102" if normalized == "1111111111" else "202", "name": "Awareness"},
                "detailPlacementView": {
                    "displayName": "tech-review.example.com",
                    "placement": "tech-review.example.com",
                    "placementType": "WEBSITE",
                    "targetUrl": "https://tech-review.example.com",
                },
                "metrics": {"impressions": "9000", "clicks": "140", "costMicros": "70000000", "conversions": 4.0, "conversionsValue": 600.0},
            },
        ]
    return []
