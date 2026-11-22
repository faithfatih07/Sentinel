
import sqlite3
import re
import unicodedata

from pathlib import Path
from rapidfuzz import fuzz

# ========================================
# SENTINEL PATTERN MATCHING ENGINE
# ========================================

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "sentinel.db"

FUZZY_THRESHOLD = 90.0
MIN_VARIANT_WORDS = 2

# ========================================
# TEXT NORMALIZATION
# ========================================

def normalize_for_matching(text):
    """
    Normalize source text and pattern variants
    using the same language-neutral process.
    """

    text = unicodedata.normalize("NFKC", text or "")
    text = text.casefold()

    # Preserve Unicode letters and numbers.
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)

    return " ".join(tokens)


# ========================================
# GENERATE WORD WINDOWS
# ========================================

def generate_windows(tokens, target_length):

    lengths = {
        max(2, target_length - 1),
        target_length,
        target_length + 1
    }

    windows = set()

    for size in lengths:

        if size > len(tokens):
            continue

        for start in range(len(tokens) - size + 1):

            window = " ".join(
                tokens[start:start + size]
            )

            windows.add(window)

    return windows


# ========================================
# COMPARE ONE ARTIFACT WITH ONE VARIANT
# ========================================

def compare_text(artifact_text, variant_text):

    artifact = normalize_for_matching(artifact_text)
    variant = normalize_for_matching(variant_text)

    if not artifact or not variant:
        return None

    variant_tokens = variant.split()
    artifact_tokens = artifact.split()

    if len(variant_tokens) < MIN_VARIANT_WORDS:
        return None

    # Exact phrase match
    if f" {variant} " in f" {artifact} ":

        return {
            "match_type": "exact",
            "score": 100.0,
            "matched_excerpt": variant
        }

    # Fuzzy matching on nearby word windows
    windows = generate_windows(
        artifact_tokens,
        len(variant_tokens)
    )

    best_score = 0.0
    best_excerpt = None

    for window in windows:

        score = fuzz.ratio(
            variant,
            window
        )

        if score > best_score:

            best_score = score
            best_excerpt = window

    if best_score >= FUZZY_THRESHOLD:

        return {
            "match_type": "fuzzy",
            "score": round(best_score, 2),
            "matched_excerpt": best_excerpt
        }

    return None


# ========================================
# DATABASE CONNECTION
# ========================================

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")
from evidence_processing import apply_migrations
apply_migrations(connection)

cursor = connection.cursor()

# ========================================
# CREATE MATCH EVIDENCE TABLE
# ========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS pattern_matches (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    normalized_artifact_id INTEGER NOT NULL,

    pattern_id INTEGER NOT NULL,

    matched_variant_id INTEGER NOT NULL,

    matched_language TEXT NOT NULL,

    match_type TEXT NOT NULL
        CHECK(match_type IN ('exact', 'fuzzy')),

    similarity_score REAL NOT NULL,

    matched_excerpt TEXT NOT NULL,

    source_confidence REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (
        normalized_artifact_id,
        pattern_id
    ),

    FOREIGN KEY(normalized_artifact_id)
        REFERENCES normalized_artifacts(id),

    FOREIGN KEY(pattern_id)
        REFERENCES patterns(id),

    FOREIGN KEY(matched_variant_id)
        REFERENCES pattern_variants(id)
)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS
idx_pattern_matches_pattern
ON pattern_matches(pattern_id)
""")

connection.commit()


# ========================================
# LOAD ACTIVE PATTERNS
# ========================================

patterns = cursor.execute("""
    SELECT id, canonical_name, description
    FROM patterns
    WHERE is_active = 1
    ORDER BY id
""").fetchall()

variants = cursor.execute("""
    SELECT
        v.id,
        v.pattern_id,
        v.language,
        v.variant_text
    FROM pattern_variants v
    JOIN patterns p
        ON v.pattern_id = p.id
    WHERE p.is_active = 1
    ORDER BY v.pattern_id, v.id
""").fetchall()

categories = cursor.execute("""
    SELECT
        pcm.pattern_id,
        GROUP_CONCAT(DISTINCT c.category_key)
    FROM pattern_category_map pcm
    JOIN pattern_categories c
        ON pcm.category_id = c.id
    GROUP BY pcm.pattern_id
