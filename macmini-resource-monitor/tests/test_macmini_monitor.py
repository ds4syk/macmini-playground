#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macmini_monitor.py 的单元测试（仅用标准库）。"""

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import macmini_monitor as mm  # noqa: E402


GB = 1024 ** 3


def make_config(**overrides):
    config = copy.deepcopy(mm.DEFAULT_CONFIG)
    config["notify"]["chat_id"] = "oc_test_chat"
    config["notify"]["lark_cli"] = "/bin/echo"
    config["notify"]["max_attempts"] = 1
    config["notify"]["retry_delay_seconds"] = 0
    return mm.deep_merge(config, overrides)


def make_metrics(
    cpu=10.0,
    available_pct=50.0,
    compressor_pct=5.0,
    swap_pct=6.0,
    pressure=1,
    total=16 * GB,
):
    return mm.Metrics(
        sampled_at=0.0,
        cpu_busy_pct=float(cpu),
        cpu_user_pct=float(cpu) * 0.6,
        cpu_sys_pct=float(cpu) * 0.4,
        load1=1.0,
        load5=1.0,
        load15=1.0,
        ncpu=10,
        mem_total_bytes=total,
        mem_available_bytes=int(total * available_pct / 100.0),
        mem_free_bytes=int(total * 0.02),
        mem_inactive_bytes=int(total * 0.4),
        mem_compressor_bytes=int(total * compressor_pct / 100.0),
        swap_total_bytes=2 * GB,
        swap_used_bytes=int(total * swap_pct / 100.0),
        pressure_level=pressure,
        disk_free_pct=50.0,
        pageouts=100,
    )


def drive(state, config, start, end, metrics, step=60):
    """按固定间隔推进状态机，返回期间排队的通知。"""
    queued = []
    t = start
    while t <= end:
        queued.extend(mm.evaluate(state, metrics, config, float(t), "test-host"))
        t += step
    return queued


