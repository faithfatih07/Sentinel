
import sqlite3
import re
import unicodedata

from pathlib import Path
from rapidfuzz import fuzz

DB = Path(__file__).with_name("sentinel.db")
THRESHOLD = 90.0


def normalize(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.casefold()
    return " ".join(
        re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    )


def compare_text(text, phrase):

    text = normalize(text)
    phrase = normalize(phrase)

    tokens = phrase.split()
    source_tokens = text.split()

    if len(tokens) < 2:
        return None

    if f" {phrase} " in f" {text} ":
        return "exact", 100.0

    best = 0.0

    for size in {len(tokens)-1, len(tokens), len(tokens)+1}:

        if size < 1 or size > len(source_tokens):
            continue

        for start in range(len(source_tokens)-size+1):

            window = " ".join(
                source_tokens[start:start+size]
            )

            best = max(best, fuzz.ratio(phrase, window))

    if best >= THRESHOLD:
        return "fuzzy", best

    return None


def introduce_typo(phrase):

    words = phrase.split()

    # Change one character in the longest word
    candidates = [
        (i, word)
        for i, word in enumerate(words)
        if len(word) >= 5
    ]

    if not candidates:
        return phrase

    index, word = max(candidates, key=lambda x: len(x[1]))

    position = len(word) // 2
    replacement = "x" if word[position] != "x" else "z"

    words[index] = (
        word[:position] +
        replacement +
        word[position+1:]
    )

    return " ".join(words)


db = sqlite3.connect(DB)
cursor = db.cursor()

variants = cursor.execute("""
    SELECT
        p.canonical_name,
        v.language,
        v.variant_text
    FROM pattern_variants v
    JOIN patterns p ON p.id = v.pattern_id
    WHERE p.is_active = 1
    ORDER BY p.id, v.id
""").fetchall()

exact_pass = 0
fuzzy_pass = 0
fuzzy_fail = 0

print("\n===== SENTINEL MATCHING VALIDATION =====")

for name, language, phrase in variants:

    # Exact test
    sentence = f"Example financial service: {phrase} for clients."

    exact_result = compare_text(sentence, phrase)

    if exact_result and exact_result[0] == "exact":
        exact_pass += 1
        exact_status = "PASS"
    else:
        exact_status = "FAIL"

    # Fuzzy test: deliberately alter one character
    typo = introduce_typo(phrase)
    fuzzy_result = compare_text(
        f"Example financial service: {typo}",
        phrase
    )

    if fuzzy_result and fuzzy_result[0] == "fuzzy":
        fuzzy_pass += 1
        fuzzy_status = "PASS"
    else:
        fuzzy_fail += 1
        fuzzy_status = "FAIL"

    print(f"\nPattern: {name}")
    print(f"Language: {language}")
    print(f"Phrase: {phrase}")
    print(f"Exact test: {exact_status}")
    print(f"Fuzzy test: {fuzzy_status}")

print("\n===== FINAL TEST RESULTS =====")
print(f"Variants tested: {len(variants)}")
print(f"Exact passed: {exact_pass}")
print(f"Fuzzy passed: {fuzzy_pass}")
print(f"Fuzzy failed: {fuzzy_fail}")

db.close()

