"""Tests for audit_retention helpers (export + rotation)."""
from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agency import audit_retention as ar


def _write_audit_file(data_dir: Path, day: date, entries: list[dict]) -> Path:
    path = data_dir / f"audit-{day.isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return path


def _entry(
    day: date = date.today(),
    hour: int = 12,
    operator: str = "fidel",
    customer_id: str = "5461642152",
    tool_name: str = "update_campaign_budget",
    status: str = "executed",
    audit_id: str = "aid-1",
    dry_run: bool = False,
) -> dict:
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=hour)
    return {
        "audit_id": audit_id,
        "timestamp_utc": ts.isoformat(),
        "operator": operator,
        "customer_id": customer_id,
        "client_name": "Clariola",
        "tool_name": tool_name,
        "status": status,
        "dry_run": dry_run,
        "parameters": {"a": 1},
        "result": {"ok": True},
    }


# ---------- _parse_iso ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-16T12:00:00Z", datetime(2026, 8, 16, 12, tzinfo=timezone.utc)),
        ("2026-08-16T12:00:00+00:00", datetime(2026, 8, 16, 12, tzinfo=timezone.utc)),
        (None, None),
        ("", None),
    ],
)
def test_parse_iso(raw, expected):
    assert ar._parse_iso(raw) == expected


def test_parse_iso_rejects_garbage():
    with pytest.raises(ValueError, match="invalid ISO"):
        ar._parse_iso("not-a-date")


# ---------- _daily_audit_paths ----------


def test_daily_audit_paths_returns_sorted(tmp_path):
    _write_audit_file(tmp_path, date(2026, 8, 14), [])
    _write_audit_file(tmp_path, date(2026, 8, 15), [])
    _write_audit_file(tmp_path, date(2026, 8, 13), [])
    (tmp_path / "unrelated.txt").write_text("noise")
    paths = ar._daily_audit_paths(tmp_path)
    assert [p.name for p in paths] == [
        "audit-2026-08-13.jsonl",
        "audit-2026-08-14.jsonl",
        "audit-2026-08-15.jsonl",
    ]


def test_daily_audit_paths_returns_empty_when_missing():
    assert ar._daily_audit_paths(Path("/nonexistent")) == []


# ---------- export_audit_log ----------


