"""Provider-neutral AI-assisted discovery orchestration."""
import json
from pattern_discovery import DB, import_ai_suggestions, selected_evidence, _connect, _start_run
from pattern_discovery_providers import ProviderConfig, ProviderConfigurationError, ProviderRequestError, load_provider


_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_candidate_id": {"type": "string"},
        "source_artifact_id": {"type": "integer"},
        "exact_source_excerpt": {"type": "string"},
    },
    "required": ["source_candidate_id", "source_artifact_id", "exact_source_excerpt"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_name": {"type": "string"},
                    "candidate_category": {"type": "string", "enum": [
                        "company_brand", "service_description", "recurring_phrase", "business_terminology", "other"
                    ]},
                    "language": {"type": "string"},
                    "supporting_evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number"},
                    "confidence_basis": {"type": "string"},
                    "novelty_type": {"type": "string", "enum": ["new_pattern", "variant", "possible_duplicate"]},
                    "target_pattern_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "suggested_category_key": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": [
                    "candidate_name", "candidate_category", "language", "supporting_evidence", "reasoning",
                    "confidence", "confidence_basis", "novelty_type", "target_pattern_id", "description",
                    "suggested_category_key",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proposals"],
    "additionalProperties": False,
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
                "Look for potentially relevant new companies, brands, service descriptions, business terms, and recurring expressions.",
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
    request = build_ai_request(db_path)
    metadata = {"provider": config.provider, "model": config.model}
    conn = _connect(db_path)
    run_id = _start_run(conn, len(request["selected_evidence"]), "ai_assisted")
    conn.execute("UPDATE pattern_discovery_runs SET config_json=? WHERE run_id=?",
        (json.dumps({"mode": "ai_assisted", "source": "current_selected_matching_input", **metadata}, sort_keys=True), run_id))
    conn.commit()
    conn.close()
    try:
        adapter = provider or load_provider(config, environ)
        response = adapter.generate(request)
        if not isinstance(response, (dict, str)):
            raise ValueError("provider response must be a JSON object or string")
        if isinstance(response, str) and len(response.encode("utf-8")) > 2_000_000:
            raise ValueError("provider response exceeds the 2 MB limit")
        result = import_ai_suggestions(response, db_path, run_metadata=metadata, existing_run_id=run_id)
        output = {**result, "provider": config.provider, "model": config.model}
        usage = getattr(adapter, "usage", None)
        if isinstance(usage, dict):
            output["provider_usage"] = dict(usage)
        return output
    except Exception as error:
        conn = _connect(db_path)
        if isinstance(error, ProviderRequestError):
            safe_error = error.safe_message
        elif isinstance(error, ProviderConfigurationError):
            safe_error = str(error)[:300]
        elif isinstance(error, ValueError):
            safe_error = str(error)[:300]
        else:
            safe_error = type(error).__name__
        conn.execute("UPDATE pattern_discovery_runs SET status='failed',error_text=?,completed_at=CURRENT_TIMESTAMP WHERE run_id=?",
            (safe_error, run_id))
        conn.commit()
        conn.close()
        raise


__all__ = ["build_ai_request", "run_ai_discovery", "RESPONSE_SCHEMA"]
