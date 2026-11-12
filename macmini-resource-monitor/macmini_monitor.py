#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macmini-monitor: 轻量的 macOS CPU / 内存监控，持续异常时通过飞书告警。

设计目标：
  * 只依赖 macOS 自带的 Python 3 和系统命令（top / vm_stat / sysctl），无需 pip。
  * 单次运行完成一次采样和状态推进，由 launchd 每 60 秒调用一次；状态落盘，
    因此短时间重启、launchd 重试都不会丢失“持续了多久”的判断。
  * 告警必须持续一段时间才发送，恢复也需要持续一段时间才确认，并带冷却/重发间隔，
    避免瞬时尖峰和来回抖动造成告警轰炸。
  * 通知内容包含当前指标、触发条件、已持续时间和排障需要的信息。

子命令：
  once          执行一轮采样（launchd 使用；默认静默）
  status        查看当前指标、告警状态和 launchd 状态
  test-notify   发送一条明确标注的测试通知
  install       安装/更新 LaunchAgent（会复制脚本到稳定的应用目录）
  uninstall     卸载 LaunchAgent
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import datetime as _dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "macmini-monitor"
LAUNCHD_LABEL = "local.macmini-resource-monitor"
HOSTNAME = socket.gethostname().split(".")[0]

DEFAULT_CONFIG_PATH = Path("~/.config/macmini-monitor/config.json")
DEFAULT_APP_DIR = Path("~/Library/Application Support/macmini-monitor")
DEFAULT_LOG_DIR = Path("~/Library/Logs/macmini-monitor")
DEFAULT_LAUNCH_AGENT_DIR = Path("~/Library/LaunchAgents")

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
STATE_VERSION = 1

PRESSURE_NAMES = {1: "正常", 2: "警告", 4: "严重"}

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "sample_interval_seconds": 60,
    "cpu": {
        # 整机 busy% = 100 - idle%，来自 top 两次采样中的第二次（间隔 1 秒）。
        "alarm_busy_pct": 85.0,
        "clear_busy_pct": 65.0,
        "sustain_seconds": 600,
        "clear_seconds": 300,
        "remind_seconds": 3600,
    },
    "memory": {
        # available = free + inactive + speculative（macOS 可回收内存的常用口径）。
        "alarm_available_pct": 10.0,
        "clear_available_pct": 15.0,
        # 压缩内存占物理内存比例。
        "alarm_compressor_pct": 25.0,
        # 解除线比告警线低 5 个百分点，形成迟滞，避免在阈值附近反复告警。
        "clear_compressor_pct": 20.0,
        # Swap 已用量占物理内存比例；单独 Swap 高不算，需同时可用内存偏低。
        "alarm_swap_pct": 30.0,
        "alarm_swap_available_pct": 15.0,
        # 内核自己的内存压力等级：1=正常，2=警告，4=严重。
        "alarm_pressure_level": 2,
        "sustain_seconds": 600,
        "clear_seconds": 300,
        "remind_seconds": 3600,
    },
    "notify": {
        # 二选一：webhook_url 优先；否则用 lark-cli/lark-bot 以机器人身份发到 chat_id。
        "webhook_url": "",
        "lark_cli": "",
        "chat_id": "",
        "timeout_seconds": 15,
        "max_attempts": 3,
        "retry_delay_seconds": 2,
    },
    "paths": {
        "state_file": "~/Library/Application Support/macmini-monitor/state.json",
        "log_file": "~/Library/Logs/macmini-monitor/monitor.log",
        "lock_file": "~/Library/Application Support/macmini-monitor/.lock",
    },
}


class MonitorError(RuntimeError):
    """采样或配置错误。"""


