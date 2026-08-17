"""Export and rotate the append-only audit log.

`data/audit-YYYY-MM-DD.jsonl` grows unbounded. Two operations here:

- export_audit_log: read the raw daily files, filter by
  days/operator/customer/tool, emit CSV / JSONL / markdown_report to
  a local path.
- rotate_audit_log: archive daily files older than retention_days
  into a monthly zip so the working directory stays small.

Compliance-friendly: uses the same PII redaction (`_safe`) already
applied at write time in governance.record_audit, so nothing new
leaks into exports.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR, get_settings


AUDIT_FILE_RE = re.compile(r"^audit-(\d{4})-(\d{2})-(\d{2})\.jsonl$")
ROTATED_ARCHIVE_PREFIX = "audit-archive-"


@dataclass
class ExportFilters:
    days: int | None
    operator: str | None
    customer_id: str | None
    tool_name: str | None
    status: str | None
    since: datetime | None
    until: datetime | None


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _daily_audit_paths(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(p for p in data_dir.iterdir() if AUDIT_FILE_RE.match(p.name))


def _iter_entries(
    data_dir: Path,
    filters: ExportFilters,
) -> Iterable[dict[str, Any]]:
    """Yield audit entries matching filters, ordered oldest to newest."""
    threshold = None
    if filters.days is not None:
        threshold = datetime.now(timezone.utc) - timedelta(days=max(1, min(filters.days, 3650)))

    for path in _daily_audit_paths(data_dir):
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not _matches(entry, filters, threshold):
                        continue
                    yield entry
        except OSError:
            continue


def _matches(entry: dict[str, Any], filters: ExportFilters, threshold: datetime | None) -> bool:
    ts_raw = entry.get("timestamp_utc")
    ts = _parse_iso(ts_raw) if isinstance(ts_raw, str) else None
    if ts is None:
        return False
    if threshold and ts < threshold:
        return False
    if filters.since and ts < filters.since:
        return False
    if filters.until and ts > filters.until:
        return False
    if filters.operator and entry.get("operator") != filters.operator:
        return False
    if filters.customer_id and entry.get("customer_id") != filters.customer_id:
        return False
    if filters.tool_name and entry.get("tool_name") != filters.tool_name:
        return False
    if filters.status and entry.get("status") != filters.status:
        return False
    return True


def export_audit_log(
    *,
    destination: str,
    fmt: str,
    days: int | None = None,
    operator: str | None = None,
    customer_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Write the filtered audit trail to `destination` in the requested format.

    Formats:
    - `jsonl`: one JSON object per line, preserving nesting.
    - `csv`: flat columns (nested fields serialized as JSON strings).
    - `markdown_report`: human-readable summary grouped by day.
    """
    fmt_clean = (fmt or "").strip().lower()
    if fmt_clean not in {"jsonl", "csv", "markdown_report"}:
        raise ValueError("fmt must be one of jsonl, csv, markdown_report")
    if not destination:
        raise ValueError("destination is required")
    if since:
        _parse_iso(since)
    if until:
        _parse_iso(until)
    data_dir = data_dir or DATA_DIR
    dest_path = Path(destination).expanduser()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    filters = ExportFilters(
        days=int(days) if days is not None else None,
        operator=operator or None,
        customer_id=customer_id or None,
        tool_name=tool_name or None,
        status=status or None,
        since=_parse_iso(since),
        until=_parse_iso(until),
    )

    entries = list(_iter_entries(data_dir, filters))
    if fmt_clean == "jsonl":
        rendered = _render_jsonl(entries)
    elif fmt_clean == "csv":
        rendered = _render_csv(entries)
    else:
        rendered = _render_markdown(entries, filters)

    dest_path.write_text(rendered, encoding="utf-8")
    try:
        dest_path.chmod(0o600)
    except OSError:
        pass

    return {
        "destination": str(dest_path),
        "format": fmt_clean,
        "entries_exported": len(entries),
        "bytes_written": dest_path.stat().st_size,
        "filters": {
            "days": filters.days,
            "operator": filters.operator,
            "customer_id": filters.customer_id,
            "tool_name": filters.tool_name,
            "status": filters.status,
            "since": since,
            "until": until,
        },
    }


