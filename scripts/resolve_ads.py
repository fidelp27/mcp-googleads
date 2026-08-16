"""Resolve Google Ads ad group / ad IDs (utm_content / utm_creative) to names.

The Final URL suffix records utm_content={adgroupid} and utm_creative={creative}
(the ad ID). Unlike geo IDs these are account-specific, so they are looked up on
the ad_group and ad_group_ad resources.

Usage:
    # IDs on the command line
    python scripts/resolve_ads.py --adgroups 199327068917 --ads 821049505825

    # From a report export, resolving both columns, writing two CSVs
    python scripts/resolve_ads.py --file report.csv \
        --adgroup-column utm_content --ad-column utm_creative \
        --out-prefix output/clariola

Requires the same .env credentials as the MCP server.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import google_ads_server  # noqa: F401  (loads .env + auth)
from agency.api import search


def _customer_id(override: str | None) -> str:
    source = override or os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    cid = "".join(ch for ch in source if ch.isdigit())
    if len(cid) != 10:
        sys.exit(
            "Pass --customer-id with the 10-digit CLIENT account that owns the ads "
            "(ad groups/ads live in the client account, not the manager)."
        )
    return cid


def _digits(tokens) -> list[str]:
    seen: dict[str, None] = {}
    for token in tokens:
        d = "".join(ch for ch in str(token) if ch.isdigit())
        if d:
            seen.setdefault(d, None)
    return list(seen)


def _from_file(path: str, column: str | None) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    if column:
        return [row.get(column, "") for row in csv.DictReader(text.splitlines())]
    return re.split(r"[\s,;]+", text)


def _chunks(ids: list[str], size: int = 400):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def resolve_ad_groups(cid: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk in _chunks(ids):
        q = (
            "SELECT ad_group.id, ad_group.name, ad_group.status, "
            "campaign.id, campaign.name FROM ad_group "
            f"WHERE ad_group.id IN ({','.join(chunk)})"
        )
        for r in search(cid, q):
            g, c = r["adGroup"], r["campaign"]
            out[str(g["id"])] = {
                "ad_group_id": str(g["id"]),
                "ad_group_name": g.get("name", ""),
                "ad_group_status": g.get("status", ""),
                "campaign_id": str(c["id"]),
                "campaign_name": c.get("name", ""),
            }
    return out


def resolve_ads(cid: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk in _chunks(ids):
        q = (
            "SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
            "ad_group.id, ad_group.name, campaign.name, "
            "ad_group_ad.ad.responsive_search_ad.headlines "
            f"FROM ad_group_ad WHERE ad_group_ad.ad.id IN ({','.join(chunk)})"
        )
        for r in search(cid, q):
            a, ad, g = r["adGroupAd"], r["adGroupAd"]["ad"], r["adGroup"]
            heads = ad.get("responsiveSearchAd", {}).get("headlines", [])
            out[str(ad["id"])] = {
                "ad_id": str(ad["id"]),
                "ad_type": ad.get("type", ""),
                "ad_status": a.get("status", ""),
                "ad_group_id": str(g["id"]),
                "ad_group_name": g.get("name", ""),
                "campaign_name": r["campaign"].get("name", ""),
                "first_headline": heads[0]["text"] if heads else "",
            }
    return out


def _emit(rows: list[dict], columns: list[str], out_path: str | None, label: str) -> None:
    if not rows:
        return
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns)
            w.writeheader()
            for row in rows:
                w.writerow({c: row.get(c, "") for c in columns})
        print(f"Wrote {len(rows)} {label} rows to {p}")
    else:
        print(f"# {label}")
        print(",".join(columns))
        for row in rows:
            print(",".join(str(row.get(c, "")) for c in columns))


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolve ad group / ad IDs to names")
    ap.add_argument("--adgroups", nargs="*", default=[], help="ad group IDs (utm_content)")
    ap.add_argument("--ads", nargs="*", default=[], help="ad IDs (utm_creative)")
    ap.add_argument("--file", help="report file to read IDs from")
    ap.add_argument("--adgroup-column", help="CSV column with ad group IDs")
    ap.add_argument("--ad-column", help="CSV column with ad IDs")
    ap.add_argument("--out-prefix", help="write <prefix>_adgroups.csv and <prefix>_ads.csv")
    ap.add_argument("--customer-id", help="client account that owns the ads (10 digits)")
    args = ap.parse_args()

    ag_tokens = list(args.adgroups)
    ad_tokens = list(args.ads)
    if args.file:
        if args.adgroup_column:
            ag_tokens += _from_file(args.file, args.adgroup_column)
        if args.ad_column:
            ad_tokens += _from_file(args.file, args.ad_column)

    ag_ids, ad_ids = _digits(ag_tokens), _digits(ad_tokens)
    if not ag_ids and not ad_ids:
        sys.exit("Provide --adgroups/--ads or --file with --adgroup-column/--ad-column.")

    cid = _customer_id(args.customer_id)
    ag_cols = ["ad_group_id", "ad_group_name", "ad_group_status", "campaign_id", "campaign_name"]
    ad_cols = ["ad_id", "ad_type", "ad_status", "ad_group_id", "ad_group_name",
               "campaign_name", "first_headline"]

    if ag_ids:
        resolved = resolve_ad_groups(cid, ag_ids)
        rows = [resolved.get(i, {"ad_group_id": i, "ad_group_name": "NOT_FOUND"}) for i in ag_ids]
        _emit(rows, ag_cols, f"{args.out_prefix}_adgroups.csv" if args.out_prefix else None, "ad group")
    if ad_ids:
        resolved = resolve_ads(cid, ad_ids)
        rows = [resolved.get(i, {"ad_id": i, "ad_type": "NOT_FOUND"}) for i in ad_ids]
        _emit(rows, ad_cols, f"{args.out_prefix}_ads.csv" if args.out_prefix else None, "ad")


if __name__ == "__main__":
    main()
