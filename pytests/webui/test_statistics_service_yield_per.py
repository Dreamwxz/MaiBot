"""statistics_service yield_per 行为验证测试。"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from src.services import statistics_service


class _YieldPerIterator:
    """模拟 yield_per 流式迭代结果。"""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.yield_per_called = False
        self.yield_per_batch_size: int | None = None

    def yield_per(self, batch_size: int) -> _YieldPerIterator:
        self.yield_per_called = True
        self.yield_per_batch_size = batch_size
        return self

    def __iter__(self):
        return iter(self._items)


class _Session:
    def __init__(self, results: dict[str, Any]) -> None:
        self._results = results

    def exec(self, statement: Any) -> Any:
        return self._results.pop("next", MagicMock())


def test_fetch_online_time_since_uses_yield_per() -> None:
    """fetch_online_time_since 应使用 yield_per(100) 做流式查询。"""
    source = inspect.getsource(statistics_service.fetch_online_time_since)
    assert "yield_per(100)" in source, "fetch_online_time_since 应使用 yield_per(100)"


def test_fetch_model_usage_since_uses_yield_per() -> None:
    """fetch_model_usage_since 应使用 yield_per(100) 做流式查询。"""
    source = inspect.getsource(statistics_service.fetch_model_usage_since)
    assert "yield_per(100)" in source, "fetch_model_usage_since 应使用 yield_per(100)"


def test_fetch_messages_since_uses_yield_per() -> None:
    """fetch_messages_since 应使用 yield_per(100) 做流式查询。"""
    source = inspect.getsource(statistics_service.fetch_messages_since)
    assert "yield_per(100)" in source, "fetch_messages_since 应使用 yield_per(100)"


def test_fetch_tool_records_since_uses_yield_per() -> None:
    """fetch_tool_records_since 应使用 yield_per(100) 做流式查询。"""
    source = inspect.getsource(statistics_service.fetch_tool_records_since)
    assert "yield_per(100)" in source, "fetch_tool_records_since 应使用 yield_per(100)"


def test_get_summary_statistics_online_time_uses_yield_per() -> None:
    """get_summary_statistics 中 OnlineTime 查询应使用 yield_per(100)。"""
    source = inspect.getsource(statistics_service.get_summary_statistics)
    assert "yield_per(100)" in source, "get_summary_statistics 的 OnlineTime 查询应使用 yield_per(100)"


def test_fetch_online_time_since_yields_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_online_time_since 应正确返回 (start, end) 元组列表。"""
    now = datetime(2026, 5, 6, 12, 0, 0)
    records = [
        SimpleNamespace(start_timestamp=now - timedelta(minutes=30), end_timestamp=now),
        SimpleNamespace(start_timestamp=now - timedelta(hours=2), end_timestamp=now - timedelta(hours=1)),
    ]

    yield_per_iter = _YieldPerIterator(records)

    class _FakeSession:
        def exec(self, statement: Any) -> _YieldPerIterator:
            return yield_per_iter

    @contextmanager
    def _fake_get_db_session(auto_commit: bool = True) -> Iterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(statistics_service, "get_db_session", _fake_get_db_session)

    result = statistics_service.fetch_online_time_since(now - timedelta(hours=3))

    assert len(result) == 2
    assert result[0] == (records[0].start_timestamp, records[0].end_timestamp)
    assert result[1] == (records[1].start_timestamp, records[1].end_timestamp)
    assert yield_per_iter.yield_per_called, "yield_per 应被调用"
    assert yield_per_iter.yield_per_batch_size == 100, "yield_per 批次大小应为 100"


def test_fetch_online_time_since_empty_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_online_time_since 在空数据库时返回空列表。"""
    yield_per_iter = _YieldPerIterator([])

    class _FakeSession:
        def exec(self, statement: Any) -> _YieldPerIterator:
            return yield_per_iter

    @contextmanager
    def _fake_get_db_session(auto_commit: bool = True) -> Iterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(statistics_service, "get_db_session", _fake_get_db_session)

    now = datetime(2026, 5, 6, 12, 0, 0)
    result = statistics_service.fetch_online_time_since(now - timedelta(hours=1))

    assert result == [], "空数据库应返回空列表"
    assert yield_per_iter.yield_per_called, "即使无结果也应调用 yield_per"


def test_fetch_model_usage_since_streams_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_model_usage_since 应流式返回字典列表。"""
    now = datetime(2026, 5, 6, 12, 0, 0)
    records = [
        SimpleNamespace(
            timestamp=now,
            request_type="chat.reply",
            model_api_provider_name="provider",
            model_assign_name="chat-main",
            model_name="gpt-a",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.01,
            time_cost=1.2,
        ),
    ]

    yield_per_iter = _YieldPerIterator(records)

    class _FakeSession:
        def exec(self, statement: Any) -> _YieldPerIterator:
            return yield_per_iter

    @contextmanager
    def _fake_get_db_session(auto_commit: bool = True) -> Iterator[_FakeSession]:
        yield _FakeSession()

    monkeypatch.setattr(statistics_service, "get_db_session", _fake_get_db_session)

    result = statistics_service.fetch_model_usage_since(now - timedelta(hours=1))

    assert len(result) == 1
    assert result[0]["timestamp"] == now
    assert result[0]["request_type"] == "chat.reply"
    assert yield_per_iter.yield_per_called
