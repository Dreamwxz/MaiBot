"""ImportTaskManager._tasks 完成任务清理验证测试。"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from src.A_memorix.core.utils.web_import_manager import (
    COMPLETED_TASK_KEEP_LIMIT,
    ImportTaskManager,
    ImportTaskRecord,
)


def _build_manager() -> ImportTaskManager:
    """构造最小可用的 ImportTaskManager 测试实例。"""
    manager = ImportTaskManager.__new__(ImportTaskManager)
    manager._tasks: dict[str, ImportTaskRecord] = {}
    manager._task_order: deque[str] = deque()
    manager._lock = None  # type: ignore[assignment]
    manager._storage_lock = None  # type: ignore[assignment]
    manager._queue: deque[str] = deque()
    manager._active_task_id = None
    manager._worker_task = None
    manager._stopping = False
    return manager


def _make_completed_task(task_id: str, finished_at: float, status: str = "completed") -> ImportTaskRecord:
    return ImportTaskRecord(
        task_id=task_id,
        source="upload",
        params={},
        status=status,
        finished_at=finished_at,
    )


def _make_active_task(task_id: str, status: str = "running") -> ImportTaskRecord:
    return ImportTaskRecord(
        task_id=task_id,
        source="upload",
        params={},
        status=status,
    )


class TestTaskCleanup:
    def test_completed_tasks_are_pruned(self) -> None:
        """超过 COMPLETED_TASK_KEEP_LIMIT 的已完成任务应被清理。"""
        manager = _build_manager()

        # 插入 60 个已完成任务（超过限制 50）
        for i in range(60):
            tid = f"completed_{i:03d}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(i))
            manager._task_order.appendleft(tid)

        manager._cleanup_old_tasks()

        assert len(manager._tasks) <= COMPLETED_TASK_KEEP_LIMIT, (
            f"已完成任务应被清理到 <= {COMPLETED_TASK_KEEP_LIMIT}，实际 {len(manager._tasks)}"
        )

    def test_active_tasks_are_preserved(self) -> None:
        """活跃状态的任务不应被清理。"""
        manager = _build_manager()

        # 插入活跃任务
        active_statuses = ["queued", "preparing", "running", "cancel_requested"]
        for i, status in enumerate(active_statuses):
            tid = f"active_{i}"
            manager._tasks[tid] = _make_active_task(tid, status=status)
            manager._task_order.appendleft(tid)

        # 插入一些已完成任务
        for i in range(10):
            tid = f"completed_{i}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(i))
            manager._task_order.appendleft(tid)

        manager._cleanup_old_tasks()

        for status in active_statuses:
            matching = [t for t in manager._tasks.values() if t.status == status]
            assert len(matching) == 1, f"状态为 {status} 的任务应被保留"

    def test_task_order_synchronized_after_cleanup(self) -> None:
        """清理后 _task_order 应与 _tasks 保持同步。"""
        manager = _build_manager()

        for i in range(60):
            tid = f"task_{i:03d}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(i))
            manager._task_order.appendleft(tid)

        manager._cleanup_old_tasks()

        for tid in manager._task_order:
            assert tid in manager._tasks, f"_task_order 中的 {tid} 应存在于 _tasks"

    def test_no_cleanup_when_under_limit(self) -> None:
        """未达限制时不应清理。"""
        manager = _build_manager()

        for i in range(30):
            tid = f"completed_{i}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(i))
            manager._task_order.appendleft(tid)

        manager._cleanup_old_tasks()

        assert len(manager._tasks) == 30, "未超限不应清理"

    def test_completed_task_keep_limit_value(self) -> None:
        """COMPLETED_TASK_KEEP_LIMIT 应为 50。"""
        assert COMPLETED_TASK_KEEP_LIMIT == 50

    def test_failed_status_treated_as_completed(self) -> None:
        """failed/completed_with_errors/cancelled 状态应视为非活跃。"""
        manager = _build_manager()

        terminal_statuses = ["completed", "completed_with_errors", "failed", "cancelled"]
        for i, status in enumerate(terminal_statuses):
            tid = f"terminal_{i}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(i), status=status)
            manager._task_order.appendleft(tid)

        # 添加活跃任务确保不被清空
        manager._tasks["active"] = _make_active_task("active", status="running")
        manager._task_order.appendleft("active")

        # 添加足够的已完成任务触发清理
        for i in range(55):
            tid = f"filler_{i}"
            manager._tasks[tid] = _make_completed_task(tid, finished_at=float(1000 + i))
            manager._task_order.appendleft(tid)

        manager._cleanup_old_tasks()

        assert "active" in manager._tasks, "活跃任务应保留"
