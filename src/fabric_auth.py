"""Device-code OAuth for the user's Fabric token.

The Fabric Power BI MCP rejects delegated tokens from confidential clients
(``appidacr=1``). The Bot Framework OAuth service always uses a client secret,
so we instead run the OAuth **device code** flow ourselves as a *public* client,
which yields ``appidacr=0`` tokens that the MCP accepts. Tokens are refreshed
silently afterwards using the refresh token.
"""
import logging
import time

import aiohttp

log = logging.getLogger("fabric_auth")


class FabricAuth:
    def __init__(self, tenant_id: str, client_id: str, scopes: str):
        authority = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0"
        self._devicecode_url = f"{authority}/devicecode"
        self._token_url = f"{authority}/token"
        self._client_id = client_id
        self._scopes = scopes

    async def start_device_code(self) -> dict:
        """Begin a device-code sign-in. Returns Azure AD's device-code response
        (device_code, user_code, verification_uri, expires_in, interval, ...)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._devicecode_url,
                data={"client_id": self._client_id, "scope": self._scopes},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def poll(self, device_code: str) -> dict:
        """Poll for completion. Returns either a token payload (with
        access_token/refresh_token) or an ``error`` dict (authorization_pending,
        expired_token, ...)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self._client_id,
                    "device_code": device_code,
                },
            ) as resp:
                return await resp.json()

    async def refresh(self, refresh_token: str) -> dict:
        """Exchange a refresh token for a fresh access token (stays a public
        client, so ``appidacr=0`` is preserved). Returns {} on failure."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "refresh_token": refresh_token,
                    "scope": self._scopes,
                },
            ) as resp:
                data = await resp.json()
        if "access_token" not in data:
            log.info("refresh failed: %s", data.get("error"))
            return {}
        return data


def now() -> int:
    return int(time.time())
