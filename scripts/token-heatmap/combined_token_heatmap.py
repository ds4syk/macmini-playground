#!/usr/bin/env python3
"""Combined DSH token usage for several machines, with a calendar heatmap.

The script reads provider-reported ``usage`` from every local DSH session log
(v4 and pre-v4), deduplicates assistant messages that were copied into forked
sessions, aggregates by machine / model / day / month, and estimates an API
cost:

  * DeepSeek routes use DeepSeek's official CNY list prices.  Every request is
    priced at its own peak/off-peak rate (peak = Beijing weekday 09:00-12:00
    and 14:00-18:00, excluding statutory holidays).
  * Other routes use https://models.dev/api.json, converted at ``--usd-cny``.

The heatmap and the report are rendered locally; the lab server only has to
provide its ``~/.dsh/sessions`` directory (see README for the rsync command).

Usage:
  python3 scripts/token-heatmap/combined_token_heatmap.py \
      --source local=~/.dsh/sessions \
      --source lab=/tmp/lab-dsh-sessions \
      --out-dir scripts/token-heatmap
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import shutil
import subprocess
import sys
import tempfile
import time as time_module
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

HOME = Path.home()
TZ = ZoneInfo("Asia/Shanghai")
CACHE_FILE = HOME / ".cache" / "dsh-token-usage" / "models.dev.json"
ZSTD = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
MODELS_DEV_URL = "https://models.dev/api.json"
CARD_WIDTH = 1241

MACHINE_NAMES = {
    "local": "本机 macmini",
    "lab": "lab server",
}

# DeepSeek official CNY list prices per million tokens (peak rates, checked
# against api-docs.deepseek.com on 2026-11-26).  Off-peak is half price; cache
# writes are billed as cache-miss input.
DEEPSEEK_CNY_PEAK = {
    "deepseek-flash": {"cacheHit": 0.04, "cacheMiss": 2.0, "output": 8.0},
    "deepseek-v4-pro": {"cacheHit": 0.30, "cacheMiss": 9.0, "output": 27.0},
}
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4.1-flash": "deepseek-flash",
}
PROVIDER_MODEL_ALIASES = {
    "opencode-go": {"deepseek-v4-flash": "deepseek-v4.1-flash"},
}
DEEPSEEK_PROVIDERS = ("deepseek-official", "deepseek-account")
PEAK_WINDOWS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
BUILTIN_HOLIDAYS = {
    "2026": (
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
        "2026-10-06", "2026-10-07",
    ),
}
PROVIDER_CANDIDATES = {
    "zai-coding-cn": ("zhipuai-coding-plan", "zai-coding-plan", "zhipuai", "zai"),
    "zai": ("zai", "zai-coding-plan"),
    "deepseek-official": ("deepseek",),
    "deepseek-account": ("deepseek",),
    "opencode-go": ("opencode-go", "opencode"),
    "moonshotai-cn": ("moonshotai-cn", "moonshotai"),
}
LIST_PRICE_PROVIDERS = ("zai", "zhipuai", "tencent-token-plan", "openrouter", "deepseek")


# --------------------------------------------------------------------------
# data model


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    requests: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.requests += other.requests

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
            "total": self.total,
            "requests": self.requests,
        }


@dataclass
class Row:
    machine: str
    session: str
    moment: datetime
    provider: str
    model: str
    usage: Usage
    kind: str
    cost_cny: float = 0.0
    priced: bool = True


@dataclass
class Machine:
    label: str
    name: str
    usage: Usage = field(default_factory=Usage)
    sessions: int = 0
    session_logs: int = 0
    cost_cny: float = 0.0
    by_month: Dict[str, Usage] = field(default_factory=dict)


@dataclass
class ModelUsage:
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_cny: float = 0.0
    priced: bool = True
    price_source: str = ""
    by_machine: Dict[str, Usage] = field(default_factory=dict)
    list_price_usd: Optional[float] = None
    list_price_source: str = ""


# --------------------------------------------------------------------------
# collection


def read_events(path: str) -> Iterable[dict]:
    raw = subprocess.run([ZSTD, "-dc", str(path)], capture_output=True, timeout=600)
    # A session that is still being appended may fail the final frame check;
    # its already-written events are still valid, so parse whatever came out.
    for line in raw.stdout.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def session_logs(root: Path) -> Dict[str, Path]:
    """One log per session directory, preferring the v4 migration."""
    chosen: Dict[str, Tuple[int, Path]] = {}
    for priority, pattern in enumerate(("session.v4.jsonl.zstd", "session.jsonl.zstd")):
        for path in glob.glob(str(root / "*" / "*" / pattern)):
            directory = str(Path(path).parent)
            if directory not in chosen or priority < chosen[directory][0]:
                chosen[directory] = (priority, Path(path))
    return {directory: path for directory, (_, path) in chosen.items()}


def _usage_from(raw: dict) -> Usage:
    return Usage(
        input=int(raw.get("inputTokens") or 0),
        output=int(raw.get("outputTokens") or 0),
        cache_read=int(raw.get("cacheReadTokens") or 0),
        cache_write=int(raw.get("cacheWriteTokens") or 0),
        requests=1,
    )


def scan_source(label: str, root: Path) -> Tuple[List[Row], int, int]:
    """Return (rows, session log count, sessions with usage).

    A forked/seeded session copies its parent's assistant messages.  They keep
    the parent message id, so the same id is only billed once; the earliest
    timestamp wins because that is where the call actually happened.
    """
    logs = session_logs(root)
    rows: Dict[str, Row] = {}
    sessions_with_usage = set()
    for directory, path in logs.items():
        session_id = Path(directory).name
        step_start = None
        for event in read_events(str(path)):
            event_type = event.get("type")
            timestamp = event.get("time")
            if event_type == "step/start":
                if isinstance(timestamp, (int, float)):
                    step_start = int(timestamp)
                continue
            if event_type == "step/end":
                step_start = None
                continue
            if event_type == "assistant/message":
                data = event.get("data") or {}
                raw_usage = data.get("usage")
                source = (data.get("message") or {}).get("source") or {}
                if not raw_usage or source.get("kind") != "model":
                    continue
                message_id = (data.get("message") or {}).get("id") or ""
                row_id = message_id or f"{session_id}:{event.get('seq')}"
                provider = source.get("provider") or "unknown"
                model = source.get("model") or "unknown"
                kind = "message"
            elif event_type == "compaction/summary":
                data = event.get("data") or {}
                raw_usage = data.get("usage")
                if not raw_usage:
                    continue
                row_id = f"{session_id}:compaction:{event.get('seq')}"
                provider = data.get("provider") or "unknown"
                model = data.get("model") or "unknown"
                kind = "compaction"
            else:
                continue
            if not isinstance(timestamp, (int, float)):
                continue
            moment_ms = step_start if step_start is not None else int(timestamp)
            usage = _usage_from(raw_usage)
            if usage.total == 0:
                continue
            row = Row(
                machine=label,
                session=session_id,
                moment=datetime.fromtimestamp(moment_ms / 1000, TZ),
                provider=provider,
                model=model,
                usage=usage,
                kind=kind,
            )
            old = rows.get(row_id)
            if old is None or row.moment < old.moment:
                rows[row_id] = row
            sessions_with_usage.add(session_id)
    return list(rows.values()), len(logs), len(sessions_with_usage)


# --------------------------------------------------------------------------
# pricing


def is_deepseek_provider(provider: str) -> bool:
    return provider in DEEPSEEK_PROVIDERS or provider.lower().startswith("deepseek")


def canonical_model(provider: str, model: str) -> str:
    return MODEL_ALIASES.get(model, model) if is_deepseek_provider(provider) else model


def is_peak_time(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    if moment.strftime("%Y-%m-%d") in BUILTIN_HOLIDAYS.get(str(moment.year), ()):
        return False
    minutes = moment.hour * 60 + moment.minute
    return any(start <= minutes < end for start, end in PEAK_WINDOWS)


def deepseek_cost_cny(usage: Usage, prices: dict, peak: bool) -> float:
    factor = (1.0 if peak else 0.5) / 1_000_000
    return (
        (usage.input + usage.cache_write) * prices["cacheMiss"]
        + usage.cache_read * prices["cacheHit"]
        + usage.output * prices["output"]
    ) * factor


def load_models_dev(refresh: bool, cache_max_age_hours: float) -> Tuple[dict, str]:
    if CACHE_FILE.exists() and not refresh:
        age = time_module.time() - CACHE_FILE.stat().st_mtime
        if age <= cache_max_age_hours * 3600:
            try:
                data = json.loads(CACHE_FILE.read_text())
                return data.get("providers") or data, data.get("fetchedAt", "")
            except (OSError, json.JSONDecodeError):
                pass
    try:
        request = urllib.request.Request(
            MODELS_DEV_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; dsh-token-heatmap/1.0)"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps({"fetchedAt": fetched_at, "providers": data}, ensure_ascii=False)
        )
        return data, fetched_at
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"[warn] models.dev fetch failed: {exc}", file=sys.stderr)
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text())
                return data.get("providers") or data, data.get("fetchedAt", "")
            except (OSError, json.JSONDecodeError):
                pass
    return {}, ""


def models_dev_entry(catalog: dict, provider: str, model: str) -> Tuple[Optional[dict], str, str]:
    aliases = PROVIDER_MODEL_ALIASES.get(provider, {})
    model_candidates = [aliases[model], model] if model in aliases else [model]
    for candidate_provider in PROVIDER_CANDIDATES.get(provider, (provider,)):
        models = (catalog.get(candidate_provider) or {}).get("models") or {}
        for candidate_model in model_candidates:
            if candidate_model and candidate_model in models:
                return models[candidate_model], candidate_provider, candidate_model
    matches = [
        (pid, (entry.get("models") or {})[model])
        for pid, entry in catalog.items()
        if model in (entry.get("models") or {})
    ]
    if len(matches) == 1:
        return matches[0][1], matches[0][0], model
    return None, "", ""


def find_metered_price(catalog: dict, model: str) -> Tuple[Optional[dict], str]:
    ordered = [pid for pid in LIST_PRICE_PROVIDERS if pid in catalog]
    ordered += [pid for pid in catalog if pid not in LIST_PRICE_PROVIDERS]
    for pid in ordered:
        item = (catalog.get(pid) or {}).get("models", {}).get(model)
        cost = (item or {}).get("cost") or {}
        if cost.get("input") or cost.get("output"):
            return item, pid
    return None, ""


def price_models_dev(usage: Usage, item: dict, usd_cny: float) -> float:
    cost = item.get("cost") or {}
    input_price = float(cost.get("input") or 0)
    output_price = float(cost.get("output") or 0)
    cache_read_price = float(cost.get("cache_read") or 0)
    cache_write_price = cost.get("cache_write")
    cache_write_price = input_price if cache_write_price is None else float(cache_write_price)
    usd = (
        usage.input * input_price
        + usage.output * output_price
        + usage.cache_read * cache_read_price
        + usage.cache_write * cache_write_price
    ) / 1_000_000
    return usd * usd_cny


def price_rows(rows: List[Row], catalog: dict, usd_cny: float) -> List[ModelUsage]:
    entry_cache: Dict[Tuple[str, str], Tuple[Optional[dict], str, str]] = {}
    route_machine: Dict[Tuple[str, str], Dict[str, Usage]] = defaultdict(dict)
    models: Dict[Tuple[str, str], ModelUsage] = {}
    for row in rows:
        model = canonical_model(row.provider, row.model)
        key = (row.provider, model)
        entry, matched_provider, matched_model = entry_cache.get(key, (None, "", ""))
        if key not in entry_cache and catalog:
            entry, matched_provider, matched_model = models_dev_entry(catalog, row.provider, model)
            entry_cache[key] = (entry, matched_provider, matched_model)
        if is_deepseek_provider(row.provider) and model in DEEPSEEK_CNY_PEAK:
            row.cost_cny = deepseek_cost_cny(row.usage, DEEPSEEK_CNY_PEAK[model], is_peak_time(row.moment))
            row.priced = True
            source = "DeepSeek 官方价目"
        elif entry is not None:
            row.cost_cny = price_models_dev(row.usage, entry, usd_cny)
            row.priced = True
            source = f"models.dev · {matched_provider}"
            if matched_model and matched_model != model:
                source += f"（{matched_model}）"
        else:
            row.cost_cny = 0.0
            row.priced = False
            source = "未定价"

        machine_usage = route_machine[key].setdefault(row.machine, Usage())
        machine_usage.add(row.usage)
        record = models.get(key)
        if record is None:
            record = ModelUsage(provider=row.provider, model=model)
            models[key] = record
        record.usage.add(row.usage)
        record.cost_cny += row.cost_cny
        record.priced = record.priced and row.priced
        if record.price_source and record.price_source != source:
            record.price_source = record.price_source + " / " + source
        else:
            record.price_source = source

    for key, record in models.items():
        record.by_machine = route_machine[key]
        if catalog and record.cost_cny == 0:
            listed, listed_by = find_metered_price(catalog, record.model)
            if listed is not None:
                record.list_price_usd = price_models_dev(record.usage, listed, 1.0)
                record.list_price_source = listed_by
    return sorted(models.values(), key=lambda item: item.usage.total, reverse=True)


# --------------------------------------------------------------------------
# aggregation


def build_sources(specs: List[Tuple[str, Path]]) -> Tuple[List[Row], List[Machine]]:
    rows: List[Row] = []
    machines: List[Machine] = []
    for label, root in specs:
        if not root.exists():
            print(f"[warn] source {label}: {root} does not exist", file=sys.stderr)
            continue
        machine_rows, log_count, sessions = scan_source(label, root)
        if log_count == 0:
            print(f"[warn] source {label}: no DSH session logs under {root}", file=sys.stderr)
        if not machine_rows:
            print(f"[warn] source {label}: no provider usage found", file=sys.stderr)
        machine = Machine(
            label=label,
            name=MACHINE_NAMES.get(label, label),
            sessions=sessions,
            session_logs=log_count,
        )
        for row in machine_rows:
            machine.usage.add(row.usage)
            machine.by_month.setdefault(row.moment.strftime("%Y-%m"), Usage()).add(row.usage)
        rows.extend(machine_rows)
        machines.append(machine)
    return rows, machines


def attach_costs(rows: List[Row], machines: List[Machine]) -> None:
    machine_by_label = {machine.label: machine for machine in machines}
    for row in rows:
        machine_by_label[row.machine].cost_cny += row.cost_cny


def day_series(rows: List[Row], machines: List[Machine]) -> List[dict]:
    by_day: Dict[str, Dict[str, Usage]] = defaultdict(dict)
    for row in rows:
        day = row.moment.strftime("%Y-%m-%d")
        by_day[day].setdefault(row.machine, Usage()).add(row.usage)
    labels = [machine.label for machine in machines]
    result = []
    for day in sorted(by_day):
        total = Usage()
        item = {"date": day}
        for label in labels:
            usage = by_day[day].get(label, Usage())
            item[label] = usage.total
            total.add(usage)
        item["total"] = total.total
        item["requests"] = total.requests
        result.append(item)
    return result


def totals_by(rows: List[Row], key_fn) -> Dict[str, Usage]:
    result: Dict[str, Usage] = {}
    for row in rows:
        result.setdefault(key_fn(row), Usage()).add(row.usage)
    return result


# --------------------------------------------------------------------------
# formatting


def fmt_tokens(value: int) -> str:
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return f"{value:,}"


def fmt_tokens_short(value: int) -> str:
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.0f}万"
    return str(value)


def fmt_heat_bound(value: int) -> str:
    if value >= 10_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.0f}万"
    return str(value)


def fmt_cny(value: float) -> str:
    return f"¥{value:,.0f}" if abs(value) >= 1000 else f"¥{value:,.2f}"


def fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


# --------------------------------------------------------------------------
# report


def render_markdown(data: dict) -> str:
    machines = data["machines"]
    usage = data["usage"]
    lines = [
        "# 本机 + lab DSH Token 用量合计与热力图",
        "",
        f"- 统计范围：{data['start'][:16].replace('T', ' ')} — {data['end'][:16].replace('T', ' ')}（Asia/Shanghai）",
        "- 范围：macmini 与 lab server 上两个 DSH 实例的会话日志；不含 Claude Code / Codex / OpenCode 等其它 CLI。",
        "- 数据源：`~/.dsh/sessions/*/*/session.v4.jsonl.zstd`（同一会话优先 v4，旧版 `session.jsonl.zstd` 兜底）；"
        "计入每条 `assistant/message` 的 provider `usage` 与 `compaction/summary` 摘要调用，按 assistant message id 去重。",
        f"- 会话：{data['sessions']} 个有 token 记录（扫描 {data['sessionLogs']} 个会话目录）",
        f"- 模型请求：{data['requests']:,}",
        f"- 合计 Token：**{usage['total']:,}**（{fmt_tokens(usage['total'])}）",
        f"- 缓存命中率：{data['cacheHitPct']:.2f}%",
        f"- API 费用估算：**{fmt_cny(data['costCny'])}**",
        "",
        "## 分机器",
        "",
        "| 机器 | Token | 占比 | 请求 | 会话 | 费用 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for machine in machines:
        lines.append(
            f"| {machine['name']} | {machine['usage']['total']:,} | "
            f"{pct(machine['usage']['total'], usage['total']):.1f}% | "
            f"{machine['usage']['requests']:,} | {machine['sessions']} | {fmt_cny(machine['costCny'])} |"
        )
    lines.append(
        f"| **合计** | **{usage['total']:,}** | 100% | **{data['requests']:,}** | **{data['sessions']}** | **{fmt_cny(data['costCny'])}** |"
    )
    lines += [
        "",
        "## Token 构成",
        "",
        "| 类别 | Token | 占比 |",
        "| --- | ---: | ---: |",
        f"| 缓存读取 | {usage['cacheRead']:,} | {pct(usage['cacheRead'], usage['total']):.2f}% |",
        f"| 未缓存输入 | {usage['input']:,} | {pct(usage['input'], usage['total']):.2f}% |",
        f"| 缓存写入 | {usage['cacheWrite']:,} | {pct(usage['cacheWrite'], usage['total']):.2f}% |",
        f"| 输出 | {usage['output']:,} | {pct(usage['output'], usage['total']):.2f}% |",
        f"| **合计** | **{usage['total']:,}** | 100% |",
        "",
        "## 按模型",
        "",
    ]
    machine_headers = "".join(f" {machine['name']} |" for machine in machines)
    machine_sep = "".join(" ---: |" for _ in machines)
    lines.append(f"| 模型 | Provider |{machine_headers} 合计 | 请求 | 费用 | 单价来源 |")
    lines.append(f"| --- | --- |{machine_sep} ---: | ---: | ---: | --- |")
    for model in data["models"]:
        split = "".join(
            f" {model['byMachine'].get(machine['label'], {}).get('total', 0):,} |"
            for machine in machines
        )
        cost = fmt_cny(model["costCny"]) if model["priced"] else "—"
        lines.append(
            f"| `{model['model']}` | `{model['provider']}` |{split} "
            f"{model['usage']['total']:,} | {model['usage']['requests']:,} | {cost} | "
            f"{model['priceSource'] or '未定价'} |"
        )
    total_split = "".join(f" {machine['usage']['total']:,} |" for machine in machines)
    lines.append(
        f"| **合计** | |{total_split} **{usage['total']:,}** | **{data['requests']:,}** | "
        f"**{fmt_cny(data['costCny'])}** | |"
    )
    lines += ["", "## 按月", ""]
    month_headers = "".join(f" {machine['name']} |" for machine in machines)
    month_sep = "".join(" ---: |" for _ in machines)
    lines.append(f"| 月份 |{month_headers} 合计 | 请求 |")
    lines.append(f"| --- |{month_sep} ---: | ---: |")
    for month in data["monthly"]:
        split = "".join(f" {month['machines'].get(machine['label'], 0):,} |" for machine in machines)
        lines.append(
            f"| {month['month']} |{split} {month['total']:,} | {month['requests']:,} |"
        )
    lines += ["", "## 单日 Top 10", "", "| 日期 | Token | 请求 |", "| --- | ---: | ---: |"]
    for day in data["topDays"]:
        lines.append(f"| {day['date']} | {day['total']:,} | {day['requests']:,} |")
    lines += ["", "## 计价口径", ""]
    lines += [
        "- DeepSeek 路由按 DeepSeek 官方 CNY 价目（2026-11 核对）：每条请求按自身时间戳判断高峰/空闲，"
        "空闲价为高峰价一半；缓存写入并入缓存未命中输入。`deepseek-flash` 高峰价："
        "缓存命中 ¥0.04/M、未命中 ¥2/M、输出 ¥8/M。",
        f"- 其他模型按 models.dev（{data['pricing']['modelsDevFetchedAt'] or '未获取'}）USD 单价 × "
        f"{data['pricing']['usdCny']} 换算为人民币；provider 需与 DSH 路由对应，匹配不到就不猜价格。",
    ]
    for model in data["models"]:
        if model["listPriceUsd"] is not None:
            lines.append(
                f"- `{model['model']}` 当前路由在 models.dev 上是订阅制（$0/token）；"
                f"若按该模型在其他 provider 的列表价（{model['listPriceSource']}）折算，"
                f"约 {fmt_usd(model['listPriceUsd'])}。"
            )
    if data["listPriceCny"]:
        lines.append(
            f"- 若把订阅制的模型按上述列表价计入，合计约 "
            f"{fmt_cny(data['costCny'] + data['listPriceCny'])}（仅作参考）。"
        )
    lines += [
        "- 不含 Search / Fetch / OCR / 语音等按次计费项，也不含会话标题生成、网页搜索等没有本地 provider usage "
        "事件的调用；费用是公开单价估算，不是账单。",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# heatmap rendering


def align_weeks(start: date, end: date) -> List[List[date]]:
    first = start - timedelta(days=start.weekday())
    last = end + timedelta(days=6 - end.weekday())
    weeks = []
    cursor = first
    while cursor <= last:
        weeks.append([cursor + timedelta(days=offset) for offset in range(7)])
        cursor += timedelta(days=7)
    return weeks


def quantile_bins(values: List[int], buckets: int = 5) -> List[int]:
    positive = sorted(value for value in values if value > 0)
    if not positive:
        return []
    bounds = []
    for index in range(1, buckets):
        value = positive[min(len(positive) - 1, int(len(positive) * index / buckets))]
        if not bounds or value > bounds[-1]:
            bounds.append(value)
    return bounds


def scale_color(value: int, bounds: List[int], colors: Tuple[str, ...]) -> str:
    if value <= 0:
        return "#f0f1f5"
    index = bisect.bisect_right(bounds, value)
    return colors[min(index, len(colors) - 1)]


def heatmap_html(
    weeks: List[List[date]],
    values: Dict[str, int],
    start: date,
    end: date,
    bounds: List[int],
    colors: Tuple[str, ...],
    cell: int,
    gap: int,
    label_size: int = 13,
) -> str:
    month_labels = []
    labelled_months = set()
    for index, week in enumerate(weeks):
        for day in week:
            if day < start or day > end:
                continue
            if day.day == 1 or day == start:
                if day.month not in labelled_months:
                    month_labels.append(
                        f'<span style="left:{index * (cell + gap)}px">{day.month}月</span>'
                    )
                    labelled_months.add(day.month)
                break
    weekday_names = ("一", "二", "三", "四", "五", "六", "日")
    weekday_html = "".join(
        f'<div style="height:{cell}px;line-height:{cell}px">{name}</div>'
        for name in weekday_names
    )
    columns = []
    for week in weeks:
        cells = []
        for day in week:
            if day < start or day > end:
                cells.append(f'<div style="width:{cell}px;height:{cell}px"></div>')
                continue
            value = values.get(day.isoformat(), 0)
            color = scale_color(value, bounds, colors)
            cells.append(
                f'<div class="cell" style="width:{cell}px;height:{cell}px;background:{color}" '
                f'title="{day.isoformat()} · {value:,} token"></div>'
            )
        columns.append(f'<div class="week">{"".join(cells)}</div>')
    grid_width = len(weeks) * (cell + gap) - gap
    return f"""
      <div class="cal" style="--cell:{cell}px;--gap:{gap}px">
        <div class="months" style="width:{grid_width}px;height:20px">{''.join(month_labels)}</div>
        <div class="cal-body">
          <div class="weekdays" style="width:22px">{weekday_html}</div>
          <div class="grid" style="gap:{gap}px">{''.join(columns)}</div>
        </div>
      </div>"""


def legend_html(bounds: List[int], colors: Tuple[str, ...]) -> str:
    labels = []
    previous = 0
    for index, bound in enumerate(bounds + [None]):
        if bound is None:
            text = f"{fmt_heat_bound(previous)}+"
        else:
            text = f"{fmt_heat_bound(previous)}–{fmt_heat_bound(bound)}"
        labels.append(text)
        previous = bound if bound is not None else previous
    items = []
    for index, color in enumerate(colors):
        label = labels[index] if index < len(labels) else ""
        items.append(
            f'<div class="legend-item"><i style="background:{color}"></i><span>{label}</span></div>'
        )
    return "".join(items)


def render_html(data: dict) -> str:
    start = date.fromisoformat(data["start"][:10])
    end = date.fromisoformat(data["end"][:10])
    weeks = align_weeks(start, end)
    day_values = {item["date"]: item["total"] for item in data["daily"]}
    local_values = {item["date"]: item.get("local", 0) for item in data["daily"]}
    lab_values = {item["date"]: item.get("lab", 0) for item in data["daily"]}
    all_values = list(day_values.values())
    combined_bounds = quantile_bins(all_values)
    local_bounds = quantile_bins(list(local_values.values()))
    lab_bounds = quantile_bins(list(lab_values.values()))
    combined_colors = ("#d9daf0", "#b9baeb", "#8b8ddc", "#5c5dca", "#302d9b")
    local_colors = ("#dedef5", "#c2c3ee", "#999be0", "#6d6ed0", "#4643b5")
    lab_colors = ("#d5f0ed", "#afe0da", "#7accc4", "#45b1a7", "#168c84")

    total = data["usage"]["total"]
    peak_day = max(data["daily"], key=lambda item: item["total"]) if data["daily"] else None
    active_days = sum(1 for value in all_values if value > 0)
    span_days = (end - start).days + 1
    avg_per_active = total // active_days if active_days else 0
    machines = {machine["label"]: machine for machine in data["machines"]}
    local = machines.get("local", {"usage": {"total": 0, "requests": 0}, "costCny": 0.0})
    lab = machines.get("lab", {"usage": {"total": 0, "requests": 0}, "costCny": 0.0})
    local_share = pct(local["usage"]["total"], total)
    lab_share = pct(lab["usage"]["total"], total)

    top_days = data["topDays"][:3]
    top_days_html = "".join(
        f'<div class="top-row"><span>{item["date"][5:]}</span><b>{fmt_tokens_short(item["total"])}</b></div>'
        for item in top_days
    )

    combined = heatmap_html(
        weeks, day_values, start, end, combined_bounds, combined_colors, 50, 6, 13
    )
    local_map = heatmap_html(
        weeks, local_values, start, end, local_bounds, local_colors, 22, 4, 10
    )
    lab_map = heatmap_html(
        weeks, lab_values, start, end, lab_bounds, lab_colors, 22, 4, 10
    )

    def kpi(value: str, label: str, sub: str = "", accent: str = "#17171d") -> str:
        sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
        return (
            '<div class="kpi-card">'
            f'<div class="kpi-value" style="color:{accent}">{value}</div>'
            f'<div class="kpi-label">{label}</div>{sub_html}</div>'
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    width: {CARD_WIDTH}px; background: #f4f5f8;
    font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased; color: #17171d;
  }}
  .page {{ padding: 46px 56px 34px; display: flex; flex-direction: column; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .brand {{ font-size: 17px; letter-spacing: 4px; font-weight: 800; color: #5b5bd6; }}
  .date {{ font-size: 18px; color: #8f93a3; }}
  h1 {{ margin: 22px 0 0; font-size: 56px; line-height: 1.05; font-weight: 800; letter-spacing: -1px; }}
  .subtitle {{ margin-top: 10px; font-size: 19px; color: #5f6270; }}
  .kpis {{ margin-top: 22px; display: flex; gap: 14px; }}
  .kpi-card {{ flex: 1; background: #fff; border: 1px solid #e8e9f0; border-radius: 18px; padding: 18px 20px 15px; }}
  .kpi-value {{ font-size: 33px; font-weight: 800; letter-spacing: -0.5px; line-height: 1.1; white-space: nowrap; }}
  .kpi-label {{ margin-top: 7px; font-size: 16px; color: #858896; }}
  .kpi-sub {{ margin-top: 2px; font-size: 14px; color: #a0a3b1; }}
  .card {{ background: #fff; border: 1px solid #e8e9f0; border-radius: 20px; }}
  .heat {{ margin-top: 18px; padding: 20px 24px 16px; }}
  .panel-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 21px; color: #2b2c33; font-weight: 700; }}
  .panel-head .note {{ color: #8f93a3; font-size: 16px; font-weight: 400; }}
  .heat-body {{ display: flex; gap: 20px; margin-top: 12px; }}
  .cal {{ display: flex; flex-direction: column; }}
  .months {{ position: relative; margin-left: 30px; font-size: 13px; color: #8f93a3; }}
  .months span {{ position: absolute; top: 0; }}
  .cal-body {{ display: flex; margin-top: 2px; }}
  .weekdays {{ font-size: 12px; color: #a0a3b1; text-align: right; padding-right: 8px; }}
  .grid {{ display: flex; }}
  .week {{ display: flex; flex-direction: column; }}
  .cell {{ border-radius: 7px; }}
  .side {{ flex: 1; padding-left: 6px; display: flex; flex-direction: column; }}
  .side-title {{ font-size: 14px; color: #8f93a3; margin-bottom: 6px; }}
  .legend {{ display: flex; flex-direction: column; gap: 5px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; color: #6f7280; }}
  .legend-item i {{ width: 15px; height: 15px; border-radius: 4px; display: inline-block; }}
  .top-days {{ margin-top: 16px; border-top: 1px solid #eceef4; padding-top: 10px; }}
  .top-row {{ display: flex; justify-content: space-between; font-size: 14px; color: #5f6270; padding: 3px 0; }}
  .top-row b {{ color: #17171d; }}
  .mini {{ margin-top: 16px; display: flex; gap: 16px; }}
  .mini-card {{ flex: 1; padding: 18px 20px 16px; }}
  .mini-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 18px; font-weight: 700; }}
  .mini-head .note {{ font-size: 14px; color: #8f93a3; font-weight: 400; }}
  .mini-map {{ margin-top: 8px; display: flex; justify-content: center; }}
  .footer {{ margin-top: 18px; padding-top: 12px; display: flex; justify-content: space-between; font-size: 13px; color: #a0a3b1; border-top: 1px solid #e8e9f0; }}
</style></head><body><div class="page">
  <div class="top"><div class="brand">DSH CROSS-MACHINE TOKEN REPORT</div><div class="date">{data['end'][:16].replace('T', ' ')}</div></div>
  <h1>本机 + lab Token 热力图</h1>
  <div class="subtitle">macmini + lab · {data['start'][:10]} — {data['end'][:10]} · {data['sessions']} 个会话 · {data['requests']:,} 次模型请求</div>
  <div class="kpis">
    {kpi(fmt_tokens(total), "合计 Token", f"{span_days} 天 · {active_days} 个活跃日")}
    {kpi(fmt_cny(data['costCny']), "合计费用", "公开单价估算", "#c2410c")}
    {kpi(fmt_tokens(local['usage']['total']), "本机 macmini", f"{local_share:.1f}% · {fmt_cny(local['costCny'])}")}
    {kpi(fmt_tokens(lab['usage']['total']), "lab server", f"{lab_share:.1f}% · {fmt_cny(lab['costCny'])}", "#0f766e")}
    {kpi(f"{active_days} 天", "活跃天数", f"日均 {fmt_tokens_short(avg_per_active)}")}
  </div>
  <div class="card heat">
    <div class="panel-head"><span>每日 Token · 本机 + lab</span><span class="note">单日峰值 {peak_day['date'][5:] if peak_day else '—'} · {fmt_tokens(peak_day['total']) if peak_day else '—'}</span></div>
    <div class="heat-body">
      {combined}
      <div class="side">
        <div class="side-title">合计图例（按活跃日分位）</div>
        <div class="legend">{legend_html(combined_bounds, combined_colors)}</div>
        <div class="top-days"><div class="side-title">单日峰值</div>{top_days_html}</div>
      </div>
    </div>
  </div>
  <div class="mini">
    <div class="card mini-card">
      <div class="mini-head"><span>本机 macmini</span><span class="note">{local['usage']['total']:,} · {local['usage']['requests']:,} 请求</span></div>
      <div class="mini-map">{local_map}</div>
    </div>
    <div class="card mini-card">
      <div class="mini-head"><span>lab server</span><span class="note">{lab['usage']['total']:,} · {lab['usage']['requests']:,} 请求</span></div>
      <div class="mini-map">{lab_map}</div>
    </div>
  </div>
  <div class="footer">
    <span>数据：两台机器 ~/.dsh/sessions · DeepSeek 官方 CNY 价目 · 其他 models.dev × {data['pricing']['usdCny']} · 不含其它 CLI</span>
    <span>DSH 自动生成 · 估算非账单</span>
  </div>
</div></body></html>"""


