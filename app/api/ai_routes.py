from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.logging import get_logger
from app.database import get_db
from app.models.database import AIConversation, AIMessage, AIRealtimeSession, User
from app.schemas.ai import (
    AIConversationListItem, AIConversationOut, AIMessageOut,
    AISendMessageRequest, AISendMessageResponse, AIInsightsResponse, AISpeechRequest,
    AIRealtimeSessionOut, AIRealtimeToolCallRequest, AIRealtimeToolCallResponse,
    AIRealtimeUsageRequest, AIRealtimeUsageResponse, AIRealtimeHeartbeatRequest, AIRealtimeEndRequest,
    DailyDigestResponse, DigestDismissRequest, MonthlyAIUsageResponse,
    CopywriteRequest, CopywriteResponse, ComplianceCheckRequest, ComplianceCheckResponse,
)
from app.services.ai import ai_copywrite_service, ai_insights_service, ai_tools
from app.services.ai.ai_compaction_service import maybe_compact
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import (
    AIOrchestratorError, run_turn, _extract_record_refs, _extract_result_refs,
)
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, check_and_reserve, check_input_length, monthly_ai_budget_snapshot,
    monthly_cost_so_far, record_usage_event, release, reconcile_budget, reserve_global_and_emergency,
)
from app.services.ai.ai_realtime_service import (
    create_ephemeral_session, end_realtime_session, record_realtime_usage, sweep_abandoned_sessions,
)
from app.services.ai.ai_tts_service import synthesize_speech
from app.utils.auth import get_current_active_user, require_admin_user, require_active_plan

logger = get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


async def _get_owned_conversation(
    db: AsyncSession, conversation_id: str, owner_id: str, load_messages: bool = False
) -> AIConversation:
    stmt = select(AIConversation).where(
        AIConversation.id == conversation_id,
        AIConversation.owner_id == owner_id,
        AIConversation.archived_at.is_(None),
    )
    if load_messages:
        stmt = stmt.options(selectinload(AIConversation.messages))
    result = await db.execute(stmt)
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


async def _build_history(db: AsyncSession, conversation: AIConversation, max_messages: int) -> List[dict]:
    """Recent-message replay window, oldest first. Only the final text of each past turn is
    replayed - not the tool calls that produced it - so token cost stays flat as a
    conversation grows, per the configured max_conversation_messages cap. If the conversation
    has been auto-compacted (ai_compaction_service.py), a leading summary stands in for
    everything at or before summary_covers_through instead of that context just vanishing."""
    stmt = (
        select(AIMessage)
        .where(AIMessage.conversation_id == conversation.id)
        .order_by(AIMessage.created_at.desc())
        .limit(max_messages)
    )
    if conversation.summary_covers_through:
        stmt = stmt.where(AIMessage.created_at > conversation.summary_covers_through)
    result = await db.execute(stmt)
    messages = list(reversed(result.scalars().all()))
    history = [{"role": m.role, "content": m.content} for m in messages]
    if conversation.summary:
        history.insert(0, {"role": "user", "content": f"[Summary of earlier conversation: {conversation.summary}]"})
    return history


