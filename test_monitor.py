#!/usr/bin/env python3
"""盯盘告警决策与行情拉取重试。"""

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor


def stalled_state():
    return {
        "phase": "monitoring",
        "last_check_date": "2026-11-04",
        "last_price": 3.423,
        "signals": {
            "volume_shrink": False,
            "macd_turning": False,
            "price_stabilize": False,
            "dividend_yield_high": False,
            "price_in_zone": False,
        },
        "signals_met_count": 0,
        "early_warning_sent": True,
        "early_warning_date": "2026-06-30",
        "buy_emails_sent": 1,
        "buy_signal_date": "2026-07-03",
        "buy_signal_confirmed": False,
        "last_alert_signature": "",
        "last_error_alert_date": "",
        "alert_pending": False,
        "notes": "",
    }


def all_failed():
    return {key: False for key in monitor.SIGNAL_NAMES}


class PlanAlertsTest(unittest.TestCase):
    def test_stalled_buy_sequence_sends_one_problem_alert_and_resets(self):
        messages, new_state = monitor.plan_alerts(
            stalled_state(), all_failed(), 0, "成交量: ❌未萎缩", 3.423, "2026-11-05"
        )
        self.assertEqual(len(messages), 1)
        subject, body = messages[0]
        self.assertIn("盯盘告警", subject)
        self.assertIn("中断", subject)
        self.assertIn("成交量萎缩", body)
        self.assertIn("2026-07-03", body)
        self.assertIn("预警标记已重置", body)
        self.assertEqual(new_state["buy_emails_sent"], 0)
        self.assertEqual(new_state["buy_signal_date"], "")
        self.assertFalse(new_state["early_warning_sent"])
        self.assertTrue(new_state["last_alert_signature"])

        again, _ = monitor.plan_alerts(new_state, all_failed(), 0, "成交量: ❌未萎缩", 3.423, "2026-11-06")
        self.assertEqual(again, [])

    def test_problem_change_alerts_once(self):
        state = stalled_state()
        state["buy_emails_sent"] = 0
        state["buy_signal_date"] = ""
        state["early_warning_sent"] = False
        state["early_warning_date"] = ""
        first, state = monitor.plan_alerts(state, all_failed(), 0, "五项都未过", 3.423, "2026-11-05")
        self.assertEqual(len(first), 1)
        self.assertIn("5项未通过", first[0][0])

        improved = all_failed()
        improved["price_stabilize"] = True
        second, state = monitor.plan_alerts(state, improved, 1, "价格企稳", 3.40, "2026-11-06")
        self.assertEqual(len(second), 1)
        self.assertIn("4项未通过", second[0][0])
        self.assertIn("价格企稳: 未通过 → 通过", second[0][1])

        third, _ = monitor.plan_alerts(state, improved, 1, "价格企稳", 3.40, "2026-11-09")
        self.assertEqual(third, [])

    def test_buy_and_warning_paths_still_send(self):
        state = stalled_state()
        state["buy_emails_sent"] = 0
        state["early_warning_sent"] = False
        signals = {key: True for key in monitor.SIGNAL_NAMES}
        messages, new_state = monitor.plan_alerts(state, signals, 5, "全部通过", 2.85, "2026-11-05")
        self.assertEqual(len(messages), 1)
        self.assertIn("信号确认①", messages[0][0])
        self.assertEqual(new_state["buy_emails_sent"], 1)

        warning_signals = all_failed()
        warning_signals["volume_shrink"] = True
        warning_signals["price_stabilize"] = True
        messages, warned = monitor.plan_alerts(
            stalled_state() | {"buy_emails_sent": 0, "early_warning_sent": False},
            warning_signals, 2, "两项通过", 2.90, "2026-11-05",
        )
        self.assertEqual([m[0] for m in messages], ["【盯盘预警】510880红利ETF 买入信号逼近，请关注"])
        self.assertTrue(warned["early_warning_sent"])
        quiet, _ = monitor.plan_alerts(warned, warning_signals, 2, "两项通过", 2.90, "2026-11-06")
        self.assertEqual(quiet, [])

    def test_partial_sequence_at_two_signals_keeps_early_warning(self):
        state = stalled_state()
        signals = all_failed()
        signals["volume_shrink"] = True
        signals["macd_turning"] = True
        messages, new_state = monitor.plan_alerts(state, signals, 2, "两项通过", 3.20, "2026-11-05")
        self.assertEqual(len(messages), 1)
        self.assertIn("保持开启", messages[0][1])
        self.assertTrue(new_state["early_warning_sent"])
        self.assertEqual(new_state["buy_emails_sent"], 0)

    def test_stop_loss_email_does_not_duplicate_problem_alert(self):
        messages, first = monitor.plan_alerts(stalled_state() | {"buy_emails_sent": 0}, all_failed(), 0, "跌破", 2.50, "2026-11-05")
        self.assertEqual(len(messages), 1)
        self.assertIn("跌破止损位", messages[0][0])
        again, _ = monitor.plan_alerts(first, all_failed(), 0, "跌破", 2.49, "2026-11-06")
        self.assertEqual(len(again), 1)
        self.assertIn("跌破止损位", again[0][0])


