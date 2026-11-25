#!/usr/bin/env python3
"""All-time DSH token usage, priced with DeepSeek's official rates and models.dev.

The report reads the provider-reported `usage` recorded in every local DSH
session log, aggregates the whole history, and converts token counts to an
estimated API cost:

  * DeepSeek routes use DeepSeek's official CNY list prices, applied per
    request at that request's peak/off-peak rate (same rule as the daily brief).
  * Every other route is looked up in https://models.dev/api.json, using the
    provider/model entry that matches the DSH route. models.dev prices are USD
    per million tokens and are converted at `--usd-cny` (default 7.1).

Nothing here calls an LLM. Network access is only needed for a fresh models.dev
catalog; a cached copy is reused for `--cache-max-age-hours`.

Usage:
  python3 scripts/token-usage/all_time.py --out-dir scripts/token-usage
  python3 scripts/token-usage/all_time.py --start 2026-10-01 --end 2026-11-01
  python3 scripts/token-usage/all_time.py --refresh-models --no-card
"""

from __future__ import annotations

import argparse
import glob
import html
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
TZ = ZoneInfo("Asia/Shanghai")
DSH_SESSIONS = HOME / ".dsh" / "sessions"
CACHE_FILE = HOME / ".cache" / "dsh-token-usage" / "models.dev.json"
ZSTD = shutil.which("zstd") or "/opt/homebrew/bin/zstd"

MODELS_DEV_URL = "https://models.dev/api.json"
CARD_WIDTH = 1241

# DeepSeek's official CNY list prices per million tokens (peak rates, checked
# against api-docs.deepseek.com on 2026-11-26). Off-peak is half price; cache
# writes are billed as cache-miss input.
DEEPSEEK_CNY_PEAK = {
    "deepseek-flash": {"cacheHit": 0.04, "cacheMiss": 2.0, "output": 8.0},
    "deepseek-v4-pro": {"cacheHit": 0.30, "cacheMiss": 9.0, "output": 27.0},
}
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
# Provider-specific renames in the other direction: the Go gateway now serves
# V4 Flash as V4.1 Flash; old session logs keep the old id.
PROVIDER_MODEL_ALIASES = {
    "opencode-go": {"deepseek-v4-flash": "deepseek-v4.1-flash"},
}
DEEPSEEK_PROVIDERS = ("deepseek-official", "deepseek-account")
PEAK_WINDOWS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
# Chinese statutory holidays in 2026; an unknown year falls back to weekends only.
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

# DSH provider id -> models.dev provider ids, most specific first. The China
# coding-plan endpoint is the Zhipu AI Coding Plan; models.dev lists it at $0
# because the plan is a subscription, not metered per token.
PROVIDER_CANDIDATES = {
    "zai-coding-cn": ("zhipuai-coding-plan", "zai-coding-plan", "zhipuai", "zai"),
    "zai": ("zai", "zai-coding-plan"),
    "deepseek-official": ("deepseek",),
    "deepseek-account": ("deepseek",),
    "opencode-go": ("opencode-go", "opencode"),
}


def is_deepseek_provider(provider: str) -> bool:
    return provider in DEEPSEEK_PROVIDERS or provider.lower().startswith("deepseek")


def canonical_model(provider: str, model: str) -> str:
    """Normalize the id only for the provider that actually serves it."""
    return MODEL_ALIASES.get(model, model) if is_deepseek_provider(provider) else model


