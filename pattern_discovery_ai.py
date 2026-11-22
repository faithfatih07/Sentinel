"""Provider-neutral AI-assisted discovery orchestration.

No LLM provider or network dependency is bundled. Integrators install an adapter
under the provider entry-point group and configure it through environment vars.
"""
from pathlib import Path

from pattern_discovery import DB, import_ai_suggestions, selected_evidence, _connect
from pattern_discovery_providers import ProviderConfig, load_provider


RESPONSE_SCHEMA = {
    "proposals": [{
        "candidate_name": "string",
        "candidate_category": "company_brand|service_description|recurring_phrase|business_terminology|other",
        "language": "BCP-47 language code or und",
        "supporting_evidence": [{
            "source_candidate_id": "exact selected evidence candidate ID",
            "source_artifact_id": "integer source artifact ID",
            "exact_source_excerpt": "verbatim substring of that source's raw evidence",
        }],
        "reasoning": "why this may be a reusable pattern, without asserting truth beyond evidence",
        "confidence": "number from 0 to 1; uncalibrated estimate",
        "confidence_basis": "what evidence supports this estimate and its limitations",
        "novelty_type": "new_pattern|variant|possible_duplicate",
        "target_pattern_id": "integer active approved pattern ID for variant/possible_duplicate; null for new_pattern",
    }]
}


def build_ai_request(db_path=DB):
    """Build a request containing current selected evidence and active library only."""
    conn = _connect(db_path)
    try:
        evidence = selected_evidence(conn)
        patterns = [dict(row) for row in conn.execute("""SELECT p.id,p.canonical_name,p.description,
            GROUP_CONCAT(DISTINCT v.variant_text) AS variants,
            GROUP_CONCAT(DISTINCT c.category_key) AS categories
            FROM patterns p
            LEFT JOIN pattern_variants v ON v.pattern_id=p.id
            LEFT JOIN pattern_category_map pcm ON pcm.pattern_id=p.id
            LEFT JOIN pattern_categories c ON c.id=pcm.category_id
            WHERE p.is_active=1 GROUP BY p.id ORDER BY p.id""")]
        categories = [dict(r) for r in conn.execute("SELECT category_key,name,description FROM pattern_categories ORDER BY id")]
        return {
            "task": "Suggest reviewable reusable patterns. Never make approval or database changes.",
            "rules": [
                "Use only the supplied selected evidence and active approved pattern library.",
                "Cite source_candidate_id and source_artifact_id and provide exact verbatim excerpts.",
                "Do not invent IDs, quotations, names or facts. Do not infer fraud or intent.",
                "Separate a new pattern from a variant or possible duplicate and cite target_pattern_id when applicable.",
                "Confidence is an uncalibrated estimate; explain its basis and limitations.",
                "Return only one JSON object conforming to response_schema; no markdown or prose outside JSON.",
            ],
            "response_schema": RESPONSE_SCHEMA,
            "approved_pattern_library": patterns,
            "available_library_categories": categories,
            "selected_evidence": [{
                "source_candidate_id": r["candidate_id"],
                "source_artifact_id": r["source_artifact_id"],
                "advertisement_id": r["advertisement_id"],
                "modality": r["modality"],
                "language": r["language"],
                "raw_text": r["original_text"],
                "normalized_text": r["normalized_text"],
                "normalization_version": r["normalization_version"],
                "frame_timestamp_seconds": r["frame_timestamp_seconds"],
                "interval_start_seconds": r["interval_start_seconds"],
                "interval_end_seconds": r["interval_end_seconds"],
            } for r in evidence],
        }
    finally:
        conn.close()


def run_ai_discovery(db_path=DB, provider=None, config=None, environ=None):
    """Call an explicitly configured/injected provider and persist validated suggestions."""
    config = config or ProviderConfig.from_environment(environ)
    adapter = provider or load_provider(config, environ)
    request = build_ai_request(db_path)
    response = adapter.generate(request)
    if not isinstance(response, (dict, str)):
        raise ValueError("provider response must be a JSON object or string")
    if isinstance(response, str) and len(response.encode("utf-8")) > 2_000_000:
        raise ValueError("provider response exceeds the 2 MB limit")
    metadata = {"provider": config.provider, "model": config.model}
    return import_ai_suggestions(response, db_path, run_metadata=metadata)


__all__ = ["build_ai_request", "run_ai_discovery", "RESPONSE_SCHEMA"]
