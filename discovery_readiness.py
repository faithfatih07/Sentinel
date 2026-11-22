
import sqlite3
import json

from pathlib import Path
from collections import Counter, defaultdict
from urllib.parse import urlparse
from datetime import datetime, timezone

# ==========================================
# SENTINEL DISCOVERY READINESS
# Read-only database assessment
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "sentinel.db"
OUTPUT = BASE_DIR / "outputs"

OUTPUT.mkdir(exist_ok=True)

# Operational pilot heuristics.
# These are not regulatory or scientific thresholds.
MIN_ADVERTISEMENTS = 10
MIN_COMPANIES = 3
MIN_PLATFORMS = 2

if not DB.exists():
    raise FileNotFoundError(f"Database not found: {DB}")

# Open SQLite in read-only mode
connection = sqlite3.connect(
    f"file:{DB.as_posix()}?mode=ro",
    uri=True
)
connection.row_factory = sqlite3.Row

cursor = connection.cursor()

# ==========================================
# HELPERS
# ==========================================

def get_tables():

    rows = cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name NOT LIKE 'sqlite_%'
    """).fetchall()

    return {row["name"] for row in rows}


def count_values(rows, field):

    return Counter(
        str(row[field])
        for row in rows
        if row[field] not in (None, "")
    )


def get_domain(url):

    if not url:
        return None

    try:
        parsed = urlparse(
            url if "://" in url else "//" + url
        )

        host = parsed.hostname

        if host:
            return host.lower().removeprefix("www.")

    except ValueError:
        pass

    return None


def average(values):

    valid = [
        float(value)
        for value in values
        if value is not None
    ]

    if not valid:
        return None

    return round(sum(valid) / len(valid), 2)


def distribution(rows, field):

    return dict(count_values(rows, field))


# ==========================================
# CHECK DATABASE STRUCTURE
# ==========================================

tables = get_tables()

required_tables = {
    "advertisements",
    "media_assets",
    "extracted_artifacts",
    "normalized_artifacts",
    "patterns",
    "pattern_categories",
    "pattern_variants",
    "pattern_proposals",
    "advertisement_relationships"
}

missing = required_tables - tables

if missing:
    raise RuntimeError(
        f"Missing required tables: {sorted(missing)}"
    )

print("\n===== SENTINEL DISCOVERY READINESS =====")
print("Database opened in read-only mode.")


# ==========================================
# 1. ADVERTISEMENT ECOSYSTEM
# ==========================================

ads = cursor.execute("""
    SELECT
        id,
        company,
        platform,
        advertiser,
        url,
        content_hash
    FROM advertisements
    ORDER BY id
""").fetchall()

ad_ids = {row["id"] for row in ads}

companies = {
    row["company"].strip().casefold()
    for row in ads
    if row["company"] and row["company"].strip()
}

platforms = {
    row["platform"].strip().casefold()
    for row in ads
    if row["platform"] and row["platform"].strip()
}

advertisers = {
    row["advertiser"].strip().casefold()
    for row in ads
    if row["advertiser"] and row["advertiser"].strip()
}

domains = {
    get_domain(row["url"])
    for row in ads
    if get_domain(row["url"])
}

content_hashes = [
    row["content_hash"]
    for row in ads
    if row["content_hash"]
]

ad_metrics = {
    "advertisement_records": len(ads),
    "unique_companies": len(companies),
    "unique_platforms": len(platforms),
    "unique_advertiser_names": len(advertisers),
    "unique_domains": len(domains),
    "records_with_content_hash": len(content_hashes),
    "distinct_content_hashes": len(set(content_hashes))
}


# ==========================================
# 2. MEDIA COLLECTION
# ==========================================

assets = cursor.execute("""
    SELECT
        id,
        advertisement_id,
        asset_type,
        duration_seconds,
        file_size_bytes
    FROM media_assets
""").fetchall()

asset_types = distribution(assets, "asset_type")

ads_with_media = {
    row["advertisement_id"]
    for row in assets
}

media_metrics = {
    "total_media_assets": len(assets),
    "asset_types": dict(asset_types),
    "advertisements_with_media": len(ads_with_media),
    "advertisements_without_media": len(ad_ids - ads_with_media)
}


# ==========================================
# 3. EXTRACTION COVERAGE
# ==========================================

extracted = cursor.execute("""
    SELECT
        id,
        advertisement_id,
        media_asset_id,
        artifact_type,
        text_content,
        start_seconds,
        end_seconds,
        extraction_method,
        model_name,
        confidence,
        metadata_json
    FROM extracted_artifacts
