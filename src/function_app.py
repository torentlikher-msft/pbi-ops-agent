"""Azure Functions (Python v2) host.

Endpoints:
  * POST /api/messages  – Bot Framework messaging endpoint (Teams -> bot).
  * POST /api/notify    – Operations trigger. Proactively messages a user and
                          runs the Foundry agent. Body:
                          { "userId": "<AAD object id>",
                            "message": "prompt for the agent",
                            "sendRaw": false }
"""
import json
import logging

import azure.functions as func
from botbuilder.core import TurnContext
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
    ConversationReference,
)

from bot import PbiOpsBot
from config import Config
from config import app_credential
from eventhouse_monitor import EventhouseMonitor
from fabric_auth import FabricAuth
from foundry_client import FoundryAgent
from graph_client import GraphClient
from query_alerts import run_query_alert_poll as _run_query_alert_poll
from semantic_model_owners import SemanticModelOwnerResolver
from state_store import StateStore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("host")

CONFIG = Config()
STORE = StateStore(CONFIG.STORAGE_ACCOUNT_NAME)
FOUNDRY = FoundryAgent(
    CONFIG.FOUNDRY_PROJECT_ENDPOINT,
    CONFIG.FOUNDRY_AGENT_NAME,
    CONFIG.FOUNDRY_TOKEN_SCOPE,
    CONFIG.TOOLBOX_NAME,
)
GRAPH = GraphClient(CONFIG.TEAMS_APP_EXTERNAL_ID)
FABRIC_AUTH = FabricAuth(CONFIG.APP_TENANTID, CONFIG.APP_ID, CONFIG.FABRIC_SCOPES)
BOT = PbiOpsBot(FOUNDRY, STORE, FABRIC_AUTH)
OWNER_RESOLVER = SemanticModelOwnerResolver(app_credential(), GRAPH)
EVENTHOUSE_MONITOR = EventhouseMonitor(
    app_credential(),
    CONFIG.EVENTHOUSE_MONITORS_JSON,
    CONFIG.QUERY_ALERT_DURATION_MS,
    CONFIG.QUERY_ALERT_LOOKBACK_MINUTES,
    CONFIG.QUERY_ALERT_MAX_ROWS,
)

ADAPTER = CloudAdapter(ConfigurationBotFrameworkAuthentication(CONFIG))


async def _on_error(context: TurnContext, error: Exception):
    log.exception("Bot turn error: %s", error)
    try:
        await context.send_activity("The agent hit an unexpected error.")
    except Exception:  # pragma: no cover - best effort
        pass


ADAPTER.on_turn_error = _on_error

app = func.FunctionApp()


