
import sqlite3
from pathlib import Path

db = Path(__file__).with_name("sentinel.db")

connection = sqlite3.connect(db)
cursor = connection.cursor()

cursor.execute("""
    SELECT
        start_seconds,
        confidence,
        text_content
    FROM extracted_artifacts
    WHERE artifact_type = 'ocr_text'
    AND model_name = 'tesseract_tsv_v2_tur_eng_psm6'
    AND start_seconds IN (14, 15)
    ORDER BY start_seconds
""")

for timestamp, confidence, content in cursor.fetchall():

    print("\n" + "=" * 50)
    print(f"TIMESTAMP: {timestamp}s")
    print(f"OCR CONFIDENCE: {confidence}")
    print(f"CHARACTERS: {len(content)}")
    print("=" * 50)

    print(content[:2500])

connection.close()

