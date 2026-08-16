"""Search terms clustering + negative keyword suggestions.

`detect_waste` returns a flat list of non-converting search terms.
That's fine for eyeball review, but a cluster view saves hours per
account per month: 40 similar terms grouped as one "auditoría seo
gratis*" pattern become one negative decision instead of 40.

Two tools:
- analyze_search_terms_clusters — Jaccard-based grouping of
  zero-conversion terms, sorted by wasted spend.
- suggest_negatives_from_clusters — payload ready for
  add_negative_keywords_bulk (dry-run only; user still calls the
  existing negative-keywords tool to actually apply them).

No external ML deps. Tokenization + set intersection are enough for
this scale (accounts rarely have >1000 unique zero-conv terms in a
30d window).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, search
from .config import ensure_customer_allowed, ensure_tool_allowed
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_TOKEN_RE = re.compile(r"[a-z0-9áéíóúñü]+", re.UNICODE)
# Common Spanish + English stopwords that don't carry intent
_STOPWORDS = frozenset(
    {
        "a", "al", "de", "del", "el", "en", "la", "las", "lo", "los",
        "un", "una", "unos", "unas", "y", "o", "u", "que", "para", "por",
        "con", "sin", "sobre", "es", "son", "se", "su", "sus",
        "the", "a", "an", "of", "in", "on", "for", "to", "by", "with",
        "and", "or", "is", "are", "be", "as", "at",
    }
)
_MIN_TOKEN_LEN = 2
_MAX_TERMS_TO_CLUSTER = 2000
_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})


def _tokens(text: str) -> frozenset[str]:
    """Return non-stopword tokens length >= 2, lowercased."""
    if not text:
        return frozenset()
    lowered = text.lower()
    raw = _TOKEN_RE.findall(lowered)
    return frozenset(
        t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = a & b
    if not intersection:
        return 0.0
    union = a | b
    return len(intersection) / len(union)


def _cluster_terms(
    terms: list[dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    """Agglomerative single-linkage on token Jaccard until nothing merges.

    Each input term must have {'search_term', 'tokens', 'metrics', ...}.
    Returns clusters as {'terms': [...], 'shared_tokens': set, 'metrics': totals}.
    """
    if not terms:
        return []
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")

    clusters: list[dict[str, Any]] = [
        {
            "terms": [term],
            "token_union": set(term["tokens"]),
            "token_intersection": set(term["tokens"]),
        }
        for term in terms
    ]

    merged = True
    while merged and len(clusters) > 1:
        merged = False
        best_i, best_j, best_score = -1, -1, threshold
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                score = _jaccard(
                    frozenset(clusters[i]["token_union"]),
                    frozenset(clusters[j]["token_union"]),
                )
                if score >= best_score:
                    best_score = score
                    best_i, best_j = i, j
        if best_i < 0:
            break
        # merge j into i
        clusters[best_i]["terms"].extend(clusters[best_j]["terms"])
        clusters[best_i]["token_union"] |= clusters[best_j]["token_union"]
        clusters[best_i]["token_intersection"] &= clusters[best_j]["token_intersection"]
        del clusters[best_j]
        merged = True

    # Compute per-cluster totals and shared tokens
    results: list[dict[str, Any]] = []
    for cluster in clusters:
        totals = {
            "term_count": len(cluster["terms"]),
            "impressions": 0,
            "clicks": 0,
            "cost_micros": 0,
        }
        for t in cluster["terms"]:
            metrics = t["metrics"]
            totals["impressions"] += metrics["impressions"]
            totals["clicks"] += metrics["clicks"]
            totals["cost_micros"] += metrics["cost_micros"]
        shared = sorted(cluster["token_intersection"])
        results.append(
            {
                "shared_tokens": shared,
                "pattern": " ".join(shared) if shared else "(no shared tokens)",
                "totals": totals,
                "top_terms": sorted(
                    cluster["terms"],
                    key=lambda t: t["metrics"]["cost_micros"],
                    reverse=True,
                )[:5],
                "all_terms": [t["search_term"] for t in cluster["terms"]],
            }
        )
    results.sort(key=lambda c: c["totals"]["cost_micros"], reverse=True)
    return results


def _fetch_zero_conv_terms(
    customer_id: str,
    days: int,
    min_cost_micros: int,
    campaign_id: Optional[str],
) -> list[dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    date_clause = (
        f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )
    filters = [
        date_clause,
        "metrics.conversions = 0",
        f"metrics.cost_micros >= {int(min_cost_micros)}",
    ]
    if campaign_id:
        clean = str(campaign_id).strip()
        if not clean.isdigit() or int(clean) <= 0:
            raise ValueError("campaign_id must be a positive integer")
        filters.append(f"campaign.id = {int(clean)}")
    query = f"""
        SELECT search_term_view.search_term, search_term_view.status,
               campaign.id, campaign.name, ad_group.id, ad_group.name,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions
        FROM search_term_view
        WHERE {' AND '.join(filters)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {_MAX_TERMS_TO_CLUSTER}
    """
    rows = search(customer_id, query)
    terms: list[dict[str, Any]] = []
    for row in rows:
        stv = row.get("searchTermView", {}) or {}
        term_text = stv.get("searchTerm") or ""
        metrics = row.get("metrics", {}) or {}
        campaign = row.get("campaign", {}) or {}
        ad_group = row.get("adGroup", {}) or {}
        terms.append(
            {
                "search_term": term_text,
                "tokens": _tokens(term_text),
                "status": stv.get("status"),
                "campaign_id": campaign.get("id"),
                "campaign_name": campaign.get("name"),
                "ad_group_id": ad_group.get("id"),
                "ad_group_name": ad_group.get("name"),
                "metrics": {
                    "impressions": int(metrics.get("impressions") or 0),
                    "clicks": int(metrics.get("clicks") or 0),
                    "cost_micros": int(metrics.get("costMicros") or 0),
                },
            }
        )
    return terms


def register_waste_intelligence_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def analyze_search_terms_clusters(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
        min_cost_micros: int = Field(
            default=0,
            description="Only include terms with cost_micros >= this value",
            ge=0,
        ),
        similarity_threshold: float = Field(
            default=0.5,
            description="Jaccard threshold for merging clusters (0-1). Higher = stricter groups.",
        ),
        campaign_id: Optional[str] = Field(
            default=None, description="Optional campaign filter"
        ),
    ) -> str:
        """Cluster zero-conversion search terms by token similarity.

        Groups are formed by agglomerative single-linkage over Jaccard
        similarity of tokenized terms (stopwords filtered). Sorted by
        wasted spend descending — the first cluster is where negatives
        will save the most money.
        """
        ensure_tool_allowed("analyze_search_terms_clusters")
        cid = ensure_customer_allowed(customer_id)
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")

        terms = _fetch_zero_conv_terms(cid, days, min_cost_micros, campaign_id)
        clusters = _cluster_terms(terms, threshold=float(similarity_threshold))
        return format_json(
            {
                "customer_id": cid,
                "tool": "analyze_search_terms_clusters",
                "fresh": True,
                "filter": {
                    "days": days,
                    "min_cost_micros": int(min_cost_micros),
                    "similarity_threshold": float(similarity_threshold),
                    "campaign_id": campaign_id,
                },
                "summary": {
                    "term_count": len(terms),
                    "cluster_count": len(clusters),
                    "total_wasted_cost_micros": sum(
                        c["totals"]["cost_micros"] for c in clusters
                    ),
                    "term_cap": _MAX_TERMS_TO_CLUSTER,
                },
                "clusters": clusters,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def suggest_negatives_from_clusters(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
        min_cost_micros: int = Field(
            default=0,
            description="Only include terms with cost_micros >= this value",
            ge=0,
        ),
        similarity_threshold: float = Field(default=0.5),
        match_type: str = Field(
            default="PHRASE",
            description="Match type for suggested negatives: EXACT, PHRASE or BROAD",
        ),
        max_clusters: int = Field(
            default=25,
            description="Max top-cost clusters to convert into suggestions",
            ge=1,
            le=200,
        ),
        campaign_id: Optional[str] = Field(default=None),
    ) -> str:
        """Return negative-keyword suggestions ready for add_negative_keywords_bulk.

        For each top cluster, emits one suggestion whose text is the
        shared token pattern (or the highest-cost term if the cluster
        has no shared tokens). This is a preview only — apply with
        add_negative_keywords_bulk after operator review.
        """
        ensure_tool_allowed("suggest_negatives_from_clusters")
        cid = ensure_customer_allowed(customer_id)
        match = (match_type or "").strip().upper()
        if match not in _MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(_MATCH_TYPES)}")
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")

        terms = _fetch_zero_conv_terms(cid, days, min_cost_micros, campaign_id)
        clusters = _cluster_terms(terms, threshold=float(similarity_threshold))
        top_clusters = clusters[: int(max_clusters)]
        suggestions: list[dict[str, Any]] = []
        for cluster in top_clusters:
            if cluster["shared_tokens"]:
                text = " ".join(cluster["shared_tokens"])
                basis = "shared_tokens"
            else:
                # Fall back to the highest-cost term
                text = cluster["top_terms"][0]["search_term"] if cluster["top_terms"] else ""
                basis = "highest_cost_term"
            suggestions.append(
                {
                    "keyword_text": text,
                    "match_type": match,
                    "basis": basis,
                    "cluster_size": cluster["totals"]["term_count"],
                    "wasted_cost_micros": cluster["totals"]["cost_micros"],
                    "wasted_clicks": cluster["totals"]["clicks"],
                    "example_terms": [t["search_term"] for t in cluster["top_terms"]],
                }
            )
        return format_json(
            {
                "customer_id": cid,
                "tool": "suggest_negatives_from_clusters",
                "fresh": True,
                "filter": {
                    "days": days,
                    "min_cost_micros": int(min_cost_micros),
                    "similarity_threshold": float(similarity_threshold),
                    "match_type": match,
                    "campaign_id": campaign_id,
                    "max_clusters": int(max_clusters),
                },
                "summary": {
                    "total_terms_analyzed": len(terms),
                    "clusters_found": len(clusters),
                    "suggestions_returned": len(suggestions),
                    "total_wasted_cost_micros_in_suggestions": sum(
                        s["wasted_cost_micros"] for s in suggestions
                    ),
                },
                "suggestions": suggestions,
                "next_step": (
                    "Review each suggestion. Apply with add_negative_keywords_bulk "
                    "or add_negative_keyword after selecting the right scope "
                    "(campaign_id or shared negative list)."
                ),
            }
        )