def find_chromium() -> Optional[Path]:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    candidates += [
        str(Path(pattern).expanduser())
        for pattern in (
            HOME / "Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell",
            HOME / "Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        )
    ]
    for pattern in candidates:
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[-1])
    for name in ("chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def screenshot(html_path: Path, png_path: Path, width: int, height: int) -> None:
    chromium = find_chromium()
    if not chromium:
        raise RuntimeError("no chromium/chrome binary found for headless screenshot")
    proc = subprocess.run(
        [
            str(chromium), "--headless", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
            "--force-device-scale-factor=1", f"--window-size={width},{height}",
            f"--screenshot={png_path}", html_path.as_uri(),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"chromium screenshot failed: {proc.stderr[-400:]}")
    if png_path.stat().st_size < 5000:
        raise RuntimeError("chromium screenshot is suspiciously small")


def trim_bottom(png_path: Path, background=(244, 245, 248)) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    image = Image.open(png_path).convert("RGB")
    width, height = image.size
    pixels = image.load()
    last = height - 1
    while last > 0:
        row = [pixels[x, last] for x in range(0, width, 8)]
        if any(abs(pixel[0] - background[0]) > 3 or abs(pixel[1] - background[1]) > 3
               or abs(pixel[2] - background[2]) > 3 for pixel in row):
            break
        last -= 1
    if last < height - 1:
        image.crop((0, 0, width, last + 2)).save(png_path)


# --------------------------------------------------------------------------
# CLI


def parse_source(value: str) -> Tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--source must be LABEL=PATH")
    label, path = value.split("=", 1)
    if not label or not path:
        raise argparse.ArgumentTypeError("--source must be LABEL=PATH")
    return label, Path(path).expanduser()


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined DSH token usage and calendar heatmap")
    parser.add_argument(
        "--source", action="append", type=parse_source, required=True,
        help="machine label and DSH sessions directory, e.g. local=~/.dsh/sessions",
    )
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent), help="artifact directory")
    parser.add_argument("--usd-cny", type=float, default=7.1, help="USD -> CNY conversion rate")
    parser.add_argument("--refresh-models", action="store_true", help="ignore the cached models.dev catalog")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0)
    parser.add_argument("--no-card", action="store_true", help="skip the PNG heatmap")
    args = parser.parse_args()

    rows, machines = build_sources(args.source)
    if not rows:
        print("[error] no provider usage found", file=sys.stderr)
        return 1
    catalog, models_dev_at = load_models_dev(args.refresh_models, args.cache_max_age_hours)
    models = price_rows(rows, catalog, args.usd_cny)
    attach_costs(rows, machines)

    total = Usage()
    for row in rows:
        total.add(row.usage)
    start = min(row.moment for row in rows)
    end = max(row.moment for row in rows)
    daily = day_series(rows, machines)
    top_days = sorted(daily, key=lambda item: item["total"], reverse=True)[:10]
    monthly = totals_by(rows, lambda row: row.moment.strftime("%Y-%m"))
    cost_cny = sum(row.cost_cny for row in rows)
    cache_denominator = total.cache_read + total.input
    list_price_cny = 0.0
    for model in models:
        if model.list_price_usd is not None:
            list_price_cny += model.list_price_usd * args.usd_cny

    data = {
        "generatedAt": datetime.now(TZ).isoformat(timespec="seconds"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sum(machine.sessions for machine in machines),
        "sessionLogs": sum(machine.session_logs for machine in machines),
        "requests": total.requests,
        "usage": {
            "input": total.input,
            "output": total.output,
            "cacheRead": total.cache_read,
            "cacheWrite": total.cache_write,
            "total": total.total,
            "requests": total.requests,
        },
        "cacheHitPct": 100.0 * total.cache_read / cache_denominator if cache_denominator else 0.0,
        "costCny": round(cost_cny, 2),
        "listPriceCny": round(list_price_cny, 2),
        "machines": [
            {
                "label": machine.label,
                "name": machine.name,
                "sessions": machine.sessions,
                "sessionLogs": machine.session_logs,
                "usage": machine.usage.as_dict(),
                "costCny": round(machine.cost_cny, 2),
                "byMonth": {
                    month: usage.as_dict() for month, usage in sorted(machine.by_month.items())
                },
            }
            for machine in machines
        ],
        "models": [
            {
                "provider": model.provider,
                "model": model.model,
                "usage": model.usage.as_dict(),
                "byMachine": {
                    label: usage.as_dict() for label, usage in sorted(model.by_machine.items())
                },
                "costCny": round(model.cost_cny, 2),
                "priced": model.priced,
                "priceSource": model.price_source,
                "listPriceUsd": None if model.list_price_usd is None else round(model.list_price_usd, 4),
                "listPriceSource": model.list_price_source,
            }
            for model in models
        ],
        "monthly": [
            {
                "month": month,
                "total": usage.total,
                "requests": usage.requests,
                "machines": {
                    machine.label: machine.by_month.get(month, Usage()).total
                    for machine in machines
                },
            }
            for month, usage in sorted(monthly.items())
        ],
        "daily": daily,
        "topDays": top_days,
        "pricing": {
            "modelsDevFetchedAt": models_dev_at,
            "usdCny": args.usd_cny,
            "deepseek": "DeepSeek 官方 CNY 价目，逐请求高峰/空闲",
        },
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(data)
    (out_dir / "token-heatmap-report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "token-heatmap-usage.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    card_path = None
    if not args.no_card:
        html_text = render_html(data)
        with tempfile.TemporaryDirectory(prefix="dsh-token-heatmap-") as workdir:
            html_path = Path(workdir) / "heatmap.html"
            html_path.write_text(html_text, encoding="utf-8")
            png_path = Path(workdir) / "heatmap.png"
            try:
                screenshot(html_path, png_path, CARD_WIDTH, 1500)
                trim_bottom(png_path)
                card_path = out_dir / "token-heatmap.png"
                shutil.copyfile(png_path, card_path)
            except Exception as exc:  # noqa: BLE001 - the report is still useful
                print(f"[warn] heatmap render failed: {exc}", file=sys.stderr)

    print(markdown)
    print(json.dumps(
        {
            "report": str(out_dir / "token-heatmap-report.md"),
            "json": str(out_dir / "token-heatmap-usage.json"),
            "heatmap": str(card_path) if card_path else None,
            "tokens": total.total,
            "costCny": round(cost_cny, 2),
        },
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