def is_peak_time(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    if moment.strftime("%Y-%m-%d") in BUILTIN_HOLIDAYS.get(str(moment.year), ()):
        return False
    minutes = moment.hour * 60 + moment.minute
    return any(start <= minutes < end for start, end in PEAK_WINDOWS)


def deepseek_cost_cny(usage: "Usage", prices: dict, peak: bool) -> float:
    factor = (1.0 if peak else 0.5) / 1_000_000
    return (
        (usage.input + usage.cache_write) * prices["cacheMiss"]
        + usage.cache_read * prices["cacheHit"]
        + usage.output * prices["output"]
    ) * factor


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


@dataclass
class ModelUsage(Usage):
    provider: str = ""
    model: str = ""
    cost_cny: float = 0.0
    priced: bool = False
    price_source: str = ""
    # Set when the matched models.dev entry is a zero-cost subscription and a
    # metered list price exists elsewhere in the catalog.
    list_price_usd: float | None = None
    list_price_source: str = ""


@dataclass
class Report:
    start: datetime | None = None
    end: datetime | None = None
    sessions: int = 0
    session_logs: int = 0
    requests: int = 0
    usage: Usage = field(default_factory=Usage)
    models: list[ModelUsage] = field(default_factory=list)
    monthly: dict[str, Usage] = field(default_factory=dict)
    models_dev_at: str = ""
    usd_cny: float = 7.1
    unpriced: list[str] = field(default_factory=list)

    @property
    def cost_cny(self) -> float:
        return sum(model.cost_cny for model in self.models)

    @property
    def cache_hit_pct(self) -> float:
        denominator = self.usage.cache_read + self.usage.input
        return 100.0 * self.usage.cache_read / denominator if denominator else 0.0

    @property
    def list_price_cny(self) -> float:
        return sum(model.list_price_usd or 0.0 for model in self.models) * self.usd_cny


# --------------------------------------------------------------------------
# collection


@dataclass
class Row:
    """One billed model call: an assistant message or a compaction summary."""

    session: str
    moment: datetime
    provider: str
    model: str
    usage: Usage
    kind: str


def read_events(path: str):
    raw = subprocess.run([ZSTD, "-dc", path], capture_output=True, timeout=300)
    if raw.returncode != 0:
        return
    for line in raw.stdout.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def session_logs() -> dict[str, Path]:
    """One log per session directory, preferring the v4 migration.

    Fourteen pre-v4 sessions only have ``session.jsonl.zstd``; one migrated
    session has both files, and counting both would duplicate its usage.
    """
    chosen: dict[str, tuple[int, Path]] = {}
    for priority, pattern in enumerate(("session.v4.jsonl.zstd", "session.jsonl.zstd")):
        for path in glob.glob(str(DSH_SESSIONS / "*" / "*" / pattern)):
            directory = str(Path(path).parent)
            if directory not in chosen or priority < chosen[directory][0]:
                chosen[directory] = (priority, Path(path))
    return {Path(directory).name: path for directory, (_, path) in chosen.items()}


def _usage_from(raw: dict) -> Usage:
    return Usage(
        input=int(raw.get("inputTokens") or 0),
        output=int(raw.get("outputTokens") or 0),
        cache_read=int(raw.get("cacheReadTokens") or 0),
        cache_write=int(raw.get("cacheWriteTokens") or 0),
        requests=1,
    )


def scan_rows(start: datetime | None, end: datetime | None) -> tuple[list[Row], int]:
    """Every billed call in the range, deduplicated by assistant message id."""
    start_ms = int(start.timestamp() * 1000) if start else None
    end_ms = int(end.timestamp() * 1000) if end else None
    rows: list[Row] = []
    seen_messages: set[str] = set()
    logs = session_logs()
    for session_id, path in logs.items():
        for event in read_events(str(path)):
            event_type = event.get("type")
            timestamp = int(event.get("time") or 0)
            if not timestamp:
                continue
            if start_ms is not None and timestamp < start_ms:
                continue
            if end_ms is not None and timestamp >= end_ms:
                continue
            data = event.get("data") or {}
            if event_type == "assistant/message":
                raw_usage = data.get("usage")
                source = (data.get("message") or {}).get("source") or {}
                if not raw_usage or source.get("kind") != "model":
                    continue
                message_id = (data.get("message") or {}).get("id") or ""
                if message_id:
                    if message_id in seen_messages:
                        continue
                    seen_messages.add(message_id)
                provider = source.get("provider") or "unknown"
                model = source.get("model") or "unknown"
                kind = "message"
            elif event_type == "compaction/summary":
                # Compaction summary calls are billed but do not emit an
                # assistant/message event; the summary event carries its usage.
                raw_usage = data.get("usage")
                if not raw_usage:
                    continue
                provider = data.get("provider") or "unknown"
                model = data.get("model") or "unknown"
                kind = "compaction"
            else:
                continue
            row = _usage_from(raw_usage)
            if row.total == 0:
                continue
            rows.append(
                Row(
                    session=session_id,
                    moment=datetime.fromtimestamp(timestamp / 1000, TZ),
                    provider=provider,
                    model=model,
                    usage=row,
                    kind=kind,
                )
            )
    return rows, len(logs)


def aggregate_rows(rows: list[Row]) -> tuple[Usage, dict[tuple[str, str], Usage], dict[str, Usage], int]:
    total = Usage()
    routes: dict[tuple[str, str], Usage] = {}
    monthly: dict[str, Usage] = {}
    sessions: set[str] = set()
    for row in rows:
        total.add(row.usage)
        sessions.add(row.session)
        key = (row.provider, canonical_model(row.provider, row.model))
        routes.setdefault(key, Usage()).add(row.usage)
        month = row.moment.strftime("%Y-%m")
        monthly.setdefault(month, Usage()).add(row.usage)
    return total, routes, monthly, len(sessions)


# --------------------------------------------------------------------------
# pricing


def load_models_dev(refresh: bool, cache_max_age_hours: float) -> tuple[dict, str]:
    """Return (catalog, fetched-at ISO) from cache or network; {} when unavailable."""
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
            headers={"User-Agent": "Mozilla/5.0 (compatible; dsh-token-usage/1.0)"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"fetchedAt": fetched_at, "providers": data}, ensure_ascii=False))
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