""").fetchall()

category_map = {
    pattern_id: category_names
    for pattern_id, category_names in categories
}

# Organize variants by pattern
variants_by_pattern = {}

for variant in variants:

    variant_id, pattern_id, language, phrase = variant

    variants_by_pattern.setdefault(
        pattern_id, []
    ).append(variant)


# ========================================
# LOAD NORMALIZED ARTIFACTS
# ========================================

artifacts = cursor.execute("""
    SELECT
        id,
        advertisement_id,
        modality,
        original_text,
        start_seconds,
        end_seconds,
        confidence,
        source_model
    FROM current_selected_matching_input
    ORDER BY advertisement_id, start_seconds, id
""").fetchall()


# ========================================
# MATCHING PROCESS
# ========================================

# These records are derived matching results.
# Rebuild them on each run using current patterns.

cursor.execute("DELETE FROM pattern_matches")
connection.commit()

total_matches = 0
exact_matches = 0
fuzzy_matches = 0

print("\n===== SENTINEL PATTERN MATCHING =====")

print(f"Active patterns: {len(patterns)}")
print(f"Available variants: {len(variants)}")
print(f"Artifacts to analyze: {len(artifacts)}")
print(f"Fuzzy threshold: {FUZZY_THRESHOLD}")


for artifact in artifacts:

    (
        artifact_id,
        advertisement_id,
        modality,
        original_text,
        start,
        end,
        confidence,
        source_model
    ) = artifact

    # Keep only the strongest match per pattern
    best_matches = {}

    for pattern in patterns:

        pattern_id, canonical_name, description = pattern

        pattern_variants = variants_by_pattern.get(
            pattern_id, []
        )

        for variant in pattern_variants:

            (
                variant_id,
                variant_pattern_id,
                language,
                phrase
            ) = variant

            result = compare_text(
                original_text,
                phrase
            )

            if result is None:
                continue

            current = best_matches.get(pattern_id)

            if (
                current is None
                or result["score"] > current["score"]
            ):

                best_matches[pattern_id] = {
                    "variant_id": variant_id,
                    "language": language,
                    "phrase": phrase,
                    "match_type": result["match_type"],
                    "score": result["score"],
                    "excerpt": result["matched_excerpt"],
                    "canonical_name": canonical_name,
                    "description": description
                }

    # Save strongest match for each pattern
    for pattern_id, match in best_matches.items():

        cursor.execute("""
            INSERT INTO pattern_matches (
                normalized_artifact_id,
                pattern_id,
                matched_variant_id,
                matched_language,
                match_type,
                similarity_score,
                matched_excerpt,
                source_confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            artifact_id,
            pattern_id,
            match["variant_id"],
            match["language"],
            match["match_type"],
            match["score"],
            match["excerpt"],
            confidence
        ))

        total_matches += 1

        if match["match_type"] == "exact":
            exact_matches += 1
        else:
            fuzzy_matches += 1

        print("\n----------------------------------------")
        print(f"Advertisement: {advertisement_id}")
        print(f"Artifact ID: {artifact_id}")
        print(f"Modality: {modality}")
        print(f"Timestamp: {start}s - {end}s")
        print(f"Pattern: {match['canonical_name']}")
        print(f"Category: {category_map.get(pattern_id)}")
        print(f"Variant language: {match['language']}")
        print(f"Matched phrase: {match['phrase']}")
        print(f"Match type: {match['match_type']}")
        print(f"Similarity: {match['score']}")
        print(f"Matched excerpt: {match['excerpt']}")
        print(f"Source confidence: {confidence}")


connection.commit()


# ========================================
# FINAL SUMMARY
# ========================================

print("\n===== MATCHING SUMMARY =====")

print(f"Artifacts analyzed: {len(artifacts)}")
print(f"Patterns available: {len(patterns)}")
print(f"Variants checked: {len(variants)}")
print(f"Total pattern matches: {total_matches}")
print(f"Exact matches: {exact_matches}")
print(f"Fuzzy matches: {fuzzy_matches}")

if total_matches == 0:
    print("\nNo pattern matches found.")

print("\nMatching results saved to sentinel.db")

connection.close()

