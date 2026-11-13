"""Command-line orchestration for the paper-trading platform."""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import config, engine, export, market, models, store, strategy


def log(message: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message), flush=True)


def ensure_data(conn, refresh: bool = True) -> None:
    store.init_portfolios(conn)
    if refresh:
        log("刷新行情（%d 个代码）..." % len(market.all_codes()))
        try:
            updated = market.refresh_bars(conn, market.all_codes(), days=420)
            log("行情已更新: %d 个代码" % len(updated))
        except Exception as exc:  # noqa: BLE001
            log("行情刷新失败（继续使用本地缓存）: %s" % exc)


def calendars(conn) -> list:
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM bars WHERE symbol = 'sh000001'"
    ).fetchone()
    if not row or not row["hi"]:
        return []
    return store.last_trading_days(conn, row["lo"], row["hi"])


def _build_prompts(conn, day: str) -> dict:
    prompts = {}
    for model in config.MODELS:
        reference = config.portfolio_id(model["id"], max(config.CAPITALS))
        prompts[model["id"]] = strategy.build_prompt(conn, model["id"], day, reference)
    return prompts


def generate_decisions(conn, day: str, only_missing: bool = False, only_errors: bool = False, max_workers: int = 5) -> dict:
    prompts = _build_prompts(conn, day)
    pending = []
    for model in config.MODELS:
        row = conn.execute(
            "SELECT id, error FROM decisions WHERE model_id = ? AND date = ?",
            (model["id"], day),
        ).fetchone()
        if only_missing and row:
            continue
        if only_errors and (not row or not row["error"]):
            continue
        pending.append(model)
    if not pending:
        return {}

    results = {}

    def work(model):
        started = time.time()
        try:
            parsed = models.generate_decision(model, prompts[model["id"]]["system"], prompts[model["id"]]["user"])
            return model["id"], parsed, None, int((time.time() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            return model["id"], None, str(exc), int((time.time() - started) * 1000)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
        futures = [pool.submit(work, model) for model in pending]
        for future in futures:
            model_id, parsed, error, wall_ms = future.result()
            model = config.model_by_id(model_id)
            if parsed:
                response = parsed["response"]
                store.save_decision(
                    conn,
                    model_id,
                    day,
                    parsed["targets"],
                    parsed["market_view"],
                    parsed["confidence"],
                    response["content"],
                    response["latency_ms"],
                    response["prompt_tokens"],
                    response["completion_tokens"],
                    error=None,
                )
                results[model_id] = parsed
                log(
                    "  %-16s %s 目标仓位 %d 只，置信度 %.2f，%dms"
                    % (
                        model["name"],
                        "OK",
                        len(parsed["targets"]),
                        parsed["confidence"],
                        response["latency_ms"],
                    )
                )
            else:
                store.save_decision(
                    conn, model_id, day, [], "", 0.0, "", wall_ms, 0, 0, error=error
                )
                results[model_id] = {"error": error}
                log("  %-16s 失败: %s" % (model["name"], error))
    return results


def execute_day(conn, day: str, require_decision_date: str = None) -> int:
    trades = 0
    for model in config.MODELS:
        decision = store.latest_pending_decision(conn, model["id"], day)
        if not decision:
            continue
        if require_decision_date and decision["date"] != require_decision_date:
            continue
        if decision["error"]:
            conn.execute(
                "UPDATE decisions SET executed_date = ? WHERE id = ?", (day, decision["id"])
            )
            conn.commit()
            log("  %-16s 决策失败（%s），保持持仓" % (model["name"], decision["error"][:60]))
            continue
        targets = json.loads(decision["targets_json"] or "[]")
        model_trades = 0
        for capital in config.CAPITALS:
            pid = config.portfolio_id(model["id"], capital)
            row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (pid,)).fetchone()
            result = engine.execute_targets(conn, row, model, day, targets)
            model_trades += result["trades"]
        trades += model_trades
        conn.execute(
            "UPDATE decisions SET executed_date = ? WHERE id = ?", (day, decision["id"])
        )
        conn.commit()
        log(
            "  %-16s 执行 %s 的决策于 %s 开盘，成交 %d 笔"
            % (model["name"], decision["date"], day, model_trades)
        )
    return trades


def mark_all(conn, day: str) -> None:
    for model in config.MODELS:
        for capital in config.CAPITALS:
            pid = config.portfolio_id(model["id"], capital)
            row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (pid,)).fetchone()
            engine.mark_to_market(conn, row, day)


