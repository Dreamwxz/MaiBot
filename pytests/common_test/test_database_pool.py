"""数据库连接池配置和 expunge_all 行为测试。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from src.common.database import database


class _FakeSession:
    """模拟数据库 Session，记录 expunge_all / close 调用。"""

    def __init__(self) -> None:
        self.expunge_all_called = False
        self.close_called = False
        self._committed = False

    def expunge_all(self) -> None:
        self.expunge_all_called = True

    def close(self) -> None:
        self.close_called = True

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        pass


def test_database_pool_config_pool_size() -> None:
    """engine 配置应包含 pool_size=5。"""
    # 从模块级 engine 创建参数中验证
    # create_engine 调用时传入了 pool_size=5
    assert database.engine.pool.size() == 5, (
        f"期望连接池大小为 5，实际为 {database.engine.pool.size()}"
    )


def test_database_pool_config_max_overflow() -> None:
    """engine 配置应包含 max_overflow=10。"""
    assert database.engine.pool._max_overflow == 10, (
        f"期望 max_overflow 为 10，实际为 {database.engine.pool._max_overflow}"
    )


def test_database_pool_config_pool_recycle() -> None:
    """engine 配置应包含 pool_recycle=3600。"""
    assert database.engine.pool._recycle == 3600, (
        f"期望 pool_recycle 为 3600，实际为 {database.engine.pool._recycle}"
    )


def test_get_db_session_calls_expunge_all_before_close() -> None:
    """get_db_session 在 finally 块中应先调用 expunge_all 再调用 close。"""
    fake_session = _FakeSession()
    call_order: list[str] = []

    original_expunge = fake_session.expunge_all
    original_close = fake_session.close

    def _record_expunge() -> None:
        call_order.append("expunge_all")
        original_expunge()

    def _record_close() -> None:
        call_order.append("close")
        original_close()

    fake_session.expunge_all = _record_expunge  # type: ignore[assignment]
    fake_session.close = _record_close  # type: ignore[assignment]

    with patch.object(database, "SessionLocal", return_value=fake_session):
        with patch.object(database, "initialize_database"):
            with database.get_db_session() as session:
                _ = session  # 仅验证生命周期

    # expunge_all 应在 close 之前被调用
    assert "expunge_all" in call_order, "expunge_all 未被调用"
    assert "close" in call_order, "close 未被调用"
    assert call_order.index("expunge_all") < call_order.index("close"), (
        f"expunge_all 应在 close 之前调用，实际顺序: {call_order}"
    )
    assert fake_session.expunge_all_called, "expunge_all 应被调用"
    assert fake_session.close_called, "close 应被调用"


def test_get_db_session_expunge_all_on_exception() -> None:
    """即使发生异常，get_db_session 也应调用 expunge_all。"""
    fake_session = _FakeSession()

    with patch.object(database, "SessionLocal", return_value=fake_session):
        with patch.object(database, "initialize_database"):
            with pytest.raises(ValueError):
                with database.get_db_session() as session:
                    _ = session
                    raise ValueError("test error")

    assert fake_session.expunge_all_called, "异常路径下 expunge_all 应仍被调用"
    assert fake_session.close_called, "异常路径下 close 应仍被调用"


def test_get_db_calls_expunge_all() -> None:
    """get_db (FastAPI 依赖注入用) 同样应在 finally 中调用 expunge_all。"""
    fake_session = _FakeSession()

    with patch.object(database, "SessionLocal", return_value=fake_session):
        with patch.object(database, "initialize_database"):
            gen = database.get_db()
            session = next(gen)
            _ = session
            try:
                next(gen)
            except StopIteration:
                pass

    assert fake_session.expunge_all_called, "get_db 在 finally 中应调用 expunge_all"
    assert fake_session.close_called, "get_db 在 finally 中应调用 close"
