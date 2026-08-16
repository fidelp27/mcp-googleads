"""Resolve Google Ads geo target IDs (e.g. utm_geo / {loc_physical_ms}) to names.

The Final URL suffix records the user's physical location as a numeric geo
target constant ID. Feed those IDs here (from your reports / GA4 export) to get
a readable mapping for reporting.

Usage:
    # IDs as arguments
    python scripts/resolve_geo.py 2032 1000196 20037

    # IDs from a file (one per line, or a CSV column of IDs)
    python scripts/resolve_geo.py --file ids.txt --out output/geo_mapping.csv

    # Pipe a column from a CSV
    python scripts/resolve_geo.py --file report.csv --column utm_geo

Requires the same .env credentials as the MCP server.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import google_ads_server  # noqa: F401  (loads .env + auth)
from agency.api import search

CUSTOMER_ID_ENV = "GOOGLE_ADS_LOGIN_CUSTOMER_ID"
FIELDS = [
    "geo_target_constant.id",
    "geo_target_constant.name",
    "geo_target_constant.canonical_name",
    "geo_target_constant.country_code",
    "geo_target_constant.target_type",
    "geo_target_constant.status",
]


def _customer_id() -> str:
    import os

    cid = "".join(ch for ch in os.getenv(CUSTOMER_ID_ENV, "") if ch.isdigit())
    if len(cid) != 10:
        sys.exit(f"Set {CUSTOMER_ID_ENV} to a 10-digit account in .env")
    return cid


def _collect_ids(args: argparse.Namespace) -> list[str]:
    raw: list[str] = list(args.ids)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
        if args.column:
            reader = csv.DictReader(text.splitlines())
            raw += [row.get(args.column, "") for row in reader]
        else:
            raw += re.split(r"[\s,;]+", text)
    seen: dict[str, None] = {}
    for token in raw:
        digits = "".join(ch for ch in str(token) if ch.isdigit())
        if digits:
            seen.setdefault(digits, None)
    return list(seen)


def resolve(customer_id: str, ids: list[str]) -> dict[str, dict[str, str]]:
    resolved: dict[str, dict[str, str]] = {}
    for start in range(0, len(ids), 400):  # keep the IN clause reasonable
        chunk = ids[start:start + 400]
        query = (
            f"SELECT {', '.join(FIELDS)} FROM geo_target_constant "
            f"WHERE geo_target_constant.id IN ({','.join(chunk)})"
        )
        for row in search(customer_id, query):
            g = row["geoTargetConstant"]
            resolved[str(g["id"])] = {
                "id": str(g["id"]),
                "name": g.get("name", ""),
                "canonical_name": g.get("canonicalName", ""),
                "country_code": g.get("countryCode", ""),
                "target_type": g.get("targetType", ""),
                "status": g.get("status", ""),
            }
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve geo target IDs to names")
    parser.add_argument("ids", nargs="*", help="geo target IDs")
    parser.add_argument("--file", help="file with IDs (lines, CSV, or free text)")
    parser.add_argument("--column", help="CSV column name holding the IDs")
    parser.add_argument("--out", help="write mapping to this CSV path")
    args = parser.parse_args()

    ids = _collect_ids(args)
    if not ids:
        sys.exit("No geo IDs provided. Pass IDs as arguments or via --file.")

    resolved = resolve(_customer_id(), ids)

    columns = ["id", "name", "canonical_name", "country_code", "target_type", "status"]
    rows = [resolved.get(i, {"id": i, "name": "NOT_FOUND"}) for i in ids]

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        print(f"Wrote {len(rows)} rows to {out}")
    else:
        print(",".join(columns))
        for row in rows:
            print(",".join(str(row.get(c, "")) for c in columns))

    missing = [i for i in ids if i not in resolved]
    if missing:
        print(f"\n{len(missing)} ID(s) not found: {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
