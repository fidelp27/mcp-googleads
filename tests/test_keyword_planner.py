"""Unit tests for the pure helpers in agency.keyword_planner_tools.

These do not hit the Google Ads API — they validate input parsing and
payload shape so regressions in the tool signatures show up in CI.
"""
from __future__ import annotations

import pytest

from agency import keyword_planner_tools as kp


# ------------- _split_list -------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, []),
        ("", []),
        ("auditoria seo", ["auditoria seo"]),
        ("a, b ,c", ["a", "b", "c"]),
        (["a", " b ", ""], ["a", "b"]),
        (("a", "b"), ["a", "b"]),
    ],
)
def test_split_list_accepts_str_or_iterable(value, expected):
    assert kp._split_list(value) == expected


def test_split_list_rejects_other_types():
    with pytest.raises(ValueError):
        kp._split_list(123)


# ------------- resource builders -------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1003, "languageConstants/1003"),
        ("1003", "languageConstants/1003"),
        ("languageConstants/1003", "languageConstants/1003"),
    ],
)
def test_language_resource_normalizes(raw, expected):
    assert kp._language_resource(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc"])
def test_language_resource_rejects_non_numeric(raw):
    with pytest.raises(ValueError):
        kp._language_resource(raw)


def test_geo_resources_accepts_csv_and_list():
    assert kp._geo_resources("2032, 2484") == [
        "geoTargetConstants/2032",
        "geoTargetConstants/2484",
    ]
    assert kp._geo_resources(["2032", 2484]) == [
        "geoTargetConstants/2032",
        "geoTargetConstants/2484",
    ]


def test_geo_resources_rejects_empty():
    with pytest.raises(ValueError):
        kp._geo_resources("")


def test_geo_resources_rejects_non_numeric():
    with pytest.raises(ValueError):
        kp._geo_resources("2032,ABC")


# ------------- enum validation -------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("google_search", "GOOGLE_SEARCH"), ("GOOGLE_SEARCH_AND_PARTNERS", "GOOGLE_SEARCH_AND_PARTNERS")],
)
def test_validate_network(raw, expected):
    assert kp._validate_network(raw) == expected


def test_validate_network_rejects_unknown():
    with pytest.raises(ValueError):
        kp._validate_network("DISPLAY")


def test_validate_annotations_accepts_known():
    assert kp._validate_annotations("KEYWORD_CONCEPT") == ["KEYWORD_CONCEPT"]
    assert kp._validate_annotations(["keyword_concept", "unknown"]) == [
        "KEYWORD_CONCEPT",
        "UNKNOWN",
    ]


def test_validate_annotations_rejects_bad_value():
    with pytest.raises(ValueError):
        kp._validate_annotations("KEYWORD_MOOD")


# ------------- page size & year-month -------------


@pytest.mark.parametrize(("value", "ceiling", "expected"), [(1, 1000, 1), (50, 100, 50), (5000, 1000, 1000)])
def test_clamp_page_size(value, ceiling, expected):
    assert kp._clamp_page_size(value, ceiling) == expected


@pytest.mark.parametrize("value", [0, -1, "abc"])
def test_clamp_page_size_rejects_bad(value):
    with pytest.raises(ValueError):
        kp._clamp_page_size(value, 100)


def test_yearmonth_parses_valid_month():
    assert kp._yearmonth("2026-03", field="year_month_start") == {"year": 2026, "month": "MARCH"}


@pytest.mark.parametrize("bad", ["2026-13", "2026/03", "2026-3", "abc"])
def test_yearmonth_rejects_invalid(bad):
    with pytest.raises(ValueError):
        kp._yearmonth(bad, field="year_month_start")


def test_yearmonth_returns_none_for_empty():
    assert kp._yearmonth(None, field="x") is None
    assert kp._yearmonth("", field="x") is None


# ------------- seed builder -------------


def test_seed_requires_keywords_or_url():
    with pytest.raises(ValueError):
        kp._seed(None, None)
    with pytest.raises(ValueError):
        kp._seed("", "")


def test_seed_uses_keyword_seed_only():
    assert kp._seed("a, b", None) == {"keywordSeed": {"keywords": ["a", "b"]}}


def test_seed_uses_url_seed_only():
    assert kp._seed(None, "https://example.com") == {"urlSeed": {"url": "https://example.com"}}


def test_seed_uses_keyword_and_url_seed_when_both_given():
    seed = kp._seed("a, b", "https://example.com")
    assert seed == {
        "keywordAndUrlSeed": {
            "url": "https://example.com",
            "keywords": ["a", "b"],
        }
    }


def test_seed_enforces_max_seed_keywords():
    too_many = ",".join(f"kw{i}" for i in range(kp._MAX_SEED_KEYWORDS + 1))
    with pytest.raises(ValueError):
        kp._seed(too_many, None)


# ------------- historical options -------------


def test_historical_options_requires_both_year_months_or_neither():
    with pytest.raises(ValueError):
        kp._historical_options(True, "2026-01", None)
    with pytest.raises(ValueError):
        kp._historical_options(True, None, "2026-01")


def test_historical_options_returns_range_when_both_given():
    opts = kp._historical_options(True, "2026-01", "2026-06")
    assert opts["includeAverageCpc"] is True
    assert opts["yearMonthRange"] == {
        "start": {"year": 2026, "month": "JANUARY"},
        "end": {"year": 2026, "month": "JUNE"},
    }


def test_historical_options_no_range_when_neither_given():
    opts = kp._historical_options(False, None, None)
    assert opts == {"includeAverageCpc": False}
