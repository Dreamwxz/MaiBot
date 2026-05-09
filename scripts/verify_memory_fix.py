#!/usr/bin/env python3
"""MaiBot 内存修复验证脚本 —— 基于 tracemalloc 快照对比。

支持两种模式：
  1. --compare-existing: 自动查找并对比项目根目录下的已有快照文件
  2. --simulate: 开启 tracemalloc 运行模拟操作，定时拍摄快照并对比

用法:
  # 自动查找已有快照对比
  uv run python scripts/verify_memory_fix.py --compare-existing

  # 指定快照文件对比
  uv run python scripts/verify_memory_fix.py --snapshot1 <path> --snapshot2 <path>

  # 模拟模式（5 分钟）
  uv run python scripts/verify_memory_fix.py --simulate --duration 300

  # 指定通过阈值（默认 500 MiB）
  uv run python scripts/verify_memory_fix.py --compare-existing --threshold 300

  # 输出 JSON 格式
  uv run python scripts/verify_memory_fix.py --compare-existing -o json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

# ── 常量 ──────────────────────────────────────────────────────────────────────
SNAPSHOT_PATTERN = "tracemalloc_snapshot_*.pickle"
SNAPSHOT_FILENAME_RE = re.compile(
    r"tracemalloc_snapshot_(\d{8})_(\d{6})_pid(\d+)\.pickle"
)
DEFAULT_THRESHOLD_MB = 500
DEFAULT_TOP_N = 15
DEFAULT_DURATION_SEC = 300
DEFAULT_KEY_TYPES = ("filename", "lineno")


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def parse_snapshot_timestamp(path: Path) -> Optional[datetime]:
    """从快照文件名解析时间戳。

    文件名格式: tracemalloc_snapshot_YYYYMMDD_HHMMSS_pidNNNNNN.pickle

    Returns:
        datetime 对象，解析失败则返回 None。
    """
    m = SNAPSHOT_FILENAME_RE.search(path.name)
    if not m:
        return None
    date_str, time_str = m.group(1), m.group(2)
    try:
        return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _traceback_key_to_str(tb) -> str:
    """将 StatisticDiff.traceback 转为字符串。

    Python 3.13 中 StatisticDiff.traceback 返回 Traceback 对象（Frame 序列），
    需提取 filename（和 lineno）拼接为可读字符串。
    """
    if isinstance(tb, str):
        return tb
    # Traceback 是 Frame 的序列，取第一个（最顶层）frame
    try:
        frames = list(tb)
    except TypeError:
        return str(tb)
    if not frames:
        return "<unknown>"
    frame = frames[0]
    if getattr(frame, "lineno", 0) != 0:
        return f"{frame.filename}:{frame.lineno}"
    return frame.filename


def _shorten_path(path: str) -> str:
    """将冗长的 site-packages 路径缩短为 site-packages 相对路径。"""
    idx = path.find("site-packages/")
    if idx != -1:
        return "site-packages/" + path[idx + len("site-packages/"):]
    return path


def format_size_mb(size_bytes: int) -> str:
    """格式化字节数为人类可读的 MiB 字符串。"""
    mb = size_bytes / (1024 * 1024)
    if abs(mb) < 0.01:
        return f"{size_bytes:+d} B"
    return f"{mb:+.1f} MiB"


def format_count(count: int) -> str:
    """格式化计数，带符号。"""
    if count == 0:
        return "0"
    return f"{count:+d}"


def find_snapshots(root: Path) -> List[Path]:
    """在指定目录下查找所有 tracemalloc 快照文件，按时间戳排序。"""
    matches = sorted(root.glob(SNAPSHOT_PATTERN))
    # 按文件名中的时间戳排序（fallback 到 mtime）
    def _sort_key(p: Path) -> float:
        ts = parse_snapshot_timestamp(p)
        if ts is not None:
            return ts.timestamp()
        return p.stat().st_mtime

    return sorted(matches, key=_sort_key)


def load_snapshot(path: Path) -> tracemalloc.Snapshot:
    """加载 pickled tracemalloc 快照，带友好错误处理。"""
    if not path.exists():
        sys.exit(f"错误: 快照文件不存在: {path}")
    try:
        return tracemalloc.Snapshot.load(str(path))
    except Exception as e:
        sys.exit(f"错误: 无法加载快照文件 ({path}): {e}")


def module_from_filename(key: str) -> str:
    """从比较结果的 traceback key 中提取简短的模块名。

    对 filename 比较：缩短 site-packages 路径；提取 MaiBot 源模块路径。
    """
    shortened = _shorten_path(key)
    # 对于 MaiBot 内部模块，提取相对路径
    if "site-packages" not in shortened:
        # 尝试提取 src/ 之后的部分
        idx = shortened.find("/src/")
        if idx != -1:
            return shortened[idx + 1:]  # src/...
        # 尝试提取项目根目录之后的文件名
        parts = shortened.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return shortened


def group_by_module(stats: list) -> list:
    """将统计结果按模块聚合。

    对于 filename 比较结果：统计以 site-packages 包名或 src 模块名为聚合键。
    返回: [(module_label, total_size_diff, total_count_diff), ...] 按 size_diff 降序。
    """
    groups: dict[str, list] = {}
    for s in stats:
        mod = module_from_filename(_traceback_key_to_str(s.traceback))
        groups.setdefault(mod, []).append(s)
    aggregated = []
    for mod, items in groups.items():
        total_size = sum(it.size_diff for it in items)
        total_count = sum(it.count_diff for it in items)
        aggregated.append((mod, total_size, total_count))
    aggregated.sort(key=lambda x: x[1], reverse=True)
    return aggregated


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report(
    snapshot1_path: Path,
    snapshot2_path: Path,
    stats_list: list,
    key_type: str,
    threshold_mb: float,
    top_n: int,
) -> str:
    """生成格式化的内存增长报告。"""
    total_diff = sum(s.size_diff for s in stats_list)
    total_mb = total_diff / (1024 * 1024)
    ts1 = parse_snapshot_timestamp(snapshot1_path)
    ts2 = parse_snapshot_timestamp(snapshot2_path)
    delta_str = ""
    if ts1 and ts2:
        delta = ts2 - ts1
        delta_str = f"{delta}"
    else:
        delta_str = "未知"

    verdict = "PASS" if total_mb <= threshold_mb else "FAIL"

    lines = []
    lines.append("=" * 60)
    lines.append("=== MaiBot 内存增长报告 ===")
    lines.append("=" * 60)
    lines.append(f"  快照 1: {snapshot1_path} ({ts1.strftime('%Y-%m-%d %H:%M:%S') if ts1 else '未知'})")
    lines.append(f"  快照 2: {snapshot2_path} ({ts2.strftime('%Y-%m-%d %H:%M:%S') if ts2 else '未知'})")
    lines.append(f"  时间跨度: {delta_str}")
    lines.append(f"  比较维度: {key_type}")
    lines.append("")

    # 按模块聚合
    by_module = group_by_module(stats_list)
    lines.append(f"  内存增长最多的模块 (Top {min(top_n, len(by_module))}):")
    lines.append(f"  {'排名':<5} {'模块':<55} {'增长量':>12} {'占比':>8}")
    lines.append(f"  {'-'*5} {'-'*55} {'-'*12} {'-'*8}")

    for rank, (mod, size_diff, count_diff) in enumerate(by_module[:top_n], 1):
        size_mb = size_diff / (1024 * 1024)
        pct = (size_diff / total_diff * 100) if total_diff else 0.0
        lines.append(f"  {rank:<5} {mod:<55} {size_mb:+12.1f} MiB {pct:>7.1f}%")

    if len(by_module) > top_n:
        lines.append(f"  ... 还有 {len(by_module) - top_n} 个模块")

    lines.append("")
    lines.append(f"  按行号增长最多的条目 (Top {min(top_n, len(stats_list))}):")
    lines.append(f"  {'排名':<5} {'文件:行号':<75} {'增长量':>12} {'计数':>10}")
    lines.append(f"  {'-'*5} {'-'*75} {'-'*12} {'-'*10}")

    sorted_stats = sorted(stats_list, key=lambda s: s.size_diff, reverse=True)
    for rank, s in enumerate(sorted_stats[:top_n], 1):
        size_str = format_size_mb(s.size_diff)
        count_str = format_count(s.count_diff)
        label = _shorten_path(_traceback_key_to_str(s.traceback))
        lines.append(f"  {rank:<5} {label:<75} {size_str:>12} {count_str:>10}")

    if len(sorted_stats) > top_n:
        lines.append(f"  ... 还有 {len(sorted_stats) - top_n} 条记录")

    lines.append("")
    lines.append(f"  总内存增长: {total_mb:.1f} MiB")
    lines.append(f"  判定阈值: {threshold_mb:.0f} MiB")
    lines.append(f"  判定结果: {verdict}")
    lines.append("=" * 60)

    if verdict == "FAIL":
        lines.append(f"  ⚠️  内存增长超过阈值 {threshold_mb:.0f} MiB，可能存在内存泄漏！")
    else:
        lines.append(f"  ✓  内存增长在阈值 {threshold_mb:.0f} MiB 以内，通过验证。")
    lines.append("=" * 60)

    return "\n".join(lines)


def generate_json_report(
    snapshot1_path: Path,
    snapshot2_path: Path,
    stats_list_aggregated: list,
    total_diff_bytes: int,
    threshold_mb: float,
    key_type: str,
) -> str:
    """生成 JSON 格式报告。"""
    ts1 = parse_snapshot_timestamp(snapshot1_path)
    ts2 = parse_snapshot_timestamp(snapshot2_path)
    total_mb = total_diff_bytes / (1024 * 1024)

    report = {
        "snapshot1": str(snapshot1_path),
        "snapshot1_timestamp": ts1.isoformat() if ts1 else None,
        "snapshot2": str(snapshot2_path),
        "snapshot2_timestamp": ts2.isoformat() if ts2 else None,
        "duration_seconds": (ts2 - ts1).total_seconds() if ts1 and ts2 else None,
        "key_type": key_type,
        "total_growth_mb": round(total_mb, 2),
        "threshold_mb": threshold_mb,
        "verdict": "PASS" if total_mb <= threshold_mb else "FAIL",
        "top_modules": [
            {
                "rank": i + 1,
                "module": mod,
                "growth_mb": round(size_diff / (1024 * 1024), 2),
                "pct": round((size_diff / total_diff_bytes * 100) if total_diff_bytes else 0.0, 2),
            }
            for i, (mod, size_diff, _count_diff) in enumerate(stats_list_aggregated)
        ],
    }
    return json.dumps(report, ensure_ascii=False, indent=2)


# ── 比较快照 ──────────────────────────────────────────────────────────────────

def compare_snapshots(
    snapshot1: tracemalloc.Snapshot,
    snapshot2: tracemalloc.Snapshot,
    snapshot1_path: Path,
    snapshot2_path: Path,
    key_types: Tuple[str, ...] = ("filename", "lineno"),
    threshold_mb: float = DEFAULT_THRESHOLD_MB,
    top_n: int = DEFAULT_TOP_N,
    output_format: str = "text",
) -> None:
    """比较两个快照并打印报告。"""
    if output_format == "json":
        reports = []
        for key_type in key_types:
            stats = snapshot2.compare_to(snapshot1, key_type)
            growing = [s for s in stats if s.size_diff > 0]
            growing.sort(key=lambda s: s.size_diff, reverse=True)
            total_diff = sum(s.size_diff for s in growing)
            aggregated = group_by_module(growing)
            reports.append(json.loads(generate_json_report(
                snapshot1_path, snapshot2_path,
                aggregated, total_diff, threshold_mb, key_type,
            )))
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for key_type in key_types:
            stats = snapshot2.compare_to(snapshot1, key_type)
            growing = [s for s in stats if s.size_diff > 0]
            growing.sort(key=lambda s: s.size_diff, reverse=True)
            print(generate_report(
                snapshot1_path, snapshot2_path,
                growing, key_type, threshold_mb, top_n,
            ))
            if key_type != key_types[-1]:
                print()


# ── 模拟运行模式 ──────────────────────────────────────────────────────────────

def _simulate_workload() -> None:
    """模拟典型的 MaiBot 操作，产生可追踪的内存分配。

    注意：此函数不依赖 MaiBot 运行时，使用标准库操作模拟：
      - 大量列表/字典操作（模拟消息处理缓冲区）
      - 字符串拼接（模拟日志和提示词构建）
      - JSON 序列化/反序列化（模拟 API 调用）
      - 正则表达式匹配（模拟消息解析）
      - 临时大对象创建与释放
    """

    messages = []
    for _ in range(200):
        messages.append({
            "role": random.choice(["user", "assistant", "system"]),
            "content": "这是一条模拟消息 " + "内容" * random.randint(10, 500),
            "timestamp": time.time(),
        })

    # 模拟 JSON 序列化
    for _ in range(10):
        _ = json.dumps(messages)
        _ = json.loads(json.dumps(messages[:50]))

    # 模拟字符串操作（日志、提示词构建）
    log_lines = []
    for i in range(1000):
        log_lines.append(
            f"[{datetime.now().isoformat()}] [INFO] "
            f"模拟日志行 #{i}: 处理消息 {random.randint(1, 99999)} "
            f"hash={hashlib.md5(str(i).encode()).hexdigest()}"
        )
    _ = "\n".join(log_lines)

    # 模拟正则匹配（消息解析）
    pattern = re.compile(r"@\w+|#[0-9a-fA-F]{6}|https?://\S+")
    for msg in messages:
        _ = pattern.findall(msg["content"])

    # 模拟临时大列表（会被 GC）
    _ = [[random.random() for _ in range(1000)] for _ in range(20)]


def run_simulation(
    duration_sec: float = DEFAULT_DURATION_SEC,
    snapshot_interval_sec: float = 60.0,
    threshold_mb: float = DEFAULT_THRESHOLD_MB,
    top_n: int = DEFAULT_TOP_N,
    output_format: str = "text",
) -> None:
    """以 tracemalloc 追踪的模式运行模拟工作负载。

    在模拟过程中定期拍摄快照，最后比较首末快照。

    Args:
        duration_sec: 模拟运行总时长（秒）
        snapshot_interval_sec: 快照间隔（秒）
        threshold_mb: 判定阈值（MiB）
        top_n: 报告中显示的条目数
        output_format: 输出格式 ('text' 或 'json')
    """
    print(f"开始模拟，时长 {duration_sec} 秒，采样间隔 {snapshot_interval_sec} 秒...", file=sys.stderr)
    print("提示: 使用 Ctrl+C 可提前结束\n", file=sys.stderr)

    tracemalloc.start(25)

    start_time = time.time()
    next_snapshot_time = start_time + snapshot_interval_sec

    baseline_snapshot = tracemalloc.take_snapshot()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 初始快照已拍摄", file=sys.stderr)

    snapshot_count = 0
    try:
        while time.time() - start_time < duration_sec:
            _simulate_workload()
            time.sleep(0.1)

            now = time.time()
            if now >= next_snapshot_time:
                snapshot = tracemalloc.take_snapshot()
                snapshot_count += 1
                stats = snapshot.compare_to(baseline_snapshot, "filename")
                growing = [s for s in stats if s.size_diff > 0]
                total_mb = sum(s.size_diff for s in growing) / (1024 * 1024)
                elapsed = now - start_time
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"快照 #{snapshot_count} (已运行 {elapsed:.0f}s): "
                    f"累计增长 {total_mb:.1f} MiB",
                    file=sys.stderr,
                )
                next_snapshot_time = now + snapshot_interval_sec
    except KeyboardInterrupt:
        print("\n用户中断模拟。", file=sys.stderr)

    final_snapshot = tracemalloc.take_snapshot()
    tracemalloc.stop()

    elapsed = time.time() - start_time
    print(f"\n模拟结束，实际运行 {elapsed:.0f} 秒。", file=sys.stderr)
    print(f"\n比较初始快照和最终快照 (间隔 {elapsed:.0f}s):\n", file=sys.stderr)
    compare_snapshots(
        baseline_snapshot,
        final_snapshot,
        snapshot1_path=Path("(baseline - simulation start)"),
        snapshot2_path=Path("(final - simulation end)"),
        key_types=DEFAULT_KEY_TYPES,
        threshold_mb=threshold_mb,
        top_n=top_n,
        output_format=output_format,
    )


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MaiBot 内存修复验证 — 基于 tracemalloc 快照对比",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --compare-existing
  %(prog)s --snapshot1 snap1.pickle --snapshot2 snap2.pickle
  %(prog)s --simulate --duration 300
  %(prog)s --compare-existing --threshold 300 -o json
        """,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--compare-existing",
        action="store_true",
        help="自动查找并对比项目根目录下的已有快照文件",
    )
    mode_group.add_argument(
        "--simulate",
        action="store_true",
        help="运行模拟模式，定时拍摄并对比快照",
    )

    parser.add_argument(
        "--snapshot1",
        type=Path,
        default=None,
        help="第一个快照文件路径",
    )
    parser.add_argument(
        "--snapshot2",
        type=Path,
        default=None,
        help="第二个快照文件路径",
    )
    parser.add_argument(
        "--key-type",
        choices=["filename", "lineno", "all"],
        default="all",
        help="比较维度 (默认: all, 即同时比较 filename 和 lineno)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_MB,
        help=f"判定阈值 MiB (默认: {DEFAULT_THRESHOLD_MB})",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"报告中显示的条目数 (默认: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SEC,
        help=f"模拟模式运行时长秒数 (默认: {DEFAULT_DURATION_SEC})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="模拟模式下快照采样间隔秒数 (默认: 60)",
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=["text", "json"],
        default="text",
        help="输出格式 (默认: text)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="查找快照文件的根目录 (默认: 项目根目录)",
    )
    return parser.parse_args()