class NotificationError(RuntimeError):
    """飞书通知发送失败。"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def iso_time(ts: Optional[float] = None) -> str:
    dt = _dt.datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def human_time(ts: Optional[float] = None) -> str:
    dt = _dt.datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_bytes(value: float) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def pressure_name(level: Optional[int]) -> str:
    if level is None:
        return "未知"
    return PRESSURE_NAMES.get(level, f"未知（{level}）")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def validate_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    def pct(path: str, value: Any, low: float = 0.0, high: float = 100.0) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{path} 必须是数字")
        elif not (low <= float(value) <= high):
            errors.append(f"{path} 必须在 {low:g}..{high:g} 之间")

    def positive(path: str, value: Any) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            errors.append(f"{path} 必须是正数")

    positive("sample_interval_seconds", config.get("sample_interval_seconds"))
    if isinstance(config.get("sample_interval_seconds"), (int, float)) and config["sample_interval_seconds"] < 10:
        errors.append("sample_interval_seconds 不能小于 10 秒")

    for section in ("cpu", "memory"):
        cfg = config.get(section)
        if not isinstance(cfg, dict):
            errors.append(f"{section} 配置缺失")
            continue
        for key in ("sustain_seconds", "clear_seconds", "remind_seconds"):
            positive(f"{section}.{key}", cfg.get(key))
        if isinstance(cfg.get("sustain_seconds"), (int, float)) and isinstance(cfg.get("remind_seconds"), (int, float)):
            if cfg["remind_seconds"] < cfg["sustain_seconds"]:
                errors.append(f"{section}.remind_seconds 不能小于 sustain_seconds")

    cpu = config.get("cpu", {})
    pct("cpu.alarm_busy_pct", cpu.get("alarm_busy_pct"))
    pct("cpu.clear_busy_pct", cpu.get("clear_busy_pct"))
    if isinstance(cpu.get("alarm_busy_pct"), (int, float)) and isinstance(cpu.get("clear_busy_pct"), (int, float)):
        if cpu["clear_busy_pct"] >= cpu["alarm_busy_pct"]:
            errors.append("cpu.clear_busy_pct 必须小于 cpu.alarm_busy_pct")

    mem = config.get("memory", {})
    for key in ("alarm_available_pct", "clear_available_pct", "alarm_compressor_pct",
                "clear_compressor_pct", "alarm_swap_pct", "alarm_swap_available_pct"):
        pct(f"memory.{key}", mem.get(key))
    if isinstance(mem.get("alarm_available_pct"), (int, float)) and isinstance(mem.get("clear_available_pct"), (int, float)):
        if mem["clear_available_pct"] <= mem["alarm_available_pct"]:
            errors.append("memory.clear_available_pct 必须大于 memory.alarm_available_pct")
    if isinstance(mem.get("alarm_compressor_pct"), (int, float)) and isinstance(mem.get("clear_compressor_pct"), (int, float)):
        if mem["clear_compressor_pct"] >= mem["alarm_compressor_pct"]:
            errors.append("memory.clear_compressor_pct 必须小于 memory.alarm_compressor_pct")
    if isinstance(mem.get("alarm_swap_available_pct"), (int, float)) and isinstance(mem.get("alarm_available_pct"), (int, float)):
        if mem["alarm_swap_available_pct"] < mem["alarm_available_pct"]:
            errors.append("memory.alarm_swap_available_pct 不能小于 memory.alarm_available_pct")

    notify = config.get("notify", {})
    positive("notify.timeout_seconds", notify.get("timeout_seconds"))
    positive("notify.max_attempts", notify.get("max_attempts"))
    if not isinstance(notify.get("max_attempts"), int):
        errors.append("notify.max_attempts 必须是整数")
    if not isinstance(notify.get("retry_delay_seconds"), (int, float)) or notify["retry_delay_seconds"] < 0:
        errors.append("notify.retry_delay_seconds 必须是非负数")
    chat_id = str(notify.get("chat_id") or "")
    webhook = str(notify.get("webhook_url") or "")
    lark_cli = str(notify.get("lark_cli") or "")
    if not webhook and not chat_id:
        errors.append("notify.chat_id 或 notify.webhook_url 必须配置一个")
    if not webhook and chat_id and not lark_cli:
        errors.append("使用 chat_id 时必须配置 notify.lark_cli（lark-cli/lark-bot 的绝对路径）")
    if chat_id and not chat_id.startswith("oc_"):
        errors.append("notify.chat_id 看起来不是飞书 chat_id（应以 oc_ 开头）")
    if webhook and not (webhook.startswith("https://") or webhook.startswith("http://")):
        errors.append("notify.webhook_url 必须是 http(s) URL")

    paths = config.get("paths", {})
    for key in ("state_file", "log_file", "lock_file"):
        if not paths.get(key):
            errors.append(f"paths.{key} 不能为空")

    return errors


def load_config(path: Path) -> Dict[str, Any]:
    path = expand_path(str(path))
    if not path.exists():
        raise MonitorError(
            f"配置文件不存在：{path}\n请先运行 install 生成，例如：\n"
            f"  python3 {Path(__file__).name} install --chat-id oc_xxx"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"读取配置文件失败：{path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise MonitorError(f"配置文件根节点必须是对象：{path}")
    config = deep_merge(DEFAULT_CONFIG, raw)
    errors = validate_config(config)
    if errors:
        raise MonitorError("配置无效：\n  - " + "\n  - ".join(errors))
    return config


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Metrics:
    sampled_at: float = 0.0
    cpu_busy_pct: float = 0.0
    cpu_user_pct: float = 0.0
    cpu_sys_pct: float = 0.0
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    ncpu: int = 1
    mem_total_bytes: int = 0
    mem_available_bytes: int = 0
    mem_free_bytes: int = 0
    mem_inactive_bytes: int = 0
    mem_compressor_bytes: int = 0
    swap_total_bytes: int = 0
    swap_used_bytes: int = 0
    pressure_level: Optional[int] = None
    disk_free_pct: float = 0.0
    pageouts: Optional[int] = None

    @property
    def mem_available_pct(self) -> float:
        return 100.0 * self.mem_available_bytes / self.mem_total_bytes if self.mem_total_bytes else 0.0

    @property
    def mem_compressor_pct(self) -> float:
        return 100.0 * self.mem_compressor_bytes / self.mem_total_bytes if self.mem_total_bytes else 0.0

    @property
    def swap_used_pct(self) -> float:
        # 与物理内存比较，Swap 总量本身会动态增长，用物理内存做分母更稳定。
        return 100.0 * self.swap_used_bytes / self.mem_total_bytes if self.mem_total_bytes else 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data.update(
            {
                "mem_available_pct": round(self.mem_available_pct, 2),
                "mem_compressor_pct": round(self.mem_compressor_pct, 2),
                "swap_used_pct": round(self.swap_used_pct, 2),
            }
        )
        return data


def _run(cmd: List[str], timeout: float = 15.0) -> str:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise MonitorError(f"命令不存在：{cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MonitorError(f"命令超时（{timeout:g}s）：{' '.join(cmd)}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MonitorError(f"命令失败（exit={proc.returncode}）：{' '.join(cmd)}：{detail[:300]}")
    return proc.stdout


def parse_top_cpu(text: str) -> Tuple[float, float, float]:
    matches = re.findall(
        r"^CPU usage:\s+([\d.]+)% user,\s+([\d.]+)% sys,\s+([\d.]+)% idle",
        text,
        re.MULTILINE,
    )
    if not matches:
        raise MonitorError("无法解析 top 的 CPU usage 行")
    user, sys, idle = matches[-1]
    return float(user), float(sys), float(idle)


def parse_load_avg(text: str) -> Tuple[float, float, float]:
    match = re.search(
        r"^Load Avg:\s+([\d.]+),\s+([\d.]+),\s+([\d.]+)",
        text,
        re.MULTILINE,
    )
    if not match:
        return 0.0, 0.0, 0.0
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def parse_vm_stat(text: str) -> Tuple[int, Dict[str, int]]:
    match = re.search(r"page size of (\d+) bytes", text)
    if not match:
        raise MonitorError("无法从 vm_stat 输出中解析 page size")
    page_size = int(match.group(1))
    pages: Dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^Pages ([A-Za-z][A-Za-z ]*?):\s+(\d+)\.?\s*$", line)
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    return page_size, pages


def _size_to_bytes(value: str, unit: str) -> int:
    factor = {
        "": 1,
        "B": 1,
        "K": 1024,
        "M": 1024 ** 2,
        "G": 1024 ** 3,
        "T": 1024 ** 4,
        "P": 1024 ** 5,
    }.get(unit.upper(), 1)
    return int(float(value) * factor)


def parse_swapusage(text: str) -> Tuple[int, int]:
    match = re.search(
        r"total\s*=\s*([\d.]+)\s*([KMGTPB]?)\s+used\s*=\s*([\d.]+)\s*([KMGTPB]?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise MonitorError("无法解析 vm.swapusage 输出")
    total = _size_to_bytes(match.group(1), match.group(2))
    used = _size_to_bytes(match.group(3), match.group(4))
    return total, used


def read_cpu() -> Tuple[float, float, float, float, float, float, int]:
    # top 第一行是自启动以来的累计值，第二行（-l 2 -s 1）才是这一秒的采样。
    top_out = _run(["/usr/bin/top", "-l", "2", "-n", "0", "-s", "1"], timeout=20.0)
    user, sys, idle = parse_top_cpu(top_out)
    busy = max(0.0, min(100.0, 100.0 - idle))
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1, load5, load15 = parse_load_avg(top_out)
    ncpu = os.cpu_count() or 1
    return busy, user, sys, load1, load5, load15, ncpu


def read_memory() -> Tuple[int, int, int, int, int, int, int, Optional[int], Optional[int]]:
    vm_out = _run(["/usr/bin/vm_stat"], timeout=10.0)
    page_size, pages = parse_vm_stat(vm_out)
    required = ("free", "inactive", "speculative", "occupied by compressor")
    missing = [name for name in required if name not in pages]
    if missing:
        raise MonitorError(f"vm_stat 缺少字段：{', '.join(missing)}")

    total_text = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=10.0).strip()
    try:
        mem_total = int(float(total_text))
    except ValueError as exc:
        raise MonitorError(f"无法解析 hw.memsize：{total_text!r}") from exc

    swap_text = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], timeout=10.0).strip()
    swap_total, swap_used = parse_swapusage(swap_text)

    pressure = None
    try:
        pressure_text = _run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            timeout=10.0,
        ).strip()
        pressure = int(pressure_text)
    except (MonitorError, ValueError):
        # 该 sysctl 在不同 macOS 版本上不保证存在；缺失时不影响其它指标。
        pressure = None

    free_pages = pages["free"]
    inactive_pages = pages["inactive"]
    speculative_pages = pages["speculative"]
    compressor_pages = pages["occupied by compressor"]

    free_bytes = free_pages * page_size
    inactive_bytes = inactive_pages * page_size
    available_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
    compressor_bytes = compressor_pages * page_size

    pageouts = None
    match = re.search(r"^Pageouts:\s+(\d+)\.?\s*$", vm_out, re.MULTILINE)
    if match:
        pageouts = int(match.group(1))

    return (
        mem_total,
        available_bytes,
        free_bytes,
        inactive_bytes,
        compressor_bytes,
        swap_total,
        swap_used,
        pressure,
        pageouts,
    )


def read_disk_free_pct() -> float:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return 0.0
    return 100.0 * usage.free / usage.total if usage.total else 0.0


def collect_metrics(now: Optional[float] = None) -> Metrics:
    sampled_at = time.time() if now is None else now
    busy, user, sys, load1, load5, load15, ncpu = read_cpu()
    (
        mem_total,
        available,
        free,
        inactive,
        compressor,
        swap_total,
        swap_used,
        pressure,
        pageouts,
    ) = read_memory()
    return Metrics(
        sampled_at=sampled_at,
        cpu_busy_pct=busy,
        cpu_user_pct=user,
        cpu_sys_pct=sys,
        load1=load1,
        load5=load5,
        load15=load15,
        ncpu=ncpu,
        mem_total_bytes=mem_total,
        mem_available_bytes=available,
        mem_free_bytes=free,
        mem_inactive_bytes=inactive,
        mem_compressor_bytes=compressor,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        pressure_level=pressure,
        disk_free_pct=read_disk_free_pct(),
        pageouts=pageouts,
    )


# ---------------------------------------------------------------------------
# 判断规则
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Verdict:
    alarm: bool
    clear: bool
    reasons: List[str]


def judge_cpu(metrics: Metrics, config: Dict[str, Any]) -> Verdict:
    cfg = config["cpu"]
    reasons: List[str] = []
    if metrics.cpu_busy_pct >= cfg["alarm_busy_pct"]:
        reasons.append(
            f"CPU 使用率 {metrics.cpu_busy_pct:.1f}% ≥ {cfg['alarm_busy_pct']:.0f}%"
        )
    clear = metrics.cpu_busy_pct <= cfg["clear_busy_pct"]
    return Verdict(alarm=bool(reasons), clear=clear, reasons=reasons)


def judge_memory(metrics: Metrics, config: Dict[str, Any]) -> Verdict:
    cfg = config["memory"]
    reasons: List[str] = []
    if metrics.mem_available_pct <= cfg["alarm_available_pct"]:
        reasons.append(
            f"可用内存 {metrics.mem_available_pct:.1f}% ≤ {cfg['alarm_available_pct']:.0f}%"
        )
    if metrics.mem_compressor_pct >= cfg["alarm_compressor_pct"]:
        reasons.append(
            f"压缩内存 {metrics.mem_compressor_pct:.1f}% ≥ {cfg['alarm_compressor_pct']:.0f}%"
        )
    if (
        metrics.swap_used_pct >= cfg["alarm_swap_pct"]
        and metrics.mem_available_pct <= cfg["alarm_swap_available_pct"]
    ):
        reasons.append(
            f"Swap {metrics.swap_used_pct:.1f}% ≥ {cfg['alarm_swap_pct']:.0f}% 且 "
            f"可用内存 ≤ {cfg['alarm_swap_available_pct']:.0f}%"
        )
    if (
        metrics.pressure_level is not None
        and metrics.pressure_level >= cfg["alarm_pressure_level"]
    ):
        reasons.append(
            f"内核内存压力为 {pressure_name(metrics.pressure_level)}（{metrics.pressure_level}）"
        )

    clear = (
        metrics.mem_available_pct >= cfg["clear_available_pct"]
        and metrics.mem_compressor_pct <= cfg["clear_compressor_pct"]
        and (
            metrics.pressure_level is None
            or metrics.pressure_level < cfg["alarm_pressure_level"]
        )
    )
    return Verdict(alarm=bool(reasons), clear=clear, reasons=reasons)


# ---------------------------------------------------------------------------
# 通知文本
# ---------------------------------------------------------------------------


def _metric_detail_lines(metric: str, metrics: Metrics) -> List[str]:
    if metric == "cpu":
        return [
            f"CPU：{metrics.cpu_busy_pct:.1f}%（{metrics.ncpu} 核）｜"
            f"Load {metrics.load1:.2f} / {metrics.load5:.2f} / {metrics.load15:.2f}"
        ]
    lines = [
        f"可用内存：{fmt_bytes(metrics.mem_available_bytes)} / "
        f"{fmt_bytes(metrics.mem_total_bytes)}（{metrics.mem_available_pct:.1f}%）",
        f"压缩内存：{fmt_bytes(metrics.mem_compressor_bytes)}"
        f"（{metrics.mem_compressor_pct:.1f}%）｜"
        f"Swap：{fmt_bytes(metrics.swap_used_bytes)}（{metrics.swap_used_pct:.1f}%）",
    ]
    if metrics.pressure_level is not None:
        lines.append(
            f"内核内存压力：{pressure_name(metrics.pressure_level)}（{metrics.pressure_level}）"
        )
    return lines


def build_notification_text(
    metric: str,
    kind: str,
    metrics: Metrics,
    reasons: List[str],
    active_since: float,
    now: float,
    host: str,
    clear_seconds: Optional[float] = None,
) -> str:
    label = "CPU 持续偏高" if metric == "cpu" else "内存紧张"
    if kind == "recovery":
        title = f"✅ Mac mini 警报解除：{label}"
    elif kind == "reminder":
        title = f"🔔 Mac mini 资源仍告警：{label}"
    else:
        title = f"⚠️ Mac mini 资源告警：{label}"

    lines = [title, f"主机：{host}"]
    lines.extend(_metric_detail_lines(metric, metrics))
    if kind != "recovery" and reasons:
        lines.append("触发条件：" + "；".join(reasons))
    if kind == "recovery":
        lines.append(f"本次告警持续：{fmt_duration(now - active_since)}")
        if clear_seconds is not None:
            lines.append(f"解除判定：恢复条件持续 {fmt_duration(clear_seconds)}")
    else:
        lines.append(f"已持续：{fmt_duration(now - active_since)}")
    lines.append(f"时间：{human_time(now)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PendingNotification:
    metric: str
    kind: str
    text: str
    key: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _new_metric_state() -> Dict[str, Any]:
    return {
        "bad_since": None,
        "clear_since": None,
        "active": False,
        "active_since": None,
        "announced": False,
        "last_notified_at": None,
        "last_notified_kind": None,
        "last_recovered_at": None,
        "last_notify_error": None,
        "pending": None,
    }


def new_state(host: Optional[str] = None) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "host": host or HOSTNAME,
        "created_at": time.time(),
        "last_sample_at": None,
        "last_metrics": None,
        "last_verdicts": {},
        "metrics": {"cpu": _new_metric_state(), "memory": _new_metric_state()},
    }


def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        return new_state()
    state.setdefault("version", STATE_VERSION)
    state.setdefault("host", HOSTNAME)
    state.setdefault("created_at", time.time())
    state.setdefault("last_sample_at", None)
    state.setdefault("last_metrics", None)
    state.setdefault("last_verdicts", {})
    if not isinstance(state.get("last_verdicts"), dict):
        state["last_verdicts"] = {}
    metrics = state.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
        state["metrics"] = metrics
    for name in ("cpu", "memory"):
        existing = metrics.get(name)
        merged = _new_metric_state()
        if isinstance(existing, dict):
            merged.update(existing)
        metrics[name] = merged
    return state


def load_state(path: Path) -> Dict[str, Any]:
    path = expand_path(str(path))
    if not path.exists():
        return new_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        corrupt = path.with_name(path.name + f".corrupt-{int(time.time())}")
        with contextlib.suppress(OSError):
            path.replace(corrupt)
        print(f"[warn] 状态文件损坏，已移至 {corrupt}：{exc}", file=sys.stderr)
        return new_state()
    return _normalize_state(raw)


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path = expand_path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def make_key(metric: str, kind: str, active_since: float, created_at: float) -> str:
    raw = f"{HOSTNAME}|{metric}|{kind}|{int(active_since)}|{int(created_at)}"
    return "mm-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _queue_notification(
    state: Dict[str, Any],
    metric: str,
    kind: str,
    metrics: Metrics,
    reasons: List[str],
    active_since: float,
    now: float,
    host: str,
    clear_seconds: Optional[float] = None,
) -> PendingNotification:
    text = build_notification_text(
        metric, kind, metrics, reasons, active_since, now, host, clear_seconds=clear_seconds
    )
    pending = PendingNotification(
        metric=metric,
        kind=kind,
        text=text,
        key=make_key(metric, kind, active_since, now),
        created_at=now,
    )
    state["metrics"][metric]["pending"] = pending.to_dict()
    return pending


def evaluate(
    state: Dict[str, Any],
    metrics: Metrics,
    config: Dict[str, Any],
    now: float,
    host: Optional[str] = None,
) -> List[PendingNotification]:
    """推进一次状态机，返回本轮新排队的通知（不发送、不落盘）。"""
    host = host or state.get("host") or HOSTNAME
    queued: List[PendingNotification] = []

    interval = float(config["sample_interval_seconds"])
    last_sample = state.get("last_sample_at")
    if last_sample is not None and now - float(last_sample) > max(interval * 2.5, 300.0):
        # 机器睡眠/重启/launchd 长时间未运行：不能把跨越空档的两次采样当成连续异常。
        for metric_state in state["metrics"].values():
            metric_state["bad_since"] = None
            metric_state["clear_since"] = None
        state["last_gap_reset_at"] = now

    judges = (("cpu", judge_cpu), ("memory", judge_memory))
    for metric, judge in judges:
        metric_state = state["metrics"].setdefault(metric, _new_metric_state())
        verdict = judge(metrics, config)
        settings = config[metric]
        state["last_verdicts"][metric] = {
            "alarm": verdict.alarm,
            "clear": verdict.clear,
            "reasons": verdict.reasons,
            "evaluated_at": now,
        }

        if not metric_state["active"]:
            if verdict.alarm:
                if metric_state["bad_since"] is None:
                    metric_state["bad_since"] = now
                if now - metric_state["bad_since"] >= settings["sustain_seconds"]:
                    metric_state["active"] = True
                    metric_state["active_since"] = metric_state["bad_since"]
                    metric_state["announced"] = False
                    metric_state["clear_since"] = None
                    metric_state["bad_since"] = None
                    metric_state["pending"] = None
                    pending = _queue_notification(
                        state,
                        metric,
                        "alarm",
                        metrics,
                        verdict.reasons,
                        metric_state["active_since"],
                        now,
                        host,
                    )
                    queued.append(pending)
            else:
                metric_state["bad_since"] = None
        else:
            if verdict.clear:
                if metric_state["clear_since"] is None:
                    metric_state["clear_since"] = now
                if now - metric_state["clear_since"] >= settings["clear_seconds"]:
                    active_since = metric_state["active_since"]
                    if active_since is None:
                        active_since = now
                    if metric_state["announced"]:
                        pending = _queue_notification(
                            state,
                            metric,
                            "recovery",
                            metrics,
                            [],
                            active_since,
                            now,
                            host,
                            clear_seconds=settings["clear_seconds"],
                        )
                        queued.append(pending)
                    else:
                        # 告警通知本身还没成功发出去就已经恢复了：不再补发过期告警，
                        # 也不需要发恢复通知。
                        metric_state["pending"] = None
                    metric_state["active"] = False
                    metric_state["active_since"] = None
                    metric_state["announced"] = False
                    metric_state["clear_since"] = None
                    metric_state["bad_since"] = None
                    metric_state["last_recovered_at"] = now
            else:
                metric_state["clear_since"] = None
                if (
                    metric_state["announced"]
                    and metric_state["pending"] is None
                    and metric_state["last_notified_at"] is not None
                    and now - metric_state["last_notified_at"] >= settings["remind_seconds"]
                ):
                    pending = _queue_notification(
                        state,
                        metric,
                        "reminder",
                        metrics,
                        verdict.reasons,
                        metric_state["active_since"]
                        if metric_state["active_since"] is not None
                        else now,
                        now,
                        host,
                    )
                    queued.append(pending)

    state["last_sample_at"] = now
    state["last_metrics"] = metrics.to_dict()
    state["last_evaluated_at"] = now
    state["host"] = host
    return queued


def mark_notification_sent(
    state: Dict[str, Any],
    metric: str,
    pending: Dict[str, Any],
    now: float,
) -> None:
    metric_state = state["metrics"].setdefault(metric, _new_metric_state())
    metric_state["pending"] = None
    metric_state["last_notified_at"] = now
    metric_state["last_notified_kind"] = pending.get("kind")
    metric_state["last_notify_error"] = None
    if pending.get("kind") in ("alarm", "reminder"):
        metric_state["announced"] = True


# ---------------------------------------------------------------------------
# 飞书通知
# ---------------------------------------------------------------------------


class FeishuNotifier:
    def __init__(self, config: Dict[str, Any]):
        notify = config["notify"]
        self.webhook_url = str(notify.get("webhook_url") or "").strip()
        self.chat_id = str(notify.get("chat_id") or "").strip()
        self.lark_cli = str(expand_path(str(notify.get("lark_cli") or "")))
        self.timeout = float(notify.get("timeout_seconds", 15))
        if not self.webhook_url and not (self.lark_cli and self.chat_id):
            raise NotificationError(
                "未配置通知方式：请设置 notify.webhook_url，或 notify.chat_id + notify.lark_cli"
            )

    def send(self, text: str, idempotency_key: str) -> None:
        if self.webhook_url:
            self._send_webhook(text)
        else:
            self._send_via_lark_cli(text, idempotency_key)

    def _send_webhook(self, text: str) -> None:
        payload = json.dumps(
            {"msg_type": "text", "content": {"text": text}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            raise NotificationError(f"飞书 webhook 请求失败：{exc}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise NotificationError(f"飞书 webhook 返回非 JSON：{body[:200]}") from exc
        code = data.get("code", data.get("StatusCode", 0))
        if code != 0:
            raise NotificationError(
                f"飞书 webhook 返回错误 code={code} msg={data.get('msg') or data.get('StatusMessage')}"
            )

    def _send_via_lark_cli(self, text: str, idempotency_key: str) -> None:
        if not self.lark_cli:
            raise NotificationError("未配置 notify.lark_cli")
        if not Path(self.lark_cli).exists():
            raise NotificationError(f"lark-cli 不存在：{self.lark_cli}")
        cmd = [
            self.lark_cli,
            "im",
            "+messages-send",
            "--chat-id",
            self.chat_id,
            "--text",
            text,
            "--format",
            "json",
            "--idempotency-key",
            idempotency_key[:50],
        ]
        env = os.environ.copy()
        env.setdefault("HOME", str(Path.home()))
        env["LANG"] = env.get("LANG") or "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise NotificationError(f"无法执行 lark-cli：{self.lark_cli}") from exc
        except subprocess.TimeoutExpired as exc:
            raise NotificationError(f"lark-cli 发送超时（{self.timeout:g}s）") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().replace(self.chat_id, "<chat_id>")
            raise NotificationError(f"lark-cli 发送失败（exit={proc.returncode}）：{detail[:400]}")
        # lark-cli 在部分错误场景仍可能以 0 退出，尽量解析 JSON 里的 code。
        body = (proc.stdout or "").strip()
        if body:
            try:
                data = json.loads(body)
                if isinstance(data, dict) and data.get("code") not in (None, 0):
                    raise NotificationError(
                        f"lark-cli 返回错误 code={data.get('code')} msg={data.get('msg')}"
                    )
            except json.JSONDecodeError:
                pass


def send_with_retry(
    notifier: FeishuNotifier,
    text: str,
    key: str,
    config: Dict[str, Any],
) -> None:
    attempts = max(1, int(config["notify"].get("max_attempts", 3)))
    delay = max(0.0, float(config["notify"].get("retry_delay_seconds", 2)))
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            notifier.send(text, key)
            return
        except NotificationError as exc:
            last_error = exc
            if attempt < attempts and delay:
                time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise NotificationError("发送失败")


# ---------------------------------------------------------------------------
# 日志与锁
# ---------------------------------------------------------------------------


def rotate_log(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        oldest = path.with_name(path.name + f".{LOG_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for index in range(LOG_BACKUPS - 1, 0, -1):
            src = path.with_name(path.name + f".{index}")
            dst = path.with_name(path.name + f".{index + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_name(path.name + ".1"))


def append_log(path: Path, level: str, event: str, **fields: Any) -> None:
    path = expand_path(str(path))
    record = {
        "ts": iso_time(),
        "level": level,
        "event": event,
        "host": HOSTNAME,
    }
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    if sys.stderr.isatty():
        print(line, file=sys.stderr)


def acquire_lock(path: Path) -> Optional[int]:
    path = expand_path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    return handle


def release_lock(handle: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(handle, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(handle)


# ---------------------------------------------------------------------------
# 一轮执行
# ---------------------------------------------------------------------------


def cmd_once(config_path: Path, dry_run: bool = False) -> int:
    config = load_config(config_path)
    paths = config["paths"]
    state_path = expand_path(paths["state_file"])
    log_path = expand_path(paths["log_file"])
    lock_path = expand_path(paths["lock_file"])

    lock_handle = acquire_lock(lock_path)
    if lock_handle is None:
        append_log(log_path, "WARN", "locked", message="另一轮仍在执行，跳过本轮")
        return 0

    try:
        state = load_state(state_path)
        now = time.time()
        try:
            metrics = collect_metrics(now)
        except MonitorError as exc:
            append_log(log_path, "ERROR", "sample_failed", error=str(exc))
            print(f"[error] 采样失败：{exc}", file=sys.stderr)
            return 1

        queued = evaluate(state, metrics, config, now, state.get("host"))
        pending_sent: List[str] = []
        pending_failed: List[str] = []
        if not dry_run:
            # 先把待发通知落盘，避免发送过程中进程被杀导致通知丢失。
            save_state(state_path, state)
            notifier: Optional[FeishuNotifier] = None
            for metric in ("cpu", "memory"):
                metric_state = state["metrics"][metric]
                pending = metric_state.get("pending")
                if not pending:
                    continue
                try:
                    if notifier is None:
                        notifier = FeishuNotifier(config)
                    send_with_retry(notifier, pending["text"], pending["key"], config)
                    mark_notification_sent(state, metric, pending, now)
                    pending_sent.append(f"{metric}:{pending['kind']}")
                    append_log(
                        log_path,
                        "INFO",
                        "notification_sent",
                        metric=metric,
                        kind=pending["kind"],
                        key=pending["key"],
                    )
                except (NotificationError, KeyError) as exc:
                    metric_state["last_notify_error"] = str(exc)
                    pending_failed.append(f"{metric}:{exc}")
                    append_log(
                        log_path,
                        "ERROR",
                        "notification_failed",
                        metric=metric,
                        kind=(pending or {}).get("kind"),
                        error=str(exc),
                    )
            if pending_sent or pending_failed:
                save_state(state_path, state)
        else:
            for metric in ("cpu", "memory"):
                pending = state["metrics"][metric].get("pending")
                if pending:
                    print(f"[dry-run] 待发送通知（{metric}/{pending['kind']}）：\n{pending['text']}\n")

        append_log(
            log_path,
            "INFO",
            "sample",
            metrics=metrics.to_dict(),
            queued=[f"{item.metric}:{item.kind}" for item in queued],
            sent=pending_sent,
            failed=pending_failed,
            active={
                "cpu": state["metrics"]["cpu"]["active"],
                "memory": state["metrics"]["memory"]["active"],
            },
        )

        if dry_run:
            print(
                f"[dry-run] CPU {metrics.cpu_busy_pct:.1f}% | "
                f"内存可用 {metrics.mem_available_pct:.1f}% | "
                f"CPU 告警={state['metrics']['cpu']['active']} | "
                f"内存告警={state['metrics']['memory']['active']}"
            )
        return 0
    finally:
        release_lock(lock_handle)


# ---------------------------------------------------------------------------
# 状态展示
# ---------------------------------------------------------------------------


def parse_launchctl_print(text: str) -> Dict[str, Any]:
    state_match = re.search(r"^\s*state = (.+?)\s*$", text, re.MULTILINE)
    pid_match = re.search(r"^\s*pid = (\d+)", text, re.MULTILINE)
    return {
        "loaded": True,
        "state": state_match.group(1) if state_match else None,
        "pid": int(pid_match.group(1)) if pid_match else None,
    }


def launchd_status() -> Dict[str, Any]:
    uid = os.getuid()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"loaded": False}
    return parse_launchctl_print(proc.stdout)


def _status_line_for_metric(name: str, state: Dict[str, Any], config: Dict[str, Any]) -> str:
    metric_state = state["metrics"][name]
    settings = config[name]
    if metric_state["active"]:
        active_since = metric_state["active_since"]
        duration = time.time() - (active_since if active_since is not None else time.time())
        last = metric_state["last_notified_at"]
        suffix = f"，最近通知 {human_time(last)}" if last else "，尚未成功通知"
        return f"告警中（已持续 {fmt_duration(duration)}{suffix}）"
    if metric_state["bad_since"] is not None:
        elapsed = time.time() - metric_state["bad_since"]
        return (
            f"观察中（异常已持续 {fmt_duration(elapsed)}，"
            f"达到 {fmt_duration(settings['sustain_seconds'])} 才告警）"
        )
    return "正常"


def cmd_status(config_path: Path, as_json: bool = False, sample: bool = False) -> int:
    config = load_config(config_path)
    state_path = expand_path(config["paths"]["state_file"])
    state = load_state(state_path)

    current: Optional[Metrics] = None
    current_error: Optional[str] = None
    if sample or not state.get("last_metrics"):
        try:
            current = collect_metrics()
        except MonitorError as exc:
            current_error = str(exc)

    if as_json:
        payload = {
            "config_path": str(expand_path(str(config_path))),
            "state_path": str(state_path),
            "state": state,
            "current_metrics": current.to_dict() if current else None,
            "current_error": current_error,
            "launchd": launchd_status(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    metrics_dict = current.to_dict() if current else state.get("last_metrics")
    print("Mac mini 资源监控")
    print(f"配置文件：{expand_path(str(config_path))}")
    print(f"状态文件：{state_path}")
    if metrics_dict:
        label = "当前" if current else "最近"
        print(
            f"{label}采样：{human_time(metrics_dict.get('sampled_at'))}"
        )
        print(
            f"CPU：{metrics_dict.get('cpu_busy_pct', 0):.1f}%｜"
            f"{metrics_dict.get('ncpu', 0)} 核｜"
            f"Load {metrics_dict.get('load1', 0):.2f} / "
            f"{metrics_dict.get('load5', 0):.2f} / {metrics_dict.get('load15', 0):.2f}"
        )
        print(
            f"内存：可用 {fmt_bytes(metrics_dict.get('mem_available_bytes', 0))} / "
            f"{fmt_bytes(metrics_dict.get('mem_total_bytes', 0))}"
            f"（{metrics_dict.get('mem_available_pct', 0):.1f}%）｜"
            f"压缩 {fmt_bytes(metrics_dict.get('mem_compressor_bytes', 0))}"
            f"（{metrics_dict.get('mem_compressor_pct', 0):.1f}%）｜"
            f"Swap {fmt_bytes(metrics_dict.get('swap_used_bytes', 0))}"
            f"（{metrics_dict.get('swap_used_pct', 0):.1f}%）"
        )
        pressure = metrics_dict.get("pressure_level")
        if pressure is None:
            print("内核内存压力：未知")
        else:
            print(f"内核内存压力：{pressure_name(pressure)}（{pressure}）")
        print(f"磁盘：根分区可用 {metrics_dict.get('disk_free_pct', 0):.1f}%")
    elif current_error:
        print(f"当前采样失败：{current_error}")
    else:
        print("还没有采样记录。")

    print(f"CPU：{_status_line_for_metric('cpu', state, config)}")
    print(f"内存：{_status_line_for_metric('memory', state, config)}")
    for name in ("cpu", "memory"):
        error = state["metrics"][name].get("last_notify_error")
        if error:
            print(f"  最近一次 {name} 通知失败：{error}")
    launchd = launchd_status()
    if launchd.get("loaded"):
        extra = f"，pid={launchd['pid']}" if launchd.get("pid") else ""
        display = {
            "not running": "空闲，等待下次采样",
            "running": "运行中",
            "spawn scheduled": "已排程",
        }.get(launchd.get("state"), launchd.get("state"))
        print(f"LaunchAgent：已加载（{display}{extra}）")
    else:
        print("LaunchAgent：未加载（运行 install 安装）")
    return 0


# ---------------------------------------------------------------------------
# 测试通知
# ---------------------------------------------------------------------------


def cmd_test_notify(config_path: Path, dry_run: bool = False) -> int:
    config = load_config(config_path)
    now = time.time()
    text = "\n".join(
        [
            "【测试】Mac mini 资源监控通知通道正常",
            f"主机：{HOSTNAME}",
            f"时间：{human_time(now)}",
            "说明：以后只会在 CPU / 内存持续异常时通知；本条为安装验证消息。",
        ]
    )
    if dry_run:
        print("[dry-run] 将发送以下测试通知：")
        print(text)
        return 0
    try:
        notifier = FeishuNotifier(config)
        notifier.send(text, "mm-test-" + str(int(now)))
    except NotificationError as exc:
        print(f"[error] 测试通知发送失败：{exc}", file=sys.stderr)
        return 1
    print("测试通知已发送到飞书。")
    return 0


# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def build_path_env() -> str:
    """launchd 的 PATH 很精简；lark-cli 的 shebang 需要 node。"""
    entries = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        str(Path("~/.local/bin").expanduser()),
    ]
    node = shutil.which("node")
    if node:
        node_dir = str(Path(node).resolve().parent)
        if node_dir not in entries:
            entries.insert(0, node_dir)
    return ":".join(entries)


def build_plist(
    script_path: Path,
    config_path: Path,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    log_dir = DEFAULT_LOG_DIR.expanduser()
    path_env = build_path_env()
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            sys.executable or "/usr/bin/python3",
            str(script_path),
            "--config",
            str(config_path),
            "once",
        ],
        "StartInterval": int(config["sample_interval_seconds"]),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "WorkingDirectory": str(DEFAULT_APP_DIR.expanduser()),
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": path_env,
            "LANG": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
        },
    }


def _write_config(path: Path, config: Dict[str, Any]) -> None:
    path = expand_path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_install(
    config_path: Path,
    chat_id: Optional[str],
    lark_cli: Optional[str],
    webhook_url: Optional[str],
    force: bool = False,
) -> int:
    config_path = expand_path(str(config_path))
    config: Dict[str, Any]
    if config_path.exists() and not force:
        # install 时允许修复一份尚未通过校验的配置（例如还没填 chat_id）。
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("根节点不是对象")
            config = deep_merge(DEFAULT_CONFIG, raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[error] 现有配置无法读取，拒绝覆盖：{exc}", file=sys.stderr)
            return 1
    else:
        config = copy.deepcopy(DEFAULT_CONFIG)

    if chat_id:
        config["notify"]["chat_id"] = chat_id
    if lark_cli:
        config["notify"]["lark_cli"] = lark_cli
    if webhook_url:
        config["notify"]["webhook_url"] = webhook_url
    if not chat_id:
        env_chat = os.environ.get("FEISHU_CHAT_ID") or os.environ.get("DSH_SUPERAGENT_FEISHU_CHAT")
        if env_chat and not config["notify"].get("chat_id"):
            config["notify"]["chat_id"] = env_chat

    if config["notify"].get("chat_id") and not config["notify"].get("lark_cli"):
        candidates = [
            Path("~/.local/bin/lark-bot").expanduser(),
            Path("~/.local/bin/lark-cli").expanduser(),
        ]
        found = next((str(item) for item in candidates if item.exists()), "")
        if not found:
            found = shutil.which("lark-cli") or shutil.which("lark-bot") or ""
        config["notify"]["lark_cli"] = found

    lark_value = config["notify"].get("lark_cli")
    if not config["notify"].get("webhook_url") and config["notify"].get("chat_id") and lark_value:
        if not Path(expand_path(str(lark_value))).exists():
            resolved = shutil.which(str(lark_value))
            if resolved:
                config["notify"]["lark_cli"] = resolved
            else:
                print(f"[error] lark-cli 不存在：{lark_value}", file=sys.stderr)
                return 1

    errors = validate_config(config)
    if errors:
        print("[error] 配置无效：\n  - " + "\n  - ".join(errors), file=sys.stderr)
        print(
            "提示：需要 --chat-id（或环境变量 FEISHU_CHAT_ID），"
            "或 --webhook-url 指定飞书自定义机器人 webhook。",
            file=sys.stderr,
        )
        return 1

    DEFAULT_APP_DIR.expanduser().mkdir(parents=True, exist_ok=True)
    DEFAULT_LOG_DIR.expanduser().mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    _write_config(config_path, config)

    target_script = DEFAULT_APP_DIR.expanduser() / "macmini_monitor.py"
    source_script = Path(__file__).resolve()
    if source_script != target_script:
        shutil.copy2(source_script, target_script)
    os.chmod(target_script, 0o755)

    plist_path = DEFAULT_LAUNCH_AGENT_DIR.expanduser() / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_data = plistlib.dumps(build_plist(target_script, config_path, config), fmt=plistlib.FMT_XML)
    plist_path.write_bytes(plist_data)
    os.chmod(plist_path, 0o644)

    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{LAUNCHD_LABEL}")
    result = _launchctl("bootstrap", f"gui/{uid}", str(plist_path))
    if result.returncode != 0:
        print(f"[error] launchctl bootstrap 失败：{result.stderr.strip()}", file=sys.stderr)
        return 1
    _launchctl("enable", f"gui/{uid}/{LAUNCHD_LABEL}")
    kick = _launchctl("kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}")
    if kick.returncode != 0:
        print(f"[warn] launchctl kickstart 失败：{kick.stderr.strip()}", file=sys.stderr)

    print("已安装并启动 Mac mini 资源监控。")
    print(f"  脚本：{target_script}")
    print(f"  配置：{config_path}")
    print(f"  launchd：{plist_path}")
    print(f"  日志：{DEFAULT_LOG_DIR.expanduser() / 'monitor.log'}")
    print("  查看状态：python3 " + str(target_script) + " status")
    print("  发送测试通知：python3 " + str(target_script) + " test-notify")
    return 0


def cmd_uninstall(purge: bool = False) -> int:
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{LAUNCHD_LABEL}")
    plist_path = DEFAULT_LAUNCH_AGENT_DIR.expanduser() / f"{LAUNCHD_LABEL}.plist"
    with contextlib.suppress(FileNotFoundError):
        plist_path.unlink()
    print("已卸载 LaunchAgent。")
    if purge:
        for path in (
            DEFAULT_APP_DIR.expanduser(),
            DEFAULT_LOG_DIR.expanduser(),
            DEFAULT_CONFIG_PATH.expanduser().parent,
        ):
            if path.exists():
                shutil.rmtree(path)
                print(f"已删除：{path}")
    else:
        print("配置、状态和日志已保留；加 --purge 可一并删除。")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mac mini CPU / 内存监控，持续异常时通过飞书通知。",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="执行一轮采样（launchd 使用）")
    once.add_argument("--dry-run", action="store_true", help="只采样和判断，不写状态、不发通知")

    status = sub.add_parser("status", help="查看当前指标与告警状态")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    status.add_argument("--sample", action="store_true", help="强制立即采样一次")

    test = sub.add_parser("test-notify", help="发送一条测试通知")
    test.add_argument("--dry-run", action="store_true", help="只打印，不实际发送")

    install = sub.add_parser("install", help="安装/更新 LaunchAgent")
    install.add_argument("--chat-id", default=None, help="飞书 chat_id（oc_ 开头）")
    install.add_argument("--lark-cli", default=None, help="lark-cli 或 lark-bot 的可执行文件路径")
    install.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook URL")
    install.add_argument("--force", action="store_true", help="用默认值重建配置")

    uninstall = sub.add_parser("uninstall", help="卸载 LaunchAgent")
    uninstall.add_argument("--purge", action="store_true", help="同时删除配置、状态和日志")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = expand_path(args.config)
    try:
        if args.command == "once":
            return cmd_once(config_path, dry_run=args.dry_run)
        if args.command == "status":
            return cmd_status(config_path, as_json=args.json, sample=args.sample)
        if args.command == "test-notify":
            return cmd_test_notify(config_path, dry_run=args.dry_run)
        if args.command == "install":
            return cmd_install(
                config_path,
                chat_id=args.chat_id,
                lark_cli=args.lark_cli,
                webhook_url=args.webhook_url,
                force=args.force,
            )
        if args.command == "uninstall":
            return cmd_uninstall(purge=args.purge)
    except MonitorError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