def models_dev_entry(catalog: dict, provider: str, model: str) -> tuple[dict | None, str, str]:
    """Return (entry, provider id, model id) from models.dev for one DSH route."""
    aliases = PROVIDER_MODEL_ALIASES.get(provider, {})
    model_candidates = [aliases[model], model] if model in aliases else [model]
    for candidate_provider in PROVIDER_CANDIDATES.get(provider, (provider,)):
        models = (catalog.get(candidate_provider) or {}).get("models") or {}
        for candidate_model in model_candidates:
            if candidate_model and candidate_model in models:
                return models[candidate_model], candidate_provider, candidate_model
    # A route we do not know: accept the model only when the catalog has it in
    # exactly one provider, so a price is never picked from an unrelated reseller.
    matches = [
        (pid, (entry.get("models") or {})[model])
        for pid, entry in catalog.items()
        if model in (entry.get("models") or {})
    ]
    if len(matches) == 1:
        return matches[0][1], matches[0][0], model
    return None, "", ""


LIST_PRICE_PROVIDERS = ("zai", "zhipuai", "tencent-token-plan", "openrouter", "deepseek")


def find_metered_price(catalog: dict, model: str) -> tuple[dict | None, str]:
    """First non-zero cost entry for the model, used as a list-price reference.

    Prefer the model vendor's own metered route before a reseller's, so the
    reference price is the recognizable list price rather than an odd gateway.
    """
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
    # Missing cache_write means the provider bills it as normal input.
    cache_write_price = input_price if cache_write_price is None else float(cache_write_price)
    usd = (
        usage.input * input_price
        + usage.output * output_price
        + usage.cache_read * cache_read_price
        + usage.cache_write * cache_write_price
    ) / 1_000_000
    return usd * usd_cny