""").fetchall()

extraction_types = distribution(extracted, "artifact_type")
extraction_methods = distribution(extracted, "extraction_method")

empty_extractions = sum(
    not (row["text_content"] or "").strip()
    for row in extracted
)

metadata_languages = Counter()

for row in extracted:

    raw_metadata = row["metadata_json"]

    if not raw_metadata:
        continue

    try:
        metadata = json.loads(raw_metadata)

        if isinstance(metadata, dict):

            language = (
                metadata.get("detected_language")
                or metadata.get("language")
            )

            if isinstance(language, str) and language.strip():
                metadata_languages[language.strip().lower()] += 1

    except (ValueError, TypeError):
        continue

extraction_metrics = {
    "total_extracted_artifacts": len(extracted),
    "artifact_types": dict(extraction_types),
    "extraction_methods": dict(extraction_methods),
    "empty_extractions": empty_extractions,
    "records_with_explicit_language_metadata": sum(
        metadata_languages.values()
    ),
    "explicit_language_distribution": dict(metadata_languages)
}


# ==========================================
# 4. NORMALIZED CONTENT COVERAGE
# ==========================================

normalized = cursor.execute("""
    SELECT
        id,
        source_artifact_id,
        advertisement_id,
        modality,
        original_text,
        normalized_text,
        normalized_hash,
        start_seconds,
        end_seconds,
        confidence,
        source_model
    FROM normalized_artifacts
""").fetchall()

modality_counts = distribution(normalized, "modality")

unique_by_modality = defaultdict(set)
confidence_by_modality = defaultdict(list)

ads_by_modality = defaultdict(set)
hash_ads = defaultdict(set)
hash_modalities = defaultdict(set)
hash_text = {}

for row in normalized:

    modality = row["modality"]
    digest = row["normalized_hash"]

    unique_by_modality[modality].add(digest)

    ads_by_modality[modality].add(
        row["advertisement_id"]
    )

    hash_ads[digest].add(
        row["advertisement_id"]
    )

    hash_modalities[digest].add(modality)
    hash_text[digest] = row["normalized_text"]

    confidence_by_modality[modality].append(
        row["confidence"]
    )

recurring_across_ads = [
    {
        "expression": hash_text[digest],
        "advertisement_count": len(advertisement_ids)
    }
    for digest, advertisement_ids in hash_ads.items()
    if len(advertisement_ids) > 1
]

cross_modal_hashes = [
    digest
    for digest, modalities in hash_modalities.items()
    if len(modalities) > 1
]

normalized_metrics = {
    "total_normalized_artifacts": len(normalized),
    "artifacts_by_modality": dict(modality_counts),

    "unique_expressions_by_modality": {
        modality: len(expressions)
        for modality, expressions in unique_by_modality.items()
    },

    "advertisements_with_audio_evidence": len(
        ads_by_modality.get("audio", set())
    ),

    "advertisements_with_visual_evidence": len(
        ads_by_modality.get("visual", set())
    ),

    "average_confidence_by_modality": {
        modality: average(values)
        for modality, values in confidence_by_modality.items()
    },

    "expressions_repeated_across_advertisements": len(
        recurring_across_ads
    ),

    "exact_cross_modal_expression_groups": len(
        cross_modal_hashes
    ),

    "recurring_expression_examples": recurring_across_ads[:20]
}


# ==========================================
# 5. PATTERN LIBRARY
# ==========================================

patterns = cursor.execute("""
    SELECT id, canonical_name, is_active
    FROM patterns
""").fetchall()

categories = cursor.execute("""
    SELECT id, category_key
    FROM pattern_categories
""").fetchall()

variants = cursor.execute("""
    SELECT id, pattern_id, language
    FROM pattern_variants
""").fetchall()

pattern_metrics = {
    "total_patterns": len(patterns),
    "active_patterns": sum(
        row["is_active"] == 1
        for row in patterns
    ),
    "categories": {
        row["category_key"]: sum(
            1
            for pattern in patterns
            if cursor.execute("""
                SELECT 1
                FROM pattern_category_map
                WHERE pattern_id = ?
                AND category_id = ?
                LIMIT 1
            """, (pattern["id"], row["id"])).fetchone()
        )
        for row in categories
    },
    "total_variants": len(variants),
    "variant_languages": dict(
        count_values(variants, "language")
    )
}


# ==========================================
# 6. MATCHING COVERAGE
# ==========================================

matches = []

if "pattern_matches" in tables:

    matches = cursor.execute("""
        SELECT
            normalized_artifact_id,
            pattern_id,
            match_type,
            similarity_score
        FROM pattern_matches
    """).fetchall()

matched_artifact_ids = {
    row["normalized_artifact_id"]
    for row in matches
}

unmatched = [
    row for row in normalized
    if row["id"] not in matched_artifact_ids
]

matching_metrics = {
    "total_matches": len(matches),

    "exact_matches": sum(
        row["match_type"] == "exact"
        for row in matches
    ),

    "fuzzy_matches": sum(
        row["match_type"] == "fuzzy"
        for row in matches
    ),

    "distinct_artifacts_matched": len(
        matched_artifact_ids
    ),

    "unmatched_normalized_artifacts": len(unmatched),

    "average_match_similarity": average(
        [row["similarity_score"] for row in matches]
    )
}


# ==========================================
# 7. AI PATTERN PROPOSALS
# ==========================================

proposals = cursor.execute("""
    SELECT status, COUNT(*) AS total
    FROM pattern_proposals
    GROUP BY status
