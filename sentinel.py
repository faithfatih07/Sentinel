
import sqlite3
import hashlib
import unicodedata
from rapidfuzz import fuzz

# SETTINGS
DATABASE = "sentinel.db"
SIMILARITY_THRESHOLD = 75

# 1. Normalize text before hashing
def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    return " ".join(text.lower().split())


# 2. Generate SHA-256 fingerprint
def generate_hash(text):
    normalized = normalize_text(text)

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# 3. Connect to database
connection = sqlite3.connect(DATABASE)
cursor = connection.cursor()

# 4. Create advertisement table
cursor.execute("""
CREATE TABLE IF NOT EXISTS advertisements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    platform TEXT,
    advertiser TEXT,
    content TEXT,
    url TEXT,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# 5. Add fingerprint column to existing database
columns = {
    row[1] for row in
    cursor.execute("PRAGMA table_info(advertisements)").fetchall()
}

if "content_hash" not in columns:
    cursor.execute(
        "ALTER TABLE advertisements ADD COLUMN content_hash TEXT"
    )

# 6. Create relationship table
cursor.execute("""
CREATE TABLE IF NOT EXISTS advertisement_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id_1 INTEGER,
    ad_id_2 INTEGER,
    similarity REAL,
    relationship_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ad_id_1, ad_id_2)
)
""")

# 7. Generate fingerprints for old records
cursor.execute("""
    SELECT id, content
    FROM advertisements
    WHERE content_hash IS NULL OR content_hash = ''
""")

for ad_id, content in cursor.fetchall():
    fingerprint = generate_hash(content)

    cursor.execute("""
        UPDATE advertisements
        SET content_hash = ?
        WHERE id = ?
    """, (fingerprint, ad_id))

connection.commit()

# 8. Simulate a newly collected advertisement
new_ad = {
    "company": "BrokerX",
    "platform": "TikTok",
    "advertiser": "Influencer E",
    "content": "Start investing with BrokerX today!",
    "url": "https://example.com/advertisement-005"
}

print("\n--- SENTINEL MATCHING ENGINE ---")

new_hash = generate_hash(new_ad["content"])

# 9. Check URL duplication
cursor.execute(
    "SELECT id FROM advertisements WHERE url = ?",
    (new_ad["url"],)
)

if cursor.fetchone():
    print("URL already collected!")

else:

    # 10. Find exact text matches
    cursor.execute("""
        SELECT id
        FROM advertisements
        WHERE content_hash = ?
    """, (new_hash,))

    exact_matches = cursor.fetchall()

    print(f"\nExact text matches: {len(exact_matches)}")

    # 11. Find other candidates from same company
    cursor.execute("""
        SELECT id, content
        FROM advertisements
        WHERE company = ?
        AND content_hash != ?
    """, (new_ad["company"], new_hash))

    candidates = cursor.fetchall()

    print(f"Additional candidates: {len(candidates)}")

    # 12. Insert new advertisement
    cursor.execute("""
        INSERT INTO advertisements
        (company, platform, advertiser, content, url, content_hash)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        new_ad["company"],
        new_ad["platform"],
        new_ad["advertiser"],
        new_ad["content"],
        new_ad["url"],
        new_hash
    ))

    new_id = cursor.lastrowid

    # 13. Save exact text relationships
    for (old_id,) in exact_matches:

        cursor.execute("""
            INSERT OR IGNORE INTO advertisement_relationships
            (ad_id_1, ad_id_2, similarity, relationship_type)
            VALUES (?, ?, ?, ?)
        """, (
            old_id,
            new_id,
            100.0,
            "exact_text_match"
        ))

        print(f"Exact text match: Ad {old_id} -> Ad {new_id}")

    # 14. Compare remaining candidates
    for old_id, old_text in candidates:

        score = fuzz.ratio(
            normalize_text(new_ad["content"]),
            normalize_text(old_text)
        )

        print(f"Ad {old_id}: {score:.2f}%")

        if score >= SIMILARITY_THRESHOLD:

            cursor.execute("""
                INSERT OR IGNORE INTO advertisement_relationships
                (ad_id_1, ad_id_2, similarity, relationship_type)
                VALUES (?, ?, ?, ?)
            """, (
                old_id,
                new_id,
                score,
                "text_similarity"
            ))

            print(f"Text relationship saved: Ad {old_id} -> Ad {new_id}")

    connection.commit()
    print("\nNew advertisement saved!")

connection.close()