def deepseek_costs(rows: list[Row]) -> dict[tuple[str, str], float]:
    """Per-request DeepSeek cost, because peak/off-peak is decided per request."""
    result: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        provider = row.provider
        model = canonical_model(provider, row.model)
        if not is_deepseek_provider(provider) or model not in DEEPSEEK_CNY_PEAK:
            continue
        result[(provider, model)] += deepseek_cost_cny(
            row.usage, DEEPSEEK_CNY_PEAK[model], is_peak_time(row.moment)
        )
    return result


def price_all(
    routes: dict[tuple[str, str], Usage], rows: list[Row], catalog: dict, usd_cny: float
) -> list[ModelUsage]:
    deepseek = deepseek_costs(rows)
    models: list[ModelUsage] = []
    for (provider, model), usage in routes.items():
        record = ModelUsage(
            provider=provider, model=model, input=usage.input, output=usage.output,
            cache_read=usage.cache_read, cache_write=usage.cache_write, requests=usage.requests,
        )
        if is_deepseek_provider(provider) and model in DEEPSEEK_CNY_PEAK:
            record.cost_cny = deepseek.get((provider, model), 0.0)
            record.priced = True
            record.price_source = "DeepSeek 官方价目"
        elif catalog:
            item, matched_provider, matched_model = models_dev_entry(catalog, provider, model)
            if item is not None:
                record.cost_cny = price_models_dev(usage, item, usd_cny)
                record.priced = True
                record.price_source = f"models.dev · {matched_provider}"
                if matched_model != model:
                    record.price_source += f"（{matched_model}）"
                if record.cost_cny == 0:
                    listed, listed_by = find_metered_price(catalog, model)
                    if listed is not None:
                        record.list_price_usd = price_models_dev(usage, listed, 1.0)
                        record.list_price_source = listed_by
        models.append(record)
    models.sort(key=lambda item: item.total, reverse=True)
    return models


# --------------------------------------------------------------------------
# formatting


def fmt_tokens(value: int) -> str:
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return f"{value:,}"


def fmt_cny(value: float) -> str:
    return f"¥{value:,.0f}" if abs(value) >= 1000 else f"¥{value:,.2f}"


def fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


# --------------------------------------------------------------------------
# report


def build_report(args: argparse.Namespace) -> Report:
    start = parse_local(args.start) if args.start else None
    end = parse_local(args.end) if args.end else None
    rows, session_logs = scan_rows(start, end)
    total, routes, monthly, sessions = aggregate_rows(rows)
    catalog, models_dev_at = load_models_dev(args.refresh_models, args.cache_max_age_hours)
    models = price_all(routes, rows, catalog, args.usd_cny)
    report = Report(
        start=min((row.moment for row in rows), default=None),
        end=max((row.moment for row in rows), default=None),
        sessions=sessions, session_logs=session_logs,
        requests=total.requests, usage=total, models=models, monthly=monthly,
        models_dev_at=models_dev_at, usd_cny=args.usd_cny,
        unpriced=[f"{model.provider}/{model.model}" for model in models if not model.priced],
    )
    return report