class CpuStateMachineTests(unittest.TestCase):
    def test_alarm_requires_full_sustain_window(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)

        queued = drive(state, config, 0, 540, bad)
        self.assertEqual(queued, [])
        self.assertFalse(state["metrics"]["cpu"]["active"])
        self.assertEqual(state["metrics"]["cpu"]["bad_since"], 0)

        queued = drive(state, config, 600, 600, bad)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "alarm")
        self.assertTrue(state["metrics"]["cpu"]["active"])
        self.assertEqual(state["metrics"]["cpu"]["active_since"], 0)

    def test_bad_sample_resets_streak(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)
        good = make_metrics(cpu=20)

        queued = drive(state, config, 0, 120, bad)
        self.assertEqual(queued, [])
        drive(state, config, 180, 180, good)
        self.assertIsNone(state["metrics"]["cpu"]["bad_since"])

        queued = drive(state, config, 240, 780, bad)
        self.assertEqual(queued, [])
        queued = drive(state, config, 840, 840, bad)
        self.assertEqual([item.kind for item in queued], ["alarm"])

    def test_recovery_needs_clear_window_and_is_announced_once(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)
        good = make_metrics(cpu=20)

        queued = drive(state, config, 0, 600, bad)
        pending = queued[0].to_dict()
        mm.mark_notification_sent(state, "cpu", pending, 600)
        self.assertTrue(state["metrics"]["cpu"]["announced"])

        queued = drive(state, config, 660, 900, good)
        self.assertEqual(queued, [])
        self.assertTrue(state["metrics"]["cpu"]["active"])

        queued = drive(state, config, 960, 960, good)
        self.assertEqual([item.kind for item in queued], ["recovery"])
        self.assertFalse(state["metrics"]["cpu"]["active"])
        self.assertIn("✅ Mac mini 警报解除：CPU 持续偏高", queued[0].text)
        self.assertIn("本次告警持续：16 分钟", queued[0].text)
        self.assertIn("解除判定：恢复条件持续 5 分钟", queued[0].text)
        self.assertFalse(state["metrics"]["cpu"]["announced"])

        # 恢复后继续正常，不应再产生通知。
        queued = drive(state, config, 1020, 1200, good)
        self.assertEqual(queued, [])

    def test_reminder_after_one_hour_and_not_before(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)

        queued = drive(state, config, 0, 600, bad)
        mm.mark_notification_sent(state, "cpu", queued[0].to_dict(), 600)

        queued = drive(state, config, 660, 4140, bad)
        self.assertEqual(queued, [])

        queued = drive(state, config, 4200, 4200, bad)
        self.assertEqual([item.kind for item in queued], ["reminder"])

    def test_stale_undelivered_alarm_is_dropped_on_recovery(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)
        good = make_metrics(cpu=20)

        queued = drive(state, config, 0, 600, bad)
        self.assertEqual([item.kind for item in queued], ["alarm"])
        self.assertFalse(state["metrics"]["cpu"]["announced"])

        queued = drive(state, config, 660, 960, good)
        self.assertEqual(queued, [])
        self.assertFalse(state["metrics"]["cpu"]["active"])
        self.assertIsNone(state["metrics"]["cpu"]["pending"])

    def test_gap_resets_uninterrupted_streak(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(cpu=90)

        mm.evaluate(state, bad, config, 0, "test-host")
        # 机器睡眠/launchd 长时间未跑：下一次采样不能继承之前的连续计数。
        mm.evaluate(state, bad, config, 1000, "test-host")
        self.assertEqual(state["metrics"]["cpu"]["bad_since"], 1000)
        self.assertFalse(state["metrics"]["cpu"]["active"])

        queued = drive(state, config, 1060, 1600, bad)
        self.assertEqual([item.kind for item in queued], ["alarm"])


class MemoryJudgementTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_available_memory_triggers(self):
        verdict = mm.judge_memory(make_metrics(available_pct=8), self.config)
        self.assertTrue(verdict.alarm)
        self.assertTrue(any("可用内存" in reason for reason in verdict.reasons))

    def test_compressor_triggers(self):
        verdict = mm.judge_memory(make_metrics(compressor_pct=30), self.config)
        self.assertTrue(verdict.alarm)
        self.assertTrue(any("压缩内存" in reason for reason in verdict.reasons))

    def test_swap_alone_does_not_trigger(self):
        verdict = mm.judge_memory(
            make_metrics(swap_pct=35, available_pct=30), self.config
        )
        self.assertFalse(verdict.alarm)

    def test_swap_with_low_available_triggers(self):
        verdict = mm.judge_memory(
            make_metrics(swap_pct=35, available_pct=12), self.config
        )
        self.assertTrue(verdict.alarm)
        self.assertTrue(any("Swap" in reason for reason in verdict.reasons))

    def test_kernel_pressure_warning_triggers(self):
        verdict = mm.judge_memory(make_metrics(pressure=2), self.config)
        self.assertTrue(verdict.alarm)
        self.assertTrue(any("内核内存压力" in reason for reason in verdict.reasons))

    def test_clear_requires_all_signals_healthy(self):
        healthy = mm.judge_memory(make_metrics(available_pct=20, compressor_pct=10, pressure=1), self.config)
        self.assertTrue(healthy.clear)

        # 可用内存已经从告警线恢复，但还没到恢复线：保持观察，不误报恢复。
        in_between = mm.judge_memory(
            make_metrics(available_pct=12, compressor_pct=10, pressure=1), self.config
        )
        self.assertFalse(in_between.clear)

        # 压缩内存 20% 是解除线（告警线 25%），22% 落在迟滞带内，不算解除。
        at_clear_line = mm.judge_memory(
            make_metrics(available_pct=20, compressor_pct=20, pressure=1), self.config
        )
        self.assertTrue(at_clear_line.clear)
        inside_band = mm.judge_memory(
            make_metrics(available_pct=20, compressor_pct=22, pressure=1), self.config
        )
        self.assertFalse(inside_band.clear)
        self.assertFalse(inside_band.alarm)
        self.assertFalse(in_between.alarm)

        pressure_high = mm.judge_memory(
            make_metrics(available_pct=20, compressor_pct=10, pressure=2), self.config
        )
        self.assertFalse(pressure_high.clear)
        self.assertTrue(pressure_high.alarm)


class MemoryStateMachineTests(unittest.TestCase):
    def test_memory_alarm_and_recovery(self):
        config = make_config()
        state = mm.new_state("test-host")
        bad = make_metrics(available_pct=8, compressor_pct=28, pressure=2)
        good = make_metrics(available_pct=30, compressor_pct=8, pressure=1)

        queued = drive(state, config, 0, 540, bad)
        self.assertEqual(queued, [])
        queued = drive(state, config, 600, 600, bad)
        self.assertEqual([item.kind for item in queued], ["alarm"])
        self.assertIn("触发条件：", queued[0].text)
        self.assertIn("内核内存压力", queued[0].text)

        mm.mark_notification_sent(state, "memory", queued[0].to_dict(), 600)
        queued = drive(state, config, 660, 900, good)
        self.assertEqual(queued, [])
        queued = drive(state, config, 960, 960, good)
        self.assertEqual([item.kind for item in queued], ["recovery"])
        self.assertIn("✅ Mac mini 警报解除：内存紧张", queued[0].text)
        self.assertIn("解除判定：恢复条件持续 5 分钟", queued[0].text)


class ParsingTests(unittest.TestCase):
    def test_parse_top_cpu_uses_last_sample(self):
        text = (
            "Load Avg: 2.20, 1.95, 1.84\n"
            "CPU usage: 14.68% user, 10.16% sys, 75.14% idle\n"
            "\n"
            "Load Avg: 2.20, 1.95, 1.84\n"
            "CPU usage: 11.93% user, 1.92% sys, 86.14% idle\n"
        )
        self.assertEqual(mm.parse_top_cpu(text), (11.93, 1.92, 86.14))

    def test_parse_top_cpu_rejects_unknown_output(self):
        with self.assertRaises(mm.MonitorError):
            mm.parse_top_cpu("no cpu line here")

    def test_parse_load_avg(self):
        self.assertEqual(
            mm.parse_load_avg("Load Avg: 2.20, 1.95, 1.84\n"),
            (2.20, 1.95, 1.84),
        )

    def test_parse_vm_stat(self):
        text = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               4043.\n"
            "Pages inactive:                          416095.\n"
            "Pages speculative:                          431.\n"
            "Pages occupied by compressor:             55530.\n"
            '"Translation faults":                     915425389.\n'
        )
        page_size, pages = mm.parse_vm_stat(text)
        self.assertEqual(page_size, 16384)
        self.assertEqual(pages["free"], 4043)
        self.assertEqual(pages["inactive"], 416095)
        self.assertEqual(pages["occupied by compressor"], 55530)

    def test_parse_swapusage(self):
        total, used = mm.parse_swapusage(
            "total = 2048.00M  used = 988.44M  free = 1059.56M  (encrypted)"
        )
        self.assertEqual(total, int(2048.00 * 1024 ** 2))
        self.assertEqual(used, int(988.44 * 1024 ** 2))

    def test_fmt_helpers(self):
        self.assertEqual(mm.fmt_duration(0), "0 秒")
        self.assertEqual(mm.fmt_duration(59), "59 秒")
        self.assertEqual(mm.fmt_duration(600), "10 分钟")
        self.assertEqual(mm.fmt_duration(3660), "1 小时 1 分钟")
        self.assertEqual(mm.fmt_bytes(16 * GB), "16.00 GB")


