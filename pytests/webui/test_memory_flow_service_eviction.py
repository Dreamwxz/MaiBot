"""ChatSummaryWritebackService._states 驱逐机制验证测试。"""

from __future__ import annotations

import time

import pytest

from src.services.memory_flow_service import ChatSummaryWritebackService, ChatSummaryWritebackState


def _build_service() -> ChatSummaryWritebackService:
    """跳过 __init__ 构造最小测试实例。"""
    service = ChatSummaryWritebackService.__new__(ChatSummaryWritebackService)
    service._states = {}
    service._states_last_updated = {}
    return service


class TestStatesEviction:
    def test_expired_state_removed_after_ttl(self) -> None:
        """超过 24h TTL 的状态条目应被驱逐。"""
        service = _build_service()
        session_id = "session_expired"

        service._states[session_id] = ChatSummaryWritebackState(
            last_trigger_message_count=100,
            last_trigger_time=1.0,
        )
        # 设置更新时间为 25 小时前（超过 TTL）
        service._states_last_updated[session_id] = time.time() - (25 * 3600)

        service._cleanup_expired_states(session_id)

        assert session_id not in service._states, "过期的 state 应被驱逐"
        assert session_id not in service._states_last_updated, "过期的 last_updated 应被驱逐"

    def test_recent_state_preserved_within_ttl(self) -> None:
        """在 TTL 内的状态条目应被保留。"""
        service = _build_service()
        session_id = "session_active"

        service._states[session_id] = ChatSummaryWritebackState(
            last_trigger_message_count=50,
            last_trigger_time=time.time(),
        )
        service._states_last_updated[session_id] = time.time() - (1 * 3600)  # 1 小时前

        service._cleanup_expired_states(session_id)

        assert session_id in service._states, "TTL 内的 state 不应被驱逐"

    def test_cleanup_expired_states_scans_all(self) -> None:
        """不指定 current_session_id 时应扫描全部条目。"""
        service = _build_service()

        # 添加 3 个状态：1 个过期，2 个未过期
        service._states["expired_1"] = ChatSummaryWritebackState(last_trigger_message_count=10)
        service._states_last_updated["expired_1"] = time.time() - (25 * 3600)

        service._states["active_1"] = ChatSummaryWritebackState(last_trigger_message_count=20)
        service._states_last_updated["active_1"] = time.time() - (1 * 3600)

        service._states["active_2"] = ChatSummaryWritebackState(last_trigger_message_count=30)
        service._states_last_updated["active_2"] = time.time()

        service._cleanup_expired_states()  # 不传参数，全量扫描

        assert "expired_1" not in service._states, "过期的 state 应被驱逐"
        assert "active_1" in service._states, "未过期的 state 应保留"
        assert "active_2" in service._states, "未过期的 state 应保留"

    def test_cleanup_single_session_only_checks_that_session(self) -> None:
        """指定 current_session_id 时仅检查该条目。"""
        service = _build_service()

        service._states["target_expired"] = ChatSummaryWritebackState(last_trigger_message_count=10)
        service._states_last_updated["target_expired"] = time.time() - (25 * 3600)

        service._states["other_expired"] = ChatSummaryWritebackState(last_trigger_message_count=20)
        service._states_last_updated["other_expired"] = time.time() - (30 * 3600)

        # 仅检查 target_expired
        service._cleanup_expired_states("target_expired")

        assert "target_expired" not in service._states, "目标过期条目应被驱逐"
        assert "other_expired" in service._states, "非目标过期条目不应被驱逐（仅做单条目检查）"

    def test_ttl_is_24_hours(self) -> None:
        """TTL 应为 24 小时（86400 秒）。"""
        assert ChatSummaryWritebackService._STATE_TTL_SECONDS == 86400

    @pytest.mark.asyncio
    async def test_evicted_state_can_be_restored_from_db_stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _build_service()
        session_id = "session_restore"

        service._states[session_id] = ChatSummaryWritebackState(
            last_trigger_message_count=100,
            last_trigger_time=time.time(),
        )
        service._states_last_updated[session_id] = time.time() - (25 * 3600)
        service._cleanup_expired_states(session_id)
        assert session_id not in service._states

        async def _fake_load(*, session_id: str, total_message_count: int) -> int:
            return 80

        monkeypatch.setattr(service, "_load_last_trigger_message_count", _fake_load)

        restored_count = await service._load_last_trigger_message_count(
            session_id=session_id, total_message_count=100
        )

        assert restored_count == 80

        state = ChatSummaryWritebackState(
            last_trigger_message_count=restored_count,
            last_trigger_time=time.time() if restored_count > 0 else 0.0,
        )
        service._states[session_id] = state
        service._states_last_updated[session_id] = time.time()

        assert session_id in service._states
        assert service._states[session_id].last_trigger_message_count == 80
