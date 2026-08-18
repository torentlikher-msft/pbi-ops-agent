"""Persistent state stored in Azure Table Storage.

Tables:
  * ``convrefs``  – Bot Framework ConversationReference keyed by user AAD object id.
  * ``respmap``   – last Foundry response id keyed by Teams conversation id
                    (used as ``previous_response_id`` for follow-ups).
    * ``querymonitor`` – Eventhouse poll watermarks and per-user alert cooldowns.
"""
import json
from typing import Optional

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.data.tables.aio import TableServiceClient
from botbuilder.schema import ConversationReference

from config import app_credential

_CONVREFS = "convrefs"
_RESPMAP = "respmap"
_FABRICTOK = "fabrictokens"
_PENDINGDC = "pendingdc"
_QUERYMONITOR = "querymonitor"
_PENDINGANALYSIS = "pendinganalysis"


class StateStore:
    def __init__(self, account_name: str):
        self._endpoint = f"https://{account_name}.table.core.windows.net"
        self._credential = app_credential()
        self._svc: Optional[TableServiceClient] = None

    async def _service(self) -> TableServiceClient:
        if self._svc is None:
            self._svc = TableServiceClient(
                endpoint=self._endpoint, credential=self._credential
            )
            await self._svc.create_table_if_not_exists(_CONVREFS)
            await self._svc.create_table_if_not_exists(_RESPMAP)
            await self._svc.create_table_if_not_exists(_FABRICTOK)
            await self._svc.create_table_if_not_exists(_PENDINGDC)
            await self._svc.create_table_if_not_exists(_QUERYMONITOR)
            await self._svc.create_table_if_not_exists(_PENDINGANALYSIS)
        return self._svc

    # ---------------------------------------------------- pending analysis
    async def set_pending_analysis(
        self, conversation_id: str, payload: dict
    ) -> None:
        """Persist the offending-query context offered proactively to a user so a
        later 'yes' can ground the analysis in that specific query."""
        if not conversation_id:
            return
        svc = await self._service()
        table = svc.get_table_client(_PENDINGANALYSIS)
        await table.upsert_entity(
            {
                "PartitionKey": "c",
                "RowKey": conversation_id,
                "payload": json.dumps(payload),
            },
            mode=UpdateMode.REPLACE,
        )

    async def get_pending_analysis(self, conversation_id: str) -> Optional[dict]:
        svc = await self._service()
        table = svc.get_table_client(_PENDINGANALYSIS)
        try:
            entity = await table.get_entity("c", conversation_id)
        except ResourceNotFoundError:
            return None
        try:
            return json.loads(entity["payload"])
        except (KeyError, ValueError):
            return None

    async def clear_pending_analysis(self, conversation_id: str) -> None:
        svc = await self._service()
        table = svc.get_table_client(_PENDINGANALYSIS)
        try:
            await table.delete_entity("c", conversation_id)
        except ResourceNotFoundError:
            pass

    # -------------------------------------------------------- query monitor
    async def get_monitor_watermark(self, monitor_key: str) -> Optional[str]:
        svc = await self._service()
        table = svc.get_table_client(_QUERYMONITOR)
        try:
            entity = await table.get_entity("watermark", monitor_key)
        except ResourceNotFoundError:
            return None
        return entity.get("timestamp")

    async def set_monitor_watermark(self, monitor_key: str, timestamp: str) -> None:
        svc = await self._service()
        table = svc.get_table_client(_QUERYMONITOR)
        await table.upsert_entity(
            {
                "PartitionKey": "watermark",
                "RowKey": monitor_key,
                "timestamp": timestamp,
            },
            mode=UpdateMode.REPLACE,
        )

    async def get_last_query_alert(self, monitor_key: str, user_id: str) -> int:
        svc = await self._service()
        table = svc.get_table_client(_QUERYMONITOR)
        row_key = f"{monitor_key}-{user_id}"
        try:
            entity = await table.get_entity("alert", row_key)
        except ResourceNotFoundError:
            return 0
        return int(entity.get("sent_at", 0))

    async def set_last_query_alert(
        self, monitor_key: str, user_id: str, sent_at: int
    ) -> None:
        svc = await self._service()
        table = svc.get_table_client(_QUERYMONITOR)
        await table.upsert_entity(
            {
                "PartitionKey": "alert",
                "RowKey": f"{monitor_key}-{user_id}",
                "sent_at": sent_at,
            },
            mode=UpdateMode.REPLACE,
        )

    # -------------------------------------------------------- fabric tokens
    async def get_fabric_refresh(self, aad_object_id: str) -> Optional[str]:
        if not aad_object_id:
            return None
        svc = await self._service()
        table = svc.get_table_client(_FABRICTOK)
        try:
            entity = await table.get_entity("u", aad_object_id)
        except ResourceNotFoundError:
            return None
        return entity.get("refresh")

    async def set_fabric_refresh(self, aad_object_id: str, refresh_token: str) -> None:
        if not aad_object_id:
            return
        svc = await self._service()
        table = svc.get_table_client(_FABRICTOK)
        await table.upsert_entity(
            {"PartitionKey": "u", "RowKey": aad_object_id, "refresh": refresh_token},
            mode=UpdateMode.REPLACE,
        )

    async def clear_fabric_refresh(self, aad_object_id: str) -> None:
        if not aad_object_id:
            return
        svc = await self._service()
        table = svc.get_table_client(_FABRICTOK)
        try:
            await table.delete_entity("u", aad_object_id)
        except ResourceNotFoundError:
            pass

    # ----------------------------------------------------- pending devicecode
    async def get_pending_dc(self, conversation_id: str) -> Optional[dict]:
        svc = await self._service()
        table = svc.get_table_client(_PENDINGDC)
        try:
            entity = await table.get_entity("c", conversation_id)
        except ResourceNotFoundError:
            return None
        return {
            "device_code": entity.get("device_code"),
            "expires_at": entity.get("expires_at"),
        }

    async def set_pending_dc(
        self, conversation_id: str, device_code: str, expires_at: int
    ) -> None:
        svc = await self._service()
        table = svc.get_table_client(_PENDINGDC)
        await table.upsert_entity(
            {
                "PartitionKey": "c",
                "RowKey": conversation_id,
                "device_code": device_code,
                "expires_at": expires_at,
            },
            mode=UpdateMode.REPLACE,
        )

    async def clear_pending_dc(self, conversation_id: str) -> None:
        svc = await self._service()
        table = svc.get_table_client(_PENDINGDC)
        try:
            await table.delete_entity("c", conversation_id)
        except ResourceNotFoundError:
            pass

    # ------------------------------------------------------------------ refs
    async def save_ref(self, aad_object_id: str, ref: ConversationReference) -> None:
        if not aad_object_id:
            return
        svc = await self._service()
        table = svc.get_table_client(_CONVREFS)
        await table.upsert_entity(
            {
                "PartitionKey": "u",
                "RowKey": aad_object_id,
                "ref": json.dumps(ref.serialize()),
            },
            mode=UpdateMode.REPLACE,
        )

    async def get_ref(self, aad_object_id: str) -> Optional[ConversationReference]:
        svc = await self._service()
        table = svc.get_table_client(_CONVREFS)
        try:
            entity = await table.get_entity("u", aad_object_id)
        except ResourceNotFoundError:
            return None
        return ConversationReference().deserialize(json.loads(entity["ref"]))

    # --------------------------------------------------------------- threads
    async def get_prev_response(self, conversation_id: str) -> Optional[str]:
        svc = await self._service()
        table = svc.get_table_client(_RESPMAP)
        try:
            entity = await table.get_entity("c", conversation_id)
        except ResourceNotFoundError:
            return None
        return entity.get("response_id")

    async def set_prev_response(self, conversation_id: str, response_id: str) -> None:
        svc = await self._service()
        table = svc.get_table_client(_RESPMAP)
        await table.upsert_entity(
            {"PartitionKey": "c", "RowKey": conversation_id, "response_id": response_id},
            mode=UpdateMode.REPLACE,
        )

    async def clear_prev_response(self, conversation_id: str) -> None:
        svc = await self._service()
        table = svc.get_table_client(_RESPMAP)
        try:
            await table.delete_entity("c", conversation_id)
        except ResourceNotFoundError:
            pass