@app.route(route="messages", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def messages(req: func.HttpRequest) -> func.HttpResponse:
    if "application/json" not in req.headers.get("Content-Type", ""):
        return func.HttpResponse(status_code=415)

    activity = Activity().deserialize(json.loads(req.get_body().decode("utf-8")))
    auth_header = req.headers.get("Authorization", "")
    try:
        response = await ADAPTER.process_activity(auth_header, activity, BOT.on_turn)
    except PermissionError:
        return func.HttpResponse("Unauthorized", status_code=401)
    if response:
        return func.HttpResponse(
            json.dumps(response.body),
            status_code=response.status,
            mimetype="application/json",
        )
    return func.HttpResponse(status_code=201)


@app.route(route="notify", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
async def notify(req: func.HttpRequest) -> func.HttpResponse:
    # Defense in depth: Function key (auth_level) + a shared API key header.
    if CONFIG.NOTIFY_API_KEY and req.headers.get("x-api-key") != CONFIG.NOTIFY_API_KEY:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    user_id = body.get("userId")
    message = body.get("message")
    send_raw = bool(body.get("sendRaw", False))
    if not user_id or not message:
        return func.HttpResponse(
            "Body requires 'userId' (AAD object id) and 'message'.", status_code=400
        )

    try:
        await _send_proactive(user_id, message, send_raw)
    except Exception as exc:  # surface Graph/install problems to the caller
        log.exception("Proactive notification failed")
        return func.HttpResponse(
            f"Could not start a conversation with the user: {exc}",
            status_code=502,
        )
    return func.HttpResponse(
        json.dumps({"status": "sent", "userId": user_id}),
        status_code=202,
        mimetype="application/json",
    )


@app.timer_trigger(
    schedule=CONFIG.QUERY_ALERT_POLL_SCHEDULE,
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
async def poll_semantic_model_logs(timer: func.TimerRequest) -> None:
    if not EVENTHOUSE_MONITOR.enabled:
        return
    if timer.past_due:
        log.warning("Semantic model log poll is running late")
    await run_query_alert_poll()


@app.route(route="poll-now", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
async def poll_now(req: func.HttpRequest) -> func.HttpResponse:
    """Manually trigger a semantic-model-log poll (for testing/on-demand runs).

    Secured the same way as /api/notify: Function key + optional shared header.
    """
    if CONFIG.NOTIFY_API_KEY and req.headers.get("x-api-key") != CONFIG.NOTIFY_API_KEY:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not EVENTHOUSE_MONITOR.enabled:
        return func.HttpResponse(
            json.dumps({"status": "disabled", "reason": "no monitors configured"}),
            status_code=200,
            mimetype="application/json",
        )
    try:
        alerts_sent = await run_query_alert_poll()
    except Exception as exc:
        log.exception("Manual query alert poll failed")
        return func.HttpResponse(f"Poll failed: {exc}", status_code=502)
    return func.HttpResponse(
        json.dumps({"status": "ok", "alertsSent": alerts_sent}),
        status_code=200,
        mimetype="application/json",
    )


async def run_query_alert_poll() -> int:
    """Poll all monitors and send alerts using the module-level singletons."""
    return await _run_query_alert_poll(
        EVENTHOUSE_MONITOR,
        STORE,
        OWNER_RESOLVER,
        CONFIG.QUERY_ALERT_COOLDOWN_SECONDS,
        _send_alert,
    )


async def _send_alert(user_id: str, greeting: str, pending: dict) -> None:
    """Deliver a proactive slow-query greeting and persist the offending-query
    context so a later 'yes' grounds the analysis in that specific query."""
    reference = await STORE.get_ref(user_id)
    if reference is None:
        reference = await _cold_start_reference(user_id)
    await STORE.set_pending_analysis(reference.conversation.id, pending)

    async def callback(turn_context: TurnContext):
        await BOT.remember(turn_context)
        await turn_context.send_activity(greeting)

    await ADAPTER.continue_conversation(reference, callback, CONFIG.APP_ID)


async def _send_proactive(user_id: str, message: str, send_raw: bool) -> None:
    reference = await STORE.get_ref(user_id)
    if reference is None:
        reference = await _cold_start_reference(user_id)

    async def callback(turn_context: TurnContext):
        await BOT.remember(turn_context)
        if send_raw:
            await turn_context.send_activity(message)
        else:
            await BOT.run(turn_context, message)

    await ADAPTER.continue_conversation(reference, callback, CONFIG.APP_ID)


async def _cold_start_reference(user_id: str) -> ConversationReference:
    """Build a ConversationReference for a user we've never seen, via Graph."""
    chat_id = await GRAPH.get_chat_id_for_user(user_id)
    reference = ConversationReference(
        channel_id="msteams",
        service_url=CONFIG.SERVICE_URL_DEFAULT,
        bot=ChannelAccount(id=f"28:{CONFIG.APP_ID}"),
        conversation=ConversationAccount(
            id=chat_id,
            tenant_id=CONFIG.APP_TENANTID,
            conversation_type="personal",
            is_group=False,
        ),
        user=ChannelAccount(aad_object_id=user_id),
    )
    await STORE.save_ref(user_id, reference)
    return reference