def render_markdown(report: Report) -> str:
    total = report.usage
    lines = [
        "# 本机 DSH 历史全部 Token 用量",
        "",
        f"- 统计范围：{report.start:%Y-%m-%d %H:%M} — {report.end:%Y-%m-%d %H:%M}（Asia/Shanghai）",
        "- 范围：仅本机 macmini 的 DSH 会话；不含其它机器，也不含 Claude Code / Codex / OpenCode 等 CLI。",
        "- 数据源：`~/.dsh/sessions/*/*/session.v4.jsonl.zstd` 与旧版 `session.jsonl.zstd`（同一会话优先 v4）；"
        "计入每条 `assistant/message` 的 provider `usage` 和 `compaction/summary` 摘要调用。",
        f"- 会话：{report.sessions} 个有 token 记录（扫描 {report.session_logs} 个会话目录）",
        f"- 模型请求：{report.requests:,}",
        f"- 合计 Token：**{total.total:,}**（{fmt_tokens(total.total)}）",
        f"- 缓存命中率：{report.cache_hit_pct:.2f}%",
        f"- API 费用估算：**{fmt_cny(report.cost_cny)}**",
        "",
        "## Token 构成",
        "",
        "| 类别 | Token | 占比 |",
        "| --- | ---: | ---: |",
        f"| 缓存读取 | {total.cache_read:,} | {pct(total.cache_read, total.total):.2f}% |",
        f"| 未缓存输入 | {total.input:,} | {pct(total.input, total.total):.2f}% |",
        f"| 缓存写入 | {total.cache_write:,} | {pct(total.cache_write, total.total):.2f}% |",
        f"| 输出 | {total.output:,} | {pct(total.output, total.total):.2f}% |",
        f"| **合计** | **{total.total:,}** | 100% |",
        "",
        "## 按模型",
        "",
        "| 模型 | Provider | Token | 请求 | 费用 | 单价来源 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for model in report.models:
        cost = fmt_cny(model.cost_cny) if model.priced else "—"
        lines.append(
            f"| `{model.model}` | `{model.provider}` | {model.total:,} | {model.requests:,} | {cost} | {model.price_source or '未定价'} |"
        )
    lines.append(f"| **合计** | | **{total.total:,}** | **{report.requests:,}** | **{fmt_cny(report.cost_cny)}** | |")
    lines += ["", "## 按月", "", "| 月份 | Token | 请求 |", "| --- | ---: | ---: |"]
    for month, usage in sorted(report.monthly.items()):
        lines.append(f"| {month} | {usage.total:,} | {usage.requests:,} |")
    lines += ["", "## 计价口径", ""]
    lines += [
        "- DeepSeek 路由按 DeepSeek 官方 CNY 价目（2026-11 核对）：每条请求按自身时间戳判断高峰/空闲，"
        "空闲价为高峰价一半；缓存写入并入缓存未命中输入。`deepseek-flash` 高峰价："
        "缓存命中 ¥0.04/M、未命中 ¥2/M、输出 ¥8/M。",
        f"- 其他模型按 models.dev（{report.models_dev_at or '未获取'}）USD 单价 × {report.usd_cny} 换算为人民币；"
        "provider 需与 DSH 路由对应，匹配不到就不猜价格。",
    ]
    if any(model.provider == "opencode-go" for model in report.models):
        lines.append(
            "- `opencode-go` 会话里的 `deepseek-v4-flash` 按 models.dev 上该网关当前的 "
            "`deepseek-v4.1-flash` 列表价计（$0.15 / $0.6 / $0.003 per M）。"
        )
    for model in report.models:
        if model.list_price_usd is not None:
            lines.append(
                f"- `{model.model}` 当前路由在 models.dev 上是订阅制（$0/token）；若按该模型在其他 provider 的"
                f"列表价（{model.list_price_source}）折算，约 {fmt_usd(model.list_price_usd)}。"
            )
    if report.list_price_cny:
        lines.append(
            f"- 若把订阅制的模型按上述列表价计入，合计约 {fmt_cny(report.cost_cny + report.list_price_cny)}（仅作参考）。"
        )
    lines += [
        "- 不含 Search / Fetch / OCR / 语音等按次计费项，也不含会话标题生成、网页搜索等没有本地 provider usage 事件的调用；"
        "费用是公开单价估算，不是账单。",
        "",
    ]
    return "\n".join(lines)


def pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


# --------------------------------------------------------------------------
# card


def find_chromium() -> Path | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    candidates += [
        str(Path(p).expanduser())
        for p in (
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


def screenshot(html_path: Path, png_path: Path, height: int) -> None:
    chromium = find_chromium()
    if not chromium:
        raise RuntimeError("no chromium/chrome binary found for headless screenshot")
    proc = subprocess.run(
        [
            str(chromium), "--headless", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
            "--force-device-scale-factor=1", f"--window-size={CARD_WIDTH},{height}",
            f"--screenshot={png_path}", html_path.as_uri(),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"chromium screenshot failed: {proc.stderr[-400:]}")
    if png_path.stat().st_size < 5000:
        raise RuntimeError("chromium screenshot is suspiciously small")


def render_html(report: Report) -> tuple[str, int]:
    total = report.usage
    card_height = 1200
    month_items = [(month, usage.total) for month, usage in sorted(report.monthly.items()) if usage.total]
    max_month = max((value for _, value in month_items), default=1)
    month_rows = "".join(
        f'<div class="month-row"><span>{month}</span>'
        f'<div class="month-bar"><i style="width:{max(value / max_month * 100, 2):.1f}%"></i></div>'
        f'<b>{fmt_tokens(value)}</b></div>'
        for month, value in month_items
    )
    model_rows = "".join(
        '<div class="model-row">'
        f'<span class="model-name">{html.escape(model.model)}</span>'
        f'<span>{fmt_tokens(model.total)}</span>'
        f'<span>{model.requests:,}</span>'
        f'<span class="model-cost">{"—" if not model.priced else fmt_cny(model.cost_cny)}</span>'
        "</div>"
        for model in report.models
    )
    unpriced_note = ""
    if report.unpriced:
        unpriced_note = " 未定价：" + "、".join(report.unpriced) + "。"
    notes = []
    for model in report.models:
        if model.list_price_usd is not None:
            notes.append(
                f"{model.model} 走订阅制（models.dev $0/token），若按列表价约 {fmt_usd(model.list_price_usd)}。"
            )
    note = (
        "DeepSeek 按官方价目（2026-11）：逐请求判断高峰/空闲，空闲半价；缓存写入并入未命中输入。"
        "其他模型按 models.dev 单价 × "
        f"{report.usd_cny} 换算（计入 compaction 摘要调用）。{''.join(notes)}{unpriced_note}估算，非账单。"
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    width: {CARD_WIDTH}px; height: {card_height}px; overflow: hidden; background: #f4f5f8;
    font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased; color: #17171d;
  }}
  .page {{ padding: 62px 72px 46px; height: {card_height}px; display: flex; flex-direction: column; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .brand {{ font-size: 22px; letter-spacing: 4px; font-weight: 800; color: #5b5bd6; }}
  .date {{ font-size: 23px; color: #8f93a3; }}
  h1 {{ margin: 36px 0 0; font-size: 72px; line-height: 1.05; font-weight: 800; letter-spacing: -1px; }}
  .subtitle {{ margin-top: 18px; font-size: 26px; color: #5f6270; }}
  .kpis {{ margin-top: 30px; display: flex; gap: 16px; }}
  .card {{ background: #fff; border: 1px solid #e8e9f0; border-radius: 20px; }}
  .token {{ margin-top: 22px; padding: 22px 26px; }}
  .token-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 21px; color: #5f6270; }}
  .token-head .note {{ color: #a0a3b1; font-size: 18px; }}
  .bar {{ margin-top: 16px; display: flex; gap: 2px; border-radius: 9px; overflow: hidden; background: #ececf3; height: 18px; }}
  .legend {{ margin-top: 15px; font-size: 19px; color: #6f7280; display: flex; flex-wrap: wrap; row-gap: 6px; }}
  .grid {{ margin-top: 22px; display: flex; gap: 18px; }}
  .models {{ flex: 1.15; padding: 18px 24px 12px; }}
  .monthly {{ flex: 1; padding: 18px 24px 12px; }}
  .panel-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 20px; color: #5f6270; }}
  .panel-head .note {{ color: #a0a3b1; font-size: 16px; }}
  .model-table {{ margin-top: 10px; }}
  .model-row {{ display: flex; align-items: baseline; padding: 5px 0; font-size: 18px; color: #4f5260; }}
  .model-row > span:nth-child(1) {{ flex: 1.5; }}
  .model-row > span:nth-child(2) {{ flex: 1; text-align: right; }}
  .model-row > span:nth-child(3) {{ flex: 0.55; text-align: right; }}
  .model-row > span:nth-child(4) {{ flex: 0.85; text-align: right; color: #17171d; }}
  .model-header {{ font-size: 14px; color: #a0a3b1; border-bottom: 1px solid #eceef4; padding-bottom: 5px; }}
  .model-total {{ border-top: 1px solid #eceef4; margin-top: 2px; padding-top: 7px; font-weight: 700; color: #17171d; }}
  .model-name {{ font-weight: 700; color: #2b2c33; }}
  .model-cost {{ font-weight: 700; }}
  .model-note {{ margin-top: 8px; font-size: 14px; color: #a0a3b1; line-height: 1.45; }}
  .month-row {{ display: flex; align-items: center; gap: 10px; margin-top: 16px; font-size: 18px; color: #4f5260; }}
  .month-row > span {{ flex: 0 0 82px; }}
  .month-row > b {{ flex: 0 0 92px; text-align: right; color: #17171d; }}
  .month-bar {{ flex: 1; height: 16px; background: #ececf3; border-radius: 8px; overflow: hidden; }}
  .month-bar i {{ display: block; height: 16px; background: #5b5bd6; border-radius: 8px; }}
  .footer {{ margin-top: auto; padding-top: 20px; display: flex; justify-content: space-between; font-size: 18px; color: #a0a3b1; border-top: 1px solid #e8e9f0; }}
</style></head><body><div class="page">
  <div class="top"><div class="brand">DSH MACMINI ALL-TIME TOKEN REPORT</div><div class="date">{report.end:%Y-%m-%d %H:%M}</div></div>
  <h1>本机历史 Token 用量</h1>
  <div class="subtitle">macmini · {report.start:%Y-%m-%d} — {report.end:%Y-%m-%d} · {report.sessions} 个会话 · {report.requests:,} 次模型请求</div>
  <div class="kpis">
    {kpi(fmt_tokens(total.total), "Token 总量")}
    {kpi(f"{report.requests:,}", "模型请求")}
    {kpi(f"{report.sessions}", "会话数")}
    {kpi(f"{report.cache_hit_pct:.1f}%", "缓存命中")}
    {kpi(fmt_cny(report.cost_cny), "API 费用", 34)}
  </div>
  <div class="card token">
    <div class="token-head"><span>Token 构成</span><span class="note">provider 计费口径</span></div>
    <div class="bar">{bar(total)}</div>
    <div class="legend">{legend(total)}</div>
  </div>
  <div class="grid">
    <div class="card models">
      <div class="panel-head"><span>模型 Token / 费用</span><span class="note">API 费用 ≈ {fmt_cny(report.cost_cny)}</span></div>
      <div class="model-table">
        <div class="model-row model-header"><span>模型</span><span>Token</span><span>请求</span><span>费用</span></div>
        {model_rows}
        <div class="model-row model-total"><span>合计</span><span>{fmt_tokens(total.total)}</span><span>{report.requests:,}</span><span>{fmt_cny(report.cost_cny)}</span></div>
      </div>
      <div class="model-note">{html.escape(note)}</div>
    </div>
    <div class="card monthly">
      <div class="panel-head"><span>按月 Token</span><span class="note">全部月份</span></div>
      {month_rows}
    </div>
  </div>
  <div class="footer">
    <span>数据：本机 DSH 会话日志（provider usage）· 统计截至 {report.end:%Y-%m-%d %H:%M}</span>
    <span>DSH 自动生成</span>
  </div>
</div></body></html>"""
    return html_text, card_height


def kpi(value: str, label: str, font: int = 42) -> str:
    return (
        '<div style="flex:1;background:#fff;border:1px solid #e8e9f0;border-radius:20px;'
        'padding:22px 22px 18px">'
        f'<div style="font-size:{font}px;font-weight:800;letter-spacing:-1px;color:#17171d;line-height:1.1;white-space:nowrap">{value}</div>'
        f'<div style="margin-top:9px;font-size:20px;color:#8a8d99">{label}</div>'
        "</div>"
    )


def bar(total: Usage) -> str:
    segments = (("缓存读取", total.cache_read, "#5b5bd6"), ("未缓存输入", total.input, "#f0a13a"), ("输出", total.output, "#c3c6d4"))
    denominator = total.total or 1
    return "".join(
        f'<div style="width:{max(value / denominator * 100, 1.2):.2f}%;background:{color};height:18px"></div>'
        for _, value, color in segments if value > 0
    )


def legend(total: Usage) -> str:
    segments = (("缓存读取", total.cache_read, "#5b5bd6"), ("未缓存输入", total.input, "#f0a13a"), ("输出", total.output, "#c3c6d4"))
    return "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
        f'<i style="width:11px;height:11px;border-radius:3px;background:{color};display:inline-block"></i>'
        f'{label} {fmt_tokens(value)}</span>'
        for label, value, color in segments
    )


# --------------------------------------------------------------------------
# CLI


def parse_local(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def main() -> int:
    parser = argparse.ArgumentParser(description="All-time DSH token usage with models.dev pricing")
    parser.add_argument("--start", help="ISO start timestamp (inclusive, Asia/Shanghai)")
    parser.add_argument("--end", help="ISO end timestamp (exclusive, Asia/Shanghai)")
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent), help="artifact directory")
    parser.add_argument("--usd-cny", type=float, default=7.1, help="USD -> CNY conversion rate")
    parser.add_argument("--refresh-models", action="store_true", help="ignore the cached models.dev catalog")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0)
    parser.add_argument("--no-card", action="store_true", help="skip the PNG card")
    args = parser.parse_args()

    report = build_report(args)
    if report.requests == 0:
        print("[error] no provider usage found for the requested range", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    (out_dir / "all-time-report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "all-time-usage.json").write_text(
        json.dumps(
            {
                "start": report.start.isoformat() if report.start else None,
                "end": report.end.isoformat() if report.end else None,
                "sessions": report.sessions,
                "sessionLogs": report.session_logs,
                "requests": report.requests,
                "usage": {
                    "input": report.usage.input, "output": report.usage.output,
                    "cacheRead": report.usage.cache_read, "cacheWrite": report.usage.cache_write,
                    "total": report.usage.total,
                },
                "costCny": round(report.cost_cny, 2),
                "usdCny": report.usd_cny,
                "modelsDevFetchedAt": report.models_dev_at,
                "models": [
                    {
                        "provider": model.provider, "model": model.model,
                        "input": model.input, "output": model.output,
                        "cacheRead": model.cache_read, "cacheWrite": model.cache_write,
                        "total": model.total, "requests": model.requests,
                        "costCny": round(model.cost_cny, 4), "priced": model.priced,
                        "priceSource": model.price_source,
                        "listPriceUsd": None if model.list_price_usd is None else round(model.list_price_usd, 4),
                    }
                    for model in report.models
                ],
            },
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    card_path = None
    if not args.no_card:
        html_text, card_height = render_html(report)
        with tempfile.TemporaryDirectory(prefix="dsh-token-usage-") as workdir:
            html_path = Path(workdir) / "card.html"
            html_path.write_text(html_text, encoding="utf-8")
            png_path = Path(workdir) / "card.png"
            try:
                screenshot(html_path, png_path, card_height)
                card_path = out_dir / "all-time-card.png"
                shutil.copyfile(png_path, card_path)
            except Exception as exc:  # noqa: BLE001 - the report is still useful
                print(f"[warn] card render failed: {exc}", file=sys.stderr)

    print(markdown)
    print(json.dumps({"report": str(out_dir / "all-time-report.md"), "card": str(card_path) if card_path else None, "costCny": round(report.cost_cny, 2)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
