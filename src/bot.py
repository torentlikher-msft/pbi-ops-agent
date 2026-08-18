"""The Teams bot: obtains the signed-in user's Fabric token via a device-code
flow (public client), then relays messages to the Foundry agent using that
token so Power BI security is enforced per user.

The Fabric Power BI MCP rejects delegated tokens from confidential clients
(``appidacr=1``), which is what Bot Framework OAuth issues. A public-client
device-code token (``appidacr=0``) is accepted, so we run that flow ourselves.
"""
import logging

from botbuilder.core import TurnContext
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.schema import Activity, ActivityTypes

from fabric_auth import FabricAuth, now
from foundry_client import FoundryAgent
from proactive_offer import (
    grounded_prompt as _grounded_prompt,
    is_affirmative as _is_affirmative,
    is_negative as _is_negative,
    pending_expired as _pending_expired,
)
from state_store import StateStore

log = logging.getLogger("bot")


class PbiOpsBot(TeamsActivityHandler):
    def __init__(self, foundry: FoundryAgent, store: StateStore, auth: FabricAuth):
        self.foundry = foundry
        self.store = store
        self.auth = auth

    async def on_message_activity(self, turn_context: TurnContext):
        await self.remember(turn_context)
        await self.run(turn_context, (turn_context.activity.text or "").strip())

    async def on_conversation_update_activity(self, turn_context: TurnContext):
        await self.remember(turn_context)
        return await super().on_conversation_update_activity(turn_context)

    async def run(self, turn_context: TurnContext, text: str):
        aad = (
            turn_context.activity.from_property.aad_object_id
            if turn_context.activity.from_property
            else None
        )
        conversation_id = turn_context.activity.conversation.id
        lowered = text.lower()
        if lowered in ("logout", "signout", "sign out"):
            await self.store.clear_fabric_refresh(aad)
            await self.store.clear_prev_response(conversation_id)
            await turn_context.send_activity(
                "Signed out. Send a question to sign in again."
            )
            return

        if lowered in ("reset", "new chat", "new conversation", "clear"):
            await self.store.clear_prev_response(conversation_id)
            await turn_context.send_activity(
                "Started a fresh conversation. Send your question again."
            )
            return

        # Follow-up to a proactive slow-query offer ("Would you like some help?").
        grounded_text = await self._grounded_followup(turn_context, conversation_id, text)
        if grounded_text is False:
            return  # a reply (e.g. a polite decline) was already sent

        token = await self._acquire_token(turn_context, aad)
        if token is None:
            return  # a sign-in prompt was already sent (pending offer preserved)

        if grounded_text:
            # Start a clean thread so the analysis is grounded only in this query.
            await self.store.clear_pending_analysis(conversation_id)
            await self.store.clear_prev_response(conversation_id)
            text = grounded_text

        previous = await self.store.get_prev_response(conversation_id)
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))
        response_id, answer = await self.foundry.ask_as_user(text, token, previous)
        if response_id:
            await self.store.set_prev_response(conversation_id, response_id)
        await turn_context.send_activity(answer)

    async def _grounded_followup(self, turn_context, conversation_id, text):
        """Resolve a reply to a pending proactive offer.

        Returns the grounded analysis prompt (str) if the user accepted, ``False``
        if we already handled the turn (declined / nothing more to do), or ``None``
        if there is no pending offer and normal processing should continue.
        """
        pending = await self.store.get_pending_analysis(conversation_id)
        if not pending:
            return None
        if _pending_expired(pending):
            await self.store.clear_pending_analysis(conversation_id)
            return None
        intent = await self._classify_reply(text)
        if intent == "accept":
            # Keep pending until we have a token so an intervening sign-in doesn't
            # lose the offer; run() clears it right before the analysis runs.
            return _grounded_prompt(pending)
        if intent == "decline":
            await self.store.clear_pending_analysis(conversation_id)
            await turn_context.send_activity(
                "No problem — I'm here whenever you'd like to dig into that query."
            )
            return False
        # The user moved on to a different question; drop the offer and continue.
        await self.store.clear_pending_analysis(conversation_id)
        return None

    async def _classify_reply(self, text: str) -> str:
        """Classify a reply to the proactive offer as accept/decline/other.

        Uses the model for a probabilistic read of intent, falling back to a
        deterministic keyword heuristic only if the model call fails."""
        intent = await self.foundry.classify_reply(text)
        if intent == "error":
            if _is_affirmative(text):
                return "accept"
            if _is_negative(text):
                return "decline"
            return "other"
        return intent

    async def _acquire_token(self, turn_context: TurnContext, aad: str):
        """Return a usable Fabric access token, or None if the user must sign in
        (in which case a sign-in message has been sent)."""
        conversation_id = turn_context.activity.conversation.id

        # 1) Silent refresh if we have a refresh token.
        refresh = await self.store.get_fabric_refresh(aad)
        if refresh:
            result = await self.auth.refresh(refresh)
            if result.get("access_token"):
                if result.get("refresh_token"):
                    await self.store.set_fabric_refresh(aad, result["refresh_token"])
                return result["access_token"]

        # 2) Complete a pending device-code sign-in.
        pending = await self.store.get_pending_dc(conversation_id)
        if pending and (pending.get("expires_at") or 0) > now():
            result = await self.auth.poll(pending["device_code"])
            if result.get("access_token"):
                await self.store.clear_pending_dc(conversation_id)
                # Fresh sign-in: start a clean agent conversation so tool state
                # isn't inherited from any earlier tokenless response.
                await self.store.clear_prev_response(conversation_id)
                if result.get("refresh_token"):
                    await self.store.set_fabric_refresh(aad, result["refresh_token"])
                return result["access_token"]
            if result.get("error") == "authorization_pending":
                await turn_context.send_activity(
                    "Still waiting for you to finish signing in — complete it, then "
                    "send your question again."
                )
                return None
            # expired / declined -> fall through and start a fresh sign-in

        # 3) Start a new device-code sign-in.
        dc = await self.auth.start_device_code()
        await self.store.set_pending_dc(
            conversation_id, dc["device_code"], now() + int(dc.get("expires_in", 900))
        )
        await turn_context.send_activity(
            "**Sign in to Power BI to continue.**\n\n"
            f"1. Open {dc['verification_uri']}\n"
            f"2. Enter code **{dc['user_code']}** and sign in\n"
            "3. Then send your question again."
        )
        return None

    async def remember(self, turn_context: TurnContext):
        """Persist the ConversationReference so we can message the user later."""
        reference = TurnContext.get_conversation_reference(turn_context.activity)
        from_property = turn_context.activity.from_property
        aad_object_id = from_property.aad_object_id if from_property else None
        if aad_object_id:
            await self.store.save_ref(aad_object_id, reference)