def _resolve_project_root(args_root: Optional[Path]) -> Path:
    """解析项目根目录。"""
    if args_root is not None:
        return args_root.resolve()
    # 默认：此脚本所在目录的父目录（即项目根目录）
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent


def main() -> None:
    args = _parse_args()
    project_root = _resolve_project_root(args.root)

    # ── 模拟模式 ──
    if args.simulate:
        run_simulation(
            duration_sec=args.duration,
            snapshot_interval_sec=args.interval,
            threshold_mb=args.threshold,
            top_n=args.top,
            output_format=args.output,
        )
        return

    # ── 快照对比模式 ──
    if args.compare_existing:
        snapshots = find_snapshots(project_root)
        if len(snapshots) < 2:
            sys.exit(
                f"错误: 在 {project_root} 中找到了 {len(snapshots)} 个快照文件，需要至少 2 个。\n"
                f"  找到的文件: {[s.name for s in snapshots]}"
            )
        snapshot1_path = snapshots[0]
        snapshot2_path = snapshots[-1]
        print(f"自动选择快照: {snapshot1_path.name} -> {snapshot2_path.name}\n", file=sys.stderr)

    elif args.snapshot1 and args.snapshot2:
        snapshot1_path = args.snapshot1.resolve()
        snapshot2_path = args.snapshot2.resolve()
    else:
        sys.exit(
            "错误: 请指定比较模式。\n"
            "  选项 1: --compare-existing (自动查找已有快照)\n"
            "  选项 2: --snapshot1 <path> --snapshot2 <path> (指定快照文件)\n"
            "  选项 3: --simulate (运行模拟模式)"
        )

    # 加载快照
    snapshot1 = load_snapshot(snapshot1_path)
    snapshot2 = load_snapshot(snapshot2_path)

    if args.key_type == "all":
        key_types = DEFAULT_KEY_TYPES
    else:
        key_types = (args.key_type,)

    compare_snapshots(
        snapshot1,
        snapshot2,
        snapshot1_path,
        snapshot2_path,
        key_types=key_types,
        threshold_mb=args.threshold,
        top_n=args.top,
        output_format=args.output,
    )


if __name__ == "__main__":
    main()
