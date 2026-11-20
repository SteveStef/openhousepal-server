"""Fix grammar - one shared button for anything an agent types (owner 11/19: "can change what they write but not
material, just grammar fixes ... show up anytime an agent manually types somewhere, like we have in blogs").

The blog editor's Refine (ai_blog_writer_service.polish_blocks) made general: passages in, each with an id, the same
passages back fixed for spelling, grammar, punctuation and clumsy wording - nothing added, nothing removed, every fact,
number, price, date, name and place kept. Code then checks every reply with the blog's own rules (polish_problem): a
number, link or line that changed, a name the agent never wrote, or a passage that grew or shrank a lot is thrown away
and the agent's own words stay. The agent sees each change before and after and picks.

Runs on the small model (Haiku). Metered like the AI copywriter: the agent's shared Monthly AI Usage allowance
(event_type="copywrite"), reserve -> call -> reconcile, a failed call is never charged.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from fastapi import HTTPException, status

from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_copywrite,
)
from app.services.ai.ai_blog_writer_service import polish_problem, REFINE_MODEL

logger = logging.getLogger(__name__)

MAX_PASSAGES = 40
MAX_CHARS = 12000

_SYSTEM = (
    "You are a careful copy editor fixing a real estate agent's own writing, one passage at a time. Fix spelling, grammar, "
    "punctuation, capitalization and clumsy wording so each passage reads cleanly, in the agent's own voice.\n"
    "Rules, all of them:\n"
    "- Keep the meaning. Keep every fact, number, price, date, name, address and place exactly as written.\n"
    "- Add nothing: no new sentence, advice, example, explanation, fact or opinion. Remove no point the agent made.\n"
    "- Keep each passage about the same length, and keep it the same passage: never merge, split, add, drop or reorder passages.\n"
    "- A passage of several lines comes back with the same number of lines, in the same order.\n"
    "- Keep **bold** marks around the same words.\n"
    "- Keep the kind of sentence: never turn a statement into a question or a question into a statement. Keep every ? and !.\n"
    "- Keep each verb's tense and how sure the agent sounds: past stays past, 'I think' stays, nothing becomes a stronger "
    "or weaker claim.\n"
    "- Keep the agent's style. Shorthand and abbreviations stay exactly as written (w/, bd, ba, sq ft, sf, DOM, OH, vm, k, "
    "tbh, prob, til, rt, SS, ARV, HOA). Words in ALL CAPS stay in capitals. Never add a $, %, or unit the agent didn't write.\n"
    "- Fix only what is wrong. A quick note that reads fine as a note stays a note - don't make it formal.\n"
    "- A passage that is already fine comes back unchanged.\n"
    "Return every passage, with its id, by calling submit_passages."
)
_TOOL = {
    "name": "submit_passages",
    "description": "Return every passage with its id and its corrected text.",
    "input_schema": {"type": "object", "properties": {"passages": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}}, "required": ["id", "text"]}}},
        "required": ["passages"]},
}


def _client():
    from app.services.ai.ai_orchestrator_service import _get_client
    return _get_client()


# Words that carry the meaning a grammar fix must never touch (owner 11/19: "doesn't make contradictions or do the
# opposite"): the not-words (one more or one fewer flips a sentence) and the comparison words (nicer -> not as nice).
_NOT_WORDS = {"not", "no", "never", "none", "nothing", "nobody", "nowhere", "neither", "nor", "without", "cannot", "hardly",
              "barely", "lacks", "lacking", "unlike"}
_COMPARE_WORDS = {"nicer", "better", "worse", "best", "worst", "above", "below", "more", "less", "fewer", "most", "least",
                  "over", "under", "higher", "lower", "larger", "smaller", "bigger", "newer", "older", "superior", "inferior",
                  "cheaper", "pricier", "faster", "slower", "up", "down", "same", "similar", "different", "only", "all",
                  "every", "each", "both", "either", "some", "many", "few", "several", "before", "after", "since", "until"}
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")


def _close(a: str, b: str) -> bool:
    """Within two letters of each other (a typo of the same word)."""
    if abs(len(a) - len(b)) > 2:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 2


def meaning_problem(before: str, after: str) -> Optional[str]:
    """Why a fix may change what the agent meant (None = it can't): a not-word or a comparison word was added or lost.
    A misspelled one the fix spelled right ("beter" -> "better", "isnt" -> "isn't") is allowed."""
    def words(t):
        out = []
        for w in _WORD.findall(t.lower().replace("’", "'")):
            if w.endswith("n't"):
                out.append("not")                 # isn't / doesn't / can't / won't all say "not"
                w = w[:-3]
            out.append(w)
        return out
    for mark, what in (("?", "a question mark"), ("!", "an exclamation mark"), ("$", "a dollar sign"), ("%", "a percent sign")):
        if before.count(mark) != after.count(mark):
            return f"{what} was added or removed"
    b, a = words(before), words(after)
    for vocab, what in ((_NOT_WORDS | {"not"}, "a 'not' word"), (_COMPARE_WORDS, "a comparison word")):
        for w in vocab:
            nb, na = b.count(w), a.count(w)
            if nb == na:
                continue
            if na > nb:
                # a typo in the agent's words, now spelled right?
                typos = [x for x in b if x not in vocab and x != w and _close(x, w) and x not in a]
                if (w == "not" and any(x.endswith("nt") and x[:-2] in {"is", "was", "do", "does", "did", "are", "were", "has",
                                                                   "have", "had", "could", "would", "should", "wo", "ca"} for x in b)) \
                        or len(typos) >= na - nb:
                    continue
            return f"{what} changed ({w})"
    return None


def checked_changes(held: dict, passages) -> tuple[list, list]:
    """The model's replies, checked: only a real fix that passes every rule is offered. Returns (changes, refused)."""
    changes, refused = [], []
    for p in passages if isinstance(passages, list) else []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id"))
        before = held.get(pid)
        after = str(p.get("text") or "").strip()
        if before is None or not after or after == before.strip():
            continue
        why = polish_problem(before, after) or meaning_problem(before, after)
        if why:
            refused.append(why)
            continue
        changes.append({"id": pid, "before": before, "after": after})
    return changes, refused


async def fix_passages(db, user, passages: list, where: Optional[str] = None) -> dict:
    """[{id, text}] -> {"changes": [{id, before, after}], "checked": n}. A passage already fine is not in the list."""
    held: dict[str, str] = {}
    for p in passages or []:
        if not isinstance(p, dict):
            continue
        t = str(p.get("text") or "").strip()
        if p.get("id") is not None and t:
            held[str(p["id"])] = t
    if not held:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "There is nothing written here yet - type a line or two first.")
    if len(held) > MAX_PASSAGES or sum(len(t) for t in held.values()) > MAX_CHARS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That is a lot at once - check one part at a time.")
    config = await ai_config_service.get_config(db)
    model = config.get("grammar_fix_model") or REFINE_MODEL
    user_msg = ((f"Where this is written (context only): {where}\n\n" if where else "")
                + "PASSAGES TO FIX (json):\n" + json.dumps([{"id": i, "text": t} for i, t in held.items()], ensure_ascii=False))
    try:
        reservation = await reserve_copywrite(db, user.id, config, user.plan_tier)
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)
    try:
        from app.services import cma_claude
        resp = await cma_claude.create(
            _client(), model=model, max_tokens=min(8000, 400 + sum(len(t) for t in held.values()) // 2),
            system=_SYSTEM, tools=[_TOOL], tool_choice={"type": "tool", "name": _TOOL["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception:
        logger.exception("grammar fix call failed")
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The grammar check didn't respond - try again in a moment. Nothing was charged.")
    cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
    await record_usage_event(db, user.id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
                             cost, 1, False, None, tool_names=[_TOOL["name"]], event_type="copywrite")
    await reconcile_budget(db, reservation, cost)
    payload = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == _TOOL["name"]), None)
    changes, refused = checked_changes(held, (payload or {}).get("passages"))
    if refused:
        logger.info("grammar fix: %d corrected passage(s) not offered: %s", len(refused), "; ".join(sorted(set(refused))))
    return {"changes": changes, "checked": len(held)}
