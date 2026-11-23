"""Provider-neutral AI-assisted discovery orchestration."""
import hashlib
import json
from pattern_discovery import DB, citation_spans, import_ai_suggestions, normalize_phrase, selected_evidence, _connect, _start_run
from pattern_discovery_providers import ProviderConfig, ProviderConfigurationError, ProviderRequestError, load_provider


_EVIDENCE_SCHEMA = {
    "type": "object",
    "description": "One exact source citation from current selected evidence.",
    "properties": {
        "source_candidate_id": {"type": "string", "description": "The exact selected evidence candidate ID."},
        "source_artifact_id": {"type": "integer", "description": "The source artifact ID belonging to that candidate."},
        "source_excerpt_id": {"type": "string", "description": "Select an exact source_excerpt_id from the cited evidence item's citation_spans."},
        "exact_source_excerpt": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Return null. Sentinel resolves this from source_excerpt_id and the immutable selected raw text."},
    },
    "required": ["source_candidate_id", "source_artifact_id", "source_excerpt_id", "exact_source_excerpt"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "description": "Review proposals grounded in selected Sentinel evidence; return an empty list only when none qualify.",
    "properties": {
        "proposals": {
            "type": "array",
            "description": "Potentially useful candidates for human review, including single-source brands, services, and terminology.",
            "items": {
                "type": "object",
                "description": "A source-supported discovery candidate. It is not an approved pattern.",
                "properties": {
                    "candidate_name": {"type": "string", "description": "A concise name or phrase that must occur in at least one cited exact source span after Sentinel normalization."},
                    "candidate_category": {"type": "string", "enum": [
                        "company_brand", "service_description", "recurring_phrase", "business_terminology", "other"
                    ], "description": "Use recurring_phrase only when similar wording recurs across distinct source artifacts."},
                    "language": {"type": "string", "description": "Language tag of the candidate wording, or und if unknown."},
                    "supporting_evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
                    "reasoning": {"type": "string", "description": "Why this source text may be useful as a pattern-library candidate."},
                    "confidence": {"type": "number", "description": "An uncalibrated estimate from 0 to 1."},
                    "confidence_basis": {"type": "string", "description": "Evidence basis and uncertainty; do not imply calibration."},
                    "novelty_type": {"type": "string", "enum": ["new_pattern", "variant", "possible_duplicate"]},
                    "target_pattern_id": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Active approved pattern ID for variant or possible_duplicate; otherwise null."},
                    "description": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Optional concise description grounded in the citations."},
                    "suggested_category_key": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Existing Pattern Library category key, or null."},
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


def _summarize_response(response):
    """Create a useful audit summary without retaining model text or citations."""
    try:
        if isinstance(response, str):
            raw = response.encode("utf-8")
            parsed = json.loads(response)
        else:
            raw = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
            parsed = response
    except (UnicodeEncodeError, json.JSONDecodeError, TypeError, ValueError):
        raw = response.encode("utf-8", errors="replace") if isinstance(response, str) else b""
        return {"parse_status": "invalid_json", "response_sha256": hashlib.sha256(raw).hexdigest()}
    summary = {
        "parse_status": "valid_json",
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "top_level_keys": sorted(parsed) if isinstance(parsed, dict) else [],
    }
    proposals = parsed.get("proposals") if isinstance(parsed, dict) else None
    if isinstance(proposals, list):
        summary["proposal_count"] = len(proposals)
        categories = {}
        citations = 0
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            category = proposal.get("candidate_category")
            if isinstance(category, str):
                categories[category] = categories.get(category, 0) + 1
            evidence = proposal.get("supporting_evidence")
            if isinstance(evidence, list):
                citations += len(evidence)
        summary["proposal_categories"] = categories
        summary["citation_count"] = citations
    else:
        summary["proposal_count"] = None
    return summary


def _reject_unsupported_names(response, db_path):
    """Drop well-formed proposals whose name is absent from every cited exact span.

    Malformed or fabricated citations are left for the strict importer to reject.
    This allows supported proposals in the same response to be persisted without
    accepting a candidate name that the cited source does not actually contain.
    """
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return response, 0
    if not isinstance(response, dict) or not isinstance(response.get("proposals"), list):
        return response, 0
    conn = _connect(db_path)
    try:
        active = {r["candidate_id"]: r for r in selected_evidence(conn)}
        kept, rejected = [], 0
        for proposal in response["proposals"]:
            if not isinstance(proposal, dict) or not isinstance(proposal.get("candidate_name"), str):
                kept.append(proposal)
                continue
            name = normalize_phrase(proposal["candidate_name"])
            citations = proposal.get("supporting_evidence")
            if not name or not isinstance(citations, list) or not citations:
                kept.append(proposal)
                continue
            excerpts, citations_valid = [], True
            for item in citations:
                if not isinstance(item, dict):
                    citations_valid = False
                    break
                source = active.get(item.get("source_candidate_id"))
                if (not source or item.get("source_artifact_id") != source["source_artifact_id"]):
                    citations_valid = False
                    break
                span_id = item.get("source_excerpt_id")
                if span_id is not None:
                    span = next((s for s in citation_spans(source["original_text"]) if s["source_excerpt_id"] == span_id), None)
                    if not span or item.get("exact_source_excerpt") not in (None, span["exact_source_excerpt"]):
                        citations_valid = False
                        break
                    excerpts.append(span["exact_source_excerpt"])
                else:
                    excerpt = item.get("exact_source_excerpt")
                    if not isinstance(excerpt, str) or not excerpt or excerpt not in (source["original_text"] or ""):
                        citations_valid = False
                        break
                    excerpts.append(excerpt)
            if citations_valid and not any(name in normalize_phrase(excerpt) for excerpt in excerpts):
                rejected += 1
            else:
                kept.append(proposal)
        return {**response, "proposals": kept}, rejected
    finally:
        conn.close()


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
            "task": "Systematically discover source-supported pattern candidates for human review. Never make approval or database changes.",
            "rules": [
                "Use only the supplied selected evidence and active approved pattern library.",
                "Scan every selected evidence item for companies, brands, publishers, channel or program titles, products, concrete offers and service descriptions, business terminology, distinctive claims, slogans, and repeated expressions.",
                "Do not require recurrence for a clearly supported company_brand, service_description, or business_terminology candidate; one source can justify a low-confidence review candidate.",
                "Use recurring_phrase only when similar wording appears in at least two distinct source artifacts, and cite each supporting artifact.",
                "A repeated channel, publisher, or program title can be a company_brand lead; do not infer legal company status or treat a person name alone as an organization.",
                "Respect each evidence item's language. Use normalized_text to notice wording, but keep the candidate in its source language.",
                "The active Pattern Library is duplication context, not a reason to suppress a related discovery; label a supported variant or possible duplicate and cite its active target pattern.",
                "Do not invent names or facts, and do not promote ambiguous fragments into specific claims. Skipping weak evidence is preferable to guessing.",
                "For every citation, choose a supplied source_excerpt_id from that evidence item's citation_spans. Return exact_source_excerpt as null; Sentinel will resolve and validate the exact immutable source text. Do not invent or alter span IDs.",
                "Each citation must include the actual source_candidate_id, matching source_artifact_id, and one supplied source_excerpt_id.",
                "Choose a cited span that contains the candidate_name itself. Do not infer candidate names from uncited context or paraphrase the name.",
                "Do not invent IDs, quotations, names or facts. Do not infer fraud or intent.",
                "Separate a new pattern from a variant or possible duplicate and cite target_pattern_id when applicable.",
                "Confidence is an uncalibrated estimate; explain its basis and limitations.",
                "Return an empty proposals list only after checking all supplied evidence and finding no source-supported, non-duplicate candidate worth human review.",
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
                "citation_spans": citation_spans(r["original_text"]),
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
        response_summary = _summarize_response(response)
        import_response, rejected_unsupported = _reject_unsupported_names(response, db_path)
        response_summary["unsupported_name_rejections"] = rejected_unsupported
        conn = _connect(db_path)
        run = conn.execute("SELECT config_json FROM pattern_discovery_runs WHERE run_id=?", (run_id,)).fetchone()
        run_config = json.loads(run[0] or "{}") if run else {}
        run_config["response_summary"] = response_summary
        conn.execute("UPDATE pattern_discovery_runs SET config_json=? WHERE run_id=?",
            (json.dumps(run_config, sort_keys=True), run_id))
        conn.commit()
        conn.close()
        result = import_ai_suggestions(import_response, db_path,
            run_metadata={**metadata, "response_summary": response_summary}, existing_run_id=run_id)
        output = {**result, "rejected": rejected_unsupported, "provider": config.provider, "model": config.model,
            "response_summary": response_summary}
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
