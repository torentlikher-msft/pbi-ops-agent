"""Orchestration for Eventhouse slow-query alerts.

Kept free of Azure Functions / Bot Framework imports so the polling logic can be
unit tested with lightweight fakes. ``function_app`` wires the real singletons in.

Each qualifying poll sends a casual, conversational opener that offers help. The
offending query (the slowest in the window) is persisted as "pending analysis"
context so that if the user says yes, the bot can ground its analysis in that
specific query instead of scanning the whole semantic model.
"""
import logging
import time

from eventhouse_monitor import format_greeting

log = logging.getLogger("query_alerts")

_PENDING_TTL_SECONDS = 24 * 60 * 60


async def run_query_alert_poll(monitor, store, owner_resolver, cooldown_seconds, send) -> int:
    """Poll each Eventhouse monitor and send proactive slow-query alerts.

    * ``monitor``        – EventhouseMonitor (``poll(store) -> list[MonitorResult]``).
    * ``store``          – StateStore for watermarks and per-user cooldowns.
    * ``owner_resolver`` – SemanticModelOwnerResolver (owners + report names).
    * ``cooldown_seconds`` – minimum seconds between alerts to the same recipient.
    * ``send``           – async ``send(user_id, greeting, pending)`` callback that
                           delivers ``greeting`` and stores ``pending`` context.

    Returns the number of alert messages actually sent.
    """
    alerts_sent = 0
    for result in await monitor.poll(store):
        recipient_events = {user_id: list(result.events) for user_id in result.user_ids}
        if result.notify_model_owner:
            owner_events = await owner_resolver.group_events_by_owner(
                result.workspace_id, result.events
            )
            for user_id, events in owner_events.items():
                recipient_events.setdefault(user_id, []).extend(events)
        if not recipient_events:
            log.warning("No recipients resolved for query alert %s", result.name)
            await store.set_monitor_watermark(result.key, result.watermark)
            continue

        handled_recipients = 0
        failed_recipients = 0
        for user_id, events in recipient_events.items():
            now = int(time.time())
            last_sent = await store.get_last_query_alert(result.key, user_id)
            if now - last_sent < cooldown_seconds:
                log.info("Query alert suppressed by cooldown for user %s", user_id)
                handled_recipients += 1
                continue
            try:
                greeting, pending = await _build_offer(owner_resolver, result, events)
                await send(user_id, greeting, pending)
                await store.set_last_query_alert(result.key, user_id, now)
                handled_recipients += 1
                alerts_sent += 1
            except Exception:
                failed_recipients += 1
                log.exception("Could not send query alert to user %s", user_id)
        # Advance the watermark as long as at least one recipient was handled
        # (delivered or cooldown-suppressed). This previously required *every*
        # recipient to succeed, so a single permanently-unreachable recipient
        # (e.g. a model owner who never installed the bot, or a stale
        # ConversationReference) froze the monitor's watermark indefinitely: the
        # same slow query was re-queried and re-greeted to the reachable
        # recipients on every poll, a notification storm rate-limited only by the
        # per-user cooldown. Only hold the watermark back when *no* recipient
        # could be handled at all, so a genuinely transient outage is still
        # retried on the next poll without replaying to everyone else.
        if handled_recipients > 0:
            if failed_recipients:
                log.warning(
                    "Advancing watermark for monitor %s despite %d failed "
                    "recipient(s); those alerts are dropped rather than replayed "
                    "to already-notified recipients.",
                    result.name,
                    failed_recipients,
                )
            await store.set_monitor_watermark(result.key, result.watermark)
    return alerts_sent


async def _build_offer(owner_resolver, result, events):
    """Build the casual greeting and the pending-analysis payload, grounded in the
    slowest ("offending") query in this recipient's window."""
    offending = max(events, key=lambda event: int(event.get("DurationMs") or 0))
    model_name = offending.get("ModelName") or offending.get("ModelId") or "your"
    duration_ms = int(offending.get("DurationMs") or 0)

    report_name = None
    report_id = offending.get("ReportId")
    if result.workspace_id and report_id:
        try:
            report_name = await owner_resolver.get_report_name(
                result.workspace_id, report_id
            )
        except Exception:
            log.exception("Could not resolve report name for %s", report_id)

    greeting = format_greeting(model_name, report_name, duration_ms, len(events) - 1)
    pending = {
        "model": offending.get("ModelName") or offending.get("ModelId"),
        "modelId": offending.get("ModelId"),
        "report": report_name,
        "reportId": report_id,
        "durationMs": duration_ms,
        "timestamp": offending.get("Timestamp"),
        "eventText": offending.get("EventText") or "",
        "extraCount": len(events) - 1,
        "expiresAt": int(time.time()) + _PENDING_TTL_SECONDS,
    }
    return greeting, pending

