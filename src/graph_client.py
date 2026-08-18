"""Microsoft Graph app-only helpers for cold-start proactive messaging.

When we have never seen a user before (no stored ConversationReference) we:
  1. Ensure the Teams app is installed for the user (personal scope).
  2. Read the resulting 1:1 chat id, which is the Bot Framework conversation id.

Requires the app registration to hold these *application* Graph permissions
(admin consent granted): TeamsAppInstallation.ReadWriteForUser.All,
AppCatalog.Read.All, Chat.ReadBasic.All.
"""
import logging
from urllib.parse import quote

import aiohttp

from config import app_credential

log = logging.getLogger("graph")
GRAPH = "https://graph.microsoft.com/v1.0"


class GraphClient:
    def __init__(self, teams_app_external_id: str):
        self._external_id = teams_app_external_id
        self._cred = app_credential()
        self._catalog_id = None

    async def _headers(self):
        token = await self._cred.get_token("https://graph.microsoft.com/.default")
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    async def get_chat_id_for_user(self, user_id: str) -> str:
        """Return the personal chat/conversation id for ``user_id`` (AAD object id)."""
        headers = await self._headers()
        async with aiohttp.ClientSession() as session:
            catalog_id = await self._catalog_app_id(session, headers)
            install_id = await self._ensure_installed(
                session, headers, user_id, catalog_id
            )
            return await self._chat_id(session, headers, user_id, install_id)

    async def get_user_id(self, user_principal_name: str) -> str:
        """Resolve a user principal name to its Entra object id."""
        headers = await self._headers()
        user = quote(user_principal_name, safe="")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{GRAPH}/users/{user}?$select=id", headers=headers
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data["id"]

    async def _catalog_app_id(self, session, headers) -> str:
        if self._catalog_id:
            return self._catalog_id
        url = (
            f"{GRAPH}/appCatalogs/teamsApps"
            f"?$filter=externalId eq '{self._external_id}'"
        )
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
        values = data.get("value", [])
        if not values:
            raise RuntimeError(
                f"Teams app with externalId '{self._external_id}' not found in the "
                "org app catalog. Upload/publish the app package first."
            )
        self._catalog_id = values[0]["id"]
        return self._catalog_id

    async def _ensure_installed(self, session, headers, user_id, catalog_id) -> str:
        existing = await self._find_install(session, headers, user_id)
        if existing:
            return existing

        body = {
            "teamsApp@odata.bind": f"{GRAPH}/appCatalogs/teamsApps/{catalog_id}"
        }
        url = f"{GRAPH}/users/{user_id}/teamwork/installedApps"
        async with session.post(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201, 409):  # 409 == already installed
                raise RuntimeError(
                    f"App install failed ({resp.status}): {await resp.text()}"
                )

        install_id = await self._find_install(session, headers, user_id)
        if not install_id:
            raise RuntimeError("App installed but installation id could not be read.")
        return install_id

    async def _find_install(self, session, headers, user_id):
        url = (
            f"{GRAPH}/users/{user_id}/teamwork/installedApps"
            f"?$expand=teamsApp&$filter=teamsApp/externalId eq '{self._external_id}'"
        )
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        values = data.get("value", [])
        return values[0]["id"] if values else None

    async def _chat_id(self, session, headers, user_id, install_id) -> str:
        url = (
            f"{GRAPH}/users/{user_id}/teamwork/installedApps/{install_id}/chat"
        )
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["id"]