def rotate_audit_log(
    *,
    retention_days: int | None = None,
    data_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Archive daily audit files older than retention_days into monthly zips.

    Files matching `audit-YYYY-MM-DD.jsonl` older than the retention
    threshold are moved into `audit-archive-YYYY-MM.zip` and removed
    from the top level. Returns an inventory of what was archived.
    """
    data_dir = data_dir or DATA_DIR
    now = now or datetime.now(timezone.utc)
    effective_retention = int(
        retention_days
        if retention_days is not None
        else get_settings().audit_retention_days
    )
    if effective_retention < 1:
        raise ValueError("retention_days must be >= 1")

    cutoff = (now - timedelta(days=effective_retention)).date()
    archived_files: dict[str, list[str]] = {}
    skipped: list[str] = []
    if not data_dir.exists():
        return {
            "retention_days": effective_retention,
            "cutoff_date": cutoff.isoformat(),
            "archives_written": [],
            "files_archived": archived_files,
            "files_skipped": skipped,
        }

    for path in _daily_audit_paths(data_dir):
        match = AUDIT_FILE_RE.match(path.name)
        if not match:
            skipped.append(path.name)
            continue
        try:
            file_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            skipped.append(path.name)
            continue
        if file_date >= cutoff:
            continue
        archive_key = f"{file_date.year:04d}-{file_date.month:02d}"
        archive_path = data_dir / f"{ROTATED_ARCHIVE_PREFIX}{archive_key}.zip"
        with zipfile.ZipFile(archive_path, mode="a", compression=zipfile.ZIP_DEFLATED) as zf:
            already = set(zf.namelist())
            if path.name not in already:
                zf.write(path, arcname=path.name)
        try:
            archive_path.chmod(0o600)
        except OSError:
            pass
        path.unlink()
        archived_files.setdefault(archive_key, []).append(path.name)

    return {
        "retention_days": effective_retention,
        "cutoff_date": cutoff.isoformat(),
        "archives_written": sorted(
            f"{ROTATED_ARCHIVE_PREFIX}{k}.zip" for k in archived_files
        ),
        "files_archived": archived_files,
        "files_skipped": skipped,
    }


# ---------- renderers ----------


def _render_jsonl(entries: list[dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries
    ) + ("\n" if entries else "")


_CSV_COLUMNS = [
    "timestamp_utc",
    "operator",
    "customer_id",
    "client_name",
    "tool_name",
    "status",
    "dry_run",
    "operation_id",
    "audit_id",
    "mutation_id",
    "idempotency_key",
]


def _render_csv(entries: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS + ["parameters", "result", "previous_state"])
    writer.writeheader()
    for entry in entries:
        row = {
            column: entry.get(column, "")
            if not isinstance(entry.get(column), (dict, list))
            else json.dumps(entry.get(column), ensure_ascii=False)
            for column in _CSV_COLUMNS
        }
        row["parameters"] = json.dumps(entry.get("parameters", {}), ensure_ascii=False)
        row["result"] = json.dumps(entry.get("result", {}), ensure_ascii=False)
        row["previous_state"] = json.dumps(entry.get("previous_state", {}), ensure_ascii=False)
        writer.writerow(row)
    return buffer.getvalue()


def _render_markdown(entries: list[dict[str, Any]], filters: ExportFilters) -> str:
    lines = ["# Audit log export", ""]
    lines.append(f"- entries: **{len(entries)}**")
    if filters.days is not None:
        lines.append(f"- window: last {filters.days} day(s)")
    if filters.operator:
        lines.append(f"- operator: `{filters.operator}`")
    if filters.customer_id:
        lines.append(f"- customer_id: `{filters.customer_id}`")
    if filters.tool_name:
        lines.append(f"- tool_name: `{filters.tool_name}`")
    if filters.status:
        lines.append(f"- status: `{filters.status}`")
    lines.append("")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        ts = _parse_iso(entry.get("timestamp_utc"))
        key = ts.date().isoformat() if ts else "unknown"
        grouped.setdefault(key, []).append(entry)

    for day in sorted(grouped):
        lines.append(f"## {day}")
        lines.append("")
        for entry in grouped[day]:
            ts = entry.get("timestamp_utc", "?")
            tool = entry.get("tool_name", "?")
            status = entry.get("status", "?")
            cid = entry.get("customer_id", "-")
            operator = entry.get("operator", "-")
            dry = entry.get("dry_run")
            marker = "[DRY-RUN]" if dry else ""
            lines.append(
                f"- `{ts}` **{tool}** `{status}` {marker} operator=`{operator}` customer=`{cid}`"
            )
        lines.append("")

    return "\n".join(lines)