class ConfigAndStateTests(unittest.TestCase):
    def test_valid_config_passes(self):
        self.assertEqual(mm.validate_config(make_config()), [])

    def test_missing_notification_config_is_rejected(self):
        config = copy.deepcopy(mm.DEFAULT_CONFIG)
        errors = mm.validate_config(config)
        self.assertTrue(any("chat_id" in error or "webhook_url" in error for error in errors))

    def test_bad_hysteresis_is_rejected(self):
        config = make_config()
        config["cpu"]["clear_busy_pct"] = 90
        config["memory"]["clear_available_pct"] = 5
        errors = mm.validate_config(config)
        self.assertTrue(any("cpu.clear_busy_pct" in error for error in errors))
        self.assertTrue(any("memory.clear_available_pct" in error for error in errors))

    def test_bad_chat_id_is_rejected(self):
        config = make_config()
        config["notify"]["chat_id"] = "not-a-chat-id"
        errors = mm.validate_config(config)
        self.assertTrue(any("chat_id" in error for error in errors))

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = mm.new_state("test-host")
            state["last_metrics"] = make_metrics().to_dict()
            mm.save_state(path, state)
            loaded = mm.load_state(path)
            self.assertEqual(loaded["host"], "test-host")
            self.assertEqual(loaded["last_metrics"]["cpu_busy_pct"], 10.0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            state = mm.load_state(path)
            self.assertFalse(path.exists())
            self.assertTrue(list(Path(tmp).glob("state.json.corrupt-*")))
            self.assertEqual(state["metrics"]["cpu"]["active"], False)

    def test_plist_contains_expected_arguments(self):
        plist = mm.build_plist(
            Path("/tmp/macmini_monitor.py"),
            Path("/tmp/config.json"),
            make_config(),
        )
        self.assertEqual(plist["Label"], mm.LAUNCHD_LABEL)
        self.assertEqual(
            plist["ProgramArguments"][1:],
            ["/tmp/macmini_monitor.py", "--config", "/tmp/config.json", "once"],
        )
        self.assertEqual(plist["StartInterval"], 60)
        self.assertEqual(plist["EnvironmentVariables"]["HOME"], str(Path.home()))
        self.assertIn("PATH", plist["EnvironmentVariables"])

    def test_launchd_path_includes_node(self):
        entries = mm.build_path_env().split(":")
        self.assertIn("/opt/homebrew/bin", entries)
        self.assertIn(str(Path("~/.local/bin").expanduser()), entries)
        node = shutil.which("node")
        if node:
            self.assertIn(str(Path(node).resolve().parent), entries)

    def test_parse_launchctl_state_with_spaces(self):
        result = mm.parse_launchctl_print(
            "gui/501/local.macmini-resource-monitor = {\n"
            "\tactive count = 0\n"
            "\tstate = not running\n"
            "}\n"
        )
        self.assertTrue(result["loaded"])
        self.assertEqual(result["state"], "not running")
        self.assertIsNone(result["pid"])

        result = mm.parse_launchctl_print(
            "gui/501/local.macmini-resource-monitor = {\n"
            "\tstate = running\n"
            "\tpid = 12345\n"
            "}\n"
        )
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["pid"], 12345)


