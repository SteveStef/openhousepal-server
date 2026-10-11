"""Canonical fingerprint for the exact saved CMA content a client can receive."""
from __future__ import annotations

import copy
import hashlib
import json


ARTIFACT_VERSION = "cma-client-artifact-v1"


def _client_pricing(pricing: dict) -> dict:
    payload = copy.deepcopy(pricing or {})
    estimate = payload.get("ai_estimate")
    if isinstance(estimate, dict):
        for key in (
            "qa_result", "qa_flags", "qa_blocking", "valuation_trace",
            "authoritative_evidence_snapshot", "reasoning_usage", "reasoning_cost_usd",
            "generated_at", "comp_analysis_model", "model",
        ):
            estimate.pop(key, None)
    return payload


def client_artifact_fingerprint(
    *, subject: dict, comparables: list, pricing: dict, market_context: dict, improvements: list,
) -> str:
    payload = {
        "subject": subject or {},
        "comparables": comparables or [],
        "pricing": _client_pricing(pricing),
        "market_context": market_context or {},
        "improvements": improvements or [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp_final_artifact_qa(
    *, subject: dict, comparables: list, pricing: dict, market_context: dict, improvements: list,
) -> dict:
    updated = copy.deepcopy(pricing or {})
    estimate = updated.get("ai_estimate")
    if not isinstance(estimate, dict):
        return updated
    qa = copy.deepcopy(estimate.get("qa_result") or {})
    qa["artifact_version"] = ARTIFACT_VERSION
    qa["artifact_sha256"] = client_artifact_fingerprint(
        subject=subject, comparables=comparables, pricing=updated,
        market_context=market_context, improvements=improvements,
    )
    qa["validated_after_offer_materialization"] = True
    if "exact_saved_client_artifact" not in qa.setdefault("checks_run", []):
        qa["checks_run"].append("exact_saved_client_artifact")
    estimate["qa_result"] = qa
    updated["ai_estimate"] = estimate
    return updated


def saved_artifact_matches(
    *, subject: dict, comparables: list, pricing: dict, market_context: dict, improvements: list,
) -> bool:
    estimate = (pricing or {}).get("ai_estimate") or {}
    qa = estimate.get("qa_result") or {}
    if qa.get("artifact_version") != ARTIFACT_VERSION:
        return True
    expected = qa.get("artifact_sha256")
    return bool(expected) and expected == client_artifact_fingerprint(
        subject=subject, comparables=comparables, pricing=pricing,
        market_context=market_context, improvements=improvements,
    )
