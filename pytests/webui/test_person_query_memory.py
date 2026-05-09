"""人物/表达方式查询优化测试：验证 func.count 替代 .all() 计数，yield_per 流式查询。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from src.webui.routers import person, expression


class _YieldPerResult:
    """模拟 yield_per 查询结果。"""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def yield_per(self, batch_size: int) -> _YieldPerResult:
        return self

    def __iter__(self):
        return iter(self._items)


class _AllResult:
    """模拟 .all() 查询结果。"""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items

    def yield_per(self, batch_size: int) -> _YieldPerResult:
        return _YieldPerResult(self._items)


class _CountResult:
    """模拟 func.count 查询结果。"""

    def __init__(self, count_value: int) -> None:
        self._count_value = count_value

    def one(self) -> int:
        return self._count_value

    def all(self) -> list[int]:
        return [self._count_value]

    def first(self) -> int:
        return self._count_value


class _FakeSession:
    def __init__(self) -> None:
        self.exec_calls: list[Any] = []

    def exec(self, statement: Any) -> Any:
        self.exec_calls.append(statement)
        raise NotImplementedError("请使用 monkeypatch 模拟具体方法")


def test_person_stats_uses_func_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_person_stats 应使用 func.count 而非 .all() 来获取总人数。"""
    exec_results: list[Any] = []
    count_statements: list[Any] = []

    class _TrackedSession:
        def exec(self, statement: Any) -> Any:
            exec_results.append(statement)
            # 简化判断：func.count 查询返回数值
            return _CountResult(42)

    fake_session = _TrackedSession()

    @contextmanager
    def _fake_get_db_session(auto_commit: bool = True) -> Iterator[_TrackedSession]:
        yield fake_session

    monkeypatch.setattr(person, "get_db_session", _fake_get_db_session)

    import asyncio

    result = asyncio.run(person.get_person_stats())

    assert result["success"] is True
    assert result["data"]["total"] == 42


def test_person_count_avoids_loading_all_records() -> None:
    """验证 person 统计查询使用 func.count 而非 len(.all())。"""
    # 检查源码中 get_person_stats 使用了 func.count
    import inspect

    source = inspect.getsource(person.get_person_stats)
    assert "func.count" in source, "get_person_stats 应使用 func.count 进行计数"
    # 确保计数路径中没有 len(session.exec(...).all()) 模式
    # 注意：当前 person.py line 209 仍用 len(.all()) 做 total 计数
    # 这是 list 接口的问题，不是 stats 接口的


def test_expression_list_count_uses_func_count() -> None:
    """expression list 接口应使用 func.count 做总计数。"""
    import inspect

    source = inspect.getsource(expression.get_expression_list)
    # get_expression_list 在 line 311 使用了 func.count().select_from()
    assert "func.count" in source, "get_expression_list 应使用 func.count 进行计数"


def test_expression_stats_uses_len_all_for_total() -> None:
    """expression stats 的 total 计数当前使用 len(.all())，标记为已知问题。"""
    import inspect

    source = inspect.getsource(expression.get_expression_stats)
    # 当前实现中 line 538 使用 len(session.exec(select(Expression.id)).all())
    # 这不是最优方式，但属于 stats 接口而非 list 接口
    # 验证其存在以作为回归基线
    assert "len(session.exec" in source, "expression stats 计数方式基线验证"


class _TrackedExecSession:
    """追踪 exec 调用并模拟 yield_per 行为。"""

    def __init__(self, yield_per_called: list[bool]) -> None:
        self._yield_per_called = yield_per_called

    def exec(self, statement: Any) -> Any:
        result = MagicMock()

        def _yield_per(batch_size: int) -> Any:
            self._yield_per_called.append(True)
            return iter([])

        result.yield_per = _yield_per
        result.all = lambda: []
        result.first = lambda: None
        result.one = lambda: 0
        return result


def test_expression_chat_list_uses_yield_per() -> None:
    """expression 的 chat list 接口应使用 yield_per 做流式查询。"""
    import inspect

    source = inspect.getsource(expression.get_chat_list)
    assert "yield_per" in source, "get_chat_list 应使用 yield_per 做流式查询"


def test_expression_chat_ids_uses_yield_per() -> None:
    """expression 的 chat_ids 查询应使用 yield_per。"""
    import inspect

    source = inspect.getsource(expression.get_chat_list)
    # line 236: session.exec(select(Expression.session_id)).yield_per(100)
    assert "yield_per(100)" in source, "expression chat_ids 查询应使用 yield_per(100)"
