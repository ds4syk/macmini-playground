#!/usr/bin/env python3
"""Generate and post the DSH weekly brief to a Feishu group.

The brief is rendered as a 1241x1754 PNG card and posted with the
dsh-superagent Feishu bot once a week (Sunday 20:00 by default).

Data sources, all local:
  * ~/.dsh/sessions/*/*/session.v4.jsonl.zstd — per-model-request provider
    usage recorded by DSH (input / output / cache-read / cache-write tokens),
    attributed to superagent tasks by the `worker-<ticketId>` session folder.
  * ~/.dsh/state/superagent/orchestrator.sqlite — superagent tickets,
    assignments and worker reports for the week's work.
  * `gh` (optional, short timeout) — state of pull requests referenced by
    the week's reports, used for the open-PR lines.

Nothing here talks to an LLM: the brief is a deterministic digest of the
recorded work, so a failed network call can only omit PR state, never the
card itself.

Usage:
  python3 weekly_brief.py --no-post
  python3 weekly_brief.py --start 2026-11-09 --end 2026-11-11T23:30 --preview
  python3 weekly_brief.py --demo --no-post
  python3 weekly_brief.py                     # what launchd runs on Sunday 20:00
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
TZ = ZoneInfo("Asia/Shanghai")
DSH_SESSIONS = HOME / ".dsh" / "sessions"
SUPERAGENT_DB = HOME / ".dsh" / "state" / "superagent" / "orchestrator.sqlite"
DEFAULT_REPO = "ds4syk/macmini-playground"
LARK_BOT = HOME / ".local" / "bin" / "lark-bot"
OUT_DIR = HOME / "datas" / "dsh-weekly-brief" / "out"
LOCK_FILE = HOME / "datas" / "dsh-weekly-brief" / "weekly-brief.lock"
CARD_WIDTH = 1241
CARD_HEIGHT = 1754
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ZSTD = shutil.which("zstd") or "/opt/homebrew/bin/zstd"

# Palette for the task-share donut: one hue per task, gray for the remainder.
PIE_COLORS = ["#5b5bd6", "#f0a13a", "#2bb3a3", "#e46a76", "#8b8fc7", "#c3c6d4"]

# DeepSeek official list prices in CNY per million tokens (peak rates,
# checked against api-docs.deepseek.com 2026-10). Off-peak is half price.
# Cache writes are billed as cache-miss input, as the official docs state.
CNY_PEAK_PRICES = {
    "deepseek-flash": {"cacheHit": 0.04, "cacheMiss": 2.0, "output": 8.0},
    "deepseek-v4-pro": {"cacheHit": 0.30, "cacheMiss": 9.0, "output": 27.0},
}
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
DEEPSEEK_PROVIDERS = ("deepseek-official", "deepseek-account")
PEAK_WINDOWS = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
# Chinese statutory holidays in 2026; an unknown year falls back to weekends only.
BUILTIN_HOLIDAYS = {
    "2026": (
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
    ),
}


def is_deepseek_provider(provider: str) -> bool:
    return provider in DEEPSEEK_PROVIDERS or provider.lower().startswith("deepseek")


def canonical_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def is_peak_time(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    if moment.strftime("%Y-%m-%d") in BUILTIN_HOLIDAYS.get(str(moment.year), ()):
        return False
    minutes = moment.hour * 60 + moment.minute
    return any(start <= minutes < end for start, end in PEAK_WINDOWS)


def request_cost_cny(usage: dict, prices: dict | None, peak: bool) -> float:
    """One request's CNY cost; None prices mean an unpriced model."""
    if not prices:
        return 0.0
    input_tokens = int(usage.get("inputTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    cache_read = int(usage.get("cacheReadTokens") or 0)
    cache_write = int(usage.get("cacheWriteTokens") or 0)
    factor = (1.0 if peak else 0.5) / 1_000_000
    return (
        (input_tokens + cache_write) * prices["cacheMiss"]
        + cache_read * prices["cacheHit"]
        + output_tokens * prices["output"]
    ) * factor


def fmt_cny(value: float) -> str:
    if value >= 1000:
        return f"¥{value:,.0f}"
    return f"¥{value:,.2f}"


# --------------------------------------------------------------------------
# small helpers


def iso_to_dt(value: str) -> datetime | None:
    """Parse a stored ISO timestamp ('...Z' or offset-aware) into aware UTC."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def parse_local(value: str) -> datetime:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' in Asia/Shanghai."""
    text = value.strip()
    if len(text) == 10:
        return datetime.combine(date_cls.fromisoformat(text), time(0, 0), tzinfo=TZ)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def strip_markdown(text: str) -> str:
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"^[#>\-\*\s]+", "", text)
    return " ".join(text.split())


def fmt_tokens(count: int) -> str:
    """Format Chinese-style token counts: 5267.7 万 / 1.63 亿 / 246."""
    if count >= 100_000_000:
        return f"{count / 100_000_000:.2f} 亿"
    if count >= 10_000:
        return f"{count / 10_000:.1f} 万"
    return f"{count:,}"


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.1f} 分钟"


