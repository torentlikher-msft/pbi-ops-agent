import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from eventhouse_monitor import EventhouseMonitor, MonitorResult, _rows_as_dicts, format_alert
from semantic_model_owners import SemanticModelOwnerResolver


class EventhouseMonitorTests(unittest.TestCase):
    def test_invalid_configuration_disables_monitor(self):
        monitor = EventhouseMonitor(None, "not-json", 30000, 5, 100)

        self.assertFalse(monitor.enabled)

    def test_parses_kusto_rows(self):
        payload = {
            "Tables": [
                {
                    "Columns": [
                        {"ColumnName": "Timestamp"},
                        {"ColumnName": "DurationMs"},
                    ],
                    "Rows": [["2026-08-17T12:00:00.0000000Z", 42000]],
                }
            ]
        }

        self.assertEqual(
            _rows_as_dicts(payload),
            [{"Timestamp": "2026-08-17T12:00:00.0000000Z", "DurationMs": 42000}],
        )

    def test_formats_one_aggregated_alert(self):
        result = MonitorResult(
            key="monitor-key",
            name="Production monitoring",
            user_ids=["user-id"],
            watermark="2026-08-17T12:01:00.0000000Z",
            events=[
                {
                    "Timestamp": "2026-08-17T12:00:00.0000000Z",
                    "DurationMs": 42000,
                    "ModelName": "Sales",
                    "ModelId": "model-id",
                    "QueryText": "EVALUATE Sales",
                    "User": "analyst@example.com",
                },
                {
                    "Timestamp": "2026-08-17T12:01:00.0000000Z",
                    "DurationMs": 31000,
                    "ModelName": "Finance",
                    "ModelId": "model-id-2",
                    "QueryText": "EVALUATE Finance",
                    "User": "",
                },
            ],
        )

        alert = format_alert(result)

        self.assertIn("2 query or queries", alert)
        self.assertIn("Sales", alert)
        self.assertIn("42.0s", alert)

    def test_loads_multiple_monitors(self):
        configuration = json.dumps(
            [
                {
                    "name": "One",
                    "queryUri": "https://one.kusto.fabric.microsoft.com",
                    "database": "Monitoring",
                    "userIds": ["user-one"],
                },
                {
                    "name": "Two",
                    "queryUri": "https://two.kusto.fabric.microsoft.com",
                    "database": "Monitoring",
                    "userIds": ["user-two"],
                },
            ]
        )

        monitor = EventhouseMonitor(None, configuration, 30000, 5, 100)

        self.assertTrue(monitor.enabled)

    def test_loads_id_based_owner_monitor(self):
        configuration = json.dumps(
            [
                {
                    "name": "Workspace monitoring",
                    "workspaceId": "workspace-id",
                    "kqlDatabaseId": "database-id",
                    "notifyModelOwner": True,
                }
            ]
        )

        monitor = EventhouseMonitor(None, configuration, 30000, 5, 100)

        self.assertTrue(monitor.enabled)
        query = monitor._build_query(datetime.now(timezone.utc), 30000)
        self.assertIn('OperationName == "QueryEnd"', query)


class SemanticModelOwnerResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_events_by_configured_owner(self):
        graph = AsyncMock()
        graph.get_user_id.return_value = "owner-object-id"
        resolver = SemanticModelOwnerResolver(None, graph)
        resolver._get_owner_upns = AsyncMock(
            return_value={"model-id": "owner@example.com"}
        )
        event = {"ModelId": "MODEL-ID", "DurationMs": 42000}

        grouped = await resolver.group_events_by_owner("workspace-id", [event])

        self.assertEqual(grouped, {"owner-object-id": [event]})
        graph.get_user_id.assert_awaited_once_with("owner@example.com")


if __name__ == "__main__":
    unittest.main()