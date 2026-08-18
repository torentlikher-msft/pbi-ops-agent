"""Async client for the Foundry 'prompt' agent via the OpenAI Responses API.

Runs the agent on behalf of the signed-in user by replicating its
model/instructions inline and injecting the user's Fabric token into the Power BI
MCP tool (so Power BI security is enforced per user).

It also expands the project **toolbox** (preview): the toolbox's MCP tool is used
per-user, and its **skills** (SKILL.md packages) are exposed through a
``read_skill`` function tool so the agent can load skill instructions on demand
(progressive disclosure). If the toolbox/skills preview API is unavailable it
falls back to the agent's inline tools.
"""
import io
import json
import logging
import zipfile
from typing import Optional, Tuple

import aiohttp

from config import app_credential

log = logging.getLogger("foundry")

_SKILLS_FEATURE = "Skills=V1Preview"


def _token_claims(token: str) -> dict:
    import base64

    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _extract_text(data: dict) -> str:
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _consent_links(data: dict) -> list:
    links = []
    for item in data.get("output", []):
        if item.get("type") == "oauth_consent_request" and item.get("consent_link"):
            links.append(item["consent_link"])
    return links


def _error_message(detail: str) -> Optional[str]:
    try:
        return json.loads(detail).get("error", {}).get("message")
    except Exception:
        return None


