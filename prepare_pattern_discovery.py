"""Prepare a selected-evidence-only offline AI discovery request."""
import json
from pathlib import Path

from pattern_discovery_ai import RESPONSE_SCHEMA, build_ai_request


BASE_DIR = Path(__file__).resolve().parent
OUTPUT = BASE_DIR / "outputs"
OUTPUT.mkdir(exist_ok=True)

request = build_ai_request()
bundle = {
    "source": "current_selected_matching_input",
    "approved_pattern_library": request["approved_pattern_library"],
    "available_library_categories": request["available_library_categories"],
    "selected_evidence": request["selected_evidence"],
}
bundle_path = OUTPUT / "pattern_discovery_bundle.json"
bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

prompt = f"""# Sentinel AI assisted pattern discovery

Suggest reviewable reusable patterns from the supplied evidence. This is a
discovery aid only. Never approve or publish a pattern, modify the library,
classify an advertisement as fraudulent, or invent names, IDs, facts, or quotes.

Use only the selected evidence and active approved Pattern Library in the JSON
bundle. Every suggestion must cite one or more source_candidate_id and
source_artifact_id values and include an exact verbatim excerpt. If the evidence
does not support a useful suggestion, return an empty proposals list.

Return only valid JSON in this shape, with every listed field present:

{json.dumps(RESPONSE_SCHEMA, ensure_ascii=False, indent=2)}

confidence is an uncalibrated estimate. Explain its basis and limitations.
"""
prompt_path = OUTPUT / "pattern_discovery_prompt.md"
prompt_path.write_text(prompt, encoding="utf-8")

print(f"Selected evidence records: {len(bundle['selected_evidence'])}")
print(f"Active approved patterns: {len(bundle['approved_pattern_library'])}")
print(f"Bundle: {bundle_path}")
print(f"Prompt: {prompt_path}")