class NotificationTransportTests(unittest.TestCase):
    def test_webhook_payload_and_success(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                captured["body"] = self.rfile.read(length).decode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":0,"msg":"success"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        config = make_config(notify={"webhook_url": f"http://127.0.0.1:{server.server_port}/hook"})
        notifier = mm.FeishuNotifier(config)
        notifier.send("hello 监控", "mm-test")
        thread.join(timeout=5)
        server.server_close()

        payload = json.loads(captured["body"])
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"]["text"], "hello 监控")

    def test_webhook_error_code_raises(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":19001,"msg":"bad request"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        config = make_config(notify={"webhook_url": f"http://127.0.0.1:{server.server_port}/hook"})
        with self.assertRaises(mm.NotificationError):
            mm.FeishuNotifier(config).send("hello", "mm-test")
        thread.join(timeout=5)
        server.server_close()

    def test_lark_cli_command_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            args_file = Path(tmp) / "args.json"
            fake = Path(tmp) / "fake-lark"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(args_file)!r}, 'w', encoding='utf-8').write("
                "json.dumps(sys.argv[1:], ensure_ascii=False))\n"
                "print('{\"code\":0}')\n",
                encoding="utf-8",
            )
            os.chmod(fake, 0o755)
            config = make_config()
            config["notify"]["lark_cli"] = str(fake)
            notifier = mm.FeishuNotifier(config)
            notifier.send("中文告警\n第二行", "mm-key")
            recorded = json.loads(args_file.read_text(encoding="utf-8"))
            self.assertEqual(
                recorded[:5],
                ["im", "+messages-send", "--chat-id", "oc_test_chat", "--text"],
            )
            self.assertEqual(recorded[5], "中文告警\n第二行")
            self.assertIn("--idempotency-key", recorded)
            self.assertIn("mm-key", recorded)

    def test_lark_cli_nonzero_exit_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake-lark"
            fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 7\n", encoding="utf-8")
            os.chmod(fake, 0o755)
            config = make_config()
            config["notify"]["lark_cli"] = str(fake)
            with self.assertRaises(mm.NotificationError):
                mm.FeishuNotifier(config).send("hello", "mm-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