class FoundryAgent:
    def __init__(
        self,
        project_endpoint: str,
        agent_name: str,
        token_scope: str,
        toolbox_name: str = "toolbox",
    ):
        base = project_endpoint.rstrip("/")
        self._base = base
        self._url = base + "/openai/v1/responses"
        self._agent_url = base + f"/agents/{agent_name}?api-version=2025-05-15-preview"
        self._agent_name = agent_name
        self._toolbox_name = toolbox_name
        self._scope = token_scope
        # Authenticate as the bot app registration (service principal).
        self._credential = app_credential()
        self._agent_def = None
        self._toolbox = None  # {"mcp_tools": [...], "skills": [{name, description}]}
        self._skill_files = {}  # skill name -> {path: text}

    async def _token(self) -> str:
        token = await self._credential.get_token(self._scope)
        return token.token

    def _headers(self, token: str, skills: bool = False) -> dict:
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if skills:
            h["Foundry-Features"] = _SKILLS_FEATURE
        return h

    async def _get_agent_def(self, headers: dict) -> dict:
        """Fetch and cache the published agent's model/instructions/tools so the
        bot stays in sync with whatever is configured in Foundry."""
        if self._agent_def:
            return self._agent_def
        async with aiohttp.ClientSession() as session:
            async with session.get(self._agent_url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        definition = data["versions"]["latest"]["definition"]
        tools = []
        for tool in definition.get("tools", []):
            if tool.get("type") == "mcp":
                tools.append(
                    {
                        "type": "mcp",
                        "server_label": tool["server_label"],
                        "server_url": tool["server_url"],
                        "require_approval": "never",
                    }
                )
        self._agent_def = {
            "model": definition["model"],
            "instructions": definition.get("instructions", ""),
            "reasoning": definition.get("reasoning"),
            "tools": tools,
        }
        return self._agent_def

    async def _get_toolbox(self, token: str) -> Optional[dict]:
        """Fetch the project toolbox (preview): MCP tools + skill list. Returns
        None if unavailable (caller falls back to the agent's inline tools)."""
        if self._toolbox is not None:
            return self._toolbox or None
        headers = self._headers(token, skills=True)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base}/toolboxes/{self._toolbox_name}?api-version=v1",
                    headers=headers,
                ) as r:
                    if r.status != 200:
                        self._toolbox = {}
                        return None
                    version = (await r.json()).get("default_version", "1")
                async with session.get(
                    f"{self._base}/toolboxes/{self._toolbox_name}/versions/{version}"
                    "?api-version=v1",
                    headers=headers,
                ) as r:
                    if r.status != 200:
                        self._toolbox = {}
                        return None
                    tb = await r.json()
                mcp_tools = [
                    {
                        "type": "mcp",
                        "server_label": t["server_label"],
                        "server_url": t["server_url"],
                        "require_approval": "never",
                    }
                    for t in tb.get("tools", [])
                    if t.get("type") == "mcp"
                ]
                skills = []
                for s in tb.get("skills", []):
                    name = s.get("name")
                    if not name:
                        continue
                    desc = ""
                    async with session.get(
                        f"{self._base}/skills/{name}?api-version=v1", headers=headers
                    ) as sr:
                        if sr.status == 200:
                            desc = (await sr.json()).get("description", "")
                    skills.append({"name": name, "description": desc})
            self._toolbox = {"mcp_tools": mcp_tools, "skills": skills}
            log.info(
                "toolbox loaded mcp=%s skills=%s",
                len(mcp_tools),
                [s["name"] for s in skills],
            )
            return self._toolbox
        except Exception as exc:
            log.warning("toolbox unavailable, using agent inline tools: %s", exc)
            self._toolbox = {}
            return None

    async def _read_skill_file(self, token: str, name: str, path: str) -> str:
        """Return the text of a file inside a skill package (cached)."""
        path = path or "SKILL.md"
        cached = self._skill_files.get(name)
        if cached is None:
            headers = self._headers(token, skills=True)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self._base}/skills/{name}/content?api-version=v1",
                        headers=headers,
                    ) as r:
                        if r.status != 200:
                            return f"(could not load skill '{name}': HTTP {r.status})"
                        raw = await r.read()
                files = {}
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    for entry in zf.namelist():
                        if entry.endswith("/"):
                            continue
                        try:
                            files[entry] = zf.read(entry).decode("utf-8", "replace")
                        except Exception:
                            pass
                self._skill_files[name] = files
                cached = files
            except Exception as exc:
                return f"(error loading skill '{name}': {exc})"
        if path in cached:
            return cached[path]
        for key in cached:
            if key.lstrip("./") == path.lstrip("./"):
                return cached[key]
        return (
            f"(file '{path}' not found in skill '{name}'. "
            f"Available: {', '.join(cached.keys())})"
        )

    async def _post(self, headers: dict, body: dict):
        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    return resp.status, {}, await resp.text()
                return resp.status, await resp.json(), ""

    async def _find_semantic_model(
        self, fabric_token: str, workspace: str, model: str
    ) -> str:
        """Resolve workspace/model names to GUIDs via the Fabric REST API using the
        user's token (per-user access). Returns a JSON string for the agent."""
        headers = {"Authorization": f"Bearer {fabric_token}"}
        api = "https://api.fabric.microsoft.com/v1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{api}/workspaces", headers=headers) as r:
                    if r.status != 200:
                        return json.dumps(
                            {"error": f"could not list workspaces ({r.status})"}
                        )
                    workspaces = (await r.json()).get("value", [])
                if workspace:
                    wl = workspace.lower()
                    ws_matches = [
                        w
                        for w in workspaces
                        if wl in (w.get("displayName", "").lower())
                    ]
                    if not ws_matches:
                        return json.dumps(
                            {
                                "found": False,
                                "message": f"No workspace matching '{workspace}' "
                                "that you can access.",
                            }
                        )
                else:
                    ws_matches = workspaces
                results = []
                for w in ws_matches[:50]:
                    async with session.get(
                        f"{api}/workspaces/{w['id']}/items?type=SemanticModel",
                        headers=headers,
                    ) as r:
                        if r.status != 200:
                            continue
                        items = (await r.json()).get("value", [])
                    for it in items:
                        if not model or model.lower() in it.get(
                            "displayName", ""
                        ).lower():
                            results.append(
                                {
                                    "workspace": w.get("displayName"),
                                    "workspaceId": w.get("id"),
                                    "model": it.get("displayName"),
                                    "artifactId": it.get("id"),
                                }
                            )
        except Exception as exc:
            return json.dumps({"error": f"lookup failed: {exc}"})
        if not results:
            return json.dumps(
                {
                    "found": False,
                    "message": "No matching semantic model you can access. "
                    "Check the name and your permissions.",
                }
            )
        return json.dumps({"found": True, "matches": results[:20]})

    async def ask_as_user(
        self,
        user_text: str,
        fabric_token: str,
        previous_response_id: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """Run the agent on behalf of the signed-in user, using the project
        toolbox (per-user MCP + skills). Returns ``(response_id, answer_text)``."""
        token = await self._token()
        headers = self._headers(token)
        agent_def = await self._get_agent_def(headers)
        toolbox = await self._get_toolbox(token)

        claims = _token_claims(fabric_token)
        log.info(
            "ask_as_user user-token aud=%s appidacr=%s scp=%s",
            claims.get("aud"),
            claims.get("appidacr"),
            claims.get("scp"),
        )

        mcp_source = toolbox["mcp_tools"] if toolbox else agent_def["tools"]
        skills = toolbox["skills"] if toolbox else []

        tools = []
        for tool in mcp_source:
            enriched = dict(tool)
            enriched["authorization"] = fabric_token
            tools.append(enriched)

        instructions = agent_def["instructions"]
        if skills:
            catalog = "\n".join(f"- **{s['name']}**: {s['description']}" for s in skills)
            instructions += (
                "\n\n# Available skills\n"
                "You can load these skills for detailed, authoritative instructions. "
                'When a request matches a skill\'s triggers, call the `read_skill` '
                'function with the skill `name` and `path` "SKILL.md" to load it, then '
                "follow it. A SKILL.md may reference files under `references/`; load "
                "those with `read_skill` as needed. Prefer skill instructions over your "
                "own assumptions.\n\n" + catalog
            )
            tools.append(
                {
                    "type": "function",
                    "name": "read_skill",
                    "description": (
                        "Load a skill file (SKILL.md or a references/ file) to get "
                        "detailed instructions for a task."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Skill name, e.g. semantic-model-consumption",
                            },
                            "path": {
                                "type": "string",
                                "description": (
                                    "File within the skill, e.g. SKILL.md or "
                                    "references/discovery-queries.md"
                                ),
                            },
                        },
                        "required": ["name", "path"],
                    },
                }
            )

        # Always give the agent a way to resolve model/workspace names to GUIDs
        # (the MCP tools require an artifactId). Fulfilled via the Fabric REST API
        # with the user's own token, so per-user access is enforced.
        instructions += (
            "\n\n# Resolving names to IDs\n"
            "The Power BI tools require an `artifactId` (GUID). When the user names a "
            "semantic model or workspace, FIRST call `find_semantic_model` with the "
            "workspace and/or model name to get the exact `artifactId`, then use that "
            "GUID with the MCP tools. Never pass a name where a GUID is required."
        )
        tools.append(
            {
                "type": "function",
                "name": "find_semantic_model",
                "description": (
                    "Resolve a Power BI semantic model and/or workspace name to its "
                    "GUID(s). Returns matching models with their artifactId."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace": {
                            "type": "string",
                            "description": "Workspace name (optional)",
                        },
                        "model": {
                            "type": "string",
                            "description": "Semantic model name (optional)",
                        },
                    },
                    "required": [],
                },
            }
        )

        body = {
            "model": agent_def["model"],
            "instructions": instructions,
            "input": user_text,
            "tools": tools,
            "store": True,
        }
        if agent_def.get("reasoning"):
            body["reasoning"] = agent_def["reasoning"]
        if previous_response_id:
            body["previous_response_id"] = previous_response_id

        log.info(
            "ask_as_user request model=%s mcp=%s skills=%s",
            agent_def.get("model"),
            len([t for t in tools if t.get("type") == "mcp"]),
            [s["name"] for s in skills],
        )

        last_id = previous_response_id
        for _ in range(8):
            status, data, detail = await self._post(headers, body)
            if status != 200:
                log.error("Foundry responses error %s: %s", status, detail)
                return last_id, (
                    _error_message(detail)
                    or f"Sorry \u2014 the agent call failed ({status})."
                )
            last_id = data.get("id")
            calls = [
                it
                for it in data.get("output", [])
                if it.get("type") == "function_call"
                and it.get("name") in ("read_skill", "find_semantic_model")
            ]
            tool_counts = [
                len(i.get("tools", []))
                for i in data.get("output", [])
                if i.get("type") == "mcp_list_tools"
            ]
            log.info(
                "ask_as_user iter status=%s toolLists=%s skillCalls=%s outTypes=%s",
                data.get("status"),
                tool_counts,
                len(calls),
                [i.get("type") for i in data.get("output", [])],
            )
            if not calls:
                return last_id, (_extract_text(data) or "(the agent returned no text)")
            outputs = []
            for fc in calls:
                try:
                    args = json.loads(fc.get("arguments") or "{}")
                except Exception:
                    args = {}
                if fc.get("name") == "find_semantic_model":
                    content = await self._find_semantic_model(
                        fabric_token, args.get("workspace", ""), args.get("model", "")
                    )
                else:
                    content = await self._read_skill_file(
                        token, args.get("name", ""), args.get("path", "SKILL.md")
                    )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": fc.get("call_id"),
                        "output": content,
                    }
                )
            body = {
                "model": agent_def["model"],
                "previous_response_id": last_id,
                "input": outputs,
                "tools": tools,
                "store": True,
            }

        return last_id, "(the agent kept loading skills; stopped after several rounds)"

    async def classify_reply(self, user_text: str) -> str:
        """Classify a reply to a proactive help offer using the model (no tools).

        Returns one of ``"accept"``, ``"decline"`` or ``"other"``. This is a cheap,
        tool-free model call so intent detection is probabilistic rather than a fixed
        keyword list. Returns ``"error"`` if the call fails, so the caller can fall
        back to a deterministic heuristic.
        """
        try:
            token = await self._token()
            headers = self._headers(token)
            agent_def = await self._get_agent_def(headers)
            instructions = (
                "You are an intent classifier. The assistant just offered to help "
                "troubleshoot and optimize a slow Power BI query and asked the user "
                "if they want help. Classify the user's reply into exactly one label:\n"
                "- accept: the user wants the help (agreement/affirmation, however "
                "phrased, including enthusiasm or 'go for it').\n"
                "- decline: the user does not want help right now.\n"
                "- other: the reply is unrelated, ambiguous, or is itself a new "
                "question or request.\n"
                "Respond with only the single lowercase label and nothing else."
            )
            body = {
                "model": agent_def["model"],
                "instructions": instructions,
                "input": user_text,
                "store": False,
            }
            status, data, detail = await self._post(headers, body)
            if status != 200:
                log.warning("classify_reply failed %s: %s", status, detail)
                return "error"
            text = _extract_text(data).strip().lower()
            for label in ("accept", "decline", "other"):
                if label in text:
                    return label
            return "other"
        except Exception:
            log.exception("classify_reply raised")
            return "error"

    async def ask(
        self, user_text: str, previous_response_id: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        """Run the agent. Returns ``(response_id, answer_text)``.

        Pass ``previous_response_id`` to continue an existing conversation.
        """
        body = {
            "agent_reference": {"type": "agent_reference", "name": self._agent_name},
            "input": user_text,
            "store": True,
        }
        if previous_response_id:
            body["previous_response_id"] = previous_response_id

        headers = {
            "Authorization": f"Bearer {await self._token()}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    log.error("Foundry responses error %s: %s", resp.status, detail)
                    return previous_response_id, (
                        f"Sorry — the agent call failed ({resp.status})."
                    )
                data = await resp.json()

        response_id = data.get("id")
        text = _extract_text(data)
        if text:
            return response_id, text

        # The Power BI MCP tool uses a delegated (OAuth2) connection. When its
        # token is missing/expired, the agent returns a consent request instead
        # of an answer. Surface the sign-in link so the user can authorize.
        consent = _consent_links(data)
        if consent:
            links = "\n".join(consent)
            return response_id, (
                "Before I can use the Power BI tools I need you to authorize the "
                "connection. Please sign in here, then send your message again:\n"
                f"{links}"
            )
        return response_id, "(the agent returned no text)"
