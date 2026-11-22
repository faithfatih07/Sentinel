
import sqlite3
from pathlib import Path

DB = Path(__file__).with_name("sentinel.db")

connection = sqlite3.connect(DB)
cursor = connection.cursor()

# Find our local video test
cursor.execute("""
    SELECT id
    FROM advertisements
    WHERE url = ?
""", ("local-test://sentinel/video-sample-001",))

row = cursor.fetchone()

if not row:
    raise RuntimeError("Video test record not found.")

advertisement_id = row[0]

# Retrieve both OCR and Whisper results
cursor.execute("""
    SELECT
        artifact_type,
        start_seconds,
        end_seconds,
        text_content,
        extraction_method,
        extraction_region,
        confidence
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    ORDER BY
        COALESCE(start_seconds, 0),
        artifact_type
""", (advertisement_id,))

artifacts = cursor.fetchall()

print("\n--- SENTINEL UNIFIED MEDIA INSPECTION ---")
print(f"Advertisement ID: {advertisement_id}")
print(f"Total artifacts: {len(artifacts)}")

for artifact in artifacts:

    kind, start, end, content, method, region, confidence = artifact

    print("\n----------------------------------")

    if start is not None:
        print(f"Timestamp: {start:.2f}s - {end:.2f}s")

    print(f"Type: {kind}")
    print(f"Method: {method}")
    print(f"Region: {region or 'Not specified'}")

    if confidence is not None:
        print(f"OCR confidence: {confidence:.1f}")

    print(f"Content: {content}")

print("\n--- END OF REPORT ---")

connection.close()

