"""Tests for the file-based multi-operator resource lock."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agency import locks


@pytest.fixture
def isolated_lock_root(tmp_path, monkeypatch):
    """Point locks.LOCK_ROOT at a tmp dir so each test gets fresh state."""
    root = tmp_path / "locks_root"
    monkeypatch.setattr(locks, "LOCK_ROOT", root)
    return root


# ---------- happy path ----------


def test_acquire_and_release(isolated_lock_root):
    with locks.resource_lock("1234567890", "customers/1/campaigns/1") as info:
        assert Path(info["lock_path"]).exists()
        assert info["lock_wait_ms"] >= 0
    # File remains but no longer held; the FileLock leaves the sentinel


def test_lock_path_created_under_customer_dir(isolated_lock_root):
    with locks.resource_lock("1234567890", "customers/1/campaigns/1") as info:
        assert "1234567890" in info["lock_path"]


def test_lock_wait_ms_is_small_when_uncontested(isolated_lock_root):
    with locks.resource_lock("1234567890", "customers/1/campaigns/1") as info:
        assert info["lock_wait_ms"] < 500


# ---------- concurrency ----------


def test_two_threads_serialize_on_same_resource(isolated_lock_root):
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str, hold_seconds: float):
        barrier.wait()
        with locks.resource_lock(
            "1234567890",
            "customers/1/campaigns/1",
            timeout_seconds=5,
        ):
            order.append(f"{name}:start")
            time.sleep(hold_seconds)
            order.append(f"{name}:end")

    t1 = threading.Thread(target=worker, args=("A", 0.2))
    t2 = threading.Thread(target=worker, args=("B", 0.2))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # No interleaving: for each worker the start must directly precede its own end
    for label in ("A", "B"):
        start_idx = order.index(f"{label}:start")
        end_idx = order.index(f"{label}:end")
        assert end_idx == start_idx + 1


def test_second_call_times_out_when_first_holds(isolated_lock_root):
    holder_done = threading.Event()

    def hold_lock():
        with locks.resource_lock(
            "1234567890",
            "customers/1/campaigns/1",
            timeout_seconds=5,
        ):
            time.sleep(0.5)
        holder_done.set()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    # Give the holder time to acquire the lock
    time.sleep(0.05)

    with pytest.raises(locks.ResourceLockedError) as exc_info:
        with locks.resource_lock(
            "1234567890",
            "customers/1/campaigns/1",
            timeout_seconds=0.1,
        ):
            pytest.fail("should never enter this block")
    assert exc_info.value.resource_key == "customers/1/campaigns/1"
    assert exc_info.value.timeout_seconds == 0.1
    holder.join()
    assert holder_done.is_set()


def test_different_customers_do_not_block_each_other(isolated_lock_root):
    resource = "customers/x/campaigns/1"
    with locks.resource_lock("1234567890", resource, timeout_seconds=0) as first:
        with locks.resource_lock("9876543210", resource, timeout_seconds=0) as second:
            assert first["lock_path"] != second["lock_path"]


def test_different_resources_same_customer_do_not_block(isolated_lock_root):
    customer_id = "1234567890"
    with locks.resource_lock(customer_id, "customers/x/campaigns/1", timeout_seconds=0) as first:
        with locks.resource_lock(customer_id, "customers/x/campaigns/2", timeout_seconds=0) as second:
            assert first["lock_path"] != second["lock_path"]


# ---------- validation ----------


def test_missing_customer_id_rejected(isolated_lock_root):
    with pytest.raises(ValueError, match="customer_id is required"):
        with locks.resource_lock("", "resource"):
            pass


def test_missing_resource_key_rejected(isolated_lock_root):
    with pytest.raises(ValueError, match="resource_key is required"):
        with locks.resource_lock("1234567890", ""):
            pass


def test_negative_timeout_rejected(isolated_lock_root):
    with pytest.raises(ValueError, match="non-negative"):
        with locks.resource_lock(
            "1234567890", "customers/1/campaigns/1", timeout_seconds=-1
        ):
            pass


def test_error_message_contains_key_fields(isolated_lock_root):
    def hold_lock():
        with locks.resource_lock(
            "1234567890", "customers/1/campaigns/1", timeout_seconds=5
        ):
            time.sleep(0.3)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    time.sleep(0.05)
    try:
        with pytest.raises(locks.ResourceLockedError) as exc_info:
            with locks.resource_lock(
                "1234567890",
                "customers/1/campaigns/1",
                timeout_seconds=0.05,
            ):
                pass
        message = str(exc_info.value)
        assert "RESOURCE_LOCKED" in message
        assert "1234567890" in message
        assert "customers/1/campaigns/1" in message
    finally:
        holder.join()


# ---------- lock_paths_for ----------


def test_lock_paths_for_lists_active_locks(isolated_lock_root):
    """filelock removes the sentinel on release; lock_paths_for is for debugging
    the currently-held locks, not historical ones."""
    with locks.resource_lock("1234567890", "customers/1/campaigns/1"):
        with locks.resource_lock("1234567890", "customers/1/campaigns/2"):
            paths = locks.lock_paths_for("1234567890")
            assert len(paths) == 2


def test_lock_paths_empty_when_no_customer_dir(isolated_lock_root):
    assert locks.lock_paths_for("9999999999") == []
