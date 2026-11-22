
import sqlite3
import hashlib
import json
import csv
import io
import re
import subprocess
import uuid

from pathlib import Path
from evidence_processing import process_database, begin_stage, finish_stage

# ----------------------------------------
# CONFIGURATION
# ----------------------------------------

ROOT = Path(__file__).with_name("sentinel_video_test")
FRAMES_DIR = ROOT / "frames"
DATABASE = Path(__file__).with_name("sentinel.db")

LANGUAGE = "tur+eng"
PSM = "6"
MIN_CONFIDENCE = 20

MODEL_NAME = f"tesseract | {LANGUAGE} | psm{PSM} | conf{MIN_CONFIDENCE}"
EXTRACTION_RUN_ID = str(uuid.uuid4())

try:
    SOFTWARE_VERSION = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
except Exception:
    SOFTWARE_VERSION = "unknown_not_recorded"

# ----------------------------------------
# HELPERS
# ----------------------------------------

def calculate_hash(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def run_ocr(image_path):
    result = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "-l", LANGUAGE,
            "--psm", PSM,
            "tsv"
        ],
        capture_output=True,
        text=True,
        check=True
    )

    return result.stdout


def parse_tsv(tsv_data):
    reader = csv.DictReader(
        io.StringIO(tsv_data),
        delimiter="\t"
    )

    lines = {}
    words_metadata = []
    confidences = []

    for row in reader:

        if row.get("level") != "5":
            continue

        word = (row.get("text") or "").strip()

        if not word:
            continue

        confidence = float(row["conf"])

        # Preserve recognized words and their positions.
        words_metadata.append({
            "text": word,
            "confidence": confidence,
            "bbox": {
                "x": int(row["left"]),
                "y": int(row["top"]),
                "width": int(row["width"]),
                "height": int(row["height"])
            }
        })

        # Discard very low-confidence words
        # from the readable text.
        if confidence < MIN_CONFIDENCE:
            continue

        line_key = (
            row["block_num"],
            row["par_num"],
            row["line_num"]
        )

        lines.setdefault(line_key, []).append(word)
        confidences.append(confidence)

    extracted_text = "\n".join(
        " ".join(words)
        for words in lines.values()
    )

    average_confidence = (
        sum(confidences) / len(confidences)
        if confidences else None
    )

    return {
        "text": extracted_text.strip(),
        "confidence": average_confidence,
        "words": words_metadata
    }


# ----------------------------------------
# DATABASE CONNECTION
# ----------------------------------------

connection = sqlite3.connect(DATABASE)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()
INGESTION_STAGE_ID = begin_stage(DATABASE, "ocr_ingestion", {
    "extraction_run_id": EXTRACTION_RUN_ID,
    "engine": "tesseract",
    "model": MODEL_NAME,
    "software_version": SOFTWARE_VERSION,
    "language": LANGUAGE,
    "configuration": {"psm": PSM, "minimum_confidence": MIN_CONFIDENCE},
})

# ----------------------------------------
# SCHEMA MIGRATION
# Add fields without destroying existing data
# ----------------------------------------

artifact_columns = {
    row[1]
    for row in cursor.execute(
        "PRAGMA table_info(extracted_artifacts)"
    ).fetchall()
}

if "extraction_region" not in artifact_columns:
    cursor.execute("""
        ALTER TABLE extracted_artifacts
        ADD COLUMN extraction_region TEXT
    """)

if "metadata_json" not in artifact_columns:
    cursor.execute("""
        ALTER TABLE extracted_artifacts
        ADD COLUMN metadata_json TEXT
    """)

connection.commit()

# ----------------------------------------
# FIND TEST ADVERTISEMENT
# ----------------------------------------

cursor.execute("""
    SELECT id
    FROM advertisements
    WHERE url = ?
""", ("local-test://sentinel/video-sample-001",))

row = cursor.fetchone()

if not row:
    raise RuntimeError(
        "Video test advertisement not found. "
        "Run ingest_whisper.py first."
    )

advertisement_id = row[0]

# Find original video asset
cursor.execute("""
    SELECT id, width, height
    FROM media_assets
    WHERE advertisement_id = ?
    AND asset_type = 'video'
    ORDER BY id
    LIMIT 1
""", (advertisement_id,))

video_asset = cursor.fetchone()

if not video_asset:
    raise RuntimeError("Original video asset not found.")

video_asset_id, video_width, video_height = video_asset

# ----------------------------------------
# DISCOVER FRAMES
# ----------------------------------------

frame_paths = sorted(
    path
    for path in FRAMES_DIR.glob("*.jpg")
    if re.fullmatch(r"frame_(\d+)\.jpg", path.name)
)

if not frame_paths:
    raise RuntimeError("No video frames found.")

