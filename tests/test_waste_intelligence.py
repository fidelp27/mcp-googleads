"""Tests for waste_intelligence tokenizer + clustering helpers."""
from __future__ import annotations

import pytest

from agency import waste_intelligence as wi


# ---------- _tokens ----------


def test_tokens_lowercases_and_splits():
    assert wi._tokens("Auditoría SEO Precio") == frozenset(
        {"auditoría", "seo", "precio"}
    )


def test_tokens_drops_stopwords_and_short():
    assert wi._tokens("a la web para pyme") == frozenset({"web", "pyme"})


def test_tokens_empty_string():
    assert wi._tokens("") == frozenset()


def test_tokens_handles_accents_and_ñ():
    assert wi._tokens("año ñato") == frozenset({"año", "ñato"})


def test_tokens_ignores_punctuation():
    assert wi._tokens("audit-seo, cheap!") == frozenset({"audit", "seo", "cheap"})


# ---------- _jaccard ----------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (frozenset(), frozenset({"x"}), 0.0),
        (frozenset({"x"}), frozenset({"x"}), 1.0),
        (frozenset({"a", "b"}), frozenset({"b", "c"}), 1 / 3),
        (frozenset({"a", "b"}), frozenset({"c", "d"}), 0.0),
        (frozenset({"a", "b", "c"}), frozenset({"a", "b"}), 2 / 3),
    ],
)
def test_jaccard(a, b, expected):
    assert wi._jaccard(a, b) == pytest.approx(expected)


# ---------- _cluster_terms ----------


def _term(text, cost=1_000_000, impressions=100, clicks=5):
    return {
        "search_term": text,
        "tokens": wi._tokens(text),
        "metrics": {
            "impressions": impressions,
            "clicks": clicks,
            "cost_micros": cost,
        },
    }


def test_cluster_empty_returns_empty():
    assert wi._cluster_terms([], threshold=0.5) == []


def test_cluster_rejects_bad_threshold():
    with pytest.raises(ValueError):
        wi._cluster_terms([_term("x")], threshold=1.5)


def test_cluster_no_merges_when_terms_are_disjoint():
    terms = [
        _term("auditoría seo precio"),
        _term("hoteles baratos madrid"),
    ]
    clusters = wi._cluster_terms(terms, threshold=0.5)
    assert len(clusters) == 2


def test_cluster_merges_similar_terms():
    terms = [
        _term("auditoría seo precio", cost=1_000_000),
        _term("auditoría seo precios", cost=2_000_000),
        _term("auditoría seo barata", cost=500_000),
    ]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["totals"]["term_count"] == 3
    assert cluster["totals"]["cost_micros"] == 3_500_000
    assert "auditoría" in cluster["shared_tokens"]
    assert "seo" in cluster["shared_tokens"]


def test_cluster_sorted_by_wasted_spend():
    terms = [
        _term("apple orange", cost=100),
        _term("apple pear", cost=100),
        _term("bmw car cheap", cost=1_000_000),
        _term("bmw motorcycle cheap", cost=2_000_000),
    ]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    # bmw cluster (3M cost) should come before apple cluster (200 cost)
    assert clusters[0]["totals"]["cost_micros"] > clusters[-1]["totals"]["cost_micros"]
    assert "bmw" in clusters[0]["shared_tokens"]


def test_cluster_top_terms_sorted_by_cost():
    terms = [
        _term("audit seo pdf", cost=100),
        _term("audit seo report", cost=500_000),
        _term("audit seo tool", cost=200_000),
    ]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    top_terms = clusters[0]["top_terms"]
    assert [t["search_term"] for t in top_terms] == [
        "audit seo report",
        "audit seo tool",
        "audit seo pdf",
    ]


def test_cluster_shared_tokens_empty_when_all_disjoint_but_transitive_merge():
    """Higher threshold keeps single-term clusters when no pair meets it."""
    terms = [
        _term("apple orange"),
        _term("apple lemon"),
        _term("banana grape"),
    ]
    # Strict threshold: apple pairs share 1/3 = 0.33; not merged at 0.5
    clusters = wi._cluster_terms(terms, threshold=0.5)
    assert len(clusters) == 3


def test_cluster_intersection_survives_multi_merge():
    """Every term shares 'seo audit'; intersection should preserve it."""
    terms = [
        _term("seo audit precio"),
        _term("seo audit barato"),
        _term("seo audit pdf"),
    ]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    assert len(clusters) == 1
    assert set(clusters[0]["shared_tokens"]) == {"seo", "audit"}


def test_cluster_top_terms_capped_at_five():
    # 6+ terms that all share 4 tokens ("seo audit precio option") so every
    # pair has jaccard >= 4/6 = 0.67 and merges into a single cluster.
    terms = [
        _term(f"seo audit precio option {suffix}", cost=(i + 1) * 100)
        for i, suffix in enumerate(["one", "two", "three", "four", "five", "six", "seven"])
    ]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    assert len(clusters) == 1
    assert len(clusters[0]["top_terms"]) == 5


def test_cluster_all_terms_flat_list():
    terms = [_term("seo audit precio"), _term("seo audit barato")]
    clusters = wi._cluster_terms(terms, threshold=0.3)
    assert set(clusters[0]["all_terms"]) == {"seo audit precio", "seo audit barato"}


def test_cluster_pattern_placeholder_when_no_shared():
    terms = [_term("aa bb"), _term("cc dd")]
    # Force a merge with threshold 0 to test the empty-intersection path
    clusters = wi._cluster_terms(terms, threshold=0.0)
    # threshold 0 will merge everything with any overlap; disjoint returns two clusters
    # (jaccard = 0, which is not >= 0.0 in our logic? actually 0 >= 0 is True)
    # Since 0 >= 0 our loop merges them; verify pattern falls back gracefully
    assert len(clusters) == 1
    assert clusters[0]["pattern"] == "(no shared tokens)"