def replay(conn, start: str, end: str, skip_generate: bool = False) -> None:
    days = store.last_trading_days(conn, start, end)
    if len(days) < 2:
        raise SystemExit("回放区间交易日不足: %s ~ %s" % (start, end))
    store.reset_portfolios(conn)
    conn.execute("DELETE FROM decisions WHERE date >= ?", (start,))
    conn.commit()
    log("回放 %d 个交易日: %s ~ %s" % (len(days), days[0], days[-1]))
    for index, day in enumerate(days):
        if index > 0:
            execute_day(conn, day, require_decision_date=days[index - 1])
        mark_all(conn, day)
        if index < len(days) - 1:
            log("生成 %s 收盘后的决策" % day)
            if not skip_generate:
                generate_decisions(conn, day)
        if index % 5 == 0 or index == len(days) - 1:
            log("进度 %d/%d" % (index + 1, len(days)))
    if not skip_generate:
        log("生成最后交易日 %s 的决策（下一交易日开盘执行）" % days[-1])
        generate_decisions(conn, days[-1], only_missing=True)
    log("回放完成")


def daily(conn, force: bool = False) -> None:
    days = calendars(conn)
    if not days:
        raise SystemExit("没有行情数据，请先 fetch")
    latest = days[-1]
    now = datetime.now()
    if now.strftime("%Y-%m-%d") == latest and now.hour * 60 + now.minute < 15 * 60 + 5 and not force:
        raise SystemExit("今天尚未收盘（%s），15:05 后再跑；调试可加 --force" % now.strftime("%H:%M"))
    last_eq = conn.execute("SELECT MAX(date) AS d FROM equity").fetchone()["d"]
    if not last_eq:
        process = [latest]
    else:
        process = [day for day in days if day > last_eq]
    if not process:
        log("没有新的交易日（最新权益日 %s）" % last_eq)
        if force:
            process = [latest]
        else:
            return
    log("处理交易日: %s" % ", ".join(process))
    for day in process:
        trades = execute_day(conn, day)
        mark_all(conn, day)
        generate_decisions(conn, day, only_missing=True)
        log("%s 完成，成交 %d 笔" % (day, trades))


def cmd_fetch(args) -> None:
    conn = store.connect()
    ensure_data(conn, refresh=True)


def cmd_replay(args) -> None:
    conn = store.connect()
    ensure_data(conn, refresh=args.refresh)
    replay(conn, args.start, args.end)
    path = config.WEB_DIR / "data.json"
    payload = export.write_export(conn, path)
    llm = payload["families"]["llm"]
    log("已生成 %s（LLM %d 个组合，%d 个交易日）" % (path, len(llm["portfolios"]), llm["trade_days"]))


def cmd_retry(args) -> None:
    conn = store.connect()
    ensure_data(conn, refresh=False)
    day = args.date or conn.execute("SELECT MAX(date) AS d FROM decisions").fetchone()["d"]
    if not day:
        raise SystemExit("no decisions to retry")
    log("重试 %s 的失败决策" % day)
    generate_decisions(conn, day, only_errors=True)
    path = config.WEB_DIR / "data.json"
    payload = export.write_export(conn, path)
    log("已重新生成 %s（截至 %s）" % (path, payload["families"]["llm"]["as_of"]))


def cmd_daily(args) -> None:
    conn = store.connect()
    ensure_data(conn, refresh=True)
    daily(conn, force=args.force)
    path = config.WEB_DIR / "data.json"
    payload = export.write_export(conn, path)
    log("已更新 %s（截至 %s）" % (path, payload["families"]["llm"]["as_of"]))


def cmd_export(args) -> None:
    conn = store.connect()
    path = pathlib.Path(args.out) if args.out else config.WEB_DIR / "data.json"
    payload = export.write_export(conn, path)
    llm = payload["families"]["llm"]
    log("已生成 %s（LLM 截至 %s，%d 个组合）" % (path, llm["as_of"], len(llm["portfolios"])))


def cmd_serve(args) -> None:
    directory = str(config.WEB_DIR)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    log("本地预览: http://%s:%d/" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_status(args) -> None:
    conn = store.connect()
    row = conn.execute("SELECT MAX(date) AS d FROM equity").fetchone()
    count = conn.execute("SELECT COUNT(*) AS n FROM portfolios").fetchone()["n"]
    trades = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    decisions = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    log("最新权益日 %s，组合 %d，成交 %d，决策 %d" % (row["d"], count, trades, decisions))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="多模型 A 股日线纸面交易平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="刷新行情数据")
    p_fetch.set_defaults(func=cmd_fetch)

    p_replay = sub.add_parser("replay", help="回放历史并生成决策")
    p_replay.add_argument("--start", default=config.START_DATE)
    p_replay.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p_replay.add_argument("--refresh", action="store_true", help="回放前刷新行情（较慢）")
    p_replay.set_defaults(func=cmd_replay)

    p_daily = sub.add_parser("daily", help="每日收盘后运行")
    p_daily.add_argument("--force", action="store_true")
    p_daily.set_defaults(func=cmd_daily)

    p_retry = sub.add_parser("retry", help="重试最新交易日的失败决策")
    p_retry.add_argument("--date", default="")
    p_retry.set_defaults(func=cmd_retry)

    p_export = sub.add_parser("export", help="导出前端数据")
    p_export.add_argument("--out", default="")
    p_export.set_defaults(func=cmd_export)

    p_serve = sub.add_parser("serve", help="本地预览")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8899)
    p_serve.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