print("\n--- SENTINEL OCR INGESTION ---")
print(f"Advertisement ID: {advertisement_id}")
print(f"Video asset ID: {video_asset_id}")
print(f"Frames discovered: {len(frame_paths)}")

saved = 0
skipped = 0
empty = 0

# ----------------------------------------
# PROCESS EACH FRAME
# ----------------------------------------

for frame_path in frame_paths:

    frame_number = int(
        re.fullmatch(
            r"frame_(\d+)\.jpg",
            frame_path.name
        ).group(1)
    )

    # Frames were extracted at 1 FPS.
    timestamp = float(frame_number - 1)

    image_hash = calculate_hash(frame_path)

    # ------------------------------------
    # Register image as a media asset
    # ------------------------------------

    cursor.execute("""
        SELECT id
        FROM media_assets
        WHERE advertisement_id = ?
        AND sha256_hash = ?
    """, (advertisement_id, image_hash))

    existing_asset = cursor.fetchone()

    if existing_asset:
        frame_asset_id = existing_asset[0]

    else:
        cursor.execute("""
            INSERT INTO media_assets (
                advertisement_id,
                parent_asset_id,
                asset_type,
                local_path,
                mime_type,
                sha256_hash,
                width,
                height,
                file_size_bytes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            advertisement_id,
            video_asset_id,
            "image",
            str(frame_path),
            "image/jpeg",
            image_hash,
            video_width,
            video_height,
            frame_path.stat().st_size
        ))

        frame_asset_id = cursor.lastrowid

    # ------------------------------------
    # Check if this OCR operation exists
    # ------------------------------------

    cursor.execute("""
        SELECT id
        FROM extracted_artifacts
        WHERE media_asset_id = ?
        AND artifact_type = 'ocr_text'
        AND extraction_method = 'tesseract'
        AND model_name = ?
    """, (frame_asset_id, MODEL_NAME))

    if cursor.fetchone():
        skipped += 1
        continue

    # ------------------------------------
    # Run OCR
    # ------------------------------------

    try:
        tsv_output = run_ocr(frame_path)
        result = parse_tsv(tsv_output)

    except subprocess.CalledProcessError as error:
        print(f"OCR failed: {frame_path.name}")
        print(error.stderr)
        continue

    extracted_text = result["text"]

    if not extracted_text:
        empty += 1
        continue

    # ------------------------------------
    # Save extraction evidence
    # ------------------------------------

    metadata = {
        "extraction_run_id": EXTRACTION_RUN_ID,
        "software_version": SOFTWARE_VERSION,
        "frame_number": frame_number,
        "language": LANGUAGE,
        "page_segmentation_mode": PSM,
        "capture_type": "screen_capture",
        "image_width": video_width,
        "image_height": video_height,
        "word_boxes": result["words"]
    }

    cursor.execute("""
        INSERT INTO extracted_artifacts (
            advertisement_id,
            media_asset_id,
            artifact_type,
            text_content,
            start_seconds,
            end_seconds,
            extraction_method,
            model_name,
            confidence,
            extraction_region,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        advertisement_id,
        frame_asset_id,
        "ocr_text",
        extracted_text,
        timestamp,
        timestamp,
        "tesseract",
        MODEL_NAME,
        result["confidence"],
        "full_frame",
        json.dumps(metadata, ensure_ascii=False)
    ))

    saved += 1

    print(
        f"[{timestamp:.0f}s] "
        f"{frame_path.name}: "
        f"{len(extracted_text)} characters"
    )

    connection.commit()


# ----------------------------------------
# SUMMARY
# ----------------------------------------

print("\n--- OCR INGESTION RESULTS ---")
print(f"Frames processed: {len(frame_paths)}")
print(f"New OCR records saved: {saved}")
print(f"Existing records skipped: {skipped}")
print(f"Frames without readable text: {empty}")

cursor.execute("""
    SELECT COUNT(*)
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    AND artifact_type = 'ocr_text'
""", (advertisement_id,))

total_ocr = cursor.fetchone()[0]

cursor.execute("""
    SELECT COUNT(*)
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    AND artifact_type = 'transcript'
""", (advertisement_id,))

total_transcripts = cursor.fetchone()[0]

print(f"\nTotal OCR artifacts: {total_ocr}")
print(f"Total transcript segments: {total_transcripts}")

print("\nOCR data successfully imported into Sentinel!")

connection.close()

# Extraction records are committed first. A downstream failure is recorded by
# the evidence stage runner and does not roll back or replace source OCR rows.
print("\n--- EVIDENCE PROCESSING ---")
result = process_database(DATABASE)
finish_stage(DATABASE, INGESTION_STAGE_ID, {"saved": saved, "skipped": skipped, "empty": empty, "evidence_result": result})
print(result)

