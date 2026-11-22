
import sqlite3
from pathlib import Path

# Use the same database as the rest of Sentinel
DB_PATH = Path(__file__).with_name("sentinel.db")

connection = sqlite3.connect(DB_PATH)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()

# ----------------------------------------
# 1. MEDIA ASSETS
# Stores images, videos and audio files
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    advertisement_id INTEGER NOT NULL,

    parent_asset_id INTEGER,

    asset_type TEXT NOT NULL
        CHECK(asset_type IN ('image', 'video', 'audio')),

    original_url TEXT,
    local_path TEXT,

    mime_type TEXT,
    sha256_hash TEXT,

    duration_seconds REAL,
    width INTEGER,
    height INTEGER,
    file_size_bytes INTEGER,

    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(advertisement_id)
        REFERENCES advertisements(id)
        ON DELETE CASCADE,

    FOREIGN KEY(parent_asset_id)
        REFERENCES media_assets(id)
        ON DELETE SET NULL
)
""")

# ----------------------------------------
# 2. EXTRACTED ARTIFACTS
# Stores information extracted from media
# ----------------------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS extracted_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    advertisement_id INTEGER NOT NULL,
    media_asset_id INTEGER,

    artifact_type TEXT NOT NULL
        CHECK(artifact_type IN (
            'ocr_text',
            'transcript',
            'frame_description',
            'metadata'
        )),

    text_content TEXT,

    start_seconds REAL,
    end_seconds REAL,

    extraction_method TEXT,
    model_name TEXT,
    confidence REAL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(advertisement_id)
        REFERENCES advertisements(id)
        ON DELETE CASCADE,

    FOREIGN KEY(media_asset_id)
        REFERENCES media_assets(id)
        ON DELETE SET NULL
)
""")

# ----------------------------------------
# 3. INDEXES
# Make database lookups more efficient
# ----------------------------------------

cursor.execute("""
CREATE INDEX IF NOT EXISTS
idx_media_advertisement
ON media_assets(advertisement_id)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS
idx_artifact_advertisement
ON extracted_artifacts(advertisement_id)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS
idx_artifact_media
ON extracted_artifacts(media_asset_id)
""")

connection.commit()

# ----------------------------------------
# 4. DISPLAY DATABASE STRUCTURE
# ----------------------------------------

print("\n--- SENTINEL MEDIA DATABASE ---")

for table in ["media_assets", "extracted_artifacts"]:

    cursor.execute(f"PRAGMA table_info({table})")

    columns = cursor.fetchall()

    print(f"\nTable: {table}")

    for column in columns:
        print(f"  - {column[1]}: {column[2]}")

print("\nMedia schema initialized successfully!")

connection.close()