""").fetchall()

proposal_metrics = {
    "total_proposals": sum(row["total"] for row in proposals),
    "proposals_by_status": {
        row["status"]: row["total"]
        for row in proposals
    }
}


# ==========================================
# 8. ADVERTISEMENT RELATIONSHIPS
# ==========================================

relationships = cursor.execute("""
    SELECT relationship_type, COUNT(*) AS total
    FROM advertisement_relationships
    GROUP BY relationship_type
""").fetchall()

relationship_metrics = {
    "total_relationships": sum(
        row["total"] for row in relationships
    ),
    "relationship_types": {
        row["relationship_type"]: row["total"]
        for row in relationships
    }
}


# ==========================================
# 9. PER-ADVERTISEMENT COVERAGE
# ==========================================

ad_coverage = {}

for ad in ads:

    ad_id = ad["id"]

    ad_normalized = [
        row for row in normalized
        if row["advertisement_id"] == ad_id
    ]

    modalities = {
        row["modality"]
        for row in ad_normalized
    }

    ad_coverage[str(ad_id)] = {
        "company": ad["company"],
        "platform": ad["platform"],
        "normalized_artifacts": len(ad_normalized),
        "modalities": sorted(modalities),
        "has_audio": "audio" in modalities,
        "has_visual": "visual" in modalities,
        "has_multimodal_evidence": (
            "audio" in modalities and "visual" in modalities
        )
    }


ads_with_both = sum(
    item["has_multimodal_evidence"]
    for item in ad_coverage.values()
)

ads_with_normalized_evidence = sum(
    item["normalized_artifacts"] > 0
    for item in ad_coverage.values()
)

coverage_metrics = {
    "advertisements_with_normalized_evidence": ads_with_normalized_evidence,
    "advertisements_without_normalized_evidence": (
        len(ads) - ads_with_normalized_evidence
    ),
    "advertisements_with_both_modalities": ads_with_both,
    "per_advertisement": ad_coverage
}


# ==========================================
# 10. READINESS FINDINGS
# ==========================================

findings = []

if len(ads) < MIN_ADVERTISEMENTS:

    findings.append({
        "area": "dataset_size",
        "status": "limited",
        "message": (
            f"Only {len(ads)} advertisement records. "
            f"The operational pilot target is {MIN_ADVERTISEMENTS}. "
            "AI discovery should be treated as exploratory."
        )
    })

if len(companies) < MIN_COMPANIES:

    findings.append({
        "area": "company_diversity",
        "status": "limited",
        "message": (
            f"Only {len(companies)} distinct company names. "
            "More independent advertisers are needed."
        )
    })

if len(platforms) < MIN_PLATFORMS:

    findings.append({
        "area": "platform_diversity",
        "status": "limited",
        "message": (
            f"Only {len(platforms)} distinct platform names. "
            "The current sample cannot represent a broad ecosystem."
        )
    })

if not metadata_languages:

    findings.append({
        "area": "language_coverage",
        "status": "unknown",
        "message": (
            "Source-language metadata was not found in the "
            "recognized metadata fields. Do not infer language "
            "coverage from Pattern Library variant languages."
        )
    })

if not normalized or len(modality_counts) < 2:

    findings.append({
        "area": "multimodal_coverage",
        "status": "limited",
        "message": (
            "Both visual and audio evidence are needed "
            "for multimodal analysis."
        )
    })

if not recurring_across_ads:

    findings.append({
        "area": "cross_ad_recurrence",
        "status": "not_established",
        "message": (
            "No exact normalized expression currently appears "
            "across multiple advertisement records. "
            "This does not rule out semantic recurrence."
        )
    })

if not matches:

    findings.append({
        "area": "known_pattern_coverage",
        "status": "no_matches",
        "message": (
            "No current Pattern Library matches were found. "
            "This is not evidence that advertisements are safe."
        )
    })


# ==========================================
# 11. FINAL REPORT
# ==========================================

report = {
    "metadata": {
        "project": "Sentinel",
        "report_type": "discovery_readiness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": DB.name,
        "read_only": True,
        "heuristics": {
            "minimum_advertisements": MIN_ADVERTISEMENTS,
            "minimum_companies": MIN_COMPANIES,
            "minimum_platforms": MIN_PLATFORMS,
            "note": "Operational pilot heuristics, not regulatory thresholds."
        }
    },

    "advertisement_ecosystem": ad_metrics,
    "media_collection": media_metrics,
    "extraction_coverage": extraction_metrics,
    "normalized_content": normalized_metrics,
    "pattern_library": pattern_metrics,
    "pattern_matching": matching_metrics,
    "ai_pattern_proposals": proposal_metrics,
    "advertisement_relationships": relationship_metrics,
    "evidence_coverage": coverage_metrics,
    "readiness_findings": findings
}

json_path = OUTPUT / "discovery_readiness.json"
text_path = OUTPUT / "discovery_readiness.txt"

with open(json_path, "w", encoding="utf-8") as file:

    json.dump(
        report,
        file,
        ensure_ascii=False,
        indent=2,
        default=str
    )

# Human-readable report
lines = [
    "===== SENTINEL DISCOVERY READINESS =====",
    "",
    "DATABASE: READ ONLY",
    "",
    "ADVERTISEMENT ECOSYSTEM",
    f"Advertisement records: {ad_metrics['advertisement_records']}",
    f"Unique companies: {ad_metrics['unique_companies']}",
    f"Unique platforms: {ad_metrics['unique_platforms']}",
    f"Unique advertiser names: {ad_metrics['unique_advertiser_names']}",
    f"Unique domains: {ad_metrics['unique_domains']}",
    "",
    "MEDIA COLLECTION",
    f"Total media assets: {media_metrics['total_media_assets']}",
    f"Asset types: {media_metrics['asset_types']}",
    "",
    "EXTRACTION",
    f"Total artifacts: {extraction_metrics['total_extracted_artifacts']}",
    f"Artifact types: {extraction_metrics['artifact_types']}",
    f"Extraction methods: {extraction_metrics['extraction_methods']}",
    "",
    "LANGUAGE COVERAGE",
    f"Explicit language records: {extraction_metrics['records_with_explicit_language_metadata']}",
    f"Language distribution: {extraction_metrics['explicit_language_distribution']}",
    "",
    "NORMALIZED EVIDENCE",
    f"Total: {normalized_metrics['total_normalized_artifacts']}",
    f"By modality: {normalized_metrics['artifacts_by_modality']}",
    f"Cross-ad recurring expressions: {normalized_metrics['expressions_repeated_across_advertisements']}",
    f"Exact cross-modal groups: {normalized_metrics['exact_cross_modal_expression_groups']}",
    "",
    "PATTERN LIBRARY",
    f"Patterns: {pattern_metrics['total_patterns']}",
    f"Active patterns: {pattern_metrics['active_patterns']}",
    f"Variants: {pattern_metrics['total_variants']}",
    f"Languages: {pattern_metrics['variant_languages']}",
    "",
    "MATCHING",
    f"Total matches: {matching_metrics['total_matches']}",
    f"Unmatched artifacts: {matching_metrics['unmatched_normalized_artifacts']}",
    "",
    "PROPOSALS",
    f"Total: {proposal_metrics['total_proposals']}",
    f"By status: {proposal_metrics['proposals_by_status']}",
    "",
    "READINESS FINDINGS"
]

for finding in findings:

    lines.append(
        f"[{finding['status'].upper()}] "
        f"{finding['area']}: {finding['message']}"
    )

lines.extend([
    "",
    "Full machine-readable report: discovery_readiness.json",
    "No database records were modified."
])

text_path.write_text(
    "\n".join(lines),
    encoding="utf-8"
)

# ==========================================
# TERMINAL SUMMARY
# ==========================================

print("\n===== READINESS SUMMARY =====")

print(f"Advertisement records: {ad_metrics['advertisement_records']}")
print(f"Unique companies: {ad_metrics['unique_companies']}")
print(f"Unique platforms: {ad_metrics['unique_platforms']}")
print(f"Unique domains: {ad_metrics['unique_domains']}")

print(f"\nExtracted artifacts: {extraction_metrics['total_extracted_artifacts']}")
print(f"Normalized artifacts: {normalized_metrics['total_normalized_artifacts']}")
print(f"Modality distribution: {normalized_metrics['artifacts_by_modality']}")

print(f"\nActive patterns: {pattern_metrics['active_patterns']}")
print(f"Pattern variants: {pattern_metrics['total_variants']}")
print(f"Pattern matches: {matching_metrics['total_matches']}")

print(f"\nPending proposals: {proposal_metrics['proposals_by_status'].get('pending', 0)}")

print("\n===== READINESS FINDINGS =====")

for finding in findings:

    print(f"\n[{finding['status'].upper()}] {finding['area']}")
    print(finding["message"])

print("\n===== REPORTS CREATED =====")
print(json_path)
print(text_path)

print("\nReadiness assessment completed.")
print("Database was not modified.")

connection.close()