class FetchKlineTest(unittest.TestCase):
    def test_retries_remote_disconnect_then_parses(self):
        calls = {"n": 0}

        class Resp:
            def read(self):
                return json.dumps({"data": {"klines": [
                    "2026-11-04,3.40,3.42,3.45,3.38,100,1,1,0.5,0.01,0.1"
                ]}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=15):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            return Resp()

        with mock.patch.object(monitor, "urlopen", fake_urlopen):
            rows, err = monitor.fetch_kline(days=30, attempts=3, sleeper=lambda _s: None)
        self.assertEqual(err, "")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 3.42)
        self.assertEqual(calls["n"], 3)

    def test_gives_up_with_error_after_retries(self):
        def fake_urlopen(req, timeout=15):
            raise http.client.RemoteDisconnected("closed")

        with mock.patch.object(monitor, "urlopen", fake_urlopen):
            rows, err = monitor.fetch_kline(days=30, attempts=2, sleeper=lambda _s: None)
        self.assertEqual(rows, [])
        self.assertIn("RemoteDisconnected", err)
        self.assertIn("closed", err)


class MainFailureAlertTest(unittest.TestCase):
    def test_fetch_failure_sends_alert_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(stalled_state()), encoding="utf-8")
            with mock.patch.object(monitor, "STATE_FILE", state_path), \
                    mock.patch.object(monitor, "fetch_kline", return_value=([], "RemoteDisconnected: closed")), \
                    mock.patch.object(monitor, "send_email", return_value=True) as send, \
                    mock.patch.object(monitor, "QQ_EMAIL", "a@qq.com"), \
                    mock.patch.object(monitor, "QQ_SMTP_CODE", "secret"):
                with self.assertRaises(SystemExit) as ctx:
                    monitor.main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(send.call_count, 1)
            self.assertIn("行情拉取失败", send.call_args[0][0])
            self.assertIn("RemoteDisconnected", send.call_args[0][1])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["last_error_alert_date"])
            self.assertEqual(saved["buy_emails_sent"], 1)

    def test_same_day_fetch_failure_does_not_email_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = stalled_state()
            state["last_error_alert_date"] = monitor.now_sh().strftime("%Y-%m-%d")
            state_path.write_text(json.dumps(state), encoding="utf-8")
            with mock.patch.object(monitor, "STATE_FILE", state_path), \
                    mock.patch.object(monitor, "fetch_kline", return_value=([], "RemoteDisconnected: closed")), \
                    mock.patch.object(monitor, "send_email", return_value=True) as send:
                with self.assertRaises(SystemExit):
                    monitor.main()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
