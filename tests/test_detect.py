#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota-watch 检测逻辑离线测试（不依赖网络 / 不消耗额度 / 不写系统目录）。

    python3 tests/test_detect.py
"""

import datetime as dt
import importlib.util
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_qw():
    spec = importlib.util.spec_from_file_location("qw", os.path.join(ROOT, "qw.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qw = load_qw()


def ev(payload, etype="event_msg"):
    return {"type": etype, "payload": payload}


def rollout_limited(resets_at, message="You’ve hit your usage limit. Upgrade to Pro or try again at 2:33 PM."):
    return [
        ev({"id": "11111111-2222-3333-4444-555555555555"}, etype="session_meta"),
        ev({"type": "task_started", "turn_id": "turn-1"}),
        ev({
            "type": "token_count",
            "info": {"total_token_usage": {}},
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": resets_at},
                "secondary": {"used_percent": 42.0, "window_minutes": 10080, "resets_at": resets_at + 90000},
            },
        }),
        ev({"type": "task_complete", "turn_id": "turn-1",
            "error": {"message": message, "codex_error_info": "usage_limit_exceeded"}}),
    ]


class TestResetParsing(unittest.TestCase):
    def test_ampm(self):
        ts = qw.parse_reset_time("... or try again at 2:33 PM.")
        self.assertIsNotNone(ts)
        d = dt.datetime.fromtimestamp(ts)
        self.assertEqual((d.hour, d.minute), (14, 33))
        self.assertGreater(ts, time.time() - 60)

    def test_ampm_midnight(self):
        ts = qw.parse_reset_time("try again at 12:48 AM")
        d = dt.datetime.fromtimestamp(ts)
        self.assertEqual((d.hour, d.minute), (0, 48))

    def test_absolute_date(self):
        ts = qw.parse_reset_time("try again at Oct 13th, 2026 12:48 AM.")
        self.assertEqual(
            dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"), "2026-10-13 00:48"
        )

    def test_resets_in(self):
        ts = qw.parse_reset_time("Your limit resets in 30 min")
        self.assertAlmostEqual(ts, time.time() + 1800, delta=15)

    def test_none(self):
        self.assertIsNone(qw.parse_reset_time("stream disconnected before completion"))
        self.assertIsNone(qw.parse_reset_time(""))


class TestCodexDetection(unittest.TestCase):
    def test_limited_turn(self):
        resets_at = int(time.time()) + 7200
        analysis = qw.analyze_codex_events(rollout_limited(resets_at))
        self.assertEqual(analysis["thread_id"], "11111111-2222-3333-4444-555555555555")
        limited = qw.codex_limited(analysis, {"usage_limit_exceeded"})
        self.assertIsNotNone(limited)
        self.assertEqual(limited["turn_id"], "turn-1")
        self.assertEqual(qw.reset_from_rate_limits(analysis["rate_limits"]), resets_at)

    def test_resumed_after_limit(self):
        resets_at = int(time.time()) + 7200
        events = rollout_limited(resets_at)
        events.append(ev({"type": "task_started", "turn_id": "turn-2"}))
        analysis = qw.analyze_codex_events(events)
        self.assertIsNone(qw.codex_limited(analysis, {"usage_limit_exceeded"}))

    def test_completed_normally(self):
        resets_at = int(time.time()) + 7200
        events = rollout_limited(resets_at)
        events[-1] = ev({"type": "task_complete", "turn_id": "turn-1", "error": None})
        analysis = qw.analyze_codex_events(events)
        self.assertIsNone(qw.codex_limited(analysis, {"usage_limit_exceeded"}))

    def test_user_aborted(self):
        events = rollout_limited(int(time.time()) + 7200)
        events.append(ev({"type": "turn_aborted", "turn_id": "turn-1"}))
        analysis = qw.analyze_codex_events(events)
        self.assertIsNone(qw.codex_limited(analysis, {"usage_limit_exceeded"}))

    def test_non_limit_error(self):
        events = rollout_limited(
            int(time.time()) + 7200,
            message="stream disconnected before completion: Transport error",
        )
        events[-1]["payload"]["error"]["codex_error_info"] = "other"
        analysis = qw.analyze_codex_events(events)
        self.assertIsNone(qw.codex_limited(analysis, {"usage_limit_exceeded"}))

    def test_reset_from_rate_limits_prefers_hot_window(self):
        now = time.time()
        rl = {
            "primary": {"used_percent": 30.0, "window_minutes": 300, "resets_at": now + 600},
            "secondary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": now + 90000},
        }
        self.assertEqual(qw.reset_from_rate_limits(rl), now + 90000)

    def test_tail_reader_handles_truncated_first_line(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            for line in rollout_limited(int(time.time()) + 3600):
                fh.write(json.dumps(line) + "\n")
            path = fh.name
        try:
            events = qw.read_tail_events(path, max_bytes=600)
            self.assertTrue(events, "应当至少解析出末尾的 JSON 行")
            self.assertEqual(events[-1]["payload"]["type"], "task_complete")
        finally:
            os.unlink(path)


def pane(*lines):
    return "\n".join(lines)


class TestClaudePaneDetection(unittest.TestCase):
    def cfg(self):
        return {"claude": {"tail_lines": 14}}

    def test_normal_pane(self):
        text = pane(
            "⏺ 已完成重构，测试通过。",
            "",
            "> 继续优化一下性能",
            "  12 passed in 0.84s",
            "> ",
        )
        limited, pattern, reset = qw.claude_pane_limited(self.cfg(), text)
        self.assertFalse(limited, pattern)

    def test_glm_429(self):
        text = pane(
            "⏺ 正在修改 service.py ...",
            "API Error: 429 {\"error\":{\"code\":\"1302\",\"message\":\"当前并发数过高，请稍后重试\"}}",
            "",
            "> ",
        )
        limited, pattern, reset = qw.claude_pane_limited(self.cfg(), text)
        self.assertTrue(limited)
        self.assertIn("429", pattern)

    def test_anthropic_session_limit_with_reset_time(self):
        text = pane(
            "You've hit your session limit · your session limit resets at 3:00 PM",
            "Retrying in 5m (resets at 3:00 PM) · attempt 3/5",
            "> ",
        )
        limited, _pattern, reset = qw.claude_pane_limited(self.cfg(), text)
        # 末尾有 Retrying 行时属于「正在重试」，不应打断
        self.assertFalse(limited)

        text2 = pane("You've hit your session limit · your session limit resets at 3:00 PM", "> ")
        limited2, _p2, reset2 = qw.claude_pane_limited(self.cfg(), text2)
        self.assertTrue(limited2)
        self.assertIsNotNone(reset2)
        self.assertEqual(dt.datetime.fromtimestamp(reset2).hour, 15)

    def test_chinese_quota(self):
        text = pane(
            "⏺ 调用模型失败",
            "当前账户额度已用尽，将于 15:30 恢复",
            "> ",
        )
        limited, _pattern, reset = qw.claude_pane_limited(self.cfg(), text)
        self.assertTrue(limited)
        self.assertIsNone(reset)  # 文案里没写可解析时间，走退避

    def test_busy_pane_not_flagged(self):
        text = pane(
            "API Error: 429 Too Many Requests",
            "✻ Thinking… (esc to interrupt)",
        )
        limited, _pattern, _reset = qw.claude_pane_limited(self.cfg(), text)
        self.assertFalse(limited)

    def test_old_mention_far_above_tail(self):
        lines = ["API Error: 429 rate limited"] + [f"普通输出 {i}" for i in range(40)] + ["> "]
        limited, _pattern, _reset = qw.claude_pane_limited(self.cfg(), pane(*lines))
        self.assertFalse(limited)


class TestStateMachine(unittest.TestCase):
    def test_codex_entry_lifecycle(self):
        tmp = os.path.join(ROOT, "tests", ".tmp-state-dir")
        os.makedirs(tmp, exist_ok=True)
        old_home = qw.QW_HOME
        qw.QW_HOME = tmp
        try:
            state = {"version": 1, "entries": {}}
            cfg = json.loads(json.dumps(qw.DEFAULT_CONFIG))
            cfg["codex"]["enabled"] = True
            cfg["notify"] = False
            resets_at = int(time.time()) + 7200
            analysis = qw.analyze_codex_events(rollout_limited(resets_at))
            limited = qw.codex_limited(analysis, {"usage_limit_exceeded"})

            # 首次检测：写入 waiting 状态，不立即续跑
            from unittest import mock
            with mock.patch.object(qw, "codex_resume", return_value=(True, "mock")) as m:
                ent = {
                    "kind": "codex", "id": analysis["thread_id"], "turn": limited["turn_id"],
                    "first_seen": time.time(), "reset_at": resets_at, "attempts": 0,
                    "next_at": resets_at + 60, "status": "waiting",
                }
                qw.handle_codex_limited(cfg, "codex:x", ent, analysis, dry=True)
                self.assertEqual(ent["attempts"], 0)
                self.assertEqual(ent["status"], "waiting")
                m.assert_not_called()

                # 到点后：调用续跑并进入 resuming
                ent["next_at"] = time.time() - 1
                with mock.patch.object(qw, "queue_pending_age", return_value=None):
                    qw.handle_codex_limited(cfg, "codex:x", ent, analysis, dry=True)
                self.assertEqual(ent["attempts"], 1)
                self.assertEqual(ent["status"], "resuming")

                # 重试上限
                ent["attempts"] = cfg["codex"]["max_attempts"]
                ent["next_at"] = time.time() - 1
                qw.handle_codex_limited(cfg, "codex:x", ent, analysis, dry=True)
                self.assertEqual(ent["status"], "giveup")
        finally:
            qw.QW_HOME = old_home
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
