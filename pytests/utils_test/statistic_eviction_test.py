"""statistic.py name_mapping 驱逐和 time_costs 列表截断验证测试。"""

from __future__ import annotations

from collections import defaultdict

import pytest

from src.chat.utils.statistic import StatisticOutputTask


def _build_statistic_task() -> StatisticOutputTask:
    """跳过 __init__ 构造最小测试实例。"""
    task = StatisticOutputTask.__new__(StatisticOutputTask)
    task.name_mapping = {}
    return task


class TestNameMappingEviction:
    """name_mapping 超过 10000 条时淘汰最早的 1000 条。"""

    def test_name_mapping_eviction_when_over_10000(self) -> None:
        task = _build_statistic_task()
        for i in range(15000):
            task.name_mapping[f"chat_{i:05d}"] = (f"名称_{i}", float(i))

        task._drop_cached_time_cost_lists({})

        # 驱逐策略：超过 10000 时淘汰最早的 1000 条，结果为 14000
        assert len(task.name_mapping) == 14000, (
            f"name_mapping 应为 14000（15000-1000），实际 {len(task.name_mapping)}"
        )

    def test_name_mapping_eviction_removes_oldest(self) -> None:
        task = _build_statistic_task()
        for i in range(15000):
            task.name_mapping[f"chat_{i:05d}"] = (f"名称_{i}", float(i))

        task._drop_cached_time_cost_lists({})

        # 最早的 1000 条应被移除，保留 chat_01000 到 chat_14999
        assert "chat_00000" not in task.name_mapping
        assert "chat_00999" not in task.name_mapping
        assert "chat_01000" in task.name_mapping
        assert "chat_14999" in task.name_mapping

    def test_name_mapping_no_eviction_when_under_limit(self) -> None:
        task = _build_statistic_task()
        for i in range(5000):
            task.name_mapping[f"chat_{i:05d}"] = (f"名称_{i}", float(i))

        task._drop_cached_time_cost_lists({})

        assert len(task.name_mapping) == 5000, "未超限不应驱逐"


class TestTimeCostsTruncation:
    """time_costs 子列表超过 1000 条时截断为最近 1000 条。"""

    def test_time_costs_truncation_over_1000(self) -> None:
        stat_data = StatisticOutputTask._build_stat_period_data()
        key = "time_costs_by_type"
        subkey = "chat"

        for i in range(1500):
            StatisticOutputTask._append_defaultdict_list(stat_data, key, subkey, float(i))

        counter = stat_data[key]
        assert len(counter[subkey]) <= 1000, (
            f"子列表应被截断到 <=1000，实际 {len(counter[subkey])}"
        )

    def test_time_costs_truncation_keeps_most_recent(self) -> None:
        stat_data = StatisticOutputTask._build_stat_period_data()
        key = "time_costs_by_type"
        subkey = "chat"

        for i in range(1500):
            StatisticOutputTask._append_defaultdict_list(stat_data, key, subkey, float(i))

        counter = stat_data[key]
        # 保留最近 1000 条：值 500 到 1499
        assert counter[subkey][0] == 500.0, f"第一个元素应为 500.0，实际 {counter[subkey][0]}"
        assert counter[subkey][-1] == 1499.0, f"最后一个元素应为 1499.0，实际 {counter[subkey][-1]}"

    def test_time_costs_no_truncation_under_1000(self) -> None:
        stat_data = StatisticOutputTask._build_stat_period_data()
        key = "time_costs_by_type"
        subkey = "chat"

        for i in range(500):
            StatisticOutputTask._append_defaultdict_list(stat_data, key, subkey, float(i))

        counter = stat_data[key]
        assert len(counter[subkey]) == 500, "未超限不应截断"
