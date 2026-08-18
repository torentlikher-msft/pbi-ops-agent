"""Poll Fabric Monitoring Eventhouses for slow semantic model queries."""
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from azure.identity.aio import ClientSecretCredential

log = logging.getLogger("eventhouse_monitor")


@dataclass
class MonitorResult:
    key: str
    name: str
    user_ids: list[str]
    events: list[dict]
    watermark: str
    workspace_id: Optional[str] = None
    notify_model_owner: bool = False


class EventhouseMonitor:
    def __init__(
        self,
        credential: ClientSecretCredential,
        monitors_json: str,
        default_duration_ms: int,
        lookback_minutes: int,
        max_rows: int,
    ):
        self._credential = credential
        try:
            self._monitors = self._load_monitors(monitors_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            log.exception("Invalid EVENTHOUSE_MONITORS_JSON; polling is disabled")
            self._monitors = []
        self._default_duration_ms = default_duration_ms
        self._lookback_minutes = lookback_minutes
        self._max_rows = max_rows

    @property
    def enabled(self) -> bool:
        return bool(self._monitors)

    @staticmethod
    def _load_monitors(value: str) -> list[dict]:
        if not value.strip():
            return []
        monitors = json.loads(value)
        if not isinstance(monitors, list):
            raise ValueError("EVENTHOUSE_MONITORS_JSON must contain a JSON array")
        for monitor in monitors:
            if not monitor.get("name"):
                raise ValueError("Each Eventhouse monitor requires a name")
            has_connection = monitor.get("queryUri") and monitor.get("database")
            has_ids = monitor.get("workspaceId") and monitor.get("kqlDatabaseId")
            if not has_connection and not has_ids:
                raise ValueError(
                    "Each monitor requires queryUri/database or workspaceId/kqlDatabaseId"
                )
            has_static_users = isinstance(monitor.get("userIds"), list) and monitor[
                "userIds"
            ]
            if not has_static_users and not monitor.get("notifyModelOwner"):
                raise ValueError(
                    "Each monitor requires userIds or notifyModelOwner=true"
                )
            if monitor.get("notifyModelOwner") and not monitor.get("workspaceId"):
                raise ValueError("notifyModelOwner requires workspaceId")
        return monitors

    async def poll(self, state_store) -> list[MonitorResult]:
        token = await self._credential.get_token(
            "https://kusto.kusto.windows.net/.default"
        )
        results = []
        async with aiohttp.ClientSession() as session:
            for monitor in self._monitors:
                try:
                    result = await self._poll_one(session, token.token, monitor, state_store)
                    if result:
                        results.append(result)
                except Exception:
                    log.exception("Failed to poll Eventhouse monitor %s", monitor["name"])
        return results

    async def _poll_one(self, session, token, monitor, state_store) -> Optional[MonitorResult]:
        query_uri, database = await self._resolve_connection(session, monitor)
        key = hashlib.sha256(
            f'{query_uri}|{database}|{monitor["name"]}'.encode()
        ).hexdigest()[:32]
        saved_watermark = await state_store.get_monitor_watermark(key)
        fallback = datetime.now(timezone.utc) - timedelta(minutes=self._lookback_minutes)
        cutoff = _parse_timestamp(saved_watermark) or fallback
        duration_ms = int(monitor.get("durationMs", self._default_duration_ms))
        query = self._build_query(cutoff, duration_ms)
        payload = {"db": database, "csl": query}
        url = f'{query_uri.rstrip("/")}/v1/rest/query'
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as response:
            response.raise_for_status()
            data = await response.json()

        events = _rows_as_dicts(data)
        if not events:
            return None
        watermark = max(event["Timestamp"] for event in events)
        return MonitorResult(
            key=key,
            name=monitor["name"],
            user_ids=monitor.get("userIds", []),
            events=events,
            watermark=watermark,
            workspace_id=monitor.get("workspaceId"),
            notify_model_owner=bool(monitor.get("notifyModelOwner")),
        )

    async def _resolve_connection(self, session, monitor) -> tuple[str, str]:
        if monitor.get("queryUri") and monitor.get("database"):
            return monitor["queryUri"], monitor["database"]
        if monitor.get("_resolvedQueryUri"):
            return monitor["_resolvedQueryUri"], monitor["kqlDatabaseId"]

        token = await self._credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        )
        url = (
            "https://api.fabric.microsoft.com/v1/workspaces/"
            f'{monitor["workspaceId"]}/kqlDatabases/{monitor["kqlDatabaseId"]}'
        )
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token.token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            data = await response.json()
        query_uri = data.get("properties", {}).get("queryServiceUri")
        if not query_uri:
            raise RuntimeError("KQL database response did not include queryServiceUri")
        monitor["_resolvedQueryUri"] = query_uri
        return query_uri, monitor["kqlDatabaseId"]

    def _build_query(self, cutoff: datetime, duration_ms: int) -> str:
        cutoff_value = cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return f"""
SemanticModelLogs
| where Timestamp > datetime({cutoff_value})
| where OperationName == "QueryEnd"
| where DurationMs >= {duration_ms}
| project Timestamp, DurationMs, ModelId=ItemId, ModelName=ItemName,
          ReportId=tostring(ApplicationContext.Sources[0].ReportId),
          EventText=substring(EventText, 0, 8000), User=ExecutingUser
| order by Timestamp asc
| take {self._max_rows}
""".strip()


def format_greeting(
    model_name: str,
    report_name: Optional[str],
    duration_ms: int,
    extra_count: int = 0,
) -> str:
    """Casual, conversational opener that offers optimization help."""
    seconds = int(duration_ms) / 1000
    source = f"the **{report_name}** report and " if report_name else ""
    message = (
        f"Greetings! I noticed a sub-optimal query (took {seconds:.1f}s) originating from "
        f"{source}the **{model_name}** semantic model. "
        "Would you like some help troubleshooting and optimizing it?"
    )
    if extra_count > 0:
        noun = "query" if extra_count == 1 else "queries"
        verb = "was" if extra_count == 1 else "were"
        message += (
            f"\n\n(There {verb} also {extra_count} other slow {noun} in this window; "
            "we can start with the slowest.)"
        )
    return message


def format_alert(result: MonitorResult) -> str:
    events = sorted(result.events, key=lambda event: int(event["DurationMs"]), reverse=True)
    lines = [
        f"**Slow semantic model queries detected: {result.name}**",
        "",
        f"{len(events)} query or queries exceeded the configured duration threshold.",
        "",
    ]
    for event in events[:5]:
        duration_seconds = int(event["DurationMs"]) / 1000
        model = event.get("ModelName") or event.get("ModelId") or "Unknown model"
        user = f" by {event['User']}" if event.get("User") else ""
        lines.append(f"- **{model}**: {duration_seconds:.1f}s{user} at {event['Timestamp']}")
    if len(events) > 5:
        lines.append(f"- Plus {len(events) - 5} more slow queries in this polling window.")
    return "\n".join(lines)


def _rows_as_dicts(payload: dict) -> list[dict]:
    tables = payload.get("Tables") or []
    if not tables:
        return []
    columns = [column["ColumnName"] for column in tables[0].get("Columns", [])]
    return [dict(zip(columns, row)) for row in tables[0].get("Rows", [])]


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))