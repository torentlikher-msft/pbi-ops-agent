import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from eventhouse_monitor import MonitorResult
from query_alerts import run_query_alert_poll


def _result(**overrides) -> MonitorResult:
    defaults = dict(
        key="monitor-key",
        name="Production monitoring",
        user_ids=["user-a"],
        watermark="2026-08-17T12:01:00.0000000Z",
        events=[
            {
                "Timestamp": "2026-08-17T12:00:00.0000000Z",
                "DurationMs": 42000,
                "ModelName": "Sales",
                "ModelId": "model-id",
                "ReportId": "report-id",
                "EventText": "EVALUATE Sales",
                "User": "analyst@example.com",
            }
        ],
        workspace_id=None,
        notify_model_owner=False,
    )
    defaults.update(overrides)
    return MonitorResult(**defaults)


def _store(last_alert=0):
    store = AsyncMock()
    store.get_last_query_alert.return_value = last_alert
    return store


class RunQueryAlertPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_greeting_with_pending_context(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [_result()]
        store = _store()
        send = AsyncMock()

        sent = await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        self.assertEqual(sent, 1)
        send.assert_awaited_once()
        user_id, greeting, pending = send.await_args.args
        self.assertEqual(user_id, "user-a")
        self.assertIn("Sales", greeting)
        self.assertIn("?", greeting)
        self.assertEqual(pending["model"], "Sales")
        self.assertEqual(pending["eventText"], "EVALUATE Sales")
        self.assertEqual(pending["durationMs"], 42000)
        self.assertIn("expiresAt", pending)
        store.set_last_query_alert.assert_awaited_once_with(
            "monitor-key", "user-a", unittest.mock.ANY
        )
        store.set_monitor_watermark.assert_awaited_once_with(
            "monitor-key", "2026-08-17T12:01:00.0000000Z"
        )

    async def test_offer_grounds_on_slowest_query(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [
            _result(
                events=[
                    {"Timestamp": "t1", "DurationMs": 31000, "ModelName": "Finance",
                     "ModelId": "m2", "EventText": "EVALUATE Finance"},
                    {"Timestamp": "t2", "DurationMs": 99000, "ModelName": "Sales",
                     "ModelId": "m1", "EventText": "EVALUATE Sales"},
                ]
            )
        ]
        store = _store()
        send = AsyncMock()

        await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        _user, greeting, pending = send.await_args.args
        self.assertEqual(pending["eventText"], "EVALUATE Sales")
        self.assertEqual(pending["durationMs"], 99000)
        self.assertEqual(pending["extraCount"], 1)
        self.assertIn("1 other slow", greeting)

    async def test_resolves_report_name_when_workspace_present(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [_result(workspace_id="ws-1")]
        store = _store()
        owner_resolver = AsyncMock()
        owner_resolver.get_report_name.return_value = "Executive Dashboard"
        send = AsyncMock()

        await run_query_alert_poll(monitor, store, owner_resolver, 900, send)

        owner_resolver.get_report_name.assert_awaited_once_with("ws-1", "report-id")
        _user, greeting, pending = send.await_args.args
        self.assertIn("Executive Dashboard", greeting)
        self.assertEqual(pending["report"], "Executive Dashboard")

    async def test_cooldown_suppresses_send_but_still_advances_watermark(self):
        import time

        monitor = AsyncMock()
        monitor.poll.return_value = [_result()]
        store = _store(last_alert=int(time.time()))
        send = AsyncMock()

        sent = await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        self.assertEqual(sent, 0)
        send.assert_not_awaited()
        store.set_last_query_alert.assert_not_awaited()
        store.set_monitor_watermark.assert_awaited_once()

    async def test_send_failure_keeps_watermark_for_retry(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [_result()]
        store = _store()
        send = AsyncMock(side_effect=RuntimeError("teams down"))

        sent = await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        self.assertEqual(sent, 0)
        store.set_last_query_alert.assert_not_awaited()
        store.set_monitor_watermark.assert_not_awaited()

    async def test_partial_send_failure_still_advances_watermark(self):
        # A persistently-unreachable recipient must not freeze the monitor
        # watermark and replay the same slow query to reachable recipients on
        # every poll (the "same message every 15 minutes" storm).
        monitor = AsyncMock()
        monitor.poll.return_value = [_result(user_ids=["good", "bad"])]
        store = _store()

        async def send(user_id, greeting, pending):
            if user_id == "bad":
                raise RuntimeError("teams down")

        sent = await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        self.assertEqual(sent, 1)
        store.set_last_query_alert.assert_awaited_once_with(
            "monitor-key", "good", unittest.mock.ANY
        )
        store.set_monitor_watermark.assert_awaited_once()

    async def test_notify_model_owner_resolves_recipients(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [
            _result(user_ids=[], workspace_id="ws-1", notify_model_owner=True)
        ]
        store = _store()
        owner_resolver = AsyncMock()
        owner_resolver.group_events_by_owner.return_value = {
            "owner-object-id": [
                {"Timestamp": "t", "DurationMs": 42000, "ModelName": "Sales",
                 "ModelId": "m1", "EventText": "EVALUATE Sales"}
            ]
        }
        owner_resolver.get_report_name.return_value = None
        send = AsyncMock()

        sent = await run_query_alert_poll(monitor, store, owner_resolver, 900, send)

        self.assertEqual(sent, 1)
        owner_resolver.group_events_by_owner.assert_awaited_once()
        self.assertEqual(send.await_args.args[0], "owner-object-id")

    async def test_no_recipients_advances_watermark_without_send(self):
        monitor = AsyncMock()
        monitor.poll.return_value = [_result(user_ids=[], notify_model_owner=False)]
        store = _store()
        send = AsyncMock()

        sent = await run_query_alert_poll(monitor, store, AsyncMock(), 900, send)

        self.assertEqual(sent, 0)
        send.assert_not_awaited()
        store.set_monitor_watermark.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