def test_export_jsonl_writes_matching_entries(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_audit_file(data_dir, date.today(), [_entry(audit_id="a"), _entry(audit_id="b")])
    destination = tmp_path / "out.jsonl"
    result = ar.export_audit_log(
        destination=str(destination), fmt="jsonl", data_dir=data_dir
    )
    assert result["entries_exported"] == 2
    lines = destination.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["audit_id"] in {"a", "b"}


def test_export_csv_flattens_nested_fields(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_audit_file(data_dir, date.today(), [_entry()])
    destination = tmp_path / "out.csv"
    result = ar.export_audit_log(
        destination=str(destination), fmt="csv", data_dir=data_dir
    )
    assert result["entries_exported"] == 1
    text = destination.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line]
    header = lines[0]
    assert "tool_name" in header
    assert "parameters" in header
    body = lines[1]
    # CSV escapes " as ""; the parameters dict {"a": 1} becomes ""a"": 1
    assert '""a"": 1' in body


def test_export_markdown_groups_by_day(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_audit_file(
        data_dir,
        date(2026, 8, 15),
        [_entry(day=date(2026, 8, 15), audit_id="a")],
    )
    _write_audit_file(
        data_dir,
        date(2026, 8, 16),
        [_entry(day=date(2026, 8, 16), audit_id="b")],
    )
    destination = tmp_path / "out.md"
    ar.export_audit_log(
        destination=str(destination), fmt="markdown_report", data_dir=data_dir
    )
    text = destination.read_text(encoding="utf-8")
    assert "## 2026-08-15" in text
    assert "## 2026-08-16" in text
    assert "entries: **2**" in text


def test_export_filters_by_operator(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_audit_file(
        data_dir,
        date.today(),
        [_entry(operator="fidel"), _entry(operator="other", audit_id="other")],
    )
    result = ar.export_audit_log(
        destination=str(tmp_path / "out.jsonl"),
        fmt="jsonl",
        data_dir=data_dir,
        operator="fidel",
    )
    assert result["entries_exported"] == 1


def test_export_filters_by_days_window(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    today = date.today()
    long_ago = today - timedelta(days=60)
    _write_audit_file(data_dir, today, [_entry(day=today, audit_id="new")])
    _write_audit_file(data_dir, long_ago, [_entry(day=long_ago, audit_id="old")])
    result = ar.export_audit_log(
        destination=str(tmp_path / "out.jsonl"),
        fmt="jsonl",
        data_dir=data_dir,
        days=30,
    )
    assert result["entries_exported"] == 1


def test_export_filters_by_customer_and_status(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_audit_file(
        data_dir,
        date.today(),
        [
            _entry(customer_id="a", status="executed"),
            _entry(customer_id="b", status="validated"),
            _entry(customer_id="a", status="validated", audit_id="a2"),
        ],
    )
    result = ar.export_audit_log(
        destination=str(tmp_path / "out.jsonl"),
        fmt="jsonl",
        data_dir=data_dir,
        customer_id="a",
        status="validated",
    )
    assert result["entries_exported"] == 1


def test_export_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="fmt must"):
        ar.export_audit_log(destination=str(tmp_path / "x"), fmt="parquet")


def test_export_rejects_empty_destination(tmp_path):
    with pytest.raises(ValueError, match="destination is required"):
        ar.export_audit_log(destination="", fmt="jsonl")


def test_export_rejects_bad_since(tmp_path):
    with pytest.raises(ValueError, match="invalid ISO"):
        ar.export_audit_log(
            destination=str(tmp_path / "out.jsonl"),
            fmt="jsonl",
            since="not-a-date",
        )


def test_export_handles_missing_data_dir(tmp_path):
    result = ar.export_audit_log(
        destination=str(tmp_path / "out.jsonl"),
        fmt="jsonl",
        data_dir=tmp_path / "nowhere",
    )
    assert result["entries_exported"] == 0


def test_export_skips_malformed_json_lines(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / f"audit-{date.today().isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_entry(audit_id="a")) + "\n")
        fh.write("this is not json\n")
        fh.write(json.dumps(_entry(audit_id="b")) + "\n")
    result = ar.export_audit_log(
        destination=str(tmp_path / "out.jsonl"),
        fmt="jsonl",
        data_dir=data_dir,
    )
    assert result["entries_exported"] == 2


# ---------- rotate_audit_log ----------


def test_rotate_moves_old_files_into_monthly_zip(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    _write_audit_file(data_dir, date(2026, 6, 5), [_entry(day=date(2026, 6, 5))])
    _write_audit_file(data_dir, date(2026, 6, 6), [_entry(day=date(2026, 6, 6))])
    _write_audit_file(data_dir, date(2026, 7, 10), [_entry(day=date(2026, 7, 10))])
    _write_audit_file(data_dir, date(2026, 8, 15), [_entry(day=date(2026, 8, 15))])

    result = ar.rotate_audit_log(retention_days=30, data_dir=data_dir, now=now)
    assert set(result["archives_written"]) == {
        "audit-archive-2026-06.zip",
        "audit-archive-2026-07.zip",
    }
    assert "2026-06" in result["files_archived"]
    assert len(result["files_archived"]["2026-06"]) == 2

    # Old files gone
    assert not (data_dir / "audit-2026-06-05.jsonl").exists()
    assert not (data_dir / "audit-2026-06-06.jsonl").exists()
    assert not (data_dir / "audit-2026-07-10.jsonl").exists()
    # Recent one stays
    assert (data_dir / "audit-2026-08-15.jsonl").exists()

    # Archives contain the daily files
    with zipfile.ZipFile(data_dir / "audit-archive-2026-06.zip") as zf:
        assert set(zf.namelist()) == {"audit-2026-06-05.jsonl", "audit-2026-06-06.jsonl"}


def test_rotate_leaves_recent_files_alone(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    _write_audit_file(data_dir, date(2026, 8, 10), [_entry(day=date(2026, 8, 10))])
    result = ar.rotate_audit_log(retention_days=30, data_dir=data_dir, now=now)
    assert result["archives_written"] == []
    assert (data_dir / "audit-2026-08-10.jsonl").exists()


def test_rotate_missing_data_dir_returns_empty(tmp_path):
    result = ar.rotate_audit_log(
        retention_days=30, data_dir=tmp_path / "nowhere", now=datetime.now(timezone.utc)
    )
    assert result["archives_written"] == []
    assert result["files_archived"] == {}


def test_rotate_rejects_zero_retention(tmp_path):
    with pytest.raises(ValueError, match=">= 1"):
        ar.rotate_audit_log(retention_days=0, data_dir=tmp_path)


def test_rotate_appends_to_existing_archive(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)

    _write_audit_file(data_dir, date(2026, 6, 5), [_entry(day=date(2026, 6, 5))])
    ar.rotate_audit_log(retention_days=30, data_dir=data_dir, now=now)

    _write_audit_file(data_dir, date(2026, 6, 6), [_entry(day=date(2026, 6, 6))])
    ar.rotate_audit_log(retention_days=30, data_dir=data_dir, now=now)

    with zipfile.ZipFile(data_dir / "audit-archive-2026-06.zip") as zf:
        assert set(zf.namelist()) == {
            "audit-2026-06-05.jsonl",
            "audit-2026-06-06.jsonl",
        }
