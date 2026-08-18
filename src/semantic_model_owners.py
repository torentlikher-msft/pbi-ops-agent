"""Resolve semantic model owners through the Power BI REST API."""
import logging
import time
from collections import defaultdict
from typing import Optional

import aiohttp
from azure.identity.aio import ClientSecretCredential

from graph_client import GraphClient

log = logging.getLogger("semantic_model_owners")
POWER_BI = "https://api.powerbi.com/v1.0/myorg"


class SemanticModelOwnerResolver:
    def __init__(
        self, credential: ClientSecretCredential, graph: GraphClient, cache_seconds=900
    ):
        self._credential = credential
        self._graph = graph
        self._cache_seconds = cache_seconds
        self._cache = {}
        self._report_cache = {}

    async def group_events_by_owner(
        self, workspace_id: str, events: list[dict]
    ) -> dict[str, list[dict]]:
        owners = await self._get_owner_upns(workspace_id)
        grouped = defaultdict(list)
        resolved_users = {}
        for event in events:
            model_id = (event.get("ModelId") or "").lower()
            owner_upn = owners.get(model_id)
            if not owner_upn:
                log.warning("No configuredBy owner found for semantic model %s", model_id)
                continue
            if owner_upn not in resolved_users:
                resolved_users[owner_upn] = await self._graph.get_user_id(owner_upn)
            grouped[resolved_users[owner_upn]].append(event)
        return dict(grouped)

    async def _get_owner_upns(self, workspace_id: str) -> dict[str, str]:
        cached = self._cache.get(workspace_id)
        if cached and cached[0] > time.time():
            return cached[1]

        token = await self._credential.get_token(
            "https://analysis.windows.net/powerbi/api/.default"
        )
        url = f"{POWER_BI}/groups/{workspace_id}/datasets"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token.token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        owners = {
            item["id"].lower(): item["configuredBy"]
            for item in data.get("value", [])
            if item.get("id") and item.get("configuredBy")
        }
        self._cache[workspace_id] = (time.time() + self._cache_seconds, owners)
        return owners

    async def get_report_name(
        self, workspace_id: str, report_id: str
    ) -> Optional[str]:
        """Resolve a report id to its display name (best effort, cached)."""
        if not workspace_id or not report_id:
            return None
        reports = await self._get_report_names(workspace_id)
        return reports.get(report_id.lower())

    async def _get_report_names(self, workspace_id: str) -> dict[str, str]:
        cached = self._report_cache.get(workspace_id)
        if cached and cached[0] > time.time():
            return cached[1]

        token = await self._credential.get_token(
            "https://analysis.windows.net/powerbi/api/.default"
        )
        url = f"{POWER_BI}/groups/{workspace_id}/reports"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token.token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        reports = {
            item["id"].lower(): item["name"]
            for item in data.get("value", [])
            if item.get("id") and item.get("name")
        }
        self._report_cache[workspace_id] = (time.time() + self._cache_seconds, reports)
        return reports