def find_chromium() -> Path | None:
    """Locate a Chrome/Chromium binary able to take a headless screenshot."""
    candidates: list[Path] = []
    for pattern in (
        str(HOME / "Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"),
        str(HOME / "Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
    ):
        candidates.extend(Path(p) for p in glob.glob(pattern))
    for candidate in sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if candidate.exists():
            return candidate
    for name in ("chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# --------------------------------------------------------------------------
# data collection


@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    sessions: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cache_hit(self) -> float:
        denom = self.cache_read_tokens + self.input_tokens
        if denom <= 0:
            return 0.0
        return 100.0 * self.cache_read_tokens / denom


@dataclass
class TaskShare:
    label: str
    tokens: int
    pct: float
    color: str


@dataclass
class ModelUsage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    cost: float = 0.0
    priced: bool = True

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def collect_usage(
    start: datetime, end: datetime
) -> tuple[UsageStats, dict[str, int], dict[tuple[str, str], ModelUsage]]:
    """Aggregate provider usage in the window, per session folder and per model."""
    stats = UsageStats()
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    session_tokens: dict[str, int] = {}
    model_usages: dict[tuple[str, str], ModelUsage] = {}
    sessions: set[str] = set()
    for path in glob.glob(str(DSH_SESSIONS / "*" / "*" / "session.v4.jsonl.zstd")):
        raw = subprocess.run([ZSTD, "-dc", path], capture_output=True, timeout=300)
        if raw.returncode != 0:
            continue
        session_id = Path(path).parent.name
        for line in raw.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant/message":
                continue
            timestamp = int(event.get("time") or 0)
            if not (start_ms <= timestamp < end_ms):
                continue
            usage = (event.get("data") or {}).get("usage") or {}
            if not usage:
                continue
            stats.requests += 1
            sessions.add(session_id)
            input_tokens = int(usage.get("inputTokens") or 0)
            output_tokens = int(usage.get("outputTokens") or 0)
            cache_read = int(usage.get("cacheReadTokens") or 0)
            cache_write = int(usage.get("cacheWriteTokens") or 0)
            stats.input_tokens += input_tokens
            stats.output_tokens += output_tokens
            stats.cache_read_tokens += cache_read
            stats.cache_write_tokens += cache_write
            session_tokens[session_id] = (
                session_tokens.get(session_id, 0) + input_tokens + output_tokens + cache_read + cache_write
            )

            source = ((event.get("data") or {}).get("message") or {}).get("source") or {}
            provider = source.get("provider") or "unknown"
            model = canonical_model(source.get("model") or "未知模型")
            key = (provider, model)
            record = model_usages.get(key)
            if record is None:
                record = ModelUsage(provider=provider, model=model)
                model_usages[key] = record
            record.requests += 1
            record.input_tokens += input_tokens
            record.output_tokens += output_tokens
            record.cache_read_tokens += cache_read
            record.cache_write_tokens += cache_write
            prices = CNY_PEAK_PRICES.get(model) if is_deepseek_provider(provider) else None
            if prices:
                moment = datetime.fromtimestamp(timestamp / 1000, TZ)
                record.cost += request_cost_cny(usage, prices, is_peak_time(moment))
            else:
                record.priced = False
    stats.sessions = len(sessions)
    return stats, session_tokens, model_usages


def attribute_task_shares(
    session_tokens: dict[str, int], titles: dict[str, str]
) -> tuple[list[TaskShare], float, int]:
    """Split total tokens into top tasks plus everything else.

    Superagent worker sessions are named `worker-<ticketId>`; a session whose
    ticket id is known counts as that task. The orchestrator session and the
    user's own sessions count as "其他".
    """
    total = sum(session_tokens.values())
    per_task: dict[str, int] = {}
    for session_id, tokens in session_tokens.items():
        ticket_id = session_id[len("worker-") :] if session_id.startswith("worker-") else ""
        title = titles.get(ticket_id) if ticket_id else None
        if title:
            per_task[title] = per_task.get(title, 0) + tokens

    task_total = sum(per_task.values())
    task_pct = 100.0 * task_total / total if total else 0.0
    if not per_task or total == 0:
        return [TaskShare("非任务会话", total, 100.0 if total else 0.0, PIE_COLORS[-1])], 0.0, 0

    ranked = sorted(per_task.items(), key=lambda pair: pair[1], reverse=True)
    shares: list[TaskShare] = []
    for index, (title, tokens) in enumerate(ranked[:4]):
        shares.append(TaskShare(title, tokens, 100.0 * tokens / total, PIE_COLORS[index]))
    remaining_tasks = sum(tokens for _, tokens in ranked[4:])
    if remaining_tasks:
        shares.append(TaskShare("其他任务", remaining_tasks, 100.0 * remaining_tasks / total, "#8b8fc7"))
    non_task = total - task_total
    if non_task > 0:
        shares.append(TaskShare("非任务会话", non_task, 100.0 * non_task / total, PIE_COLORS[-1]))
    return shares, task_pct, task_total


@dataclass
class WorkItem:
    title: str
    desc: str
    status: str  # 完成 / 进行中 / 受阻 / 待确认
    activity: int
    ended_at: datetime | None
    attention: str | None = None
    prs: list[str] = field(default_factory=list)


@dataclass
class TicketSnapshot:
    total: int = 0
    done: int = 0
    items: list[WorkItem] = field(default_factory=list)
    blocked: list[WorkItem] = field(default_factory=list)
    running: list[WorkItem] = field(default_factory=list)
    attention: list[WorkItem] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)


PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")


def _clip_desc(text: str, limit: int = 42) -> str:
    """Prefer a whole first sentence, otherwise clip at the character limit."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.find("。")
    if 6 <= cut + 1 <= limit:
        return text[: cut + 1]
    return truncate(text, limit)


def _first_meaningful_line(summary: str) -> str:
    for raw in summary.splitlines():
        line = strip_markdown(raw).strip()
        if not line:
            continue
        if re.fullmatch(r"(完成|结果|结论|限制|文件|摘要|风险|说明|验证)[:：]?", line):
            continue
        if len(line) < 3:
            continue
        return line
    return ""


def load_tickets() -> list[dict]:
    """Read all ticket records; used for both work items and title lookup."""
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{SUPERAGENT_DB}?mode=ro", uri=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        print(f"[warn] cannot open superagent db: {exc}", file=sys.stderr)
        return []
    try:
        rows = conn.execute("select value from ticket").fetchall()
    finally:
        conn.close()
    tickets: list[dict] = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("id"):
            tickets.append(item)
    return tickets


def collect_tickets(tickets: list[dict], start: datetime, end: datetime) -> TicketSnapshot:
    snapshot = TicketSnapshot()
    for ticket in tickets:
        assignments = ticket.get("assignments") or []
        activity = 0
        last_event: datetime | None = None
        for assignment in assignments:
            for key in ("createdAt", "startedAt", "deliveredAt", "endedAt"):
                moment = iso_to_dt(assignment.get(key) or "")
                if moment and start <= moment < end:
                    activity += 1
                    last_event = max(last_event, moment) if last_event else moment
        for item in ticket.get("inputs") or []:
            moment = iso_to_dt(item.get("at") or "")
            if moment and start <= moment < end:
                activity += 1
                last_event = max(last_event, moment) if last_event else moment
        if activity == 0:
            continue

        latest = assignments[-1] if assignments else {}
        reports = [
            assignment["report"]
            for assignment in assignments
            if (assignment.get("report") or {}).get("summary")
        ]
        latest_report = reports[-1] if reports else {}
        in_window_reports = [
            report
            for report in reports
            if (iso_to_dt(report.get("at") or "") or datetime.min.replace(tzinfo=TZ)) >= start
            and (iso_to_dt(report.get("at") or "") or datetime.max.replace(tzinfo=TZ)) < end
        ]
        main_report = max(in_window_reports or reports, key=lambda item: len(item.get("summary") or ""), default={})
        summary = main_report.get("summary") or ""
        state = latest_report.get("state") or ""
        assignment_state = latest.get("state") or ""
        attention = (ticket.get("attention") or {}).get("question")

        if assignment_state in ("pending", "running"):
            status = "进行中"
        elif state == "ready" and assignment_state == "ended":
            status = "完成"
        elif state in ("blocked", "failed"):
            status = "受阻"
        elif attention:
            status = "待确认"
        else:
            status = "进行中"

        title = strip_markdown(ticket.get("title") or "未命名任务")
        desc = _first_meaningful_line(summary)
        prs = sorted(set(PR_RE.findall(summary)))

        item = WorkItem(
            title=title,
            desc=desc,
            status=status,
            activity=activity,
            ended_at=last_event,
            attention=attention,
            prs=prs,
        )
        snapshot.total += 1
        snapshot.items.append(item)
        if status == "完成":
            snapshot.done += 1
        if status == "受阻":
            snapshot.blocked.append(item)
        if status == "进行中":
            snapshot.running.append(item)
        if attention:
            snapshot.attention.append(item)
        for url in prs:
            if url not in snapshot.pr_urls:
                snapshot.pr_urls.append(url)

    snapshot.items.sort(
        key=lambda i: (i.activity, i.ended_at or datetime.min.replace(tzinfo=TZ)), reverse=True
    )
    snapshot.items = snapshot.items[:12]
    return snapshot


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    state: str  # OPEN / MERGED / CLOSED / UNKNOWN


def gh_json(args: list[str], timeout: int = 6) -> object | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        proc = subprocess.run([gh, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def collect_prs(snapshot: TicketSnapshot, repo: str) -> list[PullRequest]:
    """Resolve open PRs from the week's reports plus open PRs in the watched repo."""
    prs: dict[str, PullRequest] = {}
    for url in snapshot.pr_urls[:6]:
        data = gh_json(["pr", "view", url, "--json", "number,title,url,state"])
        if isinstance(data, dict) and data.get("number"):
            canonical = data.get("url") or url
            prs[canonical] = PullRequest(
                int(data["number"]), data.get("title") or "", canonical, data.get("state") or "UNKNOWN"
            )
        else:
            number = int(url.rsplit("/", 1)[-1])
            prs[url] = PullRequest(number, "", url, "UNKNOWN")

    repo_prs = gh_json(
        ["pr", "list", "-R", repo, "--state", "open", "--limit", "20", "--json", "number,title,url,state"]
    )
    if isinstance(repo_prs, list):
        for data in repo_prs:
            url = data.get("url") or ""
            number = int(data.get("number") or 0)
            if number and url and url not in prs:
                prs[url] = PullRequest(number, data.get("title") or "", url, "OPEN")

    open_prs = [pr for pr in prs.values() if pr.state in ("OPEN", "UNKNOWN")]
    # The card shows only the number, so keep at most one entry per number.
    seen_numbers: set[int] = set()
    unique: list[PullRequest] = []
    for pr in open_prs:
        if pr.number in seen_numbers:
            continue
        seen_numbers.add(pr.number)
        unique.append(pr)
    return unique


# --------------------------------------------------------------------------
# card rendering


def donut_gradient(shares: list[TaskShare]) -> str:
    stops: list[str] = []
    acc = 0.0
    for index, share in enumerate(shares):
        start_pct = acc
        end_pct = 100.0 if index == len(shares) - 1 else acc + share.pct
        stops.append(f"{share.color} {start_pct:.2f}% {end_pct:.2f}%")
        acc = end_pct
    if not stops:
        stops.append(f"{PIE_COLORS[-1]} 0% 100%")
    return "conic-gradient(" + ", ".join(stops) + ")"


def render_html(context: dict) -> str:
    stats: UsageStats = context["stats"]
    snapshot: TicketSnapshot = context["tickets"]
    shares: list[TaskShare] = context["shares"]
    task_pct: float = context["task_pct"]
    task_total: int = context["task_total"]
    start: datetime = context["start"]
    end: datetime = context["end"]
    demo: bool = context["demo"]
    preview: bool = context["preview"]
    subtitle: str = context["subtitle"]
    models: list[ModelUsage] = context["models"]
    cost_total: float = context["cost_total"]
    cost_complete: bool = context["cost_complete"]
    card_height: int = context.get("card_height", CARD_HEIGHT)

    total = stats.total or 1
    segments = [
        ("缓存读取", stats.cache_read_tokens, "#5b5bd6"),
        ("未缓存输入", stats.input_tokens, "#f0a13a"),
        ("输出", stats.output_tokens, "#c3c6d4"),
    ]
    bar_parts = "".join(
        f'<div style="width:{max(value / total * 100, 1.2):.2f}%;background:{color};height:18px"></div>'
        for _, value, color in segments
        if value > 0
    )
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
        f'<i style="width:11px;height:11px;border-radius:3px;background:{color};display:inline-block"></i>'
        f'{label} {fmt_tokens(value)}</span>'
        for label, value, color in segments
    )

    def kpi(value: str, label: str, font: int = 42) -> str:
        return (
            '<div style="flex:1;background:#fff;border:1px solid #e8e9f0;border-radius:20px;'
            'padding:22px 22px 18px">'
            f'<div style="font-size:{font}px;font-weight:800;letter-spacing:-1px;color:#17171d;line-height:1.1;white-space:nowrap">{value}</div>'
            f'<div style="margin-top:9px;font-size:20px;color:#8a8d99">{label}</div>'
            "</div>"
        )

    def highlight(index: int, item: WorkItem) -> str:
        title = truncate(item.title, 30)
        desc = _clip_desc(item.desc or "已完成并交付")
        return (
            '<div style="display:flex;gap:34px;padding:22px 0;border-top:1px solid #e8e9f0">'
            f'<div style="flex:0 0 56px;font-size:23px;color:#8f93a3;font-weight:600;padding-top:6px">{index:02d}</div>'
            "<div>"
            f'<div style="font-size:29px;font-weight:700;color:#17171d;line-height:1.35">{html.escape(title)}</div>'
            f'<div style="margin-top:8px;font-size:23px;color:#6f7280;line-height:1.45">{html.escape(desc)}</div>'
            "</div></div>"
        )

    highlights_html = "".join(highlight(i + 1, item) for i, item in enumerate(snapshot.items[:3]))
    if not highlights_html:
        highlights_html = '<div style="padding:24px 0;font-size:24px;color:#8a8d99">本周暂无 DSH 任务记录。</div>'

    pie_legend = "".join(
        '<div style="display:flex;align-items:center;gap:8px;margin:7px 0;font-size:18px;color:#4f5260">'
        f'<i style="width:9px;height:9px;border-radius:50%;background:{share.color};flex:0 0 9px"></i>'
        f'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{html.escape(truncate(share.label, 11))}</span>'
        f'<span style="font-weight:700;color:#17171d">{share.pct:.1f}%</span>'
        "</div>"
        for share in shares
    )
    pie_css = donut_gradient(shares)

    shown_models = models[:4]
    model_rows = "".join(
        '<div class="model-row">'
        f'<span class="model-name">{html.escape(truncate(item.model, 22))}</span>'
        f'<span>{fmt_tokens(item.total)}</span>'
        f'<span>{item.requests}</span>'
        f'<span class="model-cost">{"—" if not item.priced else fmt_cny(item.cost)}</span>'
        "</div>"
        for item in shown_models
    )
    if len(models) > len(shown_models):
        rest = models[len(shown_models):]
        model_rows += (
            '<div class="model-row">'
            f'<span class="model-name">其他 {len(rest)} 个模型</span>'
            f'<span>{fmt_tokens(sum(item.total for item in rest))}</span>'
            f'<span>{sum(item.requests for item in rest)}</span>'
            f'<span class="model-cost">{"—" if not all(item.priced for item in rest) else fmt_cny(sum(item.cost for item in rest))}</span>'
            "</div>"
        )
    if not models:
        model_rows = '<div class="model-row"><span class="model-name">本周暂无模型用量</span><span></span><span></span><span></span></div>'
    model_note = (
        "按 DeepSeek 官方价目（2026-10 核对）估算：高峰价，空闲时段半价；缓存写入并入未命中输入；以实际账单为准。"
        + ("" if cost_complete else " 有未收录价格的模型未计价。")
    )

    risk_text = context["risk_text"]
    plan_items = context["plan_items"]
    plan_html = "".join(
        '<div style="flex:1;background:#fff;border:1px solid #e8e9f0;border-radius:18px;padding:20px 22px">'
        f'<div style="font-size:21px;color:#5b5bd6;font-weight:700">0{i + 1}</div>'
        f'<div style="margin-top:9px;font-size:22px;color:#2b2c33;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">{html.escape(truncate(text, 20))}</div>'
        "</div>"
        for i, text in enumerate(plan_items[:3])
    )

    badge = ""
    if preview:
        badge = (
            '<span style="margin-left:12px;font-size:17px;color:#c2410c;background:#fff3e8;'
            'border:1px solid #f5c9a5;border-radius:8px;padding:2px 10px">预览版</span>'
        )
    elif demo:
        badge = (
            '<span style="margin-left:12px;font-size:17px;color:#c2410c;background:#fff3e8;'
            'border:1px solid #f5c9a5;border-radius:8px;padding:2px 10px">示例数据</span>'
        )

    week_label = f"{start:%Y-%m-%d} ~ {end:%m-%d}"
    until_label = f"{end:%m-%d %H:%M}"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    width: {CARD_WIDTH}px; height: {card_height}px; overflow: hidden;
    background: #f4f5f8;
    font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    color: #17171d;
  }}
  .page {{ padding: 62px 72px 50px; height: {card_height}px; display: flex; flex-direction: column; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .brand {{ font-size: 22px; letter-spacing: 4px; font-weight: 800; color: #5b5bd6; }}
  .date {{ font-size: 23px; color: #8f93a3; }}
  h1 {{ margin: 38px 0 0; font-size: 74px; line-height: 1.05; font-weight: 800; letter-spacing: -1px; }}
  .subtitle {{ margin-top: 20px; font-size: 27px; color: #5f6270; }}
  .kpis {{ margin-top: 32px; display: flex; gap: 16px; }}
  .card {{ background: #fff; border: 1px solid #e8e9f0; border-radius: 20px; }}
  .token {{ margin-top: 24px; padding: 22px 26px; }}
  .token-grid {{ display: flex; gap: 26px; }}
  .token-left {{ flex: 1.35; min-width: 0; }}
  .token-right {{ flex: 1; min-width: 0; border-left: 1px solid #eceef4; padding-left: 26px; }}
  .token-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 21px; color: #5f6270; }}
  .token-head .note {{ color: #a0a3b1; font-size: 18px; }}
  .bar {{ margin-top: 16px; display: flex; gap: 2px; border-radius: 9px; overflow: hidden; background: #ececf3; height: 18px; }}
  .legend {{ margin-top: 15px; font-size: 19px; color: #6f7280; display: flex; flex-wrap: wrap; row-gap: 6px; }}
  .pie-wrap {{ display: flex; align-items: center; gap: 16px; margin-top: 12px; }}
  .donut {{ width: 132px; height: 132px; border-radius: 50%; position: relative; flex: 0 0 132px; }}
  .donut-hole {{ position: absolute; inset: 31px; background: #fff; border-radius: 50%;
                 display: flex; flex-direction: column; align-items: center; justify-content: center; }}
  .donut-hole b {{ font-size: 21px; line-height: 1; }}
  .donut-hole span {{ margin-top: 4px; font-size: 12px; color: #8a8d99; }}
  .pie-legend {{ flex: 1; min-width: 0; }}
  .section {{ margin-top: 38px; }}
  .section-title {{ display: flex; align-items: center; font-size: 25px; font-weight: 700; color: #17171d; }}
  .dot {{ width: 13px; height: 13px; border-radius: 50%; background: #5b5bd6; margin-right: 12px; }}
  .models {{ margin-top: 16px; padding: 16px 24px 12px; }}
  .models-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 19px; color: #5f6270; }}
  .models-head .note {{ color: #a0a3b1; font-size: 18px; }}
  .model-table {{ margin-top: 8px; }}
  .model-row {{ display: flex; align-items: baseline; padding: 4px 0; font-size: 18px; color: #4f5260; }}
  .model-row > span:nth-child(1) {{ flex: 1.55; }}
  .model-row > span:nth-child(2) {{ flex: 1; text-align: right; }}
  .model-row > span:nth-child(3) {{ flex: 0.55; text-align: right; }}
  .model-row > span:nth-child(4) {{ flex: 0.9; text-align: right; color: #17171d; }}
  .model-header {{ font-size: 15px; color: #a0a3b1; border-bottom: 1px solid #eceef4; padding-bottom: 5px; }}
  .model-total {{ border-top: 1px solid #eceef4; margin-top: 2px; padding-top: 6px; font-weight: 700; color: #17171d; }}
  .model-name {{ font-weight: 700; color: #2b2c33; }}
  .model-cost {{ font-weight: 700; }}
  .model-note {{ margin-top: 6px; font-size: 14px; color: #a0a3b1; }}
  .risk {{ margin-top: 24px; background: #fff7ec; border: 1px solid #f6dfbd; border-radius: 16px;
          padding: 20px 24px; font-size: 22px; color: #8a4b12; line-height: 1.5; }}
  .risk b {{ color: #b45309; margin-right: 12px; }}
  .plans {{ margin-top: 20px; display: flex; gap: 18px; }}
  .footer {{ margin-top: auto; padding-top: 22px; display: flex; justify-content: space-between;
             font-size: 18px; color: #a0a3b1; border-top: 1px solid #e8e9f0; }}
</style></head><body><div class="page">
  <div class="top">
    <div class="brand">DSH WEEKLY BRIEF{badge}</div>
    <div class="date">{week_label} · 周报</div>
  </div>
  <h1>本周工作总结</h1>
  <div class="subtitle">{html.escape(subtitle)}</div>

  <div class="kpis">
    {kpi(fmt_tokens(stats.total), "Token 用量")}
    {kpi(f"{stats.requests}", "模型请求")}
    {kpi(f"{stats.sessions}", "会话数")}
    {kpi(f"{stats.cache_hit:.1f}%", "缓存命中")}
    {kpi(fmt_cny(cost_total), "API 费用", 34)}
  </div>

  <div class="card token">
    <div class="token-grid">
      <div class="token-left">
        <div class="token-head"><span>Token 构成</span><span class="note">provider 计费口径</span></div>
        <div class="bar">{bar_parts}</div>
        <div class="legend">{legend}</div>
      </div>
      <div class="token-right">
        <div class="token-head"><span>任务 Token 占比</span><span class="note">任务合计 {task_pct:.1f}%</span></div>
        <div class="pie-wrap">
          <div class="donut" style="background:{pie_css}">
            <div class="donut-hole"><b>{task_pct:.1f}%</b><span>任务占比</span></div>
          </div>
          <div class="pie-legend">{pie_legend}</div>
        </div>
      </div>
    </div>
  </div>

  <div class="card models">
    <div class="models-head"><span>模型 Token / 费用</span><span class="note">API 费用 ≈ {fmt_cny(cost_total)}</span></div>
    <div class="model-table">
      <div class="model-row model-header"><span>模型</span><span>Token</span><span>请求</span><span>费用</span></div>
      {model_rows}
      <div class="model-row model-total"><span>合计</span><span>{fmt_tokens(stats.total)}</span><span>{stats.requests}</span><span>{fmt_cny(cost_total)}</span></div>
    </div>
    <div class="model-note">{html.escape(model_note)}</div>
  </div>

  <div class="section">
    <div class="section-title"><span class="dot"></span>本周重点</div>
    {highlights_html}
  </div>

  <div class="risk"><b>风险 / 阻塞</b>{html.escape(risk_text)}</div>

  <div class="section">
    <div class="section-title"><span class="dot"></span>下周重点</div>
    <div class="plans">{plan_html}</div>
  </div>

  <div class="footer">
    <span>数据：DSH 会话日志（provider usage）· superagent 工单</span>
    <span>本周 {start:%m-%d} ~ {end:%m-%d} · 数据截至 {until_label} · DSH 自动生成</span>
  </div>
</div></body></html>"""


def screenshot(html_path: Path, png_path: Path, height: int = CARD_HEIGHT) -> None:
    chromium = find_chromium()
    if not chromium:
        raise RuntimeError("no chromium/chrome binary found for headless screenshot")
    proc = subprocess.run(
        [
            str(chromium),
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-sandbox",
            "--force-device-scale-factor=1",
            f"--window-size={CARD_WIDTH},{height}",
            f"--screenshot={png_path}",
            html_path.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"chromium screenshot failed: {proc.stderr[-400:]}")
    if png_path.stat().st_size < 5000:
        raise RuntimeError("chromium screenshot is suspiciously small")


# --------------------------------------------------------------------------
# assembly + delivery


def resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    now = datetime.now(TZ)
    end = parse_local(args.end) if args.end else now
    if end > now:
        end = now
    if args.start:
        start = parse_local(args.start)
    else:
        monday = end.date() - timedelta(days=end.weekday())
        start = datetime.combine(monday, time(0, 0), tzinfo=TZ)
    if end - start < timedelta(hours=1):
        start = end - timedelta(days=7)
    return start, end


def build_context(args: argparse.Namespace) -> dict:
    start, end = resolve_window(args)

    if args.demo:
        stats = UsageStats(
            input_tokens=161_000,
            output_tokens=249_000,
            cache_read_tokens=52_267_000,
            requests=246,
            sessions=2,
        )
        snapshot = TicketSnapshot(total=6, done=3)
        snapshot.items = [
            WorkItem("示例任务 A：图片需求回复", "按图内要求处理并回复，对应 PR #21 仍在推进", "完成", 3, None),
            WorkItem("示例任务 B：GPU SDPR 提交验证", "实现 batch 并行 SDPR 层，提交 PR #20", "完成", 2, None),
            WorkItem("示例任务 C：三项文档提交待审", "训练 notebook、实验设计、对比文档均已提交 PR", "进行中", 1, None),
        ]
        snapshot.blocked = [WorkItem("示例任务 D", "", "受阻", 1, None)]
        prs = [PullRequest(20, "示例 PR 20", "https://github.com/example/repo/pull/20", "OPEN")]
        shares = [
            TaskShare("示例任务 A", 15_000_000, 28.5, PIE_COLORS[0]),
            TaskShare("示例任务 B", 11_000_000, 20.9, PIE_COLORS[1]),
            TaskShare("示例任务 C", 6_500_000, 12.3, PIE_COLORS[2]),
            TaskShare("非任务会话", 20_177_000, 38.3, PIE_COLORS[-1]),
        ]
        task_pct = 61.7
        task_total = 32_500_000
        models = [
            ModelUsage("deepseek-official", "deepseek-flash", 100_000, 150_000, 30_000_000, 0, 150, 2.60, True),
            ModelUsage("deepseek-official", "deepseek-v4-pro", 61_000, 99_000, 22_267_000, 0, 96, 9.90, True),
        ]
        cost_total, cost_complete = 12.50, True
        risk_text = "示例阻塞：需要人工 review 后再合并。"
        plan_items = ["审阅并合并 PR #20", "跟进示例任务 D", "补齐示例文档"]
        subtitle = "示例数据：本周处理 6 项任务，3 项完成"
    else:
        tickets = load_tickets()
        titles = {ticket["id"]: strip_markdown(ticket.get("title") or "") for ticket in tickets}
        stats, session_tokens, model_usages = collect_usage(start, end)
        models = sorted(model_usages.values(), key=lambda item: item.total, reverse=True)
        cost_total = sum(item.cost for item in models)
        cost_complete = all(item.priced for item in models) if models else True
        snapshot = collect_tickets(tickets, start, end)
        prs = collect_prs(snapshot, args.repo)
        shares, task_pct, task_total = attribute_task_shares(session_tokens, titles)

        subtitle = f"本周 DSH 处理 {snapshot.total} 项任务，{snapshot.done} 项完成"
        if snapshot.blocked:
            subtitle += f"，{len(snapshot.blocked)} 项受阻"
        if not snapshot.total:
            subtitle = "本周暂无 DSH 任务记录，以下为 token 用量统计"

        risk_parts: list[str] = []
        for item in snapshot.attention[:2]:
            risk_parts.append(f"「{truncate(item.title, 12)}」等待你确认")
        for item in snapshot.blocked[:2]:
            risk_parts.append(f"「{truncate(item.title, 12)}」受阻")
        if prs:
            risk_parts.append(f"{len(prs)} 个 PR 待人工评审/合并（{', '.join('#%d' % pr.number for pr in prs[:3])}）")
        risk_text = "；".join(risk_parts) if risk_parts else "无阻塞，本周任务均正常推进。"

        plan_items = []
        planned: set[str] = set()
        for item in snapshot.attention:
            if item.title in planned:
                continue
            planned.add(item.title)
            plan_items.append(f"回复「{truncate(item.title, 8)}」确认")
        for item in snapshot.blocked:
            if item.title in planned:
                continue
            planned.add(item.title)
            plan_items.append(f"跟进「{truncate(item.title, 8)}」")
        for pr in prs:
            plan_items.append(f"审阅并合并 PR #{pr.number}")
        for item in snapshot.running:
            if item.title in planned:
                continue
            planned.add(item.title)
            plan_items.append(f"继续「{truncate(item.title, 8)}」")
        if not plan_items:
            plan_items = ["保持日常维护与待命", "关注新请求"]

    display_rows = min(len(models), 4) + (1 if len(models) > 4 else 0)
    card_height = CARD_HEIGHT + max(0, display_rows - 2) * 30

    return {
        "stats": stats,
        "tickets": snapshot,
        "shares": shares,
        "prs": prs,
        "models": models,
        "card_height": card_height,
        "cost_total": cost_total,
        "cost_complete": cost_complete,
        "task_pct": task_pct,
        "task_total": task_total,
        "start": start,
        "end": end,
        "demo": args.demo,
        "preview": args.preview,
        "subtitle": subtitle,
        "risk_text": risk_text,
        "plan_items": plan_items,
    }


def send_text_fallback(chat: str, context: dict, reason: str) -> None:
    """Send a plain markdown digest if image rendering/posting fails."""
    stats: UsageStats = context["stats"]
    lines = [
        f"**DSH 周报 · {context['start']:%m-%d} ~ {context['end']:%m-%d}**",
        "",
        f"- Token 用量：{fmt_tokens(stats.total)}（缓存 {fmt_tokens(stats.cache_read_tokens)} / 输入 {fmt_tokens(stats.input_tokens)} / 输出 {fmt_tokens(stats.output_tokens)}）",
        f"- 模型请求：{stats.requests} · 会话数：{stats.sessions} · 缓存命中：{stats.cache_hit:.1f}%",
        f"- 任务 Token 占比：{context['task_pct']:.1f}%（{', '.join(f'{s.label} {s.pct:.1f}%' for s in context['shares'][:4])}）",
        f"- API 费用：约 {fmt_cny(context['cost_total'])}（DeepSeek 官方价目，高峰价/空闲半价）",
        *[
            f"  - {item.model}：{fmt_tokens(item.total)} token · {fmt_cny(item.cost) if item.priced else '未收录价格'}"
            for item in context["models"][:4]
        ],
        "",
        "**本周重点**",
    ]
    for i, item in enumerate(context["tickets"].items[:3], 1):
        lines.append(f"{i}. {item.title} — {truncate(item.desc, 60)}")
    if not context["tickets"].items:
        lines.append("本周暂无 DSH 任务记录。")
    lines += ["", f"**风险 / 阻塞**：{context['risk_text']}", "", "**下周重点**"]
    lines += [f"{i}. {text}" for i, text in enumerate(context["plan_items"][:3], 1)]
    lines += ["", f"_图片卡片生成失败，已降级为文本（{reason}）_"]
    proc = subprocess.run(
        [str(LARK_BOT), "im", "+messages-send", "--chat-id", chat, "--markdown", "\n".join(lines)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(proc.stdout.strip()[:500])
    if proc.returncode != 0:
        raise RuntimeError(f"text fallback send failed: {proc.stderr[-300:]}")


def post_image(chat: str, png_path: Path) -> str:
    proc = subprocess.run(
        [str(LARK_BOT), "im", "+messages-send", "--chat-id", chat, "--image", png_path.name],
        cwd=str(png_path.parent),
        capture_output=True,
        text=True,
        timeout=90,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    if proc.returncode != 0 or not payload.get("ok"):
        raise RuntimeError(f"image send failed: {proc.stderr[-300:] or proc.stdout[-300:]}")
    data = payload.get("data") or {}
    return str(data.get("message_id") or data.get("message", {}).get("message_id") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Post the DSH weekly brief to Feishu")
    parser.add_argument("--start", help="window start (YYYY-MM-DD or ISO datetime), default this Monday 00:00")
    parser.add_argument("--end", help="window end (YYYY-MM-DD or ISO datetime), default now")
    parser.add_argument("--chat", default=os.environ.get("DSH_WEEKLY_BRIEF_CHAT"),
                        help="target chat id; defaults to $DSH_WEEKLY_BRIEF_CHAT")
    parser.add_argument("--repo", default=os.environ.get("DSH_SUPERAGENT_GITHUB_REPOSITORY", DEFAULT_REPO))
    parser.add_argument("--preview", action="store_true", help="mark the card as a preview")
    parser.add_argument("--no-post", action="store_true", help="render only, do not send to Feishu")
    parser.add_argument("--dry-run", action="store_true", help="alias for --no-post, keep temp files")
    parser.add_argument("--demo", action="store_true", help="render a synthetic example card")
    parser.add_argument("--out", help="output PNG path")
    args = parser.parse_args()
    if not args.no_post and not args.dry_run and not args.chat:
        parser.error("--chat or $DSH_WEEKLY_BRIEF_CHAT is required when posting")

    started = datetime.now(TZ)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    lock_handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[skip] another weekly brief run is in progress")
        return 0

    workdir: Path | None = None
    try:
        context = build_context(args)
        png_path = Path(args.out) if args.out else OUT_DIR / f"weekly-brief-{context['end']:%Y-%m-%d}.png"
        png_path.parent.mkdir(parents=True, exist_ok=True)

        html_text = render_html(context)
        workdir = Path(tempfile.mkdtemp(prefix="dsh-weekly-brief-"))
        html_path = workdir / "brief.html"
        html_path.write_text(html_text, encoding="utf-8")
        temp_png = workdir / png_path.name

        render_error: str | None = None
        try:
            screenshot(html_path, temp_png, context.get("card_height", CARD_HEIGHT))
            shutil.copyfile(temp_png, png_path)
        except Exception as exc:  # noqa: BLE001 - fall back to text
            render_error = str(exc)
            print(f"[warn] render failed: {exc}", file=sys.stderr)

        stats: UsageStats = context["stats"]
        print(
            json.dumps(
                {
                    "start": context["start"].isoformat(),
                    "end": context["end"].isoformat(),
                    "tokens": stats.total,
                    "requests": stats.requests,
                    "sessions": stats.sessions,
                    "cache_hit_pct": round(stats.cache_hit, 1),
                    "task_token_pct": round(context["task_pct"], 1),
                    "cost_cny": round(context["cost_total"], 2),
                    "models": [
                        {"model": item.model, "tokens": item.total, "requests": item.requests,
                         "cost_cny": round(item.cost, 2), "priced": item.priced}
                        for item in context["models"]
                    ],
                    "tickets": context["tickets"].total,
                    "done": context["tickets"].done,
                    "prs": [p.number for p in context["prs"]],
                    "png": str(png_path) if render_error is None else None,
                    "post": not (args.no_post or args.dry_run),
                },
                ensure_ascii=False,
            )
        )

        if args.no_post or args.dry_run:
            print(f"[dry-run] card written to {png_path}")
            return 0

        if render_error is not None:
            send_text_fallback(args.chat, context, render_error)
            print("[fallback] posted markdown digest")
            return 0

        message_id = post_image(args.chat, png_path)
        print(f"[posted] message_id={message_id or 'unknown'}")
        elapsed = (datetime.now(TZ) - started).total_seconds()
        print(f"[done] {fmt_duration(elapsed)}")
        return 0
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        finally:
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
