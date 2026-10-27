"""Auto-compacts long AI Assistant conversations instead of silently truncating them.

Without this, `_build_history()` in ai_routes.py just replays the most recent
`max_conversation_messages` and drops everything older with no warning. This module folds
the older portion into a short running summary (a small, cheap Claude call - its cost is
logged through the normal AIUsageEvent audit trail, same as any other request, so it
counts toward the same monthly cost ceiling, never a side channel around it) instead of
losing that context. Once a conversation has been compacted `max_compactions_per_conversation`
times and is *still* too long, maybe_compact() returns a block reason instead of compacting
again - the caller (ai_routes.py) turns that into a 429 asking the agent to start fresh,
the same way a rate limit does.
"""
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import AIConversation, AIMessage, User
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import _get_client
from app.services.ai.ai_rate_limit_service import monthly_cost_so_far, record_usage_event

logger = get_logger(__name__)

COMPACTION_SYSTEM_PROMPT = (
    "Summarize the following conversation between a real estate agent and an AI assistant "
    "in 3-5 sentences. Preserve specific names, addresses, dates, and any facts or decisions "
    "mentioned - this summary replaces the raw messages as context for continuing the "
    "conversation, so don't lose anything a continuation would need. Output only the summary "
    "text itself, no preamble or commentary."
)


async def maybe_compact(db: AsyncSession, current_user: User, conversation: AIConversation, config: dict) -> Optional[str]:
    """Returns a block reason if this conversation must be rejected (too long, no compactions
    left) - the caller must not proceed to the model. Returns None otherwise, whether or not
    a compaction actually happened. Never raises: a failed compaction attempt just falls back
    to ordinary truncated replay rather than blocking the user's actual message."""
    since = conversation.summary_covers_through

    count_stmt = select(func.count(AIMessage.id)).where(AIMessage.conversation_id == conversation.id)
    if since:
        count_stmt = count_stmt.where(AIMessage.created_at > since)
    uncovered_count = (await db.execute(count_stmt)).scalar_one()

    if uncovered_count <= config["max_conversation_messages"]:
        return None

    if conversation.compaction_count >= config["max_compactions_per_conversation"]:
        return "conversation_too_long"

    # Leave a cushion so compaction itself is never the call that trips the ceiling - it's a
    # quality improvement, not something worth blocking the user's real message over.
    cost_so_far = await monthly_cost_so_far(db, current_user.id, "chat")
    if cost_so_far >= config["monthly_cost_ceiling_usd"] * 0.9:
        logger.info("Skipping compaction for conversation %s - near monthly cost ceiling", conversation.id)
        return None

    msg_stmt = select(AIMessage).where(AIMessage.conversation_id == conversation.id).order_by(AIMessage.created_at.asc())
    if since:
        msg_stmt = msg_stmt.where(AIMessage.created_at > since)
    messages = (await db.execute(msg_stmt)).scalars().all()
    if not messages:
        return None

    transcript = "\n\n".join(f"{m.role}: {m.content}" for m in messages)
    if conversation.summary:
        transcript = f"Earlier summary: {conversation.summary}\n\nNew messages since then:\n\n{transcript}"

    input_tokens = output_tokens = 0
    try:
        client = _get_client()
        response = await client.messages.create(
            model=config["model"],
            max_tokens=config["compaction_max_output_tokens"],
            system=COMPACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
            **ai_config_service.model_options(config["model"]),
        )
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        summary_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        if not summary_text:
            raise ValueError("empty summary")
    except Exception as e:
        logger.warning("Compaction failed for conversation %s, falling back to truncation: %s", conversation.id, e)
        return None

    cost = ai_config_service.estimate_cost_usd(config, config["model"], input_tokens, output_tokens)
    await record_usage_event(
        db, current_user.id, conversation.id, config["model"],
        input_tokens, output_tokens, cost, 0, False, None,
        event_type="chat",
    )

    conversation.summary = summary_text
    conversation.summary_covers_through = messages[-1].created_at
    conversation.compaction_count += 1
    db.add(conversation)
    await db.commit()

    return None
