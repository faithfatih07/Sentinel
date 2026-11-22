
import sqlite3
import json
import hashlib

from pathlib import Path
from PIL import Image
import pytesseract
from pytesseract import Output

# ----------------------------------------
# CONFIGURATION
# ----------------------------------------

ROOT = Path(__file__).with_name("sentinel_video_test")
FRAMES = ROOT / "frames"
DB = Path(__file__).with_name("sentinel.db")

LANGUAGE = "tur+eng"
CONFIG = "--psm 6"
MIN_CONFIDENCE = 20

MODEL_NAME = "pytesseract_tur_eng_psm6_v1"

# ----------------------------------------
# OCR PROCESSING
# ----------------------------------------

def extract_ocr(image_path):

    image = Image.open(image_path).convert("RGB")

    data = pytesseract.image_to_data(
        image,
        lang=LANGUAGE,
        config=CONFIG,
        output_type=Output.DICT
    )

    lines = {}
    word_boxes = []
    confidences = []

    for i, raw_word in enumerate(data["text"]):

        word = raw_word.strip()

        if not word:
            continue

        confidence = float(data["conf"][i])

        if confidence < 0:
            continue

        bbox = {
            "x": int(data["left"][i]),
            "y": int(data["top"][i]),
            "width": int(data["width"][i]),
            "height": int(data["height"][i])
        }

        word_boxes.append({
            "text": word,
            "confidence": confidence,
            "bbox": bbox
        })

        if confidence < MIN_CONFIDENCE:
            continue

        line_key = (
            data["block_num"][i],
            data["par_num"][i],
            data["line_num"][i]
        )

        lines.setdefault(line_key, []).append(word)
        confidences.append(confidence)

    text = "\n".join(
        " ".join(words)
        for words in lines.values()
    )

    average_confidence = (
        sum(confidences) / len(confidences)
        if confidences else None
    )

    return {
        "text": text.strip(),
        "confidence": average_confidence,
        "word_boxes": word_boxes,
        "image_width": image.width,
        "image_height": image.height
    }


# ----------------------------------------
# DATABASE CONNECTION
# ----------------------------------------

connection = sqlite3.connect(DB)
connection.execute("PRAGMA foreign_keys = ON")
cursor = connection.cursor()

cursor.execute("""
    SELECT id
    FROM advertisements
    WHERE url = ?
""", ("local-test://sentinel/video-sample-001",))

row = cursor.fetchone()

if not row:
    raise RuntimeError("Advertisement not found.")

advertisement_id = row[0]

cursor.execute("""
    SELECT id
    FROM media_assets
    WHERE advertisement_id = ?
    AND asset_type = 'video'
    ORDER BY id
    LIMIT 1
""", (advertisement_id,))

video = cursor.fetchone()

if not video:
    raise RuntimeError("Video asset not found.")

video_id = video[0]

# ----------------------------------------
# PROCESS FRAMES
# ----------------------------------------

frames = sorted(FRAMES.glob("frame_*.jpg"))

saved = 0
empty = 0

print("\n--- SENTINEL PYTESSERACT INGESTION ---")

for frame in frames:

    frame_number = int(frame.stem.split("_")[1])
    timestamp = float(frame_number - 1)

    result = extract_ocr(frame)

    text = result["text"]

    # Find frame asset by exact path
    cursor.execute("""
        SELECT id
        FROM media_assets
        WHERE advertisement_id = ?
        AND asset_type = 'image'
        AND local_path = ?
    """, (advertisement_id, str(frame)))

    asset = cursor.fetchone()

    if asset:
        frame_asset_id = asset[0]

    else:
        digest = hashlib.sha256(frame.read_bytes()).hexdigest()

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
            video_id,
            "image",
            str(frame),
            "image/jpeg",
            digest,
            result["image_width"],
            result["image_height"],
            frame.stat().st_size
        ))

        frame_asset_id = cursor.lastrowid

    # Skip empty outputs
    if not text:
        empty += 1
        continue

    metadata = {
        "frame_number": frame_number,
        "language": LANGUAGE,
        "config": CONFIG,
        "capture_type": "screen_capture",
        "word_boxes": result["word_boxes"],
        "image_width": result["image_width"],
        "image_height": result["image_height"]
    }

    # Check whether this version already exists
    cursor.execute("""
        SELECT id
        FROM extracted_artifacts
        WHERE media_asset_id = ?
        AND artifact_type = 'ocr_text'
        AND extraction_method = 'pytesseract'
        AND model_name = ?
    """, (frame_asset_id, MODEL_NAME))

    existing = cursor.fetchone()

    values = (
        advertisement_id,
        frame_asset_id,
        "ocr_text",
        text,
        timestamp,
        timestamp,
        "pytesseract",
        MODEL_NAME,
        result["confidence"],
        "full_frame",
        json.dumps(metadata, ensure_ascii=False)
    )

    if existing:

        cursor.execute("""
            UPDATE extracted_artifacts
            SET advertisement_id = ?,
                media_asset_id = ?,
                artifact_type = ?,
                text_content = ?,
                start_seconds = ?,
                end_seconds = ?,
                extraction_method = ?,
                model_name = ?,
                confidence = ?,
                extraction_region = ?,
                metadata_json = ?
            WHERE id = ?
        """, values + (existing[0],))

    else:

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
        """, values)

    saved += 1
    connection.commit()

    print(
        f"[{timestamp:.0f}s] "
        f"{frame.name}: {len(text)} characters"
    )

# ----------------------------------------
# VERIFICATION
# ----------------------------------------

cursor.execute("""
    SELECT COUNT(*),
           MAX(length(text_content)),
           SUM(CASE WHEN instr(text_content, char(9)) > 0
               THEN 1 ELSE 0 END)
    FROM extracted_artifacts
    WHERE advertisement_id = ?
    AND extraction_method = 'pytesseract'
    AND model_name = ?
""", (advertisement_id, MODEL_NAME))

total, max_length, tab_leaks = cursor.fetchone()

print("\n--- PYTESSERACT RESULTS ---")
print(f"Processed frames: {len(frames)}")
print(f"New or updated records: {saved}")
print(f"Empty frames: {empty}")
print(f"Total clean OCR records: {total}")
print(f"Maximum text length: {max_length}")
print(f"Records containing tabs: {tab_leaks or 0}")

print("\nPytesseract processing completed!")

connection.close()