@router.post("/conversations", response_model=AIConversationListItem, status_code=201)
async def create_ai_conversation(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    conversation = AIConversation(owner_id=current_user.id)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.get("/conversations", response_model=List[AIConversationListItem])
async def list_ai_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    stmt = (
        select(AIConversation)
        .where(AIConversation.owner_id == current_user.id, AIConversation.archived_at.is_(None))
        .order_by(AIConversation.last_message_at.desc().nullslast(), AIConversation.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/conversations/{conversation_id}", response_model=AIConversationOut)
async def get_ai_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    return await _get_owned_conversation(db, conversation_id, current_user.id, load_messages=True)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_ai_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Soft-delete only - sets archived_at. The conversation row and its messages are never
    destroyed, so the audit trail (AIUsageEvent) always resolves back to a real conversation."""
    conversation = await _get_owned_conversation(db, conversation_id, current_user.id)
    conversation.archived_at = datetime.now(timezone.utc)
    db.add(conversation)
    await db.commit()


@router.post("/conversations/{conversation_id}/messages", response_model=AISendMessageResponse)
async def send_ai_message(
    conversation_id: str,
    payload: AISendMessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    conversation = await _get_owned_conversation(db, conversation_id, current_user.id)
    config = await ai_config_service.get_config(db)

    message_text = (payload.message or "").strip()
    if not message_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        check_input_length(message_text, config)
        reservation = await check_and_reserve(db, current_user.id, config, current_user.plan_tier)
    except AIRateLimitExceeded as e:
        await record_usage_event(
            db, current_user.id, conversation.id, config.get("model"),
            None, None, None, 0, True, e.reason,
        )
        raise HTTPException(status_code=429, detail=e.user_message)

    try:
        block_reason = await maybe_compact(db, current_user, conversation, config)
        if block_reason:
            await record_usage_event(
                db, current_user.id, conversation.id, config.get("model"),
                None, None, None, 0, True, block_reason,
            )
            # The turn never ran - refund the Layer-3 reservation check_and_reserve already
            # made in full, so a conversation-length block never silently eats into the
            # platform-wide global/emergency budgets.
            await reconcile_budget(db, reservation, 0.0)
            raise HTTPException(
                status_code=429,
                detail="This conversation has gotten long enough that quality may suffer. Please start a new conversation to continue.",
            )

        history = await _build_history(db, conversation, config["max_conversation_messages"])

        user_msg = AIMessage(conversation_id=conversation.id, role="user", content=message_text)
        db.add(user_msg)
        await db.commit()
        await db.refresh(user_msg)

        try:
            text, refs, usage = await run_turn(db, current_user, history, message_text, voice_mode=payload.voice_mode)
        except AIOrchestratorError as e:
            # What the partial turn really cost (the orchestrator only prices a finished turn).
            # Logged as blocked, so it never counts on the agent's meter - they got no answer -
            # but the platform counters and admin view see the real spend.
            spent = e.usage.get("estimated_cost_usd")
            if spent is None:
                spent = ai_config_service.estimate_cost_usd(
                    config, e.usage.get("model", config.get("model")), e.usage.get("input_tokens") or 0, e.usage.get("output_tokens") or 0,
                    e.usage.get("cache_write_tokens") or 0, e.usage.get("cache_read_tokens") or 0,
                )
            await record_usage_event(
                db, current_user.id, conversation.id, e.usage.get("model", config.get("model")),
                e.usage.get("input_tokens"), e.usage.get("output_tokens"), spent,
                e.usage.get("tool_call_count", 0), True, e.reason,
                tool_names=e.usage.get("tool_names"),
            )
            # A failed/aborted turn may still have made real (partial) model calls before
            # erroring out - true up the reservation with whatever it actually cost, never a
            # blind full refund, so the global/emergency counters stay accurate either way.
            await reconcile_budget(db, reservation, spent or 0.0)
            raise HTTPException(status_code=503, detail=e.user_message)

        assistant_msg = AIMessage(conversation_id=conversation.id, role="assistant", content=text, tool_calls=refs or None)
        db.add(assistant_msg)

        conversation.last_message_at = datetime.now(timezone.utc)
        if not conversation.title:
            conversation.title = message_text[:80]
        db.add(conversation)

        await db.commit()
        await db.refresh(assistant_msg)

        await record_usage_event(
            db, current_user.id, conversation.id, usage["model"], usage["input_tokens"], usage["output_tokens"],
            usage.get("estimated_cost_usd"), usage["tool_call_count"], False, None,
            tool_names=usage.get("tool_names"),
        )
        await reconcile_budget(db, reservation, usage.get("estimated_cost_usd") or 0.0)

        return AISendMessageResponse(
            conversation_id=conversation.id,
            user_message=AIMessageOut.model_validate(user_msg),
            assistant_message=AIMessageOut.model_validate(assistant_msg),
        )
    finally:
        await release(current_user.id)


@router.get("/insights", response_model=AIInsightsResponse)
async def get_ai_insights(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Rule-based digest for the Assistant home screen - zero LLM cost, no rate limiting
    needed since it never calls the model. Open to every user (not admin-gated like the rest
    of this router) since it never touches the LLM assistant this router otherwise gates."""
    insights = await ai_insights_service.get_insights(db, current_user)
    return AIInsightsResponse(insights=insights, generated_at=datetime.now(timezone.utc))


@router.get("/digest", response_model=DailyDigestResponse)
async def get_daily_digest_route(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Powers the Dashboard's Daily Digest panel for every user - rule-based, zero LLM cost,
    same reasoning as /insights above. Kept separate from /insights (the Assistant home
    screen's admin-only feed) because this one is richer: a "needs attention" queue with
    deep links straight to the thing to address, plus a chronological feed of what actually
    happened today."""
    return await ai_insights_service.get_daily_digest(db, current_user)


@router.get("/usage/monthly", response_model=MonthlyAIUsageResponse)
async def get_monthly_ai_usage(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The "Monthly AI Usage" meter: this agent's month-to-date spend across every AI tool
    combined, as a share of their plan's monthly AI budget. Open to every user; returns
    percent=null for a tier with no configured budget (the frontend then shows no meter)."""
    config = await ai_config_service.get_config(db)
    return await monthly_ai_budget_snapshot(db, current_user.id, config, current_user.plan_tier)


@router.get("/copywrite/estimate")
async def estimate_ai_copywrite(
    purpose: str,
    chars: int = 1500,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Roughly what one /ai/copywrite call of this purpose will cost, for a "~X% of your monthly
    AI" note shown before the agent spends it (e.g. the open house Cheat Sheet's Recap). Uses the
    same model + pricing as the real call; `chars` is the length of the text it will read."""
    config = await ai_config_service.get_config(db)
    model = config["copywrite_model"]
    input_tokens = 700 + max(0, min(int(chars), 20000)) // 4
    output_tokens = min(int(config.get("copywrite_max_output_tokens") or 400), 160)
    usd = float(ai_config_service.estimate_cost_usd(config, model, input_tokens, output_tokens))
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(current_user.plan_tier)
    return {"purpose": purpose, "usd": round(usd, 5), "percent": round(usd / float(limit) * 100, 3) if limit else None}


@router.post("/copywrite", response_model=CopywriteResponse)
async def ai_copywrite(
    payload: CopywriteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """AI copywriter - Design Studio template text, Blogs AI mode, Agent Hub bio polish, plus
    caption/email/text drafts. Metered against the shared Monthly AI Usage allowance."""
    return await ai_copywrite_service.generate(
        db, current_user.id, current_user.plan_tier,
        purpose=payload.purpose, context=payload.context, tone=payload.tone, instruction=payload.instruction,
    )


@router.post("/compliance-check", response_model=ComplianceCheckResponse)
async def ai_compliance_check(
    payload: ComplianceCheckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Fair-Housing / MLS advertising review. ADVISORY: returns risky phrases with a suggested
    swap; the agent accepts or ignores each. Never rewrites the text. Metered."""
    return await ai_copywrite_service.compliance_check(db, current_user.id, current_user.plan_tier, text=payload.text)


@router.post("/digest/dismiss")
async def dismiss_daily_digest_item(
    payload: DigestDismissRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Manually dismiss one Daily Digest item without navigating away - the persistent escape
    hatch for any item that can't (or shouldn't have to) be resolved just by looking at it."""
    await ai_insights_service.dismiss_digest_item(db, current_user.id, payload.item_id)
    return {"ok": True}


@router.post("/speech")
async def get_ai_speech(
    payload: AISpeechRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Read Aloud - synthesizes exactly the text handed in (already a finished Claude reply)
    via OpenAI TTS. Never calls Claude or any reasoning model - see ai_tts_service.py."""
    config = await ai_config_service.get_config(db)

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text to speak.")
    text = text[: config["tts_max_chars"]]

    cost_so_far = await monthly_cost_so_far(db, current_user.id, "tts")
    if cost_so_far >= config["tts_monthly_cost_ceiling_usd"]:
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, "tts_cost_ceiling", event_type="tts",
        )
        raise HTTPException(status_code=429, detail="You've reached your Read Aloud usage limit for this month.")

    try:
        reservation = await reserve_global_and_emergency(
            db, "tts_global", config["tts_global_reservation_estimate_usd"],
            config["tts_global_daily_cost_ceiling_usd"], config["tts_global_monthly_cost_ceiling_usd"],
            config,
        )
    except AIRateLimitExceeded as e:
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, e.reason, event_type="tts",
        )
        raise HTTPException(status_code=429, detail=e.user_message)

    try:
        audio_bytes = await synthesize_speech(text, config)
    except Exception as e:
        logger.warning("TTS synthesis failed: %s", e)
        await record_usage_event(
            db, current_user.id, payload.conversation_id, config.get("tts_model"),
            None, None, None, 0, True, "tts_error", event_type="tts",
        )
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status_code=503, detail="Read Aloud is temporarily unavailable. Please try again shortly.")

    cost = ai_config_service.estimate_tts_cost_usd(config, config["tts_model"], len(text))
    await record_usage_event(
        db, current_user.id, payload.conversation_id, config["tts_model"],
        len(text), 0, cost, 0, False, None, event_type="tts",
    )
    await reconcile_budget(db, reservation, cost)

    return Response(content=audio_bytes, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Talk to OHP (OpenAI Realtime). The browser connects directly to OpenAI over WebRTC - see
# ai_realtime_service.py's module docstring for why these few endpoints are the only points
# left where this backend can enforce limits or tenant isolation for a voice session.
# ---------------------------------------------------------------------------

async def _get_owned_realtime_session(db: AsyncSession, session_id: str, owner_id: str) -> AIRealtimeSession:
    stmt = select(AIRealtimeSession).where(AIRealtimeSession.id == session_id, AIRealtimeSession.owner_id == owner_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session or session.ended_at:
        raise HTTPException(status_code=404, detail="Voice session not found or already ended.")
    return session


@router.post("/realtime/session", response_model=AIRealtimeSessionOut)
async def start_realtime_session(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    config = await ai_config_service.get_config(db)
    await sweep_abandoned_sessions(db, config)
    try:
        return await create_ephemeral_session(db, current_user, config)
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    except Exception as e:
        logger.warning("Failed to start realtime session: %s", e)
        raise HTTPException(status_code=503, detail="Talk to OHP is temporarily unavailable. Please try again shortly.")


@router.post("/realtime/tool-call", response_model=AIRealtimeToolCallResponse)
async def realtime_tool_call(
    payload: AIRealtimeToolCallRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """The one place a Realtime tool call actually reaches this backend - the browser is just
    the relay between OpenAI's Realtime session and this endpoint. Calls the exact same
    ai_tools.execute_tool() the text assistant uses, so a lead that doesn't belong to this
    agent is invisible here for the identical reason it already is in text - no separate
    implementation to drift or hide a tenant-isolation bug in."""
    config = await ai_config_service.get_config(db)
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)

    if session.tool_call_count >= config["realtime_max_tool_calls_per_session"]:
        raise HTTPException(status_code=429, detail="Tool call budget for this voice session is exhausted.")

    result = await ai_tools.execute_tool(db, current_user, payload.tool_name, payload.args, config)
    refs = _extract_record_refs(payload.tool_name, payload.args) + _extract_result_refs(payload.tool_name, result)

    session.tool_call_count += 1
    session.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(session)
    await db.commit()

    return AIRealtimeToolCallResponse(result=result, refs=refs)


@router.post("/realtime/usage", response_model=AIRealtimeUsageResponse)
async def realtime_usage(
    payload: AIRealtimeUsageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    """Forwards the real `usage` object from one Realtime response.done event - cost is
    tracked incrementally throughout the session from actual reported usage, never trusted as
    a single end-of-session self-report."""
    config = await ai_config_service.get_config(db)
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)
    should_end = await record_realtime_usage(db, session, current_user, config, payload.usage)
    return AIRealtimeUsageResponse(should_end=should_end)


@router.post("/realtime/heartbeat")
async def realtime_heartbeat(
    payload: AIRealtimeHeartbeatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    session = await _get_owned_realtime_session(db, payload.session_id, current_user.id)
    session.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(session)
    await db.commit()
    return {"ok": True}


@router.post("/realtime/end")
async def realtime_end(
    payload: AIRealtimeEndRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_user),
):
    stmt = select(AIRealtimeSession).where(AIRealtimeSession.id == payload.session_id, AIRealtimeSession.owner_id == current_user.id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Voice session not found.")
    config = await ai_config_service.get_config(db)
    await end_realtime_session(db, session, payload.reason, config=config)
    return {"ok": True}
